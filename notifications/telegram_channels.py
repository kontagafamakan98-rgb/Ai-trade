"""Commande Telegram `/channels` : lire et régler le balayage des canaux publics.

Trois réglages, et un seul endroit pour les lire : les canaux balayés, la période
entre deux balayages, et le plafond d'extractions qu'un balayage peut lancer par
canal (`max`). Ce dernier existe pour qu'un canal qui publie un album de vingt
photos ne consomme pas le quota de vision d'un après-midi sur un seul passage :
le surplus est **reporté au balayage suivant**.

Pourquoi un module, et pourquoi celui-ci
---------------------------------------
`main.py` n'est pas importable en test (il construit le moteur, charge `dotenv`,
exige des secrets) : ce qui décide **quoi écrire** doit donc vivre ici, comme pour
`/media` et `/transcribe` (`notifications/telegram_media.py`). Et comme la
commande change un réglage **global** — celui du worker, pas celui d'un
utilisateur, il n'y a pas de liste par chat —, la règle de qui a le droit
d'écrire mérite d'être vérifiable elle aussi.

Rien n'est validé ici. La commande appelle `core.config_runtime`, exactement les
fonctions qui valident l'environnement et la surcharge de base
(`split_telegram_channels`, `clamp_scan_minutes`, `clamp_scan_max_extractions`),
et relit l'état effectif par la fonction que lit le worker
(`database.settings.telegram_scan_settings`). Un second
validateur finirait par diverger : une valeur acceptée ici serait refusée au
balayage, ou l'inverse, et la liste affichée ne serait plus celle qui est balayée.

Ce module ne connaît donc ni Telegram ni l'horloge : il reçoit des arguments déjà
découpés et un client Supabase injectable, ce qui le rend testable sans réseau.
À ne pas confondre avec `scrapers/telegram_channel.py`, qui *lit* un canal public ;
ici on ne parle que de la configuration du balayage.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from core import config_runtime
from database import settings

#: Verbes reconnus après `/channels`, et ce qu'ils font.
#: `list` (« montre-moi ») est l'action par défaut : ne rien taper doit informer,
#: pas modifier.
ACTIONS = ("list", "add", "remove", "every", "max", "reset", "help")

#: Les actions qui écrivent un réglage, donc qui demandent le chat admin.
MUTATING_ACTIONS = ("add", "remove", "every", "max", "reset")


def _parsed(
    action: str, *, values: Sequence[str] = (), problem: Optional[str] = None
) -> Dict[str, Any]:
    """Résultat d'analyse : l'action, ses valeurs, et le problème s'il y en a un."""
    return {"action": action, "values": list(values), "problem": problem, "ok": problem is None}


def parse_channels_args(args: Sequence[Any]) -> Dict[str, Any]:
    """Analyse les arguments de `/channels` — sans rien valider des valeurs.

    Un verbe inconnu n'est **pas** pris pour un canal : `/channels canaux` rend
    l'aide au lieu d'ajouter « canaux » à la liste balayée. Même raison pour
    `add` sans valeur : une faute de frappe doit se voir, pas écrire un réglage.

    Les valeurs elles-mêmes (les canaux) sont validées plus loin, par la fonction
    de configuration : c'est elle qui sait ce qu'est un pseudo public.
    """
    tokens = [str(token) for token in (args or [])]
    if not tokens:
        return _parsed("list")
    head = tokens[0].strip().lower()
    rest = tokens[1:]
    if head in ("--help", "-h", "help", "aide"):
        return _parsed("help")
    if head in ("list", "ls", "show"):
        #: Lire s'écrit aussi explicitement : « /channels list » est ce qu'on tape
        #: naturellement, et une commande qui refuse ce mot pour un verbe inconnu
        #: ferait douter de la commande plutôt que de la frappe.
        return _parsed("list")
    if head in ("add", "remove"):
        if not rest:
            return _parsed(head, problem=f"« {head} » demande au moins un canal.")
        return _parsed(head, values=rest)
    if head == "every":
        if len(rest) != 1:
            return _parsed(
                "every", values=rest, problem="« every » demande une seule durée, en minutes."
            )
        return _parsed("every", values=rest)
    if head == "max":
        #: `0` est une valeur comme une autre ici (« sans plafond ») : la refuser
        #: pour la forme forcerait à passer par l'environnement pour retirer le
        #: plafond, ce que la commande est justement là pour éviter.
        if len(rest) != 1:
            return _parsed(
                "max", values=rest, problem="« max » demande un seul nombre (0 = sans plafond)."
            )
        return _parsed("max", values=rest)
    if head in ("reset", "default"):
        return _parsed("reset")
    return _parsed("unknown", values=tokens, problem=f"Verbe inconnu : {head!r}.")


