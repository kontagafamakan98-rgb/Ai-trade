#!/usr/bin/env python
"""Génère les secrets **locaux**, les écrit dans `.env`, et ne les affiche jamais.

Une valeur de secret ne vit qu'à deux endroits : le `.env` local (`.gitignore`) et
les variables de la plateforme de déploiement. La remplir à la main la fait passer
par un terminal, donc rester dans l'historique du shell, le scrollback ou le
presse-papier d'une session partagée (voir `docs/SECRETS.md`, « Générer »). Ce
script supprime ce trajet : les valeurs vont du générateur au fichier sans jamais
traverser un écran — seuls le **nom** et une **empreinte** non réversible sortent.

Trois modes :

* **par défaut** — remplit les secrets que cette machine sait créer et qui
  **manquent** : absents, vides, ou laissés à une valeur d'exemple (`change-me…`) ;
* **``--check``** — n'écrit rien et dit **seulement ce qui manque** (codes 0/1) ;
* **``--rotate NOM``** — rotation **assistée** d'un secret explicitement désigné :
  ce que cette rotation casse est affiché *avant*, la valeur neuve est écrite dans
  le `.env` sans jamais passer par l'écran, et l'empreinte est **horodatée** dans
  le registre. Sans `--apply`, rien n'est écrit (le mode annonce seulement) ; sans
  nom, il liste ce qu'une rotation de chaque secret casse. Pour
  `ENCRYPTION_KEY`, l'ancienne clé est recopiée en tête de
  `ENCRYPTION_KEYS_PREVIOUS` et la neuve prend la version au-dessus : c'est ce qui
  rend la rotation **réversible** jusqu'à ce que le chiffré ait été réécrit.

Deux des cinq secrets requis ne se génèrent pas ici : `SUPABASE_SERVICE_KEY` vient
du tableau de bord Supabase, `TELEGRAM_BOT_TOKEN` de BotFather. Le script les
**nomme avec leur source** plutôt que de les inventer : un jeton fabriqué ferait
échouer le démarrage à la première requête, très loin de la ligne fautive.

Ce que le script ne fait **pas**, volontairement :

* dans le mode par défaut, il ne **remplace jamais** une valeur existante qui n'est
  pas un exemple : un secret faible est signalé, jamais écrasé. Le remplacer serait
  une rotation, avec ce qu'elle coûte (`docs/SECRETS.md`, « Ce que chaque rotation
  coûte ») — et une rotation se demande, elle ne se subit pas : c'est `--rotate`,
  et lui seul, qui écrit une valeur neuve là où il y en avait déjà une ;
* il n'invente **jamais** un secret qu'il ne sait pas générer : `--rotate` refuse
  `SUPABASE_SERVICE_KEY` et `TELEGRAM_BOT_TOKEN` en nommant leur source (un jeton
  fabriqué ferait échouer le démarrage très loin de la ligne fautive) ;
* il ne touche pas au **chiffré** : tourner `ENCRYPTION_KEY` prépare l'anneau, mais
  réécrire les identifiants broker se fait avec
  `python scripts/rotate_encryption_key.py --apply`, qui sait lire la base ;
* il ne contrôle ni la robustesse complète, ni la rotation, ni les fuites : c'est
  le rôle de `python scripts/verify_secrets.py` ;
* il ignore les clés **optionnelles** (Gemini, Groq, Finnhub, Alpaca) : leur
  absence est un choix d'installation, pas un manque, et leur rotation vient de
  leur fournisseur, pas d'ici.

Codes de sortie : ``0`` tous les secrets requis sont utilisables ; ``1`` il en
reste à générer, à renseigner, ou à réparer ; ``2`` mauvaise utilisation.

Exemples
--------

::

    python scripts/generate_secrets.py            # remplit ce qui manque dans .env
    python scripts/generate_secrets.py --check    # ne dit que ce qui manque
    python scripts/generate_secrets.py --json     # même chose, pour un script

    # rotation assistée : annonce (aucune écriture), puis exécution
    python scripts/generate_secrets.py --rotate WEBHOOK_SECRET
    python scripts/generate_secrets.py --rotate WEBHOOK_SECRET --apply
    python scripts/generate_secrets.py --rotate          # ce que chaque rotation casse

    # horodater la valeur déjà en place d'un secret venu d'ailleurs (sans y toucher)
    python scripts/generate_secrets.py --record-only SUPABASE_SERVICE_KEY
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import secrets as stdlib_secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core import console  # noqa: E402
from core.secrets_audit import (  # noqa: E402
    DEFAULT_KEY_VERSION,
    DEFAULT_LEDGER_PATH,
    DEFAULT_SPECS,
    RING_SECRET_NAME,
    ROTATION_IMPACT,
    SecretSpec,
    build_ledger_entries,
    check_presence_and_strength,
    is_placeholder,
    is_valid_fernet_key,
    load_ledger,
    parse_env_file,
    parse_key_entry,
    resolve_ledger_path,
    ring_entries,
    save_ledger,
    secret_fingerprint,
)

OK_MARK = "\u2705"
KO_MARK = "\u274c"
WARN_MARK = "\u26a0\ufe0f"

#: Le titre du bloc ajouté en fin de `.env` quand le fichier ne portait pas encore
#: la ligne — jamais un remplacement de ce qui était écrit.
GENERATED_HEADER = "# --- Secrets générés localement par scripts/generate_secrets.py ---"

#: Les fichiers d'environnement d'exemple : suivis par le dépôt, donc jamais une
#: cible d'écriture. Remplir `.env.example` publierait le secret au prochain commit.
TEMPLATE_SUFFIXES = (".example", ".sample", ".template")


def generate_token() -> str:
    """Un secret opaque de 256 bits — `secrets`, pas `random` (pas de graine prédictible)."""
    return stdlib_secrets.token_urlsafe(32)


def generate_fernet_key(version: int = DEFAULT_KEY_VERSION) -> str:
    """Une clé Fernet : 32 octets aléatoires en base64 urlsafe (44 caractères).

    Écrite sans importer `cryptography` — c'est exactement ce que produit
    `Fernet.generate_key()`, mais l'audit et ce script restent sur la seule
    bibliothèque standard, et tournent donc avant toute installation.

    `version` supérieure à 1 donne une clé **préfixée** (`v2:<clé>`), celle qu'on
    écrit en tournant la clé active — voir `_next_free_version`.
    """
    key = base64.urlsafe_b64encode(stdlib_secrets.token_bytes(32)).decode("ascii")
    if not is_valid_fernet_key(key):  # pragma: no cover - invariant stdlib
        raise RuntimeError(
            "clé Fernet invalide générée — arrêt plutôt qu'un .env inutilisable"
        )
    return key if version == DEFAULT_KEY_VERSION else f"v{version}:{key}"


def _next_free_version(values: Mapping[str, str]) -> int:
    """La version à donner à une clé de chiffrement neuve.

    Sans anneau : `v1`, donc une clé nue — la forme historique, celle que tous les
    `.env` d'avant portent. Avec un anneau : la clé active qu'on vient de perdre
    (ou de vouloir remplacer) est normalement déjà dedans, donc une clé **nue**
    entrerait en collision de version avec elle. L'audit refuserait ce cas — mais
    autant ne pas le fabriquer : on prend la version au-dessus de l'anneau.

    Une entrée d'anneau illisible est ignorée ici : `check_key_ring` a la charge
    de la refuser, et deviner sa version serait pire que de l'ignorer.
    """
    versions = []
    for entry in ring_entries(values.get(RING_SECRET_NAME) or ""):
        try:
            version, _key = parse_key_entry(entry)
        except ValueError:
            continue
        versions.append(version)
    return max(versions, default=0) + 1


def _generate_value(name: str, generator: Callable[..., str], values: Mapping[str, str]) -> str:
    """La valeur d'un secret à générer — la clé de chiffrement suit l'anneau."""
    if name != "ENCRYPTION_KEY":
        return generator()
    return generator(_next_free_version(values))


#: Ce que cette machine sait créer, et l'usage de chaque valeur. `ENCRYPTION_KEY`
#: accepte une version (`vN:`) — les autres non, et c'est `_generate_value` qui
#: fait la différence, en un seul endroit.
GENERATORS: Dict[str, Callable[..., str]] = {
    "WEBHOOK_SECRET": generate_token,
    "INTERNAL_API_KEY": generate_token,
    "ENCRYPTION_KEY": generate_fernet_key,
}

#: Ce qui ne se génère **pas** ici : d'où la valeur vient, et donc d'où l'humain
#: doit la rapporter. Nommer la source fait gagner le seul temps qui compte.
MANUAL_SOURCES: Dict[str, str] = {
    "SUPABASE_SERVICE_KEY": (
        "tableau de bord Supabase → Project Settings → API → clé « service_role » "
        "(jamais l'anon)"
    ),
    "TELEGRAM_BOT_TOKEN": "BotFather → /mybots → API Token (forme « <id>:<secret> »)",
    # L'anneau n'est pas un secret qu'on génère : il se modifie en tournant
    # `ENCRYPTION_KEY` (la clé sortante y entre), ou à la main pour en retirer une.
    RING_SECRET_NAME: (
        "l'anneau lui-même : il reçoit la clé sortante quand tu tournes "
        "`ENCRYPTION_KEY`, et se retire à la main une fois le chiffré réécrit"
    ),
}


class UsageError(Exception):
    """Erreur d'invocation (code de sortie 2)."""


