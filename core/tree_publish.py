"""Publier l'arbre local sur une branche distante, et le **prouver**.

Cet arbre de travail n'est pas un dépôt git : il vit à côté du clone qui, lui, est
versionné, et il en dérive à chaque session (correctifs, tests, documentation).
Le publier à la main, c'est recopier les fichiers un par un, deviner lesquels ont
changé, se souvenir de l'élagage, puis pousser — et surtout **croire** que ce qui
est parti est bien ce qui était là. Deux incidents ont montré le prix de cette
croyance : un fichier `git` vide de 0 octet, né d'une redirection malheureuse,
s'est retrouvé publié ; dix modules hérités que l'arbre local avait remplacés
restaient sur la branche, et personne ne s'en apercevait.

Ce module fait donc le geste en entier, et finit par une **vérification**, pas par
un espoir :

1. **export** — l'arbre local est recopié dans un dossier de travail qui ne
   contient que ce que le dépôt suit. Le `.gitignore` du dépôt fait foi (c'est lui
   qui dit que `.env` et le registre de rotation ne partent pas), complété par une
   seconde ceinture pour ce que git ne peut pas savoir : sauvegardes d'éditeur,
   journaux, `.pyc` ;
2. **élagage** — un miroir ne laisse rien derrière : tout fichier suivi par la
   branche et absent du poste disparaît. C'est ce qui a manqué la première fois ;
3. **commit** — en un seul, et volontairement : la branche est un *instantané de
   l'arbre local*, pas un récit. Découper par thème produirait des commits
   intermédiaires dont la suite de tests ne passe pas (le corpus des `except`
   muets exige **tous** les fichiers annotés), et un état vert entre deux commits
   vaut mieux qu'une histoire plus jolie. Les hooks du dépôt restent actifs :
   `--no-verify` n'est jamais passé ;
4. **push** — refspec **explicite** (`branche:branche`), jamais un `git push`
   nu : ce qui part doit être nommé, et un `push.default` mal réglé ne doit pas
   pouvoir décider à notre place ;
5. **vérification** — le distant est relu (`ls-remote` par le `fetch`), et
   **chaque** fichier de la branche poussée est comparé au fichier du poste. La
   comparaison n'est pas une taille, ni une date : c'est l'identité
   content-adressée de git — un `blob` dont l'identifiant est le hachage du
   contenu — recalculée ici sur les octets du poste. Une seule exception, et elle
   est déclarée : les fins de ligne. `.gitattributes` normalise les fichiers texte
   en LF (`*.bat`/`*.cmd` en CRLF), donc un fichier écrit en CRLF au poste est
   stocké en LF ; ces fichiers-là sont comptés à part, et **nommés**, plutôt que
   tolérés en silence.

Le module ne connaît ni `argparse` ni `print` : il rend un rapport (un `dict`), et
`scripts/publish_tree.py` le met en phrases. Les appels à `git` passent tous par
`git()`, qui rend `None` quand le programme n'a pas pu démarrer — jamais une
exception qui remonterait au milieu du geste.
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

#: Dossiers jamais recopiés : dépôt, environnement virtuel, caches, artefacts.
#: `build` et `.gradle` sont aussi dans le `.gitignore`, mais un dossier ignoré
#: vide ou un cache non ignoré n'a rien à faire dans un import — et `.freebuff` en
#: est la preuve : l'état local du client (37 octets d'identifiant de projet) est
#: parti sur la branche **publique** à une publication à la main, parce qu'aucun
#: `.gitignore` ne le nommait. La ceinture est là pour ce que git ne peut pas
#: savoir.
EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".freebuff",
        ".venv",
        "venv",
        ".pgtest",
        "__pycache__",
        ".ruff_cache",
        ".mypy_cache",
        ".pytest_cache",
        ".gradle",
        ".idea",
        ".vscode",
        "build",
        "node_modules",
    }
)

#: Fichiers jamais recopiés, même si le `.gitignore` les laissait passer. La
#: liste est courte exprès : dès qu'un nom doit y entrer, c'est le `.gitignore`
#: qui est en retard, et c'est là qu'il faut corriger.
EXCLUDED_NAMES = frozenset({".env", ".env.local", ".env.tmp"})

#: Suffixes d'artefacts locaux : des octets que personne n'a écrits à la main.
EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".log", ".orig", ".rej", "~")

#: À la **racine** seulement : un fichier vide, un nom d'essai, une sauvegarde
#: d'éditeur. Ce sont exactement les trois formes des incidents connus (le `git`
#: vide, les `_patch_harness.py` d'une session). Hors racine, ces noms ont un
#: sens (un `py.typed` est vide, `_compat.py` est un nom courant), donc la règle
#: ne s'y applique pas.
STRAY_PREFIXES = ("_",)
STRAY_SUFFIXES = ("~", ".bak", ".swp", ".swo", ".tmp")

#: La branche que ce dépôt publie. Le nom dit ce qu'elle est : un import de
#: l'arbre local, pas une branche de développement.
DEFAULT_BRANCH = "import/arbre-local"

#: Dossier des hooks versionnés, et la clé de configuration qui les active.
HOOKS_DIR = ".githooks"
HOOKS_CONFIG_KEY = "core.hooksPath"

#: Plafond des commandes git : un réseau lent doit finir par échouer, pas pendre
#: la session. Le `clone` initial d'un dépôt est la seule qui s'en approche.
GIT_TIMEOUT = 600

#: Combien de chemins en échec le rapport nomme avant de compter. Le rapport doit
#: rester lisible ; la liste intégrale est dans le rapport complet (`--json`).
MAX_FAILED_NAMES = 12

#: Ce que le runtime laisse derrière lui quand un processus n'a **pas démarré**
#: (saturation de `fork` sous MSYS2). La distinction est celle de
#: `tests/hook_support.py` : un outil qui démarre et refuse **parle**, un outil que
#: l'OS n'a pas lancé laisse du bruit de runtime — ou rien du tout.
LAUNCH_NOISE = (
    "cygheap read copy failed",
    "child_copy:",
    "Resource temporarily unavailable",
    "0xC0000142",
)


def launch_incident(output: str) -> bool:
    """L'outil n'a pas démarré (bruit de runtime), ou n'a rien dit du tout."""
    return not output.strip() or any(noise in output for noise in LAUNCH_NOISE)


