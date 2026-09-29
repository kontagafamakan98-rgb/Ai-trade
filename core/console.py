"""Sortie des scripts de contrôle : lisible, et jamais un `UnicodeEncodeError`.

Les rapports de ce dépôt sont écrits en français et ponctués de symboles qui
n'existent dans aucune page de code Windows : « ✅ », « ❌ », « ⚠️ », « → ». Or la
sortie d'un script exécuté sous Windows est encodée en `cp1252` dès que le terminal
n'est pas une console Win32 native — Git Bash, mintty, un tube, une redirection vers
un fichier : Python prend alors l'encodage de la locale. `print` lève au **moment du
verdict**, c'est-à-dire à la ligne la plus utile du rapport :

    UnicodeEncodeError: 'charmap' codec can't encode character '\\u2705' in position 3

Le contrôle a travaillé pour rien, et le message parle d'encodage au lieu de parler
du dépôt. Trois scripts portaient déjà le pansement, recopié sous trois formes
différentes (`verify_secrets.py`, `pre_commit_secrets.py`, `calibrate_similarity.py`)
— la meilleure preuve que le problème se repose à chaque nouveau script, et qu'il
doit vivre à un seul endroit.

`sys.stdout` et `sys.stderr` passent donc en **UTF-8**, avec `errors="replace"` en
seconde épaisseur. L'encodage d'abord, et c'est lui qui compte : un tube, un fichier
ou un pseudo-terminal n'a pas de page de code, et tout ce qui relit un rapport — un
éditeur, git, la CI — lit UTF-8. Le remplacement ensuite, pour ce qu'on ne peut pas
promettre : si un flux refuse la reconfiguration, l'écriture ne doit pas pour autant
lever.

Ce module ne touche **pas** à la page de code de la console (`SetConsoleOutputCP`) :
depuis Python 3.6 (PEP 528), une vraie console Windows est déjà écrite en UTF-8 via
l'API large, donc la basculer ne changerait rien pour nous et laisserait le terminal
de l'utilisateur en 65001 après la sortie du script.

Un script l'appelle **en tête de son `main`**, avant d'écrire quoi que ce soit
(contrat vérifié par `tests/test_console_output.py`) :

    from core import console

    def main(argv: Optional[List[str]] = None) -> int:
        console.make_streams_utf8()
        ...
"""
from __future__ import annotations

import sys
from typing import Any

#: L'encodage des rapports. Nommé plutôt que recopié : c'est aussi celui des
#: fichiers du dépôt (`.editorconfig`), des messages du bot et des goldens.
OUTPUT_ENCODING = "utf-8"

#: Ce qu'on écrit quand un flux refuse l'UTF-8 : un caractère illisible devient un
#: `?` plutôt qu'une exception. Un rapport amputé d'un symbole reste un rapport.
OUTPUT_ERRORS = "replace"


def make_streams_utf8() -> int:
    """Règle `sys.stdout` et `sys.stderr` en UTF-8 tolérant.

    Rend le nombre de flux effectivement réglés (0, 1 ou 2) : un flux sans
    `reconfigure` — la capture d'un test, un objet maison, un `pythonw.exe` sans
    sortie — est **laissé tel quel** plutôt que remplacé. Idempotent, et ne lève
    jamais : ce module ne doit pas devenir la panne qu'il évite.
    """
    return sum(_reconfigure(stream) for stream in (sys.stdout, sys.stderr))


def _reconfigure(stream: Any) -> bool:
    """Un flux réglé, ou laissé intact s'il ne peut pas l'être — jamais une levée."""
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return False
    try:
        reconfigure(encoding=OUTPUT_ENCODING, errors=OUTPUT_ERRORS)
    except (ValueError, OSError):
        # Un flux détaché, un tampon déjà fermé : `io.UnsupportedOperation` est
        # déjà un `OSError`, et `ValueError` couvre « underlying buffer has been
        # detached ». Rien à faire, et surtout rien à casser.
        return False
    return True


__all__ = [
    "OUTPUT_ENCODING",
    "OUTPUT_ERRORS",
    "make_streams_utf8",
]
