#!/usr/bin/env python
"""Vérification **fail-closed** des secrets — à lancer en pre-deploy.

Contrôle, dans cet ordre :

1. la **présence** des secrets requis ;
2. leur **robustesse** (longueur, absence de valeur d'exemple, clé Fernet
   valide, unicité entre usages) ;
3. leur **rotation** via un registre de rotation (`security/secret_rotation.json`)
   qui n'enregistre qu'une empreinte SHA-256 tronquée + la date de rotation ;
4. la **production**, quand son URL est connue : les empreintes publiées par le
   processus déployé (`GET /secrets/fingerprints`) sont comparées une à une aux
   empreintes locales. Sans cette étape, la barrière ne pouvait affirmer que ce
   qu'un `.env` local contient — la valeur posée sur la plateforme, la rotation
   faite d'un seul côté ou le service jamais redémarré ne se voyaient nulle part,
   et le feu vert portait sur autre chose que ce qui tourne en ligne.

Codes de sortie :

* ``0`` : tout est conforme (et, si une URL était fournie, la production
  tourne bien ces valeurs), le déploiement peut continuer.
* ``1`` : au moins une erreur (secret absent, faible, partagé ou périmé ;
  production injoignable ou différente) — **déploiement refusé**.
* ``2`` : mauvaise utilisation (ex. ``--env-file`` introuvable).

Exemples
--------

::

    # Audit standard (lit .env puis l'environnement, ce dernier gagne)
    python scripts/verify_secrets.py

    # Après avoir (re)généré des secrets, enregistre la rotation
    python scripts/verify_secrets.py --record

    # Vérifie aussi ce que tourne le service déployé (aucune valeur ne circule)
    python scripts/verify_secrets.py --remote https://<service>.onrender.com

    # Exige cette vérification : sans service joignable, rien n'est autorisé
    python scripts/verify_secrets.py --require-remote

    # Sortie machine pour la CI / le pipeline de déploiement
    python scripts/verify_secrets.py --json --max-age-days 60
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core import console  # noqa: E402
from core.secrets_audit import (  # noqa: E402
    DEFAULT_DEPLOYED_TIMEOUT,
    DEFAULT_LEDGER_PATH,
    DEFAULT_SPECS,
    DEPLOYED_URL_ENV,
    ERROR,
    apply_deployed_check,
    build_ledger_entries,
    load_effective_env,
    load_ledger,
    resolve_max_age_days,
    run_audit,
    save_ledger,
)

OK_MARK = "\u2705"
KO_MARK = "\u274c"
WARN_MARK = "\u26a0\ufe0f"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verify_secrets.py",
        description="Audit fail-closed des secrets (présence, robustesse, rotation).",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Fichier .env à charger avant l'environnement (défaut : .env). "
        "Les variables d'environnement réelles gagnent sur le fichier.",
    )
    parser.add_argument(
        "--no-env-file",
        action="store_true",
        help="Ignorer complètement le fichier .env et n'utiliser que l'environnement.",
    )
    parser.add_argument(
        "--ledger",
        default=None,
        help=f"Chemin du registre de rotation (défaut : {DEFAULT_LEDGER_PATH}).",
    )
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=None,
        help="Durée de vie maximale d'un secret avant rotation (défaut : 90, "
        "ou $SECRET_MAX_AGE_DAYS).",
    )
    parser.add_argument(
        "--record",
        action="store_true",
        help="Enregistre l'empreinte et la date du jour pour chaque secret "
        "défini (à utiliser juste après une rotation).",
    )
    parser.add_argument(
        "--allow-missing-rotation",
        action="store_true",
        help="Transforme l'absence d'entrée de rotation en avertissement "
        "(utile au tout premier déploiement d'un environnement).",
    )
    parser.add_argument(
        "--skip-if-unconfigured",
        action="store_true",
        help="Si AUCUN des secrets connus n'est défini (ni .env ni environnement), "
        "sortir en 0 après un avertissement, au lieu de signaler tous les secrets "
        "requis comme absents. Destiné au hook pre-push : une machine qui ne détient "
        "aucun secret n'a rien à auditer et ne doit pas être bloquée.",
    )
    parser.add_argument(
        "--scan-repo",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Recherche les valeurs de secrets dans les fichiers du dépôt "
        "(activé par défaut ; désactiver avec --no-scan-repo).",
    )
    parser.add_argument(
        "--scan-root",
        default=None,
        help="Racine à scanner (défaut : racine du projet).",
    )
    parser.add_argument(
        "--remote",
        default=None,
        help="URL du **service déployé** (base ou endpoint complet). Ses empreintes "
        "de secrets sont comparées une à une aux empreintes locales ; une valeur "
        "différente ou un secret absent en production refusent le déploiement. "
        f"Défaut : ${DEPLOYED_URL_ENV}.",
    )
    parser.add_argument(
        "--no-remote",
        action="store_true",
        help=f"Ne pas interroger la production, même si ${DEPLOYED_URL_ENV} est définie "
        "(l'avertissement « production non vérifiée » reste affiché).",
    )
    parser.add_argument(
        "--require-remote",
        action="store_true",
        help="Exiger la vérification de la production : sans URL ou sans service "
        "joignable, le déploiement est refusé au lieu d'être autorisé sur la seule "
        "foi de la configuration locale. À utiliser dans le pipeline de déploiement.",
    )
    parser.add_argument(
        "--remote-timeout",
        type=float,
        default=DEFAULT_DEPLOYED_TIMEOUT,
        help=f"Délai d'attente de la vérification distante, en secondes "
        f"(défaut : {DEFAULT_DEPLOYED_TIMEOUT}).",
    )
    parser.add_argument("--json", action="store_true", help="Sortie JSON uniquement.")
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Pas de sortie texte si tout est conforme ET sans avertissement (un "
        "avertissement, notamment « production non vérifiée », s'affiche toujours).",
    )
    return parser


class UsageError(Exception):
    """Erreur d'invocation (code de sortie 2)."""