# --------------------------------------------------------------------------- #
# Parler à git
# --------------------------------------------------------------------------- #


def run(
    argv: Sequence[str],
    cwd: Optional[pathlib.Path] = None,
    timeout: int = GIT_TIMEOUT,
) -> Optional[subprocess.CompletedProcess]:
    """Exécute un programme. Rend `None` s'il n'a pas pu **démarrer**.

    La distinction compte : un `git` absent, un `fork` refusé par l'OS et un
    `git` qui démarre puis refuse sont trois choses différentes. Les deux
    premières ne disent rien du dépôt, la troisième si — et c'est la seule que
    l'appelant doit interpréter.
    """
    try:
        return subprocess.run(
            list(argv), cwd=str(cwd) if cwd else None, capture_output=True, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError):
        return None


def git(
    *args: str, cwd: pathlib.Path, timeout: int = GIT_TIMEOUT
) -> Optional[subprocess.CompletedProcess]:
    """`git <args>` dans `cwd` — ou `None` si git n'a pas pu démarrer."""
    return run(["git", *args], cwd=cwd, timeout=timeout)


def decode(done: Optional[subprocess.CompletedProcess], stream: str = "stdout") -> str:
    """Le flux voulu en texte, sans dépendre de la locale (les rapports sont en français)."""
    if done is None:
        return ""
    data = getattr(done, stream, None) or b""
    return data.decode("utf-8", errors="replace")


def failure(done: Optional[subprocess.CompletedProcess], what: str) -> str:
    """Pourquoi une commande a échoué — le message de git, ou le fait qu'il soit absent."""
    if done is None:
        return f"{what} : `git` n'a pas pu démarrer"
    message = decode(done, "stderr").strip() or decode(done, "stdout").strip()
    return f"{what} : code {done.returncode}" + (f" — {message}" if message else "")


# --------------------------------------------------------------------------- #
# Ce que le poste contient
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Candidate:
    """Un fichier du poste, prêt à être publié."""

    relative: str
    path: pathlib.Path
    size: int


@dataclass(frozen=True)
class Skipped:
    """Un élément du poste **écarté**, et pourquoi — jamais écarté en silence.

    C'est la même règle que celle appliquée au scan anti-fuite du dépôt : un
    périmètre qui ne dit pas ce qu'il laisse de côté se lit comme une couverture
    totale. Un `.env` non publié doit être **nommé**, sinon personne ne saura
    jamais s'il a été oublié ou volontairement exclu.
    """

    relative: str
    reason: str


@dataclass(frozen=True)
class Harvest:
    """Ce que le parcours du poste a trouvé : à publier, et le reste."""

    candidates: List[Candidate]
    skipped_files: List[Skipped]
    skipped_dirs: List[Skipped]


def collect(root: pathlib.Path) -> Harvest:
    """Parcourt le poste : ce qui peut partir, et ce qui est écarté par la forme.

    Les dossiers d'exclusion ne sont pas descendus (`.venv` en contient des
    milliers) : ils sont nommés **comme dossiers**, une fois, ce qui est aussi
    informatif et infiniment moins coûteux.
    """
    found: List[Candidate] = []
    skipped_files: List[Skipped] = []
    skipped_dirs: List[Skipped] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # `dirnames[:] = …` : modifié en place, c'est ce qui empêche `os.walk` de
        # descendre. Trié, sinon deux exécutions produisent deux rapports.
        kept_dirs = []
        for name in sorted(dirnames):
            if name in EXCLUDED_DIRS:
                relative = (pathlib.Path(dirpath) / name).relative_to(root).as_posix()
                skipped_dirs.append(Skipped(relative, "dossier exclu"))
            else:
                kept_dirs.append(name)
        dirnames[:] = kept_dirs
        for name in sorted(filenames):
            relative = (pathlib.Path(dirpath) / name).relative_to(root).as_posix()
            if name in EXCLUDED_NAMES:
                skipped_files.append(Skipped(relative, "fichier réservé (secret local)"))
                continue
            if name.endswith(EXCLUDED_SUFFIXES):
                skipped_files.append(Skipped(relative, "artefact local"))
                continue
            path = root / relative
            try:
                size = path.stat().st_size
            except OSError:
                # sans signal : disparu entre le parcours et la lecture, il n'y a plus
                # rien à publier — le nommer à chaque fois serait du bruit
                continue
            found.append(Candidate(relative, path, size))
    return Harvest(candidates=found, skipped_files=skipped_files, skipped_dirs=skipped_dirs)


