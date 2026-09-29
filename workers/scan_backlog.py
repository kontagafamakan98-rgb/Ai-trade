"""Report d'extractions : quand il se répète, ce n'est plus du retard.

Le plafond d'extractions par canal et par balayage (`max_extractions`) existe pour
qu'un album de vingt photos ne consomme pas le quota de vision d'un après-midi sur
un seul passage. Le surplus est **reporté**, et c'est normal : le balayage suivant
redonne les mêmes publications, celles qui sont déjà indexées en sont écartées
sans rien coûter, et le rattrapage avance tout seul. Un report isolé n'est donc
pas un symptôme — c'est le mécanisme qui fonctionne.

Ce qui en est un, c'est un canal qui reporte à **chaque balayage d'affilée**. Le
report ne se résorbe alors pas, et il n'y a que deux causes : le canal publie plus
vite que le budget du balayage, ou une extraction échoue à chaque passage (une
extraction réussie n'est plus reproposée, donc ce qui revient sans cesse n'a
jamais abouti). Dans les deux cas, personne ne l'apprend : le compte est
journalisé à chaque balayage (`N reporté(s) au prochain balayage`), et une ligne
parmi des centaines se lit comme la normale.

Il y a pire qu'un report qui ne se résorbe pas : un balayage qui **n'a rien lu**.
L'aperçu `t.me/s` du canal est injoignable (DNS, 503, page d'un format inattendu),
l'exception est avalée par le scraper — il ne doit pas emporter le cycle — et la
ligne de journal ressemble alors à celle d'un canal calme : aucun message, aucun
média, aucune erreur. Le canal peut publier toute la journée, rien n'arrive, et
personne ne le sait. C'est le même angle mort que le report, avec un symptôme
inverse : là où le report dit trop, l'échec ne dit rien du tout.

D'où ce module, calqué sur la veille média (`workers/media_reconcile.py`) :

* il suit, **par canal**, le nombre de balayages consécutifs qui reportent — une
  suite d'observations, qu'aucun compteur d'un seul passage ne peut porter — et,
  séparément, le nombre de balayages consécutifs **sans lecture** : les deux suites
  ne se comptent pas ensemble, un échec ne mesurant rien du report ;
* il alerte le chat d'exploitation (`TELEGRAM_ADMIN_CHAT_ID`) à partir du seuil,
  reparle si **ça empire**, et rappelle périodiquement un blocage qui ne bouge pas
  — un rappel, parce qu'une alerte unique peut se perdre dans une nuit ;
* il annonce le **retour à la normale**, sans quoi personne ne saurait jamais si
  c'est réglé — pour les deux familles : le report résorbé, et la lecture qui
  repart ;
* il **finit par renoncer** sur une lecture qui ne revient pas : les rappels d'un
  canal illisible s'espacent de plus en plus loin, puis s'arrêtent sur un dernier
  message. Un canal mort ne se répare pas en le redisant — un message lu dix fois
  se lit comme du bruit, et une alerte qui ne s'arrête jamais use la seule chose
  qui compte ici : qu'on la lise. L'abandon ne survit pas à une lecture qui
  aboutit, et cette lecture-là est annoncée ;
* une alerte qui n'a pas pu partir (chat absent, Telegram en panne) n'est **pas**
  comptée comme envoyée : le balayage suivant doit pouvoir réessayer, sinon le
  silence passerait pour une accalmie.

Ce que ce module ne fait pas : il ne modifie aucun réglage. Relever le plafond
allège la file d'attente mais laisse le problème intact si une extraction échoue
en boucle, et le faire tout seul fermerait les yeux sur la cause. Le message dit
les deux pistes ; la décision reste à l'opérateur.

Renoncer n'est pas décider à sa place : un canal abandonné reste balayé, et rien
n'est retiré de la liste. Ce qui s'arrête est le **bruit**, pas la veille — et
l'abandon tombe de lui-même à la première lecture qui aboutit.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

from core import config_runtime
from notifications.notify import send_admin_message

#: Première alerte : le canal reporte depuis assez de balayages consécutifs pour
#: que ce ne soit plus la file d'attente, mais un blocage.
ACTION_BLOCKED = "blocked"
#: Le report a dépassé son plus haut niveau connu : on reparle tout de suite.
ACTION_WORSENED = "worsened"
#: Toujours bloqué, sans changement : un rappel, à intervalle large.
ACTION_REMINDER = "reminder"
#: Le report est retombé à zéro : on ferme l'alerte, en une fois.
ACTION_RESOLVED = "resolved"
#: Le balayage n'a **rien lu** depuis assez de balayages consécutifs : le canal
#: n'est pas calme, il est illisible. Le motif est celui du scraper, nommé.
ACTION_SCAN_FAILING = "scan_failing"
#: Toujours sans lecture, sans changement : un rappel, à intervalle large.
ACTION_SCAN_REMINDER = "scan_reminder"
#: Le balayage lit de nouveau : on ferme l'alerte, en une fois.
ACTION_SCAN_RECOVERED = "scan_recovered"
#: Les rappels espacés n'ont rien donné : le canal est tenu pour mort, on le dit
#: **une dernière fois**, et on se tait jusqu'à ce qu'il se remette à lire.
ACTION_SCAN_ABANDONED = "scan_abandoned"
#: Une lecture qui aboutit **après** un abandon : le silence n'a jamais été une
#: condamnation, et le taire laisserait l'opérateur croire qu'on a renoncé pour de bon.
ACTION_SCAN_REVIVED = "scan_revived"
#: Rien à dire pour ce balayage.
ACTION_SILENT = "silent"

#: Les actions qui se comptent sur la suite d'**échecs** : elles parlent de la
#: lecture du canal, là où les autres parlent du report d'extractions.
_SCAN_ACTIONS = (
    ACTION_SCAN_FAILING,
    ACTION_SCAN_REMINDER,
    ACTION_SCAN_ABANDONED,
    ACTION_SCAN_REVIVED,
)
#: Celles qui **ouvrent** une alerte d'échec (la reprise, la relance et l'abandon,
#: eux, la ferment).
_SCAN_SIGNALLED = (ACTION_SCAN_FAILING, ACTION_SCAN_REMINDER)

#: Combien de balayages **consécutifs** avec report avant de parler. « Plusieurs »
#: et non « un » : le premier report est la preuve que le plafond travaille. Trois
#: laisse deux balayages au rattrapage pour se résorber, ce qui est le cas normal
#: quand un canal publie par salves.
DEFAULT_CONSECUTIVE_SWEEPS = 3

#: Balayages supplémentaires entre deux rappels d'un blocage inchangé. Un canal qui
#: publie plus vite que son budget reste bloqué indéfiniment : le dire une fois ne
#: suffit pas, le dire à chaque balayage serait du bruit.
REMINDER_EVERY = 10

#: Combien de rappels **espacés** avant de tenir la lecture d'un canal pour morte.
#: Les rappels s'élargissent (`REMINDER_EVERY`, puis le double, puis le quadruple…)
#: et s'arrêtent ici, sur un dernier message. Un canal injoignable depuis des jours
#: ne se répare pas en le redisant, et une alerte qui revient sans fin use la seule
#: chose qui compte — qu'on la lise. Trois rappels à 10, 20 puis 40 balayages, plus
#: les 80 du dernier intervalle, portent l'épreuve à 153 balayages sans lecture :
#: cinq messages en tout, puis le silence — trois jours à la période par défaut
#: (30 minutes), là où une cadence fixe en aurait envoyé une quinzaine d'autres.
ABANDON_AFTER_REMINDERS = 3


@dataclass
class _State:
    """Ce qu'un canal a fait depuis le début de sa séquence de reports."""

    consecutive: int = 0
    #: Plus haut nombre de reportées vu dans la séquence — la référence pour
    #: décider qu'« ça empire ».
    peak: int = 0
    alerted: bool = False
    #: Le pic au moment de la **dernière** alerte : comparer à `peak` sans cela
    #: rendrait toute aggravation invisible après le premier message.
    alerted_peak: int = 0
    sweeps_since_alert: int = 0
    #: La séquence est finie, mais sa résolution n'a pas encore pu être annoncée.
    #: La durée reste donc lisible pour le message — alors qu'un nouveau report
    #: doit déjà repartir de un, et non prolonger une séquence terminée.
    closed: bool = False
    #: Balayages d'affilée **sans lecture**. Un échec n'est pas une quantité : il
    #: n'y a pas de « pire » à comparer, seulement une suite qui s'allonge.
    failures: int = 0
    #: Rappels d'échec depuis la dernière alerte.
    failed_since_alert: int = 0
    #: Une alerte d'**échec** court (l'alerte de report a son propre drapeau : les
    #: deux peuvent être ouvertes en même temps sur le même canal).
    failure_alerted: bool = False
    #: Rappels d'échec **envoyés** depuis l'ouverture de l'alerte : c'est ce compte
    #: qui élargit la cadence, et lui seul qui autorise l'abandon. Il ne compte que
    #: ce qui est parti — un rappel que Telegram a refusé n'écourte pas l'épreuve.
    failure_reminders: int = 0
    #: La lecture du canal a été **abandonnée** : on ne dit plus rien d'elle. Les
    #: échecs continuent de s'allonger — c'est la durée vraie, celle qu'annoncera une
    #: reprise — mais aucun message n'en part.
    abandoned: bool = False
    #: La suite d'échecs est finie, mais sa reprise n'a pas encore pu être annoncée.
    failure_closed: bool = False


