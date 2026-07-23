"""
Mécanisme de "leçons apprises" — le plus simple possible.

Pas de machine learning, pas d'entraînement de poids : juste un résumé
compact des trades récents (gagnés/perdus) et du raisonnement qui les a
déclenchés, réinjecté dans le contexte de chaque future analyse via le
même canal que la base de connaissances (ai/news_analyzer.py).

Aucun nouvel appel LLM : juste du texte généré à partir des données déjà
en base (pending_signals). Mise à jour = 1 seule note, écrasée à chaque
fois (pas d'accumulation infinie, budget de tokens maîtrisé).
"""
from database.supabase_client import supabase
from database.knowledge_base import upsert_note

SOURCE = "performance:lessons"
MAX_RECENT = 12  # nombre de trades récents pris en compte dans le bilan


def update_lessons(user_id: str = None) -> None:
    """
    Reconstruit le bilan de performance à partir des derniers trades réglés
    (won/lost) et le stocke comme une note de la base de connaissances,
    automatiquement relue à chaque analyse future.
    """
    try:
        query = (
            supabase.table("pending_signals")
            .select("*")
            .in_("status", ["won", "lost"])
            .order("validated_at", desc=True)
            .limit(MAX_RECENT)
        )
        if user_id:
            query = query.eq("user_id", user_id)
        res = query.execute()
        rows = res.data or []
    except Exception as e:
        print(f"   ❌ self_review error (lecture): {type(e).__name__}: {e}")
        return

    if not rows:
        return

    wins = sum(1 for r in rows if r["status"] == "won")
    losses = sum(1 for r in rows if r["status"] == "lost")
    win_rate = round(100 * wins / len(rows), 1) if rows else 0

    lines = [f"Bilan des {len(rows)} derniers trades réglés : {wins} gagnés, {losses} perdus (win rate {win_rate}%)."]

    # Quelques exemples concrets (les 4 plus récents), pour donner du
    # contexte réel plutôt qu'un simple chiffre.
    for r in rows[:4]:
        sig = r.get("signal") or {}
        asset = sig.get("asset", "?")
        direction = sig.get("direction", "?")
        ta = (sig.get("ta_summary") or "")[:80]
        outcome = "GAGNÉ" if r["status"] == "won" else "PERDU"
        lines.append(f"- {asset} {direction} ({ta}) → {outcome}")

    summary = "\n".join(lines)
    try:
        upsert_note(source=SOURCE, title="Bilan de performance récent (auto)", content=summary)
    except Exception as e:
        print(f"   ❌ self_review error (écriture): {type(e).__name__}: {e}")