@dataclass(frozen=True)
class Plan:
    """Ce que le script fera (ou ferait, en `--check`) — **sans aucune valeur affichable**.

    `values` est le seul porteur des valeurs générées, et il n'est **jamais**
    imprimé : le rapport ne lit que les noms, et une empreinte calculée à partir
    de ces valeurs. En `--check`, `values` reste vide — on ne fabrique pas un
    secret pour ne pas l'écrire.
    """

    env_file: Path
    to_generate: List[str] = field(default_factory=list)
    values: Dict[str, str] = field(default_factory=dict)
    manual: Dict[str, str] = field(default_factory=dict)
    suspect: Dict[str, str] = field(default_factory=dict)
    already_set: List[str] = field(default_factory=list)

    @property
    def missing(self) -> List[str]:
        """Les secrets requis qui n'existent pas encore — générables ou non."""
        return [*self.to_generate, *self.manual]

    @property
    def ok(self) -> bool:
        """Rien à générer, rien à rapporter, rien de cassé à signaler."""
        return not self.missing and not self.suspect

    def as_dict(self, written: Sequence[str] = ()) -> Dict[str, object]:
        """Forme machine : des noms, des sources et des empreintes — jamais une valeur.

        `written` retire de `to_generate` ce qui vient d'être généré : un appelant qui
        lit ce JSON après un passage ne doit pas croire qu'il reste du travail.
        """
        return {
            "env_file": str(self.env_file),
            "generated": [
                {"name": name, "fingerprint": secret_fingerprint(name, self.values[name])}
                for name in written
            ],
            "to_generate": [name for name in self.to_generate if name not in written],
            "missing_manual": dict(self.manual),
            "unusable": dict(self.suspect),
            "already_set": list(self.already_set),
            "ok": self.ok,
        }


# --------------------------------------------------------------------------- #
# Lecture du fichier d'environnement
# --------------------------------------------------------------------------- #


def _read_env(env_file: Path) -> Tuple[List[str], str]:
    """Les lignes du `.env` et le style de fin de ligne à restituer (`\\n` ou `\\r\\n`).

    Lu en **octets** : en mode texte, Python traduirait déjà les fins de ligne et
    l'information serait perdue. Réécrire tout un `.env` en LF sans le dire
    changerait le fichier pour rien ; on lui rend ses propres fins de ligne.
    """
    if not env_file.is_file():
        return [], "\n"
    text = env_file.read_bytes().decode("utf-8")
    newline = "\r\n" if "\r\n" in text else "\n"
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.split("\n"), newline


