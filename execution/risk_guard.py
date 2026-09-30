"""
Garde-fous de risque par utilisateur — inspirés du RiskManager du bot
TradeLocker (limite de perte journalière + plafond de drawdown total +
nombre max de positions ouvertes), adaptés au multi-tenant.

Contrairement au bot TradeLocker (qui bloque avant de placer un ordre en
mémoire), ici l'état est persisté en base (table user_risk_state) pour
survivre aux redémarrages et fonctionner correctement avec plusieurs
utilisateurs en parallèle.

⚠️ Limite connue : pour les comptes utilisant la clé Alpaca PARTAGÉE
(pas de compte broker personnel connecté), on n'a pas de solde réel à
suivre — le calcul utilise `paper_equity` (statique) comme approximation.
Pour un compte personnel connecté, le vrai solde Alpaca est utilisé.

⚠️ Conséquence directe, et c'est un refus assumé : quand ce solde n'est pas
lisible (0, absent, négatif), **aucun** plafond en pourcentage n'est mesurable —
« 5 % de 0 » n'est pas 5 %, c'est un pourcentage d'un chiffre qu'on n'a pas. Le
garde refuse alors le trade en le disant, au lieu de sauter ses deux contrôles en
silence (`if daily_start_balance > 0`). Le refus est réparable : corriger la
référence dans `user_risk_state`, ou supprimer la ligne pour qu'elle soit
réinitialisée au prochain solde lu.

Ce refus ne reste pas muet pour l'exploitant : il incrémente un **compteur
dédié** (`reference_unusable_snapshot`, exposé par la route d'administration) et
sort dans le **journal** sous un préfixe unique (`risk_guard.reference-unusable`).
Un utilisateur bloqué lit sa raison ; l'exploitant, lui, doit pouvoir remarquer
que plusieurs comptes le sont — sinon une ligne cassée bloque en silence.
"""
import logging
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Tuple

from database.supabase_client import supabase
from database.preferences import get_preferences

logger = logging.getLogger(__name__)

TABLE = "user_risk_state"

#: Les deux références en pourcentage, nommées une fois : le contrôle et le verdict
#: structuré doivent désigner le même champ.
STARTING_FIELD = "starting_balance"
DAILY_FIELD = "daily_start_balance"

#: Les verdicts, en identifiants machines : une application les traduit et affiche
#: les chiffres du refus, au lieu de relire une phrase.
CODE_ALLOWED = "allowed"
CODE_BALANCE_UNREADABLE = "balance-unreadable"
CODE_REFERENCE_UNUSABLE = "reference-unusable"
CODE_MAX_OPEN_TRADES = "max-open-trades"
CODE_DAILY_LOSS = "daily-loss"
CODE_TOTAL_DRAWDOWN = "total-drawdown"
CODE_ERROR = "error"

#: Refus quand le solde courant n'est pas lisible. Nommé, donc réutilisable : un
#: test qui recopie le message le laisserait diverger.
NO_BALANCE_REASON = (
    "Solde de compte indisponible ou nul : aucun plafond en pourcentage ne peut être "
    "mesuré, et un compte dont le solde n'est pas lu n'est pas un compte autorisé. "
    "Trade bloqué par précaution."
)

def _reference_balance(state: dict, field: str, current_balance: float) -> float:
    """La référence d'un garde en pourcentage, ou le solde courant si elle est absente.

    `state.get(field) or current_balance` confondait « absent » et « zéro » : sur une
    référence persistée à 0, le garde se mesurait à partir du solde **courant**, donc
    la perte valait 0 %, le plafond était ouvert, et rien ne le disait. Or une
    référence à 0 n'est pas « pas de limite » — c'est un chiffre qui n'en est pas un.

    Clé jamais écrite (`None`) : dérivée du solde courant. C'est l'initialisation, et
    c'est déjà ce que fait la remise à zéro du jour ; la relire ici ne fabrique rien.
    """
    raw = state.get(field)
    if raw is None:
        return current_balance
    return float(raw)