def stray_reason(relative: str, size: int) -> Optional[str]:
    """Pourquoi ce fichier n'a rien à faire dans un import — ou `None`.

    Ne s'applique qu'aux fichiers **nouveaux** (jamais à un fichier déjà suivi),
    et seulement à la racine : ailleurs, un fichier vide ou un nom commençant par
    `_` est une convention, pas un accident.
    """
    if "/" in relative:
        return None
    if size == 0:
        return "fichier vide"
    if relative.endswith(STRAY_SUFFIXES):
        return "nom de sauvegarde"
    if relative.startswith(STRAY_PREFIXES):
        return "nom d'essai (`_…`)"
    return None


def ignored_paths(
    work_tree: pathlib.Path,
    relatives: Sequence[str],
    excludes_file: Optional[pathlib.Path] = None,
) -> Optional[Set[str]]:
    """Ce que le `.gitignore` du dépôt écarte, interrogé **depuis le clone**.

    C'est le `.gitignore` du dépôt qui fait foi, pas une liste recopiée ici : une
    règle ajoutée là-bas vaut immédiatement pour la publication. `check-ignore`
    rend 1 quand aucun chemin n'est ignoré — ce n'est pas un échec.

    `excludes_file` porte le `.gitignore` **du poste**, qui n'est pas encore dans
    le clone au premier passage. Sans lui, une règle ajoutée localement (par
    exemple « ce fichier est un secret ») n'aurait d'effet qu'à la publication
    *suivante* : le secret serait parti une fois, puis retiré — trop tard pour
    être non parti. Les motifs d'un fichier d'exclusion global sont lus par git
    exactement comme ceux d'un `.gitignore` (vérifié : nom simple, chemin ancré,
    joker), donc les deux sources s'additionnent sans changer de sens. Ce chemin
    reçoit le fichier **sans le copier** : l'essai à blanc n'écrit rien.
    """
    if not relatives:
        return set()
    payload = "\0".join(relatives).encode("utf-8") + b"\0"
    options: List[str] = []
    if excludes_file is not None:
        options = ["-c", f"core.excludesFile={excludes_file}"]
    try:
        done = subprocess.run(
            ["git", "-C", str(work_tree), *options, "check-ignore", "--stdin", "-z"],
            input=payload,
            capture_output=True,
            timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode not in (0, 1):
        return None
    return {item for item in done.stdout.decode("utf-8").split("\0") if item}


def tracked_files(work_tree: pathlib.Path) -> Optional[List[str]]:
    """Les chemins suivis par la branche, dans l'ordre."""
    done = git("ls-files", "-z", cwd=work_tree)
    if done is None or done.returncode != 0:
        return None
    return [item for item in done.stdout.decode("utf-8").split("\0") if item]


# --------------------------------------------------------------------------- #
# Le plan : ce qui serait copié, élagué, écarté
# --------------------------------------------------------------------------- #


@dataclass
class Plan:
    """Le geste, décrit avant d'être fait — et le compte rendu de ce qu'il fut."""

    root: pathlib.Path
    work_tree: pathlib.Path
    copies: List[Candidate] = field(default_factory=list)
    identical: List[str] = field(default_factory=list)
    prunes: List[str] = field(default_factory=list)
    ignored: List[str] = field(default_factory=list)
    excluded: List[Skipped] = field(default_factory=list)
    strays: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def published(self) -> List[str]:
        """Ce que la branche contiendra de l'arbre local, une fois le plan appliqué."""
        kept = {candidate.relative for candidate in self.copies}
        return sorted(kept | set(self.identical))

    def as_dict(self) -> Dict[str, Any]:
        return {
            "root": str(self.root),
            "work_tree": str(self.work_tree),
            "copies": [str(candidate.path) for candidate in self.copies],
            "identical": list(self.identical),
            "prunes": list(self.prunes),
            "ignored": list(self.ignored),
            "excluded": [
                {"path": item.relative, "reason": item.reason} for item in self.excluded
            ],
            "strays": [{"path": path, "reason": reason} for path, reason in self.strays],
            "published": self.published,
        }


def plan(
    root: pathlib.Path,
    work_tree: pathlib.Path,
    *,
    include_strays: bool = False,
) -> Optional[Plan]:
    """Ce qu'il y aurait à faire pour que le clone soit le miroir du poste.

    Rend `None` quand git n'a pas pu répondre : un plan vide serait alors un
    mensonge (il annoncerait « rien à publier » là où rien n'a été lu).
    """
    harvest = collect(root)
    local_gitignore = root / ".gitignore"
    ignored = ignored_paths(
        work_tree,
        [item.relative for item in harvest.candidates],
        excludes_file=local_gitignore if local_gitignore.is_file() else None,
    )
    if ignored is None:
        return None
    tracked = tracked_files(work_tree)
    if tracked is None:
        return None
    known = set(tracked)

    kept: List[Candidate] = []
    strays: List[Tuple[str, str]] = []
    for candidate in harvest.candidates:
        if candidate.relative in ignored:
            continue
        reason = (
            None
            if candidate.relative in known
            else stray_reason(candidate.relative, candidate.size)
        )
        if reason is not None and not include_strays:
            strays.append((candidate.relative, reason))
            continue
        kept.append(candidate)

    # Le périmètre, en entier : ce que le `.gitignore` écarte, ce que la forme
    # écarte (dossiers non descendus, artefacts, secrets locaux) — nommés, sinon
    # « 310 fichiers publiés » se lirait comme « tout le poste est publié ».
    excluded = list(harvest.skipped_dirs) + list(harvest.skipped_files)
    excluded += [Skipped(relative, "écarté par le `.gitignore` du dépôt") for relative in sorted(ignored)]
    built = Plan(
        root=root,
        work_tree=work_tree,
        ignored=sorted(ignored),
        excluded=sorted(excluded, key=lambda item: item.relative),
        strays=strays,
    )
    for candidate in kept:
        target = work_tree / candidate.relative
        try:
            if target.is_file() and target.read_bytes() == candidate.path.read_bytes():
                built.identical.append(candidate.relative)
                continue
        except OSError:
            # sans signal : illisible d'un côté ou de l'autre, donc on recopie — et
            # c'est la copie qui nomme son échec (voir `apply_plan`)
            pass
        built.copies.append(candidate)

    kept_relatives = {candidate.relative for candidate in kept}
    built.prunes = sorted(rel for rel in tracked if rel not in kept_relatives)
    built.identical.sort()
    return built


def apply_plan(built: Plan) -> Dict[str, Any]:
    """Écrit les copies et l'élagage du plan : ce qui a été fait, et ce qui a échoué.

    Une écriture impossible n'est pas avalée : elle est **nommée**, et `publish`
    s'arrête avant le commit. Une copie manquante ferait partir une branche qui
    n'est pas le miroir du poste — ce que la vérification refuserait de toute
    façon, mais après avoir écrit dans l'historique.
    """
    written = 0
    failed: List[Dict[str, str]] = []
    for candidate in built.copies:
        target = built.work_tree / candidate.relative
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(candidate.path, target)
            # Pas de `copystat` ici, et c'est délibéré. Recopier la date du poste (ou
            # écrire dans le fichier en gardant la sienne) rend au fichier une
            # empreinte de `stat` que le cache de git prend pour une preuve de
            # non-changement : même taille, même inode, même date — et sous Windows
            # la date de création ne bouge pas à l'écriture. Un contenu modifié à
            # taille constante serait alors **committé avec son ancien contenu**,
            # sans que rien ne le dise. Une date fraîche force la relecture ; ce qui
            # compte est l'octet, pas l'horodatage.
            os.utime(target, None)
        except OSError as exc:
            failed.append(
                {"path": candidate.relative, "reason": f"{type(exc).__name__}: {exc}"}
            )
            continue
        written += 1
    pruned = 0
    for relative in built.prunes:
        try:
            (built.work_tree / relative).unlink()
        except FileNotFoundError:
            # sans signal : il n'était déjà plus là — c'est le résultat voulu, et
            # l'élagage est compté juste après
            pass
        except OSError as exc:
            failed.append({"path": relative, "reason": f"{type(exc).__name__}: {exc}"})
            continue
        pruned += 1
    return {"written": written, "pruned": pruned, "failed": failed}


# --------------------------------------------------------------------------- #
# Le dossier de travail
# --------------------------------------------------------------------------- #


def default_work_tree() -> pathlib.Path:
    """Le clone réutilisé d'une publication à l'autre, dans le dossier temporaire.

    Le poste n'est **pas** un dépôt : y installer le clone obligerait à l'exclure
    de son propre miroir, et un `.git` dans le dossier temporaire n'appartient à
    aucune session — il survit, ou disparaît, sans rien emporter du dépôt.
    """
    return pathlib.Path(tempfile.gettempdir()) / "ai-trade-publication"


def remote_branch_exists(work_tree: pathlib.Path, remote: str, branch: str) -> Optional[bool]:
    """La branche distante existe-t-elle ? `None` quand la question reste sans réponse.

    `ls-remote --exit-code` : `0` trouvée, `2` absente, autre chose (128…) le
    distant n'a pas répondu — donc on ne sait pas, et cela ne doit pas se confondre
    avec « absente ».
    """
    done = git("ls-remote", "--exit-code", "--heads", remote, branch, cwd=work_tree)
    if done is None:
        return None
    if done.returncode == 0:
        return True
    if done.returncode == 2:
        return False
    return None


def fetch_branch(work_tree: pathlib.Path, remote: str, branch: str, attempts: int = 3) -> Dict[str, Any]:
    """Récupère **exactement** la branche voulue, et rejoue un incident de lancement.

    Deux leçons, toutes deux payées en conditions réelles :

    * la branche est **nommée** (`+refs/heads/<branche>:refs/remotes/<distant>/<branche>`)
      plutôt que laissée au `git fetch <distant>` nu. Celui-ci n'obéit qu'à
      `remote.<distant>.fetch` : sur un clone qui ne suit qu'une autre branche
      (`--single-branch`, ou `git remote set-branches`), la référence de suivi de
      la branche publiée **ne bouge pas**. Mesuré ici : le clone était réglé sur
      `main` seul, la référence est restée deux commits en arrière, et la bascule
      de branche a fait **reculer** le clone — suivie d'un refus en
      non-fast-forward, après qu'un commit eut été écrit par-dessus un état ancien ;
    * un incident de lancement (`fork` saturé) est rejoué — rien n'a été écrit —
      alors qu'un échec qui **parle** (dépôt introuvable, droits refusés) ne se
      rejoue pas, et se voit.

    Rend `{"ok", "exists", "error"}` : `exists=False` n'est **pas** une erreur —
    c'est le premier envoi, celui où la branche distante n'existe pas encore.
    """
    refspec = f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}"
    last: Optional[subprocess.CompletedProcess] = None
    for _ in range(attempts):
        done = git("fetch", remote, refspec, cwd=work_tree)
        if done is not None and done.returncode == 0:
            return {"ok": True, "exists": True, "error": None}
        last = done
        if done is not None and not launch_incident(decode(done) + decode(done, "stderr")):
            break
    if remote_branch_exists(work_tree, remote, branch) is False:
        return {"ok": True, "exists": False, "error": None}
    return {"ok": False, "exists": False, "error": failure(last, f"récupération de {refspec}")}


