"""Audit **fail-closed** des secrets : présence, robustesse et rotation.

Ce module ne dépend que de la bibliothèque standard, afin de pouvoir tourner en
**pre-deploy** avant même l'installation des dépendances applicatives.

Trois contrôles sont effectués :

1. **Présence** — chaque secret requis est défini et non vide.
2. **Robustesse / unicité** — longueur minimale, pas de valeur de remplacement
   (`change-me...`), pas d'entropie ridicule, clé Fernet structurellement
   valide, aucun secret partagé entre deux usages.
3. **Rotation** — chaque secret est comparé à un *registre* (`ledger`) qui ne
   stocke qu'une **empreinte** (SHA-256 tronqué) et la date de dernière
   rotation. Si l'empreinte a changé sans rotation enregistrée, ou si la
   rotation date de plus que la durée de vie maximale, l'audit échoue.
4. **Production** — les contrôles ci-dessus portent sur *cette* machine : un
   `.env` conforme ne dit rien de ce que tourne le service déployé. Quand une
   URL est disponible (`--remote` / `$DEPLOYED_URL`), les empreintes du
   **processus déployé** (publiées par `GET /secrets/fingerprints`, voir
   `core/config_runtime.deployed_secret_report`) sont comparées une à une aux
   empreintes locales : même empreinte = même valeur. Une valeur différente, un
   secret absent en production : l'audit échoue. Aucune URL fournie : l'audit
   le **dit** au lieu de laisser croire qu'il a mesuré la production, et
   `--require-remote` transforme cette absence en refus.

Politique **fail-closed** : `AuditResult.ok` vaut `False` dès qu'une issue de
sévérité `error` est détectée. Le code appelant doit alors refuser de déployer
(le CLI `scripts/verify_secrets.py` sort avec le code 1).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

LEDGER_VERSION = 1
FINGERPRINT_SALT = "ai-trade-secret-ledger:v1"

#: Durée de vie par défaut d'un secret avant rotation (jours).
DEFAULT_MAX_AGE_DAYS = 90
DEFAULT_LEDGER_PATH = "security/secret_rotation.json"
LEDGER_PATH_ENV = "SECRET_ROTATION_LEDGER"
MAX_AGE_ENV = "SECRET_MAX_AGE_DAYS"

ERROR = "error"
WARNING = "warning"

# Motifs de valeurs manifestement laissées en exemple (sous-chaîne, insensible
# à la casse). On reste volontairement prudent : pas de filtre sur « test »
# afin de ne pas casser les environnements de CI.
PLACEHOLDER_PATTERNS: Tuple[str, ...] = (
    "change-me",
    "changeme",
    "your-",
    "your_",
    "placeholder",
    "remplace",
    "a-remplir",
    "à-remplir",
    "<",
    ">",
    "xxxx",
    "todo",
    "insert-",
)

_TELEGRAM_TOKEN_RE = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{30,}$")

#: Une entrée d'**anneau de clés** : `vN:<clé>`, le préfixe étant facultatif
#: (« nu » veut dire version 1, ce qu'ont écrit tous les `.env` d'avant).
_KEY_VERSION_RE = re.compile(r"^v(\d+):(?P<key>.+)$")
DEFAULT_KEY_VERSION = 1

#: Le secret qui porte l'anneau : les clés **retirées**, gardées pour rouvrir le
#: chiffré historique. Il vit à côté de la clé active, il est vérifié comme elle.
RING_SECRET_NAME = "ENCRYPTION_KEYS_PREVIOUS"

# --- Vérification de la configuration **réellement déployée** ---------------- #
# Le contrôle local ne peut pas mentir sur ce qu'il a lu, mais il ne lit que
# `.env` et l'environnement de la machine qui l'exécute. La barrière annonçait
# donc « déploiement autorisé » sur la foi de valeurs que la production n'avait
# peut-être jamais reçues (variable jamais posée sur la plateforme, rotation
# faite d'un seul côté, service jamais redémarré : trois façons de croire un
# secret en place alors que le processus tourne avec l'ancien ou avec rien).
#
# D'où l'endpoint `GET /secrets/fingerprints` : le **processus déployé** publie
# l'empreinte de ce qu'il a chargé — jamais la valeur —, et la barrière compare.
# L'empreinte étant salée et tronquée, la comparer ne demande pas de faire
# circuler un secret, et l'égalité de deux empreintes a la même force qu'une
# égalité de valeurs. Le "sel" est **versionné** dans le rapport : deux versions
# du calcul ne se comparent pas, et le dire vaut mieux que rendre un écart
# inexpliqué.

#: Nom du rapport publié par le service déployé. Sert à refuser une réponse qui
#: vient d'autre chose que ce service (mauvaise URL, page d'erreur d'un proxy).
DEPLOYED_REPORT_KIND = "ai-trade-secret-report"
#: Version du **schéma** du rapport : un client plus ancien refuse un rapport
#: plus récent plutôt que d'en ignorer un champ qui lui importe.
DEPLOYED_REPORT_VERSION = 1
#: Chemin de l'endpoint côté service déployé.
DEPLOYED_FINGERPRINTS_PATH = "/secrets/fingerprints"
#: Variable qui porte l'URL du service déployé (facultative : sans elle, la
#: barrière reste locale — mais elle le dit, voir `apply_deployed_check`).
DEPLOYED_URL_ENV = "DEPLOYED_URL"
#: Délai d'attente de la vérification distante, en secondes.
DEFAULT_DEPLOYED_TIMEOUT = 10.0
#: Taille maximale acceptée pour la réponse du service déployé (une URL erronée
#: peut pointer une page arbitrairement grosse).
MAX_DEPLOYED_BODY_BYTES = 1_000_000
#: Nom d'issue utilisé pour tout ce qui concerne la production elle-même (elle
#: n'est pas un secret : ses problèmes ne peuvent pas être attribués à un nom de
#: secret précis).
DEPLOYED_NAME = "DEPLOYED"


@dataclass(frozen=True)
class SecretSpec:
    """Description attendue d'un secret.

    `required=False` signifie « facultatif en l'absence totale, mais s'il est
    fourni il doit être valide ». C'est le cas des clés de fournisseurs.
    """

    name: str
    min_length: int = 16
    required: bool = True
    kind: str = "string"  # "string" | "fernet" | "telegram_token"
    placeholders: Tuple[str, ...] = ()
    description: str = ""


DEFAULT_SPECS: Tuple[SecretSpec, ...] = (
    SecretSpec(
        "WEBHOOK_SECRET",
        min_length=16,
        placeholders=("change-me-super-secret",),
        description="Signature des alertes webhook entrantes (moteur interne ou source externe)",
    ),
    SecretSpec(
        "INTERNAL_API_KEY",
        min_length=16,
        placeholders=("change-me-internal-api-key",),
        description="Clé des endpoints internes FastAPI",
    ),
    SecretSpec(
        "ENCRYPTION_KEY",
        min_length=44,
        kind="fernet",
        description="Clé Fernet de chiffrement des identifiants broker (version active)",
    ),
    SecretSpec(
        RING_SECRET_NAME,
        min_length=44,
        required=False,
        kind="fernet_ring",
        description="Clés retirées, gardées pour rouvrir le chiffré historique "
        "(`vN:<clé>`, la plus récente d'abord)",
    ),
    SecretSpec(
        "SUPABASE_SERVICE_KEY",
        min_length=20,
        placeholders=("your-service-role-key",),
        description="Clé service Supabase",
    ),
    SecretSpec(
        "TELEGRAM_BOT_TOKEN",
        min_length=20,
        kind="telegram_token",
        placeholders=("123456:abc-def...",),
        description="Token du bot Telegram",
    ),
    SecretSpec("GEMINI_API_KEY", min_length=20, required=False, description="Clé Gemini"),
    SecretSpec("GROQ_API_KEY", min_length=20, required=False, description="Clé Groq"),
    SecretSpec("FINNHUB_API_KEY", min_length=16, required=False, description="Clé Finnhub"),
    SecretSpec(
        "ALPACA_API_KEY",
        min_length=16,
        required=False,
        placeholders=("pkdemo_real_alpaca_3479y",),
        description="Clé Alpaca",
    ),
    SecretSpec(
        "ALPACA_SECRET_KEY",
        min_length=16,
        required=False,
        placeholders=("sk_demo_real_secret_2893xyza",),
        description="Secret Alpaca",
    ),
)


#: Ce qu'une rotation **casse**, secret par secret. C'est la phrase que l'outil de
#: rotation affiche **avant** d'écrire quoi que ce soit (`scripts/generate_secrets.py
#: --rotate`) : tourner un secret est une décision, et une décision se prend sur ses
#: conséquences, pas sur un nom de variable.
#:
#: Elle vit ici, à côte des specs, pour deux raisons : elle doit couvrir **tous**
#: les secrets (un test l'exige, sinon un secret ajouté plus tard se tournerait
#: sans que personne ne lise ce qu'il porte), et elle doit se relire au même
#: endroit que la définition du secret. Une conséquence oubliée coûte un ordre
#: refusé, ou un bot muet — c'est-à-dire trop tard.
ROTATION_IMPACT: Dict[str, str] = {
    "WEBHOOK_SECRET": (
        "l'émetteur externe doit suivre dans la même fenêtre : la signature est "
        "comparée une seule fois, en temps constant (`api/webhook.py`), sans liste "
        "d'ancien secret — sinon ses alertes partent en 401 et n'arrivent jamais."
    ),
    "INTERNAL_API_KEY": (
        "la moins chère : les endpoints internes refusent en 401 (et en 503 s'il "
        "n'y en a aucune), et l'écran d'administration de l'app doit la ressaisir — "
        "rien à publier côté magasin d'applications."
    ),
    "ENCRYPTION_KEY": (
        "l'anneau : la clé sortante doit passer dans ENCRYPTION_KEYS_PREVIOUS, sinon "
        "les identifiants broker déjà chiffrés deviennent illisibles et leurs ordres "
        "sont refusés ; `scripts/rotate_encryption_key.py --apply` réécrit le "
        "chiffré, et c'est seulement quand il ne reste rien qu'elle peut quitter "
        "l'anneau."
    ),
    RING_SECRET_NAME: (
        "retirer une clé de l'anneau rend illisible — donc refusé — tout chiffré "
        "qu'elle seule rouvrait : `scripts/rotate_encryption_key.py` compte ce qui "
        "reste avant qu'on la retire."
    ),
    "SUPABASE_SERVICE_KEY": (
        "coupure nette : l'ancienne clé meurt à la rotation dans le tableau de bord, "
        "donc la rotation et le redémarrage doivent tomber dans le même geste — sinon "
        "le worker échoue à sa première lecture (médias, index, réglages)."
    ),
    "TELEGRAM_BOT_TOKEN": (
        "coupure nette : `/revoke` tue l'ancien jeton aussitôt et le bot devient "
        "muet (il ne plante pas) ; un webhook Telegram éventuel est à "
        "ré-enregistrer."
    ),
    "GEMINI_API_KEY": (
        "coupe l'analyse de news, la vision et les embeddings vectoriels jusqu'au "
        "redéploiement."
    ),
    "GROQ_API_KEY": (
        "coupe l'analyse de news et la transcription des vocaux (le repli local "
        "`faster-whisper` reprend s'il est installé, poids compris)."
    ),
    "FINNHUB_API_KEY": (
        "coupe les données de marché Finnhub ; les replis Yahoo/Binance/CoinGecko/"
        "Stooq prennent le relais."
    ),
    "ALPACA_API_KEY": "coupe le compte Alpaca partagé (paper) jusqu'au redéploiement.",
    "ALPACA_SECRET_KEY": "coupe le compte Alpaca partagé (paper) jusqu'au redéploiement.",
}


@dataclass(frozen=True)
class Issue:
    name: str
    severity: str
    message: str

    @property
    def is_error(self) -> bool:
        return self.severity == ERROR

    def as_dict(self) -> Dict[str, str]:
        return {"name": self.name, "severity": self.severity, "message": self.message}


@dataclass
class AuditResult:
    ok: bool
    issues: List[Issue] = field(default_factory=list)
    checked: List[str] = field(default_factory=list)
    rotation: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    #: Ce qui a été mesuré de la **production**, ou pourquoi rien n'a pu l'être
    #: (voir `apply_deployed_check`). `None` = la question n'a pas été posée.
    deployed: Optional[Dict[str, Any]] = None
    #: Le **périmètre** du scan anti-fuite : combien de fichiers lus, ce qui était
    #: illisible, et ce qui a été écarté par politique — voir `scan_repo` et
    #: `ScanOutcome`. `None` = le scan n'a pas été demandé. Un rapport qui porte
    #: « aucune fuite » sans ce champ ne dit pas *sur quoi* il porte.
    scan: Optional[Dict[str, Any]] = None

    @property
    def errors(self) -> List[Issue]:
        return [i for i in self.issues if i.is_error]

    @property
    def warnings(self) -> List[Issue]:
        return [i for i in self.issues if not i.is_error]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "checked": self.checked,
            "issues": [i.as_dict() for i in self.issues],
            "rotation": self.rotation,
            "deployed": self.deployed,
            "scan": self.scan,
        }


# --------------------------------------------------------------------------- #
# Valeurs / environnement
# --------------------------------------------------------------------------- #

_QUOTES = ('"', "'")


def parse_env_file(path: str | os.PathLike[str]) -> Dict[str, str]:
    """Lit un fichier `.env` style `KEY=VALUE` sans dépendance externe.

    Les lignes vides, les commentaires et les préfixes `export ` sont ignorés.
    Les guillemets entourant la valeur sont retirés.
    """
    values: Dict[str, str] = {}
    p = Path(path)
    if not p.is_file():
        return values
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Retire un commentaire de fin de ligne éventuel (hors guillemets).
        if value and value[0] not in _QUOTES:
            value = value.split("#", 1)[0].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in _QUOTES:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_effective_env(
    env_file: Optional[str | os.PathLike[str]] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Fusionne `.env` puis l'environnement réel (ce dernier gagne)."""
    merged: Dict[str, str] = {}
    if env_file is not None:
        merged.update(parse_env_file(env_file))
    merged.update(dict(environ if environ is not None else os.environ))
    return merged