def format_channels_help(parsed: Optional[Dict[str, Any]] = None) -> str:
    """L'aide de la commande — et le problème rencontré, s'il y en a un.

    Le message ne porte volontairement aucun balisage Markdown : le bot envoie
    ses messages sans `parse_mode`, donc des astérisques ou des accents graves
    s'afficheraient tels quels.
    """
    low, high = config_runtime.TELEGRAM_SCAN_MINUTES_BOUNDS
    cap_low, cap_high = config_runtime.TELEGRAM_SCAN_MAX_EXTRACTIONS_BOUNDS
    head = ""
    problem = (parsed or {}).get("problem")
    if problem:
        head = f"⚠️ {problem}\n\n"
    return (
        f"{head}"
        "📡 /channels — les canaux publics balayés par la boucle automatique\n\n"
        "• /channels — ce qui est balayé, et d'où vient le réglage\n"
        "• /channels add @canal, @autre — ajouter (sans retirer les autres)\n"
        "• /channels remove @canal — retirer (les retirer tous éteint la collecte)\n"
        f"• /channels every 30 — période, en minutes (bornes {low}..{high})\n"
        f"• /channels max 10 — extractions par canal et par balayage "
        f"(bornes {cap_low}..{cap_high}, 0 = sans plafond) ; le surplus est "
        "repris au balayage suivant\n"
        "• /channels reset — revenir à l'environnement (supprime la surcharge)\n\n"
        "Un canal public s'écrit sans espace : 5 à 32 caractères, minuscules, "
        "chiffres et souligné. Sont acceptés @canal, https://t.me/canal, "
        "t.me/s/canal et un lien de message (t.me/canal/42). Un titre comme "
        "« Crypto Signals » est écarté, et dit — jamais transformé en pseudo "
        "fantôme. Séparateurs : virgules, points-virgules, ou plusieurs "
        "arguments.\n"
        "L'écriture est réservée au chat configuré par TELEGRAM_ADMIN_CHAT_ID."
    )


def _source_label(override: Any, env_name: str) -> str:
    """D'où vient un réglage effectif : surcharge de base, ou environnement."""
    if override is None:
        return f"environnement ({env_name}), sinon défaut du projet"
    return "surcharge en base (bot_settings)"


def _cap_label(value: Any) -> str:
    """Le plafond d'extractions, dit en clair.

    `0` n'est pas un plafond **nul** — ce serait une collecte qui ne collecte
    rien —, c'est l'absence de plafond. L'afficher « 0 extraction(s) » ferait
    croire à une collecte éteinte alors qu'elle tourne sans limite.
    """
    count = int(value or 0)
    return f"{count} extraction(s) par canal et par balayage" if count else "sans plafond"


def format_channels_state(effective: Dict[str, Any], overrides: Dict[str, Any]) -> str:
    """L'état **effectif** des deux réglages, et d'où il vient.

    `effective` vient de `database.settings.telegram_scan_settings`, la fonction
    que lit l'auto-loop : ce qui est affiché est donc ce qui sera balayé, et non
    une seconde lecture qui pourrait en diverger.
    """
    channels = effective["channels"]
    lines: List[str] = ["📡 Canaux balayés", ""]
    lines.extend(f"• @{channel}" for channel in channels)
    if not channels:
        lines.append("• aucun (la collecte est éteinte)")
    lines.append("")
    lines.append(f"Source : {_source_label(overrides.get('channels'), 'TELEGRAM_CHANNELS')}")
    lines.append(
        f"Période : {effective['scan_minutes']} min — "
        f"{_source_label(overrides.get('minutes'), 'TELEGRAM_SCAN_MINUTES')}"
    )
    lines.append(
        f"Plafond : {_cap_label(effective['max_extractions'])} — "
        f"{_source_label(overrides.get('max_extractions'), 'TELEGRAM_SCAN_MAX_EXTRACTIONS')}"
    )
    rejected = effective.get("rejected") or []
    if rejected:
        lines.append("")
        lines.append("⚠️ Écarté, pas deviné :")
        lines.extend(
            f"• {raw!r} (pseudo public attendu, ex. « moncanal »)" for raw in rejected
        )
    lines.append("")
    lines.append("Modifier : /channels --help")
    return "\n".join(lines)