def _assignment_key(line: str) -> Optional[str]:
    """Le nom de variable d'une ligne `KEY=…`, ou `None` si ce n'en est pas une.

    Une ligne commentée n'est **jamais** un emplacement : `# WEBHOOK_SECRET=…` est
    du commentaire, et le remplacer transformerait une explication en secret.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or stripped.startswith("["):
        return None
    if stripped.startswith("export "):
        stripped = stripped[len("export ") :].lstrip()
    key, sep, _value = stripped.partition("=")
    if not sep:
        return None
    return key.strip() or None


def _duplicated(lines: Sequence[str], names: Sequence[str]) -> List[str]:
    """Les noms que le fichier définit **plusieurs fois** parmi ceux qu'on vise.

    Le dernier `KEY=` gagne à la lecture : si le doublon subsiste, l'audit verrait
    l'ancienne valeur pendant que le script croit avoir écrit la neuve — un succès
    qui ne décrit pas le fichier. On refuse plutôt que de deviner.
    """
    seen: Dict[str, int] = {}
    targets = set(names)
    for line in lines:
        key = _assignment_key(line)
        if key in targets:
            seen[key] = seen.get(key, 0) + 1
    return [name for name in names if seen.get(name, 0) > 1]


def _assignment_line(name: str, value: str) -> str:
    """`KEY="valeur"` — entre guillemets, comme `.env.example` (protège `#` et espaces)."""
    return f'{name}="{value}"'


def _replace_or_append(
    lines: Sequence[str], values: Mapping[str, str]
) -> Tuple[List[str], List[str]]:
    """Remplace les lignes visées **sur place**, et ajoute les autres à la fin.

    Tout ce qui n'est pas une ligne visée est rendu tel quel : un `.env` porte des
    commentaires qui disent où trouver les valeurs, et des réglages qu'on ne sait
    pas régénérer. Réécrire le fichier « proprement » les perdrait.
    """
    remaining = dict(values)
    out: List[str] = []
    written: List[str] = []
    for line in lines:
        name = _assignment_key(line)
        if name is not None and name in remaining:
            out.append(_assignment_line(name, remaining.pop(name)))
            written.append(name)
        else:
            out.append(line)
    if remaining:
        while out and not out[-1].strip():
            out.pop()
        if out:
            out.append("")
        out.append(GENERATED_HEADER)
        for name, value in remaining.items():
            out.append(_assignment_line(name, value))
            written.append(name)
    return out, written


def _write_env(env_file: Path, lines: Sequence[str], newline: str) -> None:
    """Écrit le fichier **d'un bloc**, via un fichier temporaire remplacé.

    Une interruption au milieu d'une écriture directe laisserait un `.env` tronqué
    — donc, pour `ENCRYPTION_KEY`, une valeur perdue que personne ne peut
    reconstruire. `os.replace` sur le même répertoire ne laisse jamais voir cet
    état intermédiaire.
    """
    payload = "\n".join(lines) + "\n"
    if newline == "\r\n":
        payload = payload.replace("\n", "\r\n")
    env_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = env_file.with_name(f"{env_file.name}.tmp")
    try:
        tmp.write_bytes(payload.encode("utf-8"))
        os.replace(tmp, env_file)
    finally:
        if tmp.exists():
            tmp.unlink()
    try:
        os.chmod(env_file, 0o600)
    except OSError:
        # Windows ne connaît pas ces permissions : leur absence n'est pas une erreur.
        pass


# --------------------------------------------------------------------------- #
# Le plan
# --------------------------------------------------------------------------- #


def _audit_problem(spec: SecretSpec, value: str) -> Optional[str]:
    """Le défaut qu'un contrôle **public** voit dans une valeur existante, ou `None`.

    On passe par `check_presence_and_strength` au lieu de recopier des règles de
    longueur : le message affiché ici est alors *exactement* celui de la barrière
    pre-deploy, et il ne peut pas diverger d'elle. Les autres secrets requis, absents
    de cette entrée minimale, sont écartés — ils ne parlent pas de cette valeur.
    """
    issues = check_presence_and_strength({spec.name: value})
    mine = [issue for issue in issues if issue.name == spec.name]
    return mine[0].message if mine else None


def _manual_reason(name: str, values: Mapping[str, str], value: str) -> str:
    """Pourquoi un secret non générable ici manque.

    Une ligne `KEY=""` et une ligne absente demandent le même geste, mais pas la
    même recherche : le dire évite de relire un fichier qu'on connaît déjà.
    """
    if value:
        return "valeur d'exemple à remplacer"
    return "vide dans le fichier" if name in values else "absent du fichier"


def build_plan(
    values: Mapping[str, str],
    env_file: Path,
    *,
    generate: bool = True,
    specs: Sequence[SecretSpec] = DEFAULT_SPECS,
) -> Plan:
    """Décide, pour chaque secret **requis**, entre générer / rapporter / laisser.

    L'ordre des cas compte : un exemple est du manquant déguisé (on le remplace),
    alors qu'une valeur *faible mais réelle* ne se devine pas — on la signale et on
    n'y touche pas.
    """
    to_generate: List[str] = []
    produced: Dict[str, str] = {}
    manual: Dict[str, str] = {}
    suspect: Dict[str, str] = {}
    already_set: List[str] = []

    for spec in specs:
        if not spec.required:
            continue
        value = (values.get(spec.name) or "").strip()
        generator = GENERATORS.get(spec.name)
        if not value or is_placeholder(value, spec):
            if generator is None:
                manual[spec.name] = _manual_reason(spec.name, values, value)
                continue
            to_generate.append(spec.name)
            if generate:
                produced[spec.name] = _generate_value(spec.name, generator, values)
            continue
        problem = _audit_problem(spec, value)
        if problem:
            suspect[spec.name] = problem
        else:
            already_set.append(spec.name)

    return Plan(
        env_file=env_file,
        to_generate=to_generate,
        values=produced,
        manual=manual,
        suspect=suspect,
        already_set=already_set,
    )


def apply_plan(plan: Plan) -> List[str]:
    """Écrit les valeurs générées dans le `.env` et rend les noms réellement écrits."""
    if not plan.values:
        return []
    lines, newline = _read_env(plan.env_file)
    duplicated = _duplicated(lines, plan.values)
    if duplicated:
        raise UsageError(
            f"{plan.env_file} définit {', '.join(duplicated)} plusieurs fois : "
            "impossible de savoir laquelle des lignes fait foi (c'est la dernière "
            "qui gagne). Supprime le doublon à la main, puis relance."
        )
    rendered, written = _replace_or_append(lines, plan.values)
    _write_env(plan.env_file, rendered, newline)
    return written


# --------------------------------------------------------------------------- #
# Rapports — des noms, des sources, des empreintes. Jamais une valeur.
# --------------------------------------------------------------------------- #


def _fingerprint_line(name: str, value: str) -> str:
    """Une empreinte salée (16 hex) : elle prouve l'égalité, jamais la valeur."""
    return f"   • {name} — empreinte {secret_fingerprint(name, value)}"


