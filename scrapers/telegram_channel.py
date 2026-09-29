"""
Scraper pour canaux Telegram PUBLICS, via la page d'aperçu web que Telegram
expose pour tout canal public (t.me/s/<nom>) — aucune authentification
nécessaire, aucun compte Telegram requis. Fonctionne uniquement pour les
canaux publics (pas les groupes/canaux privés).

Deux choses sont récupérées :

1. le **texte** des publications, poussé dans la table `insights` (même table
   que les autres sources) ;
2. les **médias** exposés par l'aperçu, téléchargés puis stockés via
   `database.media_store` (bucket privé `telegram-media` + table
   `knowledge_media`), **et leur texte indexé** dans `knowledge_chunks` —
   `ai.media_indexing` extrait le contenu (vision Gemini pour les photos,
   Whisper pour les vidéos, `pypdf` pour un PDF) et l'indexe avec la légende de
   la publication, étiqueté par actif. Sans cette étape, une lecture de
   graphique resterait un fichier dans un bucket : elle n'apparaîtrait ni dans
   `/search`, ni dans le contexte média du moteur de décision.

Ce que l'aperçu web expose RÉELLEMENT (vérifié sur des canaux publics) :

* les **photos** : l'URL du fichier est dans l'attribut `style` du bloc
  `a.tgme_widget_message_photo_wrap` — `background-image:url('https://cdn…')` ;
* les **vidéos** : l'URL est dans `src` de `<video class="tgme_widget_message_video">` ;
* les **documents** : l'aperçu n'expose **aucune URL de fichier**. Le bloc
  `a.tgme_widget_message_document_wrap` ne porte qu'un lien vers la publication
  (`https://t.me/<canal>/<id>`) et, à côté, le nom et la taille du fichier. Un
  document ne peut donc **pas** être téléchargé par cette voie : on le compte et
  on l'ignore, plutôt que de faire croire à une ingestion. Pour récupérer lefichier lui-même, il faut le bot Telegram administrateur du canal
(`notifications/telegram_media.py`).

Une troisième chose est possible sans balayer le canal : `fetch_message_media`
**retrouve une publication précise** dans l'aperçu (`?before=<id>`), pour
remettre la main sur les octets d'un média dont l'objet a disparu du bucket
(`core/media_repair.py`). Elle ne dépend donc pas d'un `telegram_file_id`, que
seul le bot possède.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from ai import media_indexing
from database import knowledge_index, media_store
from database.supabase_client import insert_insight

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

#: Provenance enregistrée pour les médias issus du scraping — distincte de
#: `telegram`, qui désigne les médias reçus directement par le bot.
CHANNEL_SOURCE = "telegram_channel"

#: Garde-fou mémoire : on refuse un fichier plus gros que ça plutôt que de le
#: charger entièrement (l'aperçu ne dit souvent pas la taille à l'avance).
MAX_MEDIA_BYTES = 20 * 1024 * 1024

#: Extensions déduites du type MIME, pour les URL qui n'en portent pas (les
#: photos Telegram sont servies sous une simple empreinte hexadécimale).
_EXTENSION_BY_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "application/pdf": ".pdf",
}

_BACKGROUND_URL = re.compile(r"background-image\s*:\s*url\('([^']+)'\)")


def _make_soup(html: str) -> BeautifulSoup:
    """Parse le HTML avec `lxml` si disponible, sinon le parseur standard.

    `lxml` est déclaré mais peut manquer (pas de wheel selon la version de
    Python) : `html.parser` évite de rendre tout le scraper inopérant pour ça.
    """
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


def _post_message_id(data_post: Optional[str]) -> Optional[int]:
    """Identifiant numérique d'une publication, depuis `data-post`.

    `data-post` vaut `<canal>/<id>` — l'id sert de clé stable pour rejouer une
    ingestion sans dupliquer le fichier dans le bucket.
    """
    tail = str(data_post or "").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


def _is_file_url(url: Optional[str]) -> bool:
    """Vrai si l'URL pointe vers un fichier téléchargeable (CDN), pas vers t.me.

    C'est ce qui distingue une photo (URL `cdn*.telesco.pe`) d'un document, dont
    le lien de l'aperçu n'est qu'un renvoi vers la publication.
    """
    host = (urlparse(str(url or "")).hostname or "").lower()
    return host.endswith("telesco.pe") or host.endswith("cdn-telegram.org")


def _photo_url(element: Any) -> Optional[str]:
    """URL du fichier d'une photo, extraite du `style` (`background-image`)."""
    match = _BACKGROUND_URL.search(element.get("style") or "")
    return match.group(1) if match else None