def channels_view(*, client: Any = None) -> Dict[str, Any]:
    """État effectif + provenance, tel qu'il faut l'afficher pour `/channels`."""
    effective = settings.telegram_scan_settings(client=client)
    overrides = {
        "channels": settings.get_setting(settings.SETTING_TELEGRAM_CHANNELS, client=client),
        "minutes": settings.get_setting(settings.SETTING_TELEGRAM_SCAN_MINUTES, client=client),
        "max_extractions": settings.get_setting(
            settings.SETTING_TELEGRAM_SCAN_MAX_EXTRACTIONS, client=client
        ),
    }
    return {"text": format_channels_state(effective, overrides), **effective, "overrides": overrides}


def admin_access_problem(*, chat_id: Any, admin_chat_id: Any) -> Optional[str]:
    """Rien si ce chat peut écrire le réglage, sinon le message qui l'explique.

    Le réglage est **global** : la liste appartient au worker, pas à un chat. Le
    droit d'écrire est donc calqué sur le seul chat de confiance du bot, celui que
    `TELEGRAM_ADMIN_CHAT_ID` désigne — et **fail-closed** quand il n'est pas
    renseigné, comme le reste de la configuration : sans lui, personne ne dit qui
    est l'opérateur, et laisser n'importe quel chat rediriger la collecte serait
    pire que refuser d'écrire.
    """
    expected = str(admin_chat_id or "").strip()
    if not expected:
        return (
            "🔒 Réglage verrouillé : aucun chat admin n'est configuré.\n"
            "Renseigne TELEGRAM_ADMIN_CHAT_ID (le chat qui reçoit déjà les "
            "comptes-rendus des publications de canal)."
        )
    if str(chat_id) != expected:
        return "🔒 Seul le chat admin peut changer les canaux balayés."
    return None


def _channel_list(channels: Sequence[str]) -> str:
    return ", ".join(f"@{channel}" for channel in channels) or "aucun"


def _rejected_note(rejected: Sequence[str]) -> str:
    return (
        "⚠️ Écarté, pas deviné : "
        + ", ".join(repr(raw) for raw in rejected)
        + " (pseudo public attendu, ex. « moncanal »)"
    )


def _no_usable_value(values: Sequence[str]) -> str:
    return (
        "❌ Aucun canal utilisable dans : "
        + ", ".join(repr(str(value)) for value in values)
        + "\nUn pseudo public s'écrit sans espace : 5 à 32 caractères, minuscules, "
        "chiffres et souligné (ex. « moncanal »)."
    )


def _write_failed(write: Dict[str, Any], confirm: str) -> str:
    """Ce qu'on dit quand le réglage n'a **pas** été écrit.

    Le message ne peut pas être un compte-rendu : rien n'a changé, et l'annoncer
    autrement ferait croire à une configuration qui n'existe pas.
    """
    reason = write.get("reason")
    detail = {
        "no_client": "aucun client Supabase configuré (SUPABASE_URL / SUPABASE_SERVICE_KEY)",
        "write_failed": f"écriture refusée par la base : {write.get('error')}",
    }.get(reason, str(reason))
    return (
        f"❌ Réglage NON appliqué : {detail}.\n"
        f"Demandé : {confirm}\n"
        "La collecte continue avec la configuration précédente."
    )


def _applied(action: str, notes: Sequence[str], *, client: Any = None) -> Dict[str, Any]:
    """Compte-rendu d'un réglage **écrit**, suivi de l'état relu.

    L'état est relu par la fonction du worker plutôt que reconstruit à la main :
    afficher l'état voulu au lieu de l'état écrit serait la façon la plus simple
    de mentir sur ce qui sera balayé au prochain cycle.
    """
    body = "\n".join(notes) + "\n\n" + channels_view(client=client)["text"]
    return {"ok": True, "action": action, "text": body, "changed": True}


