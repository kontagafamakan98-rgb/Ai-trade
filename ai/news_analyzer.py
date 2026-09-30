"""
Analyse qualitative des news via des LLM gratuits, pour remplacer le simple
comptage d'événements par une lecture réelle du contenu et de sa pertinence
pour un actif donné.

CHAÎNE DE SECOURS (du plus au moins prioritaire) :
1. Groq — 2 modèles en vérification croisée (openai/gpt-oss-120b + qwen/qwen3.6-27b)
2. Gemini (gemini-2.5-flash) — utilisé UNIQUEMENT si Groq est totalement
   indisponible (quota épuisé, panne...). Quota séparé de Groq = vraie
   redondance gratuite.
3. Ancienne méthode de comptage brut — si aucun LLM n'a pu répondre.

Mécanisme de consensus (quand 2 réponses Groq sont disponibles) :
- D'accord sur la direction → score moyenné, confiance renforcée.
- EN DÉSACCORD → on reste prudent, score ramené vers le neutre.

GESTION DU QUOTA GRATUIT :
- Un seul appel "batch" couvre TOUTE la watchlist en une fois (au lieu
  d'un appel par actif) → ~12x moins d'appels et de tokens consommés.
- Ce prompt batch inclut aussi un bloc de MÉDIAS par actif (transcriptions de
  notes vocales, lectures de graphiques) sous budget global borné : sans cela,
  le cache pré-rempli par `warm_batch` court-circuiterait le contexte média du
  chemin "par actif" pour toute la watchlist.
- Le cache dure 1h par défaut (au lieu de 10 min).

⚠️ Aucun de ces mécanismes ne garantit une précision absolue — un marché
reste fondamentalement incertain, même avec plusieurs IA d'accord entre elles.
"""
import json
import time
from typing import Dict, Any, List, Optional

try:
    import httpx
except ImportError:  # sans signal : httpx optionnel, l'appelant le voit à l'usage
    httpx = None

from config import (
    GROQ_API_KEY,
    GEMINI_API_KEY,
    GROQ_PRIMARY_MODEL,
    GROQ_SECONDARY_MODEL,
    GEMINI_MODEL,
)
from database.knowledge_base import get_knowledge_context, get_media_context

try:
    from groq import Groq
    _client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
except Exception:
    # sans signal : client Groq optionnel, l'absence est rapportée par l'appelant
    _client = None

# IDs centralisés dans config.py (surchargeables par variables d'env).
PRIMARY_MODEL = GROQ_PRIMARY_MODEL
SECONDARY_MODEL = GROQ_SECONDARY_MODEL
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

# --- Coupe-circuit --------------------------------------------------------
# Dès qu'un fournisseur échoue une fois (quota, panne...), on arrête de le
# solliciter pendant COOLDOWN_SECONDS au lieu de le marteler pour chacun des
# actifs de la watchlist (ce qui épuiserait aussi son quota en quelques
# secondes). Passé le cooldown, on retente automatiquement.
COOLDOWN_SECONDS = 600  # 10 min
_cooldown_until = {"groq": 0.0, "gemini": 0.0}

# --- Budget du contexte MÉDIA dans le prompt BATCH ------------------------
# Le prompt "batch" couvre toute la watchlist en UN seul appel LLM : les extraits
# média de chaque actif doivent donc tenir dans un budget global, sinon le prompt
# grossit avec la taille de la watchlist — et le quota Groq avec lui. Chaque
# actif reçoit une part calculée du total, pour ne pas servir les premiers et
# oublier les suivants.
BATCH_MEDIA_PER_ASSET_CHARS = 400
MAX_BATCH_MEDIA_CHARS = 2400

#: Marge réservée à la marque de troncature que `get_media_context` peut ajouter
#: au-delà de son plafond ("[...tronqué]"). Sans elle, la somme des parts
#: dépasserait le budget global de quelques caractères par actif, et le dernier
#: actif serait écarté par la sécurité de fin de boucle — exactement l'injustice
#: que la répartition par actif existe pour éviter.
MEDIA_TRUNCATION_SLACK = len("\n[...tronqué]")

#: En-tête fixe du bloc média du batch. Il ne dépend **pas** du nombre d'actifs :
#: c'est pourquoi il est hors du budget réparti — le compter dedans réduirait la
#: part de chaque actif d'un montant qui n'a rien à voir avec son contenu.
BATCH_MEDIA_HEADER = (
    "\nMédias indexés par actif (lectures de graphiques et transcriptions "
    "Telegram — OBSERVATIONS DATÉES, PAS des règles permanentes ni des "
    "actualités ; ne les généralise pas) :\n"
)