def _file_extension(url: str, content_type: Optional[str]) -> str:
    """Extension à donner au fichier : type MIME d'abord, URL en repli."""
    mime = (content_type or "").split(";")[0].strip().lower()
    if mime in _EXTENSION_BY_MIME:
        return _EXTENSION_BY_MIME[mime]
    suffix = Path(urlparse(url).path).suffix
    return suffix if suffix and len(suffix) <= 6 else ".bin"


def _post_text(post: Any) -> Optional[str]:
    """Texte d'une publication — la **légende** de ses médias dans l'aperçu.

    Telegram n'expose pas la légende d'un média séparément sur la page publique :
    c'est le texte de la publication, et il vaut pour l'album entier (un album
    partage une seule légende). On l'attache donc à chaque descripteur, exactement
    comme le bot attache la vraie légende — sans quoi une photo de canal
    n'arriverait à l'index qu'avec la description qu'en fait la vision, qui ne
    nomme pas forcément l'actif.
    """
    block = post.select_one("div.tgme_widget_message_text")
    if block is None:
        return None
    text = block.get_text(separator=" ", strip=True)
    return text or None


def _media_descriptors(post: Any, channel: str) -> List[Dict[str, Any]]:
    """Médias téléchargeables d'une publication (photos, vidéos).

    Un descripteur porte l'URL du fichier, son type, un nom éventuel, l'id du
    message et la légende de la publication. Les médias sans URL de fichier (les
    documents) ne sont pas inclus.
    """
    message_id = _post_message_id(post.get("data-post"))
    caption = _post_text(post)
    found: List[Dict[str, Any]] = []

    for wrap in post.select("a.tgme_widget_message_photo_wrap"):
        url = _photo_url(wrap)
        if _is_file_url(url):
            found.append(
                {
                    "url": url,
                    "media_type": "photo",
                    "file_name": None,
                    "message_id": message_id,
                    "caption": caption,
                }
            )

    for video in post.select("video.tgme_widget_message_video"):
        url = video.get("src")
        if _is_file_url(url):
            found.append(
                {
                    "url": url,
                    "media_type": "video",
                    "file_name": None,
                    "message_id": message_id,
                    "caption": caption,
                }
            )

    for wrap in post.select("a.tgme_widget_message_document_wrap"):
        url = wrap.get("href")
        if not _is_file_url(url):
            continue  # aperçu sans URL de fichier : rien à télécharger
        title = wrap.select_one(".tgme_widget_message_document_title")
        found.append(
            {
                "url": url,
                "media_type": "document",
                "file_name": title.get_text(strip=True) if title else None,
                "message_id": message_id,
                "caption": caption,
            }
        )

    return found


def _count_skipped_documents(posts: List[Any]) -> int:
    """Documents présents dans l'aperçu mais non téléchargeables (sans URL)."""
    return sum(
        1
        for post in posts
        for wrap in post.select("a.tgme_widget_message_document_wrap")
        if not _is_file_url(wrap.get("href"))
    )