def _write_channels(parsed: Dict[str, Any], *, mode: str, client: Any = None) -> Dict[str, Any]:
    """Ajoute ou retire des canaux de la liste **effective**.

    La liste de départ est celle qui est balayée **maintenant**
    (`telegram_scan_settings`) : ajouter à une liste vide alors que
    l'environnement en configure deux écrirait « le nouveau, seul » et ferait
    disparaître les deux autres sans le dire. Ce qui est écrit est donc la liste
    effective modifiée — et sans surcharge en base, elle devient explicite, ce qui
    est voulu : ce qu'on lit dans la réponse est ce que le worker lira.

    Une valeur écartée n'annule pas les autres : c'est déjà la règle de
    `split_telegram_channels`, et la réponse dit laquelle a été écartée.
    """
    # Les arguments sont rejoints par des virgules avant d'être découpés : ils
    # passent alors par **le** découpage de la configuration, celui de
    # `TELEGRAM_CHANNELS`, plutôt que par un découpage « de commande » qui
    # accepterait des formes que le worker refuserait.
    wanted, rejected = config_runtime.split_telegram_channels(",".join(parsed["values"]))
    if not wanted:
        return {
            "ok": False,
            "action": mode,
            "changed": False,
            "text": _no_usable_value(parsed["values"]),
        }

    current = settings.telegram_scan_settings(client=client)["channels"]
    if mode == "add":
        added = [channel for channel in wanted if channel not in current]
        known = [channel for channel in wanted if channel in current]
        updated = current + added
        if not added and not rejected:
            return {
                "ok": True,
                "action": mode,
                "changed": False,
                "text": f"ℹ️ Déjà balayé : {_channel_list(known)} — rien à ajouter.",
            }
        confirm = f"✅ Ajouté : {_channel_list(added)}" if added else "❌ Aucun canal ajouté."
    else:
        removed = [channel for channel in wanted if channel in current]
        absent = [channel for channel in wanted if channel not in current]
        updated = [channel for channel in current if channel not in wanted]
        if not removed and not rejected:
            return {
                "ok": True,
                "action": mode,
                "changed": False,
                "text": f"ℹ️ Pas balayé : {_channel_list(absent)} — rien à retirer.",
            }
        confirm = f"✅ Retiré : {_channel_list(removed)}" if removed else "❌ Aucun canal retiré."

    write = settings.set_setting(settings.SETTING_TELEGRAM_CHANNELS, updated, client=client)
    if not write["ok"]:
        return {
            "ok": False,
            "action": mode,
            "changed": False,
            "text": _write_failed(write, confirm),
        }

    notes = [confirm]
    if rejected:
        notes.append(_rejected_note(rejected))
    if not updated:
        # La liste vide **éteint** la collecte : c'est une décision légitime, mais
        # elle ne doit pas se prendre sans être lue.
        notes.append("⚠️ Plus aucun canal balayé : la collecte est éteinte.")
    return _applied(mode, notes, client=client)


def _write_minutes(parsed: Dict[str, Any], *, client: Any = None) -> Dict[str, Any]:
    """Règle la période de balayage, bornée par les bornes du balayage lui-même.

    Là où `parse_scan_minutes` (la lecture de configuration) retombe en silence sur
    la valeur précédente, la commande **refuse** une valeur illisible : elle doit
    répondre à quelqu'un qui attend, et « bientot » ne peut pas passer pour « garde
    la valeur actuelle ». Les bornes, elles, restent celles du worker — une
    période hors bornes est ramenée, et le message le dit.
    """
    raw = str(parsed["values"][0])
    minutes = config_runtime.clamp_scan_minutes(raw)
    low, high = config_runtime.TELEGRAM_SCAN_MINUTES_BOUNDS
    if minutes is None:
        return {
            "ok": False,
            "action": "every",
            "changed": False,
            "text": (
                f"❌ Période illisible : {raw!r}.\n"
                f"Attendu un nombre de minutes (bornes {low}..{high}). "
                "Exemple : /channels every 30"
            ),
        }

    note = f"✅ Période : {minutes} min."
    # La valeur demandée n'est relue que parce que `clamp_scan_minutes` vient de
    # l'accepter : ce n'est pas une seconde validation, seulement de quoi dire
    # qu'une valeur hors bornes a été ramenée (sinon « 5 min » passerait pour ce
    # qui a été demandé).
    requested = int(float(raw))
    if requested != minutes:
        note += f" ({requested} est hors bornes {low}..{high}, ramené à {minutes}.)"
    write = settings.set_setting(settings.SETTING_TELEGRAM_SCAN_MINUTES, minutes, client=client)
    if not write["ok"]:
        return {
            "ok": False,
            "action": "every",
            "changed": False,
            "text": _write_failed(write, note),
        }
    return _applied("every", [note], client=client)


