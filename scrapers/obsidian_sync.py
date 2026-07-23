"""
Synchronise les notes texte (.md) d'un vault Obsidian vers la base de
connaissances, via un dépôt GitHub (utilise le plugin communautaire
"Obsidian Git" côté Obsidian pour pousser automatiquement le vault).

Ne gère QUE le texte (.md) — PDF/images/vidéos volontairement exclus pour
l'instant (voir discussion : pas adapté à l'architecture actuelle).
"""
import httpx
from database.knowledge_base import upsert_note
from config import GITHUB_TOKEN, GITHUB_REPO, GITHUB_BRANCH

API_BASE = "https://api.github.com"

# Taille max par note individuelle avant d'être tronquée (indépendant du
# plafond global appliqué à la lecture dans knowledge_base.py).
MAX_NOTE_CHARS = 3000


def _headers():
    h = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


async def fetch_and_sync_obsidian_vault() -> int:
    if not GITHUB_REPO:
        return 0

    count = 0
    try:
        async with httpx.AsyncClient(timeout=20, headers=_headers()) as client:
            tree_url = f"{API_BASE}/repos/{GITHUB_REPO}/git/trees/{GITHUB_BRANCH}?recursive=1"
            r = await client.get(tree_url)
            r.raise_for_status()
            tree = r.json().get("tree", [])

            md_files = [f for f in tree if f.get("path", "").endswith(".md")]

            for f in md_files:
                path = f["path"]
                raw_url = (
                    f"https://raw.githubusercontent.com/{GITHUB_REPO}/"
                    f"{GITHUB_BRANCH}/{path}"
                )
                try:
                    rf = await client.get(raw_url)
                    if rf.status_code != 200:
                        continue
                    content = rf.text.strip()
                    if not content:
                        continue
                    if len(content) > MAX_NOTE_CHARS:
                        content = content[:MAX_NOTE_CHARS] + "\n[...tronqué]"

                    title = path.rsplit("/", 1)[-1].replace(".md", "")
                    upsert_note(source=f"obsidian:{path}", title=title, content=content)
                    count += 1
                except Exception as e:
                    print(f"   ❌ Obsidian note {path} error: {type(e).__name__}: {e}")
    except Exception as e:
        print(f"   ❌ Obsidian sync error: {type(e).__name__}: {e}")

    return count