def _unusable_reference_fields(starting_balance: float, daily_start_balance: float) -> List[str]:
    """Les références présentes mais non positives (0, négative), dans l'ordre des champs.

    C'est la liste que l'application reçoit : elle nomme **lesquelles** corriger, là
    où la phrase ne fait que les énumérer.
    """
    references = {
        STARTING_FIELD: starting_balance,
        DAILY_FIELD: daily_start_balance,
    }
    return [name for name, value in references.items() if value <= 0]


#: Le préfixe du journal dédié à ce refus : une seule chaîne à chercher dans les
#: journaux de production pour retrouver les comptes bloqués par une ligne cassée.
REFERENCE_UNUSABLE_LOG = "risk_guard.reference-unusable"

#: Ce que le refus laisse derrière lui, en mémoire du processus. Un compteur ne se
#: relit pas dans un journal — personne ne recompte une sortie de logs — donc il
#: doit exister là où on peut le **demander** : la route d'administration l'expose.
_observation_lock = threading.Lock()
_reference_unusable: Dict[str, Any] = {"total": 0, "users": {}}


def reference_unusable_snapshot() -> Dict[str, Any]:
    """Le compteur dédié : le total des refus, et qui est bloqué par quel champ.

    Rend une **copie** — l'appelant lit un instantané, il ne peut pas muter l'état
    du garde. Chaque utilisateur porte le nombre de refus essuyés, les champs
    fautifs et l'horodatage de sa première observation : de quoi distinguer une
    ligne cassée d'hier d'une qui vient d'apparaître, et savoir qui réparer.
    """
    with _observation_lock:
        return {
            "total": _reference_unusable["total"],
            "users": {
                user_id: dict(info) for user_id, info in _reference_unusable["users"].items()
            },
        }


def reset_reference_unusable() -> None:
    """Remet le compteur à zéro — pour un test, ou après une réparation confirmée."""
    with _observation_lock:
        _reference_unusable["total"] = 0
        _reference_unusable["users"].clear()


def _observe_reference_unusable(user_id: str, decision: "RiskDecision") -> None:
    """Comptabilise le refus et le journalise, sans noyer les journaux.

    Le premier refus d'un utilisateur, et tout changement de ses champs fautifs,
    sortent en `WARNING` : c'est ce qu'un exploitant doit voir passer. Les
    répétitions descendent en `INFO` — elles comptent toujours, mais un utilisateur
    bloqué qui retente toutes les minutes ne doit pas remplir le journal à lui seul.
    """
    fields = list(decision.fields)
    with _observation_lock:
        _reference_unusable["total"] += 1
        info = _reference_unusable["users"].get(user_id)
        first = info is None
        if info is None:
            info = {
                "count": 0,
                "fields": fields,
                "since": datetime.now(timezone.utc).isoformat(),
            }
            _reference_unusable["users"][user_id] = info
        changed = info["fields"] != fields
        info["count"] += 1
        info["fields"] = fields
        occurrences = info["count"]
        total = _reference_unusable["total"]

    message = (
        f"{REFERENCE_UNUSABLE_LOG} utilisateur={user_id} champs={','.join(fields)} "
        f"occurrences={occurrences} total={total} — état de risque à réparer "
        "(valeur positive, ou ligne supprimée pour réinitialisation)"
    )
    if first or changed:
        logger.warning(message)
    else:
        logger.info(message)


def _unusable_references_reason(starting_balance: float, daily_start_balance: float) -> str:
    """Pourquoi les plafonds en pourcentage ne peuvent pas être mesurés — sinon `""`.

    Une référence présente mais non positive (0, négative) n'est pas une limite levée :
    c'est une mesure impossible. La réparer en douce serait pire — reprendre le solde
    courant comme référence efface la perte déjà subie, donc un compte en ruine
    retraderait. On refuse, en nommant le champ à corriger et le chemin de réparation.
    """
    broken = _unusable_reference_fields(starting_balance, daily_start_balance)
    if not broken:
        return ""
    return (
        f"Référence de solde inutilisable ({', '.join(broken)} ≤ 0) : les plafonds en "
        "pourcentage ne peuvent pas être mesurés. Corrige l'état de risque de cet "
        "utilisateur (une valeur positive), ou supprime la ligne — elle sera "
        "réinitialisée au prochain solde lu. Trade bloqué par précaution."
    )


