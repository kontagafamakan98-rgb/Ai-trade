"""Sonde Supabase en **lecture seule**, à intervalle régulier, avec alerte au changement.

Le problème que ce module traite est un **silence**. La configuration Supabase peut
se casser — clé `anon` au lieu de `service_role`, migration non appliquée, table
renommée — sans que rien ne le dise : les symptômes sont indirects (aucun signal
ne remonte, une recherche ne trouve plus rien, un média n'est plus indexé) et on
cherche la cause ailleurs. `scripts/check_supabase.py` sait répondre, mais il faut
penser à le lancer, et personne ne pense à lancer une vérification qui marchait
hier.

Ce module la lance donc tout seul, en **lecture seule**, au rythme réglé
(`SUPABASE_WATCH_MINUTES`, surchargeable en base comme les autres veilles), et
n'alerte que sur un **changement de verdict** :

* une vérification qui passe du vert au rouge est une panne **à l'instant où elle
  arrive** — c'est ce qu'on veut apprendre, pas trois jours plus tard ;
* une vérification qui repasse au vert est annoncée aussi : sans ça, personne ne
  sait jamais si c'est réglé, et on finit par ne plus croire aux alertes ;
* l'état courant, lui, n'est **pas** réannoncé. Une base en panne alerterait à
  chaque passage, indéfiniment, et l'opérateur couperait la veille — le remède
  serait pire que le mal, puisqu'une alerte qui se répète sans rien dire de neuf
  est une alerte qu'on n'écoute plus.

Trois exceptions à « on ne parle qu'au changement », et aucune n'est un détail :

* **le premier passage** parle des échecs qu'il constate. Il n'y a rien à
  comparer, mais une base déjà cassée au démarrage ne doit pas se lire comme un
  silence rassurant : c'est précisément le cas qu'on veut voir ici ;
* une vérification **absente** d'un rapport garde son dernier verdict connu — son
  retour est donc comparé à ce qu'elle était, au lieu d'être pris pour une
  nouveauté (et donc pour un changement qui n'a pas eu lieu) ;
* une alerte qui **n'a pas pu partir** n'est pas comptée comme passée : le verdict
  n'est pas adopté, et le passage suivant redira le même changement. Un envoi
  refusé et une absence de changement ne doivent pas se ressembler, sinon le
  silence se lit comme une bonne nouvelle.

Ce que ce module ne fait **jamais** : écrire. Il n'appelle la sonde qu'avec
`roundtrip=False`, et c'est écrit **dans** `_read_only_probe` — pas reçu en
paramètre, donc pas contournable par distraction. Une veille périodique qui
écrirait dans la base de production toutes les cinq minutes serait exactement le
dégât qu'on veut éviter ; le seul mode qui écrit reste
`scripts/check_supabase.py --roundtrip`, lancé à la main, en le sachant.

Rien n'est persisté non plus : la mémoire des verdicts vit en processus. C'est
cohérent avec la promesse d'aucune écriture, et suffisant — ce qu'on cherche est
un changement observé **pendant que la veille tourne**. Un redémarrage repart d'un
constat, et le constat d'un échec est justement ce qu'on veut apprendre au
démarrage.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Sequence

from core import config_runtime
from notifications.notify import send_admin_message

#: Une vérification qui était verte est rouge : c'est l'alerte qui compte.
CHANGE_FAILED = "failed"
#: Une vérification qui était rouge est repassée au vert.
CHANGE_RECOVERED = "recovered"


def flatten_checks(report: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Toutes les vérifications d'un rapport, sections confondues.

    Les sections sont une **mise en page** (configuration, tables, facultatives,
    aller-retour) : ce qui se suit est le verdict d'une vérification, et son nom
    (`connexion`, `insights`, `environnement (TELEGRAM_CHANNELS)`…) est stable
    d'un passage à l'autre. C'est donc lui la clé de comparaison, et non sa place
    dans le rapport — une section qui s'ajoute ou disparaît ne doit pas faire
    croire que tout a changé.
    """
    return [
        check
        for section in (report or {}).get("sections") or []
        for check in section.get("checks") or []
    ]


