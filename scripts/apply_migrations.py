"""Applique les migrations du dépôt à une instance PostgreSQL.

Pourquoi ce script existe : les migrations de `database/migrations/` étaient
seulement **relues** par les tests (colonnes déclarées vs colonnes utilisées par
le code). Aucune ne les exécutait, donc rien ne prouvait qu'elles s'appliquent :
un `do $$` non refermé, un `alter table` visant une colonne absente, une
contrainte refusée par un type, une parenthèse oubliée — tout cela ne se voit
qu'au moment où Postgres les lit pour de vrai. C'est ce que fait ce script, et
c'est ce que fait le job CI `migrations-postgres`, sur une image qui porte
pgvector.

```bash
# 1. une instance jetable (l'image fournit l'extension `vector`)
docker run --rm -d --name ai-trade-pg -p 5432:5432 \\
    -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=ai_trade_test pgvector/pgvector:pg16

# 2. les migrations, dans l'ordre
MIGRATION_DATABASE_URL="postgresql://postgres:postgres@localhost:5432/ai_trade_test" \\
    python scripts/apply_migrations.py --platform-stub
```

`--platform-stub` n'est pas une commodité de test : la migration 008 écrit dans
`storage.buckets` et révoque des privilèges pour les rôles `anon` /
`authenticated`, qui n'existent **que** parce que Supabase les crée. Sur un
Postgres nu, ces deux lignes échouent — et c'est bien le script qui doit dire
pourquoi, pas l'utilisateur qui doit deviner. Le stub crée donc le minimum que
la plateforme fournit d'habitude (schémas `extensions` et `storage`, rôles,
table `storage.buckets`) et rien de plus : les migrations restent seules à créer
leurs tables, leurs index, leurs triggers et leur RLS.

Chaque fichier est appliqué dans **sa** transaction : une erreur laisse la base
dans l'état du dernier fichier appliqué, jamais à moitié dans un fichier. Les
migrations sont idempotentes (`if not exists`, `drop … if exists`), donc
relancer ce script est l'opération normale, pas une imprudence.

Codes de sortie : `0` appliqué, `1` échec (connexion ou fichier), `2` usage.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, List, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = PROJECT_ROOT / "database" / "migrations"

if str(PROJECT_ROOT) not in sys.path:  # exécuté comme script : `core` doit être joignable
    sys.path.insert(0, str(PROJECT_ROOT))

from core import console  # noqa: E402  (après l'ajustement de `sys.path`)

#: Variable d'environnement lue quand `--database-url` n'est pas fourni.
#: Volontairement **distincte** de tout ce que la plateforme Supabase utilise :
#: ce script modifie un schéma, il ne doit pas pouvoir partir sur la base d'un
#: autre outil par simple héritage de l'environnement.
ENV_VAR = "MIGRATION_DATABASE_URL"

#: Délai de connexion, en secondes. Sans lui, libpq attend **indéfiniment** une
#: machine qui ne répond pas : sur un port fermé ou un conteneur non démarré,
#: l'outil resterait suspendu au lieu de dire qu'il n'a pas pu se connecter.
CONNECT_TIMEOUT_SECONDS = 10

#: Nom porté par la connexion dans `pg_stat_activity`. Ce script modifie un
#: schéma : sur une base où plusieurs outils tournent, savoir **qui** tient la
#: connexion est ce qui distingue une migration en cours d'une requête de
#: lecture. `scripts/check_schema_drift.py` lit la même variable d'environnement
#: et se nomme autrement, pour la même raison.
APPLICATION_NAME = "ai-trade-migrations"

#: Numéro à trois chiffres, nom en minuscules : l'invariant de nommage des
#: migrations, vérifié aussi par `tests/test_media_migration.py`. Un fichier qui
#: ne le respecte pas n'a pas d'ordre d'application défini — on le refuse plutôt
#: que de l'appliquer dans un ordre alphabétique arbitraire.
MIGRATION_NAME = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")

#: Ce que la plateforme Supabase fournit et qu'un Postgres nu n'a pas.
#:
#:   * le schéma `extensions` — c'est là que Supabase installe ses extensions, et
#:     la migration 008 y crée explicitement `vector` ;
#:   * les rôles `anon`, `authenticated`, `service_role` — sans eux, chaque
#:     `revoke … from anon, authenticated` des migrations 007, 008, 011, 005 et
#:     006 échoue (« role does not exist ») ;
#:   * `storage.buckets` — la table du Storage Supabase, où la migration 008
#:     déclare le bucket privé `telegram-media`.
PLATFORM_STUB = """\
create schema if not exists extensions;
create schema if not exists storage;