def _manual_lines(manual: Mapping[str, str]) -> List[str]:
    return [
        f"   • {name} — {MANUAL_SOURCES.get(name, 'source externe')}  ({reason})"
        for name, reason in manual.items()
    ]


def check_report(plan: Plan) -> List[str]:
    """Le rapport de `--check` : la liste des manquants, rien d'autre."""
    spec_count = sum(1 for spec in DEFAULT_SPECS if spec.required)
    lines: List[str] = []
    if plan.missing:
        lines.append(
            f"{KO_MARK} {len(plan.missing)}/{spec_count} secret(s) requis manquant(s) "
            f"dans {plan.env_file} :"
        )
        for name in plan.to_generate:
            lines.append(f"   • {name} — à générer ici (python scripts/generate_secrets.py)")
        lines.extend(_manual_lines(plan.manual))
    else:
        lines.append(
            f"{OK_MARK} Aucun des {spec_count} secrets requis ne manque dans {plan.env_file}."
        )
    if plan.suspect:
        # Pas « manquant », mais bloquant : une clé Fernet invalide ne déchiffre
        # rien, et le script refuse justement de l'écraser en silence.
        lines.append(
            f"{WARN_MARK} En plus des manquants : {len(plan.suspect)} secret(s) présent(s) "
            "mais inutilisable(s), laissé(s) intact(s) :"
        )
        for name, problem in plan.suspect.items():
            lines.append(f"   • {name} — {problem}")
    lines.append(
        "(Ce contrôle ne dit que la présence. Robustesse, rotation et fuites : "
        "python scripts/verify_secrets.py.)"
    )
    return lines


def write_report(plan: Plan, written: Sequence[str]) -> List[str]:
    """Le rapport du mode par défaut : ce qui a été écrit, et ce qui reste."""
    lines = [f"Fichier d'environnement : {plan.env_file}"]
    if written:
        lines.append(
            f"{OK_MARK} {len(written)} secret(s) généré(s) et écrit(s) — "
            "la valeur n'est jamais passée par l'écran :"
        )
        lines.extend(_fingerprint_line(name, plan.values[name]) for name in written)
        if "ENCRYPTION_KEY" in written:
            # La version est **dans** la valeur écrite, donc jamais affichée : on
            # la relit pour la dire, sinon l'opérateur ne saurait pas où il en est
            # de son anneau.
            version = parse_key_entry(plan.values["ENCRYPTION_KEY"])[0]
            lines.append(
                f"{WARN_MARK} ENCRYPTION_KEY active : v{version}. Si une production "
                "chiffre déjà des identifiants broker avec une autre clé, remets-la "
                "dans ENCRYPTION_KEYS_PREVIOUS au lieu de la perdre "
                "(docs/SECRETS.md, « ENCRYPTION_KEY »)."
            )
    else:
        lines.append(
            f"{OK_MARK} Rien à générer — les secrets que cette machine sait créer "
            "sont déjà en place."
        )
    if plan.already_set:
        lines.append(
            f"{OK_MARK} Déjà en place et inchangé(s) : {', '.join(plan.already_set)}"
        )
    if plan.suspect:
        lines.append(
            f"{KO_MARK} {len(plan.suspect)} secret(s) présent(s) mais inutilisable(s) — "
            "laissé(s) intact(s), à réparer à la main :"
        )
        for name, problem in plan.suspect.items():
            lines.append(f"   • {name} — {problem}")
    if plan.manual:
        lines.append(
            f"{KO_MARK} {len(plan.manual)} secret(s) requis manquant(s) — "
            "à renseigner à la main :"
        )
        lines.extend(_manual_lines(plan.manual))
    if not plan.ok:
        lines.append("")
        lines.append("Prochaine étape, depuis la racine du projet :")
        lines.append(
            "   python scripts/verify_secrets.py --record   # horodate les empreintes"
        )
        lines.append("   python scripts/verify_secrets.py            # doit sortir 0")
    return lines


# --------------------------------------------------------------------------- #
# Rotation assistée — un secret désigné, ses conséquences, puis l'horodatage
# --------------------------------------------------------------------------- #


class RotationRefused(Exception):
    """La rotation demandée ne se fait **pas** ici (secret externe, anneau incohérent…).

    Lever plutôt que deviner : une valeur inventée pour `SUPABASE_SERVICE_KEY`, ou
    un anneau recollé de travers, ne se verraient qu'au premier appel authentifié —
    c'est-à-dire le plus loin possible de la ligne fautive.
    """

    def __init__(self, name: str, reason: str):
        super().__init__(reason)
        self.name = name
        self.reason = reason


@dataclass(frozen=True)
class RotationPlan:
    """Ce qu'une rotation annonce, puis écrit : des noms et des empreintes, jamais une valeur.

    En mode annonce (`generate=False`), `values` reste **vide** : on ne fabrique pas
    un secret pour ne pas l'écrire, et surtout on n'affiche pas l'empreinte d'une
    valeur jetée — elle ne serait pas celle qui finirait dans le `.env` au tour
    suivant, et l'opérateur la retrouverait introuvable dans le registre.
    """

    name: str
    env_file: Path
    values: Dict[str, str] = field(default_factory=dict)
    previous_fingerprint: str = ""
    impact: List[str] = field(default_factory=list)
    next_steps: List[str] = field(default_factory=list)
    version: Optional[int] = None
    #: La version de la clé **sortante** (celle qui va entrer dans l'anneau) : c'est
    #: elle qui dit ce qui rouvre encore l'ancien chiffré, et donc ce qu'on n'a pas le
    #: droit de retirer avant d'avoir tout réécrit.
    previous_version: Optional[int] = None
    ring_versions: List[int] = field(default_factory=list)
    initial_setup: bool = False

    @property
    def new_fingerprint(self) -> str:
        """L'empreinte de la valeur qui vient d'être produite — vide en mode annonce."""
        value = self.values.get(self.name)
        return secret_fingerprint(self.name, value) if value else ""

    @property
    def targets(self) -> List[str]:
        """Les noms réellement touchés : le secret, et l'anneau quand il suit."""
        return sorted(self.values)

    def as_dict(self, *, applied: bool, recorded: Sequence[str], ledger: str) -> Dict[str, object]:
        """Forme machine : noms, empreintes, versions — jamais une valeur."""
        return {
            "env_file": str(self.env_file),
            "rotation": {
                "name": self.name,
                "applied": applied,
                "recorded": list(recorded),
                "ledger": ledger,
                "previous_fingerprint": self.previous_fingerprint or None,
                "fingerprint": self.new_fingerprint or None,
                "version": self.version,
                "previous_version": self.previous_version,
                "ring_versions": self.ring_versions,
                "initial_setup": self.initial_setup,
                "impact": self.impact,
                "next": self.next_steps,
            },
        }