def _get_or_init_state(user_id: str, balance: float) -> dict:
    res = supabase.table(TABLE).select("*").eq("user_id", user_id).limit(1).execute()
    if res and res.data:
        state = res.data[0]
        today = date.today().isoformat()
        if state.get("daily_date") != today:
            # nouveau jour -> on repart d'un compteur journalier frais
            supabase.table(TABLE).update({
                "daily_start_balance": balance,
                "daily_date": today,
            }).eq("user_id", user_id).execute()
            state["daily_start_balance"] = balance
            state["daily_date"] = today
        return state

    # première fois pour cet utilisateur -> initialisation
    new_state = {
        "user_id": user_id,
        "starting_balance": balance,
        "daily_start_balance": balance,
        "daily_date": date.today().isoformat(),
    }
    supabase.table(TABLE).insert(new_state).execute()
    return new_state


def _count_open_trades(user_id: str) -> int:
    res = (
        supabase.table("pending_signals")
        .select("id", count="exact")
        .eq("user_id", user_id)
        .eq("status", "executed")
        .execute()
    )
    return res.count or 0


@dataclass(frozen=True)
class RiskDecision:
    """Le verdict du garde-fou, **structuré** : une phrase, et les chiffres qui la portent.

    `reason` reste la phrase que l'utilisateur lit ; `code`, `limits`, `measured` et
    `fields` portent ce qu'une application affiche — où en est le compte, quel seuil
    est franchi, quel champ corriger — sans avoir à relire le texte. Les deux formes
    sortent du **même** calcul : impossible que le texte et les chiffres divergent.
    """

    allowed: bool
    code: str
    reason: str
    #: Les seuils configurés en jeu (`max_daily_loss_pct`, `max_total_drawdown_pct`,
    #: `max_open_trades`). Vide si les préférences n'ont pas pu être lues.
    limits: Dict[str, Any] = field(default_factory=dict)
    #: Les valeurs mesurées, `None` quand elles ne sont pas calculables à ce point du
    #: contrôle : `balance`, `starting_balance`, `daily_start_balance`,
    #: `daily_loss_pct`, `total_drawdown_pct`, `open_positions`.
    measured: Dict[str, Any] = field(default_factory=dict)
    #: Les champs fautifs à corriger (`balance`, `starting_balance`,
    #: `daily_start_balance`) — vide quand le refus ne désigne aucun champ.
    fields: Tuple[str, ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        """La forme transmissible : rien n'est perdu, rien n'est recalculé."""
        return {
            "allowed": self.allowed,
            "code": self.code,
            "reason": self.reason,
            "limits": dict(self.limits),
            "measured": dict(self.measured),
            "fields": list(self.fields),
        }


def evaluate(user_id: str, current_balance: float) -> RiskDecision:
    """Le verdict complet d'un ordre : autorisé, ou refusé **avec ses chiffres**.

    Même ordre de contrôle que `can_trade` — solde, références, positions, perte du
    jour, drawdown — mais chaque refus porte, à côté de sa phrase, les seuils en jeu
    et les valeurs mesurées. Une application peut alors afficher « 5,02 % de 5 % »
    plutôt que « limite de perte journalière atteinte ».
    """
    limits: Dict[str, Any] = {}
    measured: Dict[str, Any] = {
        "balance": float(current_balance),
        "starting_balance": None,
        "daily_start_balance": None,
        "daily_loss_pct": None,
        "total_drawdown_pct": None,
        "open_positions": None,
    }
    try:
        prefs = get_preferences(user_id)
        limits = {
            "max_daily_loss_pct": float(prefs.get("max_daily_loss_pct") or 5.0),
            "max_total_drawdown_pct": float(prefs.get("max_total_drawdown_pct") or 10.0),
            "max_open_trades": int(prefs.get("max_open_trades") or 3),
        }

        # Le solde courant est refusé **avant** d'écrire un état : une ligne créée
        # avec un solde nul naîtrait avec deux références à 0, donc deux gardes
        # muets pour toujours — c'est précisément le défaut qu'on corrige.
        if current_balance <= 0:
            return RiskDecision(
                False, CODE_BALANCE_UNREADABLE, NO_BALANCE_REASON, limits, measured, ("balance",)
            )

        state = _get_or_init_state(user_id, current_balance)
        starting_balance = _reference_balance(state, STARTING_FIELD, current_balance)
        daily_start_balance = _reference_balance(state, DAILY_FIELD, current_balance)
        measured["starting_balance"] = starting_balance
        measured["daily_start_balance"] = daily_start_balance

        # Plus de `if … > 0` : les gardes s'exécutent toujours, ou le trade est
        # refusé autrement. Un plafond en pourcentage qu'on saute en silence n'est
        # pas un plafond — c'est une limite que l'utilisateur croit avoir.
        unusable = _unusable_reference_fields(starting_balance, daily_start_balance)
        if unusable:
            decision = RiskDecision(
                False,
                CODE_REFERENCE_UNUSABLE,
                _unusable_references_reason(starting_balance, daily_start_balance),
                limits,
                measured,
                tuple(unusable),
            )
            # Le refus laisse une trace lisible par l'exploitant, pas seulement par
            # l'utilisateur : c'est ce qui distingue « bloqué » de « bloqué en silence ».
            _observe_reference_unusable(user_id, decision)
            return decision

        open_count = _count_open_trades(user_id)
        measured["open_positions"] = open_count
        if open_count >= limits["max_open_trades"]:
            return RiskDecision(
                False,
                CODE_MAX_OPEN_TRADES,
                f"Nombre max de positions ouvertes atteint ({limits['max_open_trades']}).",
                limits,
                measured,
            )

        daily_loss_pct = (daily_start_balance - current_balance) / daily_start_balance * 100
        measured["daily_loss_pct"] = daily_loss_pct
        if daily_loss_pct >= limits["max_daily_loss_pct"]:
            return RiskDecision(
                False,
                CODE_DAILY_LOSS,
                f"Limite de perte journalière atteinte "
                f"({daily_loss_pct:.2f}% ≥ {limits['max_daily_loss_pct']}%).",
                limits,
                measured,
            )

        total_dd_pct = (starting_balance - current_balance) / starting_balance * 100
        measured["total_drawdown_pct"] = total_dd_pct
        if total_dd_pct >= limits["max_total_drawdown_pct"]:
            return RiskDecision(
                False,
                CODE_TOTAL_DRAWDOWN,
                f"Plafond de drawdown total atteint "
                f"({total_dd_pct:.2f}% ≥ {limits['max_total_drawdown_pct']}%).",
                limits,
                measured,
            )

        return RiskDecision(True, CODE_ALLOWED, "", limits, measured)
    except Exception as e:
        # Fail-CLOSED volontaire : un garde-fou de risque qui laisse passer
        # en cas de doute perd tout son sens. Mieux vaut bloquer un trade
        # par précaution (l'utilisateur peut retenter) qu'ignorer un vrai
        # problème de calcul de risque.
        print(f"   ⚠️ risk_guard.can_trade error (fail-closed, trade bloqué): {type(e).__name__}: {e}")
        return RiskDecision(
            False,
            CODE_ERROR,
            "Vérification du risque indisponible temporairement — trade bloqué par précaution.",
            limits,
            measured,
        )


def can_trade(user_id: str, current_balance: float) -> Tuple[bool, str]:
    """Retourne (True, '') si l'utilisateur peut trader, sinon (False, raison).

    Enveloppe de `evaluate` : la signature historique est conservée pour les appelants
    qui ne veulent que le couple (autorisé, phrase). Le texte rendu est **exactement**
    celui d'avant — le structuré s'ajoute, il ne remplace rien.
    """
    decision = evaluate(user_id, current_balance)
    return decision.allowed, decision.reason