# --- Budget du contexte CHOISI PAR L'UTILISATEUR (boucle RAG) --------------
# Ces extraits viennent d'une sélection manuelle dans les résultats de `/search`
# (voir `core/rag_loop.py`) : ils s'ajoutent au contexte automatique, donc ils
# doivent rester bornés eux aussi. Le plafond est appliqué ici, au dernier
# moment : c'est le seul endroit qui connaît le budget réel du prompt.
MAX_RAG_CONTEXT_CHARS = 1500


def _available(provider: str) -> bool:
    return time.time() >= _cooldown_until.get(provider, 0.0)


def _mark_failed(provider: str) -> None:
    _cooldown_until[provider] = time.time() + COOLDOWN_SECONDS
    print(f"   ⏸️ {provider} mis en pause {COOLDOWN_SECONDS // 60} min (échec détecté)")

SYSTEM_PROMPT = (
    "Tu es un analyste financier neutre et rigoureux, sans biais optimiste ni "
    "pessimiste. Tu évalues l'impact probable d'actualités récentes sur un ou "
    "plusieurs actifs financiers précis.\n\n"
    "Règles strictes :\n"
    "- Reste strictement factuel. Ne spécule pas au-delà de ce que les news indiquent.\n"
    "- Si aucune news n'a de lien clair et direct avec un actif, réponds bias=\"neutral\" "
    "et score=0.5 POUR CET ACTIF. Ne force JAMAIS un lien qui n'existe pas.\n"
    "- N'utilise un score extrême (proche de 0.0 ou 1.0) que si l'actualité est sans "
    "ambiguïté et à impact majeur (ex: guerre déclarée, défaut souverain, décision de "
    "taux confirmée, faillite). Les news vagues ou indirectes doivent rester proches de 0.5.\n"
    "- Tu n'es pas un conseiller financier et ne donnes aucune recommandation d'achat/vente, "
    "seulement une évaluation d'impact.\n"
    "- Réponds UNIQUEMENT avec un objet JSON valide, sans texte avant/après, sans balises markdown."
)


def _news_block(insights: List[dict]) -> str:
    return "\n".join(
        f"- [{i.get('type', '?')}] {i.get('title', '')}: {(i.get('summary') or '')[:300]}"
        for i in insights[:15]
    )


