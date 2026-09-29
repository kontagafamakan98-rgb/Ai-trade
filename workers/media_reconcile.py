"""Veille de la réconciliation média, planifiée par l'auto-loop.

Un écart entre le bucket et `knowledge_media` ne se voit que si quelqu'un le
regarde : ni l'application ni le bot ne s'en aperçoivent. Cette veille le regarde
à intervalle régulier — `media_store.reconcile_bucket`, un **seul** parcours pour
les deux sens — et alerte sur Telegram au-delà d'un seuil.

Trois propriétés, et chacune est un choix :

* **jamais de suppression automatique.** Un orphelin est un objet sans ligne, et
  rien ne prouve qu'il est inutile : upload interrompu en cours de route, ligne
  supprimée par erreur, import en cours. Les octets se retirent depuis un shell ou
  la console Supabase, où la commande laisse une trace
  (`scripts/reconcile_media.py --delete`). Une veille qui supprime est une veille
  qui peut détruire la seule copie d'un média sur un faux positif de parcours ;
* **un objet manquant alerte dès le premier, un orphelin seulement au-delà du
  seuil.** Le premier cas casse l'application — liens signés en 404, tableau de
  bord qui pointe dans le vide — tandis que le second est banal (un upload
  interrompu) et ne devient un signal que par son nombre ;
* **la même alerte ne se répète pas à chaque cycle.** L'état est gardé en
  mémoire : on parle à l'apparition, on se tait tant que c'est aussi mauvais,
  on reparle si **ça empire**, et on annonce le **retour à la normale** — sans
  quoi personne ne saurait jamais si c'est réglé. Une alerte qui n'a pas pu
  partir (chat admin absent, Telegram en panne) n'est **pas** notée comme
  envoyée : le cycle suivant doit pouvoir réessayer, sinon la veille se tairait
  sur une anomalie que personne n'a vue.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from core import config_runtime
from database import media_store
from notifications.notify import send_admin_message

#: Première alerte : l'anomalie est apparue (ou venait d'être aggravée ailleurs).
ACTION_ALERT = "alert"
#: L'anomalie a empiré depuis la dernière alerte : on reparle tout de suite.
ACTION_WORSENED = "worsened"
#: Retour sous les seuils : on l'annonce, en une fois.
ACTION_RECOVERED = "recovered"
#: Rien à dire pour ce cycle.
ACTION_SILENT = "silent"

#: Détails montrés par section dans un message (chemins ou médias). Un message
#: Telegram en porte 4096 : au-delà de quelques lignes, la liste noie le signal.
ALERT_DETAIL_LINES = 5


def _above_threshold(orphans: int, missing: int, threshold: int) -> bool:
    """Faut-il alerter ? Un média manquant suffit, les orphelins se comptent.

    Le seuil ne porte donc **que** sur les orphelins : l'abaisser à zéro alerte
    dès le premier, et il n'existe aucune valeur qui rende un média manquant
    silencieux — ce ne serait pas un réglage, ce serait une panne qu'on décide de
    ne pas voir.
    """
    return missing > 0 or orphans > threshold


class ReconcileWatch:
    """L'état entre deux cycles : faut-il parler, et de quoi ?

    La décision (`observe`) est **séparée** de la mémoire (`sent`) : une alerte
    qui n'est pas partie ne doit rien mémoriser, puisque personne ne l'a lue.
    C'est ce qui rend l'état testable sans réseau, et le cycle rejouable.
    """

    def __init__(self, threshold: int) -> None:
        self._threshold = max(0, int(threshold))
        self._alerted = False
        self._orphans = 0
        self._missing = 0

    @property
    def threshold(self) -> int:
        return self._threshold

    @property
    def alerted(self) -> bool:
        """Vrai si une anomalie a été signalée et ne l'est pas encore démentie."""
        return self._alerted

    @property
    def last_counts(self) -> Any:
        return (self._orphans, self._missing)

    def observe(self, *, orphans: int, missing: int) -> str:
        """Ce qu'il y a à dire pour ce cycle, sans rien mémoriser."""
        if _above_threshold(orphans, missing, self._threshold):
            if not self._alerted:
                return ACTION_ALERT
            if orphans > self._orphans or missing > self._missing:
                return ACTION_WORSENED
            return ACTION_SILENT
        return ACTION_RECOVERED if self._alerted else ACTION_SILENT

    def sent(self, action: str, *, orphans: int, missing: int) -> None:
        """Mémorise ce qui vient d'être envoyé — ou ce qu'on choisit de taire."""
        if action in (ACTION_ALERT, ACTION_WORSENED):
            self._alerted = True
            self._orphans = orphans
            self._missing = missing
        elif action == ACTION_RECOVERED:
            self._alerted = False
            self._orphans = 0
            self._missing = 0


def _bullets(items: List[str], lines: int = ALERT_DETAIL_LINES) -> List[str]:
    """Quelques détails, puis le reste en un compte — jamais une troncature muette."""
    shown = [f"   • {item}" for item in items[:lines]]
    rest = len(items) - len(shown)
    if rest > 0:
        shown.append(f"   • … et {rest} autre(s)")
    return shown