# --------------------------------------------------------------------------- #
# Empreintes & registre de rotation
# --------------------------------------------------------------------------- #


def secret_fingerprint(name: str, value: str) -> str:
    """Empreinte non réversible (16 hex) — ne jamais stocker le secret en clair."""
    material = f"{FINGERPRINT_SALT}|{name}|{value}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def fingerprints_for_values(
    values: Mapping[str, str],
    specs: Sequence[SecretSpec] = DEFAULT_SPECS,
) -> Dict[str, str]:
    """Empreintes des secrets **définis**, dans l'ordre des `specs`.

    C'est le seul contenu du rapport publié par un service déployé : un nom et
    16 caractères hexadécimaux par secret. L'ordre est stable (celui des specs)
    pour que deux rapports se lisent côte à côte, et un nom sans valeur n'y
    figure pas — « absent » et « vide » sont le même fait du point de vue de
    l'audit, et c'est `check_presence_and_strength` qui le dit.

    Le sel est fixe (`FINGERPRINT_SALT`) : c'est ce qui permet à deux machines
    de comparer sans jamais échanger de valeur.
    """
    fingerprints: Dict[str, str] = {}
    for spec in specs:
        value = (values.get(spec.name) or "").strip()
        if value:
            fingerprints[spec.name] = secret_fingerprint(spec.name, value)
    return fingerprints


def _empty_ledger() -> Dict[str, Any]:
    return {"version": LEDGER_VERSION, "secrets": {}}


def read_ledger(
    path: str | os.PathLike[str],
) -> Tuple[Dict[str, Any], str, str]:
    """Le registre, son **état**, et le détail de cet état.

    `load_ledger` ramène tout à un registre vide, ce qui est commode à lire mais
    efface une différence qui compte : un fichier **absent** dit « première mise
    en place », un fichier **illisible** dit « il y avait peut-être des dates de
    rotation, je ne les ai pas lues ». Confondre les deux fait afficher « rotation
    non enregistrée — lance `--record` » à quelqu'un dont le registre est en fait
    corrompu : le geste proposé l'écrase et l'historique est perdu.

    États : `LEDGER_STATE_MISSING` (rien à lire), `LEDGER_STATE_OK`, ou
    `LEDGER_STATE_CORRUPT` (JSON invalide, fichier non lisible, ou racine qui
    n'est pas un objet JSON).
    """
    p = Path(path)
    if not p.is_file():
        return _empty_ledger(), LEDGER_STATE_MISSING, "absent"
    try:
        raw = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return _empty_ledger(), LEDGER_STATE_CORRUPT, f"lecture impossible ({exc.__class__.__name__})"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return _empty_ledger(), LEDGER_STATE_CORRUPT, f"JSON invalide (ligne {exc.lineno})"
    if not isinstance(data, dict):
        return _empty_ledger(), LEDGER_STATE_CORRUPT, "la racine n'est pas un objet JSON"
    secrets = data.get("secrets")
    if not isinstance(secrets, dict):
        data["secrets"] = {}
    data.setdefault("version", LEDGER_VERSION)
    return data, LEDGER_STATE_OK, "lu"


def load_ledger(path: str | os.PathLike[str]) -> Dict[str, Any]:
    """Le registre, ramené à `{"version", "secrets"}` — vide s'il est absent ou illisible.

    Sur la seule lecture, absent et corrompu se ressemblent ; c'est `read_ledger`
    qui les distingue, et `ledger_issues` qui refuse le second. Ici on ne perd
    pas l'information de l'appelant : on lui donne le registre vide qu'il attend.
    """
    return read_ledger(path)[0]


def ledger_issues(path: str | os.PathLike[str]) -> List[Issue]:
    """L'issue (une erreur) qui dit qu'un registre n'a **pas** pu être lu.

    Vide quand le fichier est absent (première mise en place, rien à signaler) ou
    lisible. Les points d'entrée qui prennent un verdict (`enforce_secret_rotation`,
    `verify_secrets.py`, le hook pre-push) la passent à `run_audit` : un registre
    corrompu y devient une erreur **nommée**, au lieu du « rotation non
    enregistrée » trompeur qu'un registre vide fabrique pour chaque secret.
    """
    _ledger, state, detail = read_ledger(path)
    if state != LEDGER_STATE_CORRUPT:
        return []
    return [
        Issue(
            LEDGER_ISSUE_NAME,
            ERROR,
            f"registre de rotation illisible ({detail}) : {path} — inspecte-le et "
            "répare-le ; `--record` écraserait l'historique qu'il contient encore.",
        )
    ]


