"""Instantané du schéma PostgreSQL, pris à `pg_dump --schema-only`.

Les contrats de migration relisent le SQL des fichiers et les sources Python :
ils disent ce que le **texte** veut dire. Ils n'attrapent donc pas le schéma
**retenu** par Postgres — le corps compilé d'une fonction, la définition exacte
d'un index (`using hnsw (embedding vector_cosine_ops)`, un `desc`, un `where`),
le type et le défaut que `bigserial` produit vraiment, ce qu'un bloc `do $$` a
réellement créé. C'est ce que cet instantané ajoute : il est versionné
(`tests/goldens/postgres_schema.json`), la suite le compare, et il se régénère
explicitement, comme tous les goldens.

Comment il est pris, et pourquoi ainsi :

* la base est **créée pour l'occasion** (`ai_trade_snapshot_<aléa>`), les
  migrations y sont appliquées avec le **stub de plateforme** (schémas
  `extensions` et `storage`, rôles `anon`/`authenticated`/`service_role`, table
  `storage.buckets` — sans eux la migration 008 échoue), l'instantané est pris,
  puis la base est **supprimée**. Rien n'est lu d'une base habitée : un instantané
  doit décrire ce que les migrations **produisent**, pas l'état d'une machine ;
* seul le schéma `public` est dumpé — c'est la décision déjà prise par
  `scripts/check_schema_drift.py` : `extensions` et `storage` appartiennent à la
  plateforme, et un dump qui les embarquerait divergerait d'un projet Supabase à
  l'autre ;
* l'URL vient de `MIGRATION_DATABASE_URL`, **la même variable que l'applicateur et
  aucune autre** : la cible est nommée avant d'ouvrir quoi que ce soit. Ce module
  ne modifie aucun objet existant — il crée une base, y écrit, la supprime ;
* ce que `pg_dump` écrit selon **sa** version est retiré (bannière, jetons
  `\\restrict`/`\\unrestrict` tirés au hasard) et remplacé par les versions
  majeures, enregistrées **à part** : un changement d'outil se lit alors comme un
  fait de la version, pas comme deux cents lignes de diff.

Un ingrédient manquant (base, `pg_dump`, `psycopg`) lève `Unavailable` : le gate
des goldens le présente comme un contrôle **non exécuté**, jamais comme un vert.

Ce module est importé (`scripts/goldens.py`, `tests/test_schema_snapshot.py`),
pas exécuté : la régénération passe par
`python scripts/goldens.py --update postgres_schema`.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import re
import secrets
import shutil
import subprocess
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

from scripts import apply_migrations

#: La variable de la cible — la même que l'applicateur, et aucune autre.
ENV_VAR = apply_migrations.ENV_VAR

#: Le binaire `pg_dump`, quand il n'est pas dans le `PATH`. Une distribution
#: PostgreSQL peut vivre à côté du dépôt (c'est le cas des vérifications locales)
#: sans y être installée : la chercher toute seule ferait dépendre le dépôt d'une
#: machine, donc c'est à l'appelant de la nommer.
PG_DUMP_ENV = "PG_DUMP"

#: Préfixe des bases jetables. Reconnaissable dans `pg_database` : une base
#: laissée par un arrêt brutal se retrouve à l'œil, et son nom dit d'où elle vient.
SCRATCH_PREFIX = "ai_trade_snapshot"

#: Les options du dump, et leur raison : `--schema-only` (aucune donnée),
#: `--no-owner` (le propriétaire dépend de la machine et du projet Supabase), et
#: `--schema=public` (le seul schéma que les migrations remplissent).
DUMP_OPTIONS = ("--schema-only", "--no-owner", "--schema=public")

#: Ce que `pg_dump` écrit selon **sa** version, donc à retirer : les deux lignes
#: de bannière, et les jetons `\restrict`/`\unrestrict` (tirés au hasard, apparus
#: avec PostgreSQL 17). Les laisser ferait diverger le golden à chaque
#: régénération — exactement ce qu'un golden ne doit jamais faire.
VOLATILE_LINES = (
    re.compile(r"^-- Dumped from database version\b"),
    re.compile(r"^-- Dumped by pg_dump version\b"),
    re.compile(r"^\\(?:un)?restrict\b"),
)

#: Les objets comptés dans l'instantané. Le compte ne remplace pas le dump : il
#: rend un diff **lisible** (« indexes : 18 → 19 ») et interdit un instantané
#: vide, qui passerait au vert sans rien verrouiller.
OBJECT_PATTERNS: Tuple[Tuple[str, re.Pattern], ...] = (
    ("tables", re.compile(r"^CREATE TABLE ")),
    ("indexes", re.compile(r"^CREATE (?:UNIQUE )?INDEX ")),
    ("functions", re.compile(r"^CREATE (?:OR REPLACE )?FUNCTION ")),
    ("triggers", re.compile(r"^CREATE TRIGGER ")),
    ("constraints", re.compile(r"^\s*ADD CONSTRAINT ")),
    ("sequences", re.compile(r"^CREATE SEQUENCE ")),
    ("policies", re.compile(r"^CREATE POLICY ")),
)

VERSION = re.compile(r"(\d+(?:\.\d+)?)")


class Unavailable(Exception):
    """Un ingrédient manque : la base, `pg_dump` ou `psycopg`.

    Ce n'est pas une dérive : c'est un contrôle qui n'a pas pu tourner. Le gate
    des goldens le dit, il ne le passe pas silencieusement au vert.
    """


def require_psycopg() -> None:
    """Le pilote, ou `Unavailable` — jamais une erreur d'import brute."""
    if importlib.util.find_spec("psycopg") is None:
        raise Unavailable("psycopg absent : `pip install -r requirements.txt`")


