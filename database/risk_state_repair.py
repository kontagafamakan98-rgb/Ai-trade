"""Diagnostiquer et réparer les états de risque inutilisables (`user_risk_state`).

Le garde-fou de risque (`execution/risk_guard.py`) refuse de trader quand une
référence de solde n'est pas exploitable — et il a raison : « 5 % de 0 » n'est pas
5 %. Mais une ligne refusée n'est pas une ligne perdue : elle est **réparable**.
Deux formes de référence sont en cause, et elles ne se réparent pas de la même
façon — c'est tout l'objet de ce module :

* **une référence présente mais non positive** (0, négative) — le garde la refuse en
  nommant le champ. La réparer en reprenant le solde **courant** effacerait la perte
  déjà subie : un compte en ruine retraderait. C'est précisément ce que ce module
  refuse de faire ;
* **une référence absente (`NULL`)** — le garde la **dérive du solde courant**
  (`_reference_balance`), donc il ne refuse pas… et c'est le piège : la perte
  relative à cette référence disparaît en silence, et le drawdown repart à zéro.

Le seul point d'ancrage honnête pour une valeur positive est le **capital configuré**
de l'utilisateur (`user_preferences.paper_equity`) : c'est la valeur que
l'initialisation du garde aurait écrite (`_get_or_init_state` reçoit le solde lu, qui
vaut `paper_equity` en mode papier). On ne **devine** donc pas : on propose la valeur
qui aurait dû être là, **jamais le solde courant**. Quand ce capital n'est pas connu,
le module ne fabrique rien : la ligne reste `needs-value` et le rapport dit quoi
fournir (`--set <utilisateur>:<champ>=<valeur>`).

Deux invariants, chacun éprouvé par un test et par une mutation :

* **on ne supprime jamais une ligne** — la supprimer la ferait réinitialiser au
  prochain solde lu, c'est-à-dire exactement l'effacement silencieux qu'on empêche ;
* **on n'écrit jamais une valeur non positive** — c'est le défaut à réparer, pas une
  réparation.

Une référence du **jour** close (`daily_date` d'avant aujourd'hui) n'est pas un
défaut : le garde repart d'un compteur journalier frais au prochain solde lu
(`_get_or_init_state`), et une nouvelle journée n'a pas de perte « déjà subie ». Le
module le **dit sans rien écrire**.

Ce que la réparation **ne peut pas** faire : reconstituer une référence dont le
capital d'origine n'est plus nulle part. Pour ces cas-là, le seul recours est un
`--set` explicite de l'opérateur — un acte délibéré, jamais un effacement en douce.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from database.supabase_client import supabase

TABLE = "user_risk_state"
PREFERENCES_TABLE = "user_preferences"

#: Les deux références en pourcentage. Le nom sert aussi de champ `--set`.
STARTING_BALANCE = "starting_balance"
DAILY_START_BALANCE = "daily_start_balance"
REFERENCE_FIELDS: Tuple[str, ...] = (STARTING_BALANCE, DAILY_START_BALANCE)

#: La date du compteur journalier : c'est elle qui dit si une référence du jour est
#: close (le garde la réinitialise) ou courante (elle doit être exploitable).
DATE_FIELD = "daily_date"

#: La colonne de préférences qui porte le capital configuré — d'où vient la valeur
#: de reprise par défaut.
CAPITAL_FIELD = "paper_equity"

# Verdicts.
STATE_OK = "ok"
STATE_REPAIRABLE = "repairable"
STATE_NEEDS_VALUE = "needs-value"

#: Une page de lecture. PostgREST borne les réponses : on pagine plutôt que de
#: laisser une table grande rendre un résultat tronqué sans le dire.
PAGE_SIZE = 1000


def _number(value: Any) -> Optional[float]:
    """La valeur en flottant, ou `None` — jamais une exception pour une donnée illisible.

    `bool` est écarté explicitement : `True` n'est pas un solde, et `float(True)`
    vaut 1, ce qui laisserait passer une valeur qui n'en est pas une.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _positive(value: Optional[float]) -> Optional[float]:
    """La valeur si elle est strictement positive, sinon `None`."""
    return value if value is not None and value > 0 else None


def _field_state(value: Optional[float]) -> str:
    """Comment un champ se décrit dans le rapport : absent, ou sa valeur fautive."""
    return "absent" if value is None else f"≤ 0 ({value:g})"


@dataclass(frozen=True)
class Diagnosis:
    """Le verdict d'une ligne : ce qui ne va pas, et ce qu'on propose d'écrire."""

    user_id: str
    status: str
    problems: List[str] = field(default_factory=list)
    proposal: Dict[str, float] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    @property
    def touched(self) -> bool:
        """Y a-t-il quelque chose à écrire ?"""
        return bool(self.proposal)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id,
            "status": self.status,
            "problems": list(self.problems),
            "proposal": {name: float(value) for name, value in self.proposal.items()},
            "notes": list(self.notes),
        }


