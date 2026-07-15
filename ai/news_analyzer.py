"""
Analyse qualitative des news via 2 LLM gratuits sur Groq (vérification croisée),
pour remplacer le simple comptage d'événements par une lecture réelle du
contenu et de sa pertinence pour un actif donné.

Modèles utilisés (gratuits sur Groq, une seule clé API) :
- openai/gpt-oss-120b : modèle principal
- qwen/qwen3.6-27b    : modèle de vérification croisée

Mécanisme de consensus :
- Si les 2 modèles sont d'accord sur la direction (bullish/bearish/neutral)
  → score moyenné, confiance renforcée.
- Si les 2 modèles sont EN DÉSACCORD → on reste prudent : le score est
  ramené vers le neutre plutôt que de trancher arbitrairement.
- Si un seul modèle répond (l'autre en erreur) → on garde ce résultat seul.
- Si aucun ne répond → {} et decision_engine.py revient à l'ancienne
  méthode de comptage (fallback total).

⚠️ Ce mécanisme réduit le risque qu'un seul modèle hallucine un lien
inexistant, mais ne garantit AUCUNE précision absolue — un marché reste
fondamentalement incertain, même avec 2 IA d'accord entre elles.

GESTION DU QUOTA GRATUIT GROQ (important) :
- Un seul appel "batch" couvre TOUTE la watchlist en une fois (au lieu
  d'un appel par actif) → ~12x moins d'appels et de tokens consommés.
- Le cache dure 1h par défaut (au lieu de 10 min) : le contexte géo/
  sentiment n'a pas besoin d'être recalculé à chaque cycle de 10 min.
"""
import json
import time
from typing import Dict, Any, List, Optional

from config import GROQ_API_KEY

try:
    from groq import Groq
    _client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
except Exception:
    _client = None

PRIMARY_MODEL = "openai/gpt-oss-120b"
SECONDARY_MODEL = "qwen/qwen3.6-27b"

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
# Analyse PAR LOT (toute la watchlist en 1 seul appel par modèle) — usage principal
# ---------------------------------------------------------------------------

def _build_batch_prompt(assets: List[str], insights: List[dict]) -> Optional[str]:
    news_block = _news_block(insights)
    if not news_block.strip():
        return None
    asset_list = ", ".join(assets)
    return (
        f"Actualités récentes (les plus récentes en premier) :\n{news_block}\n\n"
        f"Actifs à évaluer : {asset_list}\n\n"
        f"Pour CHAQUE actif de cette liste, réponds avec un JSON de cette forme exacte "
        f"(une clé par actif, respecte EXACTEMENT l'orthographe des tickers donnés) :\n"
        f'{{"TICKER": {{"bias": "bullish|bearish|neutral", "score": 0.0-1.0, "reasoning": '
        f'"1 phrase courte factuelle en français"}}, "TICKER2": {{...}}, ...}}'
    )


def _call_model_batch(model_id: str, prompt: str, assets: List[str]) -> Dict[str, Dict[str, Any]]:
    resp = _client.chat.completions.create(
        model=model_id,
        max_tokens=max(700, 160 * len(assets)),
        temperature=0.2,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    )
    text = resp.choices[0].message.content.strip()
    text = text.replace("```json", "").replace("```", "").strip()
    data = json.loads(text)

    out = {}
    for a in assets:
        entry = data.get(a) or {}
        out[a] = _parse_single(entry)
    return out


def analyze_news_batch(assets: List[str], insights: List[dict]) -> Dict[str, Dict[str, Any]]:
    """Une seule requête par modèle pour TOUTE la liste d'actifs (économise
    le quota gratuit au lieu d'un appel par actif)."""
    if not _client or not insights:
        return {}

    prompt = _build_batch_prompt(assets, insights)
    if not prompt:
        return {}

    per_model = []
    for model_id in (PRIMARY_MODEL, SECONDARY_MODEL):
        try:
            per_model.append(_call_model_batch(model_id, prompt, assets))
        except Exception as e:
            print(f"   ❌ LLM batch ({model_id}) error: {type(e).__name__}: {e}")

    if not per_model:
        return {}
    if len(per_model) == 1:
        return per_model[0]

    a_map, b_map = per_model
    final = {}
    for asset in assets:
        a = a_map.get(asset, {"bias": "neutral", "score": 0.5, "reasoning": ""})
        b = b_map.get(asset, {"bias": "neutral", "score": 0.5, "reasoning": ""})
        final[asset] = _consensus(a, b)
    return final


# ---------------------------------------------------------------------------
# Analyse PAR ACTIF UNIQUE — fallback pour un actif hors watchlist (ex: /analyze
# sur un ticker non suivi), rarement appelé grâce au cache pré-rempli en lot.
# ---------------------------------------------------------------------------

def analyze_news_for_asset(asset: str, insights: List[dict]) -> Dict[str, Any]:
    if not _client or not insights:
        return {}

    news_block = _news_block(insights)
    if not news_block.strip():
        return {}

    prompt = (
        f"Actualités récentes (les plus récentes en premier) :\n{news_block}\n\n"
        f"Actif à évaluer : {asset}\n\n"
        f"Réponds avec ce JSON exact :\n"
        f'{{"bias": "bullish|bearish|neutral", "score": 0.0-1.0, "reasoning": "1-2 phrases '
        f'factuelles en français expliquant le lien (ou l\'absence de lien) avec {asset}"}}'
    )

    results = []
    for model_id in (PRIMARY_MODEL, SECONDARY_MODEL):
        try:
            resp = _client.chat.completions.create(
                model=model_id,
                max_tokens=300,
                temperature=0.2,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            )
            text = resp.choices[0].message.content.strip().replace("```json", "").replace("```", "").strip()
            results.append(_parse_single(json.loads(text)))
        except Exception as e:
            print(f"   ❌ LLM ({model_id}) error ({asset}): {type(e).__name__}: {e}")

    if not results:
        return {}
    if len(results) == 1:
        return results[0]
    return _consensus(results[0], results[1])


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

class NewsAnalysisCache:
    """Cache par actif avec TTL. Par défaut 1h (au lieu de 10 min) car le
    contexte géo/sentiment n'a pas besoin d'être recalculé à chaque cycle
    de l'auto-loop (10 min) — ça épuisait le quota gratuit Groq en quelques
    heures."""

    def __init__(self, ttl_seconds: int = 3600):
        self.ttl = ttl_seconds
        self._store: Dict[str, tuple] = {}  # asset -> (timestamp, result)

    def get(self, asset: str, insights: List[dict]) -> Dict[str, Any]:
        now = time.time()
        cached = self._store.get(asset)
        if cached and (now - cached[0]) < self.ttl:
            return cached[1]

        result = analyze_news_for_asset(asset, insights)
        self._store[asset] = (now, result)
        return result

    def warm_batch(self, assets: List[str], insights: List[dict]) -> None:
        """Pré-remplit le cache pour toute la watchlist en 1 seul appel par
        modèle (2 appels au total, au lieu de 2 x nombre d'actifs). Ne fait
        rien si un batch récent est encore valide (respecte le TTL)."""
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