def describe_deployed(deployed: "Mapping[str, Any] | None") -> str:
    """Ce qui a été **mesuré** de la production, ou pourquoi rien n'a pu l'être.

    Cette ligne existe pour qu'un lecteur pressé ne confonde pas « conforme »
    avec « conforme en production » : le rapport dit lequel des deux il a établi.
    """
    if not deployed:
        return "non vérifiée (aucune URL fournie)"
    if deployed.get("checked"):
        production = deployed.get("production") or {}
        measured = production.get("checked_at") or "date inconnue"
        matched = len(deployed.get("matched") or [])
        return (
            f"{deployed.get('url')} — {matched} empreinte(s) identique(s) "
            f"(mesure du {measured})"
        )
    reason = deployed.get("reason")
    if reason == "no_url":
        return f"NON VÉRIFIÉE (aucune URL ; `--remote <url>` ou ${DEPLOYED_URL_ENV})"
    if reason == "invalid_url":
        return f"NON VÉRIFIÉE (URL invalide : {deployed.get('url')})"
    if reason == "no_api_key":
        return "NON VÉRIFIÉE (INTERNAL_API_KEY absente ici : impossible de s'authentifier)"
    if reason == "unreachable":
        return f"NON VÉRIFIÉE ({deployed.get('url')} injoignable ou a refusé la clé)"
    return f"NON VÉRIFIÉE ({deployed.get('reason') or 'raison inconnue'})"


def any_secret_configured(values: Mapping[str, str]) -> bool:
    """Vrai dès qu'au moins un secret connu porte une valeur non vide.

    Sert à distinguer « aucun secret configuré » (machine qui ne détient rien)
    de « secrets configurés mais invalides ». Le premier cas n'offre rien à
    auditer : le signaler comme une erreur rendrait tout push impossible sur un
    simple clone, et le hook serait contourné en masse.
    """
    return any((values.get(spec.name) or "").strip() for spec in DEFAULT_SPECS)


def _resolve_env_file(args: argparse.Namespace) -> "str | None":
    if args.no_env_file:
        return None
    path = Path(args.env_file)
    if not path.is_file():
        # Pas de fichier .env : on continue sur l'environnement seul, sauf si
        # l'utilisateur l'a explicitement demandé.
        if args.env_file != ".env":
            raise UsageError(f"fichier .env introuvable : {path}")
        return None
    return str(path)


