"""Routeur des rapports de performance et du preflight (surface protégée).

Ces endpoints vivaient auparavant dans `api/webhook.py`, qui importait
`notifications.notify` — un module qui construisait alors un `Bot(token=...)`
**à l'import** et dépendait donc de `python-telegram-bot`. Conséquence : toute la
surface protégée devenait impossible à importer et à tester sans la pile Telegram.

En les isolant ici, la surface protégée (`/preflight`, `/reports/*`) ne dépend
plus que de :

* `api.security` (authentification par clé partagée) ;
* `core.config_runtime` (preflight, et le rapport d'empreintes de
  `/secrets/fingerprints`) ;
* `reports.performance_report` (rapports).

Aucun de ces modules n'importe Telegram. Les chemins exposés sont **inchangés**,
et `api/webhook.py` continue d'inclure ce routeur dans la même application.

Depuis, `notifications.notify` construit son `Bot` **paresseusement** et
`api/webhook.py` importe ce module dans son handler : l'extraction ci-dessus
reste utile (elle réduit le graphe de dépendances de la surface protégée) mais
n'est plus la seule chose qui protégeait le démarrage.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends

from api.security import require_api_key
from core.config_runtime import deployed_secret_report, safe_preflight
from reports.performance_report import build_performance_summary, export_run_card

router = APIRouter(
    tags=["Reports & Preflight"],
    dependencies=[Depends(require_api_key)],
)


@router.get("/preflight")
def preflight():
    return safe_preflight()


@router.get("/secrets/fingerprints")
def secrets_fingerprints():
    """Ce que **ce processus** a réellement chargé — en empreintes, jamais en clair.

    Existe pour que la barrière pre-deploy mesure la production au lieu du
    `.env` de la machine qui la lance : elle compare ces empreintes aux siennes
    (`scripts/verify_secrets.py --remote <url>`), et deux empreintes égales valent
    deux valeurs égales. L'endpoint ne peut donc pas servir à lire un secret : il
    publie un nom, 16 hexadécimaux salés, la version de l'anneau de chiffrement et
    sa date de mesure.

    Rien ici ne remplace `require_api_key` (dépendance du routeur) : sans clé
    interne, la réponse est 503 — un service exposé publiquement ne publie pas
    l'inventaire de ses secrets sans elle.
    """
    return deployed_secret_report()


@router.get("/reports/summary")
def report_summary():
    return build_performance_summary()


@router.get("/reports/summary/{user_id}")
def report_summary_user(user_id: str):
    return build_performance_summary(user_id)


@router.post("/reports/export")
def report_export(user_id: Optional[str] = None):
    return export_run_card(user_id)


__all__ = ["router"]
