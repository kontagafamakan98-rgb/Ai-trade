"""Utilitaires de sécurité sans dépendance externe (testables hors FastAPI)."""
from __future__ import annotations

import hmac
from typing import Optional


def constant_time_equals(a: Optional[str], b: Optional[str]) -> bool:
    """Comparaison de chaînes à temps constant, tolérante aux None.

    Utilise `hmac.compare_digest` pour éviter les attaques par mesure de temps
    lors de la vérification d'un secret (webhook, clé API...).
    """
    if a is None or b is None:
        return False
    return hmac.compare_digest(str(a), str(b))