def parse_channel_html(html: str, channel: str, limit: int = 15) -> Dict[str, Any]:
    """Analyse une page d'aperçu : texte → insights, médias téléchargeables.

    Fonction pure (aucun réseau) : elle prend le HTML et rend

    * `insights` — les publications prêtes pour `insert_insight` ;
    * `media` — les descripteurs de médias à télécharger ;
    * `skipped_documents` — les documents vus mais non téléchargeables, pour
      pouvoir le dire à l'utilisateur au lieu de les ignorer en silence.
    """
    soup = _make_soup(html)

    insights: List[Dict[str, Any]] = []
    for msg in soup.select("div.tgme_widget_message_text")[-limit:]:
        text = msg.get_text(separator=" ", strip=True)
        if not text or len(text) < 5:
            continue
        insights.append(
            {
                "type": "telegram_channel",
                "asset": "GLOBAL",
                "title": f"Canal Telegram @{channel}",
                "summary": text[:1200],
                "source": f"https://t.me/{channel}",
                "confidence": 0.55,
                "data": {"channel": channel},
            }
        )

    posts = soup.select("div.tgme_widget_message")[-limit:]
    media: List[Dict[str, Any]] = []
    for post in posts:
        media.extend(_media_descriptors(post, channel))

    return {
        "insights": insights,
        "media": media,
        "skipped_documents": _count_skipped_documents(posts),
    }


#: Publications demandées autour de la cible (`?before=<id>` rend les
#: publications **précédentes**, la cible étant la dernière) : une petite fenêtre
#: absorbe les publications vides (sans texte ni média) qui décaleraient la
#: dernière entrée.
MESSAGE_PREVIEW_LIMIT = 5


def message_page_url(channel: str, before: int) -> str:
    """URL d'aperçu des publications **précédant** `before` (inclus).

    `?before=N` ne rend que les publications d'id inférieur à `N` : pour inclure
    la cible `X`, on demande `X + 1`. C'est ce qui évite de re-balayer tout le
    canal pour retrouver un média ancien.
    """
    return f"https://t.me/s/{channel}?before={int(before) + 1}"


async def fetch_message_media(
    channel: str,
    message_id: int,
    *,
    client: Optional[httpx.AsyncClient] = None,
    max_bytes: int = MAX_MEDIA_BYTES,
) -> Optional[Dict[str, Any]]:
    """Octets d'un **message précis** du canal (photo, vidéo ou document exposé).

    Rend `{"data", "mime_type", "file_name", "media_type", "url"}` ou `None`
    quand l'aperçu ne permet pas de conclure : publication absente de la fenêtre
    demandée (supprimée, ou antérieure à ce que l'aperçu accepte de rendre), ou
    média sans URL de fichier (un document, que l'aperçu n'expose jamais). Les
    deux cas se confondent volontairement — `None` veut dire « pas par cette
    voie », pas « panne » : une panne réseau, elle, **lève**.

    Le média le plus volumineux du message est choisi s'il en porte plusieurs
    (un album) : `knowledge_media` décrit un fichier par ligne, donc l'appelant
    sait exactement quel objet il répare.
    """
    url = message_page_url(channel, message_id)

    async def _fetch(http_client: httpx.AsyncClient) -> Optional[Dict[str, Any]]:
        response = await http_client.get(url)
        response.raise_for_status()
        parsed = parse_channel_html(response.text, channel, MESSAGE_PREVIEW_LIMIT)
        wanted = [m for m in parsed["media"] if m.get("message_id") == message_id]
        if not wanted:
            return None
        descriptor = wanted[0]
        downloaded = await _download(http_client, descriptor["url"], max_bytes)
        if downloaded is None:
            return None
        data, content_type = downloaded
        return {
            "data": data,
            "mime_type": content_type,
            "file_name": _file_name(descriptor, content_type),
            "media_type": descriptor["media_type"],
            "url": descriptor["url"],
        }

    if client is not None:
        return await _fetch(client)
    async with httpx.AsyncClient(timeout=15, headers=HEADERS) as owned:
        return await _fetch(owned)


async def _download(client: httpx.AsyncClient, url: str, max_bytes: int):
    """Télécharge en flux, sous plafond. Rend `(octets, type_mime)` ou `None`.

    Le flux évite de charger un fichier énorme en mémoire avant de le refuser :
    on coupe dès que le plafond est franchi.
    """
    async with client.stream("GET", url, headers=HEADERS) as response:
        response.raise_for_status()
        content_type = response.headers.get("content-type")
        data = bytearray()
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > max_bytes:
                return None
    return bytes(data), content_type


