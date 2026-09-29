"""Base de connaissances PERMANENTE (règles de trading, notes Obsidian).

Distincte de la table `insights` (actualités éphémères). Chaque note est stockée
une seule fois par `source` (upsert).

Le contexte injecté dans les prompts n'est plus un extrait fixe : il provient
d'une **recherche vectorielle top-k** (`knowledge_chunks` + embeddings Gemini),
filtrée par **actif** et **régime de marché**. Si les embeddings sont
indisponibles (pas de `GEMINI_API_KEY`, panne, base vide), on retombe sur
l'ancien extrait des notes les plus récentes : un prompt ne doit jamais se
retrouver sans contexte.

Ce module expose aussi un bloc **médias** (`get_media_context`) : les lectures de
graphiques (vision Gemini) et les transcriptions (Whisper) ingérées via Telegram
sont indexées dans la même table ; leurs extraits les plus proches de l'actif
reviennent dans le prompt, dans une section **séparée** et sous leur propre
plafond — ce sont des observations datées, pas des règles permanentes.
"""
from typing import Any, Dict, List, Optional, Sequence

from database import knowledge_index
from database.supabase_client import supabase

TABLE = "knowledge_base"

# Plafond volontairement STRICT pour ne jamais faire exploser le budget de
# tokens des appels LLM (24 cycles/jour x 2 modèles = ce texte est envoyé
# ~48 fois par jour). 1200 caractères ≈ 300 tokens par appel, soit environ
# 14 000 tokens/jour ajoutés — une fraction du quota gratuit Groq (200 000/j).
# Ce n'est PAS tout ton vault, juste un extrait — augmente ce chiffre avec
# prudence si tu as de la marge de quota, pas au-delà de ~2500 sans risque.
MAX_CONTEXT_CHARS = 1200

#: Nombre de morceaux ramenés par la recherche vectorielle.
DEFAULT_TOP_K = knowledge_index.DEFAULT_TOP_K

#: Nombre de MÉDIAS remontés quand l'actif est connu.
DEFAULT_MEDIA_TOP_K = 3

# Plafond du bloc MÉDIAS, en PLUS de celui des notes : un extrait de graphique ou
# de note vocale est une observation, il ne doit pas évincer les règles
# permanentes. 900 caractères ≈ 225 tokens, soit ~10 800 tokens/jour au même
# rythme d'appels que le bloc de notes — quelques pour cent du quota gratuit.
MAX_MEDIA_CONTEXT_CHARS = 900

#: Seuil de similarité **de repli** pour retenir un média. Ce n'est plus la règle :
#: le seuil effectif est mesuré sur la base (`knowledge_index.calibration`, voir
#: `media_similarity_floor`), parce qu'une similarité cosinus n'a pas d'échelle
#: absolue — 0,5 rejette les bons extraits d'un corpus vaste et hétérogène, et
#: laisse passer du bruit sur un corpus de quelques dizaines de documents traitant
#: tous du même sujet. Cette constante ne sert donc que lorsque la mesure est
#: impossible (base non configurée, aucun vecteur, embeddings indisponibles) ou
#: quand l'appelant impose explicitement un seuil.
MIN_MEDIA_SIMILARITY = 0.5

#: Passages ramenés par la commande `/search`.
DEFAULT_SEARCH_TOP_K = 5

#: Borne haute de `/search` : au-delà, le message Telegram dépasserait sa limite
#: (4096 caractères) et serait tronqué par le client.
MAX_SEARCH_TOP_K = 20

#: Taille du **lot** interrogé une fois par `/search`, puis déroulé page par page
#: par `/search_more`. C'est ce qui permet d'afficher la suite sans relancer la
#: recherche : un second appel coûterait un embedding et pourrait reclasser les
#: passages, donc paginer sur un classement instable.
SEARCH_POOL_SIZE = MAX_SEARCH_TOP_K

#: Aperçu d'un passage dans la réponse de `/search`. Volontairement court : le but
#: est d'identifier le bon passage, pas de tout afficher dans le chat.
SEARCH_PREVIEW_CHARS = 400