class ScanBacklog:
    """L'état entre deux balayages : qui reporte, depuis combien de temps, et si on l'a dit.

    La **décision** (`observe`) est séparée de la **mémoire de l'envoi**
    (`recorded`) : une alerte qui n'est pas partie ne doit rien mémoriser, puisque
    personne ne l'a lue. Mais l'observation, elle, est notée dans `observe` — le
    fait qu'un canal ait reporté ne dépend pas de ce que le bot a réussi à envoyer.
    C'est cette séparation qui rend l'ensemble testable sans réseau.
    """

    def __init__(
        self,
        *,
        threshold: int = DEFAULT_CONSECUTIVE_SWEEPS,
        reminder_every: int = REMINDER_EVERY,
        abandon_after_reminders: int = ABANDON_AFTER_REMINDERS,
    ) -> None:
        self._threshold = max(2, int(threshold))
        self._reminder_every = max(1, int(reminder_every))
        self._abandon_after = max(1, int(abandon_after_reminders))
        self._states: Dict[str, _State] = {}

    @property
    def threshold(self) -> int:
        return self._threshold

    @property
    def reminder_every(self) -> int:
        return self._reminder_every

    @property
    def abandon_after_reminders(self) -> int:
        return self._abandon_after

    @property
    def alerted(self) -> List[str]:
        """Les canaux actuellement signalés, dans l'ordre du nom.

        Les deux familles y sont : c'est la question que se pose un opérateur
        (« qui est signalé ? »), et un canal peut mener les deux alertes de front.
        Qui veut la seule famille de l'échec lit `failing`.
        """
        return sorted(
            name
            for name, state in self._states.items()
            if state.alerted or state.failure_alerted
        )

    @property
    def failing(self) -> List[str]:
        """Ceux dont la **lecture** échoue : un sous-ensemble d'`alerted`."""
        return sorted(
            name for name, state in self._states.items() if state.failure_alerted
        )

    @property
    def abandoned(self) -> List[str]:
        """Ceux dont la lecture a été **abandonnée** : plus signalés, mais toujours suivis.

        C'est l'inverse d'un sous-ensemble d'`alerted` : ces canaux en sont sortis
        au moment de l'abandon, et c'est tout l'objet du mécanisme. Les nommer
        répond à « sur quoi le bot a-t-il renoncé ? », une question qu'un silence
        ne peut pas porter.
        """
        return sorted(
            name for name, state in self._states.items() if state.abandoned
        )

    def consecutive(self, channel: str) -> int:
        """Balayages d'affilée qui reportent — la durée du blocage en cours."""
        state = self._states.get(channel)
        return state.consecutive if state else 0

    def failures(self, channel: str) -> int:
        """Balayages d'affilée **sans pouvoir lire** l'aperçu du canal."""
        state = self._states.get(channel)
        return state.failures if state else 0

    def peak(self, channel: str) -> int:
        """Le plus grand nombre de reportées observé dans la séquence en cours."""
        state = self._states.get(channel)
        return state.peak if state else 0

    def observe(
        self,
        channel: str,
        *,
        deferred: Optional[int] = None,
        error: Optional[str] = None,
    ) -> str:
        """Ce qu'il y a à dire pour ce balayage, en notant l'observation.

        Trois états, deux suites. Un **report** mesuré (`deferred`) alimente la
        suite de reports ; un **échec de lecture** (`error`, le motif nommé par le
        scraper) alimente la suite d'échecs. Les deux ne se comptent pas ensemble :
        un balayage qui n'a rien lu ne dit rien du report, et compter son échec
        comme une résolution ferait taire l'alerte d'un canal dont la lecture vient
        justement de casser.

        Ni l'un ni l'autre (`deferred=None` sans `error`, et c'est ce que
        l'appelant passe) ne touche à aucune des deux suites.
        """
        state = self._states.setdefault(channel, _State())
        if error is not None:
            return self._failing(state) or ACTION_SILENT
        if deferred is None:
            return ACTION_SILENT
        # Un balayage qui a lu laisse tomber la suite d'échecs : c'est exactement ce
        # que l'échec constatait. Sa reprise, quand une alerte était ouverte, prime
        # sur ce que le report aurait à dire — un message par canal et par balayage,
        # et l'alerte de report se redira au balayage suivant si elle tient encore.
        readable = self._readable(state)
        reported = self._reported(state, max(0, int(deferred)))
        return readable or reported or ACTION_SILENT

    def _reported(self, state: _State, count: int) -> Optional[str]:
        """Le report mesuré de ce balayage : décision et avancement de sa suite."""
        if count <= 0:
            if not state.alerted:
                # Aucune alerte ouverte : il n'y a rien à annoncer, et la séquence
                # est soldée sur-le-champ.
                state.consecutive = 0
                state.peak = 0
                return ACTION_SILENT
            # Une alerte est ouverte : on annonce la fin. La durée du blocage
            # reste lisible jusqu'à ce que le message soit enregistré — une
            # résolution qui n'a pas pu partir doit pouvoir être redite telle
            # quelle au balayage suivant.
            state.closed = True
            return ACTION_RESOLVED

        if state.closed:
            # La séquence précédente est terminée : celle-ci commence, même si sa
            # résolution n'a pas encore pu être annoncée.
            #
            # `alerted` et `alerted_peak` **survivent** : l'alerte a été lue (c'est
            # ce que `alerted` veut dire), et elle décrit toujours l'état du canal.
            # Les effacer ici rendrait toute nouvelle valeur « pire que la
            # précédente » — un report de 4 après un pic de 5 serait annoncé comme
            # une aggravation, alors qu'il en dit moins.
            state.closed = False
            state.consecutive = 0
            state.peak = 0

        state.consecutive += 1
        state.peak = max(state.peak, count)
        if state.alerted:
            state.sweeps_since_alert += 1
        elif state.consecutive < self._threshold:
            return ACTION_SILENT

        if not state.alerted:
            return ACTION_BLOCKED
        if state.peak > state.alerted_peak:
            return ACTION_WORSENED
        if state.sweeps_since_alert >= self._reminder_every:
            return ACTION_REMINDER
        return None

    def _failing(self, state: _State) -> Optional[str]:
        """Le balayage n'a rien pu lire : décision et avancement de sa suite.

        Le seuil est celui du report, et pour la même raison : un échec isolé peut
        être un hoquet réseau (`t.me/s` limite les requêtes), c'est la répétition
        qui fait le symptôme. Ce qui distingue les deux familles, c'est ce qu'on en
        dit : ici il n'y a aucun compte à annoncer, seulement une lecture qui ne
        revient pas et un motif à nommer.

        Et une épreuve qui **se conclut** : les rappels s'espacent (le double à
        chaque fois) puis s'arrêtent sur l'abandon. La cadence de **report**, elle,
        reste fixe : un canal qui publie trop vite est vivant, et son blocage ne se
        résorbera pas parce qu'on a cessé d'en parler.
        """
        if state.failure_closed:
            # La suite précédente est terminée : celle-ci commence, même si sa
            # reprise n'a pas encore pu être annoncée — sinon la durée annoncée
            # prolongerait des échecs qui ne sont pas de la même panne.
            state.failure_closed = False
            state.failures = 0
        state.failures += 1
        if state.abandoned:
            # On s'est tu sur ce canal. La suite continue de s'allonger — c'est la
            # durée vraie, celle qu'annoncera une reprise — mais plus rien n'en part.
            return None
        if state.failure_alerted:
            state.failed_since_alert += 1
            if state.failed_since_alert < self._reminder_interval(state.failure_reminders):
                return None
            if state.failure_reminders >= self._abandon_after:
                return ACTION_SCAN_ABANDONED
            return ACTION_SCAN_REMINDER
        if state.failures < self._threshold:
            return None
        return ACTION_SCAN_FAILING

    def _reminder_interval(self, sent: int) -> int:
        """Combien de balayages attendre avant le prochain rappel d'échec.

        La cadence **s'élargit** à chaque rappel parti : `reminder_every`, puis le
        double, puis le quadruple… Un canal mort ne dit pas plus au dixième jour
        qu'au premier ; c'est cet espacement qui transforme une alerte qui ne
        s'arrête jamais en une épreuve qui se conclut.
        """
        return self._reminder_every * (2 ** max(0, int(sent)))

    def _readable(self, state: _State) -> Optional[str]:
        """Le balayage a lu : la suite d'échecs est finie, et on le dit une fois.

        Après un **abandon**, c'est la seule chose qui remet la veille en marche :
        l'abandon ne survit pas à une lecture, sinon un canal muet une semaine et
        revenu le lendemain resterait tu pour toujours.
        """
        if state.abandoned:
            return ACTION_SCAN_REVIVED
        if not state.failure_alerted:
            # Rien n'avait été signalé : il n'y a rien à annoncer, et la suite est
            # soldée sur-le-champ.
            state.failures = 0
            state.failure_closed = False
            return None
        # Une alerte est ouverte : on annonce la reprise. La durée reste lisible
        # jusqu'à ce que le message soit enregistré — une reprise qui n'a pas pu
        # partir doit pouvoir être redite telle quelle au balayage suivant.
        state.failure_closed = True
        return ACTION_SCAN_RECOVERED

    def recorded(self, channel: str, action: str) -> None:
        """Mémorise ce qui vient d'être **envoyé** — jamais ce qu'on a renoncé à dire."""
        state = self._states.get(channel)
        if state is None:
            return
        if action in (ACTION_BLOCKED, ACTION_WORSENED, ACTION_REMINDER):
            state.alerted = True
            state.alerted_peak = state.peak
            state.sweeps_since_alert = 0
        elif action == ACTION_RESOLVED:
            state.alerted = False
            state.closed = False
            state.alerted_peak = 0
            state.sweeps_since_alert = 0
            state.consecutive = 0
            state.peak = 0
        elif action in (ACTION_SCAN_FAILING, ACTION_SCAN_REMINDER):
            state.failure_alerted = True
            state.failed_since_alert = 0
            if action == ACTION_SCAN_REMINDER:
                # Seuls les rappels **partis** élargissent la cadence : un rappel que
                # personne n'a reçu laisse l'épreuve où elle en était.
                state.failure_reminders += 1
        elif action == ACTION_SCAN_ABANDONED:
            # L'alerte de lecture se referme sur un abandon : plus rien n'en partira
            # tant que le canal ne se remet pas à lire.
            state.abandoned = True
            state.failure_alerted = False
            state.failed_since_alert = 0
            state.failure_reminders = 0
        elif action == ACTION_SCAN_REVIVED:
            state.abandoned = False
            state.failure_alerted = False
            state.failure_closed = False
            state.failed_since_alert = 0
            state.failure_reminders = 0
            state.failures = 0
        elif action == ACTION_SCAN_RECOVERED:
            state.failure_alerted = False
            state.failure_closed = False
            state.failed_since_alert = 0
            state.failure_reminders = 0
            state.failures = 0

    def retain(self, channels: Any) -> List[str]:
        """Oublie les canaux qui ne sont plus balayés ; rend ceux qu'on a oubliés.

        Sans cela, un canal retiré de la liste garderait une séquence ouverte :
        s'il revenait un jour, il serait annoncé bloqué dès son premier report, en
        s'appuyant sur des balayages qui n'ont pas eu lieu.
        """
        known = {str(channel) for channel in channels}
        forgotten = [name for name in self._states if name not in known]
        for name in forgotten:
            del self._states[name]
        return sorted(forgotten)