def rev_count(work_tree: pathlib.Path, revision_range: str) -> int:
    """`git rev-list --count <plage>` — 0 si la plage n'a pas de sens ici."""
    done = git("rev-list", "--count", revision_range, cwd=work_tree)
    if done is None or done.returncode != 0:
        return 0
    try:
        return int(decode(done).strip())
    except ValueError:
        return 0


def ensure_work_tree(
    work_tree: pathlib.Path,
    *,
    url: Optional[str],
    remote: str,
    branch: str,
) -> Dict[str, Any]:
    """Clone le dépôt s'il manque, le met à jour, et pose la branche à publier.

    Rend ce qui s'est passé (`cloned`, `fetched`, `branch`, `hooks`) ou `error`.
    """
    state: Dict[str, Any] = {
        "cloned": False,
        "fetched": False,
        "branch": None,
        "hooks": None,
        "resets": 0,
    }
    if not (work_tree / ".git").is_dir():
        if not url:
            state["error"] = (
                f"dossier de travail absent ({work_tree}) et aucune URL : "
                "indiquer `--url <url>` pour le premier clonage"
            )
            return state
        work_tree.parent.mkdir(parents=True, exist_ok=True)
        if work_tree.exists():
            # Un dossier non vide sans `.git` : ne rien écraser. Le nommer est
            # plus utile que le vider, et surtout plus sûr.
            state["error"] = (
                f"{work_tree} existe sans être un dépôt git : "
                "le choisir vide, ou le supprimer à la main"
            )
            return state
        cloned = git("clone", url, str(work_tree), cwd=work_tree.parent)
        if cloned is None or cloned.returncode != 0:
            state["error"] = failure(cloned, f"clonage de {url}")
            return state
        state["cloned"] = True

    # La récupération est **obligatoire**, et c'est tout sauf un détail : sans elle,
    # la bascule de branche se fait sur une référence de suivi peut-être périmée —
    # la branche locale recule au lieu de suivre le distant, et le push suivant est
    # refusé (non-fast-forward) après qu'un commit a été écrit par-dessus un état
    # ancien. Constaté deux fois, en publiant ce module : un `fetch` mort sur un
    # `fork` saturé, puis un clone dont `remote.origin.fetch` ne nommait que `main`
    # — deux chemins vers la même référence périmée. D'où une récupération
    # **nommée** (`fetch_branch`) et rendue obligatoire : elle ne modifie rien
    # (hors références de suivi), donc un incident de lancement se rejoue, alors
    # qu'un échec qui parle arrête le geste.
    fetched = fetch_branch(work_tree, remote, branch)
    if not fetched["ok"]:
        state["error"] = (
            fetched["error"]
            + " — sans le distant relu, repositionner la branche pourrait la faire"
            " reculer : rien n'a été touché"
        )
        return state
    state["fetched"] = fetched["exists"]

    remote_ref = f"refs/remotes/{remote}/{branch}"
    local_ref = f"refs/heads/{branch}"
    ahead = rev_count(work_tree, f"{remote_ref}..{local_ref}")
    if ahead:
        # Un miroir se rebâtit à partir du poste : les commits que le distant n'a
        # pas seront repositionnés, et leur contenu vient du poste de toute façon.
        # Le dire est en revanche la moindre des choses — c'est une perte
        # d'historique, pas une perte de contenu.
        state["resets"] = ahead
    exists = git("rev-parse", "--verify", "--quiet", remote_ref, cwd=work_tree)
    if exists is not None and exists.returncode == 0:
        switched = git("checkout", "-B", branch, f"{remote}/{branch}", cwd=work_tree)
    else:
        switched = git("checkout", "-B", branch, cwd=work_tree)
    if switched is None or switched.returncode != 0:
        state["error"] = failure(switched, f"bascule sur {branch}")
        return state
    state["branch"] = branch

    hooks = work_tree / HOOKS_DIR
    if hooks.is_dir():
        configured = git("config", HOOKS_CONFIG_KEY, HOOKS_DIR, cwd=work_tree)
        if configured is None or configured.returncode != 0:
            state["error"] = failure(configured, f"activation de {HOOKS_DIR}")
            return state
        state["hooks"] = sorted(path.name for path in hooks.iterdir() if path.is_file())
    return state