def _spec_by_name(name: str, specs: Sequence[SecretSpec] = DEFAULT_SPECS) -> SecretSpec:
    """Le secret nommé, ou une erreur d'invocation qui **nomme ce qui existe**."""
    for spec in specs:
        if spec.name == name:
            return spec
    known = ", ".join(spec.name for spec in specs)
    raise UsageError(f"secret inconnu : « {name} ». Connus : {known}.")


def _ring_versions(values: Mapping[str, str]) -> List[int]:
    """Les versions déjà prises par l'anneau — une entrée illisible est ignorée ici.

    C'est `check_key_ring` qui a la charge de la refuser ; deviner sa version serait
    pire que de l'ignorer, et la mentionner comme prise empêcherait une rotation
    légitime de choisir un numéro libre.
    """
    versions: List[int] = []
    for entry in ring_entries(values.get(RING_SECRET_NAME) or ""):
        try:
            version, _key = parse_key_entry(entry)
        except ValueError:
            continue
        versions.append(version)
    return versions


def _active_key_versions(current: str) -> Tuple[int, str]:
    """`(version, clé)` de la clé active — ou `ValueError` si elle est inutilisable."""
    version, key = parse_key_entry(current)
    if not is_valid_fernet_key(key):
        raise ValueError(
            "la clé active n'est pas une clé Fernet valide (attendu : base64 urlsafe "
            "de 32 octets)"
        )
    return version, key


def _next_steps(name: str, recorded: bool) -> List[str]:
    """Ce qui reste à faire **après** l'écriture dans le `.env`.

    L'ordre est celui de `docs/SECRETS.md` : le `.env` et la plateforme avancent
    dans la même fenêtre, et c'est le contrôle distant qui **prouve** que le
    redémarrage a eu lieu — pas l'espoir qu'il a eu lieu.
    """
    steps = [
        "Poser la MÊME valeur sur la plateforme de déploiement (tableau de bord, "
        "jamais en ligne de commande), puis redémarrer le service.",
        "Prouver que la production tourne la nouvelle valeur : commande à lancer "
        "ici — python scripts/verify_secrets.py --remote <url> --require-remote "
        "(doit sortir 0).",
    ]
    if name == "ENCRYPTION_KEY":
        steps.append(
            "Réécrire le chiffré encore lu par la clé sortante : commande à lancer "
            "ici — python scripts/rotate_encryption_key.py (compte, lecture seule), "
            "puis --apply. Quand il ne reste rien, elle peut quitter l'anneau."
        )
    if not recorded:
        steps.append(
            "Horodater la rotation : python scripts/verify_secrets.py --record — la "
            "barrière refuse tant que l'empreinte du registre n'a pas suivi."
        )
    return steps


def build_rotation(
    name: str,
    values: Mapping[str, str],
    env_file: Path,
    *,
    generate: bool = True,
    recorded: bool = True,
    specs: Sequence[SecretSpec] = DEFAULT_SPECS,
) -> RotationPlan:
    """Prépare la rotation d'un secret **désigné** : version, anneau, conséquences.

    Seuls les trois secrets que cette machine sait créer se tournent ici. Les deux
    qui viennent d'un service externe sont refusés en nommant leur source ; l'anneau
    se refuse aussi, puisqu'il **suit** `ENCRYPTION_KEY` au lieu de se générer.
    """
    _spec_by_name(name, specs)
    if name == RING_SECRET_NAME:
        raise RotationRefused(
            name,
            "l'anneau ne se tourne pas : il reçoit la clé sortante quand tu tournes "
            "ENCRYPTION_KEY (--rotate ENCRYPTION_KEY --apply), et tu en retires une "
            "entrée à la main quand le chiffré a été réécrit.",
        )
    generator = GENERATORS.get(name)
    if generator is None:
        source = MANUAL_SOURCES.get(name, "un service externe")
        raise RotationRefused(
            name,
            f"« {name} » ne se génère pas ici : la valeur vient de {source}. "
            "Inventer une valeur ferait échouer la première requête authentifiée, "
            "très loin de la ligne fautive.",
        )

    current = (values.get(name) or "").strip()
    impact = [
        ROTATION_IMPACT.get(
            name,
            "aucune conséquence documentée pour ce secret — vérifie qu'il est bien "
            "décrit dans ROTATION_IMPACT avant de tourner quoi que ce soit.",
        )
    ]
    produced: Dict[str, str] = {}
    version: Optional[int] = None
    previous_version: Optional[int] = None
    ring_versions: List[int] = []
    initial_setup = not current

    if name == "ENCRYPTION_KEY":
        ring_versions = _ring_versions(values)
        if not current:
            # Rien à préserver : c'est une mise en place, et l'anneau reste vide.
            version = DEFAULT_KEY_VERSION
            if generate:
                produced[name] = generate_fernet_key(version)
            impact.append(
                "aucune clé active aujourd'hui : c'est une mise en place (v1) et non "
                "une rotation — l'anneau n'est pas touché."
            )
        else:
            try:
                current_version, current_key = _active_key_versions(current)
            except ValueError as exc:
                raise RotationRefused(
                    name,
                    f"rotation refusée : {exc}. Une clé illisible ne peut pas être "
                    f"recopiée dans {RING_SECRET_NAME} (l'audit refuserait l'entrée) — "
                    "corrige-la d'abord.",
                ) from exc
            if current_version in ring_versions:
                raise RotationRefused(
                    name,
                    f"rotation refusée : la version v{current_version} de la clé active "
                    f"est déjà dans {RING_SECRET_NAME} "
                    f"(entrée {ring_versions.index(current_version) + 1}) : deux clés "
                    "pour une version rendraient l'ouverture indécidable. Corrige "
                    "l'anneau avant de tourner la clé.",
                )
            previous_version = current_version
            version = max([current_version, *ring_versions]) + 1
            older = ring_entries(values.get(RING_SECRET_NAME) or "")
            ring_value = ", ".join([f"v{current_version}:{current_key}", *older])
            if generate:
                produced[name] = generate_fernet_key(version)
                produced[RING_SECRET_NAME] = ring_value
            impact.append(
                f"clé active actuelle : v{current_version} ; la nouvelle prend v{version}, "
                f"et v{current_version} est recopiée en tête de {RING_SECRET_NAME} (elle "
                "y rouvre le chiffré existant)."
            )
            if older:
                impact.append(
                    f"l'anneau garde {len(older)} entrée(s) plus ancienne(s) : la clé "
                    f"v{version} ne rouvre rien de ce qu'elles ont chiffré."
                )
    else:
        if generate:
            produced[name] = generator()
        if current:
            impact.append(
                "la valeur actuelle (empreinte "
                f"{secret_fingerprint(name, current)}) sera remplacée : elle ne sera "
                "plus dans ce .env, et rien ne la reconstruit."
            )
        else:
            impact.append(
                "aucune valeur aujourd'hui : c'est une mise en place, pas une rotation."
            )

    return RotationPlan(
        name=name,
        env_file=env_file,
        values=produced,
        previous_fingerprint=secret_fingerprint(name, current) if current else "",
        impact=impact,
        next_steps=_next_steps(name, recorded),
        version=version,
        previous_version=previous_version,
        ring_versions=ring_versions,
        initial_setup=initial_setup,
    )