def target_url(explicit: Optional[str] = None) -> str:
    """L'URL de la base **support**, où la base jetable sera créée.

    Une variable vide est une absence : ce module ne devine pas de serveur.
    """
    url = (explicit or os.environ.get(ENV_VAR) or "").strip()
    if not url:
        raise Unavailable(
            f"aucune base : définis {ENV_VAR} "
            "(voir la section « Migrations » du README)"
        )
    return url


def pg_dump_path(explicit: Optional[str] = None) -> str:
    """Le binaire `pg_dump` : l'option, la variable, puis le `PATH`.

    Un chemin nommé qui n'existe pas est une erreur **dite** plutôt qu'un repli
    sur le `PATH` : sinon on croirait avoir dumpé avec l'outil voulu.

    Le chemin est rendu **absolu**, y compris quand il était relatif. Ce n'est pas
    une coquetterie : sous Windows, `subprocess` refuse un exécutable relatif
    (`FileNotFoundError` alors que le fichier existe), et ailleurs un chemin
    relatif dépendrait du dossier courant de l'appelant — deux façons de livrer un
    instantané qui n'a pas été pris avec l'outil annoncé.
    """
    named = (explicit or os.environ.get(PG_DUMP_ENV) or "").strip()
    if named:
        candidate = pathlib.Path(named)
        if not candidate.exists():
            raise Unavailable(f"{PG_DUMP_ENV} pointe sur {named}, qui n'existe pas")
        return str(candidate.resolve())
    found = shutil.which("pg_dump")
    if not found:
        raise Unavailable(
            "pg_dump introuvable : installe les outils clients PostgreSQL, "
            f"ou pose {PG_DUMP_ENV}"
        )
    return found


def major_version(text: str) -> str:
    """La version **majeure** contenue dans une sortie d'outil (`16.15` → `16`).

    Le correctif de version n'est pas une information de schéma : la retenir
    ferait diverger le golden entre un poste et la CI, pour une raison qui ne dit
    rien du schéma.
    """
    match = VERSION.search(text or "")
    return match.group(1).split(".")[0] if match else "inconnue"


def scratch_name() -> str:
    """Le nom d'une base jetable — unique, donc deux instantanés ne se heurtent pas."""
    return f"{SCRATCH_PREFIX}_{secrets.token_hex(6)}"


def scratch_url(url: str, name: str) -> str:
    """L'URL de la base jetable : même serveur, même requête, autre nom.

    `urllib.parse` plutôt qu'un `rsplit` : un mot de passe peut contenir un `/` et
    une URL peut porter `?sslmode=require`. Recoller les morceaux à la main
    donnerait, un jour, une base jetable atteinte avec la mauvaise politique de
    connexion — ou pas atteinte du tout.
    """
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path="/" + name))


def normalize_dump(text: str) -> List[str]:
    """Le dump prêt à être versionné : stable d'une machine et d'une version à l'autre.

    Quatre retouches, et pas une de plus : les fins de ligne (sans quoi le même
    dump diverge entre Windows et Linux), les espaces de fin de ligne, les lignes
    de version de `pg_dump` (`VOLATILE_LINES`), et les lignes vides de fin.

    Tout le reste est conservé **tel quel**, y compris les commentaires
    `-- Name: …; Type: …`, qui disent où chaque objet commence.
    """
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    kept = [
        line for line in lines if not any(pattern.match(line) for pattern in VOLATILE_LINES)
    ]
    while kept and not kept[-1]:
        kept.pop()
    return kept


