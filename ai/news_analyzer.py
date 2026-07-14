"""
Analyse qualitative des news via un LLM gratuit (Groq / Llama 3.3), pour
remplacer le simple comptage d'événements par une lecture réelle du contenu
et de sa pertinence pour un actif donné.

Fallback gracieux : si GROQ_API_KEY absente ou erreur API, retourne {}
et decision_engine.py revient automatiquement à l'ancienne méthode (comptage).
"""
import json
import time
from typing import Dict, Any, List

from config import GROQ_API_KEY

try:
    from groq import Groq
    _client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
except Exception:
    _client = None

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


def analyze_news_for_asset(asset: str, insights: List[dict]) -> Dict[str, Any]:
    """
    Retourne {"bias": "bullish"|"bearish"|"neutral", "score": 0.0-1.0, "reasoning": str}
    ou {} si le LLM est indisponible / erreur (fallback géré côté decision_engine).
    """
    if not _client or not insights:
        return {}

    news_block = "\n".join(
        f"- [{i.get('type', '?')}] {i.get('title', '')}: {(i.get('summary') or '')[:300]}"
        for i in insights[:15]
    )
    if not news_block.strip():
        return {}

    prompt = (
        f"Actualités récentes (les plus récentes en premier) :\n{news_block}\n\n"
        f"Actif à évaluer : {asset}\n\n"
        f"Réponds avec ce JSON exact :\n"
        f'{{"bias": "bullish|bearish|neutral", "score": 0.0-1.0, "reasoning": "1-2 phrases '
        f'factuelles en français expliquant le lien (ou l\'absence de lien) avec {asset}"}}'
    )

    try:
        resp = _client.chat.completions.create(
            model="llama-3.3-70b-versatile",
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

        return {
            "bias": bias,
            "score": score,
            "reasoning": str(data.get("reasoning", ""))[:400],
        }
    except Exception as e:
        print(f"   ❌ LLM news analyzer error ({asset}): {type(e).__name__}: {e}")
        return {}


class NewsAnalysisCache:
    """Cache par actif avec TTL, pour éviter un appel LLM à chaque analyse
    (l'auto-loop tourne toutes les 10 min sur 12 actifs => sans cache, ça
    ferait 12 appels par cycle même si les news n'ont pas changé)."""

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