def _file_name(descriptor: Dict[str, Any], content_type: Optional[str]) -> str:
    """Nom de fichier stable : le titre du document, sinon `<type>_<id><ext>`."""
    title = (descriptor.get("file_name") or "").strip()
    if title:
        return Path(title).name
    media_type = descriptor.get("media_type") or "media"
    return f"{media_type}_{descriptor.get('message_id')}{_file_extension(descriptor['url'], content_type)}"


async def _already_indexed(
    channel: str,
    message_id: int,
    *,
    find_message: Any,
    chunks: Any,
) -> bool:
    """Vrai si ce message de canal est **déjà stocké et indexé**.

    C'est ce qui rend le balayage répétable : l'aperçu redonne les mêmes quinze
    publications à chaque passage, et sans cette reconnaissance on relancerait une
    extraction (vision Gemini, Whisper) par média et par balayage — même quota,
    même texte —, en plus de retélécharger pour rien. La clé est `(canal,
    message)`, la même que celle du bot, donc un média déjà pris par la route du
    bot ne sera pas non plus réextrait ici.

    Un doute (lecture impossible) rend `False` : mieux vaut une extraction refaite
    qu'un média jamais indexé.
    """
    try:
        row = await asyncio.to_thread(find_message, channel, message_id)
    except Exception as exc:
        print(f"   [canal @{channel}] lecture du média impossible : {type(exc).__name__}: {exc}")
        return False
    media_id = media_indexing.media_id_of(row)
    if not media_id:
        return False
    try:
        return bool(await asyncio.to_thread(chunks, media_id))
    except Exception as exc:
        print(f"   [canal @{channel}] morceaux illisibles ({media_id}) : {type(exc).__name__}: {exc}")
        return False


