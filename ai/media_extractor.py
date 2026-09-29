"""Extraction de contenu des médias ingérés (texte exploitable).

Trois extracteurs, chacun délégué à un fournisseur **déjà utilisé ailleurs** dans
le projet — aucun service payant, aucun compte supplémentaire :

* **images** (graphiques, captures) → **vision Gemini** : lit tendance, niveaux,
  indicateurs et chiffres visibles sur le graphique ;
* **PDF** → **pypdf**, extraction locale du texte (aucun réseau) ;
* **vidéos et notes vocales** → **transcription** : Groq `whisper-large-v3`
  quand `GROQ_API_KEY` est configurée, sinon **`faster-whisper` en local**. La
  transcription est la seule extraction qui exigeait un accès réseau **et** un
  compte : sans clé, le média n'était plus transcrit du tout (`transcribe_media`).

Deux principes de conception :

* les dépendances lourdes (`groq`, `faster-whisper`, `pypdf`, `httpx`) sont
  importées **au moment de l'usage**, jamais à l'import du module : une
  installation sans elles doit continuer à démarrer et à ingérer les médias ;
  seule l'extraction échouera, avec un motif explicite. C'est la même règle que
  `notifications/notify.py` pour Telegram ;
* une extraction impossible n'est **jamais** une exception qui remonte : elle est
  retournée comme compte-rendu (`{"ok": False, "reason": ...}`), pour que
  l'ingestion du média reste, elle, réussie.

La sortie est un dictionnaire ::

    {"ok": bool, "text": str, "method": str, "reason": str | None}
"""
from __future__ import annotations

import base64
import importlib.util
import io
import threading
from typing import Any, Dict, Optional

from config import GEMINI_API_KEY, GEMINI_MODEL, GROQ_API_KEY

#: Modèle de transcription Groq (gratuit, multilingue).
GROQ_WHISPER_MODEL = "whisper-large-v3"

#: Paquet fournissant le repli local. Nommé une fois : c'est ce que cherche
#: `local_transcription_available()`, et donc ce que publie `/preflight`.
LOCAL_WHISPER_PACKAGE = "faster_whisper"

#: Modèle du repli **local** (`faster-whisper`). Ce peut être un nom (`tiny`,
#: `base`, `small`, `medium`, `large-v3`) **ou le chemin d'un dossier déjà
#: téléchargé** : c'est ce second cas qui compte hors ligne, où le premier
#: téléchargement des poids est justement impossible.
LOCAL_WHISPER_MODEL = "small"

#: Calcul local : `int8` sur CPU tourne partout, sans GPU ni dépendance CUDA.
#: C'est plus lent que Groq — le local est un repli, pas une seconde voie
#: nominale.
LOCAL_WHISPER_DEVICE = "cpu"
LOCAL_WHISPER_COMPUTE_TYPE = "int8"

#: Les modèles locaux déjà chargés, par nom. `WhisperModel` lit des centaines de
#: mégaoctets de poids : les recharger à chaque média rendrait le repli
#: inutilisable, et deux médias ingérés en parallèle les chargeraient deux fois
#: (le pipeline appelle l'extraction depuis `asyncio.to_thread`).
_local_models: Dict[str, Any] = {}
_local_models_lock = threading.Lock()

#: Endpoint Gemini `generateContent` (vision incluse, comme dans `ai/news_analyzer.py`).
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

#: Consigne donnée au modèle de vision. On demande une lecture **factuelle** du
#: graphique, sans recommandation d'achat/vente : l'extraction alimente une base
#: de connaissances, elle ne produit pas de signal.
GEMINI_VISION_PROMPT = (
    "Tu analyses une capture d'écran de graphique financier (ou une image liée "
    "au trading). Décris de façon strictement factuelle ce qui est visible :\n"
    "- l'actif, l'unité de temps et la période si lisibles ;\n"
    "- la tendance générale et les mouvements notables ;\n"
    "- les niveaux de support/résistance et tout chiffre lisible (prix, "
    "indicateurs, RSI, moyennes mobiles, volumes) ;\n"
    "- toute annotation, flèche ou note présente sur l'image.\n"
    "Ne donne AUCUNE recommandation d'achat ou de vente. Si l'image n'est pas "
    "un graphique ou est illisible, dis-le simplement. Réponds en français, en "
    "texte brut concis."
)

#: Exensions de repli quand Telegram ne fournit pas de nom de fichier : Whisper
#: choisit son décodeur d'après l'extension, il lui en faut donc une valide.
_EXTENSION_BY_MIME = {
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/webm": ".webm",
    "audio/flac": ".flac",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "video/x-matroska": ".mkv",
}