#: Options de `/search` à valeur, et leurs formes courtes. Chacune accepte
#: `--option valeur`, `--option=valeur`, et son abréviation à une lettre (`-a`,
#: `-s`, `-r`, `-n`). Les clés sont **sans tiret** : `-source` et `--source`
#: désignent la même option, seule la convention d'écriture change.
SEARCH_OPTION_ALIASES = {
    "asset": "asset",
    "a": "asset",
    "source": "source",
    "s": "source",
    "regime": "regime",
    "r": "regime",
    "limit": "limit",
    "n": "limit",
    "top": "limit",
}

#: Sources acceptées pour `--source`. Les synonymes ne sont pas du confort :
#: personne ne devine que « médias » s'appelle `telegram` en base (c'est la
#: provenance des morceaux issus de l'ingestion Telegram), et une option qui
#: n'accepte qu'un nom interne est une option qu'on n'utilise pas.
SEARCH_SOURCE_ALIASES = {
    "note": knowledge_index.SOURCE_NOTE,
    "notes": knowledge_index.SOURCE_NOTE,
    "media": knowledge_index.SOURCE_MEDIA,
    "medias": knowledge_index.SOURCE_MEDIA,
    "média": knowledge_index.SOURCE_MEDIA,
    "médias": knowledge_index.SOURCE_MEDIA,
    "telegram": knowledge_index.SOURCE_MEDIA,
    "tout": None,
    "tous": None,
    "toutes": None,
    "all": None,
}

#: Options qui demandent l'aide plutôt qu'une recherche.
SEARCH_HELP_TOKENS = {"--help", "-h", "help", "aide"}


def upsert_note(source: str, title: str, content: str) -> None:
    supabase.table(TABLE).upsert({
        "source": source,
        "title": title,
        "content": content,
        "char_count": len(content),
    }).execute()

    # Réindexation vectorielle best-effort : une note reste créée même si
    # l'embedding échoue (l'extrait fixe prend alors le relais).
    _index_note(source, title, content)


def list_notes() -> List[Dict[str, Any]]:
    res = supabase.table(TABLE).select("source,title,char_count,updated_at").execute()
    return res.data or []


def get_knowledge_context(
    asset: Optional[str] = None,
    regime: Optional[str] = None,
    top_k: int = DEFAULT_TOP_K,
    max_chars: int = MAX_CONTEXT_CHARS,
) -> str:
    """Contexte de connaissances à injecter dans un prompt LLM.

    Recherche vectorielle top-k filtrée par actif et régime ; à défaut, repli sur
    l'extrait fixe des notes les plus récentes.
    """
    vector_context = _vector_context(
        asset=asset, regime=regime, top_k=top_k, max_chars=max_chars
    )
    if vector_context:
        return vector_context
    return _fixed_extract(max_chars=max_chars)


def media_similarity_floor() -> float:
    """Seuil de similarité effectif pour les médias : celui que la base a mesuré.

    `knowledge_index.calibration` mesure la similarité entre des questions **hors
    sujet** et les documents de la base : c'est le bruit propre à *ce* corpus
    (taille, langue, homogénéité, modèle d'embeddings). Le seuil est ce bruit, porté
    à un percentile élevé, plus une marge — donc une correspondance doit dépasser
    presque toutes les paires sans rapport de la base, pas un chiffre choisi à la
    main. La mesure est mémorisée quelques minutes (elle coûte des embeddings).

    Repli sur `MIN_MEDIA_SIMILARITY` quand la mesure est impossible : le seuil est
    une amélioration, son absence ne doit pas empêcher l'analyse de tourner. Cette
    fonction ne lève jamais.
    """
    try:
        measured = knowledge_index.calibration()
    except Exception as exc:  # pragma: no cover - `calibration` ne lève pas
        print(f"   [medias] seuil non calibre : {type(exc).__name__}: {exc}")
        return MIN_MEDIA_SIMILARITY
    floor = measured.get("floor")
    if measured.get("ok") and floor is not None:
        return float(floor)
    return MIN_MEDIA_SIMILARITY