def _consensus(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    if a["bias"] == b["bias"]:
        score = (a["score"] + b["score"]) / 2
        bias = a["bias"]
        reasoning = f"[2 IA d'accord] {a['reasoning']}"
    else:
        score = 0.5 + (a["score"] - 0.5) * 0.25 + (b["score"] - 0.5) * 0.25
        bias = "neutral"
        reasoning = (
            f"Avis partagés entre les 2 IA ({a['bias']} vs {b['bias']}) → prudence, "
            f"score neutralisé. Avis A: {a['reasoning']} | Avis B: {b['reasoning']}"
        )
    return {"bias": bias, "score": max(0.0, min(1.0, score)), "reasoning": reasoning[:500]}


def _parse_single(data: dict) -> Dict[str, Any]:
    score = max(0.0, min(1.0, float(data.get("score", 0.5))))
    bias = data.get("bias", "neutral")
    if bias not in ("bullish", "bearish", "neutral"):
        bias = "neutral"
    return {"bias": bias, "score": score, "reasoning": str(data.get("reasoning", ""))[:400]}


# ---------------------------------------------------------------------------
# Groq — appels bruts (bas niveau)
# ---------------------------------------------------------------------------

def _groq_call(model_id: str, prompt: str, max_tokens: int) -> dict:
    resp = _client.chat.completions.create(
        model=model_id,
        max_tokens=max_tokens,
        temperature=0.2,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    )
    text = resp.choices[0].message.content.strip()
    text = text.replace("```json", "").replace("```", "").strip()
    return json.loads(text)


# ---------------------------------------------------------------------------
# Gemini — secours si Groq totalement indisponible (quota séparé)
# ---------------------------------------------------------------------------

def _gemini_call(prompt: str, max_tokens: int) -> dict:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY absente")

    body = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "maxOutputTokens": max_tokens * 3,
            "thinkingConfig": {"thinkingLevel": "minimal"},
        },
    }
    with httpx.Client(timeout=30) as http_client:
        resp = http_client.post(
            GEMINI_URL,
            headers={
                "Content-Type": "application/json",
                "X-goog-api-key": GEMINI_API_KEY,
            },
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()

    candidates = data.get("candidates") or []
    if not candidates or "content" not in candidates[0] or "parts" not in candidates[0]["content"]:
        finish_reason = (candidates[0].get("finishReason") if candidates else None) or "UNKNOWN"
        raise RuntimeError(f"Réponse Gemini vide ou incomplète (finishReason={finish_reason})")

    text = candidates[0]["content"]["parts"][0]["text"]
    text = text.strip().replace("```json", "").replace("```", "").strip()
    return json.loads(text)


# ---------------------------------------------------------------------------
# Analyse PAR LOT (toute la watchlist en 1 seul appel par modèle)
# ---------------------------------------------------------------------------

def _knowledge_block(
    asset: Optional[str] = None,
    regime: Optional[str] = None,
    extra_context: Optional[str] = None,
) -> str:
    """Contextes à injecter dans le prompt : règles permanentes + médias + RAG.

    Trois sections **distinctes**, parce que leur nature diffère :

    * les **règles/principes permanents** (notes de la base de connaissances,
      `get_knowledge_context`), filtrés par actif et régime ;
    * les **observations datées** que sont les médias indexés (lectures de
      graphiques via vision, transcriptions audio/vidéo via Whisper,
      `get_media_context`), rattachées à l'actif et sous leur propre plafond ;
    * les **extraits choisis par l'utilisateur** (`extra_context`, boucle RAG) :
      il a lu les résultats de `/search` et validé ces passages-là. C'est un
      contexte ponctuel et assumé — pas une règle générale, pas une actualité — et
      le prompt le dit au modèle dans ces termes, sinon il généraliserait un
      extrait qui ne parle que d'un cas.

    Les en-têtes disent explicitement au modèle ce qu'il lit : une lecture de
    graphique ponctuelle n'a pas le statut d'une règle de trading permanente et
    ne doit pas être généralisée. Chaque section est best-effort : l'échec de
    l'une n'empêche pas l'autre, et aucune exception ne remonte.
    """
    try:
        kb = get_knowledge_context(asset=asset, regime=regime)
    except Exception as e:
        print(f"   [knowledge_base] erreur de lecture : {type(e).__name__}: {e}")
        kb = ""

    parts: List[str] = []
    if kb.strip():
        parts.append(
            f"\nConnaissances de référence (extraites de ta base de connaissances "
            f"personnelle — règles/principes permanents, PAS des actualités) :\n{kb}\n"
        )

    # Sans actif connu (chemin « batch » de la watchlist), pas de recherche
    # média : une lecture de graphique n'a de sens que rattachée à un actif.
    if asset:
        try:
            media = get_media_context(asset=asset, regime=regime)
        except Exception as e:
            print(f"   [knowledge_base] erreur médias : {type(e).__name__}: {e}")
            media = ""
        if media.strip():
            parts.append(
                f"\nMédias indexés pertinents pour {asset} (lectures de graphiques et "
                f"transcriptions Telegram — OBSERVATIONS DATÉES, PAS des règles "
                f"permanentes ni des actualités ; ne les généralise pas) :\n{media}\n"
            )

    selected = (extra_context or "").strip()
    if selected:
        # Tronqué ici (et non chez l'appelant) : c'est le dernier endroit qui
        # connaît le budget du prompt.
        parts.append(
            f"\nExtraits SÉLECTIONNÉS ET VALIDÉS PAR L'UTILISATEUR pour "
            f"{asset or 'les actifs évalués'} (retenus à la main dans sa base de "
            f"connaissances après lecture des résultats de recherche — contexte "
            f"ponctuel : ce ne sont ni des actualités, ni des règles générales, ne "
            f"les généralise pas au-delà de ce qu'ils disent) :\n"
            f"{selected[:MAX_RAG_CONTEXT_CHARS]}\n"
        )

    return "".join(parts)


def _batch_media_block(assets: List[str]) -> str:
    """Extraits de MÉDIAS (transcriptions, lectures de graphiques) par actif.

    Le prompt batch couvre tous les actifs en un seul appel : on alloue donc à
    chacun une part du budget média total — plafonnée à
    `BATCH_MEDIA_PER_ASSET_CHARS`, et réduite de la place des en-têtes et de la
    marque de troncature (`MEDIA_TRUNCATION_SLACK`) — au lieu de servir les
    premiers et d'oublier les suivants. La somme des parts tient ainsi dans
    `MAX_BATCH_MEDIA_CHARS` par construction : chaque actif a droit à son extrait,
    l'en-tête `### <actif>` compris. Un actif sans extrait assez proche — la
    similarité minimale s'applique déjà dans `get_media_context` — n'occupe
    aucune place, laissée aux suivants.

    Les extraits sont des observations datées : l'en-tête le dit explicitement,
    pour que le modèle ne les transforme pas en règles permanentes.
    """
    if not assets:
        return ""

    # La part de chaque actif est calculée **en-têtes compris** : un bloc coûte
    # aussi son titre `### <actif>`, et l'imputer après coup faisait sortir la
    # somme des parts du budget total dès six actifs — le dernier perdait alors
    # silencieusement ses extraits au profit des premiers.
    header = max(len(str(asset)) for asset in assets) + len("### \n")
    share = MAX_BATCH_MEDIA_CHARS // max(1, len(assets)) - header - MEDIA_TRUNCATION_SLACK
    per_asset = max(1, min(BATCH_MEDIA_PER_ASSET_CHARS, share))

    sections: List[str] = []
    used = 0
    for asset in assets:
        try:
            media = get_media_context(asset=asset, max_chars=per_asset)
        except Exception as exc:  # une recherche ratée ne prive pas les autres
            print(f"   [medias] contexte indisponible pour {asset} : {type(exc).__name__}: {exc}")
            continue
        if not media.strip():
            continue
        block = f"### {asset}\n{media.strip()}\n"
        # Sécurité, jamais atteinte par construction : la part de chaque actif est
        # déjà bornée en-têtes compris. Elle protège le budget si un bloc revient
        # plus long que demandé (plafond de `get_media_context` non respecté).
        if used + len(block) > MAX_BATCH_MEDIA_CHARS:
            break
        sections.append(block)
        used += len(block)

    if not sections:
        return ""
    return BATCH_MEDIA_HEADER + "\n".join(sections)


def _build_batch_prompt(assets: List[str], insights: List[dict]) -> Optional[str]:
    news_block = _news_block(insights)
    if not news_block.strip():
        return None
    asset_list = ", ".join(assets)
    knowledge = _knowledge_block()
    # Les médias sont rattachés à leur actif : le prompt batch ne peut pas se
    # contenter d'un bloc global, contrairement aux règles permanentes.
    media = _batch_media_block(assets)
    return (
        f"Actualités récentes (les plus récentes en premier) :\n{news_block}\n"
        f"{knowledge}\n"
        f"{media}\n"
        f"Actifs à évaluer : {asset_list}\n\n"
        f"Pour CHAQUE actif de cette liste, réponds avec un JSON de cette forme exacte "
        f"(une clé par actif, respecte EXACTEMENT l'orthographe des tickers donnés) :\n"
        f'{{"TICKER": {{"bias": "bullish|bearish|neutral", "score": 0.0-1.0, "reasoning": '
        f'"1 phrase courte factuelle en français"}}, "TICKER2": {{...}}, ...}}'
    )


def analyze_news_batch(assets: List[str], insights: List[dict]) -> Dict[str, Dict[str, Any]]:
    if not insights:
        return {}
    prompt = _build_batch_prompt(assets, insights)
    if not prompt:
        return {}

    max_tokens = max(700, 160 * len(assets))
    per_model = []

    if _client and _available("groq"):
        for model_id in (PRIMARY_MODEL, SECONDARY_MODEL):
            try:
                data = _groq_call(model_id, prompt, max_tokens)
                per_model.append({a: _parse_single(data.get(a) or {}) for a in assets})
            except Exception as e:
                print(f"   ❌ LLM batch ({model_id}) error: {type(e).__name__}: {e}")
        if not per_model:
            # Coupe le circuit seulement si LES DEUX modèles ont échoué —
            # un seul hoquet JSON d'un des deux ne doit pas priver l'autre
            # (qui fonctionnait peut-être très bien) pendant 10 minutes.
            _mark_failed("groq")

    if not per_model and GEMINI_API_KEY and _available("gemini"):
        print("   ⚠️ Groq indisponible → secours Gemini (batch)")
        try:
            data = _gemini_call(prompt, max_tokens)
            return {a: _parse_single(data.get(a) or {}) for a in assets}
        except Exception as e:
            print(f"   ❌ Gemini batch error: {type(e).__name__}: {e}")
            _mark_failed("gemini")

    if not per_model:
        return {}
    if len(per_model) == 1:
        return per_model[0]

    a_map, b_map = per_model
    default = {"bias": "neutral", "score": 0.5, "reasoning": ""}
    return {asset: _consensus(a_map.get(asset, default), b_map.get(asset, default)) for asset in assets}


# ---------------------------------------------------------------------------
# Analyse PAR ACTIF UNIQUE — fallback pour un actif hors watchlist
# ---------------------------------------------------------------------------

def _build_single_prompt(
    asset: str,
    insights: List[dict],
    regime: Optional[str] = None,
    extra_context: Optional[str] = None,
) -> Optional[str]:
    news_block = _news_block(insights)
    if not news_block.strip():
        return None
    knowledge = _knowledge_block(asset=asset, regime=regime, extra_context=extra_context)
    return (
        f"Actualités récentes (les plus récentes en premier) :\n{news_block}\n"
        f"{knowledge}\n"
        f"Actif à évaluer : {asset}\n\n"
        f"Réponds avec ce JSON exact :\n"
        f'{{"bias": "bullish|bearish|neutral", "score": 0.0-1.0, "reasoning": "1-2 phrases '
        f'factuelles en français expliquant le lien (ou l\'absence de lien) avec {asset}"}}'
    )


def analyze_news_for_asset(
    asset: str,
    insights: List[dict],
    regime: Optional[str] = None,
    extra_context: Optional[str] = None,
) -> Dict[str, Any]:
    if not insights:
        return {}
    prompt = _build_single_prompt(asset, insights, regime=regime, extra_context=extra_context)
    if not prompt:
        return {}

    results = []
    if _client and _available("groq"):
        for model_id in (PRIMARY_MODEL, SECONDARY_MODEL):
            try:
                data = _groq_call(model_id, prompt, 300)
                results.append(_parse_single(data))
            except Exception as e:
                print(f"   ❌ LLM ({model_id}) error ({asset}): {type(e).__name__}: {e}")
        if not results:
            _mark_failed("groq")

    if not results and GEMINI_API_KEY and _available("gemini"):
        print(f"   ⚠️ Groq indisponible → secours Gemini ({asset})")
        try:
            data = _gemini_call(prompt, 300)
            return _parse_single(data)
        except Exception as e:
            print(f"   ❌ Gemini error ({asset}): {type(e).__name__}: {e}")
            _mark_failed("gemini")

    if not results:
        return {}
    if len(results) == 1:
        return results[0]
    return _consensus(results[0], results[1])


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

class NewsAnalysisCache:
    """Cache par actif avec TTL (1h par défaut) — le contexte géo/sentiment
    n'a pas besoin d'être recalculé à chaque cycle de l'auto-loop (10 min)."""

    def __init__(self, ttl_seconds: int = 3600):
        self.ttl = ttl_seconds
        self._store: Dict[str, tuple] = {}  # asset -> (timestamp, result)

    def get(
        self,
        asset: str,
        insights: List[dict],
        regime: Optional[str] = None,
        extra_context: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Analyse de l'actif, avec ou sans contexte choisi par l'utilisateur.

        Quand `extra_context` est fourni (boucle RAG), le cache est **ni lu ni
        écrit** : les deux sens seraient faux. Servir une réponse mise en cache
        sans les extraits validerait une analyse qui ne les contient pas, et
        stocker une analyse portée par la sélection d'un utilisateur la
        servirait ensuite au cycle automatique — dont le contexte est justement
        celui que la recherche vectorielle a retenu, pas ce choix-là.
        """
        now = time.time()
        if not (extra_context or "").strip():
            cached = self._store.get(asset)
            if cached and (now - cached[0]) < self.ttl:
                return cached[1]

        result = analyze_news_for_asset(
            asset, insights, regime=regime, extra_context=extra_context
        )
        if not (extra_context or "").strip():
            self._store[asset] = (now, result)
        return result

    def warm_batch(self, assets: List[str], insights: List[dict]) -> None:
        """Pré-remplit le cache pour toute la watchlist en 1 seul aller-retour
        LLM (au lieu d'un appel par actif). Ne fait rien si un batch récent
        est encore valide (respecte le TTL)."""
        now = time.time()
        marker = self._store.get("__batch__")
        if marker and (now - marker[0]) < self.ttl:
            return

        results = analyze_news_batch(assets, insights)
        if not results:
            return
        for asset, res in results.items():
            self._store[asset] = (now, res)
        self._store["__batch__"] = (now, None)