# --------------------------------------------------------------------------- #
# Commit, push
# --------------------------------------------------------------------------- #


def compose_message(branch: str, status: Sequence[str]) -> str:
    """Le message d'un import, écrit à partir de ce que git a réellement indexé.

    Les lettres viennent de `git diff --cached --name-status` : le message décrit
    le diff, il ne le suppose pas.
    """
    counts = {"A": 0, "M": 0, "D": 0, "R": 0, "C": 0}
    names: List[str] = []
    for line in status:
        letter, _, path = line.partition("\t")
        letter = letter[:1]
        if letter in counts:
            counts[letter] += 1
        names.append(path or line)
    body = (
        f"{counts['A']} fichier(s) ajouté(s), {counts['M']} modifié(s), "
        f"{counts['D']} supprimé(s) — l'arbre du poste est recopié tel quel : "
        "ce qui change ici est ce qui a changé là-bas."
    )
    listed = "\n".join(f"* {name}" for name in names[:40])
    if len(names) > 40:
        listed += f"\n* … ({len(names) - 40} autre(s))"
    return (
        f"Publier l'arbre local sur {branch}\n\n{body}\n\n{listed}\n\n"
        "Publié par `scripts/publish_tree.py` : l'arbre entier est recopié, élagué "
        "de ce qu'il n'a plus, puis comparé fichier par fichier au distant.\n"
    )


