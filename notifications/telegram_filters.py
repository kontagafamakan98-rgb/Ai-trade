"""Filtres de routage des mises à jour Telegram portant un média.

Pourquoi un module à part : `main.py` n'est pas importable en test (il construit
le moteur, charge `dotenv`, exige des secrets), donc sa table de handlers n'était
vérifiable qu'en lisant son source. Or c'est là que se joue le **routage** :
`Application` ne remet une mise à jour qu'à **un seul** handler par groupe — le
premier dont le filtre accepte l'update. Deux conséquences, toutes deux vérifiées
par des tests ici :

* `filters.PHOTO` seul accepte aussi bien une photo de chat privé qu'une photo
  **publiée dans un canal** : sans exclusion explicite, une publication de canal
  atterrirait dans le handler « privé », qui répond dans le chat — donc dans le
  canal, devant tous les abonnés. D'où `CHANNEL_MEDIA` d'un côté et les filtres
  `DIRECT_*` (hors canal) de l'autre.
* les noms de filtres de la bibliothèque **changent** : `filters.DOCUMENT`
  n'existe plus avec `python-telegram-bot` 22 (remplacé par
  `filters.Document.ALL`). Un nom disparu ne se voit qu'au démarrage du bot —
  c'est-à-dire en production. Importer et composer ces filtres dans un module
  testé fait échouer la suite de tests à la place.

`DIRECT_` désigne tout ce qui n'est pas un canal : chat privé, groupe,
supergroupe — le comportement d'origine, inchangé.
"""
from telegram.ext import filters

#: Types de médias ingérés, avec le filtre qui les reconnaît. Un document est
#: pris par `filters.Document.ALL` : les documents envoyés comme fichiers
#: (`document`) et non comme `video`/`audio` — c'est la route qui récupère ce que
#: l'aperçu public ne donne jamais, et le nom d'origine du fichier avec.
_DIRECT_MEDIA = (
    filters.PHOTO
    | filters.Document.ALL
    | filters.VIDEO
    | filters.VOICE
    | filters.AUDIO
)

#: Publication d'un **canal** où le bot est administrateur.
CHANNEL_MEDIA = filters.ChatType.CHANNEL & _DIRECT_MEDIA

#: Médias hors canal, un filtre par type — l'ordre d'enregistrement dans
#: `main.py` choisit lequel traite la mise à jour.
DIRECT_MEDIA = ~filters.ChatType.CHANNEL & _DIRECT_MEDIA
DIRECT_PHOTO = ~filters.ChatType.CHANNEL & filters.PHOTO
DIRECT_VIDEO = ~filters.ChatType.CHANNEL & filters.VIDEO
DIRECT_DOCUMENT = ~filters.ChatType.CHANNEL & filters.Document.ALL
DIRECT_VOICE = ~filters.ChatType.CHANNEL & filters.VOICE
DIRECT_AUDIO = ~filters.ChatType.CHANNEL & filters.AUDIO

__all__ = [
    "CHANNEL_MEDIA",
    "DIRECT_MEDIA",
    "DIRECT_PHOTO",
    "DIRECT_VIDEO",
    "DIRECT_DOCUMENT",
    "DIRECT_VOICE",
    "DIRECT_AUDIO",
]
