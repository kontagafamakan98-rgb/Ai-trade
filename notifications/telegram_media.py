"""Ingestion des médias Telegram vers Supabase Storage.

Séparé de `main.py` — qui importe `python-telegram-bot` au chargement — pour
rester **testable sans la dépendance Telegram** : ce module ne manipule que des
objets *duck-typés* (`message.photo`, `message.video`, `message.document`,
`message.voice`, `message.audio`) et des fonctions injectables. `main.py` ne fait
que brancher les handlers PHOTO/VIDEO/DOCUMENT/VOICE/AUDIO sur ces fonctions.

Un **album** n'est pas un type de message particulier : Telegram livre chaque
élément comme un message **distinct** partageant un `media_group_id`, à quelques
dizaines de millisecondes d'intervalle, et n'annonce **jamais** le nombre
d'éléments. Chaque élément garde donc sa ligne média, son objet et son
extraction — un album n'est pas un média à plusieurs fichiers, et le fusionner
ferait perdre la déduplication par `telegram_file_id` autant que la revue d'un
seul élément. Ce qui est mis en commun, c'est **la conversation** : les éléments
sont rassemblés par `AlbumBuffer` (une seconde de silence après le dernier),
ingérés d'un seul tenant (`ingest_album`), et rendus par **un** compte-rendu —
une ligne par élément (`format_album_report`) au lieu d'une réponse par photo.
Le `media_group_id` reste dans les métadonnées : c'est ce qui permet de
retrouver après coup les éléments d'un même envoi.

Le même fichier n'est **jamais stocké deux fois** : le `telegram_file_id` est
l'empreinte que Telegram attribue au fichier, et sa présence en base fait
renoncer à l'ingestion **avant** le téléchargement.

Contrainte du fournisseur : la Bot API ne permet de télécharger qu'un fichier de
**20 Mo maximum**. On refuse donc franchement au-delà de cette limite, plutôt que
d'échouer au milieu du transfert.

L'écriture en base passe par `database.media_store` (bucket privé
`telegram-media` + table `knowledge_media`, migration 008). La clé d'objet est
déterministe dès que `chat_id` et `message_id` sont connus, et l'upload se fait
en `upsert` : rejouer le même message réécrit le même objet au lieu de créer des
doublons.

Après l'upload, le **contenu** du média est extrait (`ai.media_extractor` :
vision Gemini pour les images, `pypdf` pour les PDF, Groq `whisper-large-v3`
pour les vidéos et notes vocales, et le repli local `faster-whisper` quand Groq
ne peut pas servir) puis indexé dans `knowledge_chunks`. L'écriture des morceaux
d'un média passe par la **délégation média** de `database.media_store`
(`replace_media_chunks`) : le découpage vit toujours dans
`database.knowledge_index`, mais un seul module — `media_store` — porte la durée
de vie d'un média, du bucket jusqu'à son texte. Une extraction impossible ne
remet jamais en cause l'ingestion du média : elle est rapportée, pas levée — et
**notée** sur la ligne média, car sans cette note un échec d'extraction serait
indiscernable d'une lecture réussie dès que le média porte une légende.

Cette note rend possible le **rattrapage** : `/transcribe` (`retranscribe_media`)
relit le fichier **stocké**, vérifie que les ingrédients de l'extraction sont
là, puis réextrait et réindexe — sans rien renvoyer sur Telegram, où le fichier
serait reconnu comme déjà stocké. Le travail lui-même est celui du bouton
« ↩️ Réindexer » (`reprocess_media`), partagé pour que les deux gestes ne
divergent pas.

La **légende** du message est indexée avec le contenu extrait (elle le précède),
et **même si l'extraction échoue** : une légende « BTC support 64k » reste
cherchable, et nomme souvent l'actif mieux que la description visuelle. Le
texte indexé est donc `légende` + `contenu extrait`.

Chaque média est ensuite **étiqueté** avec un actif quand c'est possible (`$BTC`,
« bitcoin », `BTC-USD`… dans la légende, sinon dans le texte extrait ; `core.asset_tags`).
L'étiquette est l'`asset` des morceaux, donc le filtre **préférentiel** de
`match_knowledge_chunks` : elle fait remonter le média dans les analyses de cet
actif et l'exclut de celles des autres, là où un média non étiqueté reste candidat
partout (`/tag` corrige ou retire l'étiquette).

Chaque ingestion réussie est **relue par un humain** : le message de confirmation
porte deux boutons (✅ l'extraction est correcte / ❌ la retirer de l'index). Un
modèle de vision peut décrire de travers un graphique et une transcription peut
trahir une langue ; sans revue, ces erreurs entreraient dans les prompts et dans
`/search` **définitivement**. Le rejet supprime les morceaux indexés (`delete_chunks`)
mais **conserve le fichier** : le verdict reste donc réversible (bouton
« ↩️ Réindexer », qui refait l'extraction depuis le bucket sans repasser par
Telegram). Le verdict lui-même est enregistré dans `metadata` du média
(`media_store.set_review_status`) et apparaît dans `/media` — un média rejeté a
zéro morceau, il serait sinon indiscernable d'une extraction simplement échouée.

La revue ne dépend pas de la délivrance du compte-rendu : `/media` porte les
mêmes boutons, **ligne par ligne**, et dit de quel canal vient chaque média. Un
compte-rendu envoyé en chat privé que personne n'a ouvert — le bot ne peut pas
écrire à qui ne l'a jamais contacté — ou un message noyé dans l'historique reste
donc relisible plus tard, depuis la liste. Comme `/media` borne sa lecture aux
derniers médias, ce qui **attend encore** un verdict, quel que soit son âge, a sa
propre vue : `/pending` (`pending_review_view`), qui balaie la table en entier — et
qui se **parcourt** page par page (« ▶️ Suivants » / « ◀️ Précédents »), sinon
lire la fin de la file obligerait à rendre des verdicts pour la faire avancer.

Un canal qui laisse **plusieurs** attentes peut aussi être soldé d'un clic : les
rangées « ✅ @canal » / « ↩️ @canal » de `/pending` valident ou réindexent toutes
ses attentes — pages suivantes comprises —, et rendent **un** compte-rendu
(`bulk_review_channel`). C'est le geste qu'on veut après une panne de clef :
quinze graphiques d'un même canal se rattrapent ensemble, et rejeter en masse reste
impossible (`CHANNEL_BULK_VERDICTS`).

## Médias d'un **canal** (`channel_post`)

Un canal où le bot est administrateur livre ses publications au bot : c'est la
seule route qui récupère le **fichier d'origine**, documents compris. Le scraper
public (`scrapers/telegram_channel.py`) lit `t.me/s/<canal>` et n'obtient d'un
document que son nom et sa taille ; le bot, lui, le télécharge. Les deux routes
coexistent sans se recouvrir : elles portent la **même clé d'objet** et la même
clé de déduplication `(canal, message)`, donc un message déjà pris par l'aperçu
public n'est pas ingéré une seconde fois (`channel_origin`, `_find_channel_duplicate`).

Un `channel_post` ne se répond **pas** dans le canal : la réponse s'y publierait,
devant tous les abonnés, avec la référence du média et les sous-titres internes.
Le compte-rendu, la revue ✅/❌ et le `.txt` partent donc en chat privé
(`channel_report_targets`), et disent d'où vient le fichier — hors de la
publication, une photo sans contexte ne veut rien dire.
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote, unquote

from ai import media_extractor, media_indexing
from ai.media_indexing import ChunkStore, EXCERPT_CHARS, Extractor, Recorder, Tagger
from core.asset_tags import normalize_asset
from database import knowledge_index, media_store

# L'extraction puis l'indexation d'un média vivent dans `ai/media_indexing` : les
# deux routes d'ingestion Telegram (bot et scraper public) doivent produire le
# même texte, la même étiquette d'actif et les mêmes morceaux. Ce module ne garde
# que ce qui est propre au chat : le stockage, la déduplication, les messages.
_extract_and_index = media_indexing.extract_and_index
_store_asset = media_indexing.store_asset
_indexable_text = media_indexing.indexable_text
_resolve_asset = media_indexing.resolve_asset
_excerpt = media_indexing.excerpt
_media_id = media_indexing.media_id_of

#: Limite de téléchargement de la Bot API Telegram (20 Mo).
MAX_MEDIA_BYTES = 20 * 1024 * 1024

#: Silence qui clôt un album (secondes). Telegram ne dit pas combien d'éléments
#: compose un album : la seule fin observable est qu'il n'en arrive plus. Une
#: seconde est large devant la cadence observée (quelques dizaines de
#: millisecondes entre deux éléments d'un même envoi) et courte devant l'attente
#: d'un utilisateur qui vient d'envoyer ses photos.
ALBUM_WINDOW_SECONDS = 1.0

#: Nombre de médias listés par `/media`.
DEFAULT_MEDIA_PAGE = 10

#: Borne haute de `/media` : au-delà, le message dépasserait la limite Telegram
#: (4096 caractères) une fois un lien signé ajouté pour chaque média.
MAX_MEDIA_PAGE = 25

#: Nombre d'identifiants par lecture d'index (`_indexed_media`). Ces identifiants
#: partent dans le filtre `in.(…)` de l'URL PostgREST, dont la longueur est bornée :
#: au-delà de quelques centaines, la requête est refusée. Une page de `/media`
#: n'atteint jamais la borne, mais `/pending` interroge la **file entière** (ses
#: lignes et ses lots par canal) — d'où ce découpage, comme `REMOVE_BATCH_SIZE`
#: pour les suppressions d'objets.
INDEX_IDS_PER_QUERY = 100

#: Durée de vie des liens signés fournis par `/media` (1 h). Assez court pour ne
#: pas laisser traîner un accès au bucket privé, assez long pour être utilisé.
SIGNED_URL_TTL = 3600

#: Types de médias gérés par les handlers — sous-ensemble de `media_store.MEDIA_TYPES`.
SUPPORTED_MEDIA_TYPES = ("photo", "video", "document", "voice", "audio")

#: Préfixe des `callback_data` de revue d'extraction. Le vocabulaire est court
#: parce que Telegram plafonne un `callback_data` à 64 octets — un identifiant de
#: média est un UUID (36 caractères), il ne reste de la place que pour l'action.
REVIEW_PREFIX = "med"

#: Actions de revue : garder l'extraction, la retirer de l'index, la refaire.
REVIEW_VERDICTS = ("ok", "no", "re")

#: Préfixe des `callback_data` de revue cliqués **depuis la liste** `/media`.
#: Distinct de `REVIEW_PREFIX` parce que la réponse attendue n'est pas la même : un
#: verdict rendu sous un compte-rendu d'ingestion **remplace** ce message (c'est
#: son sujet), alors qu'un verdict rendu sur une ligne de `/media` doit laisser la
#: liste en place — elle est le seul endroit d'où l'on peut relire les autres
#: médias, et la remplacer ferait perdre les lignes pas encore relues.
LIST_REVIEW_PREFIX = "medl"

#: Rappel du sens des boutons, sous la liste : un bouton ne porte qu'un numéro de
#: ligne (`✅ 3`), et un numéro seul ne dit pas ce qu'il fait.
LIST_REVIEW_HINT = (
    "✅ valider la ligne · ❌ rejeter (retirer de l'index) · "
    "↩️ refaire l'extraction depuis le fichier stocké"
)

#: Préfixe des `callback_data` de **navigation** dans la liste `/media`
#: (`medlg:<rang>`). Même famille que `medpg:` (navigation de `/pending`) et que
#: `medt:` (celle de `/transcribe`), et pour la même raison : naviguer ne rend aucun
#: verdict, ne change rien en base, et le message à réécrire est la liste
#: elle-même. Distinct de `^medl:` — qui exige un deux-points juste après `medl` —
#: parce qu'un clic sur une **ligne** juge un média là où celui-ci ne fait que
#: tourner la page. Il porte un rang, pas un identifiant de média : une page se
#: recalcule, elle ne désigne aucune ligne.
MEDIA_PAGE_PREFIX = "medlg"

#: Préfixe des `callback_data` de revue cliqués depuis la liste **`/pending`**.
#: Un troisième préfixe, et pour la même raison que le second : la réponse
#: attendue diffère. Un verdict rendu ici fait **tomber la ligne** de la liste
#: — elle a désormais un verdict — et doit réafficher la **même** vue (les
#: extractions encore en attente), là où `/media` réaffiche sa page habituelle.
PENDING_REVIEW_PREFIX = "medp"

#: Nombre d'extractions affichées par `/pending`. La liste est **balayée en
#: entier** (aucune borne d'âge), mais un message Telegram en porte 4096
#: caractères : au-delà de cette page, le reste est **annoncé** et se parcourt
#: avec « ▶️ Suivants ». Les verdicts font aussi avancer la file — la vue est
#: relue après chaque clic, donc une ligne traitée laisse la place à la suivante.
PENDING_PAGE = 20

#: Borne haute de `/pending`, comme `MAX_MEDIA_PAGE` pour `/media`.
MAX_PENDING_PAGE = 50

#: Préfixe des `callback_data` de **navigation** dans la liste `/pending`
#: (`medpg:<rang>`). Un quatrième préfixe, et pour la même raison que `medt`
#: (celui de `/transcribe`) : naviguer ne rend aucun verdict, ne change rien en
#: base, et le message à réécrire n'est pas le même que celui d'un clic sur une
#: ligne. Il porte un rang, pas un identifiant de média : une page se recalcule,
#: elle ne désigne aucune ligne.
PENDING_PAGE_PREFIX = "medpg"

#: `metadata` du média → verdict lisible. Un média jamais relu n'a pas de verdict.
REVIEW_LABELS = {
    "validated": "✅ extraction validée",
    "rejected": "❌ extraction rejetée (non indexée)",
}

#: D'où vient l'étiquette d'actif → mot lisible dans le compte-rendu.
ASSET_SOURCE_LABELS = {
    "caption": "légende",
    "extraction": "contenu extrait",
    "manual": "choix manuel",
}

#: Télécharge les octets d'un `file_id` Telegram (`async`).
Downloader = Callable[[str], Awaitable[bytes]]
#: Dépose les octets + métadonnées (`media_store.upload_media`, bloquant).
Uploader = Callable[..., Dict[str, Any]]
#: Extrait le texte exploitable d'un média (`ai.media_extractor.extract_text`).
Extractor = Callable[..., Dict[str, Any]]
#: Indexe le texte extrait : la délégation média de `database.media_store`
#: (`replace_media_chunks`), et non `database.knowledge_index` directement — pour
#: qu'un média n'ait qu'une porte d'entrée, celle de son module.
ChunkStore = Callable[..., int]
#: Enregistre l'actif associé (`database.media_store.set_media_asset`).
Tagger = Callable[..., Dict[str, Any]]
#: Réétiquette les morceaux d'un média déjà indexé
#: (`database.knowledge_index.set_chunks_asset`).
ChunkTagger = Callable[[str, Optional[str]], int]
#: Livre un lot d'album : `(media_group_id, charges accumulées, dans l'ordre)`.
Deliver = Callable[[str, List[Any]], Awaitable[None]]
#: Attend un délai (`asyncio.sleep`) — injectable pour que les tests règlent le
#: temps au lieu de l'attendre.
Sleeper = Callable[[float], Awaitable[None]]


class AlbumBuffer:
    """Rassemble les messages d'un même album avant de les traiter en un lot.

    Telegram livre chaque élément d'un album comme un message distinct partageant
    un `media_group_id`, dans l'ordre, sans dire combien il en envoie : on ne peut
    donc pas attendre « le dernier élément ». La fin se devine au **silence** —
    `window` secondes sans nouvel élément — et un élément qui arrive repousse la
    livraison, sinon un album un peu lent partirait en deux réponses (ce qu'on
    cherche justement à éviter).

    `add` **rend la main immédiatement** et la livraison se fait dans une tâche à
    part ; ce n'est pas un détail d'implémentation. `python-telegram-bot` traite
    les mises à jour **une par une** (`max_concurrent_updates = 1` par défaut, et
    rien ne l'ouvre ici) : attendre le silence *dans* le handler bloquerait la
    file, les éléments suivants ne seraient distribués qu'après, et l'album se
    réduirait à son premier message — avec une réponse par photo, en retard.

    Le tampon ne connaît ni Telegram ni la base : les charges ajoutées sont
    opaques, seul leur ordre compte. Un échec de livraison est journalisé et
    n'échappe pas : personne n'attend la tâche, donc une exception y disparaîtrait
    silencieusement.
    """

    def __init__(
        self, window: float, deliver: Deliver, *, sleep: Sleeper = asyncio.sleep
    ) -> None:
        self._window = window
        self._deliver = deliver
        self._sleep = sleep
        self._pending: Dict[str, List[Any]] = {}
        self._timers: Dict[str, "asyncio.Task[None]"] = {}

    async def add(self, group_id: str, payload: Any) -> None:
        """Ajoute une charge au lot de `group_id` et repousse la livraison."""
        self._pending.setdefault(group_id, []).append(payload)
        timer = self._timers.pop(group_id, None)
        if timer is not None:
            timer.cancel()
        self._timers[group_id] = asyncio.create_task(self._deliver_later(group_id))

    async def _deliver_later(self, group_id: str) -> None:
        """Attend le silence, puis livre le lot.

        Les retraits se font **après** l'attente, pas avant : une annulation
        (nouvel élément arrivé pendant la fenêtre) doit laisser le lot en place
        pour le minuteur suivant. `timer.cancel()` délivre l'annulation au point
        de suspension — c'est-à-dire ici, dans `sleep` —, donc rien n'est perdu.
        """
        try:
            await self._sleep(self._window)
        except asyncio.CancelledError:
            return
        self._timers.pop(group_id, None)
        batch = self._pending.pop(group_id, [])
        if not batch:
            return
        try:
            await self._deliver(group_id, batch)
        except Exception as exc:
            print(
                f"   [albums] livraison impossible ({group_id}) : "
                f"{type(exc).__name__}: {str(exc)[:200]}"
            )

    async def drain(self) -> None:
        """Attend la livraison des lots en cours (les tests s'en servent)."""
        while self._timers:
            await asyncio.gather(*list(self._timers.values()), return_exceptions=True)


def extract_media(message: Any) -> Optional[Dict[str, Any]]:
    """Décrit le média porté par un message Telegram, ou `None`.

    Duck-typing volontaire : on lit les attributs sans importer
    `python-telegram-bot`, ce qui rend la fonction testable avec de simples
    objets. Un message Telegram ne porte qu'un média à la fois ; l'ordre testé
    suit la précédence de la Bot API (photo, vidéo, document, vocal, audio).
    """
    message_id = getattr(message, "message_id", None)
    caption = getattr(message, "caption", None)
    # Telegram envoie chaque élément d'un **album** comme un message distinct
    # partageant le même `media_group_id`. Le conserver permet de retrouver les
    # éléments d'un même envoi ; la légende, elle, n'est portée que par un seul
    # élément du groupe — elle reste donc rattachée au média qui la porte.
    media_group_id = getattr(message, "media_group_id", None)

    photos = getattr(message, "photo", None)
    if photos:
        largest = photos[-1]  # Telegram trie du plus petit au plus grand
        return {
            "media_type": "photo",
            "file_id": largest.file_id,
            "file_size": getattr(largest, "file_size", None),
            "file_name": f"photo_{message_id}.jpg",
            "mime_type": "image/jpeg",
            "width": getattr(largest, "width", None),
            "height": getattr(largest, "height", None),
            "duration": None,
            "caption": caption,
            "media_group_id": media_group_id,
        }

    video = getattr(message, "video", None)
    if video is not None:
        return {
            "media_type": "video",
            "file_id": video.file_id,
            "file_size": getattr(video, "file_size", None),
            "file_name": getattr(video, "file_name", None) or f"video_{message_id}.mp4",
            "mime_type": getattr(video, "mime_type", None) or "video/mp4",
            "width": getattr(video, "width", None),
            "height": getattr(video, "height", None),
            "duration": getattr(video, "duration", None),
            "caption": caption,
            "media_group_id": media_group_id,
        }

    document = getattr(message, "document", None)
    if document is not None:
        return {
            "media_type": "document",
            "file_id": document.file_id,
            "file_size": getattr(document, "file_size", None),
            "file_name": getattr(document, "file_name", None)
            or f"document_{message_id}.bin",
            "mime_type": getattr(document, "mime_type", None)
            or "application/octet-stream",
            "width": None,
            "height": None,
            "duration": None,
            "caption": caption,
            "media_group_id": media_group_id,
        }

    voice = getattr(message, "voice", None)
    if voice is not None:
        return {
            "media_type": "voice",
            "file_id": voice.file_id,
            "file_size": getattr(voice, "file_size", None),
            "file_name": f"voice_{message_id}.ogg",
            "mime_type": getattr(voice, "mime_type", None) or "audio/ogg",
            "width": None,
            "height": None,
            "duration": getattr(voice, "duration", None),
            "caption": caption,
            "media_group_id": media_group_id,
        }

    audio = getattr(message, "audio", None)
    if audio is not None:
        return {
            "media_type": "audio",
            "file_id": audio.file_id,
            "file_size": getattr(audio, "file_size", None),
            "file_name": getattr(audio, "file_name", None) or f"audio_{message_id}.mp3",
            "mime_type": getattr(audio, "mime_type", None) or "audio/mpeg",
            "width": None,
            "height": None,
            "duration": getattr(audio, "duration", None),
            "caption": caption,
            "media_group_id": media_group_id,
        }

    return None


def album_group(message: Any) -> Optional[str]:
    """`media_group_id` du message, ou `None` s'il n'appartient pas à un album.

    Publique et séparée de `extract_media` : c'est la première question que pose
    le handler, **avant** de savoir s'il y a un média — un album se reconnaît au
    groupe, et c'est cette réponse qui décide s'il faut répondre tout de suite ou
    mettre le message de côté. La normalisation (`str`) est la même que celle des
    métadonnées, pour que le groupe du tampon et celui écrit en base concordent.
    """
    group = getattr(message, "media_group_id", None)
    if group is None or group == "":
        return None
    return str(group)


def channel_origin(message: Any) -> Optional[Dict[str, Any]]:
    """Décrit le **canal** d'où vient une publication, ou `None`.

    Duck-typing comme le reste du module. Retourne `None` pour un message
    ordinaire : la route canal est identifiée par `chat.type == "channel"`, pas
    par la présence d'un champ — un chat privé n'a rien à y faire.

    `chat_id` est la **clé de stockage** : le nom du canal quand il en a un, sinon
    son identifiant numérique. Le nom d'abord parce que c'est la clé que le
    scraper public écrit déjà (`t.me/s/<canal>`) : les deux routes déposent alors
    le même objet au même endroit, et la déduplication par `(canal, message)`
    devient possible sans colonne supplémentaire.

    `author_id`/`author` décrivent qui a publié, quand Telegram le dit : une
    publication « en tant que canal » (le défaut) n'a pas d'auteur, et c'est
    justement le cas où le compte-rendu doit viser un chat configuré.
    """
    chat = getattr(message, "chat", None)
    if getattr(chat, "type", None) != "channel":
        return None

    username = getattr(chat, "username", None) or None
    numeric_id = getattr(chat, "id", None)
    author = getattr(message, "from_user", None)
    return {
        "chat_id": username or (str(numeric_id) if numeric_id is not None else "inconnu"),
        "channel": username or getattr(chat, "title", None) or str(numeric_id or "canal"),
        "channel_id": numeric_id,
        "title": getattr(chat, "title", None),
        "username": username,
        "message_id": getattr(message, "message_id", None),
        "author_id": getattr(author, "id", None),
        "author": getattr(author, "full_name", None) or getattr(author, "username", None),
    }


async def ingest_media(
    message: Any,
    *,
    download: Downloader,
    upload: Uploader = media_store.upload_media,
    extract: Extractor = media_extractor.extract_text,
    store: ChunkStore = media_store.replace_media_chunks,
    record: Recorder = media_store.set_extraction_outcome,
    tag_store: Tagger = media_store.set_media_asset,
    channel: Optional[Dict[str, Any]] = None,
    find_message: Optional[Callable[..., Optional[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """Télécharge le média d'un message et le persiste dans Supabase.

    Retourne un **compte-rendu** (`{"ok": ...}`) plutôt que de lever : le handler
    Telegram appelant doit pouvoir répondre à l'utilisateur dans tous les cas
    (fichier trop gros, lien expiré, panne Supabase…). `download` est injecté pour
    rester testable sans réseau ni `python-telegram-bot` ; il en va de même pour
    `upload`, délesté dans un thread car `media_store.upload_media` est bloquant.

    **Déduplication** : un média déjà enregistré sous le même `telegram_file_id`
    est reconnu **avant** le téléchargement et n'occupe aucune place de plus —
    rejouer un message, ou renvoyer le même fichier dans un autre message, ne
    produit ni second objet, ni seconde ligne.

    `channel` (le résultat de `channel_origin`) déclare une publication de canal :
    le média est alors rangé sous la clé du **canal** (et non sous l'identifiant
    numérique du chat), et un second test — `(canal, message)` — écarte ce que le
    scraper public a déjà ingéré du même message. Indispensable : l'aperçu public
    n'a pas de `telegram_file_id`, donc le premier test ne peut pas le reconnaître,
    et le même fichier serait stocké deux fois.
    """
    descriptor = extract_media(message)
    if descriptor is None:
        return {"ok": False, "reason": "no_media"}

    duplicate = await _find_duplicate(descriptor["file_id"])
    if duplicate is not None:
        # Court-circuit : rien n'est téléchargé ni réécrit. Attention, une
        # légende **nouvelle** sur un fichier déjà connu ne peut pas être
        # fusionnée sans risque — `replace_media_chunks` **remplace** les morceaux
        # du média, donc indexer la seule légende effacerait le contenu extrait
        # précédemment. On préfère ne rien toucher et le dire à l'utilisateur.
        return {
            "ok": True,
            "duplicate": True,
            "media_type": descriptor["media_type"],
            "media": duplicate,
            "caption_ignored": descriptor.get("caption") or None,
        }

    if channel:
        seen = await _find_channel_duplicate(channel, find_message)
        if seen is not None:
            return {
                "ok": True,
                "duplicate": True,
                "duplicate_reason": "message",
                "media_type": descriptor["media_type"],
                "media": seen,
                "channel": channel,
            }

    declared_size = descriptor.get("file_size")
    if declared_size is not None and declared_size > MAX_MEDIA_BYTES:
        return {"ok": False, "reason": "too_large", "size": declared_size}

    try:
        data = await download(descriptor["file_id"])
    except Exception as exc:  # lien expiré, réseau, fichier retiré côté Telegram
        return {"ok": False, "reason": "download_failed", "error": str(exc)}

    size = len(data)
    if size > MAX_MEDIA_BYTES:
        return {"ok": False, "reason": "too_large", "size": size}

    # Clé de stockage : le canal quand la publication en vient (même convention
    # que le scraper public), l'identifiant du chat sinon. Une publication
    # anonyme porte `chat_id` = le canal, donc l'objet dit de lui-même d'où il
    # vient, même sans les métadonnées.
    chat_id = channel["chat_id"] if channel else getattr(message, "chat_id", None)
    try:
        row = await asyncio.to_thread(
            upload,
            data,
            media_type=descriptor["media_type"],
            mime_type=descriptor["mime_type"],
            file_name=descriptor["file_name"],
            source="telegram",
            chat_id=str(chat_id) if chat_id is not None else None,
            message_id=getattr(message, "message_id", None),
            caption=descriptor.get("caption"),
            width=descriptor.get("width"),
            height=descriptor.get("height"),
            duration_seconds=descriptor.get("duration"),
            telegram_file_id=descriptor["file_id"],
            metadata=_telegram_metadata(descriptor, channel),
            upsert=True,
        )
    except Exception as exc:
        return {"ok": False, "reason": "upload_failed", "error": str(exc)}

    media_id = _media_id(row)
    extraction = await _extract_and_index(
        data,
        descriptor,
        media_id=media_id,
        extract=extract,
        store=store,
        record=record,
    )
    stored = await _store_asset(
        media_id, extraction.get("asset"), extraction.get("asset_source"), tag_store
    )

    return {
        "ok": True,
        "media_type": descriptor["media_type"],
        "bytes": size,
        "media": row,
        "extraction": extraction,
        "asset_stored": stored,
        "channel": channel,
    }


async def ingest_album(
    messages: Sequence[Any],
    *,
    download: Downloader,
    upload: Uploader = media_store.upload_media,
    extract: Extractor = media_extractor.extract_text,
    store: ChunkStore = media_store.replace_media_chunks,
    record: Recorder = media_store.set_extraction_outcome,
    tag_store: Tagger = media_store.set_media_asset,
    channel: Optional[Dict[str, Any]] = None,
    find_message: Optional[Callable[..., Optional[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """Ingère un **album** d'un seul tenant : un résultat par élément, un lot.

    Ce qui ne change pas, et ne doit pas changer : chaque élément garde sa ligne
    média, son objet Storage, son extraction et sa déduplication. Un album n'est
    pas un média à plusieurs fichiers — le réunir en une seule ligne ferait perdre
    la reconnaissance du même fichier renvoyé seul plus tard, la revue élément par
    élément, et la reprise d'un seul élément (`/transcribe`).

    Les éléments sont ingérés **dans l'ordre du message** : c'est l'ordre de
    l'album, donc celui auquel le compte-rendu renvoie (`1/3`, `2/3`…).

    Un élément qui échoue n'arrête pas le lot — une vidéo de 30 Mo au milieu de
    trois photos ne doit pas priver les photos de leur ingestion. Une exception
    imprévue non plus : elle devient l'échec de **cet** élément, jamais du lot, et
    le motif est conservé tel quel pour que le compte-rendu dise quoi réparer.
    """
    results: List[Dict[str, Any]] = []
    for message in messages:
        try:
            results.append(
                await ingest_media(
                    message,
                    download=download,
                    upload=upload,
                    extract=extract,
                    store=store,
                    record=record,
                    tag_store=tag_store,
                    channel=channel,
                    find_message=find_message,
                )
            )
        except Exception as exc:
            results.append(
                {"ok": False, "reason": "exception", "error": f"{type(exc).__name__}: {exc}"}
            )
    return {"album": True, "count": len(results), "results": results}


async def _find_duplicate(file_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Média déjà enregistré pour ce `telegram_file_id`, ou None.

    La recherche est faite **avant** le téléchargement : c'est là tout l'intérêt —
    on économise la bande passante et l'objet Storage, pas seulement l'écriture.
    Une recherche impossible (Supabase non configuré, panne) ne doit pas bloquer
    l'ingestion : on laisse alors passer, et l'upload signalera l'erreur réelle.
    """
    if not file_id:
        return None
    try:
        return await asyncio.to_thread(media_store.find_media_by_telegram_file_id, file_id)
    except Exception as exc:
        print(f"   [medias] detection de doublon impossible : {type(exc).__name__}: {exc}")
        return None


async def _find_channel_duplicate(
    channel: Dict[str, Any],
    find_message: Optional[Callable[..., Optional[Dict[str, Any]]]] = None,
) -> Optional[Dict[str, Any]]:
    """Média déjà ingéré pour **ce message de canal**, ou None.

    La clé est `(canal, message)` : c'est celle que le scraper public écrit
    (`chat_id` = nom du canal), et le bot la reprend. Deux limites assumées :

    * un canal **privé** (sans `username`) n'a pas d'équivalent public, donc rien
      à comparer — le scraper ne peut pas l'avoir lu, et on ne consulte pas la
      base pour rien ;
    * une lecture impossible (Supabase non configuré, panne) ne bloque pas : on
      laisse passer, et l'`upsert` sur la même clé d'objet absorbera le doublon.
    """
    username = (channel or {}).get("username")
    message_id = (channel or {}).get("message_id")
    if not username or message_id in (None, ""):
        return None
    lookup = find_message or media_store.find_media_by_message
    try:
        return await asyncio.to_thread(lookup, username, message_id)
    except Exception as exc:
        print(f"   [medias] detection de doublon de canal impossible : {type(exc).__name__}: {exc}")
        return None


def _telegram_metadata(
    descriptor: Dict[str, Any], channel: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Métadonnées du média : origine Telegram + album + canal éventuel.

    `media_group_id` est ce qui permet de retrouver les éléments d'un même album
    (Telegram en fait des messages distincts) ; il n'est ajouté que s'il existe,
    pour ne pas polluer la colonne `metadata` du cas courant.

    `channel` est regroupé dans une **sous-clé** au lieu d'être étalé : c'est un
    contexte, pas un attribut du média, et une publication venue d'un canal doit
    rester reconnaissable comme telle dans `/media` (d'où elle vient, de quel
    message, par qui).
    """
    metadata: Dict[str, Any] = {"telegram": True}
    group = descriptor.get("media_group_id")
    if group:
        metadata["media_group_id"] = str(group)
    if channel:
        metadata["channel_post"] = {
            key: value
            for key, value in (
                ("channel", channel.get("channel")),
                ("channel_id", channel.get("channel_id")),
                ("title", channel.get("title")),
                ("message_id", channel.get("message_id")),
                ("author_id", channel.get("author_id")),
            )
            if value not in (None, "")
        }
    return metadata


def format_report(result: Dict[str, Any]) -> str:
    """Message utilisateur (Telegram) correspondant au compte-rendu d'ingestion."""
    if result.get("duplicate"):
        media = result.get("media") or {}
        name = media.get("file_name") or media.get("id") or "?"
        if result.get("duplicate_reason") == "message":
            # Deux routes d'ingestion pour un même canal (bot et aperçu public) :
            # dire laquelle a déjà pris le message évite de croire à un doublon
            # de fichier alors que c'est le même **message**.
            report = (
                f"⏭️ Message déjà ingéré depuis ce canal ({result.get('media_type')}).\n"
                f"• Fichier : {name}\n"
                f"• Référence : {media.get('id')}\n"
                "• L'aperçu public du scraper couvre déjà ce message : le fichier "
                "n'est pas stocké deux fois."
            )
        else:
            report = (
                f"⏭️ Déjà stocké ({result.get('media_type')}) — même `telegram_file_id`.\n"
                f"• Fichier : {name}\n"
                f"• Référence : {media.get('id')}"
            )
        if result.get("caption_ignored"):
            report += (
                "\n• Cette légende n'a pas été indexée : le fichier est déjà connu "
                "et réindexer effacerait le contenu extrait précédemment."
            )
        return report

    if result.get("ok"):
        media = result.get("media") or {}
        lines = [
            f"✅ Média enregistré ({result.get('media_type')})",
            f"• Taille : {result.get('bytes', 0) / 1024:.1f} Ko",
            f"• Type MIME : {media.get('mime_type')}",
            f"• Référence : {media.get('id')}",
        ]
        extraction = result.get("extraction")
        if extraction:
            if extraction.get("ok"):
                label = extraction.get("method") or "extraction"
                if extraction.get("has_caption"):
                    label = f"{label} + légende"
                lines.append(
                    f"• Contenu indexé : {extraction.get('chars', 0)} car. "
                    f"({label}, {extraction.get('chunks', 0)} morceau(x))"
                )
            else:
                lines.append(f"• Extraction : {extraction.get('reason')}")
                # Une légende peut être indexée alors que l'extraction a échoué :
                # le dire évite de croire que le média n'a rien apporté.
                if extraction.get("chunks"):
                    lines.append(
                        f"• Légende indexée seule : {extraction.get('chars', 0)} car. "
                        f"({extraction.get('chunks', 0)} morceau(x))"
                    )
            lines.extend(_asset_lines(result))
            # Le texte est montré même si l'indexation a échoué : il a bien été
            # extrait — c'est justement ce que l'utilisateur vérifie.
            lines.extend(_extract_lines(result))
        return "\n".join(lines)

    reason = result.get("reason")
    if reason == "too_large":
        return (
            f"⚠️ Fichier trop volumineux ({result.get('size', 0) / 1_000_000:.1f} Mo) : "
            "l'API Telegram limite les téléchargements des bots à 20 Mo. "
            "Rien n'a été stocké."
        )
    if reason == "download_failed":
        return f"❌ Téléchargement Telegram impossible : {result.get('error')}"
    if reason == "upload_failed":
        return f"❌ Enregistrement Supabase impossible : {result.get('error')}"
    if reason == "no_media":
        return "ℹ️ Aucun média reconnu dans ce message."
    return f"❌ Ingestion du média impossible ({reason})."


def channel_report_targets(
    channel: Optional[Dict[str, Any]], admin_chat_id: Any = None
) -> List[int]:
    """Chats privés à qui remettre le compte-rendu d'une publication de canal.

    Dans l'ordre : **qui a publié** (`from_user` du message) puis le chat
    configuré (`TELEGRAM_ADMIN_CHAT_ID`). Le premier est le mieux placé — il a le
    fichier sous les yeux et peut juger l'extraction —, le second est le seul
    possible quand la publication est anonyme (« en tant que canal », le défaut).

    Les doublons sont écartés, et une valeur inexploitable aussi : un id vide ou
    non numérique produirait un envoi en échec, pas une revue.
    """
    candidates = [(channel or {}).get("author_id"), admin_chat_id]
    targets: List[int] = []
    for candidate in candidates:
        try:
            chat_id = int(str(candidate).strip())
        except (TypeError, ValueError):
            continue
        if chat_id not in targets:
            targets.append(chat_id)
    return targets


def format_channel_report(result: Dict[str, Any], channel: Optional[Dict[str, Any]]) -> str:
    """Compte-rendu d'une ingestion venue d'un **canal**, à lire en chat privé.

    Le contexte est en tête et non en pied : hors de la publication, « ✅ Média
    enregistré » ne veut rien dire tant qu'on ne sait pas de quel canal, de quel
    message, et de quel fichier il s'agit. On dit aussi que **rien n'a été publié
    dans le canal** — sinon on croit que la revue est visible là-bas, et elle ne
    l'est pas.
    """
    origin = channel or {}
    label = origin.get("title") or origin.get("channel") or "canal inconnu"
    username = origin.get("username")
    if username:
        label = f"{label} (@{username})"

    header = [f"📥 Média publié dans {label}"]
    if origin.get("message_id") is not None:
        header.append(f"• Message : {origin['message_id']}")
    if origin.get("author"):
        header.append(f"• Auteur de la publication : {origin['author']}")
    header.append("• Rien n'a été publié dans le canal : la revue se fait ici.")
    return "\n".join(header) + "\n\n" + format_report(result)


def _human_size(size: Any) -> str:
    """Taille lisible (« 12.3 Ko ») ; jamais d'exception sur une valeur absente."""
    try:
        value = float(size)
    except (TypeError, ValueError):
        return "taille inconnue"
    if value < 1024:
        return f"{value:.0f} o"
    if value < 1024 ** 2:
        return f"{value / 1024:.1f} Ko"
    return f"{value / 1024 ** 2:.1f} Mo"


def full_text(result: Dict[str, Any]) -> str:
    """Texte **intégral** que l'extraction a indexé (légende + contenu), ou `""`.

    C'est exactement la chaîne remise à `knowledge_chunks` — donc ce que les
    embeddings, les prompts et `/search` liront. La pièce jointe ne rejoue pas
    l'extraction : elle rend ce qui a été indexé.
    """
    extraction = result.get("extraction") or {}
    return str(extraction.get("text") or "")


def attachment_needed(text: str, *, limit: int = EXCERPT_CHARS) -> bool:
    """Vrai quand l'aperçu ne peut **pas** montrer tout le texte (il serait tronqué).

    Le critère est celui de `_excerpt` : même normalisation des espaces, même
    limite. Ainsi « tronqué » et « joint en pièce jointe » ne peuvent pas
    diverger — la pièce jointe existe si et seulement si l'aperçu perd du texte.
    """
    return len(" ".join(str(text or "").split())) > limit


def _attachment_name(result: Dict[str, Any]) -> str:
    """Nom du `.txt` joint, dérivé du média et sûr pour le client Telegram.

    Le nom d'origine n'est pas repris tel quel : il peut contenir un chemin, des
    espaces ou des caractères refusés par Telegram. On n'en garde que la tige, et
    l'identifiant sert de secours s'il ne reste rien (« .pdf », nom vide).
    """
    row = result.get("media") or {}
    stem = str(row.get("file_name") or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    if stem.startswith("."):
        # `.pdf`, `.gitignore`… : il ne reste que l'extension, rien à nommer.
        stem = ""
    elif "." in stem[1:]:
        stem = stem.rsplit(".", 1)[0]
    clean = "".join(ch if (ch.isalnum() or ch in "-_.") else "-" for ch in stem).strip("-._")
    clean = clean[:60]
    if not clean:
        media_id = str(result.get("media_id") or row.get("id") or "")
        clean = f"extraction-{media_id[:8]}" if media_id else "extraction-media"
    return f"{clean}-extraction.txt"


def attachment_for(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Pièce jointe `.txt` portant le texte extrait **complet**, ou `None`.

    Rien à joindre quand l'aperçu montre déjà tout (`attachment_needed`), ou quand
    il n'y a pas de texte (extraction impossible sans légende) : un fichier vide
    n'apprendrait rien et ferait douter des autres.

    Retourne des **octets** et un nom, pas un objet Telegram : ce module n'importe
    pas `python-telegram-bot`, c'est le handler qui envoie le document.
    """
    text = full_text(result)
    if not attachment_needed(text):
        return None
    return {
        "filename": _attachment_name(result),
        "content": text.encode("utf-8"),
        "caption": (
            f"🧾 Texte extrait complet — {len(text)} caractères. "
            "C'est ce qui est indexé, donc ce que les analyses et `/search` liront."
        ),
        "chars": len(text),
    }


#: Aide de `/tag`. Elle est aussi affichée sur une erreur : les options ne
#: doivent pas s'apprendre en lisant le code.
TAG_HELP = """🏷️ `/tag` — associer un actif à un média ingéré

• `/tag BTC-USD` — en **réponse au message du média** (rien à copier)
• `/tag BTC-USD <référence>` — avec l'identifiant du média, tel qu'il apparaît dans le compte-rendu
• `/tag --clear` — retire l'étiquette (mêmes deux façons de désigner le média)

À quoi ça sert : l'étiquette est l'`asset` des morceaux indexés, donc le filtre
**préférentiel** de la recherche. Un média étiqueté `BTC-USD` remonte en tête des
analyses de BTC-USD et **disparaît** de celles des autres actifs ; sans étiquette,
il reste candidat partout, sans bonus de classement.

Ex : `/tag bitcoin` (en réponse à un graphique) → étiquette `BTC-USD`."""


#: Options d'effacement acceptées (`/tag --clear`).
TAG_CLEAR_FLAGS = ("--clear", "--effacer", "-c")


def parse_tag_args(args: Sequence[str]) -> Dict[str, Any]:
    """Décompose les arguments de `/tag`, ou explique pourquoi c'est faux.

    Deux formes seulement : `--clear` (effacer) et `<ACTIF>` (associer), chacune
    suivie d'une **référence** facultative. Une référence donnée en plus s'ajoute
    au message en réponse : elle sert quand il n'y a pas de message à désigner
    (média récupéré par le scraper de canal, par exemple).
    """
    tokens = [str(token) for token in (args or [])]
    if not tokens or tokens[0] in ("--help", "-h", "help"):
        return {"ok": False, "reason": "help"}
    if len(tokens) > 2:
        # Refuser plutôt qu'ignorer : `--asset`-like silencieux = arguments perdus.
        return {"ok": False, "reason": "too_many", "args": tokens}
    clear = tokens[0] in TAG_CLEAR_FLAGS
    return {
        "ok": True,
        "asset": None if clear else tokens[0],
        "clear": clear,
        "reference": tokens[1] if len(tokens) > 1 else None,
    }


async def find_media(
    *, 
    reference: Optional[str] = None,
    replied: Any = None,
    lookup: Callable[[str], Optional[Dict[str, Any]]] = media_store.find_media_by_telegram_file_id,
) -> Dict[str, Any]:
    """Identifie le média **déjà stocké** que désigne l'utilisateur, ou explique pourquoi pas.

    Deux commandes en ont besoin (`/tag`, `/transcribe`), et c'est la même
    question : réponse explicite à un message, ou référence donnée à la main.

    La réponse est le chemin naturel — l'utilisateur a le média sous les yeux et
    n'a aucun identifiant à copier. La référence explicite reste nécessaire pour
    les médias qui n'ont jamais transité par ce chat (`scrapers/telegram_channel.py`).
    """
    ref = str(reference or "").strip()
    if ref:
        # On ne valide pas l'identifiant ici : `tag_media` le résout en base et
        # dira lui-même « introuvable » — une seule source de vérité.
        return {"ok": True, "media_id": ref}

    if replied is None:
        return {"ok": False, "reason": "no_reference"}

    descriptor = extract_media(replied)
    if descriptor is None:
        return {"ok": False, "reason": "no_media_in_reply"}

    try:
        row = await asyncio.to_thread(lookup, descriptor["file_id"])
    except Exception as exc:
        return {"ok": False, "reason": "lookup_failed", "error": str(exc)}
    if not row:
        return {"ok": False, "reason": "not_found"}
    return {"ok": True, "media_id": str(_media_id(row))}


def _reference_problem(result: Optional[Dict[str, Any]], *, missing: str) -> str:
    """Préambule expliquant pourquoi le média désigné n'a pas pu être identifié.

    Partagé par `/tag` et `/transcribe` : les échecs sont **les mêmes** (ils
    viennent tous deux de `find_media`), et les réécrire de part et d'autre
    laisserait l'une des deux commandes mentir à la première évolution. `missing`
    est le seul texte que chaque commande apporte : la façon de désigner le média
    n'est pas la même selon ce qu'elle en fait.
    """
    result = result or {}
    reason = result.get("reason")
    if reason == "no_reference":
        return missing
    if reason == "no_media_in_reply":
        return "⚠️ Le message auquel tu réponds ne contient pas de média.\n\n"
    if reason == "not_found":
        return "⚠️ Ce média n'est pas en base (jamais ingéré, ou introuvable).\n\n"
    if reason == "lookup_failed":
        return f"❌ Recherche du média impossible : {result.get('error')}\n\n"
    return ""


def format_tag_help(result: Optional[Dict[str, Any]] = None) -> str:
    """Aide de `/tag`, préfacée du problème quand il y en a un."""
    prefix = ""
    if (result or {}).get("reason") == "too_many":
        prefix = "⚠️ Trop d'arguments : au plus un actif et une référence.\n\n"
    prefix += _reference_problem(
        result,
        missing=(
            "⚠️ Quel média ? Réponds au **message du média** avec `/tag BTC-USD`, "
            "ou donne sa référence : `/tag BTC-USD <référence>`.\n\n"
        ),
    )
    return prefix + TAG_HELP


async def tag_media(
    media_id: str,
    asset: Optional[str],
    *,
    source: str = "manual",
    reviewer: Optional[str] = None,
    fetch: Callable[[str], Optional[Dict[str, Any]]] = media_store.get_media,
    set_chunks: ChunkTagger = knowledge_index.set_chunks_asset,
    tag_store: Tagger = media_store.set_media_asset,
) -> Dict[str, Any]:
    """Associe un actif à un média déjà ingéré (`asset=None` efface l'étiquette).

    Deux écritures, et c'est la première qui compte : les **morceaux**
    (`knowledge_chunks.asset`) sont ce que lit le filtre préférentiel SQL ; la
    ligne média (`metadata.asset`) est ce qui permet de retrouver l'étiquette
    après coup, dans `/media` comme après un `↩️ Réindexer`. Les deux sont
    faites ici pour qu'elles ne puissent pas diverger.

    Aucune re-vectorisation : `asset` n'entre pas dans l'embeddings, et le
    `content` ne change pas. Aucune réextraction non plus — étiqueter un média
    dont l'extraction avait échoué reste utile, l'étiquette vaudra pour la
    prochaine tentative.

    Ne lève jamais : le handler doit pouvoir répondre dans tous les cas.
    """
    clean = normalize_asset(asset) if asset else None
    if asset and clean is None:
        return {"ok": False, "reason": "bad_asset", "asset": str(asset)}

    try:
        row = await asyncio.to_thread(fetch, media_id)
    except Exception as exc:
        return {"ok": False, "reason": "lookup_failed", "error": str(exc), "media_id": str(media_id)}
    if not row:
        return {"ok": False, "reason": "not_found", "media_id": str(media_id)}

    previous, _ = media_store.asset_tag(row)
    try:
        chunks = int(await asyncio.to_thread(set_chunks, media_id, clean))
    except Exception as exc:
        return {
            "ok": False,
            "reason": "chunks_failed",
            "error": str(exc),
            "media_id": str(media_id),
            "asset": clean,
        }

    stored = False
    try:
        await asyncio.to_thread(
            tag_store,
            media_id,
            clean,
            source=source if clean else None,
            reviewer=reviewer if clean else None,
        )
        stored = True
    except Exception as exc:
        # Les morceaux portent l'étiquette : la recherche la respecte déjà. Ce qui
        # manque, c'est sa trace sur la ligne média.
        print(f"   [medias] etiquette non enregistree ({media_id}) : {type(exc).__name__}: {exc}")

    return {
        "ok": True,
        "media_id": str(media_id),
        "asset": clean,
        "previous": previous,
        "chunks": chunks,
        "stored": stored,
        "source": source if clean else None,
        "reviewer": reviewer,
    }


def format_tag_report(result: Dict[str, Any]) -> str:
    """Message affiché après un `/tag` (étiquetage ou effacement)."""
    if not result.get("ok"):
        reason = result.get("reason")
        if reason == "not_found":
            return "⚠️ Média introuvable : rien n'a été étiqueté."
        if reason == "bad_asset":
            return (
                f"⚠️ « {result.get('asset')} » n'est pas un actif exploitable : utilise un "
                "symbole (`BTC-USD`, `AAPL`) ou un nom connu (`bitcoin`)."
            )
        if reason == "chunks_failed":
            return f"❌ Étiquetage impossible : {result.get('error')}"
        if reason == "lookup_failed":
            return f"❌ Lecture du média impossible : {result.get('error')}"
        return f"❌ Étiquetage impossible ({reason})."

    asset = result.get("asset")
    chunks = int(result.get("chunks") or 0)
    previous = result.get("previous")

    if asset is None:
        lines = [
            "🏷️ Étiquette retirée.",
            f"• {chunks} morceau(x) remis en « non étiqueté » : le média redevient "
            "candidat pour tous les actifs (sans bonus de classement).",
        ]
        if previous:
            lines.append(f"• Étiquette effacée : {previous}")
        return "\n".join(lines)

    lines = [f"🏷️ Actif associé : {asset}"]
    if previous and previous != asset:
        lines.append(f"• Remplace : {previous}")
    if chunks:
        lines.append(
            f"• {chunks} morceau(x) étiqueté(s) : les analyses de {asset} remonteront ce "
            "média en tête, et les analyses des autres actifs ne le verront plus."
        )
    else:
        lines.append(
            "• Aucun morceau indexé pour l'instant (extraction absente ou rejetée) : "
            "l'étiquette vaudra pour la prochaine extraction."
        )
    if not result.get("stored", True):
        lines.append(
            "  ⚠️ étiquette appliquée aux morceaux, mais non enregistrée sur le média"
        )
    return "\n".join(lines)


def _asset_lines(result: Dict[str, Any]) -> List[str]:
    """Ligne d'actif du compte-rendu : l'étiquette posée, ou comment la poser.

    Sans étiquette, le média reste le **joker** du filtre SQL (candidat pour tout
    actif, sans bonus). Ce n'est pas une erreur — mais c'est le cas où il faut le
    dire, parce que c'est le seul endroit où l'utilisateur peut le corriger, et
    parce qu'une détection ratée est invisible autrement.
    """
    extraction = result.get("extraction") or {}
    asset = str(extraction.get("asset") or "").strip()
    if not asset:
        return [
            "• Actif : non reconnu — le média reste candidat pour tous les actifs. "
            "Pour l'étiqueter : `/tag BTC-USD` en réponse au message du média."
        ]
    source = ASSET_SOURCE_LABELS.get(str(extraction.get("asset_source") or ""), "détection")
    lines = [f"• Actif associé : {asset} ({source})"]
    if not result.get("asset_stored", True):
        # Les morceaux portent bien l'étiquette (donc la recherche la respecte),
        # mais la ligne média n'a pas pu être écrite : le dire, sinon on croira
        # que `/tag --clear` ou une réindexation retrouvera cette étiquette.
        lines.append("  ⚠️ étiquette appliquée aux morceaux, mais non enregistrée sur le média")
    return lines


def _extract_lines(result: Dict[str, Any]) -> List[str]:
    """Ligne(s) du compte-rendu montrant le texte extrait.

    Deux cas, jamais de milieu : soit l'aperçu montre le texte **entier**, soit il
    annonce la pièce jointe qui le porte — plus d'aperçu tronqué, qui laissait
    croire à une extraction courte tout en cachant ce qu'on validait.
    """
    extraction = result.get("extraction") or {}
    attachment = attachment_for(result)
    if attachment:
        # Le nombre de caractères est déjà donné plus haut : le répéter ici
        # n'ajouterait rien, c'est le fichier qui manque au lecteur.
        return [
            f"• Texte extrait complet → pièce jointe .txt ({attachment['filename']})"
        ]
    excerpt = (extraction.get("excerpt") or "").strip()
    return [f"• Aperçu : {excerpt}"] if excerpt else []


def _channel_label(value: Any) -> str:
    """Nom de canal lisible : `@pseudo` pour un pseudo, la valeur brute sinon.

    Les deux routes n'écrivent pas la même chose dans `metadata` : le bot range le
    pseudo quand le canal en a un, le titre sinon, et le scraper public le nom de
    l'URL. Un titre (`Crypto Signals`) ou un identifiant numérique n'ont pas de
    `@` à porter — l'ajouter d'office afficherait `@Crypto Signals`, qui ne
    désigne rien de cliquable.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if " " in text or text.lstrip("-").isdigit():
        return text
    return text if text.startswith("@") else f"@{text}"


def channel_name(row: Optional[Dict[str, Any]]) -> Optional[str]:
    """Nom du **canal** d'où vient un média, ou `None` pour un chat privé.

    Deux écritures pour la même chose, selon la route d'ingestion : le bot range
    le canal dans `metadata.channel_post.channel` (le pseudo s'il existe, sinon le
    titre), le scraper public dans `metadata.channel` (le nom de `t.me/s/<nom>`).
    Les deux se lisent ici, dans cet ordre — c'est ce que `media_origin` affiche,
    et ce qui **regroupe** les attentes d'un même canal (`channel_bulk_key`).

    Une sous-clé `channel_post` présente mais sans nom rend `None` sans regarder
    `metadata.channel` : une publication de canal dont le canal n'est pas nommé
    n'est pas un aperçu du scraper, et l'appeler ainsi serait faux.
    """
    metadata = (row or {}).get("metadata") or {}
    if not isinstance(metadata, dict):
        return None
    post = metadata.get("channel_post")
    if isinstance(post, dict):
        return str(post.get("channel") or post.get("title") or "").strip() or None
    return str(metadata.get("channel") or "").strip() or None


def media_origin(row: Optional[Dict[str, Any]]) -> Optional[str]:
    """D'où vient un média de **canal**, ou `None` pour un média de chat privé.

    `/media` liste deux origines qui se ressemblent et qu'il ne faut pas
    confondre :

    * une publication ingérée par le bot (`metadata.channel_post`) — le fichier
      d'origine est dans le bucket, on sait de quel message il vient ;
    * un aperçu du scraper public (`metadata.channel` + `web_preview`) — la page
      `t.me/s/<canal>` ne donne qu'une URL d'image et un texte, jamais le
      document.

    Le second cas est signalé explicitement : sans ça, on relirait un aperçu
    comme s'il était la publication, et on croirait le fichier complet.
    """
    metadata = (row or {}).get("metadata") or {}
    if not isinstance(metadata, dict):
        return None

    name = channel_name(row)
    channel = _channel_label(name) if name else ""
    if not channel:
        return None
    if isinstance(metadata.get("channel_post"), dict):
        parts = [f"📡 {channel}"]
        message_id = metadata["channel_post"].get("message_id")
        if message_id:
            parts.append(f"msg {message_id}")
        return " · ".join(parts)
    return f"📡 {channel} · aperçu web" if metadata.get("web_preview") else f"📡 {channel}"


def _media_line(row: Dict[str, Any]) -> str:
    """Résumé d'un média : type, nom, origine, taille, date, verdict éventuel.

    L'origine (canal de publication, aperçu du scraper) est affichée ici parce que
    `/media` est le seul endroit d'où l'on relit après coup : sans elle, un média
    de canal et un média envoyé au bot sont indiscernables, alors qu'ils ne
    portent pas le même contenu (l'un a le fichier d'origine, l'autre un aperçu).

    Le verdict est affiché ici parce que c'est le seul endroit où l'on peut voir
    après coup ce qui a été relu — un média rejeté a zéro morceau indexé, il
    ressemblerait donc sinon à un média dont l'extraction a simplement échoué.
    """
    kind = row.get("media_type") or "média"
    name = row.get("file_name") or row.get("id") or "sans nom"
    created = str(row.get("created_at") or "")[:10]
    parts = [str(kind), str(name)]
    origin = media_origin(row)
    if origin:
        parts.append(origin)
    parts.append(_human_size(row.get("file_size")))
    if created:
        parts.append(created)
    # L'actif étiqueté est ce qui décide de la remontée du média dans un contexte
    # d'analyse : le voir dans la liste évite de croire un média « neutre » quand
    # il est en réalité exclu des recherches des autres actifs.
    asset = media_store.media_asset(row)
    if asset:
        parts.append(f"🏷️ {asset}")
    verdict = media_store.review_status(row)
    if verdict:
        parts.append(REVIEW_LABELS.get(verdict, verdict))
    # Un échec d'extraction est signalé ici, et **seulement** l'échec : dès qu'il y
    # a une légende, un média jamais lu a des morceaux indexés exactement comme un
    # média lu, donc sans cette marque les deux lignes seraient identiques et rien
    # ne dirait qu'il y a quelque chose à rattraper (`/transcribe`).
    outcome = media_store.extraction_outcome(row)
    if outcome is not None and not outcome.get("ok"):
        parts.append("⚠️ texte non extrait")
    return " · ".join(parts)


def _entry_lines(entries: Sequence[Dict[str, Any]]) -> List[str]:
    """Une ligne par média, suivie de son lien signé (ou de son absence).

    Partagé par `/media` et `/pending` : les deux listes affichent la même chose
    — le résumé de `_media_line` et le lien —, et le numéro doit rester celui de
    la ligne pour que les boutons (`✅ 3`) désignent le bon média.
    """
    lines: List[str] = []
    for index, entry in enumerate(entries, 1):
        row = entry.get("row") or {}
        url = entry.get("url")
        lines.append(f"\n{index}. {_media_line(row)}")
        lines.append(f"   {url}" if url else "   ⚠️ lien indisponible")
    return lines


def format_media_list(
    entries: List[Dict[str, Any]],
    *,
    expires_in: int = SIGNED_URL_TTL,
    offset: int = 0,
    more: bool = False,
) -> str:
    """Message Telegram listant des médias avec leur lien signé temporaire.

    Fonction **pure** : elle reçoit les entrées déjà résolues (`{"row", "url"}`),
    ce qui la rend testable sans réseau. Un lien manquant est signalé pour CE
    média, sans priver les autres du leur.

    `offset` est le **rang** de la première ligne affichée, `more` dit qu'une page
    suivante existe. Les deux sont annoncés : taire le rang ferait passer la
    troisième page pour le début de la liste, et une lecture **bornée** ne permet
    pas d'annoncer un reste qu'on n'a pas lu — d'où « d'autres suivent », sans
    nombre, là où `/pending` peut compter son solde. La première page sans suite
    garde son en-tête d'origine : c'est le cas courant, et il n'a rien à dire de
    plus.
    """
    if not entries:
        if offset:
            # Le rang demandé est au-delà de la fin : la liste a changé depuis son
            # affichage. Le dire, plutôt que d'annoncer « aucun média » — faux, lui
            # —, et rappeler le geste qui remonte. On ne calcule pas la dernière page
            # réelle, comme le fait `_pending_start` : cette lecture-ci est **bornée**
            # (`list_media`), donc la vue ne connaît pas le total, et le bouton de
            # retour reste la seule réponse honnête.
            return (
                "📭 Rien à ces rangs — la liste des médias a changé depuis son "
                "affichage. « ◀️ Précédents » remonte vers les plus récents."
            )
        return "📭 Aucun média stocké pour l'instant."
    minutes = max(1, int(expires_in) // 60)
    first = offset + 1
    last = offset + len(entries)
    if not offset and not more:
        header = f"🗂️ {len(entries)} dernier(s) média(s) — liens valables {minutes} min :"
    else:
        header = (
            f"🗂️ Derniers médias — lignes {first}–{last}, du plus récent au plus "
            f"ancien, liens valables {minutes} min :"
        )
    lines = [header]
    lines.extend(_entry_lines(entries))
    if more:
        lines.extend(
            [
                "",
                "… d'autres suivent, plus anciens — « ▶️ Suivants » va les chercher.",
            ]
        )
    return "\n".join(lines)


def media_page_data(offset: int) -> str:
    """`callback_data` du bouton qui mène à la page commençant à ce **rang**."""
    return f"{MEDIA_PAGE_PREFIX}:{max(0, int(offset))}"


def parse_media_page(data: Optional[str]) -> Optional[int]:
    """Rang porté par un `callback_data` de navigation de `/media`, ou `None`.

    Publique et séparée du handler, comme `parse_pending_page` : c'est la seule
    partie du bouton qui doit rester testable sans `python-telegram-bot`. Un rang
    qui n'est pas un entier positif est **refusé** plutôt que ramené à zéro — un
    bouton qu'on ne sait pas lire ne doit pas réafficher la première page en
    donnant l'impression d'avoir été compris.
    """
    parts = str(data or "").split(":", 1)
    if len(parts) != 2 or parts[0] != MEDIA_PAGE_PREFIX:
        return None
    try:
        offset = int(parts[1])
    except ValueError:
        return None
    return offset if offset >= 0 else None


def media_page_buttons(
    *, offset: int = 0, more: bool = False, page: int = DEFAULT_MEDIA_PAGE
) -> List[List[Tuple[str, str]]]:
    """Clavier de navigation de `/media` : Précédents / Suivants.

    Rien n'est affiché quand il n'y a nulle part où aller : un clavier qui ne
    mène nulle part laisserait croire qu'il reste des lignes. Les deux directions
    partagent **une** rangée — ce ne sont pas des lignes de la liste,
    contrairement aux boutons de `list_buttons` — et elle vient après elles.

    La suite à donner est dans `more`, et non dans un total comme pour
    `pending_buttons` : la lecture de `/media` est **bornée** (`list_media`), donc
    la vue ne connaît pas le nombre de médias de la table. Elle lit une ligne de
    plus que sa page, et c'est cette ligne qui répond : « ▶️ Suivants » n'est
    proposé que si une page suivante existe **vraiment**, plutôt que d'envoyer une
    fois sur deux sur une page vide.

    Aucun état n'est mémorisé : le bouton porte un rang, et la page est recalculée
    au clic. C'est le seul comportement honnête ici aussi, la liste changeant dès
    qu'une ingestion a lieu — une page figée montrerait d'autres médias que ceux
    qu'on croyait.
    """
    buttons: List[Tuple[str, str]] = []
    if offset > 0:
        buttons.append(("◀️ Précédents", media_page_data(offset - page)))
    if more:
        buttons.append(("▶️ Suivants", media_page_data(offset + page)))
    return [buttons] if buttons else []


def _indexed_media(rows: Sequence[Dict[str, Any]]) -> set:
    """Identifiants des médias listés qui ont du **texte indexé** (une requête).

    Le clavier a besoin de le savoir : un média sans morceau n'a rien à valider,
    et proposer « ✅ » dessus enregistrerait une relecture de rien — l'état
    trompeur que le module évite partout ailleurs.

    Une lecture impossible rend l'ensemble vide, donc « rien d'indexé » : le pire
    cas est de proposer une réindexation de trop, pas de croire une extraction
    relue à tort.

    La lecture est **découpée** en lots d'identifiants : `media_with_chunks` les
    passe dans le filtre `in.(…)` de l'URL PostgREST, dont la longueur est bornée.
    `/media` n'en envoie qu'une page, mais `/pending` interroge la **file entière**
    — ses lignes et ses lots par canal —, et une requête refusée pour cause de
    longueur rendrait « rien d'indexé » pour tout le monde d'un coup : la liste
    n'aurait plus aucun « ✅ », sans que rien ne distingue ce silence d'un backlog
    réellement vide. Découpée, la panne reste locale au lot qui la subit.
    """
    ids = [str(row.get("id")) for row in rows or [] if row.get("id")]
    if not ids:
        return set()
    found: set = set()
    for start in range(0, len(ids), INDEX_IDS_PER_QUERY):
        try:
            found |= set(
                knowledge_index.media_with_chunks(ids[start : start + INDEX_IDS_PER_QUERY])
            )
        except Exception as exc:
            print(f"   [medias] index illisible : {type(exc).__name__}: {exc}")
            return set()
    return found


def _resolve_entries(
    rows: Sequence[Dict[str, Any]],
    *,
    indexed: Optional[set] = None,
    expires_in: int = SIGNED_URL_TTL,
) -> List[Dict[str, Any]]:
    """Entrées d'une liste : la ligne, son lien signé, et « a-t-elle du texte ».

    Un lien raté n'est pas fatal — il est signalé pour CE média —, et la lecture
    de l'index qui a échoué rend « rien d'indexé » : le doute doit coûter une
    réindexation de trop, jamais une relecture crue à tort.

    `indexed` accepte un ensemble **déjà lu**, quand l'appelant a plus de lignes à
    juger que la page qu'il affiche : `pending_review_view` interroge l'index une
    fois pour toute la file, puis s'en sert pour les lignes affichées **et** pour
    les lots par canal — les deux ne peuvent donc pas se contredire sur ce qui a
    du texte.
    """
    indexed = _indexed_media(rows) if indexed is None else indexed
    entries: List[Dict[str, Any]] = []
    for row in rows or []:
        path = row.get("storage_path")
        url = None
        if path:
            try:
                url = media_store.create_signed_url(path, expires_in=expires_in)
            except Exception as exc:  # un lien raté ne doit pas masquer les autres
                print(f"   [medias] lien signe impossible ({path}) : {type(exc).__name__}: {exc}")
        entries.append(
            {
                "row": row,
                "url": url,
                # Sert au clavier : « il y a du texte à valider ou à retirer ».
                "indexed": str(row.get("id")) in indexed,
            }
        )
    return entries


def media_list_view(
    *,
    limit: int = DEFAULT_MEDIA_PAGE,
    offset: int = 0,
    source: Optional[str] = None,
    media_type: Optional[str] = None,
    expires_in: int = SIGNED_URL_TTL,
) -> Dict[str, Any]:
    """Message **et** clavier d'**une page** de `/media`, résolus en une lecture.

    Le bucket est privé : il n'existe pas d'URL publique, donc chaque fichier est
    exposé par un lien temporaire qui expire (voir `create_signed_url`).

    `offset` est le rang de la première ligne de la page — celui que porte le bouton
    de navigation —, et rien n'est mémorisé entre deux clics : la page se
    **recalcule**, parce que des ingestions s'ajoutent en tête de liste. Le rang
    s'appuie sur l'ordre **total** de `list_media` (`created_at` puis `id`) : deux
    médias d'un même album partagent leur date à la seconde, et un tri non unique
    ferait apparaître une ligne sur deux pages en en cachant une autre.

    La lecture demande **une ligne de plus** que la page : c'est elle qui dit si
    « ▶️ Suivants » a quelque part où mener (`more`). Elle n'est ni signée ni
    résolue — seules les lignes affichées le sont, un lien signé coûtant un appel
    par média.

    Retourne `{"text", "keyboard", "entries", "offset", "more"}` :

    * `text` — ce que le message doit dire, déjà formaté ;
    * `keyboard` — rangées de `(libellé, callback_data)` que le handler transforme
      en `InlineKeyboardMarkup` (ce module n'importe pas `python-telegram-bot`) :
      une rangée par média, puis la navigation ;
    * `entries` — les entrées résolues, dont le texte a été tiré ;
    * `offset` / `more` — le rang tenu, et s'il reste une page après.

    Bloquant (réseau) : à appeler hors de l'event loop. Ne lève jamais — une
    lecture impossible devient un message d'erreur **et un clavier vide** : des
    boutons pointant sur des lignes qu'on n'a pas pu lire seraient pires que pas
    de boutons.
    """
    wanted = max(1, min(int(limit), MAX_MEDIA_PAGE))
    start = max(0, int(offset))
    try:
        rows = media_store.list_media(
            source=source, media_type=media_type, limit=wanted + 1, offset=start
        )
    except Exception as exc:
        error = f"❌ Lecture des médias impossible : {type(exc).__name__}: {exc}"
        return {
            "text": error,
            "keyboard": [],
            "entries": [],
            "offset": start,
            "more": False,
        }

    rows = list(rows or [])
    more = len(rows) > wanted
    entries = _resolve_entries(rows[:wanted], expires_in=expires_in)
    review = list_buttons(entries)
    keyboard = review + media_page_buttons(offset=start, more=more, page=wanted)
    text = format_media_list(entries, expires_in=expires_in, offset=start, more=more)
    if review:
        text = f"{text}\n\n{LIST_REVIEW_HINT}"
    return {
        "text": text,
        "keyboard": keyboard,
        "entries": entries,
        "offset": start,
        "more": more,
    }


def media_list_report(
    *,
    limit: int = DEFAULT_MEDIA_PAGE,
    source: Optional[str] = None,
    media_type: Optional[str] = None,
    expires_in: int = SIGNED_URL_TTL,
) -> str:
    """Texte seul de `/media`, pour les appelants qui n'ont pas de boutons."""
    return media_list_view(
        limit=limit, source=source, media_type=media_type, expires_in=expires_in
    )["text"]


def format_pending_review(
    entries: List[Dict[str, Any]],
    *,
    total: int,
    offset: int = 0,
    expires_in: int = SIGNED_URL_TTL,
) -> str:
    """Message d'**une page** de `/pending` : les extractions **sans verdict**.

    Fonction **pure**, comme `format_media_list`. `total` est le nombre d'attentes
    trouvées par le balayage **entier**, `len(entries)` celles de cette page : les
    deux sont annoncés, parce que taire la différence ferait croire que la revue
    est finie quand il resterait des lignes plus anciennes. Et `offset` dit **où**
    l'on se trouve dans cette liste — le taire ferait passer la troisième page pour
    le début de la file.

    L'ordre est celui du balayage, de la plus récente à la plus ancienne : le rang
    affiché est donc un rang dans cet ordre, et « ▶️ Suivants » va vers le plus
    ancien.
    """
    if not total:
        return (
            "✅ Aucune extraction en attente de revue : tout ce qui est stocké a "
            "reçu un verdict."
        )
    minutes = max(1, int(expires_in) // 60)
    first = offset + 1
    last = offset + len(entries)
    if first == 1 and last >= total:
        header = (
            f"🕵️ {total} extraction(s) sans verdict — de la plus récente à la plus "
            f"ancienne, liens valables {minutes} min :"
        )
    else:
        header = (
            f"🕵️ {total} extraction(s) sans verdict — lignes {first}–{last}, de la "
            f"plus récente à la plus ancienne, liens valables {minutes} min :"
        )
    lines = [header]
    lines.extend(_entry_lines(entries))
    remaining = total - last
    if remaining > 0:
        lines.extend(
            [
                "",
                f"… et {remaining} autre(s), plus ancienne(s) — « ▶️ Suivants » va les "
                "chercher, et un verdict rendu ici fait aussi remonter la file.",
            ]
        )
    return "\n".join(lines)


def pending_page_data(offset: int) -> str:
    """`callback_data` du bouton qui mène à la page commençant à ce **rang**."""
    return f"{PENDING_PAGE_PREFIX}:{max(0, int(offset))}"


def parse_pending_page(data: Optional[str]) -> Optional[int]:
    """Rang porté par un `callback_data` de navigation de `/pending`, ou `None`.

    Publique et séparée du handler, comme `parse_transcribe_page` : c'est la seule
    partie du bouton qui doit rester testable sans `python-telegram-bot`. Un rang
    qui n'est pas un entier positif est refusé plutôt que ramené à zéro — un bouton
    qu'on ne sait pas lire ne doit pas réafficher la première page en donnant
    l'impression d'avoir été compris.
    """
    parts = str(data or "").split(":", 1)
    if len(parts) != 2 or parts[0] != PENDING_PAGE_PREFIX:
        return None
    try:
        offset = int(parts[1])
    except ValueError:
        return None
    return offset if offset >= 0 else None


def pending_buttons(
    *, offset: int = 0, shown: int = 0, total: int = 0, page: int = PENDING_PAGE
) -> List[List[Tuple[str, str]]]:
    """Clavier de navigation de `/pending` : Précédents / Suivants.

    Rien n'est affiché quand tout tient sur une page : un clavier qui ne mène nulle
    part laisserait croire qu'il reste des lignes. Les deux directions partagent
    **une** rangée, et elle vient après les rangées de revue — ce ne sont pas des
    lignes de la liste, contrairement aux boutons de `list_buttons`.

    Aucun état n'est mémorisé : le bouton porte un rang, et la page est recalculée
    au clic. C'est le seul comportement honnête ici aussi, la file changeant dès
    qu'un verdict est rendu.
    """
    buttons: List[Tuple[str, str]] = []
    if offset > 0:
        buttons.append(("◀️ Précédents", pending_page_data(offset - page)))
    if offset + shown < total:
        buttons.append(("▶️ Suivants", pending_page_data(offset + page)))
    return [buttons] if buttons else []


def _pending_start(offset: int, *, total: int, page: int) -> int:
    """Rang de départ de la page demandée, ramené **dans les bornes** de la liste.

    Un rang qui a dépassé la fin n'est pas une erreur : la liste change sous les
    doigts. Un verdict rendu fait tomber des lignes, et une page 7 peut devenir la
    dernière page d'une file qui n'en compte plus que quatre. Afficher une page
    vide serait exact — « il n'y a plus rien ici » — mais muet sur ce qu'il reste :
    on revient donc sur la dernière page réelle.
    """
    start = max(0, int(offset))
    if start < total:
        return start
    return max(0, total - max(1, int(page)))


def pending_review_view(
    *, limit: int = PENDING_PAGE, offset: int = 0, expires_in: int = SIGNED_URL_TTL
) -> Dict[str, Any]:
    """Message **et** clavier d'**une page** de `/pending`, en une seule lecture.

    La question à laquelle `/media` ne peut pas répondre : « qu'est-ce qui attend
    encore une relecture ? ». `/media` borne sa lecture aux derniers médias, donc une
    extraction ancienne jamais relue en disparaît dès qu'il y a eu dix ingestions
    depuis. Ici la table est parcourue **en entier** (`list_pending_review`),
    sans borne d'âge, puis les lignes **sans verdict** sont affichées — les plus
    récentes d'abord —, à concurrence de `limit`.

    `offset` est le rang de la première ligne de la page : c'est ce que porte le
    bouton de navigation, et rien n'est mémorisé entre deux clics. Parcourir la
    file ne demande donc **aucun verdict** — « ▶️ Suivants » et « ◀️ Précédents »
    ne font que relire la vue au rang demandé.

    Restitue `{"text", "keyboard", "entries", "channels", "total", "shown",
    "offset"}`, même contrat que `transcribe_candidates`. Le clavier est celui
    d'une **liste** (`/pending`) : rendre un verdict ici ne remplace pas la vue, il la
    **rafraîchit**, où la ligne traitée a disparu et où la suivante a pris sa
    place. Viennent ensuite, dans l'ordre, la rangée de navigation
    (`pending_buttons`) puis les rangées de **lot par canal**
    (`channel_bulk_buttons`) : elles portent sur des lignes qu'on n'affiche pas
    forcément (`channels`, calculé sur la file entière), donc sur un cran plus
    large que la page — d'où leur place à la fin.

    Bloquant (réseau) : à appeler hors de l'event loop. Ne lève jamais — une
    lecture impossible devient un message d'erreur **et un clavier vide**.
    """
    wanted = max(1, min(int(limit), MAX_PENDING_PAGE))
    try:
        pending = media_store.list_pending_review()
    except Exception as exc:
        error = f"❌ Lecture des médias impossible : {type(exc).__name__}: {exc}"
        return {
            "text": error,
            "keyboard": [],
            "entries": [],
            "channels": [],
            "total": 0,
            "shown": 0,
            "offset": 0,
        }

    pending = list(pending or [])
    start = _pending_start(offset, total=len(pending), page=wanted)
    shown = pending[start : start + wanted]
    # L'index est lu **une fois** pour toute la file : il décide des boutons des
    # lignes affichées comme de ceux des lots par canal, qui portent sur des
    # lignes qu'on n'affiche pas (les pages suivantes).
    indexed = _indexed_media(pending)
    entries = _resolve_entries(shown, indexed=indexed, expires_in=expires_in)
    channels = pending_channel_groups(pending, indexed=indexed)
    keyboard = list_buttons(entries, prefix=PENDING_REVIEW_PREFIX)
    keyboard += pending_buttons(
        offset=start, shown=len(shown), total=len(pending), page=wanted
    )
    keyboard += channel_bulk_buttons(channels)
    text = format_pending_review(
        entries, total=len(pending), offset=start, expires_in=expires_in
    )
    if keyboard:
        text = f"{text}\n\n{LIST_REVIEW_HINT}"
    return {
        "text": text,
        "keyboard": keyboard,
        "entries": entries,
        "channels": channels,
        "total": len(pending),
        "shown": len(shown),
        "offset": start,
    }


# --------------------------------------------------------------------------- #
# Album : un compte-rendu pour tout l'envoi, une ligne par élément
# --------------------------------------------------------------------------- #

#: Cause d'échec d'un élément → mot lisible. Le compte-rendu d'un média **seul**
#: explique longuement (`format_report`) ; dans un lot il faut tenir sur une ligne,
#: mais nommer la cause reste indispensable : c'est elle qui dit s'il y a quelque
#: chose à refaire.
FAILURE_LABELS = {
    "no_media": "aucun média dans le message",
    "too_large": "trop volumineux",
    "download_failed": "téléchargement impossible",
    "upload_failed": "stockage impossible",
    "exception": "panne inattendue",
}

#: Longueur maximale des détails d'échec et des aperçus dans un album (caractères).
#: Un message Telegram en porte 4096, et un album jusqu'à dix éléments : chaque
#: élément tient donc sur une ligne et un aperçu, jamais sur un compte-rendu
#: complet. Le texte entier, lui, ne passe pas par ce chemin (il part en `.txt`).
ALBUM_DETAIL_CHARS = 120


def album_counts(report: Dict[str, Any]) -> Dict[str, int]:
    """`{"stored", "duplicates", "failed"}` d'un lot — une seule définition.

    Chaque élément tombe dans **une** catégorie et une seule : les trois comptes
    font donc le nombre d'éléments. Le compte-rendu et son clavier lisent les
    mêmes, ce qui évite qu'une ligne dise « enregistré » là où le bouton propose
    de refaire l'extraction.
    """
    counts = {"stored": 0, "duplicates": 0, "failed": 0}
    for result in report.get("results") or []:
        if not result.get("ok"):
            counts["failed"] += 1
        elif result.get("duplicate"):
            counts["duplicates"] += 1
        else:
            counts["stored"] += 1
    return counts


def _short(value: Any, limit: int = ALBUM_DETAIL_CHARS) -> str:
    """Détail borné : dix messages d'erreur entiers ne tiendraient pas dans un album.

    Les retours à la ligne sont repliés en espaces : sinon un motif d'erreur
    multiligne casserait la numérotation du compte-rendu, seul repère entre une
    ligne et son bouton.
    """
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1]}…"


def _album_asset(result: Dict[str, Any]) -> str:
    """Étiquette d'actif affichée pour un élément, ou chaîne vide.

    Pour un média **ingéré à l'instant**, la détection du jour fait foi. Pour un
    doublon, il n'y a pas d'extraction neuve : on lit l'étiquette de sa ligne, qui
    existe déjà — sans quoi un doublon étiqueté passerait pour un média neutre.
    """
    extraction = result.get("extraction") or {}
    asset = str(extraction.get("asset") or "").strip()
    if asset:
        source = str(extraction.get("asset_source") or "")
        return f"{asset} ({ASSET_SOURCE_LABELS.get(source, 'détection')})"
    row_asset = media_store.media_asset(result.get("media") or {})
    return str(row_asset) if row_asset else ""


def _album_preview(result: Dict[str, Any]) -> Optional[str]:
    """Aperçu d'un élément, ramené à une ligne, ou `None`.

    Même politique que le compte-rendu d'un média seul, vue depuis un lot : soit
    le texte part en `.txt` (et la ligne le dit déjà), soit il est montré — mais
    borné, puisqu'un album en porte jusqu'à dix. Un aperçu tronqué n'est pas une
    entorse : la revue d'une extraction longue passe par la pièce jointe.
    """
    if attachment_for(result) is not None:
        return None
    raw = str((result.get("extraction") or {}).get("excerpt") or "")
    preview = " ".join(raw.split())
    if not preview:
        return None
    return preview if len(preview) <= ALBUM_DETAIL_CHARS else f"{preview[:ALBUM_DETAIL_CHARS]}…"


def _album_element_line(result: Dict[str, Any]) -> str:
    """Une ligne pour un élément : ce qui lui est arrivé, et de quoi le relire après.

    La **référence** est sur la ligne, et pas seulement dans un pied de message :
    c'est elle qui permet de rattraper un élément précis (`/transcribe <référence>`)
    alors que la réponse au compte-rendu ne désigne, elle, que le dernier élément
    de l'album — la réponse est sous le dernier message, c'est là que `/tag` et
    `/transcribe` la lisent.
    """
    parts = [str(result.get("media_type") or "média")]
    media = result.get("media") or {}

    if result.get("duplicate"):
        if result.get("duplicate_reason") == "message":
            parts.append("⏭️ déjà ingéré depuis ce canal")
        else:
            parts.append("⏭️ déjà stocké")
        if result.get("caption_ignored"):
            parts.append("légende non indexée")
    elif not result.get("ok"):
        reason = str(result.get("reason") or "inconnue")
        label = FAILURE_LABELS.get(reason, f"échec ({reason})")
        detail = ""
        if reason == "too_large":
            detail = f" ({result.get('size', 0) / 1_000_000:.1f} Mo)"
        elif result.get("error"):
            detail = f" ({_short(result.get('error'))})"
        parts.append(f"❌ {label}{detail}")
    else:
        if result.get("bytes"):
            parts.append(_human_size(result.get("bytes")))
        extraction = result.get("extraction") or {}
        if extraction.get("ok"):
            if attachment_for(result) is not None:
                parts.append("✅ texte extrait → .txt")
            else:
                parts.append(f"✅ texte extrait ({extraction.get('chunks', 0)} morceau(x))")
        else:
            parts.append(f"⚠️ texte non extrait ({_short(extraction.get('reason') or 'sans extraction')})")
            #: Une légende indexée toute seule n'est pas une extraction nulle :
            #: c'est même souvent elle qui nomme l'actif.
            if extraction.get("chunks"):
                parts.append("légende indexée seule")

    asset = _album_asset(result)
    if asset:
        parts.append(f"🏷️ {asset}")
    if media.get("id"):
        parts.append(str(media["id"]))
    return " · ".join(parts)


def format_album_report(report: Dict[str, Any]) -> str:
    """Compte-rendu d'un **album** : une ligne par élément, numérotée.

    La numérotation n'est pas décorative : c'est elle qui relie une ligne au
    bouton de revue (`✅ 3`), et les deux sont tirés des **mêmes** entrées, dans le
    même ordre (`album_entries`). Un élément en échec garde son rang, même sans
    bouton — sinon le bouton 3 désignerait la ligne 4.
    """
    results = list(report.get("results") or [])
    if not results:
        return "🗂️ Album vide : aucun élément à ingérer."
    total = int(report.get("count") or len(results))
    counts = album_counts(report)
    summary = []
    if counts["stored"]:
        summary.append(f"{counts['stored']} enregistré(s)")
    if counts["duplicates"]:
        summary.append(f"{counts['duplicates']} déjà stocké(s)")
    if counts["failed"]:
        summary.append(f"{counts['failed']} en échec")
    header = f"🗂️ Album — {total} média(s)"
    lines = [f"{header} : {', '.join(summary)}" if summary else header]

    for rank, result in enumerate(results, 1):
        lines.append(f"{rank}. {_album_element_line(result)}")
        preview = _album_preview(result)
        if preview:
            lines.append(f"   ⤷ {preview}")

    #: L'absence d'étiquette n'est pas une erreur — le média reste le joker du
    #: filtre d'actif —, mais c'est le seul endroit où on peut la corriger.
    untagged = [r for r in results if r.get("ok") and not _album_asset(r)]
    if untagged:
        lines.append(
            "ℹ️ Sans étiquette, un média reste candidat pour tous les actifs : "
            "`/tag BTC-USD` en réponse à son message."
        )
    return "\n".join(lines)


def album_entries(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Entrées du clavier de revue : **une par ligne** du compte-rendu, dans l'ordre.

    L'ordre et la place comptent : `list_buttons` numérote par position, donc un
    élément en échec — sans ligne média — doit garder la sienne. Il n'aura
    simplement pas de bouton, là où ses voisins en auront.

    Une seule requête pour tout le lot (`media_with_chunks`), comme `/media` : le
    clavier a besoin de savoir ce qui est indexé, et le demander dix fois pour un
    album coûterait dix allers-retours. Bloquant (réseau) : à appeler hors de
    l'event loop.
    """
    rows = [(result.get("media") or {}) for result in report.get("results") or []]
    indexed = _indexed_media(rows)
    return [
        {"row": row, "indexed": bool(row.get("id")) and str(row.get("id")) in indexed}
        for row in rows
    ]


def album_report_view(report: Dict[str, Any]) -> Dict[str, Any]:
    """Message **et** clavier d'un album, résolus en une seule lecture.

    Même contrat que `media_list_view` : `{"text", "keyboard"}`, tirés des mêmes
    entrées — donc les numéros des boutons sont ceux des lignes. Le clavier est
    celui d'une **liste** (`LIST_REVIEW_PREFIX`) : un verdict rendu ici ne doit pas
    remplacer le compte-rendu de l'album, sinon les boutons des éléments pas encore
    relus disparaîtraient avec lui. C'est exactement la raison qui a fait
    distinguer les deux préfixes.

    Bloquant (une requête) : à appeler hors de l'event loop.
    """
    entries = album_entries(report)
    keyboard = list_buttons(entries)
    text = format_album_report(report)
    if keyboard:
        text = f"{text}\n\n{LIST_REVIEW_HINT}"
    return {"text": text, "keyboard": keyboard}


# --------------------------------------------------------------------------- #
# Revue d'une extraction : boutons valider / rejeter / réindexer
# --------------------------------------------------------------------------- #

def review_target(result: Dict[str, Any]) -> Optional[str]:
    """Identifiant du média à revoir, ou `None` si la revue n'a pas de sens.

    On ne propose la revue que s'il y a **quelque chose à garder ou à retirer** :
    une ingestion en échec, un doublon reconnu ou une extraction qui n'a indexé
    aucun morceau n'ont rien produit à valider. Afficher des boutons sans effet
    ferait douter de tous les autres.
    """
    if not result.get("ok") or result.get("duplicate"):
        return None
    media_id = _media_id(result.get("media"))
    if not media_id:
        return None
    extraction = result.get("extraction") or {}
    if not extraction.get("chunks"):
        return None
    return str(media_id)


def review_callback_data(media_id: str, verdict: str, *, prefix: str = REVIEW_PREFIX) -> str:
    """`callback_data` d'un bouton de revue (`med:ok:<uuid>`).

    `prefix` distingue **d'où** le clic vient (`LIST_REVIEW_PREFIX` pour une ligne
    de `/media`) : c'est la même action, mais le message à réécrire n'est pas le
    même, et le bouton est le seul endroit qui le sait.
    """
    if verdict not in REVIEW_VERDICTS:
        raise ValueError(f"verdict inconnu : {verdict!r} (attendu : {', '.join(REVIEW_VERDICTS)})")
    return f"{prefix}:{verdict}:{media_id}"


def parse_review_callback(
    data: Optional[str], *, prefix: str = REVIEW_PREFIX
) -> Optional[Tuple[str, str]]:
    """`(verdict, media_id)` d'un `callback_data`, ou `None` s'il n'est pas à nous.

    Publique et séparée du handler : c'est la seule partie du bouton qui doit
    rester testable sans `python-telegram-bot` (ce module ne l'importe pas). Le
    préfixe est un paramètre parce que deux claviers produisent des boutons de
    revue (compte-rendu d'ingestion, ligne de `/media`) et qu'un handler ne doit
    accepter que les siens.
    """
    parts = str(data or "").split(":", 2)
    if len(parts) != 3 or parts[0] != prefix or parts[1] not in REVIEW_VERDICTS:
        return None
    if not parts[2].strip():
        return None
    return parts[1], parts[2]


def review_buttons(media_id: str) -> List[Tuple[str, str]]:
    """Boutons de revue d'une extraction : `(libellé, callback_data)`.

    Le handler les transforme en clavier ; les construire ici garde le module
    sans dépendance à `python-telegram-bot`. Le rejet annonce sa conséquence
    (« retirer de l'index ») : c'est le seul bouton du bot qui détruit quelque
    chose, il ne doit pas se découvrir après le clic.
    """
    return [
        ("✅ Extraction correcte", review_callback_data(media_id, "ok")),
        ("❌ Rejeter (retirer de l'index)", review_callback_data(media_id, "no")),
    ]


def reject_button(media_id: str) -> List[Tuple[str, str]]:
    """Bouton de rejet seul — ce qui reste après une validation."""
    return [("❌ Rejeter (retirer de l'index)", review_callback_data(media_id, "no"))]


def restore_buttons(media_id: str) -> List[Tuple[str, str]]:
    """Bouton de retour en arrière après un rejet.

    Un rejet est destructeur (les morceaux sont supprimés) et Telegram ne peut
    pas le rejouer : le fichier est déjà connu, donc le renvoyer ne réindexerait
    rien (voir la déduplication). Sans ce bouton, un clic de trop serait définitif.
    """
    return [("↩️ Réindexer", review_callback_data(media_id, "re"))]


def follow_up_buttons(result: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Boutons à afficher **après** un verdict, sachant ce qu'il vient de faire.

    La politique est ici, pas dans le handler, pour deux raisons : elle dépend
    étroitement du compte-rendu (`review_media`), et c'est la partie la plus
    facile à casser sans s'en apercevoir — un clavier qui disparaît là où il
    faudrait pouvoir revenir en arrière, ou l'inverse.

    Les règles :

    * média introuvable → aucun bouton : aucun verdict ne pourrait aboutir ;
    * travail **refusé avant d'être commencé** (`not_ready`, propre à
      `/transcribe`) → aucun bouton non plus : il n'y a rien à relire, et le
      bouton « ↩️ » ne ferait pas mieux que la commande qui vient de refuser ;
    * action **échouée** → on rend le clavier complet, pour réessayer ;
    * ``no`` réussi → le bouton « Réindexer », la seule sortie d'un rejet ;
    * ``re`` réussi avec des morceaux → le clavier complet : c'est une extraction
      **neuve**, elle doit être relue comme telle (et peut encore être rejetée) ;
    * ``re`` réussi sans morceaux (texte illisible) → « Réindexer » : rien n'a été
      indexé, il ne reste qu'à retenter ;
    * ``ok`` réussi → le rejet seul : valider n'est pas irréversible, on garde de
      quoi défaire un clic de trop.
    """
    media_id = str(result.get("media_id") or "")
    if not media_id:
        return []
    if not result.get("ok"):
        if result.get("reason") in ("not_found", "not_ready"):
            return []
        return review_buttons(media_id)

    verdict = result.get("verdict")
    if verdict == "no":
        return restore_buttons(media_id)
    if verdict == "re":
        return review_buttons(media_id) if result.get("chunks") else restore_buttons(media_id)
    if verdict == "ok":
        return reject_button(media_id)
    return []


def list_callback_data(
    media_id: str, verdict: str, *, prefix: str = LIST_REVIEW_PREFIX
) -> str:
    """`callback_data` d'un bouton de revue **de liste** (`medl:ok:<uuid>`).

    `prefix` distingue **de quelle** liste le clic vient (`/media`, `/pending`) :
    c'est la même action, mais le message à réafficher n'est pas le même.
    """
    return review_callback_data(media_id, verdict, prefix=prefix)


def list_buttons(
    entries: Sequence[Dict[str, Any]], *, prefix: str = LIST_REVIEW_PREFIX
) -> List[List[Tuple[str, str]]]:
    """Clavier de revue d'une **liste** de médias : une rangée par média.

    Le numéro porté par le bouton est celui de la ligne du message : c'est le seul
    repère qui relie un bouton à ce qu'il va changer, et il n'est donc pas
    décoratif — `format_media_list` numérote dans le même ordre, à partir des
    **mêmes entrées**.

    La politique reprend celle de `follow_up_buttons`, mais vue depuis l'état de
    la ligne plutôt que depuis le verdict qui vient d'être rendu :

    * **rien d'indexé** (extraction vide, ou rejetée) → « ↩️ » seul : il n'y a rien
      à valider, et le seul progrès possible est de refaire l'extraction depuis le
      fichier stocké ;
    * **déjà validé** → « ❌ » seul : valider n'est pas irréversible, on garde de
      quoi défaire un clic de trop ;
    * sinon → « ✅ » et « ❌ ».

    Un média sans identifiant est sauté : son bouton ne pourrait rien désigner.

    `prefix` ne change que le préfixe des `callback_data` : la politique des
    boutons, elle, ne dépend pas de la liste d'où l'on regarde.
    """
    rows: List[List[Tuple[str, str]]] = []
    for rank, entry in enumerate(entries, 1):
        row = entry.get("row") or {}
        media_id = _media_id(row)
        if not media_id:
            continue
        media_id = str(media_id)
        verdict = media_store.review_status(row)
        if not entry.get("indexed") or verdict == "rejected":
            rows.append([(f"↩️ {rank}", list_callback_data(media_id, "re", prefix=prefix))])
        elif verdict == "validated":
            rows.append([(f"❌ {rank}", list_callback_data(media_id, "no", prefix=prefix))])
        else:
            rows.append(
                [
                    (f"✅ {rank}", list_callback_data(media_id, "ok", prefix=prefix)),
                    (f"❌ {rank}", list_callback_data(media_id, "no", prefix=prefix)),
                ]
            )
    return rows


def review_toast(result: Dict[str, Any]) -> str:
    """Confirmation courte (`query.answer`) après un verdict cliqué dans une liste.

    Un verdict rendu sur une ligne ne remplace pas la liste : le compte-rendu de
    `format_review_report` (plusieurs lignes) n'y a pas sa place. Mais un clic sans
    réponse laisserait croire qu'il n'a rien fait — donc court, et seulement ce qui
    a changé. Telegram coupe au-delà de 200 caractères.
    """
    verdict = result.get("verdict")
    if not result.get("ok"):
        return f"⚠️ Revue impossible : {result.get('reason') or 'raison inconnue'}"
    if verdict == "no":
        return f"❌ Rejeté : {result.get('removed', 0)} morceau(x) retiré(s) de l'index."
    if verdict == "re":
        chunks = int(result.get("chunks") or 0)
        if not chunks:
            return "↩️ Réindexé : aucun texte n'a pu être extrait."
        return f"↩️ Réindexé : {chunks} morceau(x) indexé(s)."
    return "✅ Extraction validée et enregistrée."


async def reprocess_media(
    media_id: str,
    *,
    row: Optional[Dict[str, Any]] = None,
    reviewer: Optional[str] = None,
    fetch: Callable[[str], Optional[Dict[str, Any]]] = media_store.get_media,
    download: Callable[[str], bytes] = media_store.download_media,
    extract: Extractor = media_extractor.extract_text,
    store: ChunkStore = media_store.replace_media_chunks,
    tag_store: Tagger = media_store.set_media_asset,
    mark: Callable[..., Dict[str, Any]] = media_store.set_review_status,
    record: Recorder = media_store.set_extraction_outcome,
) -> Dict[str, Any]:
    """Refait l'extraction depuis le **fichier stocké**, puis réindexe.

    Le cœur commun de deux gestes qui ne doivent pas diverger : le bouton
    « ↩️ Réindexer » d'une revue, et `/transcribe` — le rattrapage d'une ingestion
    faite sans la clef qu'il fallait. Le second vérifie ses ingrédients avant de
    commencer et la présentation diffère, mais le travail (relire l'objet,
    extraire, indexer, reposer l'étiquette, reposer le verdict) est ici, et une
    seule fois.

    `row` évite une seconde lecture quand l'appelant a déjà la ligne — les deux
    l'ont : la revue l'a lue pour appliquer son verdict, `/transcribe` pour son
    diagnostic. Sans elle, la fonction la lit elle-même.

    **L'étiquette d'actif survit** : celle de la ligne média est réappliquée (un
    choix manuel ne doit pas être perdu par une réindexation), et une détection
    neuve — quand il n'y en avait aucune — est enregistrée. Le verdict, lui, est
    reposé à « validée » : c'est ce qu'une reprise **veut dire**, et c'est
    pourquoi `reprocess_media` ne s'appelle pas à l'insu de personne.

    Ne lève jamais : les handlers doivent pouvoir répondre dans tous les cas.
    """
    if row is None:
        try:
            row = await asyncio.to_thread(fetch, media_id)
        except Exception as exc:
            return {
                "ok": False,
                "reason": "lookup_failed",
                "error": str(exc),
                "media_id": str(media_id),
            }
        if not row:
            return {"ok": False, "reason": "not_found", "media_id": str(media_id)}

    path = row.get("storage_path")
    if not path:
        return {"ok": False, "reason": "no_object", "media_id": str(media_id)}
    try:
        data = await asyncio.to_thread(download, path)
    except Exception as exc:
        return {
            "ok": False,
            "reason": "download_failed",
            "error": str(exc),
            "media_id": str(media_id),
        }

    stored_asset, stored_source = media_store.asset_tag(row)
    extraction = await _extract_and_index(
        data,
        _descriptor_from_row(row),
        media_id=media_id,
        extract=extract,
        store=store,
        record=record,
        asset=stored_asset,
        asset_source=stored_source,
    )
    chunks = int(extraction.get("chunks") or 0)
    # L'étiquette enregistrée sur la **ligne** est réappliquée telle quelle :
    # `media_asset` lit la forme tracée (`{"value", "source"}`) que `asset_tag`
    # déplie, et `media_indexing.store_asset` la réécrit dans cette forme.
    asset_stored = bool(media_store.media_asset(row))
    if not asset_stored and extraction.get("asset"):
        asset_stored = await _store_asset(
            media_id,
            extraction.get("asset"),
            extraction.get("asset_source"),
            tag_store,
            reviewer=reviewer,
        )
    try:
        updated = await asyncio.to_thread(
            mark, media_id, "validated", reviewer=reviewer, chunks=chunks
        )
    except Exception as exc:
        # Les morceaux sont écrits : le verdict est une annotation, son échec ne
        # doit pas faire croire que rien n'a été refait.
        print(f"   [medias] verdict non enregistre ({media_id}) : {type(exc).__name__}: {exc}")
        updated = None
    return {
        "ok": True,
        "media_id": str(media_id),
        "media": updated or row,
        "extraction": extraction,
        "chunks": chunks,
        "asset_stored": asset_stored,
        "status": "validated",
    }


async def review_media(
    media_id: str,
    verdict: str,
    *,
    reviewer: Optional[str] = None,
    fetch: Callable[[str], Optional[Dict[str, Any]]] = media_store.get_media,
    download: Callable[[str], bytes] = media_store.download_media,
    extract: Extractor = media_extractor.extract_text,
    store: ChunkStore = media_store.replace_media_chunks,
    record: Recorder = media_store.set_extraction_outcome,
    remove: Callable[[str], int] = knowledge_index.delete_chunks,
    mark: Callable[..., Dict[str, Any]] = media_store.set_review_status,
    tag_store: Tagger = media_store.set_media_asset,
) -> Dict[str, Any]:
    """Applique un verdict de revue à l'extraction d'un média.

    * ``ok`` — l'extraction est correcte : elle reste indexée, le verdict est
      enregistré (c'est tout l'intérêt : savoir plus tard ce qui a été relu) ;
    * ``no`` — l'extraction est fausse : ses morceaux sont **supprimés** de
      `knowledge_chunks`, donc elle ne remonte plus ni dans les prompts ni dans
      `/search`. Le fichier, lui, reste dans le bucket : c'est ce qui rend le
      verdict réversible ;
    * ``re`` — on refait l'extraction depuis le fichier stocké, puis on
      réindexe. C'est le retour en arrière après un rejet.

    Ne lève jamais : comme `ingest_media`, le handler doit pouvoir répondre dans
    tous les cas (média introuvable, panne Supabase, extraction impossible).
    """
    if verdict not in REVIEW_VERDICTS:
        return {"ok": False, "reason": "bad_verdict", "verdict": verdict}

    try:
        row = await asyncio.to_thread(fetch, media_id)
    except Exception as exc:
        return {
            "ok": False,
            "reason": "lookup_failed",
            "error": str(exc),
            "media_id": str(media_id),
            "verdict": verdict,
        }
    if not row:
        return {"ok": False, "reason": "not_found", "media_id": str(media_id), "verdict": verdict}

    if verdict == "no":
        try:
            removed = int(await asyncio.to_thread(remove, media_id))
        except Exception as exc:
            return {
                "ok": False,
                "reason": "delete_failed",
                "error": str(exc),
                "media_id": str(media_id),
                "verdict": verdict,
            }
        try:
            updated = await asyncio.to_thread(
                mark, media_id, "rejected", reviewer=reviewer, chunks=0
            )
        except Exception as exc:
            # Les morceaux sont bien partis : le verdict est une annotation, son
            # échec ne doit pas faire croire que le rejet n'a rien fait.
            print(f"   [medias] verdict non enregistre ({media_id}) : {type(exc).__name__}: {exc}")
            updated = None
        return {
            "ok": True,
            "verdict": verdict,
            "media_id": str(media_id),
            # La ligne **relue après écriture** porte le verdict : renvoyer celle
            # d'avant le clic donnerait un média rejeté sans mention de rejet.
            "media": updated or row,
            "removed": removed,
            "status": "rejected",
        }

    if verdict == "re":
        outcome = await reprocess_media(
            media_id,
            row=row,
            reviewer=reviewer,
            download=download,
            extract=extract,
            store=store,
            record=record,
            tag_store=tag_store,
            mark=mark,
        )
        # Le verdict habille le compte-rendu commun : c'est la seule chose que la
        # revue ajoute au travail lui-même, et ce que lisent le rapport et le
        # clavier de suite.
        return {**outcome, "verdict": verdict}

    try:
        updated = await asyncio.to_thread(mark, media_id, "validated", reviewer=reviewer)
    except Exception as exc:
        return {
            "ok": False,
            "reason": "mark_failed",
            "error": str(exc),
            "media_id": str(media_id),
            "verdict": verdict,
        }
    return {
        "ok": True,
        "verdict": verdict,
        "media_id": str(media_id),
        "media": updated or row,
        "status": "validated",
    }


def _descriptor_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Descripteur d'extraction reconstruit depuis une ligne `knowledge_media`.

    La réindexation ne repasse pas par Telegram : le fichier est dans le bucket,
    et la ligne contient tout ce que l'extracteur attend (type, MIME, nom,
    légende). C'est ce qui rend le retour en arrière possible après un rejet.
    """
    return {
        "media_type": row.get("media_type") or "other",
        "file_id": row.get("telegram_file_id"),
        "file_name": row.get("file_name"),
        "mime_type": row.get("mime_type"),
        "caption": row.get("caption"),
        "width": row.get("width"),
        "height": row.get("height"),
        "duration": row.get("duration_seconds"),
    }


def _reextracted_lines(
    result: Dict[str, Any], *, done: str, nothing: str
) -> List[str]:
    """Corps du compte-rendu d'une extraction **refaite**, titré par l'appelant.

    Écrit une fois pour deux commandes : le bouton « ↩️ Réindexer » d'une revue et
    `/transcribe`. Elles ne diffèrent que par leur titre (`done` quand du texte a
    été lu, `nothing` quand rien n'en est sorti) et par ce que `/transcribe`
    ajoute **avant** — une fois le travail fait, ce qu'il y a à dire est identique,
    et deux copies de ces phrases divergeraient à la première retouche.
    """
    extraction = result.get("extraction") or {}
    chunks = int(result.get("chunks") or 0)
    if not chunks:
        return [
            f"{nothing}, mais aucun texte n'a pu être extrait "
            f"({extraction.get('reason') or 'raison inconnue'})."
        ]
    label = extraction.get("method") or "extraction"
    if extraction.get("has_caption"):
        label = f"{label} + légende"
    lines = [
        f"{done}.",
        f"• {extraction.get('chars', 0)} car. ({label}, {chunks} morceau(x))",
    ]
    lines.extend(_asset_lines(result))
    lines.extend(_extract_lines(result))
    return lines


def format_review_report(result: Dict[str, Any]) -> str:
    """Message affiché après un clic de revue."""
    verdict = result.get("verdict")
    if not result.get("ok"):
        reason = result.get("reason")
        if reason == "not_found":
            return "⚠️ Média introuvable en base : rien n'a été modifié."
        if reason == "no_object":
            return "⚠️ Ce média n'a plus de fichier dans le stockage : impossible de refaire l'extraction."
        if reason == "delete_failed":
            return f"❌ Suppression impossible : {result.get('error')}"
        if reason == "download_failed":
            return f"❌ Relecture du fichier impossible : {result.get('error')}"
        if reason == "lookup_failed":
            return f"❌ Lecture du média impossible : {result.get('error')}"
        if reason == "mark_failed":
            return f"❌ Enregistrement du verdict impossible : {result.get('error')}"
        return f"❌ Revue impossible ({reason})."

    if verdict == "no":
        removed = result.get("removed", 0)
        lines = [
            "❌ Extraction rejetée.",
            f"• {removed} morceau(x) retiré(s) de l'index : ce contenu ne remonte "
            "plus dans les analyses ni dans `/search`.",
            "• Le fichier reste dans le stockage — c'est ce qui permet de revenir en arrière.",
            "• Ne pas le renvoyer sur Telegram pour le réindexer : il serait reconnu "
            "comme déjà stocké et rien ne serait réindexé.",
            "• Le bouton ci-dessous relance l'extraction depuis le fichier stocké.",
        ]
        return "\n".join(lines)

    if verdict == "re":
        return "\n".join(
            _reextracted_lines(
                result,
                done="↩️ Extraction refaite et réindexée",
                nothing="↩️ Réindexation relancée",
            )
        )

    return (
        "✅ Extraction validée : elle reste indexée, et le verdict est enregistré "
        "(il apparaît dans `/media`)."
    )


# --------------------------------------------------------------------------- #
# `/pending` en lot : un canal entier d'un seul clic
# --------------------------------------------------------------------------- #

#: Nombre minimum d'attentes pour qu'un canal mérite une rangée de **lot**. À une
#: seule attente, les boutons de sa ligne font déjà exactement le même geste : la
#: rangée serait un doublon. C'est le cas courant qui décide — un canal de signaux
#: qui publie un graphique de temps en temps laisse rarement deux attentes —, et
#: sans ce seuil la liste se doublerait de rangées qui ne vont nulle part.
CHANNEL_BULK_MIN = 2

#: Plafond d'un lot, en médias traités par clic. Il existe parce que
#: « ↩️ Tout réindexer » télécharge et relit **chaque** fichier : un seul clic qui
#: en lancerait deux cents ferait durer la réponse des minutes, sans que rien ne
#: dise à l'opérateur ce qu'il vient de déclencher. Ce qui dépasse est **compté et
#: annoncé**, jamais tu — relancer le bouton prend la suite.
CHANNEL_BULK_MAX = 25

#: Longueur maximale du nom de canal dans une étiquette de bouton (caractères). Un
#: canal peut n'avoir qu'un **titre** (`Crypto Signals`) comme nom ; l'étiquette
#: reste courte pour que le clavier reste lisible.
CHANNEL_LABEL_CHARS = 24

#: Longueur maximale d'un `callback_data`, en **octets** (limite Telegram). Un
#: bouton qui la dépasse fait échouer l'envoi du message **entier** : mieux vaut ne
#: pas l'afficher.
CALLBACK_DATA_LIMIT = 64

#: Nombre de lignes d'échec **détaillées** dans un compte-rendu de lot ; le reste
#: est compté. Un lot de vingt-cinq lignes en panne ne doit pas produire un message
#: plus long que la liste qu'il remplace.
BULK_ISSUE_LINES = 5

#: Longueur maximale d'un motif d'échec rapporté sur une ligne de lot.
BULK_REASON_CHARS = 80

#: Les deux seules actions de lot. `no` (rejeter en masse) en est absent à dessein :
#: un rejet retire des morceaux de l'index, et « tout rejeter » est le genre de
#: geste qu'on ne veut pas pouvoir déclencher sur un canal entier d'un clic.
CHANNEL_BULK_VERDICTS = ("ok", "re")

#: Action de lot → ce que la **confirmation** déclenchera, en clair. L'aperçu
#: remplace le message de la liste : le bouton cliqué n'est plus là pour dire ce
#: qu'on s'apprête à faire, donc la phrase le dit — c'est tout l'intérêt de l'étape.
BULK_ACTION_LABELS = {
    "ok": "✅ valider le texte indexé",
    "re": "↩️ réindexer les fichiers",
}

#: Préfixe des `callback_data` de **lot par canal** (`medc:ok:%40signals`).
#: Cinquième préfixe de clavier, pour la même raison que `medl:`/`medp:`/`medpg:`/
#: `medt:` : le geste n'est pas le même, donc la réponse attendue non plus. Distinct
#: de `^med:` (qui exige un deux-points juste après `med`) et de `medpg:`. Il porte
#: un **nom de canal**, pas un identifiant de média : la liste des lignes qu'il vise
#: se recalcule au clic.
CHANNEL_BULK_PREFIX = "medc"

#: Préfixe des `callback_data` de **confirmation** de lot (`medck:ok:%40signals`).
#: Sixième préfixe de clavier, et il ouvre la seule question que le lot ne pouvait
#: pas poser : un clic « ✅ @signals » écrivait sur **plusieurs** lignes d'un coup,
#: sans que rien n'ait montré lesquelles. Ici, le clic de la liste **n'écrit
#: rien** : il ouvre l'aperçu (`bulk_preview_view`), et c'est cette charge utile-ci
#: qui exécute. Deux préfixes pour un même lot, parce que la réponse attendue
#: diffère — l'aperçu se relit, la confirmation agit. Distinct de `^medc:` (qui
#: exige un deux-points juste après `medc`).
CHANNEL_BULK_CONFIRM_PREFIX = "medck"

#: Préfixe des `callback_data` d'**annulation** de lot (`medcx:ok:%40signals`).
#: Septième préfixe, et il ne fait rien d'autre que **rendre la liste** : un aperçu
#: sans sortie serait un cul-de-sac, l'opérateur n'ayant plus que `/pending` à
#: retaper pour retrouver ce qu'il avait sous les yeux. Distinct de `^medc:` et de
#: `^medck:` pour la même raison que les autres : ce n'est pas le même geste.
CHANNEL_BULK_CANCEL_PREFIX = "medcx"

#: Longueur maximale d'une ligne d'aperçu de lot (caractères). Un aperçu porte
#: jusqu'à `CHANNEL_BULK_MAX` lignes et le message Telegram en porte 4096 : chaque
#: ligne est donc bornée, et aucune ne porte de lien signé (la liste `/pending`,
#: elle, les porte — c'est d'elle qu'on vient).
BULK_PREVIEW_CHARS = 100

#: Motif d'échec d'une ligne de lot → mot lisible. `format_bulk_report` résume, il
#: ne peut pas dérouler les explications longues de `format_review_report` : il
#: doit donc nommer la cause en trois mots, sans inventer quand il ne la connaît pas.
BULK_FAILURE_LABELS = {
    "not_found": "média introuvable en base",
    "no_object": "fichier absent du stockage",
    "download_failed": "relecture du fichier impossible",
    "not_ready": "ingrédient d'extraction manquant",
    "mark_failed": "verdict non enregistré",
    "delete_failed": "retrait de l'index impossible",
    "exception": "panne inattendue",
}


def channel_bulk_key(row: Optional[Dict[str, Any]]) -> Optional[str]:
    """Clé de regroupement d'une ligne par canal : son nom, sans `@`.

    C'est **le nom affiché** qui groupe, pas le `channel_id` : deux lignes qui
    affichent `📡 @signals` ne sont pas distinguables par qui les relit, et un
    bouton de lot qui agirait sur d'autres lignes que son étiquette ne le dit
    serait un piège. Le `channel_id` existe quand le bot a vu la publication, mais
    pas quand le scraper a lu l'aperçu : s'y fier séparerait un même canal en deux
    boutons identiques, et laisserait des attentes hors de tout lot.

    Un média de chat privé n'a pas de canal : il n'est jamais visé par un lot par
    canal (les boutons de sa ligne restent disponibles).
    """
    name = channel_name(row)
    if not name:
        return None
    return name.lstrip("@").strip() or None


def pending_channel_groups(
    rows: Sequence[Dict[str, Any]],
    *,
    indexed: Optional[set] = None,
    min_count: int = CHANNEL_BULK_MIN,
) -> List[Dict[str, Any]]:
    """Canaux du **backlog entier**, du plus fourni au moins fourni.

    La portée est la file entière, pas la page affichée : « toutes les extractions
    en attente d'un même canal » veut dire toutes, pages suivantes comprises. Un
    lot borné à la page rendrait un compte-rendu qui ne dit pas ce qu'on croit —
    il ferait passer un canal à moitié traité pour un canal soldé.

    `indexed` (comme pour `list_buttons`) dit s'il y a du texte **à valider** : un
    canal dont aucune attente n'a de texte indexé ne propose donc pas « ✅ ».

    Chaque groupe porte `channel` (la clé), `label` (le nom affiché), `count`,
    `validatable` et `media_ids` — de quoi construire le clavier **et** expliquer,
    sans relire la liste.
    """
    wanted = max(1, int(min_count))
    known = indexed if indexed is not None else set()
    groups: Dict[str, Dict[str, Any]] = {}
    for row in rows or []:
        key = channel_bulk_key(row)
        if not key:
            continue
        group = groups.setdefault(
            key,
            {"channel": key, "count": 0, "validatable": 0, "media_ids": []},
        )
        group["count"] += 1
        media_id = _media_id(row)
        if not media_id:
            continue
        group["media_ids"].append(str(media_id))
        if str(media_id) in known:
            group["validatable"] += 1
    ordered = [group for group in groups.values() if group["count"] >= wanted]
    ordered.sort(key=lambda group: (-group["count"], group["channel"]))
    for group in ordered:
        group["label"] = _channel_label(group["channel"])
    return ordered


def channel_bulk_data(
    verdict: str, channel: str, *, prefix: str = CHANNEL_BULK_PREFIX
) -> str:
    """`callback_data` d'un bouton de **lot par canal** (`medc:ok:%40signals`).

    Le nom du canal est **encodé** (`urllib.parse.quote`) : un titre peut contenir
    le `:` du séparateur, un `@`, un espace, et deux canaux distincts ne doivent
    pas produire la même charge utile. Le décodage est le travail de
    `parse_channel_bulk`, pas du handler.

    `prefix` dit **à quel temps** du lot le bouton appartient (aperçu,
    confirmation, annulation), comme `prefix` dans `review_callback_data` dit de
    quelle liste il vient : le canal et le verdict s'encodent pareil, seule
    l'attente du handler change.
    """
    if verdict not in CHANNEL_BULK_VERDICTS:
        raise ValueError(
            f"verdict de lot inconnu : {verdict!r} "
            f"(attendu : {', '.join(CHANNEL_BULK_VERDICTS)})"
        )
    return f"{prefix}:{verdict}:{quote(str(channel), safe='')}"


def parse_channel_bulk(
    data: Optional[str], *, prefix: str = CHANNEL_BULK_PREFIX
) -> Optional[Tuple[str, str]]:
    """`(verdict, canal)` d'un `callback_data` de lot, ou `None`.

    Publique et séparée du handler, comme `parse_review_callback` : c'est la partie
    testable sans `python-telegram-bot`. Un bouton illisible est **refusé** plutôt
    que ramené à un défaut : un lot appliqué au mauvais canal est précisément ce
    qu'on ne veut pas deviner.

    `prefix` est un paramètre, exactement comme dans `parse_review_callback` : trois
    claviers produisent des boutons de lot (la liste, l'aperçu et son annulation) et
    un handler ne doit accepter que les siens. Sans cela, la charge utile de
    l'aperçu — qui **n'écrit rien** — serait relue par le handler qui exécute.

    La charge utile doit être **celle qu'on aurait écrite** : le nom ré-encodé doit
    redonner les mêmes octets. Sinon un `:` brut — que `channel_bulk_data` encode
    toujours en `%3A` — glisserait dans le nom et désignerait un canal qui n'existe
    pas. Un bouton qu'on n'a pas pu écrire, on ne le relit pas.
    """
    parts = str(data or "").split(":", 2)
    if len(parts) != 3 or parts[0] != prefix:
        return None
    verdict, raw = parts[1], parts[2]
    if verdict not in CHANNEL_BULK_VERDICTS or not raw:
        return None
    channel = unquote(raw).strip()
    if not channel or quote(channel, safe="") != raw:
        return None
    return verdict, channel


def channel_bulk_actions(verdict: str, channel: str) -> Optional[Dict[str, str]]:
    """Les trois temps d'un lot par canal, ou `None` s'ils ne tiennent pas ensemble.

    Rend `{"ask", "confirm", "cancel"}` : le bouton de la liste (`ask`), celui qui
    **exécute** (`confirm`) et celui qui **rend la liste** (`cancel`). Construits
    ensemble parce qu'ils tiennent ou ne tiennent pas ensemble : un nom de canal qui
    laisse tout juste la place à `medc:` (un octet de moins que `medck:`) donnerait
    un aperçu dont ni la confirmation ni l'annulation n'entreraient dans les 64
    octets de Telegram — un message dont on ne pourrait plus sortir qu'en retapant
    `/pending`. Un bouton qui mène à une impasse n'est pas proposé.
    """
    payloads = {
        "ask": channel_bulk_data(verdict, channel),
        "confirm": channel_bulk_data(verdict, channel, prefix=CHANNEL_BULK_CONFIRM_PREFIX),
        "cancel": channel_bulk_data(verdict, channel, prefix=CHANNEL_BULK_CANCEL_PREFIX),
    }
    if not all(_fits_callback(data) for data in payloads.values()):
        return None
    return payloads


def _fits_callback(data: str) -> bool:
    """Ce `callback_data` tient-il dans les 64 **octets** de Telegram ?"""
    return len(data.encode("utf-8")) <= CALLBACK_DATA_LIMIT


def _short_label(text: str, *, limit: int = CHANNEL_LABEL_CHARS) -> str:
    """Étiquette de bouton bornée : un titre de canal peut être long."""
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def channel_bulk_buttons(
    groups: Sequence[Dict[str, Any]],
) -> List[List[Tuple[str, str]]]:
    """Une rangée par canal : « ✅ » si du texte est validable, « ↩️ » toujours.

    Le nombre porté par le bouton est celui sur lequel il agit **vraiment**, et non
    le total du canal : un « ✅ @signals (7) » qui n'en validerait que cinq ferait
    mentir son étiquette. « ✅ » disparaît quand il n'y a rien à valider, comme dans
    `list_buttons` — valider une extraction sans texte écrirait une relecture de
    rien. Une rangée dont les deux boutons seraient écartés n'est pas ajoutée.

    Ces boutons n'exécutent plus rien : ils ouvrent l'**aperçu** du lot, et c'est
    l'aperçu qui confirme. La longueur des charges utiles est donc jugée pour les
    trois temps (`channel_bulk_actions`) : proposer un aperçu impossible à confirmer
    serait une impasse.
    """
    rows: List[List[Tuple[str, str]]] = []
    for group in groups or []:
        channel = group.get("channel")
        if not channel:
            continue
        label = _short_label(group.get("label") or f"@{channel}")
        count = int(group.get("count") or 0)
        validatable = int(group.get("validatable") or 0)
        row: List[Tuple[str, str]] = []
        validating = channel_bulk_actions("ok", channel)
        if validating and validatable:
            row.append((f"✅ {label} ({validatable})", validating["ask"]))
        reindexing = channel_bulk_actions("re", channel)
        if reindexing:
            row.append((f"↩️ {label} ({count})", reindexing["ask"]))
        if row:
            rows.append(row)
    return rows


def channel_bulk_confirmation_buttons(
    verdict: str, channel: str, *, confirmable: bool = True
) -> List[List[Tuple[str, str]]]:
    """Clavier de l'**aperçu** : lancer le lot, ou revenir à la liste.

    Deux boutons, deux issues, rien d'autre : l'aperçu n'a pas d'autre geste à
    proposer. « ✖️ Annuler » est **toujours** là, même quand il n'y a plus rien à
    confirmer ou quand la file vient d'être illisible — sans lui, l'opérateur serait
    bloqué sur un message d'aperçu, sans autre retour que de retaper `/pending`.

    `confirmable=False` retire le bouton de confirmation quand le clic n'aurait
    **rien** à faire (plus rien en attente pour ce canal, ou aucune extraction
    validable) : un bouton sans effet ferait douter de tous les autres.
    """
    payloads = channel_bulk_actions(verdict, channel)
    if not payloads:
        return []
    row: List[Tuple[str, str]] = []
    if confirmable:
        row.append(("✅ Confirmer le lot", payloads["confirm"]))
    row.append(("✖️ Annuler", payloads["cancel"]))
    return [row]


def _preview_target(
    row: Dict[str, Any], *, verdict: str, indexed: Optional[set] = None
) -> Dict[str, Any]:
    """Ce que l'aperçu retient d'une ligne visée : la ligne, et ce qu'on en ferait.

    Deux questions distinctes, et c'est ce qui rend l'aperçu utile :

    * `indexed` — y a-t-il du texte indexé à valider ? Faux, la ligne serait
      **laissée** par un lot « ✅ » (le compte-rendu compte ces lignes en
      `skipped`, et l'aperçu les marque avant le clic) ;
    * `acted` — la confirmation poserait-elle un verdict sur cette ligne ? Faux, et
      l'aperçu ne propose pas de confirmer : un bouton qui n'aurait rien à faire
      ferait douter de tous les autres.

    Les deux se séparent exactement là où `bulk_review_channel` sépare ses comptes :
    un média sans texte pour « ok », un média sans identifiant pour « re ». L'aperçu
    et le compte-rendu lisent donc la **même** règle, sinon le second démentirait le
    premier.
    """
    media_id = _media_id(row)
    known = indexed if indexed is not None else set()
    has_text = bool(media_id) and str(media_id) in known
    return {
        "row": row,
        "indexed": has_text,
        "acted": has_text if verdict == "ok" else bool(media_id),
    }


def format_bulk_preview(preview: Dict[str, Any]) -> str:
    """Message d'**aperçu** d'un lot : ce qu'il viserait, et ce qu'il n'a pas fait.

    Fonction **pure**. Les lignes affichées sont celles de la liste (`_media_line`),
    numérotées dans le même ordre : c'est ce qui permet de reconnaître ce qu'on
    vient d'y voir. Celles que le lot **ne toucherait pas** sont marquées `⏭️`
    plutôt que tues, et leur compte est écrit : un aperçu qui annoncerait vingt-cinq
    lignes traitées quand quatre seront laissées de côté serait pire que pas
    d'aperçu du tout.

    Aucun lien signé ici : l'aperçu porte jusqu'à `CHANNEL_BULK_MAX` lignes et le
    message Telegram en porte 4096 caractères — un lien par ligne les dépasserait.
    La liste `/pending`, dont on vient, les porte déjà.
    """
    label = preview.get("label") or preview.get("channel") or "canal inconnu"
    if not preview.get("ok"):
        reason = preview.get("reason")
        if reason == "lookup_failed":
            return (
                "❌ Liste des attentes illisible : "
                f"{preview.get('error') or 'raison inconnue'}\n"
                "Rien n'a été modifié."
            )
        if reason == "no_channel":
            return "⚠️ Canal illisible : rien n'a été modifié."
        if reason == "bad_verdict":
            return "⚠️ Action de lot inconnue : rien n'a été modifié."
        return f"❌ Aperçu impossible ({reason})."

    verdict = preview.get("verdict")
    action = BULK_ACTION_LABELS.get(verdict, str(verdict))
    requested = int(preview.get("requested") or 0)
    if not requested:
        # Comme pour le compte-rendu : le clic arrive après coup, des verdicts sont
        # tombés entre l'affichage et lui. Le dire vaut mieux que d'ouvrir un
        # aperçu vide — et mieux que de laisser croire à un clic perdu.
        return (
            f"📭 Plus rien en attente pour {label} : un verdict a déjà été rendu "
            "(ici, dans `/media`, ou depuis un autre message).\nRien n'a été modifié."
        )

    waiting = int(preview.get("waiting") or 0)
    lines = [
        f"🛠️ Aperçu d'un lot — {action}",
        f"📡 {label} : {requested} extraction(s) visée(s) sur les {waiting} en "
        "attente pour ce canal :",
    ]
    for index, target in enumerate(preview.get("targets") or [], 1):
        line = _short(_media_line(target.get("row") or {}), BULK_PREVIEW_CHARS)
        # Le marqueur dit ce que le lot ne fera **pas** : c'est la seule information
        # que le compte-rendu ne peut plus donner après coup.
        marker = "" if target.get("acted") else "⏭️ "
        lines.append(f"{index}. {marker}{line}")

    skipped = int(preview.get("skipped") or 0)
    if skipped:
        if verdict == "ok":
            lines.append(
                f"⏭️ {skipped} des extractions visées n'ont pas de texte indexé "
                "(marquées ⏭️) : elles seront laissées sans verdict."
            )
        else:
            lines.append(
                f"⏭️ {skipped} des extractions visées n'ont pas d'identifiant "
                "(marquées ⏭️) : elles ne peuvent pas être relues."
            )
    remaining = int(preview.get("remaining") or 0)
    if remaining:
        lines.append(
            f"⏳ {remaining} laissée(s) de côté : le lot s'arrête à "
            f"{CHANNEL_BULK_MAX} par clic, relance pour la suite."
        )

    lines.append("")
    if not preview.get("confirmable"):
        if verdict == "ok":
            lines.append(
                "⚠️ Aucune des extractions visées n'a de texte indexé : il n'y a "
                "rien à valider. « ↩️ » sur la liste, elle, les réindexerait."
            )
        lines.append("Rien n'a été modifié : « ✖️ Annuler » ramènera la liste.")
    else:
        lines.append(
            "Rien n'a encore été modifié : « ✅ Confirmer le lot » le lancera, "
            "« ✖️ Annuler » ramènera la liste."
        )
    return "\n".join(lines)


def preview_toast(preview: Dict[str, Any]) -> str:
    """Confirmation courte (`query.answer`) de l'**ouverture** d'un aperçu.

    Le clic sur un bouton de lot n'écrit plus rien : le dire au moment où il
    arrive est le seul retour immédiat — l'aperçu, lui, met le temps d'une lecture
    de la file à apparaître, et un silence pourrait passer pour un clic perdu.
    """
    if not preview.get("ok"):
        return "⚠️ Aperçu impossible"
    if not preview.get("requested"):
        return "📭 Plus rien en attente"
    if not preview.get("confirmable"):
        return "⚠️ Rien à confirmer"
    return f"🛠️ {preview['requested']} visée(s) — rien n'est encore modifié"


def bulk_preview_view(
    channel: str,
    verdict: str,
    *,
    limit: int = CHANNEL_BULK_MAX,
    list_pending: Callable[[], List[Dict[str, Any]]] = media_store.list_pending_review,
    indexed_of: Callable[[Sequence[Dict[str, Any]]], set] = _indexed_media,
) -> Dict[str, Any]:
    """Message **et** clavier de l'**aperçu** d'un lot par canal, en une lecture.

    La question que le clic « ✅ @canal » ne posait pas : « qu'est-ce que ce lot va
    toucher ? ». Elle est relue **au clic**, à partir du backlog courant et avec la
    même règle que `bulk_review_channel` — même filtre de canal, même plafond, même
    découpage. Un aperçu qui compterait autre chose que ce que le lot fera serait
    pire qu'aucun aperçu : il ferait confirmer à l'aveugle, en croyant avoir vu.

    N'écrit **rien** : ni verdict, ni index, ni session. La liste des lignes lues est
    rendue telle quelle (`targets`), marquée de ce que le lot en ferait.

    Restitue la ligne, les comptes, le texte et le clavier — dont le bouton de
    confirmation, absent quand le clic n'aurait rien à faire. Bloquant (réseau) : à
    appeler hors de l'event loop. Ne lève jamais — une file illisible devient un
    message d'erreur, et garde le bouton d'annulation, pour que l'aperçu ne soit
    jamais un cul-de-sac.
    """
    base: Dict[str, Any] = {
        "ok": True,
        "reason": None,
        "error": None,
        "channel": str(channel or "").lstrip("@").strip(),
        "label": "",
        "verdict": verdict,
        "requested": 0,
        "acted": 0,
        "skipped": 0,
        "remaining": 0,
        "waiting": 0,
        "targets": [],
        "confirmable": False,
    }
    base["label"] = _channel_label(base["channel"])

    if verdict not in CHANNEL_BULK_VERDICTS:
        preview = {**base, "ok": False, "reason": "bad_verdict"}
        return {**preview, "text": format_bulk_preview(preview), "keyboard": []}
    if not base["channel"]:
        preview = {**base, "ok": False, "reason": "no_channel"}
        return {**preview, "text": format_bulk_preview(preview), "keyboard": []}

    try:
        rows = list_pending()
    except Exception as exc:
        preview = {
            **base,
            "ok": False,
            "reason": "lookup_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
        return {
            **preview,
            "text": format_bulk_preview(preview),
            # Le canal, lui, a bien été lu : le retour à la liste est conservé pour
            # que l'erreur de lecture ne coince pas l'opérateur sur l'aperçu.
            "keyboard": channel_bulk_confirmation_buttons(
                verdict, base["channel"], confirmable=False
            ),
        }

    mine = [row for row in rows or [] if channel_bulk_key(row) == base["channel"]]
    targets = mine[: max(1, int(limit))]
    indexed: set = set()
    if verdict == "ok":
        try:
            indexed = indexed_of(mine) or set()
        except Exception as exc:
            print(
                f"   [medias] index illisible (aperçu {base['channel']}) : "
                f"{type(exc).__name__}: {exc}"
            )

    entries = [_preview_target(row, verdict=verdict, indexed=indexed) for row in targets]
    acted = sum(1 for entry in entries if entry["acted"])
    preview = {
        **base,
        "requested": len(entries),
        "acted": acted,
        "skipped": len(entries) - acted,
        "remaining": len(mine) - len(entries),
        "waiting": len(mine),
        "targets": entries,
        "confirmable": acted > 0,
    }
    return {
        **preview,
        "text": format_bulk_preview(preview),
        "keyboard": channel_bulk_confirmation_buttons(
            verdict, base["channel"], confirmable=preview["confirmable"]
        ),
    }


def _bulk_reason(result: Dict[str, Any]) -> str:
    """Motif lisible d'un échec de ligne dans un lot — jamais un code nu."""
    reason = result.get("reason")
    if reason in BULK_FAILURE_LABELS:
        return BULK_FAILURE_LABELS[reason]
    if reason:
        return str(reason)[:BULK_REASON_CHARS]
    error = result.get("error")
    if error:
        return str(error)[:BULK_REASON_CHARS]
    return "échec sans motif rapporté"


async def bulk_review_channel(
    channel: str,
    verdict: str,
    *,
    reviewer: Optional[str] = None,
    limit: int = CHANNEL_BULK_MAX,
    list_pending: Callable[[], List[Dict[str, Any]]] = media_store.list_pending_review,
    indexed_of: Callable[[Sequence[Dict[str, Any]]], set] = _indexed_media,
    review: Callable[..., Awaitable[Dict[str, Any]]] = review_media,
) -> Dict[str, Any]:
    """« ✅ Tout valider » / « ↩️ Tout réindexer » les attentes d'**un canal**.

    La cible est **relue au clic**, à partir du backlog courant : entre l'affichage
    du bouton et le clic, un verdict a pu tomber — depuis cette liste, depuis
    `/media`, ou depuis le compte-rendu d'un autre message. Agir sur les
    identifiants vus à l'affichage rouvrirait des lignes déjà jugées ; relire la
    file ne fait que ce qui **reste**, et si plus rien ne reste, le compte-rendu le
    dit (`requested` à zéro) au lieu de ne rien faire en silence.

    Pour « ✅ », une ligne **sans texte indexé** est comptée à part (`skipped`) et
    non jugée : elle n'a rien à valider, et lui écrire un verdict de relecture
    serait exactement l'état trompeur que le reste du module évite.

    Ne lève jamais : chaque ligne est un résultat, exactement comme
    `repair_missing` — un lot doit continuer après une ligne perdue.
    """
    base: Dict[str, Any] = {
        "channel": str(channel or "").lstrip("@").strip(),
        "label": "",
        "verdict": verdict,
        "requested": 0,
        "handled": 0,
        "remaining": 0,
        "validated": 0,
        "reindexed": 0,
        "textless": 0,
        "failed": 0,
        "skipped": 0,
        "chunks": 0,
        "chars": 0,
        "issues": [],
        "total_issues": 0,
        "reason": None,
    }
    base["label"] = _channel_label(base["channel"])

    if verdict not in CHANNEL_BULK_VERDICTS:
        return {**base, "ok": False, "reason": "bad_verdict"}
    if not base["channel"]:
        return {**base, "ok": False, "reason": "no_channel"}

    try:
        rows = await asyncio.to_thread(list_pending)
    except Exception as exc:
        return {
            **base,
            "ok": False,
            "reason": "lookup_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }

    mine = [row for row in rows or [] if channel_bulk_key(row) == base["channel"]]
    targets = mine[: max(1, int(limit))]
    remaining = len(mine) - len(targets)

    indexed: set = set()
    if verdict == "ok":
        try:
            indexed = indexed_of(mine) or set()
        except Exception as exc:
            print(
                f"   [medias] index illisible (lot {base['channel']}) : "
                f"{type(exc).__name__}: {exc}"
            )

    validated = reindexed = textless = failed = skipped = 0
    chunks = chars = 0
    issues: List[Dict[str, str]] = []

    for row in targets:
        media_id = _media_id(row)
        if not media_id:
            skipped += 1
            continue
        if verdict == "ok" and str(media_id) not in indexed:
            skipped += 1
            continue
        try:
            result = await review(str(media_id), verdict, reviewer=reviewer)
        except Exception as exc:
            result = {
                "ok": False,
                "reason": "exception",
                "error": f"{type(exc).__name__}: {exc}",
            }
        if not result.get("ok"):
            failed += 1
            issues.append({"media_id": str(media_id), "reason": _bulk_reason(result)})
            continue
        if verdict == "ok":
            validated += 1
            continue
        produced = int(result.get("chunks") or 0)
        chunks += produced
        chars += int((result.get("extraction") or {}).get("chars") or 0)
        if produced:
            reindexed += 1
        else:
            # La réindexation a bien eu lieu (le verdict est posé, l'état noté) :
            # seule la lecture n'a rien donné. La confondre avec un échec ferait
            # relancer un lot qu'on vient de faire.
            textless += 1

    return {
        **base,
        "ok": True,
        "reason": None,
        "requested": len(targets),
        "handled": validated + reindexed + textless,
        "remaining": remaining,
        "validated": validated,
        "reindexed": reindexed,
        "textless": textless,
        "failed": failed,
        "skipped": skipped,
        "chunks": chunks,
        "chars": chars,
        "issues": issues[:BULK_ISSUE_LINES],
        "total_issues": len(issues),
    }


def format_bulk_report(result: Dict[str, Any]) -> str:
    """Compte-rendu **unique** d'un lot : ce qui a été fait, et ce qui reste.

    Fonction pure. Un lot ne rend pas un compte-rendu par ligne : vingt-cinq
    messages pour un clic seraient illisibles, et c'est le lot qu'on a demandé,
    pas vingt-cinq revues. Les échecs, eux, sont **nommés** — mais plafonnés
    (`BULK_ISSUE_LINES`) : un rapport plus long que la liste qu'il remplace ne se
    lit pas non plus.
    """
    label = result.get("label") or result.get("channel") or "canal inconnu"
    if not result.get("ok"):
        reason = result.get("reason")
        if reason == "lookup_failed":
            return (
                f"❌ Liste des attentes illisible : "
                f"{result.get('error') or 'raison inconnue'}\nRien n'a été modifié."
            )
        if reason == "no_channel":
            return "⚠️ Canal illisible : rien n'a été modifié."
        if reason == "bad_verdict":
            return "⚠️ Action de lot inconnue : rien n'a été modifié."
        return f"❌ Lot impossible ({reason})."

    requested = int(result.get("requested") or 0)
    if not requested:
        # Le clic arrive après coup : des verdicts sont tombés entre l'affichage du
        # bouton et le clic. Le dire vaut mieux que de laisser croire à un clic
        # perdu — et mieux que de rendre un compte-rendu de zéro ligne.
        return (
            f"📭 Plus rien en attente pour {label} : un verdict a déjà été rendu "
            "(ici, dans `/media`, ou depuis un autre message)."
        )

    lines = [f"🛠️ {label} — {requested} extraction(s) en attente traitée(s) :"]
    if result.get("verdict") == "ok":
        if result.get("validated"):
            lines.append(
                f"✅ {result['validated']} validée(s) : leur texte reste indexé"
            )
        if result.get("skipped"):
            lines.append(f"⏭️ {result['skipped']} sans texte indexé : rien à valider")
    else:
        if result.get("reindexed"):
            lines.append(
                f"↩️ {result['reindexed']} réindexée(s) : "
                f"{result.get('chars', 0)} car. ({result.get('chunks', 0)} morceau(x))"
            )
        if result.get("textless"):
            lines.append(
                f"⚠️ {result['textless']} refaite(s) sans texte extractible "
                "(clef absente, ou média illisible)"
            )
    if result.get("failed"):
        lines.append(f"❌ {result['failed']} échec(s)")
    issues = result.get("issues") or []
    for issue in issues:
        media_id = str(issue.get("media_id") or "?")
        lines.append(f"   • {media_id[:8]} : {issue.get('reason')}")
    hidden = int(result.get("total_issues") or 0) - len(issues)
    if hidden > 0:
        lines.append(f"   • … et {hidden} autre(s)")
    if result.get("remaining"):
        lines.append(
            f"⏳ {result['remaining']} laissée(s) de côté : le lot s'arrête à "
            f"{CHANNEL_BULK_MAX} par clic, relance pour la suite"
        )
    return "\n".join(lines)


def bulk_toast(result: Dict[str, Any]) -> str:
    """Confirmation courte (`query.answer`) d'un lot cliqué, comme `review_toast`."""
    if not result.get("ok"):
        if result.get("reason") == "lookup_failed":
            return "⚠️ Liste illisible"
        if result.get("reason") == "bad_verdict":
            return "⚠️ Action inconnue"
        return "⚠️ Lot impossible"
    if not result.get("requested"):
        return "📭 Plus rien en attente"
    parts: List[str] = []
    if result.get("validated"):
        parts.append(f"✅ {result['validated']}")
    if result.get("reindexed"):
        parts.append(f"↩️ {result['reindexed']}")
    if result.get("textless"):
        parts.append(f"⚠️ {result['textless']}")
    if result.get("skipped"):
        parts.append(f"⏭️ {result['skipped']}")
    if result.get("failed"):
        parts.append(f"❌ {result['failed']}")
    return " · ".join(parts) or "rien à faire"


# --------------------------------------------------------------------------- #
# `/transcribe` : refaire l'extraction d'un média **déjà stocké**
# --------------------------------------------------------------------------- #

#: Nombre de rattrapages affichés par page de `/transcribe`. Chaque entrée tient
#: sur trois lignes (statut, motif du dernier échec, référence) : c'est le message
#: Telegram, plafonné à 4096 caractères, qui donne la borne — pas le nombre de
#: médias à rattraper, puisque la liste est parcourue **en entier**.
TRANSCRIBE_PAGE = 10

#: Borne haute de la page de `/transcribe`, comme `MAX_PENDING_PAGE`.
MAX_TRANSCRIBE_PAGE = 15

#: Préfixe des `callback_data` de **navigation** dans la liste `/transcribe`
#: (`medt:<rang>`). Il porte un rang, pas un identifiant de média : une page se
#: recalcule, elle ne désigne aucune ligne. Distinct des préfixes de revue pour
#: la même raison qu'eux : le geste, et donc la réponse attendue, n'est pas le
#: même — naviguer ne rend aucun verdict et ne change rien en base.
TRANSCRIBE_PAGE_PREFIX = "medt"

#: Aide de `/transcribe`. Affichée aussi sur une erreur : ce que la commande fait
#: ne doit pas s'apprendre en lisant le code.
TRANSCRIBE_HELP = """🎙️ `/transcribe` — refaire l'extraction d'un média **déjà stocké**

• `/transcribe` — en **réponse au message du média** (rien à copier)
• `/transcribe <référence>` — avec l'identifiant du média, tel qu'il apparaît dans `/media`
• `/transcribe` seul — liste **paginée** des médias sans texte extrait, et ce qui
  manque à chacun (la table est balayée en entier : « ▶️ Suivants » va plus loin)

À quoi ça sert : une ingestion faite sans `GROQ_API_KEY` (vocal, vidéo) ou sans
`GEMINI_API_KEY` (image) stocke bien le fichier, mais sans le texte — et **le
renvoyer sur Telegram ne réindexe rien**, il est reconnu comme déjà stocké. La
commande relit le fichier **dans le stockage**, réextrait et réindexe.

Avant de commencer, elle vérifie ses ingrédients et refuse en nommant ce qui
manque : une extraction dont la clef est absente ne peut pas réussir, et relire
vingt minutes de vidéo pour retrouver le même échec n'apprend rien."""

#: Ce que `/transcribe` répond quand aucun média n'a été désigné.
TRANSCRIBE_MISSING_MEDIA = (
    "⚠️ Quel média ? Réponds au **message du média** avec `/transcribe`, ou donne "
    "sa référence : `/transcribe <référence>`. Sans argument, la commande liste "
    "les médias sans texte extrait.\n\n"
)

#: Comment désigner un média, sous la liste des rattrapages possibles.
TRANSCRIBE_PICK = (
    "Pour relancer : `/transcribe <référence>` — ou `/transcribe` en réponse au "
    "message du média."
)


def parse_transcribe_args(args: Sequence[str]) -> Dict[str, Any]:
    """Décompose les arguments de `/transcribe`, ou explique pourquoi c'est faux.

    Une seule forme : une **référence** facultative. Sans argument, la commande
    reste utilisable — en réponse au message du média — donc « aucun argument »
    n'est pas une erreur ici, contrairement à `/tag`, qui exige un actif. C'est le
    handler qui décide quand l'absence d'argument devient une demande d'aide :
    quand il n'y a rien à quoi répondre non plus.
    """
    tokens = [str(token) for token in (args or [])]
    if tokens and tokens[0] in ("--help", "-h", "help"):
        return {"ok": False, "reason": "help"}
    if len(tokens) > 1:
        # Refuser plutôt qu'ignorer : une référence avalée en silence ferait
        # relancer l'extraction du mauvais média.
        return {"ok": False, "reason": "too_many", "args": tokens}
    return {"ok": True, "reference": tokens[0] if tokens else None}


def format_transcribe_help(result: Optional[Dict[str, Any]] = None) -> str:
    """Aide de `/transcribe`, préfacée du problème quand il y en a un."""
    prefix = ""
    if (result or {}).get("reason") == "too_many":
        prefix = "⚠️ Trop d'arguments : une seule référence suffit.\n\n"
    prefix += _reference_problem(result, missing=TRANSCRIBE_MISSING_MEDIA)
    return prefix + TRANSCRIBE_HELP


def transcribe_page_data(offset: int) -> str:
    """`callback_data` du bouton qui mène à la page commençant à ce **rang**."""
    return f"{TRANSCRIBE_PAGE_PREFIX}:{max(0, int(offset))}"


def parse_transcribe_page(data: Optional[str]) -> Optional[int]:
    """Rang porté par un `callback_data` de navigation, ou `None` s'il n'est pas à nous.

    Publique et séparée du handler : c'est la seule partie du bouton qui doit
    rester testable sans `python-telegram-bot` (ce module ne l'importe pas), comme
    `parse_review_callback`. Un rang qui n'est pas un entier positif est refusé
    plutôt que ramené à zéro : un bouton qu'on ne sait pas lire ne doit pas
    réafficher la première page en donnant l'impression d'avoir été compris.
    """
    parts = str(data or "").split(":", 1)
    if len(parts) != 2 or parts[0] != TRANSCRIBE_PAGE_PREFIX:
        return None
    try:
        offset = int(parts[1])
    except ValueError:
        return None
    return offset if offset >= 0 else None


def transcribe_buttons(
    *, offset: int = 0, shown: int = 0, total: int = 0, page: int = TRANSCRIBE_PAGE
) -> List[List[Tuple[str, str]]]:
    """Clavier de navigation de la liste `/transcribe` : Précédents / Suivants.

    Rien n'est affiché quand tout tient sur une page : un clavier qui ne mène
    nulle part est du bruit, et il laisserait croire qu'il reste des lignes. Les
    deux directions partagent **une** rangée — ce ne sont pas des lignes de la
    liste, contrairement aux boutons de revue de `list_buttons`.

    Aucun état n'est mémorisé : le bouton porte un rang, et la page est recalculée
    au clic. C'est le seul comportement honnête, la liste changeant dès qu'une
    extraction est relancée — une page figée enverrait sur du travail déjà fait.
    """
    buttons: List[Tuple[str, str]] = []
    if offset > 0:
        buttons.append(("◀️ Précédents", transcribe_page_data(offset - page)))
    if offset + shown < total:
        buttons.append(("▶️ Suivants", transcribe_page_data(offset + page)))
    return [buttons] if buttons else []


def _readiness_label(state: Dict[str, Any]) -> str:
    """Marque d'une ligne de liste : peut-on relancer, et à quel prix ?

    Trois états, et pas deux : « prêt », « prêt mais dégradé » (sans clef Groq, la
    transcription partira sur le repli local — possible, plus lent), et « pas
    prêt », où l'on nomme ce qui manque. Un média d'un type que l'extraction ne
    sait pas lire n'arrive pas ici : il n'est pas listé du tout.
    """
    if not state.get("ready"):
        missing = ", ".join(state.get("missing") or []) or "ingrédients absents"
        return f"⚠️ {missing}"
    if state.get("hint"):
        return "🕒 repli local"
    return "✅"


def _needs_retry(row: Dict[str, Any], *, indexed: bool) -> bool:
    """Ce média a-t-il du texte à **rattraper** ?

    Le résultat de la dernière extraction est la seule réponse exacte, et trois
    cas se présentent :

    * **noté échoué** → oui, quel que soit l'index : une légende indexée n'est pas
      du contenu lu, et c'est précisément le cas d'une clef absente ;
    * **noté réussi** → non : relire un média déjà lu est une relecture, pas un
      rattrapage, et la liste ne doit pas proposer du travail inutile ;
    * **jamais noté** (média antérieur à ce champ) → on ne devine pas : on ne le
      propose que s'il n'a **rien** d'indexé, le seul cas où l'absence de texte
      est un fait plutôt qu'une supposition.
    """
    outcome = media_store.extraction_outcome(row)
    if outcome is None:
        return not indexed
    return not outcome.get("ok")


async def retranscribe_media(
    media_id: str,
    *,
    row: Optional[Dict[str, Any]] = None,
    reviewer: Optional[str] = None,
    fetch: Callable[[str], Optional[Dict[str, Any]]] = media_store.get_media,
    download: Callable[[str], bytes] = media_store.download_media,
    extract: Extractor = media_extractor.extract_text,
    store: ChunkStore = media_store.replace_media_chunks,
    tag_store: Tagger = media_store.set_media_asset,
    mark: Callable[..., Dict[str, Any]] = media_store.set_review_status,
    record: Recorder = media_store.set_extraction_outcome,
    readiness: Callable[..., Dict[str, Any]] = media_extractor.extraction_readiness,
) -> Dict[str, Any]:
    """Relance l'extraction **et** l'indexation d'un média déjà stocké.

    Ce que `/transcribe` ajoute à la revue, c'est de **vérifier d'abord**. La ligne
    est lue une seule fois — elle sert au diagnostic *et* au travail, d'où `row`
    passé à `reprocess_media` —, la sorte d'extraction est classée, et si les
    ingrédients manquent on s'arrête **avant** de relire l'objet : « il manque
    `GEMINI_API_KEY` » est une réponse utile, un échec de plus ne l'est pas.

    Le reste est délégué à `reprocess_media`, partagé avec le bouton
    « ↩️ Réindexer » : le travail ne doit exister qu'en un endroit.

    Le résultat porte `readiness` et `verdict = "re"` — la commande *refait* une
    extraction, exactement ce que ce verdict désigne, donc le clavier de suite est
    celui d'une extraction neuve (✅/❌), pas un clavier à réinventer.
    """
    if row is None:
        try:
            row = await asyncio.to_thread(fetch, media_id)
        except Exception as exc:
            return {
                "ok": False,
                "reason": "lookup_failed",
                "error": str(exc),
                "media_id": str(media_id),
            }
        if not row:
            return {"ok": False, "reason": "not_found", "media_id": str(media_id)}

    state = readiness(
        media_type=row.get("media_type"),
        mime_type=row.get("mime_type"),
        file_name=row.get("file_name"),
    )
    if not state.get("ready"):
        return {
            "ok": False,
            "reason": "not_ready",
            "media_id": str(media_id),
            "media": row,
            "readiness": state,
        }

    outcome = await reprocess_media(
        media_id,
        row=row,
        reviewer=reviewer,
        download=download,
        extract=extract,
        store=store,
        record=record,
        tag_store=tag_store,
        mark=mark,
    )
    return {**outcome, "verdict": "re", "readiness": state}


def _undecided_media(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Les lignes dont l'index doit être consulté : celles **sans résultat noté**.

    `_needs_retry` ne lit l'index que pour elles — un résultat d'extraction noté
    tranche à lui seul. Interroger l'index pour toute la table ferait un
    `in (…)` de tous les identifiants, pour une réponse que personne ne lirait.
    """
    return [
        row
        for row in rows or []
        if row.get("id") and media_store.extraction_outcome(row) is None
    ]


def transcribe_candidates(
    *,
    offset: int = 0,
    page: int = TRANSCRIBE_PAGE,
    rows: Optional[Sequence[Dict[str, Any]]] = None,
    indexed: Optional[set] = None,
    readiness: Callable[..., Dict[str, Any]] = media_extractor.extraction_readiness,
) -> Dict[str, Any]:
    """Médias stockés qui ont du texte à rattraper, **page par page**.

    `/transcribe` sans argument ne peut pas se contenter d'une aide : la question
    qu'on se pose est « lesquels ? ». La réponse ne peut pas venir de `/media` — un
    média dont l'extraction a échoué y ressemble à un média lu, dès qu'il porte une
    légende —, donc elle vient du résultat d'extraction noté sur chaque ligne.

    La table est parcourue **en entier** (`list_all_media`) : borner la lecture aux
    derniers médias rendait un rattrapage ancien invisible, et la liste répondait
    « rien à rattraper » alors qu'il en restait, plus loin. Le grand nombre est
    traité par la **pagination** (bouton « ▶️ Suivants » : `medt:<rang>`), jamais
    par une borne silencieuse.

    Restitue `{"text", "keyboard", "entries", "total", "shown", "offset",
    "count", "scanned"}`, même contrat que `pending_review_view` : `total` compte
    les rattrapages de **toute** la liste, `count`/`shown` ceux de cette page.

    Bloquant (réseau) : à appeler hors de l'event loop. Ne lève jamais.
    """
    if rows is None:
        try:
            rows = media_store.list_all_media()
        except Exception as exc:
            error = f"❌ Lecture des médias impossible : {type(exc).__name__}: {exc}"
            return {
                "text": error,
                "keyboard": [],
                "entries": [],
                "total": 0,
                "shown": 0,
                "count": 0,
                "scanned": 0,
                "offset": 0,
            }
    rows = list(rows or [])
    if indexed is None:
        indexed = _indexed_media(_undecided_media(rows))

    candidates: List[Dict[str, Any]] = []
    for row in rows:
        media_id = str(row.get("id") or "")
        if not media_id or not _needs_retry(row, indexed=media_id in indexed):
            continue
        state = readiness(
            media_type=row.get("media_type"),
            mime_type=row.get("mime_type"),
            file_name=row.get("file_name"),
        )
        if state.get("kind") is None:
            # Aucune extraction n'est possible pour ce type : le proposer
            # promettrait une réparation que rien ne peut faire.
            continue
        candidates.append({"row": row, "readiness": state})

    start = max(0, int(offset))
    wanted = max(1, min(int(page), MAX_TRANSCRIBE_PAGE))
    shown = candidates[start : start + wanted]
    return {
        "text": format_transcribe_list(
            shown, total=len(candidates), offset=start, scanned=len(rows)
        ),
        "keyboard": transcribe_buttons(
            offset=start, shown=len(shown), total=len(candidates), page=wanted
        ),
        "entries": shown,
        "total": len(candidates),
        "shown": len(shown),
        "count": len(shown),
        "scanned": len(rows),
        "offset": start,
    }


def format_transcribe_list(
    entries: List[Dict[str, Any]], *, total: int, offset: int = 0, scanned: int = 0
) -> str:
    """Message d'**une page** de `/transcribe` sans argument : quoi rattraper, et comment.

    Fonction pure : elle reçoit les entrées déjà résolues, donc testable sans
    réseau. Trois nombres sont dits, et chacun évite une croyance fausse :

    * `total` — combien de rattrapages existent **en tout** (le taire ferait
      croire que la page est la liste) ;
    * le rang de la page — où l'on se trouve dans cette liste (le taire ferait
      croire qu'on regarde le début) ;
    * `scanned` — combien de médias ont été **examinés** pour le dire, dans le seul
      cas où c'est la réponse elle-même (« rien à rattraper »), et où c'est donc
      l'étendue du contrôle qu'on veut connaître.
    """
    if not total:
        if not scanned:
            return (
                "✅ Rien à rattraper : aucun média n'est stocké pour l'instant.\n\n"
                + TRANSCRIBE_HELP
            )
        return (
            f"✅ Rien à rattraper : les {scanned} média(s) stocké(s) ont du texte "
            "extrait, ou un type que l'extraction ne sait pas lire.\n\n"
            + TRANSCRIBE_HELP
        )
    first = offset + 1
    last = offset + len(entries)
    if first == 1 and last == total:
        header = f"🎙️ {total} média(s) à rattraper :"
    else:
        header = f"🎙️ {total} média(s) à rattraper — {first}–{last} :"
    lines = [header, ""]
    for rank, entry in enumerate(entries, first):
        row = entry.get("row") or {}
        lines.append(
            f"{rank}. {_readiness_label(entry.get('readiness') or {})} · {_media_line(row)}"
        )
        # Le motif du dernier échec : c'est lui qui dit **quoi réparer** avant de
        # relancer, et il ne se déduit pas du type de média.
        reason = (media_store.extraction_outcome(row) or {}).get("reason")
        if reason:
            lines.append(f"   Dernier échec : {reason}")
        lines.append(f"   Référence : {row.get('id')}")
    remaining = total - last
    if remaining > 0:
        lines.extend(["", f"⏭️ {remaining} autre(s) : bouton « ▶️ Suivants »."])
    lines.extend(["", TRANSCRIBE_PICK])
    return "\n".join(lines)


def format_transcribe_report(result: Dict[str, Any]) -> str:
    """Message de `/transcribe` : le diagnostic d'abord, le travail ensuite.

    Trois issues, et elles ne se recouvrent pas : un refus **avant** tout travail
    (`not_ready`, que la revue ne connaît pas), le compte-rendu d'une extraction
    refaite, et les échecs de lecture ou d'écriture — pour lesquels on rend les
    phrases de la revue, parce que ce sont les mêmes causes et qu'en écrire une
    seconde version les ferait diverger.
    """
    if result.get("reason") == "not_ready":
        return _not_ready_report(result)
    if result.get("verdict") == "re":
        return "\n".join(
            _reextracted_lines(
                result,
                done="♻️ Extraction refaite et réindexée",
                nothing="♻️ Extraction relancée",
            )
        )
    return format_review_report(result)


def _not_ready_report(result: Dict[str, Any]) -> str:
    """Refus **avant** tout travail, avec ce qui manque et ce qu'il faut faire.

    Le média est décrit malgré le refus : sans sa ligne, on ne sait pas de quel
    fichier on parle — et une commande qui répond « non » sans dire à quoi doit
    pouvoir se vérifier.
    """
    state = result.get("readiness") or {}
    missing = ", ".join(state.get("missing") or [])
    lines = [
        "⏸️ Rien n'a été tenté : cette extraction ne peut pas aboutir en l'état.",
        f"• Média : {_media_line(result.get('media') or {})}",
        (
            f"• Il manque : {missing}"
            if missing
            else "• Son type n'est pas extractible (ni image, ni PDF, ni audio/vidéo)."
        ),
        f"• {state.get('hint')}" if state.get("hint") else "",
        "• Le fichier est toujours dans le stockage : une fois la clef (ou le paquet) "
        "en place, `/transcribe <référence>` la refait.",
    ]
    return "\n".join(line for line in lines if line)


__all__ = [
    "DEFAULT_MEDIA_PAGE",
    "EXCERPT_CHARS",
    "MAX_MEDIA_BYTES",
    "LIST_REVIEW_HINT",
    "LIST_REVIEW_PREFIX",
    "MAX_MEDIA_PAGE",
    "MAX_PENDING_PAGE",
    "PENDING_PAGE",
    "PENDING_REVIEW_PREFIX",
    "CHANNEL_BULK_MAX",
    "CHANNEL_BULK_MIN",
    "CHANNEL_BULK_PREFIX",
    "bulk_review_channel",
    "bulk_toast",
    "channel_bulk_buttons",
    "channel_bulk_data",
    "channel_bulk_key",
    "channel_name",
    "format_bulk_report",
    "parse_channel_bulk",
    "pending_channel_groups",
    "REVIEW_LABELS",
    "REVIEW_PREFIX",
    "REVIEW_VERDICTS",
    "SIGNED_URL_TTL",
    "SUPPORTED_MEDIA_TYPES",
    "attachment_for",
    "attachment_needed",
    "ASSET_SOURCE_LABELS",
    "extract_media",
    "follow_up_buttons",
    "format_tag_report",
    "full_text",
    "format_media_list",
    "PENDING_PAGE_PREFIX",
    "format_pending_review",
    "format_report",
    "format_review_report",
    "ingest_media",
    "list_buttons",
    "list_callback_data",
    "media_list_report",
    "media_list_view",
    "media_origin",
    "parse_pending_page",
    "pending_buttons",
    "pending_page_data",
    "pending_review_view",
    "parse_review_callback",
    "reject_button",
    "restore_buttons",
    "review_buttons",
    "review_callback_data",
    "review_media",
    "review_target",
    "review_toast",
    "TAG_HELP",
    "find_media",
    "format_tag_help",
    "parse_tag_args",
    "tag_media",
    "MAX_TRANSCRIBE_PAGE",
    "TRANSCRIBE_HELP",
    "TRANSCRIBE_PAGE",
    "TRANSCRIBE_PAGE_PREFIX",
    "format_transcribe_help",
    "format_transcribe_list",
    "format_transcribe_report",
    "parse_transcribe_args",
    "parse_transcribe_page",
    "reprocess_media",
    "retranscribe_media",
    "transcribe_buttons",
    "transcribe_candidates",
    "transcribe_page_data",
]