create table if not exists storage.buckets (
    id text primary key,
    name text not null,
    public boolean not null default false
);

do $$
declare
    role_name text;
begin
    foreach role_name in array array['anon', 'authenticated', 'service_role']
    loop
        if not exists (select 1 from pg_roles where rolname = role_name) then
            execute format('create role %I nologin noinherit', role_name);
        end if;
    end loop;
end $$;
"""


@dataclass(frozen=True)
class Applied:
    """Un fichier appliqué, avec sa durée — de quoi voir où le temps passe."""

    file: str
    milliseconds: int


class MigrationFailed(RuntimeError):
    """Un fichier a échoué : nommé, avec la ligne quand Postgres la connaît."""

    def __init__(self, path: Path, line: Optional[int], message: str) -> None:
        where = f"{path.name}:{line}" if line else path.name
        super().__init__(f"{where} — {message}")
        self.path = path
        self.line = line
        self.message = message


def migration_files(directory: Path = MIGRATIONS_DIR) -> List[Path]:
    """Les fichiers de migration, **dans l'ordre des numéros**.

    Un tri par nom suffirait ici (numéros à trois chiffres), mais on lit les
    numéros pour de bon : c'est le seul endroit qui peut dire qu'une migration
    manque ou qu'un numéro est pris deux fois, et le silence sur ce point
    donnerait un ordre d'application arbitraire.
    """
    found: List[Path] = []
    for path in sorted(directory.glob("*.sql")):
        match = MIGRATION_NAME.match(path.name)
        if not match:
            raise RuntimeError(
                f"nom de migration inattendu : {path.name} "
                "(attendu : 3 chiffres, puis un nom en minuscules)"
            )
        found.append(path)
    if not found:
        raise RuntimeError(f"aucune migration trouvée dans {directory}")
    numbers = [int(path.name[:3]) for path in found]
    duplicated = sorted({number for number in numbers if numbers.count(number) > 1})
    if duplicated:
        numbers_shown = ", ".join(f"{number:03d}" for number in duplicated)
        raise RuntimeError(
            f"numéros de migration dupliqués : {numbers_shown} "
            f"({', '.join(path.name for path in found)})"
        )
    return found


def database_url(explicit: Optional[str] = None) -> str:
    """L'URL cible : l'option d'abord, puis `MIGRATION_DATABASE_URL`."""
    url = (explicit or os.environ.get(ENV_VAR) or "").strip()
    if not url:
        raise RuntimeError(
            f"aucune base cible : passe --database-url ou définis {ENV_VAR}"
        )
    return url


def redacted(url: str) -> str:
    """L'URL sans son mot de passe : elle est affichée avant d'écrire.

    Un mot de passe qui finit dans un journal de CI est un mot de passe à
    révoquer ; et l'URL est justement affichée pour qu'on voie **où** on écrit.
    """
    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", url)


def connect(
    url: str, timeout: int = CONNECT_TIMEOUT_SECONDS, application_name: str = APPLICATION_NAME
) -> Any:
    """Ouvre la connexion, en nommant la dépendance manquante si elle l'est.

    `application_name` : la connexion se nomme dans `pg_stat_activity`, donc on
    sait qui interroge la base quand plusieurs outils tournent — et une lecture
    ne se fait pas passer pour une écriture. `connect_timeout` évite l'attente
    infinie sur un hôte injoignable — le cas le plus fréquent quand on s'est
    trompé de port ou que le conteneur n'est pas démarré.
    """
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - dépend de l'environnement
        raise RuntimeError(
            "psycopg est absent : `pip install -r requirements.txt`"
        ) from exc
    return psycopg.connect(
        url, connect_timeout=timeout, application_name=application_name
    )