def get_media_context(
    asset: Optional[str] = None,
    regime: Optional[str] = None,
    top_k: int = DEFAULT_MEDIA_TOP_K,
    max_chars: int = MAX_MEDIA_CONTEXT_CHARS,
    min_similarity: Optional[float] = None,
) -> str:
    """Extraits de **médias indexés** pertinents pour un actif.

    Il n'existe volontairement **aucun repli** ici, contrairement aux notes : sans
    actif connu, sans vecteur, ou sans correspondance assez proche, on retourne
    une chaîne vide. Un extrait de média hors sujet est pire que pas d'extrait.

    `min_similarity=None` (le défaut) laisse la base fixer son propre seuil
    (`media_similarity_floor`) ; une valeur explicite le remplace, pour un appelant
    qui sait mieux que la mesure (un test, un réglage manuel).

    Ce seuil ne gouverne que les extraits que la **similarité** a fait remonter : un
    média dont l'actif est étiqueté en base (commande `/tag`) est retenu même sous
    le seuil, et classé en tête (voir `knowledge_index.select_media_candidates`).
    L'étiquette dit l'appartenance — un fait déclaré —, le seuil mesure autre chose :
    le bruit du corpus. Un seuil explicite n'annule pas davantage une étiquette, mais
    celle-ci reste bornée par `knowledge_index.TAGGED_HARD_FLOOR`.

    Jamais d'exception : enrichir le prompt est une amélioration, son échec ne
    doit pas empêcher l'analyse de se poursuivre avec les seules notes.
    """
    target = (asset or "").strip()
    if not target:
        return ""

    floor = media_similarity_floor() if min_similarity is None else float(min_similarity)
    try:
        hits = knowledge_index.search_media_for_asset(
            target,
            regime=regime,
            top_k=top_k,
            min_similarity=floor,
        )
    except Exception as exc:
        print(f"   [medias] recherche indisponible : {type(exc).__name__}: {exc}")
        return ""
    return _format_media_hits(hits or [], max_chars=max_chars)


# --------------------------------------------------------------------------- #
# Commande `/search` : options
# --------------------------------------------------------------------------- #


def parse_search_args(args: Sequence[str]) -> Dict[str, Any]:
    """Interprète les arguments de `/search` : requête libre + options.

    La requête est tout ce qui n'est pas une option, dans l'ordre reçu :
    `/search cassure des 100k --source medias -n 8` cherche « cassure des 100k ».
    `--` met fin aux options, pour une requête qui commence par un tiret.

    Retourne le **même contrat structuré** que `search_knowledge` (`ok`/`reason`)
    plutôt qu'une exception : l'appelant affiche le résultat tel quel.

    * ``ok=True`` — `query`, `asset`, `source`, `regime`, `top_k` renseignés ;
    * ``reason="help"`` — aide demandée (`--help`, `-h`, `help`, `aide`) ;
    * ``reason="empty_query"`` — aucune requête (invite à lire l'aide) ;
    * ``reason="bad_option"`` — option inconnue, sans valeur, ou hors bornes.
    """
    parsed: Dict[str, Any] = {
        "ok": True,
        "reason": None,
        "error": None,
        "query": "",
        "asset": None,
        "source": None,
        "regime": None,
        "top_k": DEFAULT_SEARCH_TOP_K,
    }

    words: List[str] = []
    literal = False  # après `--`, tout est requête
    index = 0
    tokens = [str(token) for token in (args or [])]
    while index < len(tokens):
        token = tokens[index]
        index += 1

        if literal:
            words.append(token)
            continue
        if token == "--":
            literal = True
            continue
        if token.lower() in SEARCH_HELP_TOKENS:
            return _search_rejected("help")
        if not token.startswith("-"):
            words.append(token)
            continue

        name, separator, inline = token.partition("=")
        option = SEARCH_OPTION_ALIASES.get(name.lstrip("-"))
        if option is None:
            return _search_rejected("bad_option", f"option inconnue : {name}")

        value = inline.strip()
        if not separator:
            # Une valeur ne commence pas par un tiret : sinon `--asset --source x`
            # passerait `"--source"` comme actif, et le filtre ne trouverait rien
            # sans que personne ne comprenne pourquoi.
            following = tokens[index].strip() if index < len(tokens) else ""
            if following and not following.startswith("-"):
                value = following
                index += 1
        if not value:
            return _search_rejected(
                "bad_option", f"option {name} sans valeur"
            )

        problem = _apply_search_option(parsed, option, value)
        if problem:
            return _search_rejected("bad_option", problem)

    query = " ".join(word for word in words if word).strip()
    parsed["query"] = query
    if not query:
        return _search_rejected("empty_query")
    return parsed