def _repair_problem(field_name: str, current: Optional[float], value: float, explicit: bool) -> str:
    """Pourquoi on propose cette valeur — et pourquoi ce n'est pas le solde courant."""
    origin = (
        "valeur fournie explicitement (--set)"
        if explicit
        else f"capital configuré ({CAPITAL_FIELD})"
    )
    return (
        f"{field_name} {_field_state(current)} : reprise proposée à {value:g} — {origin}. "
        "Jamais le solde courant, qui remettrait la perte déjà subie à zéro."
    )


def diagnose(
    state: Mapping[str, Any],
    *,
    configured_capital: Optional[float] = None,
    today: Optional[str] = None,
    overrides: Optional[Mapping[str, float]] = None,
) -> Diagnosis:
    """Le verdict d'une ligne `user_risk_state`, sans rien écrire.

    `configured_capital` est le capital de l'utilisateur (`paper_equity`) : la
    valeur de reprise par défaut. `overrides` porte les valeurs explicites de
    l'opérateur (`--set`), qui **priment** — mais jamais au point d'écrire une
    valeur non positive : celles-là sont écartées comme le défaut qu'elles sont.
    """
    overrides = overrides or {}
    today = today or date.today().isoformat()
    user_id = str(state.get("user_id") or "")

    starting = _number(state.get(STARTING_BALANCE))
    daily = _number(state.get(DAILY_START_BALANCE))
    daily_date = str(state.get(DATE_FIELD) or "")

    problems: List[str] = []
    notes: List[str] = []
    proposal: Dict[str, float] = {}
    needs_value = False

    # --- starting_balance : la référence du drawdown total -----------------
    if _positive(starting) is None:
        explicit = overrides.get(STARTING_BALANCE)
        value = _positive(explicit)
        if value is None:
            value = _positive(configured_capital)
        if value is not None:
            proposal[STARTING_BALANCE] = value
            problems.append(_repair_problem(STARTING_BALANCE, starting, value, explicit is not None))
        else:
            needs_value = True
            problems.append(
                f"{STARTING_BALANCE} {_field_state(starting)} : aucune valeur positive "
                f"connue (capital configuré « {CAPITAL_FIELD} » manquant). Fournis-la avec "
                f"--set <utilisateur>:{STARTING_BALANCE}=<valeur>."
            )

    # --- daily_start_balance : la référence du compteur du jour ------------
    if _positive(daily) is None:
        if daily_date != today:
            # Journée close : le garde repart d'un compteur frais au prochain solde
            # lu. Rien à écrire, et rien à effacer — une nouvelle journée n'a pas de
            # perte « déjà subie ».
            notes.append(
                f"{DAILY_START_BALANCE} {_field_state(daily)} mais la journée est close "
                f"({DATE_FIELD} = {daily_date or '—'}) : le garde le réinitialise au "
                "prochain solde lu — aucune écriture."
            )
        else:
            explicit = overrides.get(DAILY_START_BALANCE)
            value = _positive(explicit)
            if value is None:
                # Faute de connaître le solde d'ouverture du jour, on prend la
                # référence de capital : au pire la perte du jour est **surévaluée**
                # (un refus de trop), jamais effacée. Le refus est réparable ; une
                # perte effacée ne l'est pas.
                value = _positive(proposal.get(STARTING_BALANCE))
            if value is None:
                value = _positive(configured_capital)
            if value is not None:
                proposal[DAILY_START_BALANCE] = value
                problems.append(
                    f"{DAILY_START_BALANCE} {_field_state(daily)} et la journée est "
                    f"ouverte ({DATE_FIELD} = {today}) : le solde d'ouverture est "
                    f"inconnu, on propose {value:g} (la référence de capital, jamais le "
                    "solde courant) — au pire la perte du jour est surévaluée, jamais "
                    "effacée."
                )
            else:
                needs_value = True
                problems.append(
                    f"{DAILY_START_BALANCE} {_field_state(daily)}, journée ouverte, et "
                    "aucune référence de capital connue. Fournis-la avec "
                    f"--set <utilisateur>:{DAILY_START_BALANCE}=<valeur>."
                )

    if needs_value:
        status = STATE_NEEDS_VALUE
    elif proposal:
        status = STATE_REPAIRABLE
    else:
        status = STATE_OK

    return Diagnosis(
        user_id=user_id,
        status=status,
        problems=problems,
        proposal=proposal,
        notes=notes,
    )


