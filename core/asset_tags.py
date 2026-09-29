"""Étiquetage d'actif : associer un actif canonique à un texte (légende, transcription).

Pourquoi ça compte : `knowledge_chunks.asset` est le filtre **préférentiel** de
`match_knowledge_chunks` (migration 009). Un média non étiqueté (`asset` NULL)
reste candidat pour n'importe quel actif — c'est le joker — et ne bénéficie jamais
du bonus de classement. Étiqueter, c'est dire « ce graphique parle de BTC-USD »,
ce qui sert deux choses à la fois : le média remonte en tête quand on cherche
BTC-USD, et il cesse de polluer l'analyse d'un autre actif — un morceau étiqueté
`ETH-USD` est **exclu** d'une recherche filtrée sur `BTC-USD`, alors qu'un morceau
non étiqueté y reste candidat (voir la sémantique `null` = joker de la migration).

Trois règles de conception :

* **Ne jamais deviner.** Un actif faux est pire qu'une absence : NULL est un
  joker, un actif faux *exclut* le média des recherches de l'actif réel. On ne
  retient donc qu'une correspondance nette, et on préfère `None` au doute ;
* **Un seul actif par média.** `asset` est une égalité en base : « BTC-USD ou
  ETH-USD » n'a aucun sens, et un média qui parle de deux actifs se corrige à la
  main (`/tag`) au lieu d'être étiqueté au hasard ;
* **Les tableaux sont fermés.** Une liste ouverte devinerait des symboles dans
  n'importe quel sigle ; ici, ce qui n'est pas listé n'est pas étiqueté.
"""
from __future__ import annotations

import re
from typing import Dict, Optional, Tuple

#: Bases crypto reconnues, avec la convention `<BASE>-USD` du reste du dépôt
#: (`database.preferences.DEFAULT_WATCHLIST`, `utils.market_data`).
CRYPTO_BASES = (
    "BTC",
    "ETH",
    "SOL",
    "XRP",
    "ADA",
    "DOGE",
    "BNB",
    "AVAX",
    "LINK",
    "DOT",
    "MATIC",
    "LTC",
)

#: Alias reconnus **quelle que soit la casse** : noms longs et marques, assez
#: spécifiques pour qu'aucun mot ordinaire d'une phrase ne les imite. Les
#: tickers courts en sont volontairement absents (voir `TICKERS`).
UNAMBIGUOUS_ALIASES: Dict[str, str] = {
    "bitcoin": "BTC-USD",
    "ethereum": "ETH-USD",
    "ether": "ETH-USD",
    "solana": "SOL-USD",
    "ripple": "XRP-USD",
    "cardano": "ADA-USD",
    "dogecoin": "DOGE-USD",
    "avalanche": "AVAX-USD",
    "chainlink": "LINK-USD",
    "polkadot": "DOT-USD",
    "polygon": "MATIC-USD",
    "litecoin": "LTC-USD",
    "apple": "AAPL",
    "microsoft": "MSFT",
    "google": "GOOGL",
    "alphabet": "GOOGL",
    "amazon": "AMZN",
    "tesla": "TSLA",
    "nvidia": "NVDA",
    "netflix": "NFLX",
    "facebook": "META",
    "jpmorgan": "JPM",
}

#: Tickers courts **dont le sigle n'est pas un mot** : reconnus en toutes
#: lettres. « btc », « eth », « xrp », « doge », « aapl », « nvda » n'existent dans
#: aucune phrase française ou anglaise — et c'est la casse de la vie réelle : une
#: légende Telegram s'écrit « btc support 64k » bien plus souvent que « BTC support
#: 64k ». Les exiger en majuscules revenait à ne **rien** détecter dans la légende,
#: donc à laisser `asset` NULL — et `match_knowledge_chunks` n'avait alors aucun
#: signal à classer : tous les médias restaient des jokers.
UNAMBIGUOUS_TICKERS: Dict[str, str] = (
    {
        # Bases crypto (`CRYPTO_BASES`) : celles dont le sigle est un mot vont
        # dans l'autre tableau, plus bas.
        base: f"{base}-USD"
        for base in ("BTC", "ETH", "XRP", "DOGE", "BNB", "AVAX", "MATIC", "LTC")
    }
    | {
        "AAPL": "AAPL",
        "MSFT": "MSFT",
        "GOOGL": "GOOGL",
        "GOOG": "GOOGL",
        "AMZN": "AMZN",
        "TSLA": "TSLA",
        "NVDA": "NVDA",
        "NFLX": "NFLX",
        "AMD": "AMD",
        "INTC": "INTC",
        "JPM": "JPM",
        "QQQ": "QQQ",
        "XAUUSD": "XAUUSD",
        "EURUSD": "EURUSD",
    }
)