class VerdictWatch:
    """Le dernier verdict **connu** de chaque vérification — et rien d'autre.

    Deux dictionnaires, et leur écart est le cœur du module : `observe` compare le
    passage courant aux verdicts **adoptés** et met le nouveau résultat de côté,
    `commit` l'adopte. Séparer les deux permet la seule conduite honnête quand
    l'alerte n'est pas partie : on n'adopte pas, donc le changement reste à
    annoncer.
    """

    def __init__(self) -> None:
        self._verdicts: Dict[str, bool] = {}
        self._pending: Dict[str, bool] = {}

    def verdicts(self) -> Dict[str, bool]:
        """Copie des verdicts adoptés — pour journaliser, pas pour décider."""
        return dict(self._verdicts)

    def observe(self, checks: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Enregistre le passage et rend **ce qui a changé**.

        Un changement est `{name, change, detail, first}` : `first` dit qu'il n'y
        avait rien à comparer — premier passage, ou vérification jamais vue. Un
        premier passage **vert** ne dit rien (il n'y a rien à raconter d'un état
        normal) ; un premier passage rouge, si : c'est le constat de départ.

        Une vérification sans nom est ignorée plutôt que suivie sous la clé
        `""` : toutes les entrées anonymes se confondraient, et un rapport qui
        n'en nomme aucune ne doit pas produire une alerte unique et trompeuse.
        """
        changes: List[Dict[str, Any]] = []
        pending: Dict[str, bool] = {}
        for check in checks or []:
            name = str(check.get("name") or "").strip()
            if not name:
                continue
            ok = bool(check.get("ok"))
            pending[name] = ok
            previous = self._verdicts.get(name)
            if previous is None:
                if not ok:
                    changes.append(self._change(name, check, CHANGE_FAILED, first=True))
            elif previous and not ok:
                changes.append(self._change(name, check, CHANGE_FAILED))
            elif not previous and ok:
                changes.append(self._change(name, check, CHANGE_RECOVERED))
        self._pending = pending
        return changes

    def commit(self) -> None:
        """Adopte le passage observé comme dernier verdict connu.

        À appeler quand il n'y avait rien à annoncer — la base doit avancer, sinon
        le changement se redirait à chaque passage — et **après** un envoi réussi.
        Pas après un envoi refusé : c'est ce qui transforme un échec de livraison
        en nouvelle tentative au passage suivant.

        Les vérifications **absentes** de ce passage gardent leur verdict :
        remplacer la mémoire par le seul passage courant ferait dire « constat du
        premier passage » au retour d'une vérification dont on connaissait très
        bien l'état d'avant — c'est-à-dire annoncer une découverte là où il y a
        une transition, ou l'inverse selon le sens du changement.
        """
        merged = dict(self._verdicts)
        merged.update(self._pending)
        self._verdicts = merged

    def discard(self) -> None:
        """Oublie le passage observé : la sonde a échoué, on ne sait rien.

        Sans cela, un `commit` ultérieur adopterait l'état d'un passage dont on
        n'a rien pu lire — et ferait croire qu'on a observé quelque chose.
        """
        self._pending = {}

    @staticmethod
    def _change(
        name: str, check: Dict[str, Any], change: str, *, first: bool = False
    ) -> Dict[str, Any]:
        return {
            "name": name,
            "change": change,
            "first": bool(first),
            "detail": str(check.get("detail") or ""),
        }


def format_alert(changes: Sequence[Dict[str, Any]]) -> str:
    """Le message d'alerte : ce qui a bougé, et quoi en faire.

    Pas de balisage Markdown, pas d'accent grave : le bot envoie ses messages sans
    `parse_mode`, donc les astérisques et les backticks s'afficheraient tels quels.
    Les échecs viennent **avant** les rétablissements : c'est ce qu'on lit en
    premier quand les deux arrivent dans le même passage.
    """
    failed = [change for change in changes if change.get("change") == CHANGE_FAILED]
    recovered = [change for change in changes if change.get("change") == CHANGE_RECOVERED]
    lines: List[str] = []
    if failed:
        lines.append(f"❌ Sonde Supabase — {len(failed)} vérification(s) en échec :")
        lines.append("")
        lines.extend(f"• {change['name']} — {change['detail']}" for change in failed)
        if any(change.get("first") for change in failed):
            lines.append("")
            lines.append(
                "Constat du premier passage : ces échecs existaient déjà avant la "
                "veille, ils ne viennent pas d'apparaître."
            )
    if recovered:
        if lines:
            lines.append("")
        lines.append(
            f"✅ Rétabli — {len(recovered)} vérification(s) repassée(s) au vert :"
        )
        lines.append("")
        lines.extend(f"• {change['name']} — {change['detail']}" for change in recovered)
    if not lines:
        # Inatteignable depuis `watch_once`, qui n'alerte que sur des changements :
        # mieux vaut une phrase neutre qu'un message réduit à sa signature.
        return "ℹ️ Sonde Supabase — rien de changé."
    lines.append("")
    lines.append(
        "La veille lit sans jamais écrire (roundtrip désactivé). Détail complet : "
        "scripts/check_supabase.py, et --roundtrip à la main pour éprouver les "
        "écritures."
    )
    return "\n".join(lines)


def _read_only_probe(*, client: Any = None) -> Dict[str, Any]:
    """`check_supabase.run` en **lecture seule** — le seul mode appelé ici.

    L'import est **local** : la veille doit s'importer sans la pile de la sonde
    (ni `supabase`, ni `dotenv`), comme le reste des workers testables. Et
    `roundtrip=False` est écrit **ici** plutôt que reçu : c'est la garantie du
    module, elle ne doit pas pouvoir être perdue par distraction chez un appelant.
    """
    from scripts import check_supabase

    return check_supabase.run(roundtrip=False, as_json=False, client=client)


async def watch_once(
    *,
    watch: Optional[VerdictWatch] = None,
    probe: Any = None,
    notify: Any = None,
    chat_id: Optional[str] = None,
    config: Any = None,
    client: Any = None,
) -> Dict[str, Any]:
    """Un passage : lire, comparer, alerter si un verdict a bougé.

    Ne lève **jamais** — une veille qui tombe emporterait le cycle qui l'appelle,
    et le silence qui suivrait serait exactement ce que ce module existe pour
    éviter. Une sonde en échec est un résultat (`reason="probe_failed"`), et rien
    n'est adopté dans ce cas : on ne sait rien de l'état, donc on ne peut pas dire
    qu'une vérification a changé.

    `probe`, `notify` et `client` sont injectables : c'est ce qui rend la veille
    testable sans base ni Telegram.

    Retourne `{ok, reason, error, changes, alerted, checked, failing}` — `checked`
    et `failing` servent la ligne de journal de la boucle, pas la décision.
    """
    watched = watch if watch is not None else VerdictWatch()
    probe = probe or _read_only_probe
    notify = notify or send_admin_message
    config = config or config_runtime.get_env_config()
    if chat_id is None:
        chat_id = config.telegram_admin_chat_id

    try:
        # La sonde est **bloquante** (réseau, lectures base) : hors de l'event loop.
        report = await asyncio.to_thread(probe, client=client)
    except Exception as exc:
        watched.discard()
        print(f"   [veille supabase] sonde impossible : {type(exc).__name__}: {exc}")
        return {
            "ok": False,
            "reason": "probe_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "changes": [],
            "alerted": False,
            "checked": 0,
            "failing": 0,
        }

    checks = flatten_checks(report)
    changes = watched.observe(checks)
    result: Dict[str, Any] = {
        "ok": True,
        "reason": None,
        "error": None,
        "changes": changes,
        "alerted": False,
        "checked": len(checks),
        "failing": sum(1 for check in checks if not check.get("ok")),
    }
    if not changes:
        # Rien à annoncer : la base avance, sinon ce passage serait recomparé
        # indéfiniment au précédent.
        watched.commit()
        return result

    if not chat_id:
        # Le dire est le minimum : sans ce message, l'alerte serait perdue et rien
        # ne l'expliquerait. Le verdict n'est **pas** adopté — si le chat est
        # configuré plus tard, ce changement partira enfin.
        print(
            "   [veille supabase] aucune alerte envoyée : TELEGRAM_ADMIN_CHAT_ID "
            "n'est pas renseigné"
        )
        result["reason"] = "no_admin_chat"
        return result

    try:
        await notify(chat_id, format_alert(changes))
    except Exception as exc:
        print(f"   [veille supabase] alerte non envoyée : {type(exc).__name__}: {exc}")
        result["reason"] = "send_failed"
        return result

    watched.commit()
    result["alerted"] = True
    return result


__all__ = [
    "CHANGE_FAILED",
    "CHANGE_RECOVERED",
    "VerdictWatch",
    "flatten_checks",
    "format_alert",
    "watch_once",
]