def save_ledger(path: str | os.PathLike[str], ledger: Mapping[str, Any]) -> None:
    """Écrit le registre **en octets**, et donc toujours en LF.

    `write_text` traduit `\n` en fin de ligne de la plateforme : sur Windows, le
    registre sortait en CRLF, ce qu'`.editorconfig` interdit et ce que le contrôle
    de fins de ligne du dépôt refuse — un `--record` sur ce poste faisait échouer
    la suite, pour un fichier que personne ne relit. Même règle que les scripts de
    ce dépôt : un fichier écrit par un outil s'écrit en octets.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": LEDGER_VERSION,
        "secrets": ledger.get("secrets", {}),
    }
    body = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    p.write_bytes(body.encode("utf-8"))


def build_ledger_entries(
    values: Mapping[str, str],
    specs: Sequence[SecretSpec] = DEFAULT_SPECS,
    now: Optional[datetime] = None,
) -> Dict[str, Dict[str, str]]:
    """Construit/replace les entrées de registre pour les secrets définis.

    Utilisé par le mode `--record` après une rotation légitime.
    """
    stamp = (now or datetime.now()).date().isoformat()
    entries: Dict[str, Dict[str, str]] = {}
    for spec in specs:
        value = (values.get(spec.name) or "").strip()
        if not value:
            continue
        entries[spec.name] = {
            "fingerprint": secret_fingerprint(spec.name, value),
            "rotated_at": stamp,
        }
    return entries


# --------------------------------------------------------------------------- #
# Contrôles
# --------------------------------------------------------------------------- #


def is_placeholder(value: str, spec: SecretSpec) -> bool:
    """Vrai si la valeur est un exemple laissé en place, jamais un vrai secret.

    Deux lecteurs en dépendent, et ils doivent répondre **la même chose** :

    * l'audit, qui refuse une valeur d'exemple à la place d'un secret
      (`check_presence_and_strength`) ou la laisse hors du scan anti-fuite ;
    * les outils qui remplissent un `.env` vide, pour lesquels un exemple est du
      *manquant* déguisé — une valeur qui doit être remplacée, pas conservée.

    D'où l'exposition publique : recopier cette liste de motifs ailleurs serait
    la faire diverger de l'audit, en silence.
    """
    lowered = value.strip().strip("\"'").lower()
    if lowered in {p.lower() for p in spec.placeholders}:
        return True
    return any(pattern in lowered for pattern in PLACEHOLDER_PATTERNS)


def _check_strength(spec: SecretSpec, value: str) -> Optional[str]:
    if is_placeholder(value, spec):
        return "valeur de remplacement / exemple détectée — à régénérer"
    if spec.kind == "fernet_ring":
        # Une liste porte forcément des séparateurs, et une entrée fautive ne dit
        # pas la même chose qu'une liste fautive : c'est `check_key_ring` qui la
        # juge, entrée par entrée, et qui nomme celle qui cloche.
        return None
    if any(c.isspace() for c in value):
        return "la valeur contient un espace"
    if len(value) < spec.min_length:
        return f"trop court ({len(value)} < {spec.min_length} caractères)"
    distinct = len(set(value))
    if distinct < 8:
        return f"entropie faible ({distinct} caractères distincts)"
    if spec.kind == "fernet":
        try:
            _version, key = parse_key_entry(value)
        except ValueError as exc:
            return str(exc)
        if not is_valid_fernet_key(key):
            return (
                "clé Fernet invalide (attendu : base64 urlsafe de 32 octets, "
                "précédée éventuellement de « vN: »)"
            )
    if spec.kind == "telegram_token" and not _TELEGRAM_TOKEN_RE.match(value):
        return "format de token Telegram invalide (attendu : « <id>:<secret> »)"
    return None


def is_valid_fernet_key(value: str) -> bool:
    """Valide la *structure* d'une clé Fernet sans dépendre du paquet cryptography."""
    if not value or len(value) != 44:
        return False
    try:
        raw = base64.urlsafe_b64decode(value.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        return False
    return len(raw) == 32


def parse_key_entry(entry: str, default_version: int = DEFAULT_KEY_VERSION) -> Tuple[int, str]:
    """`(version, clé)` d'une entrée d'anneau — `vN:<clé>` ou `<clé>` (version 1).

    Une clé Fernet (comme tout base64 urlsafe) ne contient **jamais** de « : » :
    sa présence signifie donc qu'un préfixe de version a été écrit, et un préfixe
    illisible est signalé comme tel plutôt que pris pour la clé elle-même — sinon
    la faute se paierait au premier déchiffrement, très loin de la ligne écrite.

    Lève `ValueError` avec un message qui ne recopie **jamais** la valeur.
    """
    text = str(entry).strip()
    if ":" in text:
        match = _KEY_VERSION_RE.match(text)
        if not match:
            raise ValueError("préfixe de version invalide : attendu « vN:<clé> » avec N ≥ 1")
        version = int(match.group(1))
        if version < 1:
            raise ValueError("version invalide : l'anneau commence à v1")
        return version, match.group("key")
    return default_version, text


def is_valid_key_entry(entry: str, default_version: int = DEFAULT_KEY_VERSION) -> bool:
    """L'entrée porte-t-elle une clé Fernet exploitable (voir `parse_key_entry`) ?"""
    try:
        _version, key = parse_key_entry(entry, default_version)
    except ValueError:
        return False
    return is_valid_fernet_key(key)


def ring_entries(value: str) -> List[str]:
    """Les entrées d'un anneau écrit à la main : virgules, points-virgules, espaces.

    L'espace **est** un séparateur ici, à la différence de la liste des canaux
    Telegram (`core/config_runtime.py`) : une clé Fernet n'en contient jamais, donc
    découper dessus ne peut pas fabriquer deux clés fantômes.
    """
    return [token for token in re.split(r"[,;\s]+", str(value or "").strip()) if token]


@dataclass(frozen=True)
class KeyRingInfo:
    """Ce qu'on peut dire d'un anneau de clés **sans déchiffrer** ni le journaliser.

    Sert au rapport de production : « quelle version chiffre, quelles versions
    rouvrent ». C'est une lecture d'en-tête (`vN:`), donc ni `cryptography` ni la
    moindre clé ne sont nécessaires — et rien n'est recopié dans le rapport.
    """

    primary_version: Optional[int] = None
    versions: Tuple[int, ...] = ()
    #: Renseigné quand une entrée est illisible : la production tourne alors un
    #: anneau incomplet, ce que la barrière doit dire plutôt que de le taire.
    error: str = ""

    def as_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "primary_version": self.primary_version,
            "versions": list(self.versions),
        }
        if self.error:
            payload["error"] = self.error
        return payload


def key_ring_info(primary_value: str, previous_value: str = "") -> KeyRingInfo:
    """`(version active, versions de l'anneau)` d'une configuration, sans clé.

    Lève `ValueError` si la clé **active** est illisible (l'appelant décide quoi
    en faire) ; une entrée d'anneau illisible n'interrompt pas la lecture : elle
    est rendue dans `error`, avec sa **position**, jamais sa valeur.
    """
    raw_primary = str(primary_value or "").strip()
    if not raw_primary:
        return KeyRingInfo(None, (), "ENCRYPTION_KEY absente")
    primary_version, _ = parse_key_entry(raw_primary)
    versions: List[int] = [primary_version]
    for index, entry in enumerate(ring_entries(previous_value), start=1):
        try:
            version, _ = parse_key_entry(entry, default_version=DEFAULT_KEY_VERSION)
        except ValueError as exc:
            return KeyRingInfo(
                primary_version, tuple(versions), f"entrée {index} de l'anneau illisible : {exc}"
            )
        versions.append(version)
    return KeyRingInfo(primary_version, tuple(versions))


def check_key_ring(
    values: Mapping[str, str],
    specs: Sequence[SecretSpec] = DEFAULT_SPECS,
) -> List[Issue]:
    """Vérifie l'anneau de clés : préfixes, versions uniques, doublon inutile.

    L'enjeu n'est pas la propreté. Une clé retirée mal écrite, c'est du chiffré
    historique qui ne se rouvre plus — et qui ne l'annonce qu'au moment où
    quelqu'un veut exécuter un ordre, donc le pire moment. On ne devine donc
    **rien** : chaque entrée est vérifiée, chaque version doit être unique, et une
    entrée qui recopie la clé active est refusée — elle ne rouvre rien de plus et
    prolonge la vie d'un secret qu'on voulait retirer.
    """
    if not any(spec.name == RING_SECRET_NAME for spec in specs):
        return []
    raw = (values.get(RING_SECRET_NAME) or "").strip()
    if not raw:
        return []

    issues: List[Issue] = []
    versions: Dict[int, int] = {}
    keys: Dict[str, int] = {}
    for index, entry in enumerate(ring_entries(raw), start=1):
        try:
            version, key = parse_key_entry(entry)
        except ValueError as exc:
            issues.append(Issue(RING_SECRET_NAME, ERROR, f"entrée {index} : {exc}"))
            continue
        if not is_valid_fernet_key(key):
            issues.append(
                Issue(
                    RING_SECRET_NAME,
                    ERROR,
                    f"entrée {index} (v{version}) : clé Fernet invalide "
                    "(attendu : base64 urlsafe de 32 octets)",
                )
            )
            continue
        if version in versions:
            issues.append(
                Issue(
                    RING_SECRET_NAME,
                    ERROR,
                    f"version v{version} écrite deux fois (entrées {versions[version]} "
                    f"et {index}) : laquelle ouvre le chiffré existant ?",
                )
            )
        else:
            versions[version] = index
        if key in keys:
            issues.append(
                Issue(
                    RING_SECRET_NAME,
                    WARNING,
                    f"les entrées {keys[key]} et {index} portent la MÊME clé sous deux "
                    "versions : une seule suffit",
                )
            )
        else:
            keys[key] = index

    primary = (values.get("ENCRYPTION_KEY") or "").strip()
    if primary:
        try:
            primary_version, primary_key = parse_key_entry(primary)
        except ValueError:
            return issues  # déjà signalé par le contrôle de robustesse
        for version, index in sorted(versions.items()):
            if version == primary_version:
                issues.append(
                    Issue(
                        RING_SECRET_NAME,
                        ERROR,
                        f"l'entrée {index} (v{version}) porte la version de "
                        "ENCRYPTION_KEY : deux clés pour une version rendraient "
                        "l'ouverture indécidable",
                    )
                )
        for key, index in sorted(keys.items()):
            if key == primary_key:
                issues.append(
                    Issue(
                        RING_SECRET_NAME,
                        ERROR,
                        f"l'entrée {index} recopie ENCRYPTION_KEY : elle ne rouvre "
                        "rien de plus et prolonge un secret qu'on voulait retirer",
                    )
                )
    return issues


def check_presence_and_strength(
    values: Mapping[str, str],
    specs: Sequence[SecretSpec] = DEFAULT_SPECS,
) -> List[Issue]:
    issues: List[Issue] = []
    for spec in specs:
        raw = values.get(spec.name)
        value = (raw or "").strip()
        if not value:
            if spec.required:
                issues.append(Issue(spec.name, ERROR, "secret absent ou vide"))
            continue
        problem = _check_strength(spec, value)
        if problem:
            severity = ERROR if spec.required else WARNING
            issues.append(Issue(spec.name, severity, problem))
    return issues


def check_distinctness(
    values: Mapping[str, str],
    specs: Sequence[SecretSpec] = DEFAULT_SPECS,
) -> List[Issue]:
    """Deux secrets ne doivent jamais partager la même valeur."""
    issues: List[Issue] = []
    seen: Dict[str, str] = {}
    for spec in specs:
        value = (values.get(spec.name) or "").strip()
        if not value:
            continue
        if value in seen:
            issues.append(
                Issue(
                    spec.name,
                    ERROR,
                    f"valeur identique à {seen[value]} — utilise deux secrets distincts",
                )
            )
        else:
            seen[value] = spec.name
    return issues


def _parse_date(value: Any, fallback: Optional[date] = None) -> Optional[date]:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return fallback
    return fallback


def check_rotation(
    values: Mapping[str, str],
    ledger: Mapping[str, Any],
    specs: Sequence[SecretSpec] = DEFAULT_SPECS,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    now: Optional[datetime] = None,
    allow_missing: bool = False,
) -> Tuple[List[Issue], Dict[str, Dict[str, Any]]]:
    """Vérifie que chaque secret est enregistré et « frais »."""
    issues: List[Issue] = []
    reference = now or datetime.now()
    today = reference.date()
    entries = ledger.get("secrets", {}) if isinstance(ledger, dict) else {}
    report: Dict[str, Dict[str, Any]] = {}

    for spec in specs:
        value = (values.get(spec.name) or "").strip()
        if not value:
            continue
        fingerprint = secret_fingerprint(spec.name, value)
        entry = entries.get(spec.name) if isinstance(entries, dict) else None
        info: Dict[str, Any] = {"fingerprint": fingerprint, "recorded": bool(entry)}

        if not isinstance(entry, dict):
            info["status"] = "unrecorded"
            if not allow_missing:
                issues.append(
                    Issue(
                        spec.name,
                        ERROR,
                        "rotation non enregistrée — lance `--record` après la "
                        "première mise en place du secret",
                    )
                )
            else:
                issues.append(
                    Issue(spec.name, WARNING, "rotation non enregistrée (toléré)")
                )
            report[spec.name] = info
            continue

        recorded_fp = str(entry.get("fingerprint") or "")
        info["recorded_fingerprint"] = recorded_fp
        if recorded_fp != fingerprint:
            info["status"] = "changed"
            issues.append(
                Issue(
                    spec.name,
                    ERROR,
                    "secret modifié sans rotation enregistrée — vérifie que la "
                    "rotation était intentionnelle puis relance `--record`",
                )
            )
            report[spec.name] = info
            continue

        rotated_at = _parse_date(entry.get("rotated_at"))
        if rotated_at is None:
            info["status"] = "bad_date"
            issues.append(
                Issue(spec.name, ERROR, "date de rotation manquante/illisible dans le registre")
            )
            report[spec.name] = info
            continue

        age_days = (today - rotated_at).days
        info["status"] = "ok"
        info["rotated_at"] = rotated_at.isoformat()
        info["age_days"] = age_days
        info["max_age_days"] = max_age_days
        if age_days > max_age_days:
            info["status"] = "expired"
            issues.append(
                Issue(
                    spec.name,
                    ERROR,
                    f"rotation en retard : {age_days} jours > {max_age_days} jours "
                    f"(dernière : {rotated_at.isoformat()})",
                )
            )
        report[spec.name] = info

    return issues, report


