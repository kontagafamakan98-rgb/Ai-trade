#!/usr/bin/env python
"""Valeurs de référence (goldens) versionnées — le gate qui rend un changement visible.

Un test qui contient ses valeurs attendues **en ligne** les rend modifiables sans
que personne ne le remarque : on ajuste le chiffre, la CI repasse au vert, et la
dérive est validée par le même commit que la dérive. Ce script déplace ces
valeurs dans des fichiers JSON **versionnés** (`tests/goldens/`) :

* la CI recalcule les valeurs depuis le code et les compare aux fichiers ;
* toute différence fait **échouer** le gate, avec un diff et la commande exacte
  pour régénérer (`python scripts/goldens.py --update <jeu>`) ;
* accepter un changement devient donc un acte explicite : on relit le diff du
  fichier JSON, on le commite avec le changement de code, et le relecteur voit
  la valeur de référence bouger dans la revue.

Ce n'est pas un test de plus, c'est le **journal** de ce qui a changé.

Trois jeux de valeurs de référence :

1. ``alert_engine`` — les nombres du moteur d'alerte (EMA, ATR de Wilder) sur la
   série déterministe documentée du dépôt, **et la sortie complète de
   `detect_cross`** (action, prix, stop-loss, take-profit, message, EMA, ATR) sur
   une série de croisement documentée, avec les multiplicateurs et périodes par
   défaut, puis avec des valeurs déplacées. Une dérive du calcul (amorçage,
   facteur de lissage, arrondi) déplace ces nombres ; une régression qui cesse
   d'appliquer `sl_atr_mult`/`tp_atr_mult` rend identiques deux enregistrements
   qui doivent différer — dans les deux cas, c'est le diff qui le dit.
2. ``android_localization`` — le contrat de localisation : jeu de clés de
   référence, clés ``translatable="false"``, et pour chaque langue les clés
   manquantes ou en trop. Ajouter, renommer ou déplacer une chaîne est un acte
   délibéré, donc un diff à relire. Le **texte** des traductions n'est pas figé
   ici (le corriger est du travail éditorial légitime) : `tests/test_android_strings.py`
   vérifie sa correction (parité, échappement, arguments de format).
3. ``api_routes`` — la surface HTTP publique (méthode + gabarit de chemin) de
   ``api.webhook.app``. Un endpoint qui disparaît ou change de chemin casse des
   clients : c'est exactement ce qu'on veut voir dans un diff.

Le troisième jeu charge l'application FastAPI dans un **sous-processus** avec un
environnement factice : l'extraction ne peut donc ni dépendre de vrais secrets,
ni polluer la configuration des autres tests.

Usage :

    python scripts/goldens.py                      # vérifie (défaut, code 1 si dérive)
    python scripts/goldens.py --update             # régénère tous les goldens
    python scripts/goldens.py --update alert_engine # régénère un seul jeu
    python scripts/goldens.py --list               # liste les jeux connus

Un fichier ``tests/goldens/*.json`` ne s'édite **jamais** à la main : régénère-le.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import pathlib
import re
import subprocess
import sys
import textwrap
import xml.etree.ElementTree as ET
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
# Lancé comme `python scripts/goldens.py`, l'interpréteur met `scripts/` sur le
# chemin d'import, pas la racine : les jeux de valeurs importent `core`/`api`.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Importé **après** l'insertion ci-dessus : un module de `scripts/` n'est
# atteignable que la racine sur le chemin d'import (le cas d'un lancement direct).
from core import console  # noqa: E402
from scripts import apply_migrations  # noqa: E402

#: La variable qui désigne la base des migrations — la même que l'applicateur, et
#: aucune autre (voir `scripts/schema_snapshot.py`). Citée dans les consignes de
#: régénération du jeu de schéma : recopiée, elle finirait par mentir.
MIGRATION_ENV_VAR = apply_migrations.ENV_VAR

DEFAULT_DIR = REPO_ROOT / "tests" / "goldens"
RES_DIR = REPO_ROOT / "app" / "src" / "main" / "res"
UPDATE_COMMAND = "python scripts/goldens.py --update"

#: Clés référencées par `AndroidManifest.xml` : présentes en anglais seulement,
#: par choix — il n'y a rien à traduire dans le nom du paquet affiché par le
#: lanceur. Les réclamer dans chaque langue produirait un faux défaut.
MANIFEST_ONLY_KEYS = {"app_name"}

#: Nombre de lignes de diff affichées avant troncature.
DIFF_LIMIT = 40

#: Argument de format d'une chaîne de ressource : `%1$s`, `%2$d`, `%s`, `%d`.
FORMAT_ARG = re.compile(r"%(?:\d+\$)?[sd]")


class Unavailable(Exception):
    """Le jeu de valeurs ne peut pas être calculé dans cet environnement.

    Deux cas : `api_routes` sans FastAPI installé, et `postgres_schema` sans
    serveur PostgreSQL (ou sans `pg_dump`). Ce n'est pas une dérive,
    mais un contrôle **non exécuté** — le script le dit, il ne le passe pas
    silencieusement au vert.
    """


class Dataset(NamedTuple):
    """Un jeu de valeurs de référence : un nom, une intention, un calcul."""

    name: str
    description: str
    build: Callable[[], Any]
    #: Ce qu'il faut pour le **recalculer**, quand il n'est pas recalculable
    #: partout (une base jetable, un binaire). Affiché avec le skip et avec la
    #: consigne de régénération, jamais ailleurs : une condition qui ne s'applique
    #: pas ne doit pas s'afficher.
    requires: str = ""


class Outcome(NamedTuple):
    """Résultat d'une vérification.

    ``status`` vaut ``ok`` (à jour), ``changed`` (dérive : le gate échoue),
    ``error`` (lecture ou extraction impossible : le gate échoue aussi) ou
    ``skip`` (jeu non exécutable ici, annoncé explicitement).
    """

    name: str
    status: str
    message: str

    @property
    def ok(self) -> bool:
        return self.status in {"ok", "skip"}


# --------------------------------------------------------------------------- #
# Jeu 1 : valeurs numériques du moteur d'alerte
# --------------------------------------------------------------------------- #


def documented_candles(n: int = 60) -> List[Dict[str, float]]:
    """Série déterministe et documentée — reproductible à la main.

    * ``close[i] = 100 + (i % 7) + (i // 7) * 3`` ;
    * ``high[i] = close[i] + 1 + (i % 3)`` ;
    * ``low[i]  = close[i] - 1 - (i % 4)``.
    """
    candles: List[Dict[str, float]] = []
    for i in range(n):
        close = 100 + (i % 7) + (i // 7) * 3
        candles.append(
            {"high": close + 1 + (i % 3), "low": close - 1 - (i % 4), "close": close}
        )
    return candles


#: Amplitude du saut de la dernière chandelle de la série de croisement.
BREAKOUT_AMPLITUDE = 20.0


def documented_breakout_candles(direction: str = "up", n: int = 60) -> List[Dict[str, float]]:
    """Série de **croisement**, documentée et vérifiable à la main.

    La série de niveau (`documented_candles`) ne croise jamais : sa dérive
    positive garde EMA20 au-dessus d'EMA50 d'un bout à l'autre (le golden fige
    d'ailleurs ce `None`, voir `crosses.none_on_level_series`). Pour figer la
    logique de croisement — et les multiplicateurs de stop/take-profit, qui ne
    valent que sur une alerte — il faut une série qui croise, et qui croise sur
    la **dernière chandelle close**, la seule que `detect_cross` examine.

    Forme retenue, reproductible à la main :

    * les ``n - 1`` premières chandelles sont **plates** à 100 — donc
      ``EMA20 == EMA50`` juste avant la dernière clôture ;
    * la dernière clôture casse à ``100 + amplitude`` (haussier) ou
      ``100 - amplitude`` (baissier) ;
    * ``high``/``low`` gardent l'écart des chandelles documentées
      (``high = close + 1 + (i % 3)``, ``low = close - 1 - (i % 4)``), donc l'ATR
      n'est pas dégénéré et le stop/take-profit restent significatifs.

    L'égalité ``f_prev == s_prev`` satisfait la condition de non-égalité
    (``f_prev <= s_prev`` / ``f_prev >= s_prev``) : c'est le saut final, et lui
    seul, qui fait basculer la relation.
    """
    if direction not in {"up", "down"}:
        raise ValueError(f"direction inconnue : {direction!r} (attendu : up, down)")
    candles: List[Dict[str, float]] = []
    for i in range(n):
        close = 100.0
        if i == n - 1:
            close += BREAKOUT_AMPLITUDE if direction == "up" else -BREAKOUT_AMPLITUDE
        candles.append(
            {"high": close + 1 + (i % 3), "low": close - 1 - (i % 4), "close": close}
        )
    return candles


def _flat(closes: Sequence[float]) -> List[Dict[str, float]]:
    """Chandelles dégénérées (high = low = close) : ATR = amplitude close-à-close."""
    return [{"high": float(c), "low": float(c), "close": float(c)} for c in closes]


def _round(value: Optional[float], digits: int = 9) -> Optional[float]:
    return None if value is None else round(float(value), digits)


def alert_engine_values() -> Dict[str, Any]:
    """Nombres de référence du moteur d'alerte interne (EMA/ATR + croisements)."""
    from core import alert_engine as ae

    candles = documented_candles()
    closes = [c["close"] for c in candles]
    fast = ae.ema(closes, ae.EMA_FAST)
    slow = ae.ema(closes, ae.EMA_SLOW)
    return {
        "series": {
            "length": len(candles),
            "formula": (
                "close[i]=100+(i%7)+(i//7)*3 ; high=close+1+(i%3) ; low=close-1-(i%4)"
            ),
        },
        "constants": {
            "EMA_FAST": ae.EMA_FAST,
            "EMA_SLOW": ae.EMA_SLOW,
            "ATR_PERIOD": ae.ATR_PERIOD,
            "SL_ATR_MULT": ae.SL_ATR_MULT,
            "TP_ATR_MULT": ae.TP_ATR_MULT,
            "ALERT_CONFIDENCE": ae.ALERT_CONFIDENCE,
            "INTERNAL_SOURCE": ae.INTERNAL_SOURCE,
            "EXTERNAL_SOURCE": ae.EXTERNAL_SOURCE,
        },
        # Série complète, pas seulement un échantillon : c'est le journal du
        # calcul, et un diff y montre la chandelle exacte qui a bougé.
        "ema_fast": [_round(value) for value in fast],
        "ema_slow": [_round(value) for value in slow],
        "atr": {str(period): _round(ae.atr(candles, period)) for period in (2, 5, 14, 21)},
        "true_ranges_head": [_round(value) for value in ae.true_ranges(candles)[:8]],
        "crossing_series": {
            "length": 60,
            "formula": (
                "close[i]=100 (i<n-1) et close[n-1]=100+/-amplitude ; "
                "high=close+1+(i%3) ; low=close-1-(i%4)"
            ),
            "amplitude": BREAKOUT_AMPLITUDE,
        },
        # Sortie **complète** de `detect_cross` (action, prix, stop-loss,
        # take-profit, message, EMA et ATR) : figer les nombres sans figer le stop
        # et le take-profit laisserait libre la partie qui parle d'argent.
        "crosses": {
            # La série de niveau ne croise pas : ce n'est pas un oubli mais la
            # propriété qui doit rester vraie (une régression de l'amorçage des
            # EMA, ou du sens de l'inégalité, la ferait basculer à tort).
            "none_on_level_series": ae.detect_cross(documented_candles()),
            "documented_bullish": ae.detect_cross(documented_breakout_candles("up")),
            "documented_bearish": ae.detect_cross(documented_breakout_candles("down")),
            # Mêmes chandelles, multiplicateurs déplacés : si le calcul cessait
            # d'appliquer `sl_atr_mult`/`tp_atr_mult` (constantes 1,5/3,0 figées
            # en dur), ces deux enregistrements deviendraient identiques et le
            # diff le montrerait.
            "documented_bullish_displaced_multipliers": ae.detect_cross(
                documented_breakout_candles("up"), sl_atr_mult=1.0, tp_atr_mult=2.0
            ),
            # Périodes passées en paramètres : fige aussi le message (`EMA5
            # crossover EMA10`), donc le gabarit du texte affiché.
            "documented_bullish_custom_periods": ae.detect_cross(
                documented_breakout_candles("up"), ema_fast=5, ema_slow=10, atr_period=5
            ),
            # Cas courts, vérifiables à la main, déjà utilisés par
            # `tests/test_alert_engine.py` : ils verrouillent le comportement
            # dégénéré (ATR = amplitude close-à-close, high = low = close).
            "hand_check_bullish": ae.detect_cross(
                _flat([10.0, 10.0, 10.0, 10.0, 20.0]), ema_fast=2, ema_slow=3, atr_period=2
            ),
            "hand_check_bearish": ae.detect_cross(
                _flat([20.0, 20.0, 20.0, 20.0, 10.0]), ema_fast=2, ema_slow=3, atr_period=2
            ),
            "flat_market": ae.detect_cross(
                _flat([10.0] * 10), ema_fast=2, ema_slow=3, atr_period=2
            ),
        },
    }


# --------------------------------------------------------------------------- #
# Jeu 2 : contrat de localisation Android
# --------------------------------------------------------------------------- #


def _string_nodes(path: pathlib.Path) -> Dict[str, Dict[str, Optional[str]]]:
    root = ET.parse(path).getroot()
    return {
        node.get("name"): {
            "text": node.text or "",
            "translatable": node.get("translatable"),
            "formatted": node.get("formatted"),
        }
        for node in root.findall("string")
    }


def _localization_files() -> Dict[str, pathlib.Path]:
    """Langue -> fichier de chaînes, par **scan** du dossier (pas une liste figée).

    Un `values-<code>/strings.xml` ajouté à la main apparaît donc dans le
    golden : c'est le contrôle qui manquerait si la liste des langues était
    écrite en dur ici.
    """
    files = {"values": RES_DIR / "values" / "strings.xml"}
    for directory in sorted(RES_DIR.glob("values-*")):
        path = directory / "strings.xml"
        if path.is_file():
            files[directory.name] = path
    return files


def android_localization_values() -> Dict[str, Any]:
    """Contrat de localisation : quelles clés existent, et dans quelles langues."""
    files = _localization_files()
    default = _string_nodes(files["values"])
    non_translatable = sorted(
        name for name, node in default.items() if node["translatable"] == "false"
    )
    manifest_only = sorted(MANIFEST_ONLY_KEYS & set(default))
    translatable = set(default) - set(non_translatable) - set(manifest_only)

    locales: Dict[str, Any] = {}
    for label, path in files.items():
        if label == "values":
            continue
        keys = set(_string_nodes(path))
        locales[label] = {
            "count": len(keys),
            "missing": sorted(translatable - keys),
            "extra": sorted(keys - translatable),
        }

    # Les arguments de format sont un contrat (`String.format` lève si un `%2$s`
    # disparaît) : les figer ici rend visible tout changement de signature.
    format_args = {
        name: sorted(_format_args(node["text"]))
        for name, node in default.items()
        if _format_args(node["text"])
    }
    return {
        "default_keys": sorted(default),
        "non_translatable_keys": non_translatable,
        "manifest_only_keys": manifest_only,
        "locales": locales,
        "default_format_args": format_args,
    }


def _format_args(text: str) -> List[str]:
    """Arguments de format d'une chaîne, dans l'ordre d'apparition.

    Un `%%` littéral n'est pas un argument : c'est un pour cent affiché.
    """
    found: List[str] = []
    index = 0
    while index < len(text):
        if text[index] == "%" and text[index : index + 2] != "%%":
            match = FORMAT_ARG.match(text, index)
            if match:
                found.append(match.group(0))
                index = match.end()
                continue
        index += 1
    return found


# --------------------------------------------------------------------------- #
# Jeu 3 : surface HTTP publique
# --------------------------------------------------------------------------- #

#: Programme d'extraction, exécuté dans un sous-processus. Il fixe un
#: environnement **factice** (`setdefault` : en CI les vraies valeurs, si elles
#: existent, sont conservées) pour que l'import de l'application n'exige aucun
#: secret et ne dépende d'aucun. Le sous-processus isole aussi l'import : la
#: configuration de `config.py`, figée à l'import, n'est pas polluée pour les
#: autres tests.
_ROUTE_PROBE = textwrap.dedent(
    """
    import json, os
    PLACEHOLDERS = {
        "INTERNAL_API_KEY": "golden-probe-internal-key",
        "WEBHOOK_SECRET": "golden-probe-webhook-secret",
        "TELEGRAM_BOT_TOKEN": "123456789:AAF7c3d2e1b0a9f8e7d6c5b4a3f2e1d0",
        "SUPABASE_URL": "https://golden-probe.supabase.co",
        "SUPABASE_SERVICE_KEY": "golden-probe-service-key-0123456789",
        "ENCRYPTION_KEY": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",
        "PAPER_TRADING": "true",
        "HEADLESS": "true",
    }
    for name, value in PLACEHOLDERS.items():
        os.environ.setdefault(name, value)
    try:
        from api.webhook import app
    except ImportError:
        raise SystemExit("UNAVAILABLE: fastapi (ou httpx) n'est pas installe")
    paths = app.openapi().get("paths", {})
    print(json.dumps(sorted(f"{m.upper()} {p}" for p, ops in paths.items() for m in ops)))
    """
).strip()


def api_routes_values() -> Dict[str, Any]:
    """Surface HTTP de `api.webhook.app` : méthode + gabarit de chemin, triée."""
    environment = dict(os.environ)
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONPATH"] = str(REPO_ROOT)
    proc = subprocess.run(
        [sys.executable, "-c", _ROUTE_PROBE],
        cwd=str(REPO_ROOT),
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else "aucune sortie"
        if "UNAVAILABLE" in tail:
            raise Unavailable("fastapi n'est pas installé : surface HTTP non calculable")
        raise RuntimeError(f"extraction des routes impossible : {tail}")
    lines = [line for line in (proc.stdout or "").splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("extraction des routes impossible : aucune sortie")
    return {"routes": json.loads(lines[-1])}


# --------------------------------------------------------------------------- #
# Jeu 4 : seuil de similarité calibré sur la base
# --------------------------------------------------------------------------- #

#: Corpus joués dans le golden : des vecteurs **fixes** (aucune base, aucun
#: réseau), qui modélisent les trois régimes que la calibration doit distinguer.
#: En 2D, l'angle entre un document et une sonde **est** leur similarité cosinus :
#: c'est ce qui rend ces corpus lisibles (et vérifiables à la main).
#:
#: * `mid_noise_base` — corpus au jargon partagé (le cas d'un ensemble de
#:   graphiques et de transcriptions traitant tous de trading) : les sondes sont
#:   hors sujet mais dans la même région de l'espace, le bruit est haut → le seuil
#:   calibré passe **au-dessus** de la constante 0,5, qui laissait passer du bruit ;
#: * `low_noise_base` — base discriminante (sondes orthogonales aux documents) :
#:   le bruit est bas → le seuil passe **en dessous** de 0,5, là où la constante
#:   jetait des correspondances utiles ;
#: * `high_noise_base` — documents et sondes confondus : la mesure dit que la base
#:   ne distingue plus rien, et le seuil bute sur sa borne haute.
#:
#: Les sonde sont des **directions**, pas des textes : ce sont les 4 phrases de
#: `NOISE_PROBE_QUERIES`, dont on ne joue ici que la géométrie.
CALIBRATION_MID_NOISE_DOCS = [
    [0.087156, 0.996195],
    [0.034899, 0.999391],
    [0.0, 1.0],
    [-0.034899, 0.999391],
    [-0.087156, 0.996195],
]
CALIBRATION_MID_NOISE_PROBES = [[0.819152, 0.573576], [-0.819152, 0.573576]]
CALIBRATION_LOW_NOISE_DOCS = [
    [0.996195, -0.087156],
    [0.999391, -0.034899],
    [1.0, 0.0],
    [0.999391, 0.034899],
    [0.996195, 0.087156],
]
CALIBRATION_LOW_NOISE_PROBES = [[0.087156, 0.996195], [-0.087156, 0.996195]]
CALIBRATION_HIGH_NOISE_DOCS = [
    [0.743145, 0.669131],
    [0.719340, 0.694658],
    [0.707107, 0.707107],
    [0.694658, 0.719340],
    [0.669131, 0.743145],
]
CALIBRATION_HIGH_NOISE_PROBES = [[0.707107, 0.707107], [0.669131, 0.743145]]

#: Morceaux candidats pour la règle de **sélection** des extraits média, tels que
#: `match_knowledge_chunks` les remonte (média, similarité, étiquette). Chaque ligne
#: existe pour un cas qui décide d'une injection : un média étiqueté pour l'actif
#: mais peu proche, un média proche sans étiquette, une étiquette portée par un
#: **autre** actif, du bruit non étiqueté, et une étiquette posée sur un contenu
#: sans rapport (le cas de l'erreur de saisie).
MEDIA_SELECTION_CANDIDATES = [
    {"media_id": "tagged-close", "content": "volume BTC", "similarity": 0.88, "asset": "BTC-USD"},
    {"media_id": "close-untagged", "content": "cassure BTC", "similarity": 0.75, "asset": None},
    {"media_id": "tagged-far", "content": "photo du graphique", "similarity": 0.60, "asset": "BTC-USD"},
    {"media_id": "weak-untagged", "content": "notes", "similarity": 0.62, "asset": None},
    {"media_id": "other-asset", "content": "range ETH", "similarity": 0.45, "asset": "ETH-USD"},
    {"media_id": "noise-untagged", "content": "réunion", "similarity": 0.22, "asset": None},
    {"media_id": "tagged-off-topic", "content": "recette", "similarity": 0.19, "asset": "BTC-USD"},
]


def _calibration_record(
    document_vectors: Sequence[Sequence[float]], probe_vectors: Sequence[Sequence[float]]
) -> Dict[str, Any]:
    """Distribution du bruit et seuil retenu, sur des vecteurs donnés.

    Les scores bruts ne sont pas enregistrés (bruit de diff inutile) : ce sont les
    statistiques et le seuil qui font référence, et `tests/test_knowledge_index.py`
    vérifie que le seuil est bien recalculé depuis les scores. Les vecteurs sont
    repris, eux : sans eux, le golden ne dirait plus ce qu'il mesure.
    """
    from database import knowledge_index as ki

    distribution = ki.noise_distribution(list(document_vectors), list(probe_vectors))
    return {
        "vectors": [[_round(v) for v in vector] for vector in document_vectors],
        "probe_vectors": [[_round(v) for v in vector] for vector in probe_vectors],
        "documents": len(document_vectors),
        "probes": len(probe_vectors),
        "pairs": distribution["pairs"],
        "min": _round(distribution["min"]),
        "median": _round(distribution["median"]),
        "p75": _round(distribution["p75"]),
        "p90": _round(distribution["p90"]),
        "p95": _round(distribution["p95"]),
        "max": _round(distribution["max"]),
        "noise_at_percentile": _round(
            ki.percentile(distribution["scores"], ki.NOISE_PERCENTILE)
        ),
        "floor": _round(ki.similarity_floor(distribution)),
        # Le percentile est un paramètre : ces trois seuils figent la montée, donc
        # le fait que le seuil soit bien déduit de la distribution à chaque appel.
        "floor_at_p50": _round(ki.similarity_floor(distribution, percentile_value=50)),
        "floor_at_p90": _round(ki.similarity_floor(distribution, percentile_value=90)),
        "floor_at_p100": _round(ki.similarity_floor(distribution, percentile_value=100)),
    }


def _media_selection_record(floor: Optional[float]) -> Dict[str, Any]:
    """Extraits retenus par la règle de sélection, pour un seuil donné.

    On appelle la **vraie** fonction (`knowledge_index.select_media_candidates`),
    pas une copie de sa formule : ce qui est verrouillé ici, c'est la politique —
    étiquette d'abord, étiquette qui passe le seuil — et pas une réécriture qui
    pourrait diverger d'elle.
    """
    from database import knowledge_index as ki

    kept = ki.select_media_candidates(
        MEDIA_SELECTION_CANDIDATES,
        target="BTC-USD",
        top_k=len(MEDIA_SELECTION_CANDIDATES),
        min_similarity=floor,
    )
    return {
        "floor": _round(floor) if floor is not None else None,
        "kept": [
            {
                "media_id": row["media_id"],
                "similarity": _round(row["similarity"]),
                "tagged": row["tagged"],
            }
            for row in kept
        ],
    }


def similarity_calibration_values() -> Dict[str, Any]:
    """Seuil de similarité des médias : statistiques et formule de calibration.

    La constante 0,5 n'est plus la règle (elle se trompait dans les deux sens) :
    le seuil est un percentile élevé du **bruit mesuré sur la base**, plus une
    marge. Ce que ce golden verrouille, c'est la formule, ses bornes, et le fait
    qu'un corpus homogène donne un seuil plus strict qu'un corpus varié.
    """
    from database import knowledge_index as ki

    return {
        "constants": {
            "NOISE_PERCENTILE": ki.NOISE_PERCENTILE,
            "NOISE_MARGIN": ki.NOISE_MARGIN,
            "FLOOR_BOUNDS": list(ki.FLOOR_BOUNDS),
            "CALIBRATION_DOCS": ki.CALIBRATION_DOCS,
            "CALIBRATION_MIN_DOCS": ki.CALIBRATION_MIN_DOCS,
            "CALIBRATION_TTL_SECONDS": ki.CALIBRATION_TTL_SECONDS,
            "probes": list(ki.NOISE_PROBE_QUERIES),
            # L'unité du seuil doit être celle de pgvector (`<=>`), sinon la
            # comparaison côté SQL n'a plus le même sens que la mesure.
            "cosine_hand_check": _round(ki.cosine_similarity([3.0, 4.0], [4.0, 3.0])),
            "cosine_orthogonal": _round(ki.cosine_similarity([1.0, 0.0], [0.0, 1.0])),
            "percentile_median_1_to_4": _round(ki.percentile([1, 2, 3, 4], 50)),
        },
        "media_selection": {
            # Le seuil du corpus « mid » est réutilisé ici : c'est celui qu'une base
            # de trading réelle produit, donc le cas où la comparaison étiquette vs
            # similarité se joue vraiment.
            "with_measured_floor": _media_selection_record(
                ki.similarity_floor(
                    ki.noise_distribution(
                        CALIBRATION_MID_NOISE_DOCS, CALIBRATION_MID_NOISE_PROBES
                    )
                )
            ),
            # Corpus très homogène : le seuil bute sur 0,9, et seules les étiquettes
            # explicites passent encore — l'inverse d'une base aveugle à ses tags.
            "with_strict_floor": _media_selection_record(ki.FLOOR_BOUNDS[1]),
            # Sans seuil : la règle reste un classement, étiquette d'abord.
            "without_floor": _media_selection_record(None),
            "tagged_hard_floor": _round(ki.TAGGED_HARD_FLOOR),
            "candidates": [dict(candidate) for candidate in MEDIA_SELECTION_CANDIDATES],
        },
        "mid_noise_base": _calibration_record(
            CALIBRATION_MID_NOISE_DOCS, CALIBRATION_MID_NOISE_PROBES
        ),
        "low_noise_base": _calibration_record(
            CALIBRATION_LOW_NOISE_DOCS, CALIBRATION_LOW_NOISE_PROBES
        ),
        "high_noise_base": _calibration_record(
            CALIBRATION_HIGH_NOISE_DOCS, CALIBRATION_HIGH_NOISE_PROBES
        ),
    }


# --------------------------------------------------------------------------- #
# Jeu 5 : schéma PostgreSQL obtenu, au `pg_dump --schema-only`
# --------------------------------------------------------------------------- #


def postgres_schema_values() -> Dict[str, Any]:
    """Le schéma **retenu** par Postgres, pris sur une base bâtie des migrations.

    Ce que ce jeu ajoute aux contrats de migration : le corps compilé des
    fonctions, la définition exacte des index (méthode, opclasse, `desc`, `where`),
    le type et le défaut que `bigserial` produit, les contraintes telles que
    Postgres les a acceptées. Tout cela vit dans la base, pas dans le texte des
    fichiers — et rien d'autre ne le relit.

    Les ingrédients manquants sont annoncés (`Unavailable`) : sans base ni
    `pg_dump`, ce jeu est **non exécuté**, et le gate le dit au lieu de passer au
    vert. La CI le recalcule là où une base existe (job `migrations-postgres`).
    """
    from scripts import schema_snapshot

    try:
        return schema_snapshot.snapshot_values()
    except schema_snapshot.Unavailable as exc:
        raise Unavailable(str(exc)) from exc


# --------------------------------------------------------------------------- #
# Harnais
# --------------------------------------------------------------------------- #

DATASETS: Tuple[Dataset, ...] = (
    Dataset(
        "alert_engine",
        "moteur d'alerte : EMA/ATR sur la série documentée + sortie complète de detect_cross (SL, TP, message)",
        alert_engine_values,
    ),
    Dataset(
        "android_localization",
        "contrat de localisation : clés par langue, clés non traduisibles, arguments de format",
        android_localization_values,
    ),
    Dataset(
        "api_routes",
        "surface HTTP publique (méthode + chemin) de api.webhook.app",
        api_routes_values,
    ),
    Dataset(
        "similarity_calibration",
        "seuil de similarité des médias : bruit mesuré sur la base, percentile, marge, "
        "bornes, et extraits que la règle de sélection retient",
        similarity_calibration_values,
    ),
    Dataset(
        "postgres_schema",
        "schéma public obtenu par les migrations, au `pg_dump --schema-only` : corps de "
        "fonctions, définitions exactes d'index, types, défauts et contraintes",
        postgres_schema_values,
        requires=(
            f"un serveur PostgreSQL joignable via {MIGRATION_ENV_VAR} (une base "
            "jetable y est créée puis supprimée), `pg_dump` dans le PATH ou via "
            "PG_DUMP, et psycopg"
        ),
    ),
)


def dataset_by_name(name: str) -> Dataset:
    for dataset in DATASETS:
        if dataset.name == name:
            return dataset
    known = ", ".join(dataset.name for dataset in DATASETS)
    raise KeyError(f"jeu de valeurs inconnu : {name} (connus : {known})")


def golden_path(dataset: Dataset, directory: pathlib.Path = DEFAULT_DIR) -> pathlib.Path:
    return pathlib.Path(directory) / f"{dataset.name}.json"


def render(dataset: Dataset, values: Any) -> str:
    """Représentation canonique : JSON trié, UTF-8, deux espaces, saut final."""
    payload = {
        "_comment": (
            "Valeur de référence versionnée : ne pas éditer à la main. "
            f"Régénérer avec `{UPDATE_COMMAND} {dataset.name}`."
        ),
        "dataset": dataset.name,
        "description": dataset.description,
        "values": values,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def read_recorded(
    dataset: Dataset, directory: pathlib.Path = DEFAULT_DIR
) -> Tuple[Optional[Any], Optional[str]]:
    """(valeurs versionnées, problème de lecture) — l'un des deux est `None`."""
    path = golden_path(dataset, directory)
    if not path.is_file():
        return None, f"fichier absent : {_relative(path)}"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"{_relative(path)} illisible ({exc})"
    if not isinstance(payload, dict) or "values" not in payload:
        return None, f"{_relative(path)} ne contient pas de clé « values »"
    if payload.get("dataset") != dataset.name:
        return None, (
            f"{_relative(path)} déclare le jeu « {payload.get('dataset')} » "
            f"au lieu de « {dataset.name} »"
        )
    return payload["values"], None


def write_golden(dataset: Dataset, directory: pathlib.Path = DEFAULT_DIR) -> pathlib.Path:
    """Régénère le fichier depuis le code courant. Renvoie le chemin écrit."""
    directory = pathlib.Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = golden_path(dataset, directory)
    # `newline="\n"` : le fichier doit être identique qu'on le régénère sous
    # Windows ou sous Linux (`.editorconfig` impose LF), sinon le diff d'un
    # golden régénéré sur un poste ferait apparaître tout le fichier.
    path.write_text(render(dataset, dataset.build()), encoding="utf-8", newline="\n")
    return path


def check(dataset: Dataset, directory: pathlib.Path = DEFAULT_DIR) -> Outcome:
    """Compare les valeurs du code aux valeurs versionnées, ou explique l'échec."""
    path = golden_path(dataset, directory)
    # Le calcul vient en premier : un jeu indisponible dans cet environnement
    # (FastAPI absent) est annoncé comme **non exécuté**, même si son golden
    # n'existe pas encore — sinon on annoncerait une dérive là où le contrôle
    # n'a simplement pas tourné.
    try:
        current = dataset.build()
    except Unavailable as exc:
        needed = f"\n  Pour le recalculer : {dataset.requires}" if dataset.requires else ""
        return Outcome(
            dataset.name, "skip", f"[--] {dataset.name} ignoré : {exc}{needed}"
        )
    except Exception as exc:  # pragma: no cover - dépend de l'environnement
        return Outcome(
            dataset.name,
            "error",
            "\n".join(
                [
                    f"[ÉCHEC] {dataset.name} : valeurs de référence non recalculables.",
                    f"  {type(exc).__name__}: {exc}",
                    "",
                    "  Le golden n'est donc PAS validé. Corrige le code, puis relance :",
                    f"      {UPDATE_COMMAND} {dataset.name}",
                ]
            ),
        )
    recorded, problem = read_recorded(dataset, directory)
    if problem is not None:
        return Outcome(dataset.name, "error", _missing_message(dataset, path, problem))
    if recorded == current:
        return Outcome(dataset.name, "ok", "")
    return Outcome(dataset.name, "changed", _drift_message(dataset, path, recorded, current))


def check_all(directory: pathlib.Path = DEFAULT_DIR) -> List[Outcome]:
    return [check(dataset, directory) for dataset in DATASETS]





# --------------------------------------------------------------------------- #
# Messages : ce que lit la personne dont la CI vient de rougir
# --------------------------------------------------------------------------- #


def _relative(path: pathlib.Path) -> str:
    try:
        return str(pathlib.Path(path).resolve().relative_to(REPO_ROOT))
    except ValueError:  # pragma: no cover - chemin hors dépôt (tests)
        return str(path)


def _regeneration_lines(dataset: Dataset) -> List[str]:
    return [
        "  Si le changement est VOULU : régénère le golden, relis le diff du JSON,",
        "  puis commite le fichier dans le MÊME commit que le changement de code :",
        f"      {UPDATE_COMMAND} {dataset.name}",
        f"      {UPDATE_COMMAND}                          (tous les jeux de valeurs)",
        *(f"  Pour ce jeu : {dataset.requires}" for _ in [0] if dataset.requires),
        "",
        "  Si le changement n'est PAS voulu, corrige le code : ce fichier est la",
        "  référence. Ne l'édite jamais à la main, sinon il ne référence plus rien.",
    ]


def _drift_message(dataset: Dataset, path: pathlib.Path, recorded: Any, current: Any) -> str:
    before = render(dataset, recorded).splitlines()
    after = render(dataset, current).splitlines()
    diff = list(
        difflib.unified_diff(
            before,
            after,
            fromfile=f"{_relative(path)} (versionné)",
            tofile=f"{_relative(path)} (recalculé depuis le code)",
            lineterm="",
        )
    )
    shown = diff[:DIFF_LIMIT]
    if len(diff) > DIFF_LIMIT:
        shown.append(f"... {len(diff) - DIFF_LIMIT} lignes de diff en plus")
    return "\n".join(
        [
            f"[ÉCHEC] {dataset.name} : la valeur de référence a changé.",
            f"  {dataset.description}",
            f"  Fichier versionné : {_relative(path)}",
            "",
            "  Différences :",
            *[f"    {line}" for line in shown],
            "",
            *_regeneration_lines(dataset),
        ]
    )


def _missing_message(dataset: Dataset, path: pathlib.Path, problem: str) -> str:
    return "\n".join(
        [
            f"[ÉCHEC] {dataset.name} : {problem}",
            "  Sans fichier de valeurs de référence, il n'y a rien à comparer : le",
            "  gate refuse de passer au vert (un golden manquant est un golden modifié).",
            f"  Attendu : {_relative(path)}",
            "",
            *_regeneration_lines(dataset),
        ]
    )


# --------------------------------------------------------------------------- #
# Ligne de commande
# --------------------------------------------------------------------------- #


def main(argv: Sequence[str]) -> int:
    #: Sans cela, un message d'erreur non représentable en cp1252 ferait échouer
    #: l'affichage du gate lui-même — la panne serait donc dans le contrôle.
    console.make_streams_utf8()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare les valeurs du code aux goldens et échoue en cas de dérive (défaut)",
    )
    parser.add_argument(
        "--update",
        nargs="*",
        metavar="JEU",
        help="régénère les goldens (tous, ou seulement ceux nommés)",
    )
    parser.add_argument("--list", action="store_true", help="liste les jeux de valeurs connus")
    parser.add_argument(
        "--dir",
        default=str(DEFAULT_DIR),
        help=f"dossier des goldens (défaut : {_relative(DEFAULT_DIR)})",
    )
    args = parser.parse_args(argv)
    directory = pathlib.Path(args.dir)

    if args.list:
        for dataset in DATASETS:
            print(f"{dataset.name:<22} {dataset.description}")
        print(f"fichiers : {_relative(directory)}/<jeu>.json")
        return 0

    if args.update is not None:
        if not args.update:
            print(f"Régénération de tous les goldens dans {_relative(directory)} :")
        try:
            datasets = (
                [dataset_by_name(name) for name in args.update]
                if args.update
                else list(DATASETS)
            )
        except KeyError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        # Un jeu à la fois : les ingrédients d'un jeu peuvent manquer **ici** (pas
        # de serveur PostgreSQL, pas de `pg_dump`) alors que les autres se
        # régénèrent très bien. S'arrêter au premier venu laisserait les précédents
        # déjà écrits sans le dire, et une trace d'exception ne dirait pas quoi
        # faire. Chaque fichier écrit est annoncé **au fur et à mesure**.
        for dataset in datasets:
            try:
                path = write_golden(dataset, directory)
            except Unavailable as exc:
                print(f"[ÉCHEC] {dataset.name} : régénération impossible — {exc}", file=sys.stderr)
                if dataset.requires:
                    print(f"  Ce jeu se recalcule là où : {dataset.requires}", file=sys.stderr)
                print(
                    "  Les jeux précédents ont pu être écrits, celui-ci non : "
                    "`git status` le dit.",
                    file=sys.stderr,
                )
                return 1
            print(f"  écrit  {_relative(path)}")
        return 0

    failures = 0
    for outcome in check_all(directory):
        if outcome.status == "ok":
            print(f"[OK] {outcome.name} : valeurs de référence à jour")
        else:
            failures += outcome.status != "skip"
            print(outcome.message)
    if failures:
        print(
            f"\n{failures} jeu(x) de valeurs de référence en dérive. "
            "Régénère-les explicitement (voir ci-dessus) ou corrige le code."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