def _search_rejected(reason: str, error: Optional[str] = None) -> Dict[str, Any]:
    return {"ok": False, "reason": reason, "error": error, "query": "", "hits": []}


def _apply_search_option(parsed: Dict[str, Any], option: str, value: str) -> Optional[str]:
    """Renseigne une option validée. Retourne le message d'erreur, ou `None`."""
    if option == "asset":
        if any(separator in value for separator in (",", ";", " ")):
            # `filter_asset` est une égalité stricte côté SQL : une liste ne
            # remonterait rien, et le silence serait trompeur.
            return (
                f"un seul actif à la fois (reçu « {value} ») : le filtre est une "
                "égalité stricte, donc `--asset BTC-USD,ETH-USD` ne trouverait rien"
            )
        parsed["asset"] = value.upper().strip()
        return None

    if option == "source":
        wanted = SEARCH_SOURCE_ALIASES.get(value.strip().lower(), "inconnu")
        if wanted == "inconnu":
            allowed = "notes | medias | tout"
            return f"source inconnue : {value} (attendu : {allowed})"
        parsed["source"] = wanted
        return None

    if option == "regime":
        parsed["regime"] = value.strip()
        return None

    if option == "limit":
        try:
            wanted = int(value)
        except ValueError:
            return f"nombre de résultats invalide : {value}"
        if not 1 <= wanted <= MAX_SEARCH_TOP_K:
            return (
                f"nombre de résultats hors bornes : {wanted} "
                f"(entre 1 et {MAX_SEARCH_TOP_K})"
            )
        parsed["top_k"] = wanted
        return None

    return f"option inconnue : {option}"  # pragma: no cover - alias déjà filtrés


def format_search_help() -> str:
    """Aide de `/search` : sans elle, les options n'existent qu'en lisant le code."""
    return (
        "🔎 /search — recherche sémantique dans tes notes et tes médias\n\n"
        "Usage : /search <texte à chercher> [options]\n\n"
        "Options :\n"
        "• --asset <actif> — ne garder que cet actif (ex: BTC-USD)\n"
        "• --source <où> — notes | medias | tout (défaut : tout)\n"
        "• --regime <nom> — ne remonter que ce régime de marché\n"
        f"• -n, --limit <N> — passages par page, 1 à {MAX_SEARCH_TOP_K} "
        f"(défaut : {DEFAULT_SEARCH_TOP_K})\n"
        "• --help — affiche cette aide\n\n"
        "Exemples :\n"
        "• /search niveaux de support bitcoin\n"
        "• /search cassure des 100k --source medias -n 8\n"
        "• /search gestion du risque --asset BTC-USD\n"
        "• /search -- -5% de perte maximum (-- : fin des options)\n\n"
        f"Suite des résultats : `/search` interroge {SEARCH_POOL_SIZE} passages au "
        "maximum et en affiche une page ; `/search_more` déroule le lot, sans "
        "relancer la recherche (même classement, aucun appel réseau).\n\n"
        "La recherche est **sémantique** : décris l'idée plutôt qu'un mot exact. "
        "Un actif ou un régime non étiqueté sur une note reste candidat, mais "
        "les passages étiquetés remontent en premier."
    )


def _filters_label(options: Optional[Dict[str, Any]]) -> str:
    """Rappel des filtres actifs (« [médias · BTC-USD · max 8] »).

    Sans ce rappel, un résultat unique ou vide paraît arbitraire : l'utilisateur
    ne sait pas qu'il a laissé un filtre actif.
    """
    if not options:
        return ""
    parts: List[str] = []
    if options.get("source") == knowledge_index.SOURCE_MEDIA:
        parts.append("médias")
    elif options.get("source"):
        parts.append("notes")
    if options.get("asset"):
        parts.append(str(options["asset"]))
    if options.get("regime"):
        parts.append(f"régime {options['regime']}")
    if options.get("top_k") and options["top_k"] != DEFAULT_SEARCH_TOP_K:
        # « 4 par page » et non « max 4 » : depuis la pagination, ce nombre est la
        # taille de page, pas un plafond de résultats.
        parts.append(f"{options['top_k']} par page")
    return f" [{' · '.join(parts)}]" if parts else ""