def ledger_summary(
    ledger: Mapping[str, Any],
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Synthèse du registre de rotation, **lisible sans ouvrir le JSON**.

    `check_rotation` dit, secret par secret, si la rotation est enregistrée ; cette
    fonction dit ce que le registre contient *en tant que fichier*, d'un seul coup
    d'œil : combien d'entrées, de quand date la plus ancienne (et son âge), et si
    des dates sont illisibles. C'est la ligne qu'une personne doit pouvoir lire sur
    un poste ou dans un journal de CI sans ouvrir `security/secret_rotation.json`.

    Un registre absent ou vide rend des compteurs à zéro plutôt que `None` :
    « rien d'enregistré » est une réponse, et c'est à l'appelant de la relie
    à l'état du fichier (`read_ledger`).
    """
    secrets = ledger.get("secrets", {}) if isinstance(ledger, dict) else {}
    if not isinstance(secrets, dict):
        secrets = {}

    dates: List[date] = []
    unreadable = 0
    for entry in secrets.values():
        rotated_at = _parse_date(entry.get("rotated_at")) if isinstance(entry, dict) else None
        if rotated_at is None:
            unreadable += 1
        else:
            dates.append(rotated_at)

    oldest = min(dates) if dates else None
    newest = max(dates) if dates else None
    today = (now or datetime.now()).date()
    return {
        "entries": len(secrets),
        "dated": len(dates),
        "unreadable": unreadable,
        "oldest": oldest.isoformat() if oldest else None,
        "newest": newest.isoformat() if newest else None,
        "oldest_days": (today - oldest).days if oldest else None,
    }


# --------------------------------------------------------------------------- #
# Détection de fuite : valeurs de secrets présentes dans les fichiers du dépôt
# --------------------------------------------------------------------------- #

#: Répertoires jamais parcourus (dépendances, artefacts, caches, VCS).
DEFAULT_SCAN_EXCLUDED_DIRS = frozenset(
    {
        ".git", ".hg", ".svn",
        ".venv", "venv", "env",
        "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
        ".gradle", "build", "dist", "out", "target",
        "node_modules", "artifacts", ".freebuff", ".idea", ".vscode",
    }
)

#: Extensions binaires (un secret "verbatim" y apparaîtrait illisible).
DEFAULT_SCAN_EXCLUDED_SUFFIXES = (
    ".pyc", ".pyo", ".pyd", ".so", ".dll", ".dylib", ".exe", ".bin",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp",
    ".pdf", ".zip", ".gz", ".tar", ".7z", ".jar", ".war", ".class",
    ".apk", ".aab", ".aar", ".dex", ".ttf", ".otf", ".woff", ".woff2",
    ".eot", ".db", ".sqlite", ".sqlite3", ".keystore", ".jks", ".p12",
)

#: Longueur minimale d'une valeur à rechercher (évite le bruit sur les valeurs courtes).
DEFAULT_SCAN_MIN_LENGTH = 8

#: Taille maximale d'un fichier à lire.
DEFAULT_SCAN_MAX_BYTES = 2_000_000

#: Occurrences rapportées au maximum par couple (secret, fichier).
MAX_HITS_PER_FILE = 5

#: Nom d'issue des faits qui portent sur le **scan lui-même** et non sur un secret
#: précis : un fichier trop gros pour être lu, un blob indexé illisible, un
#: `stat` refusé. « Aucune fuite détectée » ne doit jamais valoir « aucun
#: fichier n'a été laissé de côté » — un fichier non lu est un fait à nommer.
SCAN_NAME = "scan"

#: Pourquoi un fichier n'a pas été analysé **par politique** — et non parce qu'il
#: était illisible. Ces écarts ne sont pas des trous de couverture, mais ils
#: **bornent** ce que le scan prouve : « aucune fuite détectée » ne doit pas se lire
#: « tout le dépôt a été lu ». Ils sont donc nommés dans le rapport.
EXCLUSION_ENV = "env"
EXCLUSION_DIRECTORY = "dir"
EXCLUSION_SUFFIX = "suffix"
EXCLUSION_EMPTY = "empty"

#: États d'un registre de rotation, tels que `read_ledger` les distingue.
#: Un fichier **absent** et un fichier **illisible** mènent au même registre vide,
#: mais pas au même geste : le premier est une première mise en place, le second
#: a peut-être porté des dates de rotation que `--record` écraserait.
LEDGER_STATE_MISSING = "missing"
LEDGER_STATE_OK = "ok"
LEDGER_STATE_CORRUPT = "corrupt"

#: Nom d'issue d'un registre de rotation inexploitable.
LEDGER_ISSUE_NAME = "ledger"


def mask_secret(value: str) -> str:
    """Version masquée d'un secret, sûre à journaliser.

    On ne ré-écrit JAMAIS un secret en clair dans un rapport ou un log : un
    rapport de fuite qui contient la fuite est une seconde fuite.
    """
    if len(value) <= 6:
        return "***"
    return f"{value[:4]}…{value[-2:]}"


def _is_local_env_file(name: str) -> bool:
    """Fichiers d'environnement locaux (gitignorés) — sources, pas cibles."""
    return name == ".env" or (name.endswith(".env") and name != ".env.example")


def git_tracked_files(root: "str | os.PathLike[str]" = ".") -> Optional[List[str]]:
    """Liste des fichiers suivis par git, ou `None` si git/le dépôt est absent."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    decoded = proc.stdout.decode("utf-8", errors="replace")
    files = [p for p in decoded.split("\0") if p]
    return files or None


def _scan_label(root_path: Path, path: Path) -> str:
    """Le libellé d'un fichier dans un rapport : relatif à la racine, sinon absolu."""
    try:
        return str(path.relative_to(root_path))
    except ValueError:
        return str(path)


def _matching_suffix(name: str, suffixes: Sequence[str]) -> Optional[str]:
    """Le suffixe exclu qui fait tomber ce nom, ou `None` — pour le nommer."""
    for suffix in suffixes:
        if name.endswith(suffix):
            return suffix
    return None


def iter_scan_files(
    root: "str | os.PathLike[str]" = ".",
    tracked_files: Optional[Sequence[str]] = None,
    excluded_dirs: Iterable[str] = DEFAULT_SCAN_EXCLUDED_DIRS,
    excluded_suffixes: Sequence[str] = DEFAULT_SCAN_EXCLUDED_SUFFIXES,
    max_bytes: int = DEFAULT_SCAN_MAX_BYTES,
    on_skip: Optional[Callable[[Path, str], None]] = None,
    on_excluded: Optional[Callable[[str, str, str], None]] = None,
) -> Iterable[Path]:
    """Fichiers à analyser.

    Si `tracked_files` est fourni, seuls ces fichiers (relatifs à `root`) sont
    considérés : c'est la sémantique « fichiers suivis par le dépôt ». Sinon, on
    parcourt l'arborescence en excluant dépendances, caches et artefacts, et en
    ignorant les `.env` locaux (qui *contiennent* légitimement les secrets).

    Deux appels sortent, et ils ne disent pas la même chose :

    * `on_skip(chemin, raison)` — un fichier **écarté faute d'être lisible** (non
      régulier, `stat` refusé, plus gros que `max_bytes`) : c'est un trou dans la
      couverture du scan, donc un fait à signaler ;
    * `on_excluded(libellé, raison, genre)` — un fichier **écarté par politique**
      (`.env` local, suffixe binaire, répertoire exclu, fichier vide) : il n'y a
      rien à chercher dedans, mais ces écarts **bornent** ce que le scan prouve, et
      un rapport qui les tairait laisserait croire que tout le dépôt a été lu.
      Un répertoire exclu est nommé **une fois** (son nom), jamais fichier par
      fichier.
    """
    root_path = Path(root)
    if tracked_files is not None:
        candidates: Iterable[Path] = (
            root_path / rel for rel in tracked_files if not Path(rel).is_absolute()
        )
    else:
        candidates = (p for p in root_path.rglob("*") if p.is_file())

    for path in candidates:
        name = path.name
        if _is_local_env_file(name):
            _note_excluded(
                on_excluded,
                _scan_label(root_path, path),
                "fichier d'environnement local (source, pas cible)",
                EXCLUSION_ENV,
            )
            continue
        suffix = _matching_suffix(name, excluded_suffixes)
        if suffix is not None:
            _note_excluded(
                on_excluded,
                _scan_label(root_path, path),
                f"suffixe non analysable ({suffix})",
                EXCLUSION_SUFFIX,
            )
            continue
        # Exclusion sur le chemin RELATIF : sinon un projet cloné sous un
        # dossier nommé `build`/`env`/`target` serait intégralement ignoré.
        try:
            relative_parts = path.relative_to(root_path).parts
        except ValueError:  # sans signal : pas de chemin relatif, on nomme par l'absolu
            relative_parts = path.parts
        excluded_dir = next((part for part in relative_parts if part in excluded_dirs), None)
        if excluded_dir is not None:
            _note_excluded(
                on_excluded,
                excluded_dir,
                f"répertoire exclu : {excluded_dir}",
                EXCLUSION_DIRECTORY,
            )
            continue
        try:
            size = path.stat().st_size
        except OSError as exc:
            _note_skip(on_skip, path, f"stat impossible ({exc.__class__.__name__})")
            continue
        if not path.is_file():
            _note_skip(on_skip, path, "n'est pas un fichier régulier")
            continue
        if size > max_bytes:
            _note_skip(
                on_skip, path, f"plus gros que la limite du scan ({size} > {max_bytes} octets)"
            )
            continue
        if size == 0:
            # Un fichier vide ne porte rien : l'écarter ne retire aucune preuve,
            # mais on le compte quand même pour que la couverture soit lisible.
            _note_excluded(
                on_excluded,
                _scan_label(root_path, path),
                "fichier vide (aucun octet)",
                EXCLUSION_EMPTY,
            )
            continue
        yield path


def _note_skip(
    on_skip: Optional[Callable[[Path, str], None]],
    path: Path,
    reason: str,
) -> None:
    if on_skip is not None:
        on_skip(path, reason)


def _note_excluded(
    on_excluded: Optional[Callable[[str, str, str], None]],
    label: str,
    reason: str,
    kind: str,
) -> None:
    if on_excluded is not None:
        on_excluded(label, reason, kind)


def scannable_targets(
    values: Mapping[str, str],
    specs: Sequence[SecretSpec] = DEFAULT_SPECS,
    min_length: int = DEFAULT_SCAN_MIN_LENGTH,
) -> List[Tuple[str, str]]:
    """Valeurs réellement recherchables : non vides, assez longues, non factices.

    Exposé publiquement pour que les appelants (hook pre-commit) puissent
    **savoir si le scan a une valeur de référence** : sans secret configuré, le
    scan ne trouve rien par construction, ce qui ne doit jamais passer pour une
    preuve d'absence de fuite.
    """
    return _scanning_targets(values, specs, min_length)


def _ring_key_material(value: str) -> List[str]:
    """Le matériau de chaque entrée d'anneau, préfixe de version retiré.

    Une entrée illisible est rendue telle quelle : elle n'a pas de matériau à
    chercher, et c'est `check_key_ring` qui a la charge de la refuser.
    """
    material: List[str] = []
    for entry in ring_entries(value):
        try:
            _version, key = parse_key_entry(entry)
        except ValueError:
            material.append(entry)
            continue
        material.append(key)
    return material


def _scanning_targets(
    values: Mapping[str, str],
    specs: Sequence[SecretSpec],
    min_length: int,
) -> List[Tuple[str, str]]:
    """Secrets réellement recherchables : non vides, assez longs, non factices."""
    targets: List[Tuple[str, str]] = []
    for spec in specs:
        value = (values.get(spec.name) or "").strip()
        if not value or len(value) < min_length:
            continue
        if is_placeholder(value, spec):
            continue
        if spec.kind == "fernet_ring":
            # Une clé **retirée** reste un secret vivant : elle rouvre le chiffré
            # historique. C'est donc son **matériau** qu'on recherche, pas la liste
            # entière (recopiée d'un bloc, elle ne se retrouve nulle part) ni
            # l'entrée préfixée — une fuite laisse tomber le « vN: ».
            targets.extend(
                (spec.name, key)
                for key in _ring_key_material(value)
                if len(key) >= min_length
            )
            continue
        targets.append((spec.name, value))
    return targets


def scan_text_for_values(
    text: str,
    targets: Sequence[Tuple[str, str]],
    path_label: str = "<text>",
) -> List[Issue]:
    """Cherche chaque valeur de secret dans un texte et rapporte ses occurrences."""
    issues: List[Issue] = []
    for name, value in targets:
        if not value:
            continue
        found = 0
        index = text.find(value)
        while index != -1 and found < MAX_HITS_PER_FILE:
            line = text.count("\n", 0, index) + 1
            issues.append(
                Issue(
                    name,
                    ERROR,
                    f"fuite : valeur retrouvée verbatim dans {path_label}:{line} "
                    f"(masquée {mask_secret(value)})",
                )
            )
            found += 1
            index = text.find(value, index + len(value))
        if found == MAX_HITS_PER_FILE:
            issues.append(
                Issue(
                    name,
                    WARNING,
                    f"fuite : plus de {MAX_HITS_PER_FILE} occurrences dans "
                    f"{path_label} (tronqué)",
                )
            )
    return issues


@dataclass
class ScanOutcome:
    """Le scan d'un dépôt : ce qu'il a trouvé, **et ce qu'il n'a pas regardé**.

    Un rapport de fuite ne dit jamais seulement « rien trouvé » : il dit aussi
    combien de fichiers il a lus, ce qui était illisible (nommé, donc un trou de
    couverture) et ce qui a été écarté **par politique** (`.env` local, suffixe
    binaire, répertoire exclu, fichier vide). Les deux derniers sont ce qui rend le
    verdict honnête : « aucune fuite » vaut pour le périmètre, et le périmètre se
    lit ici.
    """

    issues: List[Issue] = field(default_factory=list)
    #: Nombre de valeurs de référence réellement cherchées.
    targets: int = 0
    #: Nombre de fichiers lus (analysés ou reconnus binaires).
    scanned: int = 0
    #: `(libellé, raison)` des fichiers écartés faute d'être **lisibles** — un
    #: trou de couverture, également porté par une issue `[scan]`.
    unreadable: List[Tuple[str, str]] = field(default_factory=list)
    #: Fichiers d'environnement local écartés (nommés).
    env: List[str] = field(default_factory=list)
    #: Répertoires exclus, nommés **une fois** chacun.
    directories: List[str] = field(default_factory=list)
    #: `(libellé, raison)` des fichiers écartés pour leur suffixe.
    suffixes: List[Tuple[str, str]] = field(default_factory=list)
    #: Nombre de fichiers vides écartés.
    empty: int = 0

    @property
    def excluded_total(self) -> int:
        return len(self.env) + len(self.directories) + len(self.suffixes) + self.empty

    def as_dict(self) -> Dict[str, Any]:
        return {
            "targets": self.targets,
            "scanned": self.scanned,
            "unreadable": [
                {"file": label, "reason": reason} for label, reason in self.unreadable
            ],
            "excluded": {
                "env": list(self.env),
                "directories": list(self.directories),
                "suffixes": [
                    {"file": label, "reason": reason} for label, reason in self.suffixes
                ],
                "empty": self.empty,
            },
        }


def scan_repo(
    values: Mapping[str, str],
    root: "str | os.PathLike[str]" = ".",
    specs: Sequence[SecretSpec] = DEFAULT_SPECS,
    min_length: int = DEFAULT_SCAN_MIN_LENGTH,
    tracked_files: Optional[Sequence[str]] = None,
) -> ScanOutcome:
    """Scanne le dépôt : les fuites trouvées, et **le périmètre du scan**.

    Même détection que `scan_repo_for_secrets` — tout secret de `values` retrouvé
    verbatim, les valeurs factices ou trop courtes étant ignorées — mais le verdict
    vient avec sa couverture : combien de fichiers lus, ce qui était illisible, et
    ce qui a été écarté par politique. C'est ce que le rapport affiche pour que
    « aucune fuite détectée » ne se confonde pas avec « tout a été lu ».
    """
    targets = _scanning_targets(values, specs, min_length)
    outcome = ScanOutcome(targets=len(targets))
    if not targets:
        return outcome

    files = tracked_files
    if files is None:
        files = git_tracked_files(root)

    root_path = Path(root)

    # Ce qui est écarté faute d'être lisible est **nommé** : un fichier trop gros
    # pour être analysé n'est pas un fichier propre, et le rapport doit le dire.
    skipped: List[Tuple[str, str]] = []
    seen_env: set = set()
    seen_dirs: set = set()

    def _collect(label: str, reason: str, kind: str) -> None:
        if kind == EXCLUSION_ENV:
            if label not in seen_env:
                seen_env.add(label)
                outcome.env.append(label)
        elif kind == EXCLUSION_DIRECTORY:
            if label not in seen_dirs:
                seen_dirs.add(label)
                outcome.directories.append(label)
        elif kind == EXCLUSION_SUFFIX:
            outcome.suffixes.append((label, reason))
        elif kind == EXCLUSION_EMPTY:
            outcome.empty += 1

    for path in iter_scan_files(
        root,
        tracked_files=files,
        on_skip=lambda p, reason: skipped.append((_scan_label(root_path, p), reason)),
        on_excluded=_collect,
    ):
        label = _scan_label(root_path, path)
        try:
            raw = path.read_bytes()
        except OSError as exc:
            # Lisible pour `stat` mais pas pour `read` : le scan ne peut pas
            # répondre, et ce qu'il ne peut pas lire, il ne peut pas l'innocenter.
            outcome.issues.append(
                Issue(
                    SCAN_NAME,
                    ERROR,
                    f"fichier illisible, non analysé : {label} "
                    f"({exc.__class__.__name__})",
                )
            )
            continue
        outcome.scanned += 1
        if b"\x00" in raw[:8192]:
            continue  # binaire : hors périmètre par politique (suffixes exclus)
        text = raw.decode("utf-8", errors="replace")
        outcome.issues.extend(scan_text_for_values(text, targets, label))

    for label, reason in skipped:
        outcome.issues.append(
            Issue(SCAN_NAME, WARNING, f"fichier non analysé : {label} ({reason})")
        )
    outcome.unreadable = skipped
    outcome.directories.sort()
    return outcome


#: `run_audit` porte un paramètre **`scan_repo` (booléen)** qui masque la fonction
#: du même nom dans son corps. Cet alias lui rend la fonction sous un autre nom,
#: sans renommer le paramètre (l'API publique garde `scan_repo=True`).
_scan_repo_outcome = scan_repo


def scan_repo_for_secrets(
    values: Mapping[str, str],
    root: "str | os.PathLike[str]" = ".",
    specs: Sequence[SecretSpec] = DEFAULT_SPECS,
    min_length: int = DEFAULT_SCAN_MIN_LENGTH,
    tracked_files: Optional[Sequence[str]] = None,
) -> List[Issue]:
    """Signale tout secret de `values` retrouvé verbatim dans les fichiers suivis.

    Enveloppe de `scan_repo` : la signature historique rend la seule liste d'issues,
    pour les appelants qui n'ont pas besoin du périmètre du scan.
    """
    return scan_repo(
        values, root=root, specs=specs, min_length=min_length, tracked_files=tracked_files
    ).issues


def git_staged_files(
    root: "str | os.PathLike[str]" = ".",
    diff_filter: str = "ACMR",
) -> Optional[List[str]]:
    """Fichiers **indexés** (staged), ou `None` si git/le dépôt est absent.

    `ACMR` = ajoutés, copiés, modifiés, renommés : les suppressions sont exclues,
    puisqu'il n'y a plus de contenu à analyser.

    La distinction avec `git_tracked_files` compte : ici une liste **vide** est
    une information (« git est là, rien n'est indexé »), alors que `None`
    signifie « impossible de savoir ».
    """
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "diff",
                "--cached",
                "--name-only",
                f"--diff-filter={diff_filter}",
                "-z",
            ],
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    decoded = proc.stdout.decode("utf-8", errors="replace")
    return [p for p in decoded.split("\0") if p]


