"""Vérifie la configuration Supabase, puis la base elle-même.

Trois niveaux, du moins cher au plus engageant :

1. **la configuration** — `SUPABASE_URL` est bien l'URL de l'**API** du projet
   (pas la base Postgres, pas un `/rest/v1` collé du navigateur) et
   `SUPABASE_SERVICE_KEY` porte bien le rôle `service_role`. C'est le contrôle qui
   attrape la confusion anon/service_role : la RLS de ce projet est en
   « deny by default » sans aucune policy (migrations 005 à 011), donc une clé
   publique ne lit ni n'écrit **rien** — sans lever, jamais ;
2. **la lecture** — chacune des tables que l'application utilise répond
   (`REQUIRED_TABLES`) ; une table absente signale une migration non appliquée ;
3. **l'aller-retour** (`--roundtrip`) — écriture, relecture et **suppression** sur
   les **sept** tables que l'application utilise (`--only` les restreint : c'est le
   seul mode où la sonde n'écrit pas partout) : les trois de la migration 011
   (`users`, `insights`, `pending_signals`), puis les quatre des migrations 005
   et 006 (`economic_events`, `macro_bias_logs`, `trade_post_mortems`,
   `adaptive_model_weights`). La relecture passe par les fonctions réelles de
   l'application (`get_recent_insights`, `get_economic_events`,
   `get_learning_summary`, `get_adaptive_parameters`…) plutôt que par des
   requêtes réécrites ici. Ce que la sonde écrit, elle le **supprime** : la cible
   de nettoyage d'une table est posée avant son écriture, jamais après son
   verdict. L'absence de reste se vérifie sur la base
   (`tests/test_supabase_config.py::RoundtripLeakTest`) : une panne est injectée à
   chaque étape des sept tables et rien ne doit survivre — sauf la ligne qu'une
   suppression refusée laisse **et nomme** —, tandis que des lignes qui ne sont pas
   celles de la sonde doivent, elles, survivre au passage.

Deux de ces tables font exception à la règle « tout passe par l'application »,
volontairement : `trade_post_mortems` et `adaptive_model_weights` sont écrites
**directement**. Leur unique chemin applicatif,
`record_trade_settlement_and_learn()`, recalcule les poids de l'actif et
**réécrit une note de `knowledge_base`** à partir des derniers post-mortems : une
sonde de configuration n'a pas à modifier une ligne qui n'est pas la sienne, et
le nettoyage ne pourrait pas la restaurer. Elles sont en revanche **relues** par
l'application, ce qui est la moitié qui compte — une ligne que l'application sait
relire est une ligne utilisable.

```bash
python scripts/check_supabase.py                # lecture seule
python scripts/check_supabase.py --roundtrip    # + écriture/lecture/suppression
python scripts/check_supabase.py --json         # sortie exploitable par un script

# Viser une table, ou une famille : le reste n'est plus interrogé.
python scripts/check_supabase.py --only economic_events
python scripts/check_supabase.py --roundtrip --only economic_events
python scripts/check_supabase.py --only engine        # un groupe nommé
python scripts/check_supabase.py --only users,insights
```

`--only` restreint les trois niveaux de table (lecture, tables facultatives,
aller-retour) à ce qui est visé : c'est ce qu'on veut quand **une** table est en
cause et qu'on ne veut pas relire les autres, et c'est le seul mode où
`--roundtrip` n'écrit pas sur les sept tables. Un nom de **groupe** désigne une
famille (voir `TABLE_GROUPS` : `core`, `engine`, `knowledge`). Un nom **inconnu**
est un refus **franc** — code 2, message nommant les choix possibles —, jamais un
repli sur « tout » : une faute de frappe qui déclencherait l'écriture sur les sept
tables est exactement ce qu'on cherche à éviter.

Codes de sortie : `0` tout est bon, `1` au moins un échec, `2` mauvais usage,
`3` nettoyage **incomplet**. Le troisième est distinct parce qu'il ne se confond
avec aucun autre : les échecs ordinaires se corrigent et se rejouent, celui-là a
**laissé des lignes de sonde en base** — une automatisation qui traite `1` comme
« réessayer » ne doit pas prendre les deux pour la même chose. Un nettoyage
incomplet écrit aussi son **journal sur `stderr`** (pour que `--json` reste
lisible sur `stdout`) et envoie une **alerte** au chat d'exploitation
(`TELEGRAM_ADMIN_CHAT_ID`).

Les identifiants (`SUPABASE_URL`, `SUPABASE_SERVICE_KEY`) viennent de
l'environnement **ou** du `.env` de la racine, chargé exactement comme
l'application le fait (`config.py`) ; une variable exportée dans le shell reste
prioritaire sur le fichier, ce qui permet de viser une autre base sans le
modifier.

L'aller-retour **écrit dans la base pointée par ce `.env`** — y compris en
production. Il nettoie derrière lui (ses lignes portent des identifiants
`probe-…` et un actif `PROBE-…` **unique à l'exécution**), en réessayant une
suppression qui échoue avant de conclure quoi que ce soit : un transitoire ne doit
pas laisser de ligne derrière lui. Si la suppression échoue vraiment, c'est dit
explicitement, avec le détail à supprimer à la main.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import inspect
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(PROJECT_ROOT))

from core import config_runtime, console  # noqa: E402
from core.adaptive_learning import get_adaptive_parameters, get_learning_summary  # noqa: E402
from database import supabase_client  # noqa: E402
from database.supabase_client import (  # noqa: E402
    OPTIONAL_TABLES,
    REQUIRED_TABLES,
    client_status,
    create_pending_signal,
    get_economic_events,
    get_recent_insights,
    insert_insight,
    log_macro_decision,
    update_signal_status,
    upsert_economic_events,
)

#: Le `.env` de la racine, c'est-à-dire le fichier que l'application charge
#: elle-même (`config.py`).
PROJECT_ENV = PROJECT_ROOT / ".env"

OK = "✅"
FAIL = "❌"

#: Codes de sortie, nommés parce qu'ils **sont** le contrat de l'outil vis-à-vis
#: d'un script ou d'une CI. `EXIT_LEFTOVER` a son propre code : un nettoyage
#: incomplet n'est pas un échec comme un autre, il laisse des lignes en base.
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_LEFTOVER = 3

#: Le nom de la vérification qui porte le nettoyage de l'aller-retour. Elle est
#: aussi la seule à publier `leftovers` : c'est ce que le code de sortie,
#: l'alerte et la route d'administration lisent, sans avoir à relire un détail
#: écrit en français.
CLEANUP_CHECK = "nettoyage"

#: Préfixe des lignes de journal de la sonde — le même esprit que
#: `[réconciliation]` dans `workers/media_reconcile.py`.
JOURNAL_TAG = "[sonde supabase]"

#: Attente entre deux tentatives de suppression d'une ligne de sonde (secondes),
#: **croissante** et **courte**. Une suppression qui échoue une fois est le plus
#: souvent un transitoire — coupure réseau, `503`, pool saturé —, et déclarer un
#: « reste en base » sur ce hoquet coûte deux fois : il fait sortir la sonde en `3`
#: (le code qu'on ne rejoue pas) et envoie l'opérateur supprimer à la main une
#: ligne qui n'existe déjà plus. Les reprises sont **sûres** : supprimer une ligne
#: déjà partie réussit, donc réessayer ne peut rien casser ni rien effacer qui ne
#: soit pas à la sonde. Le total reste court — 1,5 s au pire par ligne —, une
#: sonde de configuration qui s'endort ne rend plus service à une CI.
CLEANUP_RETRY_DELAYS = (0.5, 1.0)

#: Paquets du projet dont un module peut avoir lié le client Supabase **à son
#: import**. Sert à `client_in_place()` : voir sa docstring.
PROJECT_PACKAGES = (
    "ai",
    "api",
    "core",
    "database",
    "execution",
    "notifications",
    "reports",
    "scrapers",
    "utils",
    "workers",
)

#: Les tables que l'**aller-retour** éprouve, dans l'ordre où il les exerce. Les
#: noms sont ceux de `database.supabase_client` (une table renommée là-bas ne doit
#: pas devenir une sonde fantôme ici), et cette liste est ce qui permet de dire ce
#: qu'une sélection `--only` restreint — sans elle, `--only knowledge_base
#: --roundtrip` aurait l'air de ne rien avoir à faire sans que rien ne le dise.
ROUNDTRIP_TABLES = (
    "users",
    "insights",
    "pending_signals",
    "economic_events",
    "macro_bias_logs",
    "trade_post_mortems",
    "adaptive_model_weights",
)

#: Groupes nommés, pour viser une famille de tables sans l'énumérer. Chacun suit
#: une raison de les regarder ensemble : une migration, un domaine. Les noms de
#: tables viennent de `REQUIRED_TABLES` / `OPTIONAL_TABLES` — un groupe ne connaît
#: pas de table que le client ne connaît pas.
TABLE_GROUPS = {
    "core": ("users", "insights", "pending_signals"),
    "engine": (
        "economic_events",
        "macro_bias_logs",
        "trade_post_mortems",
        "adaptive_model_weights",
    ),
    "knowledge": ("knowledge_base", "knowledge_chunks", "knowledge_media"),
}

#: Correspondance insensible à la casse : `--only ECONOMIC_EVENTS` vise la table
#: `economic_events` (Postgres ne connaît que les minuscules, mais la casse tapée
#: n'est pas une intention différente).
_TABLE_BY_LOWER = {table.lower(): table for table in (*REQUIRED_TABLES, *OPTIONAL_TABLES)}

#: Sentinelle : distingue « le module n'a pas d'attribut `supabase` » de
#: « l'attribut vaut `None` » — sans quoi on écrirait dans des modules qui n'ont
#: rien à voir avec la base.
_MISSING = object()


def load_project_env() -> bool:
    """Charge `<racine>/.env`, comme le fait `config.py` pour l'application.

    Sans cet appel, l'outil ne verrait que les variables d'environnement du
    processus : renseigner `.env` — ce que demande le README, et ce que fait
    l'application — suffirait à faire tourner le bot, mais pas cette
    vérification. La divergence est le pire des cas : tout paraîtrait configuré
    d'un côté et vide de l'autre.

    Une variable **déjà présente dans l'environnement l'emporte** sur le fichier
    (défaut de `python-dotenv`) : on peut donc viser une autre base sans toucher
    au `.env`. L'absence du fichier ou de la bibliothèque n'est pas une erreur —
    on continue avec l'environnement seul, comme le dépôt le fait partout.

    Rend `True` si un fichier a été lu.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return False
    return bool(load_dotenv(PROJECT_ENV))