def commit_all(work_tree: pathlib.Path, branch: str, message: Optional[str]) -> Dict[str, Any]:
    """Indexe tout, puis commite — hooks actifs, `--no-verify` jamais passé."""
    result: Dict[str, Any] = {"sha": None, "files": 0, "output": "", "error": None}
    staged = git("add", "-A", cwd=work_tree)
    if staged is None or staged.returncode != 0:
        result["error"] = failure(staged, "indexation (`git add -A`)")
        return result

    status = git("diff", "--cached", "--name-status", cwd=work_tree)
    if status is None or status.returncode != 0:
        result["error"] = failure(status, "lecture de l'index")
        return result
    lines = [line for line in decode(status).splitlines() if line.strip()]
    if not lines:
        return result  # rien à committer : la branche est déjà le miroir du poste

    result["files"] = len(lines)
    text = message or compose_message(branch, lines)
    commit = git("commit", "-m", text, cwd=work_tree)
    result["output"] = decode(commit) + decode(commit, "stderr")
    if commit is None or commit.returncode != 0:
        result["error"] = failure(commit, "commit")
        return result

    head = git("rev-parse", "HEAD", cwd=work_tree)
    if head is None or head.returncode != 0:
        result["error"] = failure(head, "lecture de HEAD")
        return result
    result["sha"] = decode(head).strip()
    return result


def push_argv(remote: str, branch: str) -> List[str]:
    """La commande de push : refspec **explicite**, et rien d'autre.

    Pas de `--no-verify` — les hooks du dépôt sont la raison d'être de ce
    chemin —, pas de `--force`, pas de `--tags` : ce qui part est nommé.
    """
    return ["git", "push", remote, f"{branch}:{branch}"]