def git_staged_content(
    root: "str | os.PathLike[str]",
    path: str,
) -> Optional[bytes]:
    """Contenu **brut du blob indexé** (`:path`), ou `None` s'il est illisible.

    On lit l'index et non la copie de travail : après un `git add -p`, le fichier
    sur le disque peut différer de ce qui sera réellement validé. C'est l'index
    qui fait foi.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "cat-file", "blob", f":{path}"],
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def scan_staged_for_secrets(
    values: Mapping[str, str],
    root: "str | os.PathLike[str]" = ".",
    specs: Sequence[SecretSpec] = DEFAULT_SPECS,
    min_length: int = DEFAULT_SCAN_MIN_LENGTH,
    staged_files: Optional[Sequence[str]] = None,
    content_reader: Optional["Callable[[str], Optional[bytes | str]]"] = None,
) -> List[Issue]:
    """Cherche les valeurs de secrets dans le contenu **indexé**.

    Différences assumées avec `scan_repo_for_secrets` (scan du dépôt) :

    * on lit l'**index**, pas la copie de travail ;
    * les exclusions de répertoires ne s'appliquent pas — si un fichier est
      indexé, il sera commité, donc il doit être analysé ;
    * un fichier `.env` indexé est analysé (c'est précisément le cas
      `git add -f .env` que le hook doit bloquer), alors que le scan du dépôt
      l'ignore comme source de secrets.    `content_reader` permet d'injecter une lecture alternative (tests) ; par