def main(argv: "list[str] | None" = None) -> int:
    console.make_streams_utf8()
    args = _build_parser().parse_args(argv)
    try:
        env_file = _resolve_env_file(args)
    except UsageError as exc:
        print(f"{KO_MARK} {exc}", file=sys.stderr)
        return 2

    ledger_path = args.ledger or DEFAULT_LEDGER_PATH
    max_age_days = resolve_max_age_days(args.max_age_days)

    values = load_effective_env(env_file)
    ledger = load_ledger(ledger_path)

    if args.skip_if_unconfigured and not any_secret_configured(values):
        if args.json:
            print(json.dumps({"ok": True, "skipped": "unconfigured"}))
        else:
            print(
                f"{WARN_MARK} Aucun secret configuré (ni .env ni environnement) : "
                "rien à auditer — opération autorisée, mais AUCUN contrôle n'a eu lieu."
            )
        return 0

    if args.record:
        entries = build_ledger_entries(values)
        if not entries:
            print(
                f"{KO_MARK} aucun secret défini à enregistrer "
                "(vérifie ton .env / tes variables d'environnement).",
                file=sys.stderr,
            )
            return 2
        ledger["secrets"] = {**ledger.get("secrets", {}), **entries}
        save_ledger(ledger_path, ledger)
        if not args.json:
            print(f"{OK_MARK} rotation enregistrée pour {len(entries)} secret(s) dans {ledger_path} :")
            for name in sorted(entries):
                print(f"   • {name} — {entries[name]['rotated_at']}")
        else:
            print(json.dumps({"recorded": sorted(entries), "ledger": ledger_path}))
        return 0

    scan_root = args.scan_root or str(REPO_ROOT)
    result = run_audit(
        values,
        ledger=ledger,
        max_age_days=max_age_days,
        allow_missing_rotation=args.allow_missing_rotation,
        scan_repo=args.scan_repo,
        repo_root=scan_root,
    )

    # La production est mesurée **après** l'audit local, et jamais à sa place :
    # les deux verdicts restent distincts dans le rapport.
    remote_url = "" if args.no_remote else (args.remote or values.get(DEPLOYED_URL_ENV) or "")
    result = apply_deployed_check(
        result,
        values,
        remote_url,
        require_remote=args.require_remote,
        api_key=values.get("INTERNAL_API_KEY", ""),
        timeout=args.remote_timeout,
    )
    deployed_checked = bool((result.deployed or {}).get("checked"))

    if args.json:
        print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
        return 0 if result.ok else 1

    # `--quiet` veut dire « silence quand il n'y a rien à dire », pas « silence
    # même quand la production n'a pas été mesurée » : un avertissement est
    # précisément ce qu'on ne veut pas rater.
    if result.ok and args.quiet and not result.warnings:
        return 0

    source = env_file or "environnement du processus"
    print(f"Audit des secrets — source : {source}")
    print(f"Registre de rotation : {ledger_path} (rotation max : {max_age_days} j)")
    scan_label = f"activé (racine : {scan_root})" if args.scan_repo else "désactivé"
    print(f"Scan anti-fuite du dépôt : {scan_label}")
    print(f"Production — {describe_deployed(result.deployed)}")
    print(f"Secrets contrôlés ({len(result.checked)}) : {', '.join(result.checked) or '—'}")
    print("")

    if not result.issues:
        verdict = " et la production tourne bien ces valeurs." if deployed_checked else "."
        print(f"{OK_MARK} Tous les secrets sont présents, robustes et à jour{verdict}")
        return 0

    for issue in result.issues:
        mark = KO_MARK if issue.severity == ERROR else WARN_MARK
        print(f"{mark} [{issue.name}] {issue.message}")

    print("")
    if result.ok:
        suite = (
            "La production a été vérifiée."
            if deployed_checked
            else "LA PRODUCTION N'A PAS ÉTÉ VÉRIFIÉE."
        )
        print(
            f"{WARN_MARK} Audit passé avec {len(result.warnings)} avertissement(s). {suite}"
        )
        return 0

    print(
        f"{KO_MARK} {len(result.errors)} erreur(s) — DÉPLOIEMENT REFUSÉ.\n"
        "Corrige les secrets puis, après une rotation intentionnelle, "
        "relance : python scripts/verify_secrets.py --record"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