def apply_rotation(plan: RotationPlan) -> List[str]:
    """Écrit les valeurs de la rotation dans le `.env` et rend les noms réellement écrits.

    Même chemin que le remplissage (`_replace_or_append` + écriture par fichier
    temporaire) : les commentaires, les réglages et l'ordre du fichier sont rendus
    tels quels, et un `.env` tronqué reste impossible.
    """
    if not plan.values:
        return []
    lines, newline = _read_env(plan.env_file)
    duplicated = _duplicated(lines, plan.values)
    if duplicated:
        raise UsageError(
            f"{plan.env_file} définit {', '.join(duplicated)} plusieurs fois : "
            "impossible de savoir laquelle des lignes fait foi (c'est la dernière "
            "qui gagne). Supprime le doublon à la main, puis relance."
        )
    rendered, written = _replace_or_append(lines, plan.values)
    _write_env(plan.env_file, rendered, newline)
    return written


def record_entries(ledger_path: Path, values: Mapping[str, str]) -> List[str]:
    """Horodate **les seuls noms fournis** dans le registre, et rend leurs noms.

    Horodater seulement ce qu'on vient de tourner n'est pas un détail :
    `verify_secrets.py --record` enregistre *tous* les secrets définis, donc fait
    repartir leur compte à 90 jours — en tourner un seul ne doit pas repousser
    l'échéance des autres.
    """
    entries = build_ledger_entries(values)
    if not entries:
        raise UsageError(
            "rien à horodater : aucune des valeurs visées n'est définie (ni .env, ni "
            "environnement)."
        )
    ledger = load_ledger(ledger_path)
    ledger["secrets"] = {**ledger.get("secrets", {}), **entries}
    save_ledger(ledger_path, ledger)
    return sorted(entries)


# --------------------------------------------------------------------------- #
# Rapports de rotation — des noms, des empreintes, des conséquences. Jamais une valeur.
# --------------------------------------------------------------------------- #


def rotation_catalog(values: Mapping[str, str]) -> List[str]:
    """Ce qu'une rotation de **chaque** secret casse, et d'où vient sa valeur neuve."""
    lines = [
        "Rotation assistée — ce que chaque rotation casse (aucune écriture ici) :",
        "",
    ]
    for spec in DEFAULT_SPECS:
        value = (values.get(spec.name) or "").strip()
        # « vide dans le fichier » et « absent du fichier » demandent le même geste,
        # mais pas la même recherche : c'est `_manual_reason` qui en décide, pour que
        # ce catalogue dise exactement ce que dit `--check`.
        state = (
            f"empreinte actuelle : {secret_fingerprint(spec.name, value)}"
            if value
            else _manual_reason(spec.name, values, value)
        )
        generatable = spec.name in GENERATORS
        origin = (
            "générée ici"
            if generatable
            else f"à obtenir : {MANUAL_SOURCES.get(spec.name, 'service externe')}"
        )
        lines.append(f"   • {spec.name} — {origin} — {state}")
        lines.append(f"     {ROTATION_IMPACT.get(spec.name, 'impact non documenté')}")
    lines.append("")
    generatable_names = ", ".join(sorted(GENERATORS))
    lines.append(f"Se tournent ici : {generatable_names}.")
    lines.append(
        "Pour en tourner un : python scripts/generate_secrets.py --rotate <NOM> "
        "(annonce), puis le même avec --apply (écrit et horodate)."
    )
    return lines