def count_objects(lines: Iterable[str]) -> Dict[str, int]:
    """Les objets créés, comptés depuis le dump (chaque famille toujours présente)."""
    texts = list(lines)
    return {
        label: sum(1 for line in texts if pattern.match(line))
        for label, pattern in OBJECT_PATTERNS
    }


def dump_schema(url: str, pg_dump: str) -> str:
    """Lance `pg_dump` sur `url` et rend sa sortie — ou lève, avec sa raison.

    Un `pg_dump` qui échoue n'est **pas** un ingrédient manquant : la base
    répondait, l'outil a refusé. C'est une dérive, et elle doit être bruyante.
    """
    proc = subprocess.run(
        [pg_dump, *DUMP_OPTIONS, url],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = " / ".join(line.strip() for line in detail[-2:]) or "aucune sortie"
        raise RuntimeError(
            f"pg_dump a refusé de dumper {apply_migrations.redacted(url)} : {tail}"
        )
    if not (proc.stdout or "").strip():
        raise RuntimeError(
            f"pg_dump n'a rien écrit pour {apply_migrations.redacted(url)} : "
            "un instantané vide ne verrouille rien"
        )
    return proc.stdout


def tool_version(pg_dump: str) -> str:
    """La version majeure du `pg_dump` qui a produit l'instantané."""
    proc = subprocess.run(
        [pg_dump, "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return major_version(proc.stdout or proc.stderr or "")


def _scratch_database(url: str, name: str) -> None:
    """Crée la base jetable — échec **nommé**, avec ce qu'il faut pour comprendre.

    `create database` ne tient pas dans une transaction : la connexion est en
    autocommit. Le message nomme le cas le plus probable quand ça échoue : une
    cible hébergée, où la création de base n'est pas permise (Supabase).
    """
    try:
        with apply_migrations.connect(url) as connection:
            connection.autocommit = True
            connection.execute(f'create database "{name}"')
    except Exception as exc:
        raise RuntimeError(
            f"création de la base jetable impossible sur "
            f"{apply_migrations.redacted(url)} ({type(exc).__name__}: {exc}) — "
            "un instantané se prend sur un serveur qui accepte `create database` : "
            "un PostgreSQL local, ou le service PostgreSQL de la CI, pas une base "
            "hébergée qui ne le permet pas"
        ) from exc


def _drop_scratch_database(url: str, name: str) -> None:
    """Supprime la base jetable. Un reste est **nommé**, jamais tu."""
    with apply_migrations.connect(url) as connection:
        connection.autocommit = True
        connection.execute(f'drop database if exists "{name}"')


def _scratch_server_version(url: str) -> str:
    """La version majeure du serveur qui a servi à l'instantané."""
    with apply_migrations.connect(url) as connection:
        row = connection.execute("select current_setting('server_version')").fetchone()
    return major_version(str(row[0]) if row else "")


def snapshot_values(
    *, url: Optional[str] = None, pg_dump: Optional[str] = None
) -> Dict[str, Any]:
    """L'instantané complet : le dump normalisé, sa provenance, ses comptes.

    Tout est injectable (`url`, `pg_dump`) : ce qui décide du contenu — la
    normalisation, les comptes, le choix des options — se vérifie donc sans
    serveur, et seule la prise d'instantané a besoin d'une base.
    """
    require_psycopg()
    base = target_url(url)
    tool = pg_dump_path(pg_dump)
    name = scratch_name()

    _scratch_database(base, name)
    try:
        scratch = scratch_url(base, name)
        with apply_migrations.connect(scratch) as connection:
            for _ in apply_migrations.apply_all(connection, platform_stub=True):
                pass
        server = _scratch_server_version(scratch)
        text = dump_schema(scratch, tool)
    finally:
        # La base jetable ne survit pas à l'instantané, y compris quand les
        # migrations échouent : une base laissée derrière polluerait la suivante.
        _drop_scratch_database(base, name)

    lines = normalize_dump(text)
    if not lines:
        raise RuntimeError("instantané vide après normalisation : rien à comparer")
    return {
        "pg_dump": tool_version(tool),
        "server": server,
        "counts": count_objects(lines),
        "dump": lines,
    }