def _widening_hint(options: Optional[Dict[str, Any]]) -> str:
    """Piste d'élargissement, nommant **le** filtre actif (et non un autre).

    Un « aucun résultat » ne veut pas dire la même chose sans filtre (formule à
    reformuler) et avec un filtre (requête juste, périmètre trop étroit) : dans
    le second cas, indiquer comment l'élargir évite de croire que le passage
    n'existe pas.
    """
    if not options:
        return " Reformule autrement, ou vérifie que la base est indexée."
    escapes: List[str] = []
    if options.get("source"):
        escapes.append("`--source tout`")
    if options.get("asset"):
        escapes.append("sans `--asset`")
    if options.get("regime"):
        escapes.append("sans `--regime`")
    if not escapes:
        return " Reformule autrement, ou vérifie que la base est indexée."
    return f" Essaie {' ou '.join(escapes)}."


def search_knowledge(
    query: str,
    *,
    top_k: int = DEFAULT_SEARCH_TOP_K,
    asset: Optional[str] = None,
    regime: Optional[str] = None,
    source: Optional[str] = None,
) -> Dict[str, Any]:
    """Recherche **sémantique** dans `knowledge_chunks` (commande `/search`).

    Contrairement à un `LIKE`, la requête est vectorisée (embeddings Gemini) :
    « support du bitcoin » retrouve un passage qui parle de « zone d'achat sous
    64k » sans partager un seul mot. Chaque passage remonte avec son **score de
    similarité** cosinus.

    Retourne un résultat **structuré** plutôt qu'une simple liste, pour que
    l'appelant distingue trois cas que `search_chunks` confond en `[]` : requête
    vide, recherche vectorielle indisponible, et « rien trouvé ». Jamais
    d'exception.
    """
    text = (query or "").strip()
    if not text:
        return {"ok": False, "reason": "empty_query", "query": "", "hits": []}

    wanted = max(1, min(int(top_k), MAX_SEARCH_TOP_K))
    try:
        hits = knowledge_index.search_chunks(
            text, asset=asset, regime=regime, source=source, top_k=wanted
        )
    except Exception as exc:
        return {
            "ok": False,
            "reason": "search_failed",
            "error": str(exc),
            "query": text,
            "hits": [],
        }

    # Un résultat vide n'a pas le même sens selon que la recherche vectorielle
    # était possible ou non : on le dit à l'utilisateur au lieu d'un « rien
    # trouvé » trompeur.
    if not hits and not knowledge_index.embeddings_available():
        return {"ok": False, "reason": "embeddings_unavailable", "query": text, "hits": []}

    return {"ok": True, "reason": None, "query": text, "hits": hits}


def format_search_results(
    result: Dict[str, Any],
    options: Optional[Dict[str, Any]] = None,
    start: Optional[int] = None,
    total: Optional[int] = None,
) -> str:
    """Message Telegram d'une recherche (`/search` et `/search_more`).

    `options` est l'objet rendu par `parse_search_args` : il sert à rappeler les
    filtres actifs dans l'en-tête. `start`/`total` décrivent la **page** dans le
    lot interrogé (« passages 6 à 10 sur 17 ») et déclenchent le rappel de
    `/search_more` : la numérotation continue d'une page à l'autre, sinon deux
    pages afficheraient deux fois « 1. ». Les cas `ok=False` (aucune requête, aide
    demandée, option fautive) affichent l'aide : c'est le seul endroit où
    l'utilisateur peut découvrir les options depuis le chat.
    """
    if not result.get("ok"):
        reason = result.get("reason")
        if reason in {"empty_query", "help"}:
            return format_search_help()
        if reason == "bad_option":
            return f"⚠️ {result.get('error')}\n\n{format_search_help()}"
        if reason == "embeddings_unavailable":
            return (
                "⚠️ Recherche vectorielle indisponible : `GEMINI_API_KEY` absente "
                "(ou `httpx` manquant). Les embeddings Gemini sont nécessaires pour "
                "la recherche sémantique."
            )
        if reason == "search_failed":
            return f"❌ Recherche impossible : {result.get('error')}"
        return f"❌ Recherche impossible ({reason})."

    hits = result.get("hits") or []
    query = result.get("query") or ""
    filters = _filters_label(options)
    if not hits:
        return f"🔎 Aucun passage trouvé pour « {query} »{filters}.{_widening_hint(options)}"

    first = int(start or 1)
    blocks = [_page_title(len(hits), first, total, query, filters)]
    for rank, hit in enumerate(hits, first):
        label = hit_label(hit)
        score = _similarity(hit)
        blocks.append(f"\n{rank}. {label} — similarité {score:.3f}\n{_preview(hit)}")
    return "\n".join(blocks) + _more_hint(first, len(hits), total)