def _ok(method: str, text: str) -> Dict[str, Any]:
    return {"ok": True, "text": text, "method": method, "reason": None}


def _fail(method: str, reason: str) -> Dict[str, Any]:
    return {"ok": False, "text": "", "method": method, "reason": reason}


def classify(media_type: Optional[str], mime_type: Optional[str], file_name: Optional[str]) -> Optional[str]:
    """Classe un média en `"image"`, `"pdf"`, `"audio"` — ou `None` si non pris en charge.

    Le type MIME prime ; le `media_type` (photo/video/voice/document) et
    l'extension servent de repli quand Telegram ne renseigne pas de MIME.
    """
    media = (media_type or "").lower()
    mime = (mime_type or "").lower()
    name = (file_name or "").lower()

    if mime.startswith("image/") or media == "photo":
        return "image"
    if mime == "application/pdf" or name.endswith(".pdf"):
        return "pdf"
    if mime.startswith(("audio/", "video/")) or media in ("voice", "video", "audio"):
        return "audio"
    return None


def _filename_for(file_name: Optional[str], mime_type: Optional[str]) -> str:
    """Garantit une extension exploitable par Whisper."""
    name = file_name or ""
    if name and "." in name.rsplit("/", 1)[-1]:
        return name
    extension = _EXTENSION_BY_MIME.get((mime_type or "").lower(), ".ogg")
    stem = name or "media"
    return f"{stem}{extension}"