async def ingest_channel_media(
    channel: str,
    media: List[Dict[str, Any]],
    *,
    client: httpx.AsyncClient,
    upload: Any = None,
    max_bytes: int = MAX_MEDIA_BYTES,
    max_extractions: Optional[int] = None,
    index: Any = None,
    tag: Any = None,
    find_message: Any = None,
    chunks: Any = None,
) -> Dict[str, int]:
    """Télécharge chaque média, le stocke, puis **indexe son texte**.

    Le stockage et l'indexation sont le même parcours que celui du bot
    (`ai.media_indexing`) : sans cela, une photo de canal serait rangée dans le
    bucket mais son texte n'existerait nulle part — invisible pour `/search`
    comme pour le contexte média du moteur de décision. C'est tout l'intérêt de
    la route : une lecture de graphique par **vision Gemini**, rendue cherchable.

    Un message déjà indexé est ignoré **avant** le téléchargement
    (`_already_indexed`). Ne lève jamais : un média en échec est signalé et
    n'empêche pas les suivants. Retourne `{"stored", "indexed",
    "already_indexed", "deferred"}` — les comptes sont distincts parce qu'ils ne
    racontent pas la même chose : un média stocké sans texte n'apporte rien à la
    recherche, et « ignoré » n'est pas un échec.

    `max_extractions` plafonne le nombre d'**extractions lancées** dans ce
    passage (`None` ou `0` = sans plafond). Un canal qui publie un album de vingt
    photos déclencherait sinon vingt appels de vision — le quota Gemini d'un
    après-midi — sur un seul balayage. Ce qui dépasse est **reporté**, pas
    abandonné : `deferred` les compte, et le balayage suivant les reprend, puisque
    l'aperçu redonne les mêmes publications et que celles déjà indexées en sont
    écartées sans rien coûter. Seules les extractions **réellement lancées**
    consomment le plafond : un téléchargement ou un stockage raté ne coûte aucun
    quota, donc il ne doit pas en coûter la place à un autre média.
    """
    upload = upload or media_store.upload_media
    index = index or media_indexing.extract_and_index
    tag = tag or media_indexing.store_asset
    find_message = find_message or media_store.find_media_by_message
    chunks = chunks or knowledge_index.has_chunks
    #: `0` — et toute valeur non positive — a la même signification que `None`
    #: (« sans plafond ») : c'est ainsi que la configuration l'écrit, et faire
    #: diverger les notations ici reproduirait exactement le défaut qu'on cherche à
    #: éviter — une collecte qui ne collecte plus rien, sans que rien ne le dise.
    budget = int(max_extractions or 0)
    budget = budget if budget > 0 else None

    counts = {"stored": 0, "indexed": 0, "already_indexed": 0, "deferred": 0}
    attempted = 0
    for descriptor in media:
        message_id = descriptor.get("message_id")
        if message_id is None:
            print(f"   [canal @{channel}] média sans identifiant de message : ignoré")
            continue
        if await _already_indexed(
            channel, message_id, find_message=find_message, chunks=chunks
        ):
            counts["already_indexed"] += 1
            continue
        if budget is not None and attempted >= budget:
            #: Le compte est tenu **avant** le téléchargement : le plafond borne le
            #: travail, pas seulement l'extraction — mais l'incrément, lui, attend
            #: l'extraction réelle (voir plus bas).
            counts["deferred"] += 1
            continue
        try:
            downloaded = await _download(client, descriptor["url"], max_bytes)
        except Exception as exc:
            print(f"   [canal @{channel}] téléchargement impossible : {type(exc).__name__}: {exc}")
            continue
        if downloaded is None:
            print(f"   [canal @{channel}] média trop volumineux (> {max_bytes // (1024 * 1024)} Mo) : ignoré")
            continue

        data, content_type = downloaded
        name = _file_name(descriptor, content_type)
        try:
            # `upload_media` est bloquant (réseau) : hors de l'event loop.
            row = await asyncio.to_thread(
                upload,
                data,
                media_type=descriptor["media_type"],
                mime_type=content_type,
                file_name=name,
                source=CHANNEL_SOURCE,
                chat_id=channel,
                message_id=message_id,
                metadata={"channel": channel, "web_preview": True},
                upsert=True,
            )
        except Exception as exc:
            print(f"   [canal @{channel}] stockage impossible ({name}) : {type(exc).__name__}: {exc}")
            continue
        counts["stored"] += 1
        #: Une extraction va être lancée : c'est **elle** qui consomme le quota,
        #: donc c'est ici que le plafond se décompte — et non à la réservation du
        #: créneau, qui aurait laissé un transfert raté manger la place d'un autre.
        attempted += 1

        # Le descripteur complété : c'est le même dictionnaire que celui du bot
        # (`media_type`, `mime_type`, `file_name`, `caption`), donc la même
        # fonction d'indexation, sans adaptation.
        indexed = await _index_media(
            channel,
            data,
            {**descriptor, "mime_type": content_type, "file_name": name},
            row,
            index=index,
            tag=tag,
        )
        if indexed:
            counts["indexed"] += 1

    if counts["deferred"]:
        print(
            f"   [canal @{channel}] {counts['deferred']} extraction(s) reportée(s) au "
            f"prochain balayage (plafond {budget} par passage)"
        )
    return counts


async def _index_media(
    channel: str,
    data: bytes,
    descriptor: Dict[str, Any],
    row: Any,
    *,
    index: Any,
    tag: Any,
) -> bool:
    """Extrait et indexe un média **déjà stocké** ; dit ce qui s'est passé.

    Ne lève jamais : le média est dans le bucket, une extraction impossible est
    rapportée (légende indexée seule, vision indisponible…). Retourne `True` quand
    du texte est effectivement parti dans l'index.
    """
    media_id = media_indexing.media_id_of(row)
    try:
        summary = await index(data, descriptor, media_id=media_id)
    except Exception as exc:
        print(f"   [canal @{channel}] indexation impossible : {type(exc).__name__}: {exc}")
        return False
    if summary.get("asset"):
        await tag(media_id, summary.get("asset"), summary.get("asset_source"))
    if not summary.get("chunks"):
        print(
            f"   [canal @{channel}] message {descriptor.get('message_id')} sans texte "
            f"à indexer ({summary.get('reason') or 'extraction vide'})"
        )
        return False
    return True