def _page_title(
    count: int, start: int, total: Optional[int], query: str, filters: str
) -> str:
    """Titre du message : « passages 6 à 10 sur 17 » quand il reste quelque chose.

    Sans `total` (appel direct, ou page unique), on garde la forme simple :
    annoncer « 1 à 5 sur 5 » n'apprendrait rien.
    """
    if total is None or start == 1 and total == count:
        return f"🔎 {count} passage(s) pour « {query} »{filters} :"
    return (
        f"🔎 passages {start} à {start + count - 1} sur {total} "
        f"pour « {query} »{filters} :"
    )


def _more_hint(start: int, count: int, total: Optional[int]) -> str:
    """Lien vers `/search_more` quand le lot contient encore des passages.

    Sans ce rappel, la pagination n'existe que pour qui connaît la commande : la
    suite du lot est déjà en mémoire, il serait absurde de ne pas la proposer.
    """
    if total is None:
        return ""
    remaining = total - (start + count - 1)
    if remaining <= 0:
        return ""
    return f"\n\n↪️ {remaining} passage(s) de plus dans ce lot — /search_more"


def format_search_more(result: Dict[str, Any]) -> str:
    """Message de `/search_more` : la page suivante du **dernier** lot interrogé.

    Les deux échecs ont des remèdes différents, donc deux messages : « aucune
    recherche récente » (relancer `/search`) et « lot épuisé » (changer de mots ou
    de filtres — le lot est borné à `SEARCH_POOL_SIZE`).
    """
    if result.get("ok"):
        return format_search_results(
            result,
            options=result.get("options"),
            start=result.get("start"),
            total=result.get("total"),
        )

    reason = result.get("reason")
    if reason == "no_history":
        return (
            "↪️ Aucune recherche récente à continuer. Lance d'abord "
            "`/search <texte>` (`/search --help` pour les options)."
        )
    if reason == "exhausted":
        query = result.get("query") or ""
        return (
            f"↪️ Tous les passages du lot ont été affichés pour « {query} » "
            f"({result.get('shown', 0)} sur {result.get('total', 0)}).\n\n"
            f"Le lot interrogé est borné à {SEARCH_POOL_SIZE} passages : relance "
            "`/search` avec d'autres mots, ou élargis les filtres "
            "(`--source tout`, sans `--asset`)."
        )
    return f"❌ Suite impossible ({reason})."