def result(
    name: str, ok: bool, detail: str = "", leftovers: Optional[List[str]] = None
) -> Dict[str, Any]:
    """Une vérification : son nom, son verdict, son détail.

    `leftovers` n'est renseigné que par le nettoyage de l'aller-retour — mais il
    l'est **toujours** quand il s'agit de lui, vide compris : ce qui reste en base
    est une donnée du rapport, pas une phrase à relire, et un consommateur n'a pas
    à distinguer « aucun reste » de « champ absent ». `run()` en fait une liste à
    part, `main()` en tire un code de sortie et une alerte, la route la rend telle
    quelle.
    """
    check = {"name": name, "ok": bool(ok), "detail": detail}
    if leftovers is not None:
        check["leftovers"] = list(leftovers)
    return check


def resolve_only(names: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """Résout une sélection `--only` en tables concrètes, et nomme les inconnues.

    Accepte un nom de table (`economic_events`), un nom de **groupe**
    (`engine` — voir `TABLE_GROUPS`) et plusieurs valeurs séparées par des
    virgules ou par la répétition de l'option. L'ordre rendu est celui de
    `REQUIRED_TABLES` puis `OPTIONAL_TABLES`, **pas** l'ordre de frappe : un
    rapport filtré doit se lire comme un rapport complet.

    Ne lève jamais. Rendre les noms inconnus — plutôt que de les ignorer ou de les
    traiter comme « aucun filtre » — est tout l'intérêt : une faute de frappe doit
    se solder par un refus, car avec `--roundtrip` un repli sur « tout » écrirait
    précisément dans les tables qu'on voulait éviter.

    `None` (option absente) et une liste vide ne sont **pas** une erreur : c'est
    « aucune sélection », donc **toutes** les tables. `--only` sans valeur, en
    revanche, en est une — une chaîne vide est une option qu'on a tapée, pas une
    absence d'option.
    """
    raw = list(names or [])
    if not raw:
        return {
            "requested": [],
            "required": list(REQUIRED_TABLES),
            "optional": list(OPTIONAL_TABLES),
            "unknown": [],
            "problem": "",
            "ok": True,
        }

    tokens: List[str] = []
    for name in raw:
        tokens.extend(part.strip() for part in str(name).split(","))
    tokens = [token for token in tokens if token]

    wanted: set = set()
    unknown: List[str] = []
    for token in tokens:
        key = token.lower()
        if key in TABLE_GROUPS:
            wanted.update(TABLE_GROUPS[key])
        elif key in _TABLE_BY_LOWER:
            wanted.add(_TABLE_BY_LOWER[key])
        else:
            unknown.append(token)

    problem = ""
    if not tokens:
        problem = "« --only » demande au moins un nom de table ou de groupe."
    elif unknown:
        problem = "Nom inconnu : " + ", ".join(unknown) + "."
    return {
        "requested": tokens,
        "required": [table for table in REQUIRED_TABLES if table in wanted],
        "optional": [table for table in OPTIONAL_TABLES if table in wanted],
        "unknown": unknown,
        "problem": problem,
        "ok": not problem,
    }


def selection_problem(selection: Dict[str, Any]) -> str:
    """Le message d'un nom inconnu — **un seul endroit**, donc toujours le même.

    Les noms connus sont tous donnés, groupes compris : après un refus, ce qu'on
    cherche est précisément quoi taper. Le message sert au script (stderr, code 2)
    comme au rapport, pour que les deux ne divergent pas.
    """
    return (
        f"{selection['problem']}\n"
        f"   Groupes : {', '.join(TABLE_GROUPS)}\n"
        f"   Tables : {', '.join((*REQUIRED_TABLES, *OPTIONAL_TABLES))}\n"
        "   Exemple : --only economic_events  (ou --only engine, ou --only users,insights)"
    )


def selection_detail(selection: Dict[str, Any]) -> str:
    """Ce que la sélection vise, groupes **résolus** : le rapport dit la vérité.

    Un groupe affiché tel quel (`engine`) laisserait croire qu'une table nommée
    `engine` a été vérifiée ; les tables réelles sont donc données, et le groupe
    n'est cité qu'en second, comme provenance.
    """
    if not selection["ok"]:
        return selection_problem(selection)
    tables = [*selection["required"], *selection["optional"]]
    groups = [token for token in selection["requested"] if token.lower() in TABLE_GROUPS]
    detail = "tables visées : " + (", ".join(tables) or "aucune")
    if groups:
        detail += f" (via le(s) groupe(s) {', '.join(groups)})"
    return detail


def configuration_checks() -> List[Dict[str, Any]]:
    """URL, clé, bibliothèque `supabase`, construction du client.

    Tout vient de `core.config_runtime` : la vérification de la configuration est
    la même ici et au démarrage de l'application, sinon les deux peuvent diverger
    (et c'est celle du démarrage qui compte).
    """
    cfg = config_runtime.get_env_config()
    checks: List[Dict[str, Any]] = []

    url_issue = config_runtime.supabase_url_issue(cfg.supabase_url)
    checks.append(result("SUPABASE_URL", not url_issue, url_issue or cfg.supabase_url))

    role = config_runtime.supabase_key_role(cfg.supabase_service_key)
    if not cfg.supabase_service_key:
        checks.append(result("SUPABASE_SERVICE_KEY", False, "clé absente"))
    elif role == "anon":
        checks.append(
            result(
                "SUPABASE_SERVICE_KEY",
                False,
                "clé publique (anon / publishable) : la RLS en deny-by-default ne laisse rien passer",
            )
        )
    else:
        checks.append(
            result(
                "SUPABASE_SERVICE_KEY",
                True,
                f"rôle service_role ({'JWT' if role else 'format inconnu — non vérifiable'})",
            )
        )

    try:
        import supabase as supabase_package  # noqa: F401

        version = getattr(supabase_package, "__version__", "?")
        checks.append(result("bibliothèque supabase", True, f"installée (version {version})"))
    except Exception as exc:
        checks.append(
            result(
                "bibliothèque supabase",
                False,
                f"{type(exc).__name__} — `pip install -r requirements.txt`",
            )
        )

    status = client_status()
    checks.append(
        result(
            "client Supabase",
            status["ready"],
            "construit" if status["ready"] else f"non construit : {status['error']}",
        )
    )
    return checks


def table_checks(client: Any, tables: Iterable[str] = REQUIRED_TABLES) -> List[Dict[str, Any]]:
    """Une lecture minimale par table : elle existe et la clé peut la lire."""
    checks: List[Dict[str, Any]] = []
    for table in tables:
        try:
            client.table(table).select("*").limit(1).execute()
        except Exception as exc:
            checks.append(
                result(
                    table,
                    False,
                    f"{type(exc).__name__}: {str(exc)[:160]} — migration non appliquée ?",
                )
            )
            continue
        checks.append(result(table, True, "lisible"))
    return checks


def _client_bindings() -> List[Any]:
    """Les modules qui ont lié le client à leur import, plus son module d'origine.

    `from database.supabase_client import supabase` **fige la valeur** : patcher
    `database.supabase_client.supabase` ne change rien pour les modules qui ont
    écrit cet import (`core/adaptive_learning.py`, `database/preferences.py`,
    `workers/performance_tracker.py`…). L'aller-retour écrirait alors avec le
    client donné et relirait avec un autre — en production les deux sont le
    même objet, donc personne ne le verrait ; en test, on interrogerait la vraie
    base au lieu de la doublure.

    La sélection se fait **par identité** : seules les liaisons qui pointent sur
    l'objet qu'on remplace sont réécrites, donc un module du projet qui aurait un
    attribut `supabase` sans rapport n'est pas touché.
    """
    original = supabase_client.supabase
    bound = [supabase_client]
    for name, module in list(sys.modules.items()):
        if module is None or module is supabase_client:
            continue
        if name.split(".")[0] not in PROJECT_PACKAGES:
            continue
        if getattr(module, "supabase", _MISSING) is original:
            bound.append(module)
    return bound


@contextlib.contextmanager
def client_in_place(client: Any):
    """Rend `client` utilisable par le code applicatif pendant le bloc.

    L'aller-retour doit passer par les **mêmes fonctions** que la production
    (`insert_insight`, `create_pending_signal`, `get_learning_summary`…) :
    réécrire les requêtes ici vérifierait ce script, pas l'application. Ces
    fonctions s'appuient sur le client de leur module — ou sur la copie qu'elles
    en ont faite à leur import —, d'où cette substitution réversible de
    **toutes** les liaisons.
    """
    previous = [(module, module.supabase) for module in _client_bindings()]
    for module, _ in previous:
        module.supabase = client
    try:
        yield client
    finally:
        for module, value in previous:
            module.supabase = value


def _probe_suffix() -> str:
    """Suffixe aléatoire : deux sondes simultanées ne se marchent pas dessus."""
    return uuid.uuid4().hex[:12]


def _probe_user_id() -> str:
    """Identifiant d'utilisateur de sonde, reconnaissable dans la table."""
    return f"probe-{_probe_suffix()}"


def _probe_asset() -> str:
    """L'actif de sonde des tables d'apprentissage, **unique à l'exécution**.

    Trois tables sont écrites sur un actif fictif (`insights`,
    `trade_post_mortems`, `adaptive_model_weights`). Il était constant (`PROBE`) :
    deux vérifications simultanées — deux branches en CI, ou une relance pendant
    qu'une autre tourne — écrivaient donc sous la **même** clé. La première à
    nettoyer emportait la ligne de l'autre, qui, relisant un actif disparu, se
    déclarait en échec alors qu'aucune sonde n'était cassée ; et `total_trades` se
    lisait sur la ligne de la voisine. Le suffixe rend chaque ligne reconnaissable
    par sa propre sonde, et par elle seule.
    """
    return f"PROBE-{_probe_suffix()}"


def _engine_table_checks(
    client: Any, tables: Optional[Iterable[str]] = None
) -> Tuple[List[Dict[str, Any]], List[Tuple[str, str, str]]]:
    """Écrit puis relit sur les quatre tables des migrations 005 et 006.

    `tables` restreint la sonde à une sélection (`--only`) : les tables non
    visées ne sont **ni écrites ni rapportées**. Une sonde de configuration n'a
    pas à toucher une table qu'on n'a pas demandée — c'est même la seule raison
    d'être d'une sélection quand on vise une table précise.

    Rend les vérifications **et** la liste de ce qu'il faudra supprimer. Chaque
    table a son propre verdict : l'échec de l'une ne doit pas cacher les trois
    autres, sinon le rapport ne dit pas lesquelles sont en cause. Les cibles de
    nettoyage sont enregistrées **avant** l'écriture — une écriture à moitié
    réussie, ou une relecture qui échoue, doit être nettoyée quand même.

    Deux familles d'écriture, et la différence est assumée :

    * `economic_events` et `macro_bias_logs` passent par les fonctions de
      l'application (`upsert_economic_events`, `log_macro_decision`) ;
    * `trade_post_mortems` et `adaptive_model_weights` sont écrites directement.
      Leur unique chemin applicatif, `record_trade_settlement_and_learn()`,
      recalcule les poids de l'actif et **réécrit une note de `knowledge_base`**
      à partir des derniers post-mortems : une sonde de configuration n'a pas à
      modifier une ligne qui n'est pas la sienne, et le nettoyage ne pourrait pas
      la restaurer. Elles sont en revanche relues **par l'application**
      (`get_learning_summary`, `get_adaptive_parameters`) : c'est la moitié qui
      compte, une ligne que l'application sait relire est une ligne utilisable.
    """
    checks: List[Dict[str, Any]] = []
    targets: List[Tuple[str, str, str]] = []
    tag = _probe_suffix()
    #: L'actif de sonde, unique à cette exécution (voir `_probe_asset`) : c'est lui
    #: qui empêche deux vérifications simultanées de se lire et de se supprimer
    #: l'une l'autre, et c'est donc lui que le nettoyage vise.
    asset = _probe_asset()
    wanted = None if tables is None else {str(table) for table in tables}

    def chosen(table: str) -> bool:
        """Cette table est-elle visée ? (sans sélection : elles le sont toutes)"""
        return wanted is None or table in wanted

    if chosen("economic_events"):
        # 1. `economic_events` — écrite par le scraper, relue par le moteur macro.
        event_id = f"ff_probe_{tag}"
        targets.append(("economic_events", "id", event_id))
        name = "economic_events (écriture → relecture)"
        try:
            with client_in_place(client):
                upsert_economic_events(
                    [
                        {
                            "id": event_id,
                            "event_id": event_id,
                            "title": "Sonde de configuration",
                            "country": "PROBE",
                            "currency": "PROBE",
                            "event_date": datetime.now(timezone.utc).isoformat(),
                            "impact": "High",
                            "forecast": "1",
                            "previous": "2",
                            "actual": "3",
                            "forecast_num": 1.0,
                            "previous_num": 2.0,
                            "actual_num": 3.0,
                            "raw_data": {"probe": True},
                        }
                    ]
                )
                events = get_economic_events(currency="PROBE", limit=50)
            found = next((row for row in events if str(row.get("id")) == event_id), None)
            checks.append(
                result(
                    name,
                    found is not None and found.get("impact") == "High",
                    f"id={event_id} relu via get_economic_events (currency=PROBE)"
                    if found is not None
                    else f"id={event_id} absent de la relecture",
                )
            )
        except Exception as exc:
            checks.append(result(name, False, f"{type(exc).__name__}: {str(exc)[:160]}"))

    if chosen("macro_bias_logs"):
        # 2. `macro_bias_logs` — journal en ajout seul, que l'application ne relit pas.
        symbol = f"PROBE-{tag}"
        targets.append(("macro_bias_logs", "symbol", symbol))
        name = "macro_bias_logs (écriture → relecture)"
        try:
            with client_in_place(client):
                log_macro_decision(symbol, "PROBE", 0.25, "low", "HOLD", "Sonde de configuration")
            rows = (
                client.table("macro_bias_logs")
                .select("*")
                .eq("symbol", symbol)
                .limit(1)
                .execute()
                .data
                or []
            )
            checks.append(
                result(
                    name,
                    bool(rows),
                    f"symbol={symbol} relu par select (l'application n'a pas de lecture ici)"
                    if rows
                    else f"symbol={symbol} absent : `log_macro_decision` avale ses erreurs, "
                    "c'est la relecture qui tranche",
                )
            )
        except Exception as exc:
            checks.append(result(name, False, f"{type(exc).__name__}: {str(exc)[:160]}"))

    if chosen("trade_post_mortems"):
        # 3. `trade_post_mortems` — le diagnostic d'un trade réglé, relu par le résumé.
        lesson = f"Sonde de configuration ({tag})"
        targets.append(("trade_post_mortems", "asset", asset))
        name = "trade_post_mortems (écriture → relecture)"
        try:
            client.table("trade_post_mortems").insert(
                {
                    "signal_id": f"probe-{tag}",
                    "asset": asset,
                    "direction": "BUY",
                    "outcome": "won",
                    "entry_price": 1.0,
                    "exit_price": 1.01,
                    "pnl": 0.01,
                    "ta_score": 0.5,
                    "sentiment_score": 0.5,
                    "macro_score": 0.5,
                    "confidence": 0.5,
                    "error_type": None,
                    "learned_lesson": lesson,
                }
            ).execute()
            with client_in_place(client):
                summary = get_learning_summary() or {}
            recent = summary.get("recent_post_mortems", [])
            written = [row for row in recent if row.get("learned_lesson") == lesson]
            checks.append(
                result(
                    name,
                    bool(written),
                    f"asset={asset} relu via get_learning_summary "
                    f"({len(recent)} post-mortems récents)"
                    if written
                    else f"asset={asset} absent de get_learning_summary",
                )
            )
        except Exception as exc:
            checks.append(result(name, False, f"{type(exc).__name__}: {str(exc)[:160]}"))

    if chosen("adaptive_model_weights"):
        # 4. `adaptive_model_weights` — relue à chaque signal, via les paramètres de décision.
        targets.append(("adaptive_model_weights", "asset", asset))
        name = "adaptive_model_weights (écriture → relecture)"
        try:
            client.table("adaptive_model_weights").upsert(
                {
                    "asset": asset,
                    "ta_weight": 0.41,
                    "sentiment_weight": 0.29,
                    "macro_weight": 0.30,
                    "sl_atr_multiplier": 1.5,
                    "tp_atr_multiplier": 3.0,
                    "consecutive_losses": 2,
                    "win_rate_pct": 66.6,
                    "total_trades": 7,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            ).execute()
            with client_in_place(client):
                params = get_adaptive_parameters(asset)
            usable = params.get("asset") == asset and params.get("total_trades") == 7
            checks.append(
                result(
                    name,
                    usable,
                    f"asset={asset} relu via get_adaptive_parameters "
                    f"(total_trades={params.get('total_trades')})",
                )
            )
        except Exception as exc:
            checks.append(result(name, False, f"{type(exc).__name__}: {str(exc)[:160]}"))

    return checks, targets


def roundtrip_checks(
    client: Any, tables: Optional[Iterable[str]] = None
) -> List[Dict[str, Any]]:
    """Écrit, relit puis supprime sur les sept tables que l'application utilise.

    Trois d'abord (migration 011), avec leurs dépendances :
    `pending_signals.user_id` référence `users.id`, donc la suppression se fait à
    l'envers de la création. Puis les quatre tables des migrations 005 et 006.
    Un nettoyage qui échoue est **annoncé**, pas tu — c'est la seule chose qui
    distingue une sonde d'un dégât.

    `tables` restreint l'aller-retour à une sélection (`--only`) : seules les
    tables visées sont écrites **et rapportées**. Deux exceptions, et elles sont
    de fond :

    * `users` est écrit même quand seul `pending_signals` est visé — la clé
      étrangère l'exige, et on ne peut pas éprouver une table qu'on refuse
      d'écrire ; sa ligne est nettoyée dans les deux cas, mais elle n'est
      rapportée que si elle est visée ;
    * une sélection qui ne touche **aucune** de ces sept tables le dit, au lieu de
      rendre une section vide qu'on lirait comme un succès.
    """
    wanted = None if tables is None else {str(table) for table in tables}

    def chosen(*names: str) -> bool:
        """Au moins une de ces tables est-elle visée ? (sans sélection : oui)"""
        return wanted is None or any(name in wanted for name in names)

    if wanted is not None and not (wanted & set(ROUNDTRIP_TABLES)):
        return [
            result(
                "aller-retour",
                True,
                "aucune des tables visées n'est éprouvée par l'aller-retour : "
                + ", ".join(sorted(wanted)),
            )
        ]

    checks: List[Dict[str, Any]] = []
    targets: List[Tuple[str, str, str]] = []
    user_id = _probe_user_id()
    #: L'actif de sonde de `insights`, unique à cette exécution : `insights` est une
    #: table de production, et deux vérifications simultanées ne doivent ni se lire
    #: ni se supprimer l'une l'autre (voir `_probe_asset`).
    asset = _probe_asset()
    signal_id: Optional[str] = None

    try:
        # 1. `users` — la ligne écrite par `/start`, avec les mêmes colonnes.
        #: Écrite **aussi** pour `pending_signals` seule : la clé étrangère exige
        #: un `users.id`, et refuser de l'écrire reviendrait à ne plus éprouver la
        #: table visée. La cible de nettoyage est posée **avant** l'écriture, pour
        #: qu'une écriture à moitié réussie soit nettoyée quand même.
        if chosen("users", "pending_signals"):
            targets.append(("users", "id", user_id))
            client.table("users").upsert(
                {
                    "id": user_id,
                    "username": f"probe_{user_id[-6:]}",
                    "first_name": "Sonde",
                    "telegram_chat_id": 0,
                    "paper_mode": True,
                }
            ).execute()
            row = (
                client.table("users")
                .select("*")
                .eq("id", user_id)
                .limit(1)
                .execute()
                .data
                or []
            )
            if chosen("users"):
                checks.append(
                    result(
                        "users (écriture → relecture)",
                        bool(row) and str(row[0].get("id")) == user_id,
                        f"id={user_id}",
                    )
                )

        # 2. `insights` — par la fonction de l'application.
        if chosen("insights"):
            targets.append(("insights", "asset", asset))
            with client_in_place(client):
                insert_insight(
                    {
                        "type": "probe",
                        "asset": asset,
                        "title": "Sonde de configuration",
                        "summary": "Écriture/lecture/ suppression automatique "
                        "(scripts/check_supabase.py).",
                        "source": "scripts/check_supabase.py",
                        "confidence": 0.0,
                        "data": {"probe": True},
                    }
                )
                stored = get_recent_insights(asset=asset, limit=1)
            checks.append(
                result(
                    "insights (écriture → relecture)",
                    bool(stored),
                    f"{len(stored)} ligne(s) relue(s) via get_recent_insights "
                    f"(asset={asset})",
                )
            )

        # 3. `pending_signals` — création, verdict, relecture du statut.
        if chosen("pending_signals"):
            #: La cible de nettoyage est posée **avant** l'écriture, et sur `user_id`
            #: plutôt que sur l'identifiant du signal : celui-ci est attribué par la
            #: base, donc il n'existe qu'au retour de `create_pending_signal` — et un
            #: verdict ou une relecture qui échoue ensuite laisserait la ligne créée
            #: sans que rien ne la nomme. `user_id` est connu d'avance et propre à la
            #: sonde (un identifiant neuf par exécution) : il désigne exactement les
            #: lignes que cette sonde-là a créées.
            targets.append(("pending_signals", "user_id", user_id))
            with client_in_place(client):
                signal_id = create_pending_signal(
                    user_id,
                    {
                        "asset": "PROBE-USD",
                        "direction": "BUY",
                        "entry": 1.0,
                        "stop_loss": 0.99,
                        "take_profit": 1.02,
                        "confidence": 0.0,
                        "source": "check_supabase",
                        "is_demo": True,
                    },
                )
                update_signal_status(
                    signal_id, "executed", {"status": "probe", "method": "probe"}
                )
                rows = (
                    client.table("pending_signals")
                    .select("*")
                    .eq("id", signal_id)
                    .limit(1)
                    .execute()
                    .data
                    or []
                )
            status = (rows[0].get("status") if rows else None)
            checks.append(
                result(
                    "pending_signals (écriture → verdict → relecture)",
                    bool(rows) and status == "executed",
                    f"id={signal_id} statut={status}",
                )
            )
    except Exception as exc:
        checks.append(
            result("aller-retour", False, f"{type(exc).__name__}: {str(exc)[:200]}")
        )
    finally:
        # Les tables des migrations 005 et 006 sont vérifiées **même si** l'étape
        # précédente a échoué : un échec sur `users` ne dit rien d'`economic_events`,
        # et un rapport qui s'arrête à la première table ne permet pas de savoir
        # lesquelles sont en cause.
        engine_checks, engine_targets = _engine_table_checks(client, tables)
        checks.extend(engine_checks)
        targets.extend(engine_targets)
        leftovers = _cleanup(client, targets)
        checks.append(
            result(
                CLEANUP_CHECK,
                not leftovers,
                "lignes de sonde supprimées"
                if not leftovers
                else f"à supprimer à la main : {leftovers}",
                leftovers=leftovers,
            )
        )
    return checks


def _delete_row(client: Any, table: str, column: str, value: str) -> Optional[str]:
    """Supprime une ligne de sonde, en réessayant avant de la déclarer restée.

    Rend `None` si la ligne est partie, sinon le motif de l'échec **définitif**
    (`CLEANUP_RETRY_DELAYS` plus une tentatives, séparées par ces délais
    croissants). La première est immédiate : l'immense majorité des suppressions
    passent du premier coup, et faire attendre tout le monde pour un cas rare
    serait payer le remède à chaque fois. Les suivantes ne coûtent rien quand
    tout va bien, et laissent au transitoire le temps de passer quand ça va mal.

    Ce n'est pas un « peut-être » : au bout des reprises, l'échec est un fait, et
    c'est le motif de la **dernière** tentative qui est rapporté — c'est lui qui
    décrit l'état dans lequel la sonde a abandonné.
    """
    failure = "suppression jamais tentée"
    for delay in (None, *CLEANUP_RETRY_DELAYS):
        if delay is not None:
            time.sleep(delay)
        try:
            client.table(table).delete().eq(column, value).execute()
        except Exception as exc:
            failure = f"{type(exc).__name__}: {str(exc)[:80]}"
            continue
        return None
    return failure


def _cleanup(client: Any, targets: Iterable[Tuple[str, str, str]]) -> List[str]:
    """Supprime ce qui a été créé ; rend ce qui n'a pas pu l'être.

    L'ordre est **l'inverse** de la création : `pending_signals.user_id`
    référence `users.id`, et supprimer l'utilisateur d'abord ferait échouer la
    suppression des signaux — ou emporterait des lignes qui ne sont pas celles de
    la sonde. Ce qui n'a pas pu être supprimé est nommé (table, colonne, valeur),
    de quoi le retirer à la main sans chercher.

    Chaque suppression est **retentée** (`_delete_row`) : un reste en base ne se
    déclare qu'après la dernière tentative, jamais sur un transitoire.
    """
    leftovers: List[str] = []

    for table, column, value in reversed(list(targets)):
        failure = _delete_row(client, table, column, value)
        if failure:
            leftovers.append(f"{table}.{column}={value} ({failure})")
    return leftovers


def reported_leftovers(sections: Iterable[Dict[str, Any]]) -> List[str]:
    """Les lignes restées en base, telles que le rapport les porte.

    Elles sont rassemblées **depuis le rapport** plutôt que rendues à part par
    `roundtrip_checks()` : le nettoyage les publie là où il les constate
    (`result(..., leftovers=…)`), et aucun appelant n'a à deviner *quelle*
    vérification les porte. Un rapport arrêté avant l'aller-retour n'en a aucune.
    """
    return [
        row for section in sections for check in section["checks"] for row in check.get("leftovers", [])
    ]


def exit_code(report: Dict[str, Any]) -> int:
    """Le code de sortie du rapport — `3` **prime** sur `1`.

    Un nettoyage incomplet est aussi un échec (`ok: false`), mais c'est la seule
    chose qui laisse une trace derrière soi : quand les deux sont vrais, c'est elle
    qui décide, sinon un reste en base se lirait comme un échec ordinaire, qu'on
    rejoue en espérant qu'il passe.
    """
    if report.get("leftovers"):
        return EXIT_LEFTOVER
    return EXIT_OK if report["ok"] else EXIT_FAILED


def format_leftover_journal(leftovers: Iterable[str]) -> str:
    """Le bloc de journal d'un nettoyage incomplet, une ligne par reste en base.

    Écrit sur `stderr` : `--json` occupe `stdout`, et un consommateur machine ne
    doit pas avoir à filtrer un avertissement pour lire sa sortie. Chaque ligne
    nomme ce qu'il faut supprimer (table, colonne, valeur).
    """
    rows = list(leftovers)
    lines = [
        f"{FAIL} {JOURNAL_TAG} nettoyage INCOMPLET — {len(rows)} ligne(s) de sonde "
        "restée(s) en base.",
    ]
    lines.extend(f"   • {row}" for row in rows)
    lines.append(
        "   À supprimer à la main dans la table nommée : une ligne de sonde laissée "
        "derrière fausse les lectures qui la rencontrent (actif `PROBE-…`, "
        "identifiant `probe-…`)."
    )
    return "\n".join(lines)


def format_leftover_alert(leftovers: Iterable[str]) -> str:
    """Le message d'alerte : ce qui reste, et quoi en faire."""
    rows = list(leftovers)
    lines = [
        f"{FAIL} Sonde Supabase — nettoyage incomplet : {len(rows)} ligne(s) "
        "restée(s) dans la base.",
        "",
    ]
    lines.extend(f"• {row}" for row in rows)
    lines.append("")
    lines.append(
        "À supprimer à la main (`table.colonne=valeur` ci-dessus). Sonde lancée par "
        "`scripts/check_supabase.py --roundtrip`."
    )
    return "\n".join(lines)


def _deliver(notify: Any, chat_id: Any, text: str) -> None:
    """Envoie le message, que `notify` soit une coroutine ou une fonction simple.

    `send_admin_message` est **asynchrone** (le bot Telegram) ; les tests injectent
    une fonction ordinaire. Trancher ici évite les deux pièges symétriques :
    `await` sur un dictionnaire, et `asyncio.run` sur un résultat déjà résolu.
    """
    outcome = notify(chat_id, text)
    if inspect.isawaitable(outcome):
        asyncio.run(outcome)


def alert_leftovers(
    leftovers: Iterable[str],
    *,
    notify: Any = None,
    chat_id: Optional[str] = None,
    config: Any = None,
) -> Dict[str, Any]:
    """Prévient l'opérateur qu'une ligne de sonde est restée **en base**.

    Même voie que la veille média (`workers/media_reconcile.py`) : le message part
    au chat d'exploitation, et l'absence de chat comme un envoi refusé sont **dits**
    — une alerte qui n'est pas partie doit se distinguer d'une alerte partie, sinon
    le silence se lit comme une bonne nouvelle.

    Ne lève pas : le nettoyage incomplet est **déjà** l'incident, il ne doit pas
    devenir un plantage qui cache le rapport. Synchrone à dessein : `main()`
    l'appelle directement, la route d'administration l'appelle **dans un thread**
    (`asyncio.run` ne tolère pas une boucle déjà en cours).
    """
    rows = list(leftovers)
    if not rows:
        return {"sent": False, "reason": "nothing_left"}
    config = config or config_runtime.get_env_config()
    if chat_id is None:
        chat_id = config.telegram_admin_chat_id
    if not chat_id:
        print(
            f"   {JOURNAL_TAG} aucune alerte envoyée : TELEGRAM_ADMIN_CHAT_ID "
            "n'est pas renseigné",
            file=sys.stderr,
        )
        return {"sent": False, "reason": "no_admin_chat"}
    if notify is None:
        # Import **local** : la sonde doit tourner sans la pile Telegram, comme le
        # reste de la surface protégée (`api/admin_router`).
        from notifications.notify import send_admin_message

        notify = send_admin_message
    try:
        _deliver(notify, chat_id, format_leftover_alert(rows))
    except Exception as exc:
        print(
            f"   {JOURNAL_TAG} alerte non envoyée : {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return {"sent": False, "reason": "send_failed"}
    return {"sent": True, "reason": None}


def render(checks: List[Dict[str, Any]], *, title: str, as_json: bool) -> str:
    if as_json:
        return json.dumps(
            {"title": title, "checks": checks, "ok": all(check["ok"] for check in checks)},
            ensure_ascii=False,
            indent=2,
        )
    lines = [title]
    for check in checks:
        mark = OK if check["ok"] else FAIL
        lines.append(f"  {mark} {check['name']} — {check['detail']}")
    return "\n".join(lines)


def run(
    *,
    roundtrip: bool,
    as_json: bool,
    client: Any = None,
    only: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Enchaîne les trois niveaux de vérification et rend le rapport complet.

    `client` est injectable : c'est ce qui rend l'outil testable sans base.

    `only` restreint les trois niveaux de **table** à une sélection (voir
    `resolve_only`). Un nom inconnu n'est pas un repli sur « tout » : c'est un
    échec, et rien n'est interrogé. Avec `--roundtrip`, un repli silencieux
    ferait écrire sur les sept tables précisément quand on a demandé une seule —
    c'est la seule façon dont cette option pourrait faire des dégâts.
    """
    sections: List[Dict[str, Any]] = []
    config_checks = configuration_checks()
    sections.append({"title": "1. Configuration", "checks": config_checks})

    selection = resolve_only(only)
    selection_checks: List[Dict[str, Any]] = []
    tables_title = "2. Tables"
    if selection["requested"] or not selection["ok"]:
        #: La sélection est une **vérification** comme une autre : un nom inconnu
        #: se lit à l'endroit où le reste se lit, pas dans un message qui aurait
        #: l'air d'un plantage — et le titre dit ce qui a été visé.
        selection_checks.append(result("--only", selection["ok"], selection_detail(selection)))
        tables_title = f"2. Tables — sélection : {', '.join(selection['requested'])}"
    if not selection["ok"]:
        sections.append({"title": tables_title, "checks": selection_checks})
        return {"sections": sections, "ok": False, "leftovers": []}

    status = client_status()
    if not status["ready"] and client is None:
        # Sans client, vérifier les tables dirait seulement « migration non
        # appliquée » : on s'arrête ici, en nommant la vraie raison.
        sections.append(
            {
                "title": "2. Base",
                "checks": [
                    *selection_checks,
                    result(
                        "connexion",
                        False,
                        f"client Supabase non construit : {status.get('error') or 'configuration incomplète'}"
                        " — corrige la configuration ci-dessus",
                    ),
                ],
            }
        )
        return {"sections": sections, "ok": False, "leftovers": []}

    if client is None:
        client = supabase_client.supabase

    sections.append(
        {
            "title": tables_title,
            "checks": [*selection_checks, *table_checks(client, selection["required"])],
        }
    )

    #: Une sélection sans table facultative (`--only economic_events` par exemple)
    #: n'affiche pas une section vide : un titre suivi de rien se lirait comme un
    #: succès silencieux.
    optional = table_checks(client, selection["optional"])
    if optional:
        sections.append(
            {
                "title": "3. Tables facultatives",
                "checks": [
                    result(
                        check["name"] if check["ok"] else f"{check['name']} (facultative)",
                        True,
                        check["detail"]
                        if check["ok"]
                        else f"{check['detail']} — sans elle, l'environnement reprend",
                    )
                    for check in optional
                ],
            }
        )

    if roundtrip:
        sections.append(
            {
                "title": "4. Aller-retour écriture/lecture",
                "checks": roundtrip_checks(client, selection["required"]),
            }
        )

    ok = all(check["ok"] for section in sections for check in section["checks"])
    #: Toujours présent, même vide : un appelant qui lit `report["leftovers"]` n'a
    #: pas à distinguer « aucun reste » de « la clé n'existe pas ».
    return {"sections": sections, "ok": ok, "leftovers": reported_leftovers(sections)}


def main(argv: Optional[List[str]] = None) -> int:
    console.make_streams_utf8()
    parser = argparse.ArgumentParser(
        description="Vérifie la configuration Supabase et (option) la base réelle.",
    )
    parser.add_argument(
        "--roundtrip",
        action="store_true",
        help="écrit, relit puis supprime sur les 7 tables de l'application (nettoyage inclus)",
    )
    parser.add_argument(
        "--only",
        action="append",
        metavar="TABLE|GROUPE",
        help=(
            "ne vérifier que ces tables (plusieurs noms séparés par des virgules, "
            "option répétable) ; groupes : " + ", ".join(TABLE_GROUPS)
        ),
    )
    parser.add_argument("--json", action="store_true", help="sortie JSON")
    args = parser.parse_args(argv)

    #: Validé **avant** de charger quoi que ce soit : une faute de frappe ne doit
    #: pas mener à lire `.env`, et encore moins à écrire (avec `--roundtrip`).
    selection = resolve_only(args.only)
    if not selection["ok"]:
        print(f"{FAIL} {selection_problem(selection)}", file=sys.stderr)
        return EXIT_USAGE

    # Chargé **ici** et non dans `run()` : un appel programmatique à `run()`
    # (les tests, un script qui l'importe) ne doit pas se mettre à lire le `.env`
    # de la machine — surtout avec l'aller-retour, qui écrit pour de vrai.
    load_project_env()

    report = run(roundtrip=args.roundtrip, as_json=args.json, only=args.only)

    #: Un nettoyage incomplet se dit **d'abord** là où un journal le garde
    #: (`stderr`), puis à l'opérateur — même si personne ne lit la sortie du
    #: processus. `stdout` reste intact, `--json` compris.
    leftovers = report.get("leftovers") or []
    if leftovers:
        print(format_leftover_journal(leftovers), file=sys.stderr)
        alert_leftovers(leftovers)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return exit_code(report)

    print(f"Vérification Supabase — {datetime.now(timezone.utc).isoformat()}")
    for section in report["sections"]:
        print(render(section["checks"], title=section["title"], as_json=False))
    print()
    if leftovers:
        print(
            f"{FAIL} Nettoyage incomplet : {len(leftovers)} ligne(s) de sonde "
            "restée(s) en base — voir le journal."
        )
    elif report["ok"]:
        print(f"{OK} Configuration et base opérationnelles.")
    else:
        print(f"{FAIL} Au moins un point à corriger (voir ci-dessus).")
    return exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