#: Tickers courts qui **sont des mots** ordinaires — `sol` (le sol), `dot` (le
#: point), `link` (le lien), `ada` (un prénom), `meta` (méta), `spy` (l'espion) :
#: reconnus **en majuscules seulement**. Le coût est mince — c'est ainsi qu'on
#: écrit un ticker — et il protège du pire : une étiquette fausse *exclut* le média
#: des recherches de l'actif réel, alors qu'un média non étiqueté reste candidat
#: partout.
AMBIGUOUS_TICKERS: Dict[str, str] = (
    {
        base: f"{base}-USD"
        for base in ("SOL", "ADA", "DOT", "LINK")
    }
    | {
        "META": "META",
        "SPY": "SPY",
    }
)

#: Les deux tableaux réunis. `TICKERS` reste la référence complète du vocabulaire,
#: mais la **casse** se lit dans les deux tableaux : c'est elle qui distingue un
#: sigle d'un mot.
TICKERS: Dict[str, str] = UNAMBIGUOUS_TICKERS | AMBIGUOUS_TICKERS

#: Marqueur explicite : `$BTC`, `#btc-usd`. Rien n'est plus net dans une légende
#: (« $BTC cassure des 100k »), donc ce signal passe avant tout le reste.
_MARKER = re.compile(r"[$#]([A-Za-z0-9]{1,10}(?:[-/][A-Za-z0-9]{1,6})?)")

#: Notation de paire complète : `BTC-USD`, `BTC/USD`, `BTCUSDT`, `BTCUSD`.
_PAIR = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z]{2,6})(?:[-/]?(?:USD|USDT))(?![A-Za-z])", re.IGNORECASE
)