def extract_text(
    data: bytes,
    *,
    media_type: Optional[str] = None,
    mime_type: Optional[str] = None,
    file_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Extrait le texte exploitable d'un média, selon son type.

    Ne lève jamais : renvoie un compte-rendu. Réseau/CPU bloquants : à appeler
    hors de l'event loop (`asyncio.to_thread`).
    """
    kind = classify(media_type, mime_type, file_name)
    if kind == "image":
        return extract_image_text_gemini(data, mime_type=mime_type)
    if kind == "pdf":
        return extract_pdf_text(data)
    if kind == "audio":
        return transcribe_media(data, mime_type=mime_type, file_name=file_name)
    return _fail(
        "unsupported",
        f"type non pris en charge ({media_type or '?'} / {mime_type or '?'})",
    )


def extract_image_text_gemini(data: bytes, *, mime_type: Optional[str] = None) -> Dict[str, Any]:
    """Décrit une image (graphique) via la vision Gemini."""
    method = "gemini_vision"
    if not GEMINI_API_KEY:
        return _fail(method, "GEMINI_API_KEY absente : vision indisponible")
    try:
        import httpx
    except ImportError:
        return _fail(method, "httpx non installé")

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": GEMINI_VISION_PROMPT},
                    {
                        "inline_data": {
                            "mime_type": (mime_type or "image/jpeg"),
                            "data": base64.b64encode(data).decode("ascii"),
                        }
                    },
                ]
            }
        ],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 1200},
    }

    try:
        with httpx.Client(timeout=60) as client:
            response = client.post(
                GEMINI_URL,
                headers={
                    "Content-Type": "application/json",
                    "X-goog-api-key": GEMINI_API_KEY,
                },
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
    except Exception as exc:
        return _fail(method, f"appel Gemini impossible : {exc}")

    text = _gemini_text(body)
    if not text:
        return _fail(method, "réponse Gemini vide")
    return _ok(method, text)


def _gemini_text(body: Dict[str, Any]) -> str:
    """Concatène les morceaux texte de la première réponse Gemini."""
    candidates = body.get("candidates") or []
    if not candidates:
        return ""
    parts = (candidates[0].get("content") or {}).get("parts") or []
    return "".join(str(part.get("text", "")) for part in parts).strip()


def extract_pdf_text(data: bytes) -> Dict[str, Any]:
    """Extrait le texte d'un PDF avec `pypdf` (local, sans réseau)."""
    method = "pypdf"
    try:
        from pypdf import PdfReader
    except ImportError:
        return _fail(method, "pypdf non installé (pip install pypdf)")

    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:
        return _fail(method, f"lecture PDF impossible : {exc}")

    text = "\n".join(pages).strip()
    if not text:
        return _fail(method, "PDF sans texte extractible (probablement scanné)")
    return _ok(method, text)


def transcribe_media_groq(
    data: bytes,
    *,
    mime_type: Optional[str] = None,
    file_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Transcrit une vidéo ou une note vocale via Groq `whisper-large-v3`.

    Whisper lit la **piste audio** du conteneur : Telegram envoie les vidéos en
    MP4 et les notes vocales en OGG, deux formats acceptés nativement — aucune
    extraction audio locale (ffmpeg) n'est donc nécessaire.
    """
    method = "groq_whisper"
    if not GROQ_API_KEY:
        return _fail(method, "GROQ_API_KEY absente : transcription indisponible")
    try:
        from groq import Groq
    except ImportError:
        return _fail(method, "groq non installé (pip install groq)")

    name = _filename_for(file_name, mime_type)
    try:
        client = Groq(api_key=GROQ_API_KEY)
        response = client.audio.transcriptions.create(
            file=(name, data, mime_type or "application/octet-stream"),
            model=GROQ_WHISPER_MODEL,
            response_format="json",
        )
    except Exception as exc:
        return _fail(method, f"transcription Groq impossible : {exc}")

    text = getattr(response, "text", None)
    if text is None and isinstance(response, dict):
        text = response.get("text")
    text = (text or "").strip()
    if not text:
        return _fail(method, "transcription vide")
    return _ok(method, text)


def _package_available(name: str) -> bool:
    """Le paquet `name` est-il **importable**, sans l'importer ?

    `find_spec` et non `import` : charger `faster-whisper` (ou `pypdf`) pour
    répondre à cette question lirait ses bibliothèques natives pour rien —
    `/health`, `/preflight` et `/transcribe` l'appellent.
    """
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):  # paquet parent absent, chemin exotique…
        return False


def local_transcription_available() -> bool:
    """Le repli local est-il **installé** (paquet présent, poids non vérifiés) ?

    Le paquet peut être installé sans qu'aucun modèle ait été téléchargé : c'est
    ce que dit le motif d'échec de `transcribe_media_local`, et ce que
    `extraction_readiness` ne prétend donc pas savoir.
    """
    return _package_available(LOCAL_WHISPER_PACKAGE)


def extraction_readiness(
    media_type: Optional[str] = None,
    mime_type: Optional[str] = None,
    file_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Ce que l'extraction de ce média exige, et ce qui manque **maintenant**.

    Répond à la question qu'on se pose devant un média resté sans texte : « si je
    relance, ai-je une chance ? ». Trois sortes de médias, trois façons de
    pouvoir échouer avant même de lire les octets :

    * ``image`` — la vision Gemini, donc ``GEMINI_API_KEY`` (et ``httpx``) ;
    * ``pdf`` — ``pypdf``, en local, sans clef ni réseau ;
    * ``audio`` — Groq d'abord, le repli ``faster-whisper`` ensuite : il suffit
      que **l'un des deux** soit là, et le repli seul reste une réponse positive
      (plus lente, c'est ce que dit `hint`).

    ``ready`` est un **verdict sur les ingrédients**, pas une promesse de texte :
    les poids du modèle local peuvent manquer paquet installé, un PDF peut être
    scanné, une clef peut être valide et le quota épuisé. Ces échecs-là se
    découvrent en essayant — refuser le travail au motif qu'ils *peuvent* arriver
    reviendrait à ne jamais rien relancer.
    """
    kind = classify(media_type, mime_type, file_name)

    if kind == "image":
        if not GEMINI_API_KEY:
            return {
                "kind": kind,
                "ready": False,
                "missing": ["GEMINI_API_KEY"],
                "hint": (
                    "La lecture d'une image passe par la vision Gemini : "
                    "renseigne GEMINI_API_KEY dans `.env`."
                ),
            }
        if not _package_available("httpx"):
            return {
                "kind": kind,
                "ready": False,
                "missing": ["httpx"],
                "hint": "L'appel Gemini est fait par `httpx` : pip install httpx",
            }
        return {"kind": kind, "ready": True, "missing": [], "hint": ""}

    if kind == "pdf":
        if not _package_available("pypdf"):
            return {
                "kind": kind,
                "ready": False,
                "missing": ["pypdf"],
                "hint": "La lecture des PDF est locale (`pypdf`) : pip install pypdf",
            }
        return {"kind": kind, "ready": True, "missing": [], "hint": ""}

    if kind == "audio":
        voices = {"groq": bool(GROQ_API_KEY), "local": local_transcription_available()}
        if not any(voices.values()):
            return {
                "kind": kind,
                "ready": False,
                "voices": voices,
                "missing": ["GROQ_API_KEY", "faster-whisper"],
                "hint": (
                    "Ni clef Groq, ni repli local : renseigne GROQ_API_KEY "
                    "ou installe `faster-whisper` (voir README)."
                ),
            }
        if not voices["groq"]:
            return {
                "kind": kind,
                "ready": True,
                "voices": voices,
                "missing": [],
                "hint": (
                    "Sans GROQ_API_KEY, la transcription passera par le repli "
                    f"local `{LOCAL_WHISPER_MODEL}` : possible, mais plus lent."
                ),
            }
        return {"kind": kind, "ready": True, "voices": voices, "missing": [], "hint": ""}

    return {
        "kind": None,
        "ready": False,
        "missing": [],
        "hint": (
            "Type non pris en charge "
            f"({media_type or '?'} / {mime_type or '?'}) : aucune extraction possible."
        ),
    }


def transcribe_media_local(
    data: bytes,
    *,
    mime_type: Optional[str] = None,
    file_name: Optional[str] = None,
    model_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Transcrit localement avec `faster-whisper` : aucun réseau, aucun compte.

    C'est le repli **hors ligne**. Deux différences avec Groq, qui expliquent sa
    place : le modèle doit être *présent* (le premier téléchargement des poids a
    besoin d'un accès réseau, donc `LOCAL_WHISPER_MODEL` doit pointer un dossier
    local sur une machine coupée du réseau), et le calcul se fait sur le CPU —
    plus lent, mais sans quota.

    L'audio est remis à `faster-whisper` sous forme de flux : le format est lu
    dans le **conteneur** (PyAV), l'extension ne sert donc qu'à Groq.
    """
    method = "faster_whisper"
    name = model_name or LOCAL_WHISPER_MODEL
    try:
        model = _local_model(name)
    except ImportError:
        return _fail(method, "faster-whisper non installé (pip install faster-whisper)")
    except Exception as exc:
        # Cas typique : hors ligne **et** poids jamais téléchargés. Le dire, sinon
        # l'utilisateur ne voit qu'un échec de transcription sans cause.
        return _fail(
            method,
            f"modèle local « {name} » indisponible : {exc} "
            "(les poids doivent avoir été téléchargés une première fois, ou "
            "« LOCAL_WHISPER_MODEL » doit pointer un dossier local)",
        )

    try:
        # Le décodage se fait **pendant l'itération** : l'envelopper est la seule
        # façon d'attraper une transcription qui échoue en cours de route.
        segments, _info = model.transcribe(io.BytesIO(data))
        text = " ".join(str(segment.text or "").strip() for segment in segments).strip()
    except Exception as exc:
        return _fail(method, f"transcription locale impossible : {exc}")

    if not text:
        return _fail(method, "transcription vide")
    return _ok(method, text)


def _local_model(name: str) -> Any:
    """Le modèle local, chargé **une fois** par nom (les appels arrivent des threads)."""
    with _local_models_lock:
        model = _local_models.get(name)
        if model is None:
            from faster_whisper import WhisperModel

            model = WhisperModel(
                name, device=LOCAL_WHISPER_DEVICE, compute_type=LOCAL_WHISPER_COMPUTE_TYPE
            )
            _local_models[name] = model
        return model


def transcribe_media(
    data: bytes,
    *,
    mime_type: Optional[str] = None,
    file_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Transcrit une vidéo ou une note vocale : Groq, sinon le repli local.

    Groq reste la voie nominale — plus rapide, et rien à télécharger. Le repli
    local prend le relais dans les **deux** cas où Groq ne peut pas servir :

    * **sans `GROQ_API_KEY`** : la voie nominale est inutilisable par
      construction, on ne l'essaie donc même pas ;
    * **après un échec de Groq** : coupure réseau, quota épuisé, format
      refusé… C'est le cas d'une machine hors ligne **avec** une clé configurée,
      qui est justement celui qu'on veut couvrir.

    Si les deux échouent, le motif nomme les **deux** tentatives : « le local a
    échoué juste après Groq » n'est pas la même information qu'« aucune
    transcription n'est possible ».
    """
    if not GROQ_API_KEY:
        return transcribe_media_local(data, mime_type=mime_type, file_name=file_name)

    report = transcribe_media_groq(data, mime_type=mime_type, file_name=file_name)
    if report.get("ok"):
        return report

    local = transcribe_media_local(data, mime_type=mime_type, file_name=file_name)
    if local.get("ok"):
        return local
    return _fail(
        str(report.get("method") or "transcription"),
        f"Groq puis repli local : {report.get('reason')} ; {local.get('reason')}",
    )


__all__ = [
    "GEMINI_VISION_PROMPT",
    "GROQ_WHISPER_MODEL",
    "extraction_readiness",
    "LOCAL_WHISPER_COMPUTE_TYPE",
    "LOCAL_WHISPER_DEVICE",
    "LOCAL_WHISPER_MODEL",
    "LOCAL_WHISPER_PACKAGE",
    "classify",
    "extract_image_text_gemini",
    "extract_pdf_text",
    "extract_text",
    "local_transcription_available",
    "transcribe_media",
    "transcribe_media_groq",
    "transcribe_media_local",
]