def error_line(error: Exception, sql: str) -> Optional[int]:
    """La ligne du fichier que Postgres a refusée, quand il la donne.

    Sans elle, un échec dit « syntax error at or near » sans dire où, dans un
    fichier de deux cents lignes de SQL et de commentaires.
    """
    position = getattr(getattr(error, "diag", None), "statement_position", None)
    if not position:
        return None
    try:
        offset = int(position)
    except (TypeError, ValueError):
        return None
    return sql.count("\n", 0, offset) + 1


def apply_all(
    connection: Any,
    files: Optional[Sequence[Path]] = None,
    *,
    platform_stub: bool = False,
) -> Iterator[Applied]:
    """Applique le stub (option) puis chaque migration, dans l'ordre.

    Générateur : l'appelant voit chaque fichier **au fur et à mesure**, ce qui
    compte quand un des fichiers suivants échoue — savoir où on s'est arrêté est
    la première question. Chaque fichier est une transaction : un échec n'en
    laisse aucun à moitié appliqué.
    """
    if platform_stub:
        started = time.monotonic()
        try:
            with connection.transaction():
                connection.execute(PLATFORM_STUB)
        except Exception as exc:
            raise MigrationFailed(Path("platform-stub"), error_line(exc, PLATFORM_STUB), str(exc).strip()) from exc
        yield Applied("platform-stub", int((time.monotonic() - started) * 1000))

    for path in files if files is not None else migration_files():
        sql = path.read_text(encoding="utf-8")
        started = time.monotonic()
        try:
            with connection.transaction():
                connection.execute(sql)
        except Exception as exc:
            raise MigrationFailed(path, error_line(exc, sql), str(exc).strip()) from exc
        yield Applied(path.name, int((time.monotonic() - started) * 1000))


def main(argv: Optional[List[str]] = None) -> int:
    console.make_streams_utf8()
    parser = argparse.ArgumentParser(
        description="Applique database/migrations/*.sql à une instance PostgreSQL.",
        epilog=(
            "Les migrations sont idempotentes : relancer ce script est normal, "
            "pas une imprudence."
        ),
    )
    parser.add_argument(
        "--database-url",
        help=f"URL PostgreSQL cible (sinon la variable {ENV_VAR})",
    )
    parser.add_argument(
        "--platform-stub",
        action="store_true",
        help=(
            "crée d'abord ce que Supabase fournit et qu'un Postgres nu n'a pas "
            "(schémas `extensions` et `storage`, rôles anon/authenticated/service_role, "
            "table `storage.buckets`) — nécessaire pour la migration 008"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="liste les migrations dans l'ordre, sans se connecter",
    )
    args = parser.parse_args(argv)

    files = migration_files()

    if args.dry_run:
        print(f"{len(files)} migrations, dans l'ordre d'application :")
        for path in files:
            print(f"  {path.name}")
        return 0

    try:
        url = database_url(args.database_url)
    except RuntimeError as exc:
        print(f"{exc}", file=sys.stderr)
        print(f"  exemple : {ENV_VAR}=\"postgresql://user:motdepasse@localhost:5432/ai_trade_test\"", file=sys.stderr)
        return 2

    print(f"Cible : {redacted(url)}")
    if args.platform_stub:
        print("Stub de plateforme : schémas extensions/storage, rôles anon/authenticated/service_role")
    try:
        with connect(url) as connection:
            for applied in apply_all(connection, files, platform_stub=args.platform_stub):
                print(f"  [ok] {applied.file} ({applied.milliseconds} ms)")
    except MigrationFailed as exc:
        print(f"  [échec] {exc}", file=sys.stderr)
        print(
            "  La base reste dans l'état du dernier fichier appliqué : "
            "chaque fichier est une transaction.",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        print(f"  [échec] connexion impossible : {exc}", file=sys.stderr)
        return 1

    print(f"\n{len(files)} migrations appliquées à {redacted(url)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
