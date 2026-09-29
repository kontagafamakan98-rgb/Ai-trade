# Registre de rotation des secrets

Ce dossier contient le **registre de rotation** utilisé par l'audit
fail-closed des secrets (`core/secrets_audit.py`, CLI
`scripts/verify_secrets.py`).

## Contenu

* `secret_rotation.json` — **local à l'environnement**, non versionné
  (voir `.gitignore`). Généré par `python scripts/verify_secrets.py --record`.
* `secret_rotation.example.json` — exemple documentant le schéma.

Le registre ne contient **jamais** de secret en clair : uniquement une
**empreinte** SHA-256 tronquée sur 16 caractères hexadécimaux et la date ISO de
la dernière rotation.

```json
{
  "version": 1,
  "secrets": {
    "WEBHOOK_SECRET": { "fingerprint": "4f9c1a7b2e8d0356", "rotated_at": "2026-01-15" }
  }
}
```

## Cycle de vie

1. **Mise en place** d'un environnement : `python scripts/generate_secrets.py`
   remplit le `.env` **local** sans jamais afficher une valeur (`--check` dit
   seulement ce qui manque), puis `python scripts/verify_secrets.py --record`
   horodate les empreintes.
2. **Pre-deploy** (à chaque déploiement) :
   `python scripts/verify_secrets.py` → sortie `1` = déploiement refusé. Avec
   l'URL du service déployé (`--remote <url>` ou `DEPLOYED_URL`), le même audit
   compare les empreintes publiées par la **production** aux siennes : une valeur
   différente, un secret absent en production ou un service injoignable refusent
   aussi le déploiement. `--require-remote` fait de l'absence de mesure un refus,
   au lieu d'un simple avertissement.
3. **Rotation** (périodique, ou après suspicion de fuite) :
   `python scripts/generate_secrets.py --rotate <NOM>` annonce ce que la rotation
   casse, `--apply` écrit la valeur neuve et **horodate son empreinte** (seulement
   celle-là). Un secret qui vient d'ailleurs (tableau de bord Supabase, BotFather)
   s'horodate avec `--record-only <NOM>`, sans y toucher. Pour `ENCRYPTION_KEY`, la
   rotation passe par l'**anneau** — la clé sortante va dans
   `ENCRYPTION_KEYS_PREVIOUS` — et ne se termine qu'avec
   `python scripts/rotate_encryption_key.py --apply`, qui réécrit les identifiants
   broker encore chiffrés par elle. Le détail — les commandes de génération, ce
que chaque rotation casse, et l'ordre exact des étapes — est dans
`docs/SECRETS.md`, section « Rotation ».

L'audit échoue si un secret n'a pas d'entrée (rotation non enregistrée), si son
empreinte ne correspond plus (secret changé sans rotation enregistrée) ou si sa
rotation date de plus que `SECRET_MAX_AGE_DAYS` (90 jours par défaut).