def format_rag_selection(result: Dict[str, Any]) -> str:
    """Message de la boucle RAG : la proposition à valider, ou pourquoi elle échoue.

    La proposition **montre les extraits qui seront lus** (et non un simple
    décompte) : valider une injection de contexte qu'on ne voit pas serait une
    validation de façade. Les échecs nomment le remède — relancer `/search`, ou
    corriger le rang en donnant la plage réelle du lot.
    """
    if not result.get("ok"):
        reason = result.get("reason")
        asset = result.get("asset") or "cet actif"
        if reason == "no_history":
            return (
                "↪️ Aucune recherche à utiliser. Lance d'abord `/search <texte>`, "
                f"puis `/use {asset}` : les passages affichés par la recherche sont "
                "ceux qui peuvent être injectés."
            )
        if reason in {"bad_rank", "out_of_range"}:
            available = result.get("available") or 0
            value = result.get("value")
            return (
                f"⚠️ Rang « {value} » hors du lot : la dernière recherche a "
                f"{available} passage(s), donc les rangs valides vont de 1 à "
                f"{available}.\n\n`/search_more` affiche la suite ; `/use {asset} "
                f"1,2` sélectionne les deux premiers."
            )
        if reason == "empty_pool":
            return (
                "↪️ La dernière recherche n'a remonté aucun passage : rien à "
                "injecter. Élargis la requête ou les filtres (`--source tout`)."
            )
        if reason == "empty_selection":
            return "⚠️ Aucun passage dans cette sélection."
        if reason == "bad_asset":
            return "⚠️ Indique un actif : `/use BTC-USD 1,2`."
        return f"❌ Sélection impossible ({reason})."

    excerpts = result.get("excerpts") or []
    asset = result.get("asset")
    ranks = ", ".join(str(rank) for rank in result.get("ranks") or [])
    query = result.get("query") or ""
    blocks = [
        f"🎯 Proposition : analyser {asset} avec {len(excerpts)} passage(s) de "
        f"ta base ({result.get('chars', 0)} caractères injectés dans le prompt)."
    ]
    if query:
        blocks.append(f"Sélection issue de la recherche « {query} » — rang(s) {ranks}.")
    requested = result.get("requested")
    if requested and requested > len(excerpts):
        # Le plafond a rogné la sélection : le dire, sinon l'utilisateur croit
        # avoir validé un contexte plus large que celui qui sera lu. Le budget
        # vient de la proposition : `MAX_CONTEXT_CHARS` désigne ici autre chose
        # (le plafond du prompt de notes) et les confondre afficherait un chiffre faux.
        budget = result.get("budget")
        limite = f"plafonné à {budget} caractères" if budget else "plafonné"
        blocks.append(
            f"✂️ {len(excerpts)} extrait(s) sur {requested} demandés : le bloc est "
            f"{limite}. Affine la sélection (`/use {asset} 1,3`)."
        )
    blocks.extend(f"\n{excerpt}" for excerpt in excerpts)
    blocks.append(
        "\nCes extraits pèseront sur la composante sentiment/géo de l'analyse "
        "(la technique reste calculée sur les prix), immédiatement — sans attendre "
        "le cycle automatique. L'analyse n'est pas mise en cache."
    )
    return "\n".join(blocks)


def _similarity(hit: Dict[str, Any]) -> float:
    """Score de similarité d'un résultat, robuste à une valeur non numérique."""
    try:
        return float(hit.get("similarity"))
    except (TypeError, ValueError):
        return 0.0


def hit_label(hit: Dict[str, Any]) -> str:
    """Étiquette d'un passage : provenance + actif/régime éventuels.

    Publique parce que deux affichages s'en servent : les résultats de `/search`
    et le bloc d'extraits injecté dans une analyse (`core/rag_loop.py`). Deux
    étiquettes différentes pour le même passage obligeraient l'utilisateur à se
    demander si c'est bien le même extrait.
    """
    is_media = hit.get("source") == knowledge_index.SOURCE_MEDIA
    parts = ["média" if is_media else "note"]
    if hit.get("asset"):
        parts.append(str(hit["asset"]))
    if hit.get("regime"):
        parts.append(str(hit["regime"]))
    return f"[{' · '.join(parts)}]"


def _preview(hit: Dict[str, Any], limit: int = SEARCH_PREVIEW_CHARS) -> str:
    """Aperçu d'un passage : espaces normalisés, tronqué avec `…`."""
    text = (hit.get("content") or hit.get("matched_chunk") or "").strip()
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[:limit].rstrip() + "…"


def reindex_notes() -> int:
    """Réindexe toutes les notes dans `knowledge_chunks`. Retourne le nombre traité.

    Utile après avoir activé les embeddings sur une base existante, ou après une
    mise à jour du modèle d'embedding.
    """
    res = supabase.table(TABLE).select("source,title,content").execute()
    rows = res.data or []
    processed = 0
    for row in rows:
        source = row.get("source")
        if not source:
            continue
        _index_note(source, row.get("title") or "Note", row.get("content") or "")
        processed += 1
    return processed


# --------------------------------------------------------------------------- #
# Recherche vectorielle
# --------------------------------------------------------------------------- #

def _retrieval_query(asset: Optional[str], regime: Optional[str]) -> str:
    """Texte de requête : plus il décrit l'actif/le régime, meilleur est le tri."""
    parts: List[str] = []
    if asset:
        parts.append(f"actif {asset}")
    if regime:
        parts.append(f"régime de marché {regime}")
    parts.append("règles de trading, gestion du risque et contexte de marché")
    return " ".join(parts)


