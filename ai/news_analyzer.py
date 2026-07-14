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
    "pessimiste. Tu évalues l'impact probable d'actualités récentes sur un actif "
    "financier précis.\n\n"
    "Règles strictes :\n"
    "- Reste strictement factuel. Ne spécule pas au-delà de ce que les news indiquent.\n"
    "- Si aucune news n'a de lien clair et direct avec l'actif, réponds bias=\"neutral\" "
    "et score=0.5. Ne force JAMAIS un lien qui n'existe pas.\n"
    "- N'utilise un score extrême (proche de 0.0 ou 1.0) que si l'actualité est sans "
    "ambiguïté et à impact majeur (ex: guerre déclarée, défaut souverain, décision de "
    "taux confirmée, faillite). Les news vagues ou indirectes doivent rester proches de 0.5.\n"
    "- Tu n'es pas un conseiller financier et ne donnes aucune recommandation d'achat/vente, "
    "seulement une évaluation d'impact.\n"
    "- Réponds UNIQUEMENT avec un objet JSON valide, sans texte avant/après, sans balises markdown."
)


def _build_prompt(asset: str, insights: List[dict]) -> Optional[str]:
    news_block = "\n".join(
        f"- [{i.get('type', '?')}] {i.get('title', '')}: {(i.get('summary') or '')[:300]}"
        for i in insights[:15]
    )
    if not news_block.strip():
        return None
    return (
        f"Actualités récentes (les plus récentes en premier) :\n{news_block}\n\n"
        f"Actif à évaluer : {asset}\n\n"
        f"Réponds avec ce JSON exact :\n"
        f'{{"bias": "bullish|bearish|neutral", "score": 0.0-1.0, "reasoning": "1-2 phrases '
        f'factuelles en français expliquant le lien (ou l\'absence de lien) avec {asset}"}}'
    )


def _call_model(model_id: str, prompt: str) -> Dict[str, Any]:
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
    text = resp.choices[0].message.content.strip()
    text = text.replace("```json", "").replace("```", "").strip()
    data = json.loads(text)

    score = max(0.0, min(1.0, float(data.get("score", 0.5))))
    bias = data.get("bias", "neutral")
    if bias not in ("bullish", "bearish", "neutral"):
        bias = "neutral"

    return {"bias": bias, "score": score, "reasoning": str(data.get("reasoning", ""))[:400]}


def analyze_news_for_asset(asset: str, insights: List[dict]) -> Dict[str, Any]:
    """
    Retourne {"bias": ..., "score": ..., "reasoning": ...} en croisant 2 modèles,
    ou {} si aucun n'a pu répondre (fallback géré côté decision_engine).
    """
    if not _client or not insights:
        return {}

    prompt = _build_prompt(asset, insights)
    if not prompt:
        return {}

    results = []
    for model_id in (PRIMARY_MODEL, SECONDARY_MODEL):
        try:
            r = _call_model(model_id, prompt)
            results.append((model_id, r))
        except Exception as e:
            print(f"   ❌ LLM ({model_id}) error ({asset}): {type(e).__name__}: {e}")

    if not results:
        return {}

    if len(results) == 1:
        _, only = results[0]
        return only

    (_, a), (_, b) = results

    if a["bias"] == b["bias"]:
        # Les 2 modèles sont d'accord → score moyenné, confiance renforcée
        final_score = (a["score"] + b["score"]) / 2
        final_bias = a["bias"]
        reasoning = f"[2 IA d'accord] {a['reasoning']}"
    else:
        # Désaccord → on reste prudent, on ramène vers le neutre au lieu
        # de trancher arbitrairement entre les deux avis.
        final_score = 0.5 + (a["score"] - 0.5) * 0.25 + (b["score"] - 0.5) * 0.25
        final_bias = "neutral"
        reasoning = (
            f"Avis partagés entre les 2 IA ({a['bias']} vs {b['bias']}) → prudence, "
            f"score neutralisé. Avis A: {a['reasoning']} | Avis B: {b['reasoning']}"
        )

    return {
        "bias": final_bias,
        "score": max(0.0, min(1.0, final_score)),
        "reasoning": reasoning[:500],
    }


class NewsAnalysisCache:
    """Cache par actif avec TTL, pour éviter des appels LLM à chaque analyse
    (l'auto-loop tourne toutes les 10 min sur 12 actifs => sans cache, ça
    ferait jusqu'à 24 appels par cycle même si les news n'ont pas changé)."""

    def __init__(self, ttl_seconds: int = 600):
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