def _write_max(parsed: Dict[str, Any], *, client: Any = None) -> Dict[str, Any]:
    """Règle le plafond d'extractions par canal et par balayage.

    Même contrat que `_write_minutes` : une valeur illisible est **refusée** (elle
    ne peut pas passer pour « garde l'ancien »), une valeur hors bornes est
    ramenée et le message le dit. Une différence, et elle vient du sens du
    réglage : `0` est conservé tel quel. C'est « sans plafond », pas « aucune
    extraction » — le borner en silence rendrait un cran d'arrêt impossible à
    retirer depuis Telegram.
    """
    raw = str(parsed["values"][0])
    cap = config_runtime.clamp_scan_max_extractions(raw)
    low, high = config_runtime.TELEGRAM_SCAN_MAX_EXTRACTIONS_BOUNDS
    if cap is None:
        return {
            "ok": False,
            "action": "max",
            "changed": False,
            "text": (
                f"❌ Plafond illisible : {raw!r}.\n"
                f"Attendu un nombre d'extractions par canal et par balayage "
                f"(bornes {low}..{high}, 0 = sans plafond). Exemple : /channels max 10"
            ),
        }

    note = (
        f"✅ Plafond : {cap} extraction(s) par canal et par balayage "
        "(le surplus est repris au balayage suivant)."
        if cap
        else "✅ Plafond retiré : plus de limite d'extractions par balayage."
    )
    # Comme pour la période : la valeur demandée n'est relue que parce que
    # `clamp_scan_max_extractions` vient de l'accepter, et seulement pour dire
    # qu'une valeur hors bornes a été ramenée.
    requested = int(float(raw))
    if requested != cap:
        note += f" ({requested} est hors bornes {low}..{high}, ramené à {cap}.)"
    write = settings.set_setting(
        settings.SETTING_TELEGRAM_SCAN_MAX_EXTRACTIONS, cap, client=client
    )
    if not write["ok"]:
        return {
            "ok": False,
            "action": "max",
            "changed": False,
            "text": _write_failed(write, note),
        }
    return _applied("max", [note], client=client)


def _reset(*, client: Any = None) -> Dict[str, Any]:
    """Supprime les surcharges : retour à l'environnement.

    `None` **supprime** la ligne au lieu d'écrire une valeur vide (voir
    `database.settings.set_setting`) : c'est la ligne absente que la lecture sait
    interpréter comme « pas de surcharge ». Écrire une liste vide dirait autre
    chose — « ne balayer aucun canal ». Un réglage ajouté au balayage doit être
    ajouté **ici** aussi : une surcharge oubliée survivrait à « revenir à
    l'environnement », qui ne reviendrait alors qu'à moitié.
    """
    writes = [
        settings.set_setting(key, None, client=client)
        for key in (
            settings.SETTING_TELEGRAM_CHANNELS,
            settings.SETTING_TELEGRAM_SCAN_MINUTES,
            settings.SETTING_TELEGRAM_SCAN_MAX_EXTRACTIONS,
        )
    ]
    failed = next((write for write in writes if not write["ok"]), None)
    if failed:
        return {
            "ok": False,
            "action": "reset",
            "changed": False,
            "text": _write_failed(failed, "suppression de la surcharge en base"),
        }
    return _applied(
        "reset",
        ["✅ Retour à l'environnement : la surcharge en base est supprimée."],
        client=client,
    )


def channels_command(
    parsed: Dict[str, Any],
    *,
    chat_id: Any = None,
    admin_chat_id: Any = "",
    client: Any = None,
) -> Dict[str, Any]:
    """Applique une commande déjà analysée et rend ce qu'il faut répondre.

    Retourne `{"ok", "action", "text", "changed"}`. Les deux drapeaux sont
    distincts à dessein : `ok` dit que la commande a été **acceptée** (un refus,
    une faute de frappe ou une écriture impossible le laissent à `False`), tandis
    que `changed` dit qu'un réglage a réellement été écrit. Un ajout déjà
    satisfait n'est donc pas un échec — et une écriture refusée par la base ne
    peut jamais être annoncée comme une réussite.
    """
    action = str(parsed.get("action") or "list")
    if action == "help":
        return {"ok": True, "action": action, "text": format_channels_help(), "changed": False}
    if not parsed.get("ok", True) or action not in ACTIONS:
        # Aide + problème : ce qui a été mal tapé est rappelé avant la marche à
        # suivre, sinon l'opérateur relit l'aide sans voir sa faute.
        return {
            "ok": False,
            "action": action,
            "text": format_channels_help(parsed),
            "changed": False,
        }
    if action == "list":
        return {
            "ok": True,
            "action": action,
            "text": channels_view(client=client)["text"],
            "changed": False,
        }

    if action in MUTATING_ACTIONS:
        #: Le réglage est global : qui a le droit de l'écrire se décide ici, et une
        #: seule fois — avant de construire quoi que ce soit.
        refusal = admin_access_problem(chat_id=chat_id, admin_chat_id=admin_chat_id)
        if refusal:
            return {"ok": False, "action": action, "text": refusal, "changed": False}

    if action == "reset":
        return _reset(client=client)
    if action == "every":
        return _write_minutes(parsed, client=client)
    if action == "max":
        return _write_max(parsed, client=client)
    return _write_channels(parsed, mode=action, client=client)


__all__ = [
    "ACTIONS",
    "MUTATING_ACTIONS",
    "admin_access_problem",
    "channels_command",
    "channels_view",
    "format_channels_help",
    "format_channels_state",
    "parse_channels_args",
]