def push(work_tree: pathlib.Path, remote: str, branch: str) -> Dict[str, Any]:
    """Pousse la branche, avec les hooks du dépôt actifs."""
    result: Dict[str, Any] = {"ok": False, "argv": push_argv(remote, branch), "output": "", "error": None}
    done = run(result["argv"], cwd=work_tree)
    result["output"] = decode(done) + decode(done, "stderr")
    if done is None or done.returncode != 0:
        result["error"] = failure(done, f"push vers {remote}")
        if done is not None and "non-fast-forward" in result["output"]:
            result["error"] += (
                " — la branche distante a avancé ailleurs : récupérer et fusionner, "
                "ce script ne force jamais"
            )
        return result
    result["ok"] = True
    return result


# --------------------------------------------------------------------------- #
# La vérification, fichier par fichier
# --------------------------------------------------------------------------- #


def blob_id(data: bytes, algorithm: str = "sha1") -> str:
    """L'identifiant qu'aurait ce contenu dans la base d'objets de git.

    C'est l'empreinte du **contenu**, pas d'une date ni d'une taille : deux
    fichiers qui diffèrent d'un octet n'ont pas le même `blob`, et c'est
    exactement ce qu'on veut prouver d'un fichier poussé.
    """
    header = f"blob {len(data)}\0".encode("ascii")
    digest = hashlib.new(algorithm)
    digest.update(header)
    digest.update(data)
    return digest.hexdigest()


def sha256_of(data: bytes) -> str:
    """L'empreinte lisible d'un fichier, telle qu'un humain peut la recopier."""
    return hashlib.sha256(data).hexdigest()


def object_format(work_tree: pathlib.Path) -> str:
    """`sha1` ou `sha256` : la base d'objets du clone, lue et non supposée."""
    done = git("rev-parse", "--show-object-format", cwd=work_tree)
    if done is None or done.returncode != 0:
        return "sha1"
    value = decode(done).strip()
    return value if value in {"sha1", "sha256"} else "sha1"


def tree_entries(work_tree: pathlib.Path, rev: str) -> Optional[Dict[str, str]]:
    """Les `blob` d'un commit : chemin → identifiant d'objet.

    `ls-tree -r` descend les sous-arbres ; un sous-module y apparaît en `commit`
    et n'a pas de contenu à comparer, donc il est laissé de côté (il n'est pas
    publié par ce chemin de toute façon).
    """
    done = git("ls-tree", "-r", "-z", rev, cwd=work_tree)
    if done is None or done.returncode != 0:
        return None
    entries: Dict[str, str] = {}
    for chunk in done.stdout.split(b"\0"):
        if not chunk:
            continue
        meta, _, path = chunk.partition(b"\t")
        parts = meta.decode("utf-8", "replace").split()
        if len(parts) == 3 and parts[1] == "blob":
            entries[path.decode("utf-8", "replace")] = parts[2]
    return entries


def remote_sha(work_tree: pathlib.Path, remote: str, branch: str) -> Optional[str]:
    """L'identifiant que porte la branche **distante**, relu après le push.

    Un `fetch` de la seule référence voulue, puis `FETCH_HEAD` : c'est le distant
    qui répond, pas notre mémoire de ce qu'on vient d'envoyer.
    """
    fetched = git("fetch", remote, f"refs/heads/{branch}", cwd=work_tree)
    if fetched is None or fetched.returncode != 0:
        return None
    head = git("rev-parse", "FETCH_HEAD", cwd=work_tree)
    if head is None or head.returncode != 0:
        return None
    return decode(head).strip()


def compare(root: pathlib.Path, entries: Dict[str, str], algorithm: str = "sha1") -> Dict[str, Any]:
    """Confronte chaque fichier publié au fichier du poste, octet par octet.

    Trois issues, et la troisième est la seule qui fasse échouer la publication :

    * **identique** — l'identifiant calculé sur les octets du poste est celui de
      l'objet poussé ;
    * **normalisé** — il ne l'est pas, mais il le devient après application de la
      seule transformation que `.gitattributes` autorise (fin de ligne CRLF → LF).
      C'est le cas d'un `.bat`, et il est nommé plutôt que toléré ;
    * **dérive** — ni l'un ni l'autre. Le fichier de la branche n'est pas celui du
      poste, et aucune empreinte ne peut le cacher.
    """
    identical: List[str] = []
    normalized: List[str] = []
    drift: List[Dict[str, str]] = []
    missing: List[str] = []
    for relative, blob in sorted(entries.items()):
        try:
            raw = (root / relative).read_bytes()
        except OSError:
            missing.append(relative)
            continue
        if blob_id(raw, algorithm) == blob:
            identical.append(relative)
        elif blob_id(raw.replace(b"\r\n", b"\n"), algorithm) == blob:
            normalized.append(relative)
        else:
            drift.append(
                {
                    "path": relative,
                    "poste": sha256_of(raw),
                    "branche": blob,
                }
            )
    return {
        "files": len(entries),
        "identical": identical,
        "normalized": normalized,
        "drift": drift,
        "missing_local": missing,
    }


# --------------------------------------------------------------------------- #
# Le geste
# --------------------------------------------------------------------------- #