defaut le contenu vient de l'index via `git_staged_content`.

    Un fichier indexé dont le contenu ne peut pas être lu est une **erreur**, pas
    une case sautée : le hook pre-commit ne peut pas prouver qu'un blob illisible
    est propre, et « aucune fuite détectée » sur un fichier qu'on n'a pas lu serait
    précisément le rapport que le hook existe pour empêcher.
    """
    targets = _scanning_targets(values, specs, min_length)
    if not targets:
        return []

    files = staged_files if staged_files is not None else git_staged_files(root)
    if not files:
        return []

    reader = content_reader or (lambda rel: git_staged_content(root, rel))
    issues: List[Issue] = []
    for relative in files:
        if relative.endswith(DEFAULT_SCAN_EXCLUDED_SUFFIXES):
            continue
        try:
            raw = reader(relative)
        except Exception as exc:  # lecture défensive : le refus est nommé, jamais avalé
            issues.append(
                Issue(
                    SCAN_NAME,
                    ERROR,
                    f"contenu indexé illisible, non analysé : {relative} "
                    f"({exc.__class__.__name__})",
                )
            )
            continue
        if raw is None:
            issues.append(
                Issue(
                    SCAN_NAME,
                    ERROR,
                    f"contenu indexé introuvable, non analysé : {relative}",
                )
            )
            continue
        if isinstance(raw, bytes):
            if len(raw) > DEFAULT_SCAN_MAX_BYTES:
                issues.append(
                    Issue(
                        SCAN_NAME,
                        WARNING,
                        f"fichier indexé non analysé : {relative} "
                        f"(plus gros que la limite du scan)",
                    )
                )
                continue
            if b"\x00" in raw[:8192]:
                continue  # binaire : hors périmètre par politique
            text = raw.decode("utf-8", errors="replace")
        else:
            text = raw
        issues.extend(scan_text_for_values(text, targets, relative))
    return issues


def run_audit(
    values: Mapping[str, str],
    ledger: Optional[Mapping[str, Any]] = None,
    specs: Sequence[SecretSpec] = DEFAULT_SPECS,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    now: Optional[datetime] = None,
    allow_missing_rotation: bool = False,
    scan_repo: bool = False,
    repo_root: "str | os.PathLike[str]" = ".",
    scan_min_length: int = DEFAULT_SCAN_MIN_LENGTH,
    tracked_files: Optional[Sequence[str]] = None,
    environment_problems: Sequence[Issue] = (),
    require_scan_targets: bool = False,
) -> AuditResult:
    """Exécute les contrôles (présence, robustesse, rotation, anti-fuite).

    `environment_problems` porte ce que l'appelant a mesuré de **son propre
    environnement** avant de bâtir l'audit — registre de rotation illisible
    (`ledger_issues`), plafond de rotation inappliqué (`max_age_issues`).
    `run_audit` ne lit ni fichier ni variable : il reçoit un `ledger` déjà chargé et
    ne peut donc pas voir qu'on lui a passé un registre vide *parce qu'il était
    corrompu*, ni qu'un `max_age_days` calculé n'est pas celui demandé. Ces issues
    viennent de celui qui tenait le chemin ou la variable.

    `require_scan_targets` refuse un scan anti-fuite **sans valeur de référence** :
    un scan qui ne sait pas quoi chercher ne trouve rien par construction, et rien
    ne doit se lire comme « aucune fuite ». C'est le cas d'une machine sans `.env`,
    et c'est cette vacuité-là qu'un gate de CI doit refuser (voir la CI).
    """
    if ledger is None:
        ledger = {"version": LEDGER_VERSION, "secrets": {}}

    issues: List[Issue] = []
    issues.extend(environment_problems)
    issues.extend(check_presence_and_strength(values, specs))
    issues.extend(check_distinctness(values, specs))
    issues.extend(check_key_ring(values, specs))
    rotation_issues, rotation_report = check_rotation(
        values,
        ledger,
        specs=specs,
        max_age_days=max_age_days,
        now=now,
        allow_missing=allow_missing_rotation,
    )
    issues.extend(rotation_issues)

    #: Le périmètre du scan : c'est ce qui permet de lire « aucune fuite » comme
    #: « rien trouvé **dans ce qui a été lu** », et non « tout a été lu ».
    scan_report: Optional[Dict[str, Any]] = None
    if scan_repo:
        if require_scan_targets and not scannable_targets(values, specs, scan_min_length):
            issues.append(
                Issue(
                    SCAN_NAME,
                    ERROR,
                    "le scan anti-fuite n'a aucune valeur de référence : il ne peut "
                    "rien trouver, donc il ne prouve rien. Fournis les valeurs "
                    "(`--env-file`, environnement) ou retire `--require-scan-targets`.",
                )
            )
        outcome = _scan_repo_outcome(
            values,
            root=repo_root,
            specs=specs,
            min_length=scan_min_length,
            tracked_files=tracked_files,
        )
        issues.extend(outcome.issues)
        scan_report = outcome.as_dict()

    checked = [s.name for s in specs if (values.get(s.name) or "").strip()]
    return AuditResult(
        ok=not any(i.is_error for i in issues),
        issues=issues,
        checked=checked,
        rotation=rotation_report,
        scan=scan_report,
    )


# --------------------------------------------------------------------------- #
# Configuration **réellement déployée**
# --------------------------------------------------------------------------- #

#: Hôtes pour lesquels un `http://` n'expose rien : la boucle locale, utilisée
#: par les tests de bout en bout de la barrière.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class DeployedUnreachable(RuntimeError):
    """La configuration déployée n'a pas pu être **lue** — donc rien n'est vérifié.

    Distinct d'une comparaison qui échoue : ici, on ne sait rien de la
    production. La barrière doit refuser (jamais retomber sur « le local suffit »),
    sinon un réseau coupé rendrait un feu vert qu'aucune mesure ne soutient.
    """


def deployed_fingerprints_url(base: str) -> str:
    """URL de l'endpoint d'empreintes, à partir d'une base **ou** de l'URL complète.

    Accepter les deux formes évite la faute la plus banale : coller l'URL du
    service sans le chemin, ou l'URL complète et se retrouver avec le chemin en
    double (`…/secrets/fingerprints/secrets/fingerprints`).
    """
    text = str(base or "").strip().rstrip("/")
    if not text:
        raise DeployedUnreachable("aucune URL de service déployé n'est fournie")
    if text.endswith(DEPLOYED_FINGERPRINTS_PATH):
        return text
    return text + DEPLOYED_FINGERPRINTS_PATH


def redact_url(url: str) -> str:
    """L'URL **sans ses identifiants**, pour tout rapport qui la publie.

    Une URL peut légalement porter `user:motdepasse@` ; un rapport, un `--json`
    et un journal de CI la recopieraient. On ne supprime pas la question (on la
    refuse, voir `remote_url_issues`), mais tout message qui cite une URL la cite
    par cette fonction : un identifiant ne doit jamais sortir par une porte
    latérale, fût-ce pour dire qu'il n'aurait pas fallu le mettre là.
    """
    text = str(url or "")
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError:
        return "<URL illisible>"
    if not (parts.username or parts.password):
        return text
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urllib.parse.urlunsplit(
        (parts.scheme, f"***@{host}", parts.path, parts.query, parts.fragment)
    )


def remote_url_issues(url: str) -> List[Issue]:
    """Ce qu'il y a à dire sur l'URL **avant** toute requête (au plus une issue).

    Une URL illisible ou d'un autre schéma que http(s) est une erreur : on
    n'essaie même pas, le message de la bibliothèque réseau serait plus obscur que
    la faute. Un `http://` public est un avertissement : la clé interne voyage
    alors en clair à chaque vérification (même si l'endpoint, lui, ne rend que des
    empreintes).
    """
    try:
        parts = urllib.parse.urlsplit(str(url or ""))
        host = (parts.hostname or "").lower()
    except ValueError as exc:
        return [Issue(DEPLOYED_NAME, ERROR, f"URL de service déployé illisible ({url}) : {exc}")]
    if parts.scheme not in ("http", "https") or not host:
        return [
            Issue(
                DEPLOYED_NAME,
                ERROR,
                f"URL de service déployé invalide : « {url} » (attendu http(s)://<hôte>, "
                "par exemple https://<service>.onrender.com)",
            )
        ]
    if parts.username or parts.password:
        # Le rapport publie l'URL (et le `--json` la met dans les journaux de la
        # CI) : des identifiants dans l'URL y seraient recopiés. On les refuse
        # sans les répéter — le message ne nomme que l'hôte.
        return [
            Issue(
                DEPLOYED_NAME,
                ERROR,
                f"l'URL de {host} porte des identifiants avant l'hôte : ils "
                "finiraient recopiés dans ce rapport. Utilise l'URL nue et "
                "`INTERNAL_API_KEY` (qui voyage en en-tête, jamais dans l'URL).",
            )
        ]
    if parts.scheme == "http" and host not in _LOOPBACK_HOSTS:
        return [
            Issue(
                DEPLOYED_NAME,
                WARNING,
                f"{url} est en http:// : la clé interne de cette machine y part en "
                "clair à chaque vérification (préfère https://, ou vérifie la "
                "production depuis la machine qui l'héberge).",
            )
        ]
    return []


def fetch_deployed_fingerprints(
    url: str,
    api_key: str,
    *,
    timeout: float = DEFAULT_DEPLOYED_TIMEOUT,
    opener: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """Lit le rapport d'empreintes du service déployé (ou lève `DeployedUnreachable`).

    `opener` est injectable (`urllib.request.urlopen` par défaut) : les tests
    n'ont pas besoin d'un vrai service pour vérifier ce qui part sur le réseau —
    et ce qui n'en part **jamais** (aucune valeur de secret, seulement la clé
    interne en en-tête, et jamais dans l'URL).

    Toute cause d'échec porte son diagnostic : 401/403 (la clé interne de la
    production n'est pas celle-ci), 503 (la production n'a pas d'`INTERNAL_API_KEY`
    — fail-closed côté service), corps illisible ou trop gros (mauvaise URL).
    """
    target = deployed_fingerprints_url(url)
    display = redact_url(target)
    request = urllib.request.Request(
        target,
        headers={"X-API-Key": str(api_key), "Accept": "application/json"},
        method="GET",
    )
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(request, timeout=timeout) as response:
            body = response.read(MAX_DEPLOYED_BODY_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise DeployedUnreachable(
                f"{display} a refusé la clé interne de cette machine (HTTP {exc.code}) : "
                "l'INTERNAL_API_KEY de la production n'est pas celle-ci — c'est déjà "
                "un écart entre les deux environnements."
            ) from exc
        if exc.code == 503:
            raise DeployedUnreachable(
                f"{display} répond {exc.code} : l'endpoint interne est désactivé, "
                "donc la production n'a pas (ou pas encore) d'INTERNAL_API_KEY."
            ) from exc
        raise DeployedUnreachable(
            f"{display} répond HTTP {exc.code} : ce n'est pas le service attendu "
            "(mauvaise URL ?)."
        ) from exc
    except urllib.error.URLError as exc:
        raise DeployedUnreachable(
            f"{display} est injoignable ({exc.reason}) : aucune empreinte distante, "
            "donc rien n'est vérifié de la production."
        ) from exc
    except (TimeoutError, OSError) as exc:
        raise DeployedUnreachable(
            f"{display} n'a pas répondu en {timeout} s ({exc.__class__.__name__}) : "
            "aucune empreinte distante, donc rien n'est vérifié de la production."
        ) from exc

    if len(body) > MAX_DEPLOYED_BODY_BYTES:
        raise DeployedUnreachable(
            f"la réponse de {display} dépasse {MAX_DEPLOYED_BODY_BYTES} octets : "
            "ce n'est pas un rapport d'empreintes (mauvaise URL ?)."
        )
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeployedUnreachable(
            f"la réponse de {display} n'est pas du JSON lisible "
            f"({exc.__class__.__name__}) : ce n'est pas le service attendu."
        ) from exc
    if not isinstance(payload, dict):
        raise DeployedUnreachable(
            f"{display} a rendu un {type(payload).__name__} au lieu d'un objet JSON : "
            "ce n'est pas un rapport d'empreintes."
        )
    return payload


@dataclass
class DeployedComparison:
    """Résultat de la comparaison local ↔ production (noms, jamais de valeurs)."""

    issues: List[Issue] = field(default_factory=list)
    matched: List[str] = field(default_factory=list)
    mismatched: List[str] = field(default_factory=list)
    #: Défini ici, absent du processus déployé.
    missing_in_production: List[str] = field(default_factory=list)
    #: Rapporté par la production, inconnu de cette machine.
    unknown_here: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(issue.is_error for issue in self.issues)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "matched": self.matched,
            "mismatched": self.mismatched,
            "missing_in_production": self.missing_in_production,
            "unknown_here": self.unknown_here,
            "issues": [issue.as_dict() for issue in self.issues],
        }


def compare_with_deployed(
    local: Mapping[str, str],
    deployed: Mapping[str, Any],
    specs: Sequence[SecretSpec] = DEFAULT_SPECS,
) -> DeployedComparison:
    """Compare les empreintes locales à celles du **processus déployé**.

    L'égalité des empreintes vaut égalité des valeurs : c'est tout l'intérêt de
    saler et de tronquer — on compare sans jamais faire circuler un secret. Avant
    toute comparaison, trois refus qui évitent de rendre un écart trompeur : le
    rapport doit venir de ce service (`kind`), dans un schéma que ce client sait
    lire (`version`), et calculé avec le **même sel** — sinon « empreintes
    différentes » ne voudrait rien dire, et l'annoncer comme un écart de valeur
    enverrait chercher une faute au mauvais endroit.
    """
    result = DeployedComparison()
    kind = str(deployed.get("kind") or "")
    if kind != DEPLOYED_REPORT_KIND:
        result.issues.append(
            Issue(
                DEPLOYED_NAME,
                ERROR,
                f"la réponse ne vient pas de ce service (kind={kind or 'absent'}, "
                f"attendu {DEPLOYED_REPORT_KIND}) : mauvaise URL ?",
            )
        )
        return result
    version = deployed.get("version")
    if version != DEPLOYED_REPORT_VERSION:
        result.issues.append(
            Issue(
                DEPLOYED_NAME,
                ERROR,
                f"la production publie le schéma v{version} de ce rapport, cette "
                f"version du dépôt parle v{DEPLOYED_REPORT_VERSION} : le code déployé "
                "et ce dépôt ne sont pas la même révision — comparaison impossible.",
            )
        )
        return result
    salt = str(deployed.get("fingerprint_salt") or "")
    if salt != FINGERPRINT_SALT:
        result.issues.append(
            Issue(
                DEPLOYED_NAME,
                ERROR,
                f"la production calcule ses empreintes avec un autre sel « {salt or 'absent'} » "
                f"(ici « {FINGERPRINT_SALT} ») : le code déployé n'est pas celui-ci, "
                "donc aucune empreinte n'est comparable.",
            )
        )
        return result

    remote = deployed.get("fingerprints")
    if not isinstance(remote, Mapping):
        result.issues.append(
            Issue(
                DEPLOYED_NAME,
                ERROR,
                "le rapport de la production n'expose pas d'empreintes "
                "(« fingerprints ») : rien n'est comparable.",
            )
        )
        return result

    known = {spec.name for spec in specs}
    for spec in specs:
        value = (local.get(spec.name) or "").strip()
        local_fp = secret_fingerprint(spec.name, value) if value else ""
        remote_fp = str(remote.get(spec.name) or "").strip()
        if local_fp and remote_fp:
            if local_fp == remote_fp:
                result.matched.append(spec.name)
                continue
            result.mismatched.append(spec.name)
            result.issues.append(
                Issue(
                    spec.name,
                    ERROR,
                    "la production tourne une valeur DIFFÉRENTE de celle de cette "
                    f"machine (empreinte locale {local_fp}…, empreinte en production "
                    f"{remote_fp}…) : ce n'est pas ce .env qui est en ligne. Pose la "
                    "valeur sur la plateforme (ou corrige-la ici), puis redémarre le "
                    "service.",
                )
            )
        elif local_fp:
            result.missing_in_production.append(spec.name)
            if spec.required:
                result.issues.append(
                    Issue(
                        spec.name,
                        ERROR,
                        "défini ici mais ABSENT du processus déployé : la production "
                        "démarre sans lui (ou refuse de démarrer, fail-closed). Pose-le "
                        "sur la plateforme, puis redémarre.",
                    )
                )
            else:
                result.issues.append(
                    Issue(
                        spec.name,
                        WARNING,
                        "défini ici, absent de la production : le service déployé ne "
                        "l'utilise pas — la fonctionnalité qu'il porte est éteinte en "
                        "ligne.",
                    )
                )
        elif remote_fp:
            if spec.required:
                result.issues.append(
                    Issue(
                        spec.name,
                        ERROR,
                        "la production définit ce secret, cette machine non : rien ici "
                        "ne peut prouver sa robustesse, ni sa fraîcheur au registre. "
                        "Renseigne-le dans .env pour pouvoir l'auditer.",
                    )
                )
            else:
                result.issues.append(
                    Issue(
                        spec.name,
                        WARNING,
                        "la production définit ce secret facultatif, cette machine non : "
                        "il n'est ni audité ni enregistré au registre de rotation ici.",
                    )
                )

    for name in sorted(remote):
        if name not in known:
            result.unknown_here.append(str(name))
            result.issues.append(
                Issue(
                    str(name),
                    WARNING,
                    "la production rapporte un secret que cette machine ne connaît pas : "
                    "le code déployé est plus récent que ce dépôt (ou un secret "
                    "supplémentaire y a été posé à la main).",
                )
            )
    return result


def _no_url_issue(require_remote: bool) -> Issue:
    """L'absence d'URL, dite : un refus si on l'exige, sinon un avertissement explicite.

    L'avertissement est le cœur de la correction : un audit local vert affirmait
    « tous les secrets sont à jour » sans jamais dire que la production, elle,
    n'avait pas été regardée. Être vert n'est pas grave ; l'être en le laissant
    croire en est un.
    """
    if require_remote:
        return Issue(
            DEPLOYED_NAME,
            ERROR,
            "vérification de la production exigée, mais aucune URL de service déployé "
            f"n'est fournie (`--remote <url>` ou ${DEPLOYED_URL_ENV}).",
        )
    return Issue(
        DEPLOYED_NAME,
        WARNING,
        "la production n'a PAS été vérifiée : aucune URL de service déployé "
        f"(`--remote <url>` ou ${DEPLOYED_URL_ENV}). Ce résultat ne décrit que cette "
        "machine — un .env conforme ne prouve rien de ce qui tourne en ligne.",
    )


def apply_deployed_check(
    result: AuditResult,
    local: Mapping[str, str],
    url: str = "",
    *,
    require_remote: bool = False,
    api_key: str = "",
    timeout: float = DEFAULT_DEPLOYED_TIMEOUT,
    opener: Optional[Callable[..., Any]] = None,
    specs: Sequence[SecretSpec] = DEFAULT_SPECS,
) -> AuditResult:
    """Complète un audit **local** par ce que tourne réellement la production.

    Trois cas, et ils ne se confondent pas :

    * une URL est fournie et répond → les empreintes sont comparées une à une ;
    * une URL est fournie mais ne répond pas (ou refuse la clé) → **erreur** :
      on a demandé à mesurer la production et on n'a rien mesuré. Revenir au
      local en silence serait précisément le feu vert menteur qu'on supprime ;
    * aucune URL → **avertissement** explicite (« la production n'a PAS été
      vérifiée »), ou une erreur si `require_remote` est activé — c'est ce que
      demande un vrai pipeline de déploiement.
    """
    target = str(url or "").strip()
    if not target:
        result.issues.append(_no_url_issue(require_remote))
        result.deployed = {"checked": False, "reason": "no_url", "required": require_remote}
        _refresh_ok(result)
        return result

    # L'URL est publiée (rapport, `--json`, journaux de CI) : on ne cite jamais
    # ses éventuels identifiants, y compris dans le constat qui les refuse.
    display = redact_url(target)
    url_issues = remote_url_issues(target)
    result.issues.extend(url_issues)
    if any(issue.is_error for issue in url_issues):
        result.deployed = {"checked": False, "url": display, "reason": "invalid_url"}
        _refresh_ok(result)
        return result

    key = str(api_key or "").strip()
    if not key:
        result.issues.append(
            Issue(
                DEPLOYED_NAME,
                ERROR,
                f"INTERNAL_API_KEY absente ici : impossible de s'authentifier auprès de "
                f"{display}, donc la production n'a pas été vérifiée. Renseigne-la, ou "
                "précise l'URL d'un service dont tu détiens la clé.",
            )
        )
        result.deployed = {"checked": False, "url": display, "reason": "no_api_key"}
        _refresh_ok(result)
        return result

    try:
        payload = fetch_deployed_fingerprints(target, key, timeout=timeout, opener=opener)
    except DeployedUnreachable as exc:
        result.issues.append(Issue(DEPLOYED_NAME, ERROR, str(exc)))
        result.deployed = {
            "checked": False,
            "url": display,
            "reason": "unreachable",
            "error": str(exc),
        }
        _refresh_ok(result)
        return result

    comparison = compare_with_deployed(local, payload, specs=specs)
    result.issues.extend(comparison.issues)
    result.deployed = {
        "checked": True,
        "url": display,
        "matched": comparison.matched,
        "mismatched": comparison.mismatched,
        "missing_in_production": comparison.missing_in_production,
        "unknown_here": comparison.unknown_here,
        "production": {
            "kind": payload.get("kind"),
            "schema": payload.get("version"),
            "checked_at": payload.get("checked_at"),
            "key_ring": payload.get("key_ring"),
        },
    }
    _refresh_ok(result)
    return result


def _refresh_ok(result: AuditResult) -> None:
    """Recalcule le verdict après ajout d'issues (fail-closed, en un seul endroit)."""
    result.ok = not any(issue.is_error for issue in result.issues)