def _vector_context(
    *,
    asset: Optional[str],
    regime: Optional[str],
    top_k: int,
    max_chars: int,
) -> str:
    try:
        hits = knowledge_index.search_chunks(
            _retrieval_query(asset, regime),
            asset=asset,
            regime=regime,
            top_k=top_k,
        )
    except Exception as exc:
        print(f"   [vector] recherche indisponible : {type(exc).__name__}: {exc}")
        return ""
    return _format_hits(hits or [], max_chars=max_chars)


def _format_hits(hits: List[Dict[str, Any]], *, max_chars: int) -> str:
    blocks: List[str] = []
    total = 0
    for hit in hits:
        content = (hit.get("content") or "").strip()
        if not content:
            continue
        tag = hit.get("asset") or "général"
        block = f"### [{tag}] {content}\n"
        if total + len(block) > max_chars:
            remaining = max_chars - total
            if remaining > 100:
                blocks.append(block[:remaining] + "\n[...tronqué]")
            break
        blocks.append(block)
        total += len(block)
    return "\n".join(blocks)


# --------------------------------------------------------------------------- #
# Repli : extrait fixe (comportement historique)
# --------------------------------------------------------------------------- #

#: Libellé du contenu selon le type de média Telegram. Déduit du `media_type`,
#: car la méthode d'extraction n'est pas persistée sur la ligne média. Le but est
#: que le modèle sache ce qu'il lit : une lecture de graphique n'a pas le statut
#: d'une transcription vocale.
_MEDIA_KIND_LABELS = {
    "photo": "lecture de graphique",
    "video": "transcription vidéo",
    "voice": "transcription vocale",
    "audio": "transcription audio",
    "document": "document",
}


def _media_label(hit: Dict[str, Any]) -> str:
    """Étiquette lisible d'un média (« MÉDIA · lecture de graphique · btc.png »)."""
    kind = _MEDIA_KIND_LABELS.get(str(hit.get("media_type") or "").lower(), "média")
    name = hit.get("file_name") or hit.get("media_id") or "sans nom"
    return f"MÉDIA · {kind} · {name}"


def _format_media_hits(hits: List[Dict[str, Any]], *, max_chars: int) -> str:
    """Bloc texte des extraits de médias, borné à `max_chars`.

    Même règle que `_format_hits` : un bloc entamé au-delà du budget est tronqué
    et marqué, les suivants sont abandonnés — on ne dépasse jamais le plafond.
    """
    blocks: List[str] = []
    total = 0
    for hit in hits:
        content = (hit.get("matched_chunk") or hit.get("content") or "").strip()
        if not content:
            continue
        block = f"### [{_media_label(hit)}] {content}\n"
        if total + len(block) > max_chars:
            remaining = max_chars - total
            if remaining > 100:
                blocks.append(block[:remaining] + "\n[...tronqué]")
            break
        blocks.append(block)
        total += len(block)
    return "\n".join(blocks)


def _fixed_extract(max_chars: int = MAX_CONTEXT_CHARS) -> str:
    """Notes les plus récentes, bornées à `max_chars`.

    Utilisé quand la recherche vectorielle n'est pas disponible. Si le vault
    dépasse la limite, seules les notes les plus récemment mises à jour sont
    incluses (les autres sont tronquées).
    """
    res = (
        supabase.table(TABLE)
        .select("title,content,updated_at")
        .order("updated_at", desc=True)
        .execute()
    )
    rows = res.data or []
    if not rows:
        return ""

    blocks = []
    total = 0
    for row in rows:
        title = row.get("title") or "Note"
        content = (row.get("content") or "").strip()
        if not content:
            continue
        block = f"### {title}\n{content}\n"
        if total + len(block) > max_chars:
            remaining = max_chars - total
            if remaining > 100:
                blocks.append(block[:remaining] + "\n[...tronqué]")
            break
        blocks.append(block)
        total += len(block)

    return "\n".join(blocks)


def _index_note(source: str, title: str, content: str) -> None:
    """Indexe une note comme morceaux vectorisables (jamais bloquant)."""
    try:
        knowledge_index.replace_chunks(
            None,
            f"{title}\n{content}".strip(),
            note_source=source,
            source=knowledge_index.SOURCE_NOTE,
        )
    except Exception as exc:
        print(f"   [vector] indexation de la note impossible ({source}) : {type(exc).__name__}: {exc}")
