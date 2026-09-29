"""Doublure **partagée** du client Supabase : le client en mémoire des tests.

Trois fichiers portaient chacun leur variante de la même API, et elles ne
disaient pas la même chose :

* `tests/test_media_store.py` tenait `_Client`/`_Table`/`_Bucket`, plus six
  doublures ad hoc (`_ReviewTable`, `_PagedTable`, `_TreeBucket`, …) dont
  certaines **ignoraient les filtres** — `rows` était rendu tel quel, quoi qu'on
  ait demandé ;
* `tests/test_knowledge_index.py` tenait `_FakeTable` (journal seul) et
  `_ScriptedTable`/`_ScriptedClient`, qui servaient des **pages écrites
  d'avance**, là encore sans appliquer les filtres ;
* `tests/test_media_cycle.py` tenait `FakeSupabase`, qui les appliquait — et
  c'est la seule des trois dont le cycle média pouvait dépendre.

Cinq autres doublures sont venues s'y joindre : `_FakeTable`/`_FakeClient` de
`tests/test_media_reconcile.py` et de `tests/test_supabase_watch.py` (les
réglages servis depuis un **dictionnaire** tenu à part, donc relus sans passer
par la lecture du module — une écriture ratée restait invisible),
`_FakeTable`/`_FakeClient` de `tests/test_telegram_scan_config.py`,
`_Query`/`_Table`/`_FakeSupabase` de `tests/test_learning_gd.py` (qui
**ignoraient les filtres**, au point qu'un historique semé sans `asset`
ressortait quand même) et `_FakeSupabase` de `tests/test_api_auth_integration.py`
(qui rendait la même liste à n'importe quelle table, quel que soit le filtre).

Semer un réglage, dès lors, c'est semer des **lignes** —
`client.store("bot_settings").rows = [{"key": …, "value": …}]` — et les relire
passe par `database.settings.get_setting`, la vraie lecture : une écriture qui
n'a pas eu lieu ne peut donc pas passer pour une réussite. Dans le même esprit,
`upsert` ne **devine** pas de clé primaire : sans `on_conflict`, il n'a rien à
quoi comparer et ajoute la ligne. Nommer la cible (ce que fait
`database/settings.py` avec `on_conflict="key"`) est donc aussi ce qui rend
l'écriture relisible.

Il n'en reste qu'une, et c'est la plus exigeante : **les écritures persistent**
(une table partagée, pas une réponse figée), **les lectures sont filtrées,
triées et paginées** comme PostgREST les rend, et les identifiants sont attribués
à l'insertion comme Postgres les attribue. Un test qui sème une ligne sans le
champ sur lequel il filtre obtient `[]` — c'est le comportement réel, donc la
bonne surprise à avoir en écrivant un test.

Le journal (`client.calls`, `table.calls`) est un flux de tuples, identique dans
les trois fichiers :

| Entrée | Émise par |
|---|---|
| `("select", args, kwargs)` | la projection demandée |
| `("insert", payload)` / `("upsert", payload, on_conflict)` | l'écriture |
| `("update", payload)` / `("delete",)` | l'écriture |
| `("eq", champ, valeur)` / `("in", champ, valeurs)` / `("or", expression)` | un filtre |
| `("order", champ, desc)` / `("limit", n)` / `("range", début, fin)` | tri et pagination |
| `("rpc", nom, params)` | l'appel de fonction Postgres |

`client.calls` tient le journal de tout, `table.calls` celui d'une table : quand
deux tables sont lues dans le même test (un média et ses morceaux), c'est la
seconde qui dit **à qui** appartient un `eq`. Les entrées sont ajoutées au
moment de la construction de la requête, pas à l'exécution : une requête bâtie
puis abandonnée reste visible, ce qui est justement ce qu'un test doit pouvoir
observer.

Les filtres interprétés sont `eq`, `in_` et `or_` (branches `champ.op.valeur`
séparées par des virgules, avec `is.null`, `eq` et `neq`) ; un filtre qu'on ne
sait pas lire **lève** plutôt que de rendre des lignes en trop — un doublure
silencieusement plus permissive que Postgres ferait passer un filtre perdu.

`SupabaseDouble.rpc(name, params)` rend les lignes de `client.rpc_rows`, ou
appelle le gestionnaire posé par `handle_rpc` — c'est ainsi que
`test_media_cycle.py` rejoue `match_knowledge_chunks` sur les morceaux vraiment
écrits, au lieu de rendre une réponse figée. Un nom d'appel sans gestionnaire ni
lignes fait échouer le test : un RPC oublié ne se confond pas avec un RPC vide.

## Le mode lecture seule

`SupabaseDouble(read_only=True)` **refuse toute écriture** : une insertion, une mise
à jour, une suppression, un dépôt dans le bucket, et un RPC non déclaré. C'est la
doublure qui a servi à porter les tests de la sonde Supabase
(`tests/test_supabase_config.py`) : `scripts/check_supabase.py` se lance sans
`--roundtrip` sur une base de **production**, donc « le chemin de lecture n'écrit
rien » est la propriété la plus importante du script — et elle se vérifie en
laissant la doublure échouer si elle est fausse, plutôt qu'en relisant le code.

Trois précisions, chacune un choix :

* **le refus tombe à la construction** de l'écriture, avant le journal : une
  écriture refusée n'a été ni demandée au serveur ni écrite, donc elle n'apparaît
  nulle part — c'est le refus, avec son message, qui dit ce qui a été tenté ;
* **un RPC est supposé écrire** tant qu'on ne dit pas le contraire : la doublure ne
  peut pas lire le corps d'une fonction Postgres, donc `handle_rpc(name, handler,
  writes=False)` est la seule façon d'en autoriser un en lecture seule. Laisser
  passer en silence serait exactement le trou que ce mode prétend fermer ;
* **le bucket suit son client** (`client.storage.owner`), donc basculer
  `client.read_only` après coup ferme aussi le bucket — un régime par objet serait
  une invitation à l'oublier.

## Les pannes programmées

`client.fail(table, operation, error)` remplace les dictionnaires `fail_on` que
chaque fichier portait à sa façon : l'opération est nommée comme dans le journal
(`select`, `insert`, `upsert`, `update`, `delete`), et un nom inconnu **lève** plutôt
que de ne rien poser — une panne qu'on croit avoir programmée et qui n'arrive pas
est un test qui ne teste rien.

`error` est une exception — la panne dure — ou une **liste**, consommée une par
tentative : `client.fail("insights", "delete", [RuntimeError("hoquet")])` échoue une
fois puis passe. C'est ainsi qu'on éprouve une reprise (la sonde réessaie ses
suppressions, et un reste déclaré sur un hoquet coûte deux fois) sans écrire une
doublure de plus.

## Ce qui a été écrit, et non ce qui a été demandé

Le journal de `calls` dit ce qui a été **demandé** — une requête bâtie puis
abandonnée y figure, c'est ce qu'un test veut parfois observer. `client.writes` dit
ce qui a été **écrit**, dans l'ordre, sous forme de couples `(opération, table)` :
`write_order("delete")` rend donc l'ordre réel des suppressions, ce dont dépend le
nettoyage de la sonde (`pending_signals` avant `users`, sans quoi la clé étrangère
emporte des lignes qui ne sont pas les siennes). Une écriture refusée ou une panne
simulée ne laisse rien dans `writes` : elle n'a pas eu lieu.

Voir `tests/hook_support.py` et `tests/sql_columns.py` pour les deux autres aides
partagées du dossier.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, NoReturn, Optional, Tuple
from unittest import mock

#: Le nom d'opération d'un test, et le champ de panne qu'il désigne.
_OPERATION_ERRORS = {
    "read": "read_error",
    "select": "read_error",
    "insert": "insert_error",
    "upsert": "upsert_error",
    "update": "update_error",
    "delete": "delete_error",
}


class ReadOnlyError(AssertionError):
    """Écriture refusée : la doublure est en **lecture seule** (voir le module).

    Un `AssertionError`, comme les autres refus de la doublure (filtre non
    interprété, RPC inattendu) : c'est un test qui a demandé quelque chose
    d'impossible, pas une panne simulée.
    """


def _refuse(operation: str, target: str, hint: str = "") -> NoReturn:
    """Le refus, en une phrase : ce qui a été tenté, et sur quoi."""
    message = (
        f"écriture refusée : la doublure est en lecture seule — {operation} sur « {target} »"
    )
    raise ReadOnlyError(f"{message} ({hint})" if hint else message)


class Result:
    """Ce que rend `.execute()` : un porteur de `data`, comme le vrai client."""

    __slots__ = ("data",)

    def __init__(self, data: List[Dict[str, Any]]):
        self.data = data


class Table:
    """Les lignes d'une table, son journal d'appels et ses réglages de test."""

    def __init__(self, client: "SupabaseDouble", name: str):
        self.client = client
        self.name = name
        #: Les lignes vivantes. On les sème par ce champ, ou par l'API (`insert`).
        self.rows: List[Dict[str, Any]] = []
        #: Le journal de **cette** table (le client tient celui de toutes).
        self.calls: List[tuple] = []
        #: Les charges écrites, dans l'ordre, telles qu'elles ont été envoyées.
        self.inserted: List[Any] = []
        self.upserted: List[Tuple[Any, Optional[str]]] = []
        self.updated: List[Dict[str, Any]] = []
        #: Un `delete` a-t-il été **exécuté** sur cette table ?
        self.deleted = False
        #: Dernière projection demandée et dernier filtre `eq` : de quoi affirmer
        #: *ce qui a été lu* (et pas seulement ce qui a été écrit).
        self.selection: Optional[str] = None
        self.where: Optional[Tuple[str, Any]] = None
        #: Identifiant attribué à une ligne insérée. `None` = un compteur
        #: (`row-1`, `row-2`, …), le défaut de la doublure ; un test qui décrit
        #: une colonne `uuid` (celle de `knowledge_media`) pose ici
        #: `lambda: uuid.uuid4().hex`.
        self.id_factory: Optional[Callable[[], Any]] = None
        #: Pannes simulées, levées à l'exécution de l'opération concernée.
        #: Pannes programmées, levées à l'exécution de l'opération concernée. Une
        #: **liste** décrit une panne transitoire (voir `next_failure`).
        self.read_error: Any = None
        self.insert_error: Any = None
        self.upsert_error: Any = None
        self.update_error: Any = None
        self.delete_error: Any = None
        self._next_id = 1

    def next_failure(self, operation: str) -> Optional[BaseException]:
        """La panne de cette tentative, ou `None` — l'exception, ou la tête d'une séquence.

        Une **liste** décrit une panne transitoire : l'élément consommé est retiré
        du champ, donc la tentative suivante ne le retrouve pas. C'est ce qui rend
        une reprise éprouvable (la sonde réessaie ses suppressions, et un reste
        déclaré sur un hoquet coûte deux fois) sans écrire une doublure de plus.
        """
        field = f"{operation}_error"
        value = getattr(self, field)
        if isinstance(value, (list, tuple)):
            remaining = list(value)
            head = remaining.pop(0) if remaining else None
            setattr(self, field, remaining or None)
            return head if isinstance(head, BaseException) else None
        return value

    def new_id(self) -> Any:
        """L'identifiant de la prochaine ligne insérée."""
        if self.id_factory is not None:
            return self.id_factory()
        value = f"row-{self._next_id}"
        self._next_id += 1
        return value

    def operations(self, name: str) -> List[tuple]:
        """Les entrées du journal de **cette** table pour une opération (`"range"`, …)."""
        return [entry for entry in self.calls if entry[0] == name]

    def insert_row(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """Pose une ligne en lui attribuant son identifiant, comme Postgres."""
        if record.get("id") is None:
            record["id"] = self.new_id()
        self.rows.append(record)
        return record


class Query:
    """Une requête chaînable : filtres, tri, pagination, puis lecture ou écriture."""

    def __init__(self, table: Table):
        self._table = table
        self._filters: List[tuple] = []
        self._orders: List[Tuple[str, bool]] = []
        self._limit: Optional[int] = None
        self._bounds: Optional[Tuple[int, int]] = None
        self._selection: Optional[str] = None
        self._write: Optional[Tuple[str, Any, Optional[str]]] = None

    # -- construction (journalisée) --------------------------------------- #

    def _logged(self, entry: tuple) -> "Query":
        self._table.calls.append(entry)
        self._table.client.calls.append(entry)
        return self

    def select(self, *args: Any, **kwargs: Any) -> "Query":
        self._selection = str(args[0]) if args else str(kwargs.get("columns", "*"))
        self._table.selection = self._selection
        return self._logged(("select", args, kwargs))

    def insert(self, payload: Any) -> "Query":
        self._refuse_write("insert")
        self._write = ("insert", payload, None)
        return self._logged(("insert", payload))

    def upsert(self, payload: Any, on_conflict: Optional[str] = None) -> "Query":
        self._refuse_write("upsert")
        self._write = ("upsert", payload, on_conflict)
        return self._logged(("upsert", payload, on_conflict))

    def update(self, payload: Dict[str, Any]) -> "Query":
        self._refuse_write("update")
        self._write = ("update", payload, None)
        return self._logged(("update", payload))

    def delete(self) -> "Query":
        self._refuse_write("delete")
        self._write = ("delete", None, None)
        return self._logged(("delete",))

    def _refuse_write(self, operation: str) -> None:
        """Le mode lecture seule du client, refusé **avant** le journal.

        Une écriture refusée n'a été ni demandée au serveur ni écrite : elle
        n'apparaît donc nulle part, et c'est le refus — nommant l'opération et la
        table — qui dit ce qui a été tenté.
        """
        table = self._table
        if table.client.read_only:
            _refuse(operation, table.name)

    def eq(self, field: str, value: Any) -> "Query":
        self._filters.append(("eq", field, value))
        self._table.where = (field, value)
        return self._logged(("eq", field, value))

    def in_(self, field: str, values: Any) -> "Query":
        self._filters.append(("in", field, list(values)))
        return self._logged(("in", field, tuple(values)))

    def or_(self, expression: str) -> "Query":
        self._filters.append(("or", expression))
        return self._logged(("or", expression))

    def order(self, field: str, desc: bool = False) -> "Query":
        self._orders.append((field, bool(desc)))
        return self._logged(("order", field, desc))

    def limit(self, count: int) -> "Query":
        self._limit = int(count)
        return self._logged(("limit", count))

    def range(self, start: int, end: int) -> "Query":
        self._bounds = (int(start), int(end))
        return self._logged(("range", start, end))

    # -- exécution -------------------------------------------------------- #

    def execute(self) -> Result:
        if self._write is not None:
            return self._write_rows(*self._write)
        table = self._table
        failure = table.next_failure("read")
        if failure is not None:
            raise failure
        return Result(self._project(self._slice(self._sort(self._matching()))))

    def _write_rows(
        self, operation: str, payload: Any, on_conflict: Optional[str]
    ) -> Result:
        table = self._table
        failure = table.next_failure(operation)
        if failure is not None:
            raise failure
        #: Une écriture **exécutée**, et sur quelle table : `calls` dit ce qui a été
        #: demandé, ceci dit ce qui a eu lieu (`write_order`).
        table.client.writes.append((operation, table.name))
        if operation == "insert":
            rows = [table.insert_row(dict(row)) for row in _rows_of(payload)]
            table.inserted.append(payload)
            return Result([dict(row) for row in rows])
        if operation == "upsert":
            return self._upsert(_rows_of(payload), on_conflict)
        if operation == "update":
            table.updated.append(payload)
            touched = []
            for row in table.rows:
                if self._matches(row):
                    row.update(payload)
                    touched.append(dict(row))
            return Result(touched)
        removed = [row for row in table.rows if self._matches(row)]
        for row in removed:
            table.rows.remove(row)
        table.deleted = True
        return Result([dict(row) for row in removed])

    def _upsert(self, rows: List[Any], on_conflict: Optional[str]) -> Result:
        """`upsert` de PostgREST : la ligne de même clé est **remplacée**, id compris.

        PostgREST accepte une ligne ou une liste, et écrit **toutes** celles qu'on
        lui donne : n'en garder qu'une ferait passer une écriture par lot pour une
        écriture simple (`upsert_economic_events` envoie une liste).
        """
        table = self._table
        # La charge telle qu'elle est **partie**, avant que l'identifiant attribué
        # ne s'y ajoute : c'est ce que le journal doit montrer.
        table.upserted.append((dict(rows[0]) if len(rows) == 1 else [dict(row) for row in rows], on_conflict))
        written = []
        for row in rows:
            payload = dict(row)
            if on_conflict:
                same_key = [
                    existing
                    for existing in table.rows
                    if existing.get(on_conflict) == payload.get(on_conflict)
                ]
                for existing in same_key:
                    payload.setdefault("id", existing.get("id"))
                    table.rows.remove(existing)
            written.append(dict(table.insert_row(payload)))
        return Result(written)

    # -- lecture ---------------------------------------------------------- #

    def _matching(self) -> List[Dict[str, Any]]:
        return [row for row in self._table.rows if self._matches(row)]

    def _matches(self, row: Dict[str, Any]) -> bool:
        return all(self._filter(row, *entry) for entry in self._filters)

    @staticmethod
    def _filter(row: Dict[str, Any], kind: str, *args: Any) -> bool:
        if kind == "eq":
            field, value = args
            return row.get(field) == value
        if kind == "in":
            field, values = args
            return row.get(field) in values
        if kind == "or":
            (expression,) = args
            return any(_branch_matches(row, part) for part in str(expression).split(","))
        raise AssertionError(f"filtre non interprété : {kind}")

    def _sort(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        # `order` se compose : le dernier appelé est la clé la plus fine, donc on
        # trie en remontant la liste (le tri de Python est stable). Un champ
        # absent se range à la fin, quel que soit le sens.
        ordered = list(rows)
        for field, desc in reversed(self._orders):
            ordered.sort(
                key=lambda row: (row.get(field) is None, row.get(field)), reverse=desc
            )
        return ordered

    def _slice(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if self._limit is not None:
            rows = rows[: self._limit]
        if self._bounds is not None:
            start, end = self._bounds
            rows = rows[start : end + 1]
        return rows

    def _project(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        columns = _projection(self._selection)
        if columns is None:
            return rows
        return [{key: value for key, value in row.items() if key in columns} for row in rows]


class RpcCall:
    """L'appel RPC chaîné : `client.rpc(nom, params).execute()`."""

    def __init__(self, handler: Callable[[Dict[str, Any]], Any], params: Dict[str, Any]):
        self._handler = handler
        self._params = params

    def execute(self) -> Result:
        return Result(list(self._handler(self._params) or []))


class Storage:
    """Le bucket en mémoire : les octets déposés, ce que `list` rend, les journaux.

    `listing` décrit ce que `list(chemin)` rend, et il faut le **renseigner** pour
    les tests de parcours : un dictionnaire quand on veut la sémantique
    **non récursive** de l'API Storage (chaque entrée ne décrit que les enfants
    directs d'un dossier), une liste plate pour un bucket sans arborescence.
    """

    def __init__(
        self,
        *,
        files: Optional[Dict[str, bytes]] = None,
        listing: Any = None,
        read_only: bool = False,
    ):
        #: Chemin → octets déposés (ce que `download` relit).
        self.files: Dict[str, bytes] = dict(files or {})
        self.listing: Any = [] if listing is None else listing
        self.uploads: List[Tuple[str, bytes, Optional[Dict[str, Any]]]] = []
        self.removed: List[List[str]] = []
        #: Les buckets atteints via `storage.from_(…)`, dans l'ordre.
        self.buckets: List[str] = []
        self.bucket_name: Optional[str] = None
        #: Le bucket peut être en lecture seule pour lui-même…
        self.read_only = bool(read_only)
        #: …ou parce que le client qui le porte l'est : le régime du client
        #: s'applique, **même posé après la construction** (voir `SupabaseDouble`).
        self.owner: Optional["SupabaseDouble"] = None

    def from_(self, name: str) -> "Storage":
        self.buckets.append(name)
        self.bucket_name = name
        return self

    def _refuse_write(self, operation: str, target: str) -> None:
        """Le refus de dépôt, avant que quoi que ce soit soit journalisé ou déposé."""
        if self.read_only or (self.owner is not None and self.owner.read_only):
            _refuse(operation, target, hint="le bucket suit le client qui le porte")

    def upload(self, path: str, data: bytes, options: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
        self._refuse_write("upload", path)
        payload = bytes(data)
        self.uploads.append((path, payload, dict(options) if options else options))
        self.files[path] = payload
        return {"path": path}

    def download(self, path: str) -> bytes:
        """Les octets déposés — `KeyError` si l'objet n'est pas là, comme l'API."""
        return self.files[path]

    def remove(self, paths: Any) -> Dict[str, List[str]]:
        for path in paths:
            self._refuse_write("remove", path)
        removed = list(paths)
        self.removed.append(removed)
        for path in removed:
            self.files.pop(path, None)
        return {"removed": removed}

    def list(self, path: Optional[str] = None, options: Optional[Dict[str, Any]] = None) -> Any:
        if isinstance(self.listing, dict):
            return self.listing.get(path or "", [])
        return self.listing

    def create_signed_url(self, path: str, expires_in: int) -> Dict[str, str]:
        """Le vrai client signe côté serveur ; la doublure rend une URL déterministe."""
        return {"signedURL": f"https://example.test/{path}?exp={expires_in}"}


class SupabaseDouble:
    """Le client : des tables nommées, un bucket, des RPC, et le journal des appels."""

    def __init__(
        self,
        *,
        storage: Optional[Storage] = None,
        handlers: Optional[Dict[str, Callable[[Dict[str, Any]], Any]]] = None,
        read_only: bool = False,
    ):
        #: Le flux de toutes les opérations **demandées**, dans l'ordre (voir le module).
        self.calls: List[tuple] = []
        #: Ce qui a été **écrit**, dans l'ordre : `(opération, table)` (voir `write_order`).
        self.writes: List[Tuple[str, str]] = []
        #: Le régime du client : `True` refuse toute écriture (voir le module).
        #: Posable après coup : les tables, le bucket et les RPC le relisent à
        #: chaque tentative, donc rien n'échappe à un basculement tardif.
        self.read_only = bool(read_only)
        self.storage = storage if storage is not None else Storage()
        self.storage.owner = self
        #: Les lignes que rend un RPC **sans** gestionnaire dédié. `None` (le
        #: défaut) fait échouer un RPC inconnu au lieu de rendre une base vide.
        self.rpc_rows: Optional[List[Dict[str, Any]]] = None
        self._tables: Dict[str, Table] = {}
        self._handlers: Dict[str, Callable[[Dict[str, Any]], Any]] = dict(handlers or {})
        #: Ce que chaque RPC déclare faire (`handle_rpc(..., writes=…)`). Absent =
        #: supposé écrire, donc refusé en lecture seule.
        self._rpc_writes: Dict[str, bool] = {}

    def fail(self, table: str, operation: str, error: Any) -> None:
        """Programme une panne : `client.fail("insights", "select", RuntimeError(…))`.

        `error` est une exception — la panne dure — ou une **liste**, consommée une
        par tentative (voir `Table.next_failure`) : c'est ainsi qu'on éprouve une
        reprise sans écrire une doublure de plus. Une opération qu'on ne sait pas
        nommer **lève** : une panne qu'on croit avoir programmée et qui n'arrive
        pas est un test qui ne teste rien.
        """
        field = _OPERATION_ERRORS.get(str(operation))
        if field is None:
            raise AssertionError(f"opération inconnue pour une panne : {operation!r}")
        setattr(self.store(table), field, error)

    def write_order(self, operation: Optional[str] = None) -> List[str]:
        """Les tables écrites, **dans l'ordre** — toutes, ou une seule opération.

        L'ordre n'est pas cosmétique : le nettoyage de la sonde supprime
        `pending_signals` avant `users`, sans quoi la clé étrangère emporterait des
        lignes qui ne sont pas les siennes.
        """
        return [
            name for kind, name in self.writes if operation is None or kind == operation
        ]

    # -- tables ------------------------------------------------------------ #

    def store(self, name: str) -> Table:
        """La table nommée, créée à la première demande (pour semer des lignes)."""
        return self._tables.setdefault(name, Table(self, name))

    def table(self, name: str) -> Query:
        """Une requête neuve sur la table, comme `client.table(...)`."""
        return Query(self.store(name))

    def tables(self) -> List[str]:
        """Les tables touchées jusqu'ici, dans l'ordre de leur première demande."""
        return list(self._tables)

    # -- fonctions Postgres ------------------------------------------------ #

    def handle_rpc(
        self, name: str, handler: Callable[[Dict[str, Any]], Any], *, writes: bool = True
    ) -> None:
        """Fait calculer un RPC par `handler(params)` au lieu de `rpc_rows`.

        `writes=True` par défaut : une fonction Postgres dont on ne dit rien est
        supposée écrire, donc **refusée** en lecture seule. C'est la seule réponse
        honnête — la doublure ne peut pas lire le corps d'une fonction — et elle se
        lève en nommant la déclaration à faire (`writes=False` pour une lecture,
        comme `match_knowledge_chunks`).
        """
        self._handlers[name] = handler
        self._rpc_writes[name] = bool(writes)

    def rpc(self, name: str, params: Dict[str, Any]) -> RpcCall:
        params = dict(params)
        if self.read_only and self._rpc_writes.get(name, True):
            _refuse(
                "rpc",
                name,
                hint=f"déclare-le en lecture : handle_rpc({name!r}, …, writes=False)",
            )
        self.calls.append(("rpc", name, params))
        if name in self._handlers:
            handler = self._handlers[name]
        elif self.rpc_rows is not None:
            handler = lambda _params: list(self.rpc_rows or [])  # noqa: E731
        else:
            raise AssertionError(f"RPC inattendu : {name}")
        return RpcCall(handler, params)

    def rpc_calls(self) -> List[tuple]:
        """Les appels RPC, dans l'ordre : `(nom, params)`."""
        return [entry for entry in self.calls if entry[0] == "rpc"]

    # -- commodités d'assertion -------------------------------------------- #

    def updates(self) -> List[Dict[str, Any]]:
        """Les charges `update` envoyées, dans l'ordre."""
        return [entry[1] for entry in self.calls if entry[0] == "update"]

    def operations(self, name: str) -> List[tuple]:
        """Les entrées du journal pour une opération (`"select"`, `"eq"`, …)."""
        return [entry for entry in self.calls if entry[0] == name]


def use_supabase(test: Any, client: SupabaseDouble, *modules: Any) -> SupabaseDouble:
    """Pose `client` comme `supabase` des modules visés, retiré en fin de test."""
    for module in modules:
        patcher = mock.patch.object(module, "supabase", client)
        patcher.start()
        test.addCleanup(patcher.stop)
    return client


def _rows_of(payload: Any) -> List[Any]:
    """La charge d'une écriture, en lignes : PostgREST accepte une ligne ou une liste."""
    if isinstance(payload, list):
        return payload
    return [payload]


def _projection(selection: Optional[str]) -> Optional[set]:
    """Les colonnes d'une projection PostgREST, ou `None` pour « toutes » (`*`)."""
    if not selection or selection.strip() == "*":
        return None
    return {column.strip() for column in selection.split(",") if column.strip()}


def _branch_matches(row: Dict[str, Any], branch: str) -> bool:
    """Une branche d'un `or_` PostgREST : `champ.op.valeur` (op = `is`/`eq`/`neq`)."""
    parts = str(branch).split(".", 2)
    if len(parts) != 3:
        raise AssertionError(f"branche `or_` illisible : {branch}")
    field, operator, raw = parts
    if operator == "is":
        if raw != "null":
            raise AssertionError(f"branche `or_` non interprétée : {branch}")
        return row.get(field) is None
    if operator == "eq":
        return row.get(field) == raw
    if operator == "neq":
        return row.get(field) != raw
    raise AssertionError(f"branche `or_` non interprétée : {branch}")