#: Alias en toutes lettres, cherchés mot à mot (`\b`) pour ne pas étiqueter
#: « ethereum » dans « ethereumclassic ».
_ALIAS_WORDS = re.compile(
    r"\b(" + "|".join(sorted((re.escape(k) for k in UNAMBIGUOUS_ALIASES), key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

def _words(table: Dict[str, str]) -> str:
    """Alternatives d'un tableau de tickers, la plus longue d'abord.

    L'ordre n'est pas cosmétique : sans lui, `GOOG` l'emporterait sur `GOOGL` —
    l'alternative la plus courte est essayée en premier, et le `\b` final la
    rejetterait avant qu'on ait essayé la plus longue.
    """
    return "|".join(sorted((re.escape(k) for k in table), key=len, reverse=True))


#: Tickers dont le sigle n'est pas un mot : **toutes les casses**.
_ANY_CASE_TICKERS = re.compile(r"\b(" + _words(UNAMBIGUOUS_TICKERS) + r")\b", re.IGNORECASE)

#: Tickers qui sont des mots : **majuscules seulement** (pas de `IGNORECASE`).
_UPPER_TICKERS = re.compile(r"\b(" + _words(AMBIGUOUS_TICKERS) + r")\b")

#: Divises : une devise de cotation n'est pas un actif. `USDT` seul dans une
#: légende (« paiement en USDT ») n'a rien à étiqueter.
QUOTE_CURRENCIES = ("USD", "USDT", "USDC")

#: Longueur maximale d'un actif accepté (une saisie fantaisiste n'a aucune raison
#: d'entrer en base : elle ne correspondrait à aucun filtre un jour).
MAX_ASSET_CHARS = 20

#: Forme canonique : majuscules, chiffres, un tiret optionnel (BTC-USD, XAUUSD).
_CANONICAL = re.compile(r"^[A-Z0-9]{1,12}(?:-[A-Z0-9]{1,6})?$")


def normalize_asset(raw: Optional[str]) -> Optional[str]:
    """Forme canonique d'un actif saisi à la main, ou `None` si ce n'en est pas un.

    Accepte les écritures courantes d'un même actif — `btc`, `BTC`, `BTC/USD`,
    `BTCUSDT`, `btc-usd` — et les ramène à la convention du dépôt (`BTC-USD`),
    parce que c'est cette chaîne qui sera comparée à l'égalité dans le filtre SQL :
    `btc` et `BTC-USD` ne se rencontrent jamais.

    Les actions restent telles quelles (`aapl` → `AAPL`) : elles n'ont pas de
    paire de cotation, et deviner un suffixe fabriquerait un actif qui n'existe
    dans aucun flux de prix. Un symbole ne contient pas d'espace : au-delà des
    espaces autour, une entrée qui en contient est refusée plutôt que recollée.
    `None` plutôt qu'une exception : l'appelant décide de la façon de le dire.
    """
    text = str(raw or "").strip().lstrip("$#")
    if not text or len(text) > MAX_ASSET_CHARS:
        return None
    if any(ch.isspace() for ch in text):
        # « pas un actif » ou « BTC USD » : coller les morceaux fabriquerait un
        # symbole (`PASUNACTIF`, `BTCUSD`) que l'utilisateur n'a pas écrit.
        return None

    text = text.upper().replace("/", "-")
    if text in TICKERS:
        return TICKERS[text]
    if text.lower() in UNAMBIGUOUS_ALIASES:
        return UNAMBIGUOUS_ALIASES[text.lower()]
    if text in QUOTE_CURRENCIES:
        return None

    for suffix in ("USDT", "USD"):
        if text.endswith(suffix) and len(text) > len(suffix):
            base = text[: -len(suffix)].rstrip("-")
            if base in CRYPTO_BASES and not text.endswith(f"{base}-{suffix}"):
                return f"{base}-USD"
    if text.endswith("-USD"):
        return text if _CANONICAL.match(text) else None
    if text in CRYPTO_BASES:
        # Une base crypto seule est une paire implicite (`BTC` → `BTC-USD`).
        return f"{text}-USD"
    return text if _CANONICAL.match(text) else None


def _from_marker(text: str) -> Optional[str]:
    for match in _MARKER.finditer(text):
        asset = normalize_asset(match.group(1))
        if asset:
            return asset
    return None


def _from_pair(text: str) -> Optional[str]:
    """Paire écrite en clair (`BTC-USD`, `btcusdt`), base **connue** seulement.

    Accepter une base inventée ferait entrer dans la base un actif que le projet
    ne suit nulle part — et un actif faux exclut le média des recherches du vrai.
    """
    for match in _PAIR.finditer(text):
        base = match.group(1).upper()
        if base in CRYPTO_BASES:
            return f"{base}-USD"
    return None


def _from_ticker(text: str) -> Optional[str]:
    """Ticker nu, le **plus tôt** dans le texte, entre les deux tableaux.

    « SOL puis btc » parle de SOL et « btc puis SOL » parle de BTC : dans un texte,
    c'est la **position** qui tranche, pas la table — les deux tableaux ont la même
    forme (« un ticker seul »), et l'un n'est pas un signal plus net que l'autre.
    Un ticker ambigu ne gagne donc que s'il est écrit en majuscules et arrive
    avant, ou faute d'autre candidat.
    """
    best: Optional[Tuple[int, str]] = None
    for pattern, table in (
        (_ANY_CASE_TICKERS, UNAMBIGUOUS_TICKERS),
        (_UPPER_TICKERS, AMBIGUOUS_TICKERS),
    ):
        match = pattern.search(text)
        if match is None:
            continue
        if best is None or match.start() < best[0]:
            best = (match.start(), table[match.group(1).upper()])
    return best[1] if best else None


def detect_asset(*texts: Optional[str]) -> Optional[str]:
    """Premier actif nettement reconnaissable dans les textes, dans l'ordre donné.

    L'ordre des textes porte la priorité : on passe la **légende** avant le texte
    extrait, parce qu'elle nomme l'actif explicitement là où une description
    visuelle peut le déduire de travers.

    Dans un même texte, la priorité suit la netteté du signal : marqueur explicite
    (`$BTC`), puis notation de paire (`BTC-USD`, `BTCUSDT`), puis nom en toutes
    lettres (`bitcoin`), puis ticker nu — **en toutes lettres** quand son sigle
    n'est pas un mot (`btc`, `eth`, `aapl`), **en majuscules** quand il l'est
    (`SOL`, `DOT`, `META`). Le premier trouvé gagne — on n'étiquette jamais deux
    actifs, `asset` étant une égalité en base.
    """
    for text in texts:
        raw = str(text or "")
        if not raw.strip():
            continue
        asset = _from_marker(raw)
        if asset:
            return asset
        asset = _from_pair(raw)
        if asset:
            return asset
        aliases = _ALIAS_WORDS.findall(raw)
        if aliases:
            return UNAMBIGUOUS_ALIASES[aliases[0].lower()]
        asset = _from_ticker(raw)
        if asset:
            return asset
    return None


__all__ = [
    "AMBIGUOUS_TICKERS",
    "CRYPTO_BASES",
    "MAX_ASSET_CHARS",
    "QUOTE_CURRENCIES",
    "TICKERS",
    "UNAMBIGUOUS_ALIASES",
    "UNAMBIGUOUS_TICKERS",
    "detect_asset",
    "normalize_asset",
]
