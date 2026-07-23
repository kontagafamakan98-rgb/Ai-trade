# utils/security.py
from typing import Optional
import re

def is_private_chat(update) -> bool:
    """Vérifie que la commande est en message privé (sécurité clés API)"""
    return update.effective_chat.type == "private"

def sanitize_asset(asset: str) -> str:
    """Nettoie le ticker"""
    if not asset:
        return ""
    return re.sub(r'[^A-Z0-9\-/]', '', asset.upper().strip())[:12]

def validate_admin(update, admin_ids: list) -> bool:
    """Vérifie si l'utilisateur est admin"""
    return str(update.effective_user.id) in admin_ids