def resolve_ledger_path(
    explicit: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> str:
    env = environ if environ is not None else os.environ
    return explicit or env.get(LEDGER_PATH_ENV) or DEFAULT_LEDGER_PATH


def resolve_max_age_days(
    explicit: Optional[int] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> int:
    """Le plafond de rotation effectif, en jours.

    Une valeur illisible retombe sur `DEFAULT_MAX_AGE_DAYS`, et une valeur non
    positive est ramenée à 1 jour. Ces replis ne sont pas muets pour autant :
    `max_age_issues` les nomme, parce que ce plafond décide si une rotation est
    « en retard » — et un opérateur qui croit avoir réglé 180 jours alors que
    l'audit en applique 90 reçoit un refus qu'il ne comprendra pas.
    """
    if explicit is not None:
        return explicit
    env = environ if environ is not None else os.environ
    raw = env.get(MAX_AGE_ENV)
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:  # sans signal : plafond illisible, le défaut s'applique
            pass
    return DEFAULT_MAX_AGE_DAYS


def max_age_issues(
    environ: Optional[Mapping[str, str]] = None,
) -> List[Issue]:
    """L'issue qui dit que `$SECRET_MAX_AGE_DAYS` n'a **pas** été appliqué tel quel.

    Sans elle, `SECRET_MAX_AGE_DAYS=90j` (unité recopiée par habitude) ou `=0`
    produisait le même verdict qu'une machine sans réglage : l'audit repartait à
    90 jours et ne le disait nulle part. Le plafond de rotation n'est pas un
    détail — c'est lui qui décide du refus —, donc ce qui n'a pas été appliqué
    doit se lire dans le rapport.
    """
    env = environ if environ is not None else os.environ
    raw = (env.get(MAX_AGE_ENV) or "").strip()
    if not raw:
        return []
    try:
        value = int(raw)
    except ValueError:
        return [
            Issue(
                MAX_AGE_ENV,
                ERROR,
                f"valeur illisible ({raw!r}) : un entier de jours positifs est attendu "
                f"(sans unité). L'audit a appliqué {DEFAULT_MAX_AGE_DAYS} jours par "
                "défaut — la durée de vie que tu as voulue ne s'applique pas.",
            )
        ]
    if value < 1:
        return [
            Issue(
                MAX_AGE_ENV,
                ERROR,
                f"valeur hors bornes ({raw!r}) : l'audit a appliqué 1 jour, pas ce qui "
                "est écrit. Une durée de vie de 0 jour n'a pas de sens (elle rendrait "
                "toute rotation immédiatement en retard).",
            )
        ]
    return []


def enforce_secret_rotation(
    values: Optional[Mapping[str, str]] = None,
    ledger_path: Optional[str] = None,
    max_age_days: Optional[int] = None,
    allow_missing: bool = False,
) -> AuditResult:
    """Point d'entrée « démarrage » : lève `RuntimeError` si l'audit échoue."""
    effective = dict(values if values is not None else os.environ)
    path = resolve_ledger_path(ledger_path)
    result = run_audit(
        effective,
        ledger=load_ledger(path),
        max_age_days=resolve_max_age_days(max_age_days),
        allow_missing_rotation=allow_missing,
        environment_problems=[*ledger_issues(path), *max_age_issues()],
    )
    if not result.ok:
        raise RuntimeError(
            "Audit des secrets en échec (fail-closed) :\n- "
            + "\n- ".join(f"{i.name}: {i.message}" for i in result.errors)
        )
    return result


#: D'où vient le plafond de rotation **appliqué** : de la variable
#: d'environnement, ou du défaut du projet. Un opérateur qui croit avoir réglé
#: 180 jours doit pouvoir lire ici que l'audit en applique 90.
MAX_AGE_SOURCE_ENV = "env"
MAX_AGE_SOURCE_DEFAULT = "default"


def effective_settings(
    environ: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Les réglages **effectifs** de l'audit, tels qu'ils seront appliqués.

    Répond, sans lancer l'audit ni lire un seul secret, à trois questions qu'un
    opérateur ne devrait pas avoir à chercher dans un journal :

    * **quel plafond de rotation s'applique vraiment** — la valeur retenue, sa
      provenance, et le reproche éventuel (`max_age_issues`) quand la variable
      écrite n'a pas été appliquée telle quelle ;
    * **dans quel état est le registre** — `ok`, `missing` ou `corrupt`, avec le
      détail de `read_ledger` et le nombre d'entrées qu'il porte ;
    * le **chemin** du registre, pour retrouver le fichier sans le deviner.

    Le **rôle de la clé Supabase** n'est pas ici : il appartient à
    `core.config_runtime`, qui le compose à côté de ces faits — ce module ne lit pas
    la configuration applicative. Aucun secret n'est publié : ni empreinte, ni
    valeur, seulement la politique et l'état du fichier.
    """
    env = environ if environ is not None else os.environ
    problems = max_age_issues(env)
    ledger_path = resolve_ledger_path(environ=env)
    ledger, state, detail = read_ledger(ledger_path)
    secrets = ledger.get("secrets", {}) if isinstance(ledger, dict) else {}
    if not isinstance(secrets, dict):
        secrets = {}
    return {
        "max_age_days": resolve_max_age_days(environ=env),
        "max_age_source": (
            MAX_AGE_SOURCE_ENV if (env.get(MAX_AGE_ENV) or "").strip() else MAX_AGE_SOURCE_DEFAULT
        ),
        "max_age_problem": problems[0].message if problems else None,
        "ledger_path": ledger_path,
        "ledger_state": state,
        "ledger_detail": detail,
        "ledger_entries": len(secrets),
    }


def iter_ledger_secret_names(ledger: Mapping[str, Any]) -> Iterable[str]:
    secrets = ledger.get("secrets", {}) if isinstance(ledger, dict) else {}
    if isinstance(secrets, dict):
        return secrets.keys()
    return ()

__all__ = [
    "AuditResult",
    "DEFAULT_DEPLOYED_TIMEOUT",
    "DEFAULT_LEDGER_PATH",
    "DEFAULT_MAX_AGE_DAYS",
    "DEFAULT_SPECS",
    "DEPLOYED_FINGERPRINTS_PATH",
    "DEPLOYED_NAME",
    "DEPLOYED_REPORT_KIND",
    "DEPLOYED_REPORT_VERSION",
    "DEPLOYED_URL_ENV",
    "DeployedComparison",
    "DeployedUnreachable",
    "ERROR",
    "KeyRingInfo",
    "LEDGER_ISSUE_NAME",
    "LEDGER_PATH_ENV",
    "LEDGER_STATE_CORRUPT",
    "LEDGER_STATE_MISSING",
    "LEDGER_STATE_OK",
    "MAX_AGE_ENV",
    "MAX_DEPLOYED_BODY_BYTES",
    "PLACEHOLDER_PATTERNS",
    "SCAN_NAME",
    "SecretSpec",
    "Issue",
    "WARNING",
    "apply_deployed_check",
    "compare_with_deployed",
    "deployed_fingerprints_url",
    "fetch_deployed_fingerprints",
    "fingerprints_for_values",
    "ROTATION_IMPACT",
    "key_ring_info",
    "redact_url",
    "remote_url_issues",
    "DEFAULT_SCAN_EXCLUDED_DIRS",
    "DEFAULT_SCAN_EXCLUDED_SUFFIXES",
    "DEFAULT_SCAN_MIN_LENGTH",
    "MAX_AGE_SOURCE_DEFAULT",
    "MAX_AGE_SOURCE_ENV",
    "effective_settings",
    "EXCLUSION_DIRECTORY",
    "EXCLUSION_EMPTY",
    "EXCLUSION_ENV",
    "EXCLUSION_SUFFIX",
    "ScanOutcome",
    "scan_repo",
    "ledger_summary",
    "build_ledger_entries",
    "DEFAULT_KEY_VERSION",
    "RING_SECRET_NAME",
    "check_distinctness",
    "check_key_ring",
    "check_presence_and_strength",
    "is_valid_key_entry",
    "parse_key_entry",
    "ring_entries",
    "check_rotation",
    "enforce_secret_rotation",
    "git_tracked_files",
    "is_placeholder",
    "is_valid_fernet_key",
    "git_staged_content",
    "git_staged_files",
    "iter_scan_files",
    "load_effective_env",
    "scannable_targets",
    "scan_staged_for_secrets",
    "load_ledger",
    "read_ledger",
    "ledger_issues",
    "mask_secret",
    "max_age_issues",
    "parse_env_file",
    "scan_repo_for_secrets",
    "scan_text_for_values",
    "resolve_ledger_path",
    "resolve_max_age_days",
    "run_audit",
    "save_ledger",
    "secret_fingerprint",
]