def rotation_report(
    plan: RotationPlan, *, applied: bool, recorded: Sequence[str], ledger_path: Path
) -> List[str]:
    """Le rapport de rotation : ce qui a été mesuré, écrit, horodaté — et ce qui reste."""
    lines = [f"Rotation assistée — {plan.name}", f"Fichier d'environnement : {plan.env_file}"]
    lines.append(f"Registre de rotation : {ledger_path}")
    lines.append("")
    if plan.initial_setup:
        lines.append(f"{WARN_MARK} Aucune valeur en place pour {plan.name} aujourd'hui.")
    else:
        lines.append(
            f"Valeur actuelle — empreinte {plan.previous_fingerprint} (sera remplacée par "
            "la neuve, jamais affichée)"
        )
    lines.append("")
    lines.append("Ce que cette rotation casse :")
    for item in plan.impact:
        lines.append(f"   {WARN_MARK} {item}")
    lines.append("")

    if not applied:
        lines.append(
            f"{WARN_MARK} Rien n'a été écrit : ceci est l'annonce. Relance avec "
            "--apply pour faire la rotation (le .env est alors modifié et "
            "l'empreinte horodatée)."
        )
        lines.append("")
        lines.append("Après l'écriture, il resterait à faire :")
        lines.extend(f"   {index}. {step}" for index, step in enumerate(plan.next_steps, start=1))
        return lines

    lines.append(
        f"{OK_MARK} Rotation appliquée : empreinte {plan.previous_fingerprint or '—'} → "
        f"{plan.new_fingerprint} (écrite dans {plan.env_file.name}, jamais affichée)."
    )
    if plan.name == "ENCRYPTION_KEY":
        # Ce qui **rouvre** encore l'ancien chiffré : la clé sortante, puis les
        # entrées déjà présentes. C'est la liste de ce qu'on ne peut pas retirer
        # avant que `rotate_encryption_key.py` ne trouve plus rien à réécrire.
        opens = [
            version
            for version in [plan.previous_version, *plan.ring_versions]
            if version
        ]
        holders = ", ".join(f"v{version}" for version in opens) or "—"
        lines.append(f"   • anneau : la clé active chiffre en v{plan.version} ; rouvrent : {holders}")
    if recorded:
        lines.append(
            f"{OK_MARK} Registre horodaté : {', '.join(recorded)} — {ledger_path}"
        )
    else:
        lines.append(
            f"{WARN_MARK} Registre NON horodaté (--no-record) : la barrière refusera "
            "tant que l'empreinte n'aura pas suivi — c'est volontaire, et c'est le "
            f"seul geste qui reste : python scripts/verify_secrets.py --record"
        )
    lines.append("")
    lines.append("Il reste à faire :")
    steps = _next_steps(plan.name, bool(recorded))
    lines.extend(f"   {index}. {step}" for index, step in enumerate(steps, start=1))
    return lines


def record_only_report(
    names: Sequence[str], values: Mapping[str, str], ledger_path: Path
) -> List[str]:
    """Le rapport de `--record-only` : le registre a suivi, la valeur n'a pas bougé."""
    lines = [
        f"{OK_MARK} Rotation horodatée dans {ledger_path} :",
    ]
    for name in names:
        lines.append(_fingerprint_line(name, values[name]))
    lines.append(
        "(La valeur est inchangée : seul le registre a été mis à jour — c'est le geste "
        "des secrets qui ne se génèrent pas ici.)"
    )
    return lines


def refused_rotation_report(name: str, reason: str, values: Mapping[str, str]) -> List[str]:
    """Ce que la rotation refusée aurait cassé, et par où passer à la place."""
    value = (values.get(name) or "").strip()
    lines = [
        f"{KO_MARK} Rotation impossible ici — {name}",
        "",
        f"   {reason}",
        "",
        "Ce qu'une rotation de ce secret casserait :",
        f"   {WARN_MARK} {ROTATION_IMPACT.get(name, 'impact non documenté')}",
    ]
    if value:
        lines.append("")
        lines.append(
            "Une valeur est déjà en place (empreinte "
            f"{secret_fingerprint(name, value)}) : si elle vient d'être tournée chez le "
            "fournisseur, horodate-la sans y toucher — commande à lancer ici — "
            f"python scripts/generate_secrets.py --record-only {name}"
        )
    return lines


# --------------------------------------------------------------------------- #
# Invocation
# --------------------------------------------------------------------------- #


def _is_env_target(path: Path) -> bool:
    """Un fichier d'environnement **local** : `.env`, `.env.local`, `prod.env`…

    `.env.example` et compagnie sont exclus, et ce sont eux qui comptent : ils sont
    **suivis** par le dépôt, donc y écrire publierait le secret au prochain commit.
    """
    name = path.name
    if name.endswith(TEMPLATE_SUFFIXES):
        return False
    return name == ".env" or name.startswith(".env") or name.endswith(".env")


def _resolve_env_file(raw: str) -> Path:
    """Le chemin cible — un relatif est résolu depuis la **racine du projet**.

    Ancrer le défaut sur la racine évite d'écrire un `.env` dans le répertoire
    courant d'où l'on a lancé le script, qui n'est presque jamais celui du projet.
    """
    candidate = Path(raw)
    return candidate if candidate.is_absolute() else REPO_ROOT / candidate


def _resolve_ledger_path(raw: Optional[str]) -> Path:
    """Le registre visé — même ancrage que le `.env` (option, puis `$SECRET_ROTATION_LEDGER`).

    `verify_secrets.py` lit le même chemin par `resolve_ledger_path` : les deux
    outils doivent désigner le **même** fichier, sinon la rotation horodaterait un
    registre que la barrière ne lit pas.
    """
    resolved = Path(resolve_ledger_path(raw))
    return resolved if resolved.is_absolute() else REPO_ROOT / resolved


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="generate_secrets.py",
        description="Génère les secrets locaux dans .env sans jamais les afficher "
        "(et tourne un secret désigné, en disant ce que la rotation casse).",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Fichier d'environnement à remplir (défaut : .env). Un chemin relatif "
        "est résolu depuis la racine du projet, pas depuis le répertoire courant.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="N'écrit rien : dit seulement ce qui manque (sortie 1 s'il manque "
        "quelque chose, 0 sinon).",
    )
    parser.add_argument(
        "--rotate",
        nargs="?",
        const="",
        default=None,
        metavar="NOM",
        help="Tourne un secret **désigné** : annonce ce que la rotation casse, écrit "
        "la valeur neuve sans l'afficher, et horodate l'empreinte. Rien n'est écrit "
        "sans --apply. Sans nom, liste ce qu'une rotation de chaque secret casse.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Exécute réellement la rotation (sans ce drapeau, --rotate annonce seulement).",
    )
    parser.add_argument(
        "--no-record",
        action="store_true",
        help="Tourne sans horodater le registre : l'audit refuse alors tant que "
        "l'empreinte n'a pas suivi (à réserver à l'ordre « horodater après le "
        "redéploiement »).",
    )
    parser.add_argument(
        "--record-only",
        default=None,
        metavar="NOM",
        help="Horodate l'empreinte **actuelle** d'un seul secret, sans y toucher — le "
        "geste des deux secrets qui ne se génèrent pas ici (tableau de bord Supabase, "
        "BotFather). `verify_secrets.py --record` ferait repartir aussi les autres.",
    )
    parser.add_argument(
        "--ledger",
        default=None,
        help=f"Registre de rotation visé (défaut : {DEFAULT_LEDGER_PATH}, ou "
        "$SECRET_ROTATION_LEDGER).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Sortie JSON. Elle contient des noms et des empreintes — jamais une valeur.",
    )
    return parser