def publish(
    root: pathlib.Path,
    work_tree: pathlib.Path,
    *,
    remote: str = "origin",
    branch: str = DEFAULT_BRANCH,
    url: Optional[str] = None,
    message: Optional[str] = None,
    include_strays: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Export, commit, push, vérification. Rend le rapport complet du geste.

    Ne lève pas : chaque étape qui échoue s'arrête **là**, et le rapport dit
    laquelle — un push refusé par un hook n'est pas une vérification ratée, et
    les confondre ferait chercher au mauvais endroit.
    """
    report: Dict[str, Any] = {
        "root": str(root),
        "work_tree": str(work_tree),
        "remote": remote,
        "branch": branch,
        "dry_run": dry_run,
        "strays_published": include_strays,
        "plan": None,
        "commit": None,
        "push": None,
        "remote_sha": None,
        "head": None,
        "verification": None,
        "ok": False,
        "dry": {},
        "problems": [],
    }

    if not root.is_dir():
        report["problems"].append(f"arbre local introuvable : {root}")
        return report

    if dry_run:
        # Un essai à blanc ne touche à rien : ni clone, ni copie, ni commit. Il ne
        # peut donc pas montrer le diff réel — il montre ce qui serait copié, et
        # c'est déjà ce qui permet de repérer un fichier de trop.
        built = plan(root, work_tree, include_strays=include_strays)
        if built is None:
            report["problems"].append(
                "plan impossible : `git` n'a pas pu lire le dossier de travail "
                f"({work_tree}) — un clone est nécessaire pour connaître le `.gitignore`"
            )
            return report
        report["plan"] = built.as_dict()
        return report

    state = ensure_work_tree(work_tree, url=url, remote=remote, branch=branch)
    report["work_tree_state"] = state
    if state.get("error"):
        report["problems"].append(state["error"])
        return report

    built = plan(root, work_tree, include_strays=include_strays)
    if built is None:
        report["problems"].append("plan impossible : git n'a pas répondu")
        return report
    report["plan"] = built.as_dict()
    applied = apply_plan(built)
    report["copied"] = applied["written"]
    report["pruned"] = applied["pruned"]
    if applied["failed"]:
        names = ", ".join(item["path"] for item in applied["failed"][:MAX_FAILED_NAMES])
        report["problems"].append(
            f"{len(applied['failed'])} écriture(s) impossible(s) : {names}"
        )
        return report

    committed = commit_all(work_tree, branch, message)
    report["commit"] = committed
    if committed["error"]:
        report["problems"].append(committed["error"])
        return report

    pushed = push(work_tree, remote, branch)
    report["push"] = pushed
    if not pushed["ok"]:
        report["problems"].append(pushed["error"] or "push refusé")
        return report

    # À partir d'ici, c'est le **distant** qui est jugé — pas ce qu'on croit avoir
    # envoyé. Le fetch rend la référence telle qu'elle est là-bas.
    remote_head = remote_sha(work_tree, remote, branch)
    report["remote_sha"] = remote_head
    head = git("rev-parse", "HEAD", cwd=work_tree)
    report["head"] = decode(head).strip() if head is not None and head.returncode == 0 else None
    if remote_head is None:
        report["problems"].append(
            f"le distant ne rend pas refs/heads/{branch} : la vérification est impossible"
        )
        return report
    if report["head"] and remote_head != report["head"]:
        report["problems"].append(
            f"le distant porte {remote_head[:12]} alors que le commit publié est "
            f"{report['head'][:12]} : la branche a bougé ailleurs"
        )
        return report

    entries = tree_entries(work_tree, remote_head)
    if entries is None:
        report["problems"].append(f"arbre de {remote_head[:12]} illisible")
        return report
    verification = compare(root, entries, object_format(work_tree))
    report["verification"] = verification
    if verification["drift"]:
        report["problems"].append(
            f"{len(verification['drift'])} fichier(s) de la branche ne sont pas ceux du poste"
        )
    if verification["missing_local"]:
        report["problems"].append(
            f"{len(verification['missing_local'])} fichier(s) publiés n'existent plus au poste"
        )
    report["ok"] = not report["problems"]
    return report


__all__ = [
    "Candidate",
    "DEFAULT_BRANCH",
    "EXCLUDED_DIRS",
    "EXCLUDED_NAMES",
    "EXCLUDED_SUFFIXES",
    "GIT_TIMEOUT",
    "HOOKS_CONFIG_KEY",
    "HOOKS_DIR",
    "Harvest",
    "Plan",
    "STRAY_PREFIXES",
    "STRAY_SUFFIXES",
    "Skipped",
    "LAUNCH_NOISE",
    "apply_plan",
    "blob_id",
    "collect",
    "commit_all",
    "compare",
    "compose_message",
    "decode",
    "default_work_tree",
    "ensure_work_tree",
    "failure",
    "git",
    "fetch_branch",
    "ignored_paths",
    "launch_incident",
    "object_format",
    "plan",
    "publish",
    "push",
    "push_argv",
    "remote_branch_exists",
    "remote_sha",
    "rev_count",
    "run",
    "sha256_of",
    "stray_reason",
    "tracked_files",
    "tree_entries",
]