def format_alert(
    *,
    orphans: List[str],
    missing: List[Dict[str, Any]],
    threshold: int,
    action: str = ACTION_ALERT,
    previous: Any = None,
) -> str:
    """Le message d'alerte : ce qui est constaté, et où agir.

    Les deux sens sont annoncés séparément parce qu'ils ne se réparent pas de la
    même façon : un orphelin se **retire** (après vérification), un média manquant
    se **re-télécharge** (`POST /admin/media/missing/repair`).
    """
    header = (
        "🚨 Réconciliation média — anomalie détectée"
        if action != ACTION_WORSENED
        else "⚠️ Réconciliation média — ça empire"
    )
    lines = [header, ""]

    if orphans:
        since = ""
        # « Contre N » seulement si **ce** compte a monté : l'annoncer alors qu'il
        # n'a pas bougé ferait croire à une aggravation qui n'a pas eu lieu (c'est
        # l'autre sens qui a empiré).
        if action == ACTION_WORSENED and previous and len(orphans) > previous[0]:
            since = f", contre {previous[0]} à la dernière alerte"
        lines.append(
            f"🗑️ {len(orphans)} objet(s) sans ligne `knowledge_media` "
            f"(seuil {threshold}{since}) — du stockage sans référence :"
        )
        lines.extend(_bullets(orphans))
        lines.append("")

    if missing:
        since = ""
        if action == ACTION_WORSENED and previous and len(missing) > previous[1]:
            since = f", contre {previous[1]} à la dernière alerte"
        lines.append(
            f"🗂️ {len(missing)} média(s) décrit(s) dont l'objet a disparu du bucket"
            f"{since} — l'application pointe dans le vide :"
        )
        lines.extend(
            _bullets(
                [
                    f"{row.get('storage_path')}"
                    + ("" if row.get("telegram_file_id") else " (sans `file_id`)")
                    for row in missing
                ]
            )
        )
        lines.append("")

    lines.append("Aucune suppression automatique :")
    lines.append("   • retirer les orphelins : python scripts/reconcile_media.py --delete")
    lines.append("   • réparer les manquants : POST /admin/media/missing/repair")
    return "\n".join(lines)


def format_recovery(*, orphans: int, missing: int, threshold: int) -> str:
    """Le retour à la normale — sans lui, une alerte ne se referme jamais."""
    return (
        "✅ Réconciliation média — retour à la normale\n\n"
        f"🗑️ {orphans} objet(s) sans ligne (seuil {threshold})\n"
        f"🗂️ {missing} média(s) dont l'objet manque\n\n"
        "Le bucket et `knowledge_media` concordent à nouveau."
    )


async def reconcile_once(
    *,
    store: Any = None,
    notify: Any = None,
    watch: Optional[ReconcileWatch] = None,
    threshold: Optional[int] = None,
    chat_id: Optional[str] = None,
    config: Any = None,
) -> Dict[str, Any]:
    """Un cycle : constater, décider, alerter si nécessaire. **Ne supprime rien.**

    Ne lève **jamais** : une veille qui tombe emporterait le cycle avec elle, et
    l'auto-loop avec. Un parcours impossible est rapporté (`ok: False`) et le
    cycle suivant réessaiera.

    Collaborateurs injectables (store, notify, watch, config) pour être testable
    sans Supabase, sans Telegram et sans horloge.
    """
    store = store or media_store.reconcile_bucket
    notify = notify or send_admin_message
    config = config or config_runtime.get_env_config()
    if threshold is None:
        threshold = config.media_orphan_alert_threshold
    if chat_id is None:
        chat_id = config.telegram_admin_chat_id
    watch = watch or ReconcileWatch(threshold)

    try:
        report = await asyncio.to_thread(store, "")
    except Exception as exc:
        print(f"   [réconciliation] parcours impossible : {type(exc).__name__}: {exc}")
        return {
            "ok": False,
            "action": None,
            "alerted": False,
            "reason": "scan_failed",
            "orphans": None,
            "missing": None,
        }

    orphans = list(report.get("orphans") or [])
    missing = list(report.get("missing") or [])
    action = watch.observe(orphans=len(orphans), missing=len(missing))
    print(
        f"   [réconciliation] {len(orphans)} orphelin(s), {len(missing)} manquant(s) "
        f"(seuil {threshold}) — {action}"
    )

    result: Dict[str, Any] = {
        "ok": True,
        "action": action,
        "alerted": False,
        "reason": None,
        "orphans": len(orphans),
        "missing": len(missing),
    }
    if action == ACTION_SILENT:
        return result

    if not chat_id:
        # Le dire est le minimum : sans ce message, aucune alerte n'arriverait et
        # rien ne l'expliquerait.
        print(
            "   [réconciliation] aucune alerte envoyée : TELEGRAM_ADMIN_CHAT_ID "
            "n'est pas renseigné"
        )
        result["reason"] = "no_admin_chat"
        return result

    if action == ACTION_RECOVERED:
        text = format_recovery(
            orphans=len(orphans), missing=len(missing), threshold=threshold
        )
    else:
        text = format_alert(
            orphans=orphans,
            missing=missing,
            threshold=threshold,
            action=action,
            previous=watch.last_counts,
        )

    try:
        await notify(chat_id, text)
    except Exception as exc:
        # On ne mémorise **pas** : l'anomalie n'a été vue par personne.
        print(f"   [réconciliation] alerte non envoyée : {type(exc).__name__}: {exc}")
        result["reason"] = "send_failed"
        return result

    watch.sent(action, orphans=len(orphans), missing=len(missing))
    result["alerted"] = True
    return result