def parse_overrides(items: Sequence[str]) -> Dict[str, Dict[str, float]]:
    """Traduit les `--set <utilisateur>:<champ>=<valeur>` en plan par utilisateur.

    Une valeur non positive est **refusée** : c'est le défaut qu'on répare, pas une
    réparation. Un champ inconnu ou une forme illisible lève, pour que l'erreur se
    lise à la ligne de commande plutôt que dans un rapport qu'on croira appliqué.
    """
    fields = "|".join(REFERENCE_FIELDS)
    out: Dict[str, Dict[str, float]] = {}
    for item in items:
        user_id, _, rest = str(item).partition(":")
        field_name, sep, raw = rest.partition("=")
        user_id, field_name, raw = user_id.strip(), field_name.strip(), raw.strip()
        if not user_id or not sep or field_name not in REFERENCE_FIELDS or not raw:
            raise ValueError(
                f"--set attend <utilisateur>:{fields}=<valeur> (reçu : {item!r})"
            )
        try:
            value = float(raw)
        except ValueError as exc:
            raise ValueError(f"valeur non numérique pour {user_id}/{field_name} : {raw!r}") from exc
        if value <= 0:
            raise ValueError(
                f"valeur refusée pour {user_id}/{field_name} : {value:g} ≤ 0 — c'est le "
                "défaut à réparer, pas une réparation."
            )
        out.setdefault(user_id, {})[field_name] = value
    return out


def _read_all(table: str) -> List[Dict[str, Any]]:
    """Toutes les lignes d'une table, page par page (PostgREST borne les réponses)."""
    rows: List[Dict[str, Any]] = []
    start = 0
    while True:
        res = supabase.table(table).select("*").range(start, start + PAGE_SIZE - 1).execute()
        chunk = list((res.data if res else None) or [])
        rows.extend(chunk)
        if len(chunk) < PAGE_SIZE:
            return rows
        start += PAGE_SIZE


def survey(
    *,
    today: Optional[str] = None,
    overrides: Optional[Mapping[str, Mapping[str, float]]] = None,
) -> List[Diagnosis]:
    """Le verdict de **chaque** ligne d'état de risque — sans rien écrire."""
    if not supabase:
        raise RuntimeError(
            "Supabase n'est pas configuré : renseigne SUPABASE_URL et "
            "SUPABASE_SERVICE_KEY (voir .env.example)."
        )
    overrides = overrides or {}
    capitals: Dict[str, Optional[float]] = {}
    for row in _read_all(PREFERENCES_TABLE):
        capitals[str(row.get("user_id") or "")] = _number(row.get(CAPITAL_FIELD))

    diagnoses: List[Diagnosis] = []
    for row in _read_all(TABLE):
        user_id = str(row.get("user_id") or "")
        diagnoses.append(
            diagnose(
                row,
                configured_capital=capitals.get(user_id),
                today=today,
                overrides=overrides.get(user_id) or {},
            )
        )
    return diagnoses


def apply_repairs(diagnoses: Sequence[Diagnosis]) -> int:
    """Écrit les seules valeurs proposées — positives, jamais une suppression.

    Rend le nombre de lignes écrites. Rien n'est supprimé : une suppression ferait
    réinitialiser la ligne au prochain solde lu, donc effacerait la perte déjà subie
    en silence.
    """
    written = 0
    for diagnosis in diagnoses:
        payload = {
            name: float(value)
            for name, value in diagnosis.proposal.items()
            if value is not None and value > 0
        }
        if not payload:
            continue
        supabase.table(TABLE).update(payload).eq("user_id", diagnosis.user_id).execute()
        written += 1
    return written


def build_report(
    diagnoses: Sequence[Diagnosis], *, applied: bool = False, repaired: int = 0
) -> Dict[str, Any]:
    """Le rapport — des verdicts et des propositions, jamais une donnée sensible."""
    counts = {STATE_OK: 0, STATE_REPAIRABLE: 0, STATE_NEEDS_VALUE: 0}
    for diagnosis in diagnoses:
        counts[diagnosis.status] = counts.get(diagnosis.status, 0) + 1
    return {
        "table": TABLE,
        "rows": len(diagnoses),
        "ok": counts[STATE_OK],
        "repairable": counts[STATE_REPAIRABLE],
        "needs_value": counts[STATE_NEEDS_VALUE],
        "applied": bool(applied),
        "repaired": int(repaired),
        "diagnoses": [diagnosis.as_dict() for diagnosis in diagnoses],
    }


def run(
    *,
    apply: bool = False,
    today: Optional[str] = None,
    overrides: Optional[Mapping[str, Mapping[str, float]]] = None,
) -> Dict[str, Any]:
    """Diagnostique, puis (si `apply`) répare, et rend le rapport."""
    diagnoses = survey(today=today, overrides=overrides)
    repaired = apply_repairs(diagnoses) if apply else 0
    return build_report(diagnoses, applied=apply, repaired=repaired)


__all__ = [
    "CAPITAL_FIELD",
    "DAILY_START_BALANCE",
    "DATE_FIELD",
    "Diagnosis",
    "PAGE_SIZE",
    "PREFERENCES_TABLE",
    "REFERENCE_FIELDS",
    "STARTING_BALANCE",
    "STATE_NEEDS_VALUE",
    "STATE_OK",
    "STATE_REPAIRABLE",
    "TABLE",
    "apply_repairs",
    "build_report",
    "diagnose",
    "parse_overrides",
    "run",
    "survey",
]
