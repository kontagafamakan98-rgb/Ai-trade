"""Le SDK Alpaca est-il installé ? — la question, séparée de son usage.

`execution.order_executor` y répond le plus exactement possible (`ALPACA_OK`) :
il importe pour de vrai les quatre noms dont il a besoin, donc sa réponse vaut
« un client peut être construit ». Mais la payer coûte tout son graphe — base de
données, chiffrement, garde-fou de risque, plus de trois cents modules — pour une
question qu'on pose dans `/health`. La sonde ci-dessous ne charge, elle, que
`importlib.util`, et c'est elle que publie `/preflight`.

Le nom du paquet est écrit **ici** et recopié nulle part : c'est ce que lit
`alpaca_sdk_available()`, et donc ce que dit `alpaca_shared.sdk_installed`.

Ce que cette sonde ne prétend **pas** savoir : qu'un ordre partira. Un paquet
présent peut être cassé (l'import réel, lui, le dirait), l'API peut être
injoignable, et les identifiants peuvent manquer — le dernier cas est publié à
part (`alpaca_shared.keys_present`), les deux autres ne se savent qu'en exécutant.
"""
from __future__ import annotations

import importlib.util

#: Paquet fournissant le SDK (`from alpaca.trading.client import TradingClient`
#: côté exécuteur). Même forme que `ai.media_extractor.LOCAL_WHISPER_PACKAGE`.
ALPACA_SDK_PACKAGE = "alpaca"


def alpaca_sdk_available() -> bool:
    """Le paquet du SDK Alpaca est-il importable sur cette machine ?

    `find_spec` et non `import` : charger le SDK — et ses bibliothèques — pour
    répondre à « es-tu là ? » ferait payer la réponse par un endpoint de santé.
    Une recherche qui échoue (`ImportError` : paquet parent absent ; `ValueError` :
    chemin exotique) est un **non**, jamais une exception : la même règle que
    `ai/media_extractor._package_available`.
    """
    try:
        return importlib.util.find_spec(ALPACA_SDK_PACKAGE) is not None
    except (ImportError, ValueError):
        return False