async def fetch_and_push_telegram_channel(
    channel: str,
    limit: int = 15,
    *,
    client: Optional[httpx.AsyncClient] = None,
    upload: Any = None,
    index: Any = None,
    tag: Any = None,
    find_message: Any = None,
    chunks: Any = None,
    max_extractions: Optional[int] = None,
) -> Dict[str, Any]:
    """Récupère les derniers messages d'un canal public : texte ET médias.

    Le texte alimente la table `insights` ; les médias sont stockés via
    `media_store` **et indexés** (texte extrait par vision/transcription), donc
    cherchables dans `/search` et injectables dans le contexte du moteur de
    décision. Retourne `{"insights", "media", "indexed", "skipped_documents",
    "deferred", "error"}`, où `deferred` compte les extractions laissées au
    balayage suivant par `max_extractions` (`None` ou `0` = sans plafond) et
    `error` nomme le motif quand l'aperçu est injoignable (sinon `None`).

    Un échec de lecture rend tous les comptes à **zéro** : ils ne mesurent rien,
    et sans le motif ils seraient indiscernables d'un canal calme — d'où `error`,
    que `workers/scan_backlog.py` suit balayage après balayage.

    Les collaborateurs (client HTTP, upload, indexation, lecture des doublons)
    sont injectables pour rester testable sans réseau ni base.
    """
    url = f"https://t.me/s/{channel}"

    async def _ingest(http_client: httpx.AsyncClient, parsed: Dict[str, Any]) -> Dict[str, int]:
        return await ingest_channel_media(
            channel,
            parsed["media"],
            client=http_client,
            upload=upload,
            index=index,
            tag=tag,
            find_message=find_message,
            chunks=chunks,
            max_extractions=max_extractions,
        )

    try:
        if client is not None:
            response = await client.get(url)
            response.raise_for_status()
            parsed = parse_channel_html(response.text, channel, limit)
            counts = await _ingest(client, parsed)
        else:
            async with httpx.AsyncClient(timeout=15, headers=HEADERS) as owned:
                response = await owned.get(url)
                response.raise_for_status()
                parsed = parse_channel_html(response.text, channel, limit)
                counts = await _ingest(owned, parsed)
    except Exception as exc:
        print(f"   [canal @{channel}] erreur : {type(exc).__name__}: {exc}")
        return {
            "insights": 0,
            "media": 0,
            "indexed": 0,
            "skipped_documents": 0,
            "deferred": 0,
            #: Rien n'a été **lu** : les zéros ci-dessus ne sont pas une mesure.
            #: Le motif est rendu tel quel, pour que l'appelant puisse le nommer
            #: sans avoir à deviner (`workers/scan_backlog.py`).
            "error": f"{type(exc).__name__}: {exc}",
        }

    pushed = 0
    for insight in parsed["insights"]:
        try:
            await asyncio.to_thread(insert_insight, insight)
            pushed += 1
        except Exception as exc:
            print(f"   [canal @{channel}] insight non enregistre : {type(exc).__name__}: {exc}")

    if parsed["skipped_documents"]:
        print(
            f"   [canal @{channel}] {parsed['skipped_documents']} document(s) ignoré(s) : "
            "l'aperçu web n'expose pas l'URL du fichier"
        )

    return {
        "insights": pushed,
        "media": counts["stored"],
        "indexed": counts["indexed"],
        "skipped_documents": parsed["skipped_documents"],
        "deferred": counts["deferred"],
        #: La lecture a abouti : c'est cette absence de motif qui solde la suite
        #: d'échecs du canal, côté suivi du balayage.
        "error": None,
    }