def _check_invocation(args: argparse.Namespace) -> None:
    """Les combinaisons qui n'ont pas de sens — refusées avant toute lecture.

    Elles disent toutes la même chose : ce que l'utilisateur croit déclencher n'est
    pas ce qui se déclencherait. Laisser passer serait le pire des deux mondes.
    """
    if args.apply and args.rotate is None:
        raise UsageError(
            "--apply ne concerne que --rotate : sans rotation demandée, il n'y a rien "
            "à confirmer."
        )
    if args.rotate is not None and args.record_only is not None:
        raise UsageError(
            "--rotate et --record-only s'excluent : l'un tourne une valeur, l'autre "
            "horodate celle qui est en place."
        )
    if args.record_only is not None and args.no_record:
        raise UsageError("--record-only horodate ; --no-record le contredit.")
    if args.check and (args.rotate is not None or args.record_only is not None):
        raise UsageError(
            "--check ne dit que ce qui manque : il ne tourne rien et n'horodate rien."
        )
    if args.record_only is not None and args.record_only.strip() == "":
        raise UsageError("--record-only demande un nom de secret.")


def _rotation_mode(args: argparse.Namespace, values: Mapping[str, str], env_file: Path) -> int:
    """Tourner / horodater un secret désigné — et rendre le code de sortie de l'action."""
    ledger_path = _resolve_ledger_path(args.ledger)

    if args.record_only is not None:
        name = args.record_only.strip()
        _spec_by_name(name)
        value = (values.get(name) or "").strip()
        if not value:
            print(
                f"{KO_MARK} « {name} » n'a aucune valeur à horodater : renseigne-la "
                f"d'abord dans {env_file} ({MANUAL_SOURCES.get(name, 'source externe')}).",
                file=sys.stderr,
            )
            return 1
        names = record_entries(ledger_path, {name: value})
        if args.json:
            print(
                json.dumps(
                    {
                        "env_file": str(env_file),
                        "recorded": names,
                        "ledger": str(ledger_path),
                        "fingerprint": secret_fingerprint(name, value),
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            for line in record_only_report(names, {name: value}, ledger_path):
                print(line)
        return 0

    if not args.rotate:
        # Mode catalogue : ce que chaque rotation casse, et d'où vient chaque valeur.
        if args.json:
            print(
                json.dumps(
                    {
                        "env_file": str(env_file),
                        "rotations": [
                            {
                                "name": spec.name,
                                "generatable_here": spec.name in GENERATORS,
                                "source": None
                                if spec.name in GENERATORS
                                else MANUAL_SOURCES.get(spec.name, "service externe"),
                                "fingerprint": secret_fingerprint(
                                    spec.name, (values.get(spec.name) or "").strip()
                                )
                                if (values.get(spec.name) or "").strip()
                                else None,
                                "impact": ROTATION_IMPACT.get(spec.name, "impact non documenté"),
                            }
                            for spec in DEFAULT_SPECS
                        ]
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            for line in rotation_catalog(values):
                print(line)
        return 0

    name = args.rotate.strip()
    recorded = not args.no_record
    try:
        plan = build_rotation(
            name, values, env_file, generate=args.apply, recorded=recorded
        )
    except RotationRefused as exc:
        if args.json:
            print(
                json.dumps(
                    {
                        "env_file": str(env_file),
                        "rotation": {
                            "name": exc.name,
                            "applied": False,
                            "refused": exc.reason,
                            "impact": ROTATION_IMPACT.get(exc.name, "impact non documenté"),
                        },
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            for line in refused_rotation_report(exc.name, exc.reason, values):
                print(line)
        return 1

    if not args.apply:
        if args.json:
            print(
                json.dumps(
                    plan.as_dict(applied=False, recorded=(), ledger=str(ledger_path)),
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            for line in rotation_report(
                plan, applied=False, recorded=(), ledger_path=ledger_path
            ):
                print(line)
        return 0

    written = apply_rotation(plan)
    recorded_names: List[str] = []
    if recorded:
        recorded_names = record_entries(ledger_path, plan.values)
    if args.json:
        print(
            json.dumps(
                plan.as_dict(applied=True, recorded=recorded_names, ledger=str(ledger_path)),
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        for line in rotation_report(
            plan, applied=True, recorded=recorded_names, ledger_path=ledger_path
        ):
            print(line)
    return 0 if written else 1


def main(argv: Optional[List[str]] = None) -> int:
    console.make_streams_utf8()
    args = _build_parser().parse_args(argv)

    env_file = _resolve_env_file(args.env_file)
    if not _is_env_target(env_file):
        print(
            f"{KO_MARK} « {env_file.name} » n'est pas un fichier d'environnement local : "
            "un secret s'écrit dans `.env` (ou un nom en `*.env`), jamais dans un "
            "fichier suivi comme `.env.example`.",
            file=sys.stderr,
        )
        return 2

    try:
        _check_invocation(args)
    except UsageError as exc:
        print(f"{KO_MARK} {exc}", file=sys.stderr)
        return 2

    values = parse_env_file(env_file)

    if args.rotate is not None or args.record_only is not None:
        try:
            return _rotation_mode(args, values, env_file)
        except UsageError as exc:
            print(f"{KO_MARK} {exc}", file=sys.stderr)
            return 2

    plan = build_plan(values, env_file, generate=not args.check)

    if args.check:
        if args.json:
            print(json.dumps(plan.as_dict(), indent=2, ensure_ascii=False))
        else:
            for line in check_report(plan):
                print(line)
        return 0 if plan.ok else 1

    try:
        written = apply_plan(plan)
    except UsageError as exc:
        print(f"{KO_MARK} {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(plan.as_dict(written), indent=2, ensure_ascii=False))
    else:
        for line in write_report(plan, written):
            print(line)
    return 0 if plan.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