def _cap_label(cap: Optional[int]) -> Optional[str]:
    """La ligne du plafond, ou rien : « sans plafond » ne peut pas reporter.

    `deferred` n'existe que si un plafond a été appliqué (`budget is not None`
    dans `ingest_channel_media`) : dire « plafond : sans plafond » dans une alerte
    de report serait le signe d'un état incohérent, pas une information.
    """
    count = int(cap or 0)
    return f"{count} extraction(s) par canal et par balayage" if count else None


def _short_error(error: Any, *, limit: int = 200) -> str:
    """Le motif sur **une** ligne : une exception peut porter un retour à la ligne.

    Sans cela, un `str(exc)` multiligne casserait la puce du message, et la suite
    de l'exception se lirait comme la suite de l'alerte. Tronqué aussi : un motif
    de dix mille caractères noierait ce qu'il faut retenir.
    """
    flat = " ".join(str(error).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def format_report(
    *,
    channel: str,
    action: str,
    deferred: int = 0,
    sweeps: int = 0,
    peak: int = 0,
    cap: Optional[int] = None,
    error: Optional[str] = None,
) -> str:
    """Le message d'alerte : ce qui est constaté, depuis quand, et où agir.

    `deferred`, `peak` et `cap` ne concernent que le **report** : une alerte de
    lecture échouée n'a aucun compte à annoncer, seulement des balayages et un
    motif. Les deux familles partagent le reste (le canal nommé, la durée, la
    marche à suivre), parce que c'est la même personne qui les lit.

    Pas de balisage Markdown : le bot envoie ses messages sans `parse_mode`, donc
    des astérisques s'afficheraient telles quelles.
    """
    if action == ACTION_RESOLVED:
        return (
            f"✅ Retard de balayage résorbé — @{channel}\n\n"
            f"Plus aucune extraction reportée, après {sweeps} balayage(s) d'affilée "
            f"(jusqu'à {peak} reportée(s) par passage).\n\n"
            "Le canal rattrape son retard tout seul : rien à faire."
        )

    if action == ACTION_SCAN_RECOVERED:
        return (
            f"✅ Canal de nouveau lisible — @{channel}\n\n"
            f"Le balayage a de nouveau abouti, après {sweeps} balayage(s) d'affilée "
            "sans pouvoir lire l'aperçu du canal.\n\n"
            "Les publications de l'intervalle sont reprises au balayage suivant : "
            "rien à faire."
        )

    if action == ACTION_SCAN_REVIVED:
        return (
            f"✅ Canal de nouveau lisible — @{channel}\n\n"
            f"Le balayage a de nouveau abouti, après {sweeps} balayage(s) d'affilée "
            "sans pouvoir lire l'aperçu du canal — dont les derniers sous abandon.\n\n"
            "L'abandon est levé : les échecs de ce canal seront de nouveau signalés. "
            "Les publications de l'intervalle sont reprises au balayage suivant : "
            "rien à faire."
        )

    if action in (ACTION_SCAN_FAILING, ACTION_SCAN_REMINDER):
        scan_title = {
            ACTION_SCAN_FAILING: "🛑 Balayage en échec",
            ACTION_SCAN_REMINDER: "🛑 Balayage toujours en échec",
        }[action]
        scan_lines = [
            f"{scan_title} — @{channel}",
            "",
            f"{sweeps} balayage(s) d'affilée sans pouvoir lire l'aperçu du canal",
        ]
        if error:
            scan_lines.append(f"   • dernier motif constaté : {_short_error(error)}")
        scan_lines.extend(
            [
                "",
                "Le canal n'est pas forcément calme : c'est sa lecture qui casse. "
                "Rien de ce qu'il publie n'est arrivé — ni texte, ni média — et sa ligne de "
                "journal ressemble à celle d'un canal calme. Le suivi du report n'y "
                "voit rien non plus : un balayage qui n'a rien lu ne reporte rien.",
                "",
                "   • /channels — vérifier le pseudo balayé",
                "   • /refresh_data — relancer un cycle tout de suite",
                f"   • https://t.me/s/{channel} — ouvrir l'aperçu dans un navigateur",
            ]
        )
        return "\n".join(scan_lines)

    if action == ACTION_SCAN_ABANDONED:
        abandon_lines = [
            f"🪦 Canal tenu pour mort — @{channel}",
            "",
            f"{sweeps} balayage(s) d'affilée sans pouvoir lire l'aperçu du canal, et "
            "des rappels de plus en plus espacés, tous restés sans réponse.",
        ]
        if error:
            abandon_lines.append(f"   • dernier motif constaté : {_short_error(error)}")
        abandon_lines.extend(
            [
                "",
                "On cesse d'alerter sur ce canal : il a été signalé, puis rappelé, et un "
                "message de plus n'apprendrait rien. Rien de ce qu'il publie n'arrive, et "
                "sa lecture ne repartira pas toute seule.",
                "",
                "   • /channels — vérifier qu'il est encore balayé, ou le retirer",
                f"   • https://t.me/s/{channel} — ouvrir l'aperçu dans un navigateur",
                "",
                "Une lecture qui repart lèvera l'abandon, et sera annoncée : le silence "
                "n'est pas une condamnation.",
            ]
        )
        return "\n".join(abandon_lines)

    title = {
        ACTION_BLOCKED: "⚠️ Retard de balayage",
        ACTION_WORSENED: "⚠️ Retard de balayage — ça s'aggrave",
        ACTION_REMINDER: "⚠️ Retard de balayage — toujours bloqué",
    }.get(action, "⚠️ Retard de balayage")

    lines = [
        f"{title} — @{channel}",
        "",
        f"{sweeps} balayage(s) d'affilée avec des extractions reportées",
        f"   • reportées au dernier balayage : {deferred}",
        f"   • plus haut vu depuis le début du blocage : {peak}",
    ]
    cap_label = _cap_label(cap)
    if cap_label:
        lines.append(f"   • plafond effectif : {cap_label}")
    lines.extend(
        [
            "",
            "Un report isolé est normal : le balayage suivant reprend le surplus et "
            "les publications déjà indexées ne sont plus reproposées. Ici, il ne se "
            "résorbe pas — soit le canal publie plus vite que le budget, soit une "
            "extraction échoue et revient à chaque passage.",
            "",
            "   • /channels — le plafond et la période effectifs",
            "   • /channels max 20 — relever le plafond (0 = sans plafond)",
            "   • /transcribe — rattraper une extraction qui n'a pas eu lieu",
        ]
    )
    return "\n".join(lines)


async def observe_sweep(
    results: Mapping[str, Optional[int]],
    *,
    errors: Optional[Mapping[str, Optional[str]]] = None,
    watch: Optional[ScanBacklog] = None,
    notify: Any = None,
    chat_id: Optional[str] = None,
    cap: Optional[int] = None,
    config: Any = None,
) -> Dict[str, Any]:
    """Un balayage des canaux : constater, décider, alerter si nécessaire.

    `results` associe chaque canal balayé au nombre d'extractions reportées —
    `None` quand le balayage n'a **rien** mesuré (canal en erreur), ce qui n'est ni
    un report ni une résolution, et ne doit donc pas casser la séquence.

    `errors` associe un canal au motif de son **échec de lecture** (aperçu `t.me/s`
    injoignable, page d'un format inattendu). C'est ce qui manquait pour distinguer
    un canal calme d'un canal injoignable : les deux se présentent au balayage sous
    la forme de comptes à zéro, et un canal illisible ne reporte rien — il n'a rien
    lu, donc il n'y a rien à reporter. Le motif n'est jamais deviné : il vient du
    scraper, qui l'a constaté.

    Ne lève **jamais** : une veille qui tombe emporterait le balayage et la
    boucle avec elle. Les collaborateurs sont injectables pour rester testable
    sans Telegram ni base.
    """
    watch = watch if watch is not None else ScanBacklog()
    notify = notify or send_admin_message
    config = config or config_runtime.get_env_config()
    if chat_id is None:
        chat_id = config.telegram_admin_chat_id

    errors = errors if errors is not None else {}
    actions: Dict[str, str] = {}
    for channel, deferred in results.items():
        error = errors.get(channel)
        if deferred is None and not error:
            continue
        actions[channel] = watch.observe(channel, deferred=deferred, error=error)
    watch.retain(results.keys())

    to_send = {name: action for name, action in actions.items() if action != ACTION_SILENT}
    result: Dict[str, Any] = {
        "ok": True,
        "actions": actions,
        "alerted": [],
        # `resolved` couvre les deux familles : ce qui est annoncé, c'est que le
        # canal est revenu à la normale, quel que soit ce qui n'allait pas — y
        # compris une lecture qui repart **après un abandon**, seule façon de lever
        # l'abandon.
        "resolved": [
            name
            for name, action in actions.items()
            if action in (ACTION_RESOLVED, ACTION_SCAN_RECOVERED, ACTION_SCAN_REVIVED)
        ],
        "failing": [
            name for name, action in actions.items() if action in _SCAN_SIGNALLED
        ],
        # Ceux sur lesquels on vient de **renoncer** : nommés une fois, au moment où
        # le silence commence — sans quoi le silence serait inexplicable.
        "abandoned": [
            name for name, action in actions.items() if action == ACTION_SCAN_ABANDONED
        ],
        "reason": None,
    }
    if not to_send:
        return result

    if not chat_id:
        # Le dire est le minimum : sans ce message, l'alerte serait perdue et rien
        # ne l'expliquerait. Rien n'est mémorisé — personne n'a rien lu.
        print(
            "   [balayage] aucune alerte de retard envoyée : TELEGRAM_ADMIN_CHAT_ID "
            "n'est pas renseigné"
        )
        result["reason"] = "no_admin_chat"
        return result

    for channel, action in to_send.items():
        if action in _SCAN_ACTIONS:
            # Un échec de lecture ne se compte pas en extractions : `sweeps` est la
            # suite d'échecs, et le motif vient de l'appelant, jamais du module.
            text = format_report(
                channel=channel,
                action=action,
                sweeps=watch.failures(channel),
                error=errors.get(channel),
            )
        else:
            text = format_report(
                channel=channel,
                action=action,
                deferred=int(results[channel] or 0),
                sweeps=watch.consecutive(channel),
                peak=watch.peak(channel),
                cap=cap,
            )
        try:
            await notify(chat_id, text)
        except Exception as exc:
            # On ne mémorise pas : l'anomalie n'a été vue par personne. Le
            # balayage suivant redemandera la même alerte.
            print(f"   [balayage] alerte non envoyée pour @{channel} : {type(exc).__name__}: {exc}")
            result["reason"] = result["reason"] or "send_failed"
            continue
        watch.recorded(channel, action)
        result["alerted"].append(channel)

    result["alerted"].sort()
    return result


__all__ = [
    "ABANDON_AFTER_REMINDERS",
    "ACTION_BLOCKED",
    "ACTION_REMINDER",
    "ACTION_RESOLVED",
    "ACTION_SCAN_ABANDONED",
    "ACTION_SCAN_FAILING",
    "ACTION_SCAN_RECOVERED",
    "ACTION_SCAN_REMINDER",
    "ACTION_SCAN_REVIVED",
    "ACTION_SILENT",
    "ACTION_WORSENED",
    "DEFAULT_CONSECUTIVE_SWEEPS",
    "REMINDER_EVERY",
    "ScanBacklog",
    "format_report",
    "observe_sweep",
]
