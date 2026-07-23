"""
Base de connaissances PERMANENTE (règles de trading, notes Obsidian) —
distincte de la table `insights` (actualités éphémères, news récentes).

Chaque note est stockée une seule fois par `source` (upsert = mise à jour
en place, pas d'accumulation infinie comme pour les news).
"""
from typing import List, Dict, Any
from database.supabase_client import supabase

TABLE = "knowledge_base"

# Plafond volontairement STRICT pour ne jamais faire exploser le budget de
# tokens des appels LLM (24 cycles/jour x 2 modèles = ce texte est envoyé
# ~48 fois par jour). 1200 caractères ≈ 300 tokens par appel, soit environ
# 14 000 tokens/jour ajoutés — une fraction du quota gratuit Groq (200 000/j).
# Ce n'est PAS tout ton vault, juste un extrait — augmente ce chiffre avec
# prudence si tu as de la marge de quota, pas au-delà de ~2500 sans risque.
MAX_CONTEXT_CHARS = 1200


def upsert_note(source: str, title: str, content: str) -> None:
    supabase.table(TABLE).upsert({
        "source": source,
        "title": title,
        "content": content,
        "char_count": len(content),
    }).execute()


def list_notes() -> List[Dict[str, Any]]:
    res = supabase.table(TABLE).select("source,title,char_count,updated_at").execute()
    return res.data or []


def get_knowledge_context(max_chars: int = MAX_CONTEXT_CHARS) -> str:
    """
    Retourne un extrait borné de la base de connaissances, à injecter dans
    les prompts LLM. Si le vault dépasse la limite, seules les notes les
    plus récemment mises à jour sont incluses (les autres sont tronquées).
    """
    res = (
        supabase.table(TABLE)
        .select("title,content,updated_at")
        .order("updated_at", desc=True)
        .execute()
    )
    rows = res.data or []
    if not rows:
        return ""

    blocks = []
    total = 0
    for row in rows:
        title = row.get("title") or "Note"
        content = (row.get("content") or "").strip()
        if not content:
            continue
        block = f"### {title}\n{content}\n"
        if total + len(block) > max_chars:
            remaining = max_chars - total
            if remaining > 100:
                blocks.append(block[:remaining] + "\n[...tronqué]")
            break
        blocks.append(block)
        total += len(block)

    return "\n".join(blocks)
