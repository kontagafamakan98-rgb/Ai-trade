"""Authentification par clé partagée pour les endpoints internes.

Tous les endpoints qui exécutent des ordres, modifient l'apprentissage ou
exposent des données par utilisateur DOIVENT être protégés. On utilise une
seule clé interne (`INTERNAL_API_KEY`) comparée en temps constant
(`hmac.compare_digest`) pour éviter les attaques par timing.

Conventions :
- En-tête recommandé : `X-API-Key: <clé>`
- Alternative acceptée  : `Authorization: Bearer <clé>`
"""
from __future__ import annotations

from typing import Optional

from fastapi import Header, HTTPException

from core.config_runtime import get_env_config
from core.security_utils import constant_time_equals


def _extract_key(x_api_key: Optional[str], authorization: Optional[str]) -> Optional[str]:
    if x_api_key:
        return x_api_key
    if authorization:
        parts = authorization.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
        return authorization.strip()
    return None


async def require_api_key(
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    authorization: Optional[str] = Header(None, alias="Authorization"),
) -> bool:
    """Dépendance FastAPI : refuse (401) si la clé interne est absente/incorrecte."""
    cfg = get_env_config()
    expected = cfg.internal_api_key
    if not expected:
        # Fail-closed : pas de clé configurée => endpoint inaccessible.
        raise HTTPException(
            status_code=503,
            detail="INTERNAL_API_KEY non configurée : endpoint interne désactivé.",
        )

    provided = _extract_key(x_api_key, authorization)
    if not constant_time_equals(provided, expected):
        raise HTTPException(status_code=401, detail="Clé API interne invalide.")
    return True
