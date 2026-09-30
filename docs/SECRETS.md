# Secrets — audit fail-closed & rotation

Ce document décrit la vérification des secrets exécutée **avant chaque
déploiement**. Elle est volontairement **fail-closed** : au moindre problème,
le déploiement est refusé.

## Où c'est implémenté

| Élément | Rôle |
|---|---|
| `core/secrets_audit.py` | Logique pure, sans dépendance externe (stdlib uniquement) |
| `scripts/verify_secrets.py` | CLI **pre-deploy** et **pre-push** (codes de sortie 0/1/2) |
| `scripts/generate_secrets.py` | Remplit le `.env` local **sans jamais afficher une valeur** (`--check` : ce qui manque ; `--rotate` : rotation assistée d'un secret désigné) |
| `scripts/rotate_encryption_key.py` | Termine une rotation d'`ENCRYPTION_KEY` : compte puis réécrit les identifiants chiffrés par une clé retirée |
| `scripts/pre_commit_secrets.py` | CLI **pre-commit** : scan anti-fuite du contenu **indexé** + `--install` |
| `.githooks/pre-commit` | Hook git versionné : bloque une fuite dans un commit |
| `.githooks/pre-push` | Hook git versionné : exige l'audit complet avant diffusion |
| `security/secret_rotation.json` | Registre de rotation, **local à l'environnement** (non versionné) |
| `tests/test_secrets_audit.py` | Tests unitaires (présence, robustesse, rotation, CLI) |
| `tests/test_generate_secrets.py` | Tests du remplissage du `.env` et de la rotation assistée (aucune valeur affichée, rien d'écrasé, un seul secret tourné) |
| `GET /secrets/fingerprints` (`api/reports_router.py`) | Publie les **empreintes** de ce que le processus déployé a chargé — jamais une valeur |
| `tests/test_deployed_secrets_check.py` | Tests de la comparaison local ↔ production, dont une chaîne HTTP réelle |
| `tests/test_encryption_ring.py`, `tests/test_broker_credentials_rotation.py` | Tests de l'anneau (rouvrir l'ancien, chiffrer avec le nouveau) et de la réécriture |
| `tests/test_pre_commit_hook.py` | Tests du scan indexé et du hook |
| `tests/test_pre_commit_hook_e2e.py` | Bout en bout : vrai `git commit` bloqué dans un dépôt jetable |
| `tests/test_pre_push_hook.py` | Bout en bout : vrai `git push` bloqué/accepté selon l'état des secrets |

Le garde-fou de démarrage existant (`core/config_runtime.enforce_secure_config()`)
reste la première barrière ; cet audit le complète en vérifiant en plus la
**robustesse** et la **fraîcheur** (rotation) de l'ensemble des secrets.

## Les cinq contrôles

### 1. Présence

Chaque secret **requis** doit être défini et non vide :

| Secret | Requis | Contrainte |
|---|---|---|
| `WEBHOOK_SECRET` | ✅ | ≥ 16 caractères |
| `INTERNAL_API_KEY` | ✅ | ≥ 16 caractères |
| `ENCRYPTION_KEY` | ✅ | clé Fernet valide (base64 urlsafe de 32 octets) |
| `SUPABASE_SERVICE_KEY` | ✅ | ≥ 20 caractères |
| `TELEGRAM_BOT_TOKEN` | ✅ | format `<id>:<secret>` |
| `GEMINI_API_KEY` / `GROQ_API_KEY` / `FINNHUB_API_KEY` | ⛔ | facultatif, mais validé si fourni |
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | ⛔ | facultatif, mais validé si fourni |

### 2. Robustesse / unicité

Rejeté si :

- la valeur est un **placeholder** connu (`change-me...`, `your-...`, `xxxx`,
  `REMPLACE...`, etc.) ou une valeur d'exemple de `.env.example` ;
- la valeur compte moins que la longueur minimale ;
- l'entropie est trop faible (moins de 8 caractères distincts) ;
- deux secrets **partagent la même valeur** (ex. `INTERNAL_API_KEY` ==
  `WEBHOOK_SECRET`) ;
- `ENCRYPTION_KEY` n'est pas une clé Fernet structurellement valide.

### 3. Rotation

Un registre (`security/secret_rotation.json`) associe à chaque secret :

```json
{ "fingerprint": "4f9c1a7b2e8d0356", "rotated_at": "2026-01-15" }
```

- **empreinte** = `sha256("ai-trade-secret-ledger:v1|<NOM>|<valeur>")[:16]` —
  le secret lui-même n'est jamais stocké ;
- **`rotated_at`** = date ISO de la dernière rotation.

L'audit échoue si :

- aucun enregistrement n'existe pour un secret déclaré (**rotation non
  enregistrée**) ;
- l'empreinte enregistrée **diffère** de la valeur courante (**secret modifié
  sans rotation enregistrée**) ;
- la rotation date de plus de `SECRET_MAX_AGE_DAYS` jours (**rotation en
  retard**, 90 jours par défaut).

Ce plafond est lu dans `SECRET_MAX_AGE_DAYS` (ou `--max-age-days`). Une valeur
illisible (`90j`, l'unité recopiée par habitude), ou non positive (`0`), ne
passe plus en silence : l'audit applique son repli **et le dit** (`[SECRET_MAX_AGE_DAYS]
valeur illisible…`). Sans quoi il rendait exactement le même verdict qu'une
machine sans réglage, alors que c'est ce plafond qui décide du refus.

### 4. Détection de fuite (scan du dépôt)

Chaque valeur de secret configurée est recherchée **verbatim** dans les fichiers
du dépôt (fichiers suivis par git si le dépôt en est un, sinon parcours de
l'arborescence en excluant `.venv`, `build`, `artifacts`, `__pycache__`, etc.).

* Toute occurrence est signalée comme **erreur** : `secret retrouvé verbatim
dans <fichier>:<ligne>` ;
* la valeur est **masquée** dans le rapport (`whse…8a`) — un rapport de fuite
  ne doit jamais ré-écrire le secret en clair ;
* les valeurs factices (`change-me…`) et les valeurs trop courtes (< 8 car.)
  sont ignorées pour éviter les faux positifs ;
* les fichiers binaires, les caches et les `.env` locaux ne sont pas scannés,
  et la liste des fichiers suivis est résolue via `git ls-files` quand c'est
  possible (sinon repli sur un parcours filtré).

Actif par défaut ; désactivable avec `--no-scan-repo` (utile quand l'orchestrateur
injecte des secrets factices directement dans un fichier du dépôt — c'est le cas
de la CI, dont le workflow contient ses propres valeurs de test).

### 5. La configuration **réellement déployée**

Les quatre contrôles ci-dessus portent sur **cette machine**. Un `.env` irréprochable
ne dit rien de ce que tourne le service déployé : variable jamais posée sur la
plateforme, rotation faite d'un seul côté, ou service jamais redémarré donnent tous
les trois un feu vert parfaitement honnête… sur la mauvaise configuration.

Quand une URL est fournie (`--remote <url>`, ou `DEPLOYED_URL`), la barrière
interroge `GET /secrets/fingerprints` du service déployé et compare, secret par
secret, l'empreinte publiée à la sienne :

| Réponse | Empreinte locale | Verdict |
|---|---|---|
| présente | identique | apparié — la production tourne bien cette valeur |
| présente | différente | **erreur** : « la production tourne une valeur DIFFÉRENTE » |
| absente | présente | **erreur** si le secret est requis (« ABSENT du processus déployé »), avertissement s'il est facultatif |
| présente | absente | **erreur** pour un secret requis (rien ici ne peut juger sa robustesse ni sa fraîcheur), avertissement s'il est facultatif |
| présente | — (nom inconnu ici) | avertissement : le code déployé est plus récent que ce dépôt |

Trois points font que cette comparaison ne peut pas mentir :

* ce qui circule est une **empreinte salée tronquée** (16 hexadécimaux), jamais une
  valeur — et la clé interne voyage en en-tête, jamais dans l'URL. L'endpoint ne
  peut donc pas servir à lire un secret : il est protégé par la même clé interne
  que le reste de la surface privée (503 sans elle, 401 si elle est fausse) ;
* le service publie le **sel** et la **version** de son schéma de rapport. Un code
  déployé d'une autre révision est **refusé** (« le code déployé n'est pas celui-ci »)
  au lieu de produire un écart de valeurs qui enverrait chercher la faute au mauvais
  endroit ;
* une URL fournie mais **injoignable** est une **erreur**, pas un repli silencieux
  sur le local : on a demandé à mesurer la production, rien n'a été mesuré. Sans URL
  du tout, l'audit reste vert mais l'écrit en clair (« la production n'a PAS été
  vérifiée ») — et `--require-remote` transforme cette absence en refus.

## Utilisation en pre-deploy

```bash
# 1. Contrôle standard (le .env est chargé puis l'environnement gagne)
python scripts/verify_secrets.py

# 2. Sortie machine pour le pipeline (JSON) et politique de 60 jours
python scripts/verify_secrets.py --json --max-age-days 60

# 3. Après une rotation légitime : ré-enregistrer les empreintes/dates
python scripts/verify_secrets.py --record

# 4. Tout premier déploiement d'un environnement (tolère l'absence initiale)
python scripts/verify_secrets.py --allow-missing-rotation

# 5. Désactiver le scan anti-fuite du dépôt (activé par défaut)
python scripts/verify_secrets.py --no-scan-repo

# 6. Vérifier aussi ce que tourne le service déployé (aucune valeur ne circule)
python scripts/verify_secrets.py --remote https://<service>.onrender.com

# 7. Exiger cette vérification : sans service joignable, rien n'est autorisé
python scripts/verify_secrets.py --remote https://<service>.onrender.com --require-remote

# 8. Ne pas interroger la production, même si DEPLOYED_URL est définie
python scripts/verify_secrets.py --no-remote
```

`DEPLOYED_URL` peut remplacer `--remote` (elle vit dans le `.env` local, et gagne
l'environnement du processus). C'est le seul réglage de cette page qui ne soit pas
un secret : elle se pose donc sans précaution particulière, et se met dans les
**variables** du dépôt pour un pipeline.

Codes de sortie :

| Code | Signification |
|---|---|
| `0` | Conforme — et, si une URL est fournie, la production tourne bien ces valeurs : le déploiement peut continuer |
| `1` | Au moins une erreur (secret local absent/faible/périmé, production injoignable ou différente) — **déploiement refusé** |
| `2` | Mauvaise utilisation (ex. `--env-file` introuvable) |

## Utilisation en pre-commit (blocage avant le commit)

Le scan du dépôt ci-dessus s'exécute en **pre-deploy**. Pour empêcher qu'une
valeur parte dans l'historique, un second contrôle tourne au **`git commit`**,
sur le **contenu indexé** uniquement :

```bash
python scripts/pre_commit_secrets.py --install   # une fois par clone
```

`--install` exécute simplement `git config core.hooksPath .githooks`, ce qui
active **les deux** hooks versionnés (`pre-commit` **et** `pre-push`). Un hook
versionné ne s'active pas tout seul : c'est le compromis pour ne pas dépendre du
framework `pre-commit` (aucune dépendance, aucun réseau).

### Pourquoi il lit l'index, pas la copie de travail

On analyse le contenu du **blob indexé** (`git cat-file blob :<chemin>`) : après
un `git add -p`, ce qui partira dans le commit n'est pas forcément ce que montre
l'éditeur. C'est l'index qui fait foi.

Trois différences assumées avec le scan du dépôt :

| Cas | Scan du dépôt | Scan des fichiers indexés |
|---|---|---|
| `.env` local | ignoré (c'est la *source* des secrets) | **analysé** — `git add -f .env` doit être bloqué |
| Répertoire exclu (`.venv`, `build`…) | ignoré | analysé si indexé : il sera commité |
| Fichier supprimé | — | hors périmètre (`--diff-filter=ACMR`) |

### Ce que le hook ne fait pas (volontairement)

Il ne vérifie **ni la présence, ni la robustesse, ni la rotation**. Ces contrôles
dépendent de l'environnement (registre de rotation local, secrets injectés par
la plateforme) : les exécuter à chaque commit bloquerait toute machine non
configurée, et un hook qu'on contourne ne protège plus rien. Le scan anti-fuite,
lui, ne dépend d'aucun secret côté serveur.

Ces contrôles sont exigés **au push** (section suivante). Pour les demander
ponctuellement dès le commit :

```bash
python scripts/pre_commit_secrets.py --strict-rotation
```

### Sortie

```
❌ [WEBHOOK_SECRET] fuite : valeur retrouvée verbatim dans config.py:1 (masquée whse…18)

❌ COMMIT REFUSÉ : 1 fuite(s) de secret dans les 1 fichier(s) indexé(s).
```

La valeur est **masquée** dans le rapport : un rapport de fuite qui ré-écrit le
secret en clair serait une seconde fuite. Codes de sortie : `0` conforme, `1`
fuite détectée (commit refusé), `2` mauvaise invocation — et `3` quand le
**wrapper** n'a pas pu démarrer son outillage (voir « Contrat de sortie des
wrappers » plus bas).

⚠️ **Sans secret configuré, le hook ne vérifie rien.** Il l'annonce
explicitement (`aucune valeur de référence…`) plutôt que de laisser croire à un
contrôle effectif : sans `.env` ni variables d'environnement, il n'a aucune
valeur à rechercher.

Ce trou-là est **local par construction** — le refuser ici rendrait le hook
inutilisable, donc contourné — et il se ferme en CI : le job `secrets-audit`
relit l'arbre de la branche poussée avec les **vraies** valeurs, et refuse un
scan qui n'aurait rien à chercher (voir « Le gate de CI » plus bas).

## Utilisation en pre-push (gate avant diffusion)

Le pre-commit empêche d'**introduire** une fuite ; le pre-push exige que les
secrets soient **sains** avant de diffuser le dépôt. À chaque `git push`, le
wrapper `.githooks/pre-push` lance l'audit **complet** — présence, robustesse,
rotation et scan anti-fuite — via `scripts/verify_secrets.py`. Un secret absent,
faible, dupliqué, modifié sans `--record`, ou dont la rotation date de plus de
90 jours **refuse le push**.

### Machine sans aucun secret configuré

Le hook passe `--skip-if-unconfigured` : si **aucun** secret connu n'est défini
(ni `.env`, ni environnement), il l'annonce et laisse passer — un clone qui ne
détient aucun secret n'a rien à auditer, et un hook qui bloquerait tout le monde
serait contourné en masse. Pour exiger le gate malgré tout :

```bash
SECRETS_HOOK_STRICT=1 git push
```

### Et la production, dans le hook ?

Le hook lance l'audit **tel quel** : si `DEPLOYED_URL` est renseignée dans ton
`.env`, un `git push` interroge donc aussi le service déployé — et un service
injoignable refuse le push, comme il refuse un déploiement. C'est cohérent (on a
demandé la mesure, la mesure a échoué), mais c'est un aller-retour réseau à chaque
push : si tu préfères garder le hook purement local, lance-le explicitement avec
`--no-remote`, ou garde `DEPLOYED_URL` hors du `.env` et ne la passe qu'en option
(`--remote <url>`) le jour du déploiement. Le hook n'impose ni l'un ni l'autre :
la vérification de la production appartient au pipeline, pas au poste de travail.

### Contournement

`git push --no-verify` saute le hook. L'audit **fail-closed** reste exécuté en
pre-deploy : c'est lui la barrière de sécurité, les hooks ne sont que le filet
local qui fait échouer tôt.

```
pre-push : audit des secrets (présence, robustesse, rotation)…
❌ [ENCRYPTION_KEY] rotation non enregistrée — lance `--record` après la première mise en place du secret

❌ 5 erreur(s) — DÉPLOIEMENT REFUSÉ.
```

### Contrat de sortie des wrappers `.githooks/*`

Les deux wrappers publient le même contrat : les trois codes de l'outil qu'ils
délèguent, plus un qu'il n'émet jamais.

| Code | Signification |
|---|---|
| `0` | L'outillage a tourné et a conclu : rien à refuser |
| `1` | L'outillage a tourné et a conclu : **refus** |
| `2` | Invocation invalide |
| `3` | L'outillage **n'a pas pu démarrer** : rien n'a été contrôlé |

`3` existe pour qu'un incident d'OS — fork MSYS2 saturé, interpréteur Python
introuvable, script de l'outil absent — ne puisse plus se lire comme un refus.
Le wrapper **ne laisse plus passer en silence** quand il n'a rien pu lancer : il
l'annonce et sort en `3`, avec le mot `NON DÉMARRÉ` dans son message. Sous git,
cet incident **bloque** le commit ou le push, exactement comme un refus.

Deux limites à connaître :

* `git commit` et `git push` **écrasent à `1`** le code de leurs hooks, quelle
  qu'en soit la valeur. La distinction ne survit donc que si le wrapper est
  appelé directement — c'est ainsi que `tests/test_hook_tooling_contract.py` la
  vérifie, sans lire la sortie ;
* pour ne jamais la perdre à travers git, le wrapper écrit toujours laquelle des
  quatre issues a eu lieu.

## Intégration pipeline

```yaml
# Barrière locale seule (la CI ne peut pas, à elle seule, prouver la production :
# elle audite des valeurs factices). C'est le repli quand aucun service déployé
# n'est déclaré ; l'audit l'affiche comme « production non vérifiée ».
- name: Audit des secrets
  run: python scripts/verify_secrets.py --max-age-days 90

# Barrière complète : exige que le service déployé tourne **ces** valeurs. À
# brancher là où l'audit dispose des vraies valeurs (pipeline de déploiement) ou
# d'une machine qui les détient, la CI ne détenant que des valeurs factices.
- name: Secrets et production
  run: python scripts/verify_secrets.py --require-remote --max-age-days 90
  env:
    DEPLOYED_URL: ${{ vars.DEPLOYED_URL }}
```

`SECRET_MAX_AGE_DAYS` et `SECRET_ROTATION_LEDGER` peuvent aussi être définis
par variables d'environnement au lieu des options CLI.

### Le gate de CI — `secrets-audit`

Le job `secrets-audit` (`.github/workflows/ci.yml`) existe pour une seule chose :
fermer le silence d'un poste **sans `.env`**. Le hook pre-commit ne cherche que
ce qu'il **connaît** ; sans valeur de référence il l'annonce et laisse passer, et
c'est volontaire. Le job, lui, dispose des valeurs et relit l'**arbre entier** de
la branche poussée :

* sur `push` **uniquement** — une PR venue d'un fork n'a pas accès aux secrets du
dépôt, et un job qui exige des valeurs qu'il ne peut pas recevoir échouerait pour
tout le monde (or un gate qu'on apprend à ignorer ne contrôle rien) ; les PR
d'une branche du dépôt sont couvertes par le `push` de cette branche ;
* `--require-scan-targets` transforme « le scan n'avait rien à chercher » en
**refus** : un scan sans cible ne prouve rien, et ce silence-là ne doit jamais se
lire comme « aucune fuite » ;
* `--allow-missing-rotation` : le job juge l'arbre poussé, pas la fraîcheur des
rotations — le registre est propre à la machine (et gitignoré). La rotation garde
son contrôle local, au push.
* le rapport **écrit son périmètre** : la ligne du scan donne les valeurs
cherchées, les fichiers **lus** et ceux écartés **par politique** (`.env` locaux,
répertoires exclus, suffixes non analysables, fichiers vides), et la ligne du
registre en donne la synthèse (entrées, plus ancienne et son âge, dates
illisibles). Un « rien trouvé » se lit donc avec son domaine de validité — sans
ouvrir le JSON, que `--json` porte en entier.

Les valeurs sont lues dans les **secrets du dépôt** (`Settings → Secrets and
variables → Actions`) :

| Secret du dépôt | Rôle dans ce job |
|---|---|
| `WEBHOOK_SECRET`, `INTERNAL_API_KEY`, `ENCRYPTION_KEY`, `SUPABASE_SERVICE_KEY`, `TELEGRAM_BOT_TOKEN` | les cinq **obligatoires** : sans eux l'audit refuse (présence) et le scan n'a aucune valeur de référence |
| `GEMINI_API_KEY`, `FINNHUB_API_KEY`, `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` | facultatifs : présents, ils élargissent le scan ; absents, ils ne bloquent rien |

Tant qu'ils ne sont pas posés, le job **échoue** : c'est voulu — un gate qui ne
peut pas mesurer ne rend pas un feu vert. Un secret écrit dans le workflow, lui,
serait publié avec le dépôt ; d'où les secrets de dépôt, et jamais un `.env`
commité.

Aucune dépendance n'est installée : l'audit ne tient qu'à la bibliothèque
standard, exprès, pour tourner même si `requirements.txt` est cassé.

## Rotation — la procédure, secret par secret

### Ce que la barrière mesure vraiment

`verify_secrets.py` lit le `.env` **local**, puis l'environnement du processus (ce
dernier gagne). C'est ce qu'il peut lire **tout seul**, et c'est pourquoi le
résultat nomme sa source : un `.env` irréprochable ne prouve rien de ce que tourne
le service déployé.

Ce trou-là se ferme avec `--remote <url>` (ou `DEPLOYED_URL`) : les empreintes
publiées par le **processus déployé** (`GET /secrets/fingerprints`) sont comparées
une à une aux empreintes locales, et une valeur différente, un secret absent d'un
côté ou un code d'une autre révision **refusent** le déploiement. Une URL fournie
mais injoignable refuse aussi. Sans URL, l'audit reste autorisé mais écrit
« la production n'a PAS été vérifiée » ; `--require-remote` — ce que doit utiliser
un vrai pipeline de déploiement — transforme cette absence en refus.

Le `.env` local et les variables de déploiement doivent donc avancer **ensemble**,
dans la même fenêtre : c'est la première cause de feu vert trompeur, et quand
l'URL est connue la barrière la **vérifie** au lieu de la supposer.

Une valeur ne vit qu'à deux endroits : le `.env` local (`.gitignore`, ligne 2) et
les variables de l'environnement de déploiement. Jamais dans un fichier suivi,
jamais dans un test, jamais dans un document, jamais dans un message.

### Générer

Le chemin normal est le script : il remplit le `.env` **local** sans jamais
afficher une valeur, et nomme celles qui ne se génèrent pas ici.

```bash
python scripts/generate_secrets.py            # remplit ce qui manque dans .env
python scripts/generate_secrets.py --check    # ne dit que ce qui manque (sortie 1)
```

Il ne **remplace jamais** une valeur existante qui n'est pas un exemple
(`change-me…`) : une valeur faible est signalée, pas écrasée — la remplacer est
une rotation, avec ce qu'elle coûte, et une rotation se **demande**.

### Tourner un secret — `--rotate`

C'est le mode qui écrit une valeur neuve **là où il y en avait déjà une**, et il
n'y a qu'un seul chemin pour le faire sans se raconter d'histoires : annoncer
d'abord ce que la rotation casse, exécuter ensuite.

```bash
# 1. ce que chaque rotation casse (aucune écriture) : l'état actuel de chaque secret
#    (empreinte, « vide dans le fichier », « absent du fichier » — les mots de --check),
#    d'où vient sa valeur neuve, et ce qu'elle coûte
python scripts/generate_secrets.py --rotate

# 2. l'annonce d'un secret précis : empreinte actuelle, conséquences, étapes suivantes
python scripts/generate_secrets.py --rotate WEBHOOK_SECRET

# 3. l'exécution : valeur neuve écrite dans .env (jamais affichée) + empreinte horodatée
python scripts/generate_secrets.py --rotate WEBHOOK_SECRET --apply

# 4. la variante (déconseillée) : tourner sans horodater — l'audit refusera ensuite,
#    c'est l'ordre « horodater après le redémarrage »
python scripts/generate_secrets.py --rotate WEBHOOK_SECRET --apply --no-record

# 5. un secret qui vient d'ailleurs : la valeur est déjà collée dans .env, on l'horodate
python scripts/generate_secrets.py --record-only SUPABASE_SERVICE_KEY
```

Trois décisions valent d'être lues :

* **rien n'est inventé.** Seuls `WEBHOOK_SECRET`, `INTERNAL_API_KEY` et
  `ENCRYPTION_KEY` se tournent ici. `SUPABASE_SERVICE_KEY` et `TELEGRAM_BOT_TOKEN`
  sont **refusés** avec leur source (tableau de bord, BotFather) : un jeton fabriqué
  ferait échouer la première requête authentifiée, très loin de la ligne fautive.
  Pour ceux-là, la sortie de secours est `--record-only NOM` — l'horodatage de la
  valeur déjà collée, sans y toucher ;
* **l'horodatage est ciblé.** `--rotate … --apply` n'enregistre que le secret tourné
  (et l'anneau quand il suit). `python scripts/verify_secrets.py --record` enregistre
  **tous** les secrets définis, donc fait repartir leur compte à 90 jours — tourner
  un secret ne doit pas repousser l'échéance des autres ;
* **`ENCRYPTION_KEY` passe par l'anneau**, jamais par un remplacement sec : la
  nouvelle clé prend la version au-dessus (v2, puis v3…), la sortante est recopiée
  en tête de `ENCRYPTION_KEYS_PREVIOUS`, et le rapport dit ce qui rouvre encore
  l'ancien chiffré. Une rotation est **refusée** si la clé active est illisible (elle
  ne peut pas être recopiée dans l'anneau) ou si sa version y est déjà (deux clés
  pour une version rendraient l'ouverture indécidable). Le chiffré, lui, se réécrit
  avec `scripts/rotate_encryption_key.py` — ce script ne touche pas la base.

Ce que le mode **ne** fait pas : poser la valeur sur la plateforme (aucun outil ne
sait le faire à ta place sans son API), ni redémarrer le service. Il imprime l'ordre
exact à suivre — et l'étape qui **prouve** le redémarrage :
`python scripts/verify_secrets.py --remote <url> --require-remote` → `0`.

Le tableau ci-dessous reste la référence de ce que le script fait, et la sortie de
secours si on préfère la main (ou si le `.env` vit sur une autre machine) :

| Secret | Commande / source | Contrainte |
|---|---|---|
| `WEBHOOK_SECRET` | `python -c "import secrets; print(secrets.token_urlsafe(32))"` | ≥ 16 car., **≠** clé interne |
| `INTERNAL_API_KEY` | la même commande, un **second** tirage | ≥ 16 car., **≠** webhook |
| `ENCRYPTION_KEY` | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` | clé Fernet valide (44 car.), préfixe `vN:` facultatif |
| `ENCRYPTION_KEYS_PREVIOUS` | les clés **retirées** de l'anneau (celles d'avant), recopiées à la main | liste de clés Fernet `vN:<clé>`, une par version |
| `SUPABASE_SERVICE_KEY` | tableau de bord Supabase → *Settings → API Keys* : clé **`sb_secret_…`** (nouvelle génération), ou l'ancienne `service_role` | ≥ 20 car., **jamais** l'`anon` ni une `sb_publishable_` |
| `TELEGRAM_BOT_TOKEN` | BotFather → `/mybots` → *API Token* (`/revoke` **seulement** pour tourner) | forme `<id>:<secret>` |

Trois surfaces retiennent une valeur malgré vous : l'**historique du shell** (un
`config:set KEY=valeur` y reste), le **scrollback** du terminal, et le
presse-papier d'une session partagée ou enregistrée. D'où la règle : préférer le
**tableau de bord** de la plateforme à son CLI, et coller la valeur directement
dans `.env` — elle ne traverse alors aucun shell. C'est la raison d'être du script
ci-dessus : pour les trois secrets locaux, aucune valeur ne touche un écran ; les
deux autres n'ont pas le choix, et viennent d'un tableau de bord quel qu'il soit.

### Ce que chaque rotation coûte

Ce tableau est la **source** de ce que `--rotate` annonce : le même texte est
porté par `ROTATION_IMPACT` dans `core/secrets_audit.py`, et un test exige qu'il
couvre tous les secrets — un secret ajouté au code ne peut donc pas se tourner
sans que sa conséquence soit écrite.

| Secret | Ce qui casse si on tourne sans préparation |
|---|---|
| `ENCRYPTION_KEY` | **rattrapable, mais pas à moitié.** La clé **active** chiffre `api_key_enc` / `api_secret_enc` de `user_broker_credentials` (`database/broker_credentials.py`) ; les clés **retirées** de `ENCRYPTION_KEYS_PREVIOUS` les rouvrent (`utils/encryption.py`). Une rotation ne perd donc rien **tant que l'ancienne clé reste dans l'anneau**. Le piège a changé de forme sans disparaître : la retirer **trop tôt** rend illisibles les lignes qu'elle chiffrait — et ces ordres-là sont désormais **refusés** (`BrokerCredentialsUnreadable`), plus jamais exécutés sur le compte partagé. `python scripts/rotate_encryption_key.py` dit combien de lignes restent avant qu'on puisse la retirer. |
| `SUPABASE_SERVICE_KEY` | **coupure nette.** L'ancienne clé meurt dès la rotation dans le tableau de bord ; le worker échoue à la première lecture (médias, index, réglages). Aucune fenêtre de recouvrement : la rotation et le redémarrage doivent tomber dans le même geste. |
| `TELEGRAM_BOT_TOKEN` | **coupure nette.** `/revoke` tue l'ancien jeton aussitôt : la boucle de `main.py` et l'envoi des alertes (`notifications/notify.py`) s'arrêtent. Le worker ne plante pas, il devient muet. Un webhook Telegram éventuel est à ré-enregistrer. |
| `WEBHOOK_SECRET` | Comparé **une fois**, en temps constant, dans `api/webhook.py` : aucune liste « ancien secret encore accepté ». L'émetteur externe doit suivre dans la même fenêtre, sinon ses signaux partent en 401 et n'arrivent jamais. |
| `INTERNAL_API_KEY` | La moins chère : protège les endpoints internes (`api/security.py`), en fail-closed (503 si absente, 401 si fausse). Son appelant n'est pas compilé — l'écran d'administration de l'app en fait un **champ de saisie** — donc **aucune publication d'application** n'est nécessaire : quelqu'un la ressaisit. |

### `SUPABASE_SERVICE_KEY` — les deux générations de clés

La clé `service_role` **legacy** n'est plus tournable : Supabase documente qu'il
est « no longer possible to rotate the legacy anon, service and JWT secrets ».
La rotation passe donc par une clé de la **nouvelle génération**, `sb_secret_…`,
créée dans *Settings → API Keys*, posée partout, puis l'ancienne **désactivée**
(désactivation réversible, contrairement à une révocation).

L'ordre n'est pas indifférent : **le client doit accepter la clé neuve avant
qu'elle soit posée**. C'est le cas depuis `database/supabase_client.py`
(`build_supabase_client`) — sans quoi `supabase-py` refuse une clé qui n'a pas la
forme d'un JWT (`Invalid API key`), et comme ce module avale l'exception, la base
paraîtrait **vide** : un symptôme qui ressemble à « rien à lire ». Ce que la
correction change sur le fil :

| Génération | `apikey` | `Authorization` |
|---|---|---|
| `service_role` (JWT) | la clé | `Bearer <clé>` — sa place d'origine |
| `sb_secret_…` (opaque) | la clé | **absente** : la passerelle y attend un JWT d'**utilisateur**, pas une clé opaque |

`core/config_runtime.supabase_key_role` lit le préfixe, donc `/preflight` publie
`service_role` pour une clé `sb_secret_…` — le rôle reste lisible à l'écran. La
preuve que rien ne part avec un `Authorization` en trop est faite par
`tests/test_supabase_opaque_keys.py`, sur la requête **préparée pour l'envoi**.

Trois réponses, et il importe de ne pas les confondre : `""` (pas de clé) ;
`service_role` / `anon` (rôle lu) ; **`unreadable`** (une clé est là, mais ni JWT
`a.b.c`, ni `sb_secret_`/`sb_publishable_`). Cette troisième n'est pas un feu
vert : le serveur la refusera. Elle était confondue avec `""` — donc une clé
collée de travers passait pour une absence, et `supabase_issues()` ne la
signalait pas. `scripts/check_supabase.py` la refuse désormais explicitement, au
lieu de l'afficher comme « rôle service_role ».

### `ENCRYPTION_KEY` — l'anneau, et la fin de la rotation

Cette clé n'est plus seule : `ENCRYPTION_KEY` **chiffre**, et
`ENCRYPTION_KEYS_PREVIOUS` — les clés retirées — **rouvre** l'ancien chiffré. Le
jeton écrit porte sa version (`v2:<jeton>`), donc on sait toujours quelle clé a
servi : « reste-t-il des lignes à tourner ? » se lit au lieu de se deviner.

Le même geste, en une commande, sans qu'aucune clé ne passe par l'écran :
`python scripts/generate_secrets.py --rotate ENCRYPTION_KEY --apply` — elle prend la
version au-dessus de l'anneau, recopie la sortante en tête, et horodate les deux
empreintes. Les commandes ci-dessous restent la version manuelle (et la référence de
ce que le script fait) :

```bash
# 1. la clé neuve (v2) ; l'ancienne passe dans l'anneau, elle rouvre l'existant
ENCRYPTION_KEY="v2:$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
ENCRYPTION_KEYS_PREVIOUS="v1:<l'ancienne clé, celle qui chiffrait avant>"

# 2. ce qu'il reste à tourner — lecture seule, aucune valeur affichée
python scripts/rotate_encryption_key.py

# 3. réécrire les lignes avec la clé active (une ligne est réécrite entièrement, ou pas du tout)
python scripts/rotate_encryption_key.py --apply

# 4. une fois qu'il ne reste rien : retirer v1 de l'anneau, puis horodater
python scripts/verify_secrets.py --record
```

Une clé **nue** vaut `v1:` : les `.env` d'avant continuent de fonctionner sans
être réécrits, et leurs jetons — écrits sans version — sont essayés contre chaque
clé de l'anneau, puis signalés comme *à tourner*.

Trois choses valent la peine d'être dites :

* **rien n'est inventé** : une entrée illisible, une version écrite deux fois, une
  clé de l'anneau identique à la clé active, ou une entrée qui revendique la
  version active font **refuser le déploiement** (`core/secrets_audit.py`). Une
  clé retirée mal recopiée ne se plaindrait qu'au premier ordre refusé, c'est-à-dire
  trop tard ;
* **une clé retirée reste un secret** : elle rouvre l'ancien chiffré, donc elle est
  traquée comme les autres — le scan cherche son **matériau** (la clé), pas la
  ligne du `.env` qui la porte ;
* **on ne tourne plus à l'aveugle** : un identifiant que l'anneau ne rouvre pas
  lève `BrokerCredentialsUnreadable`, l'ordre est **refusé**, et le compte partagé
  n'est jamais utilisé à la place (`execution/order_executor.py`). Avant, ce cas
  rendait `None` — indiscernable de « aucun compte connecté » — et l'ordre partait
  sur le compte du propriétaire, en silence.

La **ressaisie** reste la sortie de secours si une clé est irrécupérable : chaque
utilisateur reconnecte son courtier (`/connect_broker`), et les lignes orphelines
sont celles qui ne se rouvrent pas — `rotate_encryption_key.py` les nomme.

Ce que la CI utilise (`AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=`) est une clé
d'exemple **publique** : elle ne doit jamais être la clé de production.

### L'ordre exact

**Première mise en place** — le registre est vide, rien ne tourne encore :

1. réunir les cinq valeurs (trois générées, deux obtenues) ;
2. les écrire dans le `.env` **local** ;
3. `python scripts/verify_secrets.py --record` — enregistre empreintes et dates ;
4. `python scripts/verify_secrets.py` → doit sortir `0` : c'est la barrière ;
5. poser les **mêmes** valeurs en variables de la plateforme ;
6. déployer, **puis** vérifier la production (« Vérifier la production »
   ci-dessous) : au tout premier déploiement, il n'y a encore rien à interroger,
   donc l'audit local est la seule mesure possible.

**Rotation** — le secret est déjà en service :

1. décider **quoi** tourne, en lisant ce que la rotation casse :
   `python scripts/generate_secrets.py --rotate <NOM>` (annonce, aucune écriture) ;
2. générer la nouvelle valeur :
   `python scripts/generate_secrets.py --rotate <NOM> --apply` — le `.env` local est
   modifié et l'empreinte horodatée dans la foulée. Pour `ENCRYPTION_KEY`, l'anneau
   suit tout seul (nouvelle version, clé sortante conservée), et la rotation se
   **termine** par `python scripts/rotate_encryption_key.py --apply`. Pour
   `SUPABASE_SERVICE_KEY` et `TELEGRAM_BOT_TOKEN`, la valeur vient d'ailleurs : on
   la colle dans le `.env` puis on l'horodate avec `--record-only <NOM>` ;
3. la poser sur la plateforme dans le même geste (la valeur locale **est** la
   neuve : c'est elle qu'il faut recopier) ;
4. redémarrer tout de suite — pour `SUPABASE_SERVICE_KEY` et
   `TELEGRAM_BOT_TOKEN`, l'ancienne valeur est **déjà morte** : la fenêtre entre
   le tableau de bord et le redémarrage est une panne ;
5. `python scripts/verify_secrets.py --record` si — et seulement si — l'étape 2 a
   été faite **sans** horodatage (`--no-record`), ou pour les secrets tournés à la
   main dans le tableau de bord. Toute modification de l'anneau compte comme une
   rotation : le registre la voit, et refuse un déploiement dont l'empreinte a
   changé sans enregistrement ;
6. `python scripts/verify_secrets.py` → `0`, puis
   `python scripts/verify_secrets.py --remote <url> --require-remote` → `0` :
   la seconde commande **prouve** que le processus redémarré tourne la nouvelle
   valeur (l'empreinte publiée est celle du `.env`), au lieu de le supposer.

L'ordre n'est pas le même dans les deux cas, et c'est délibéré : en rotation,
`--record` **suit** le déploiement. Enregistrer avant ferait dire au registre
« c'est tourné » alors que la production tourne encore sur l'ancienne valeur. Avec
une URL, la barrière le **verrait** et refuserait (l'empreinte publiée par le
processus n'a pas encore changé) ; sans URL, l'écart d'empreinte fait refuser le
déploiement suivant. L'erreur se paie donc en refus, jamais en silence — et la
seule façon d'obtenir un `0` complet est que les deux côtés portent la même valeur.

### Vérifier la production

Le processus déployé publie l'empreinte de ce qu'il a chargé, jamais une valeur :

```bash
# ce que le service a réellement chargé (nom, empreinte, version de l'anneau)
curl -s -H "X-API-Key: $INTERNAL_API_KEY" https://<service>/secrets/fingerprints

# la barrière compare ces empreintes aux siennes, et refuse tout écart
python scripts/verify_secrets.py --remote https://<service> --require-remote
```

Trois cas à connaître :

* **au premier déploiement**, il n'y a rien à interroger : l'audit local reste la
  seule mesure possible, et il le dit. `--require-remote` échouerait — normal ;
* **après une rotation**, la bonne séquence est : poser la valeur sur la plateforme,
  redémarrer (changer une variable redémarre le service sur la plupart des
  plateformes), *puis* `--require-remote` → `0`, *puis* `--record`. Le `0` de la
  barrière distante est ce qui prouve que le redémarrage a bien eu lieu ;
* **si le service a été déployé avec un secret que cette machine ne connaît pas**
  (`SUPABASE_SERVICE_KEY` absent du `.env` local, par exemple), l'audit le dit au
  lieu de supposer : un secret requis que seule la production détient est une
  **erreur**, un secret facultatif un simple avertissement.

### Après le redéploiement

Le démarrage est lui aussi fail-closed : `run.py` et `main.py` appellent
`core.config_runtime.enforce_secure_config()`, qui **refuse de démarrer** tant que
`WEBHOOK_SECRET` porte encore sa valeur par défaut ou que `INTERNAL_API_KEY`
manque ou vaut le webhook. Les autres manques se lisent dans le préflight
(`/preflight`) — une `SUPABASE_SERVICE_KEY` absente ou porteuse d'une clé `anon`
ne bloque pas le démarrage, elle rend le bot muet, et c'est l'audit pre-deploy qui
l'arrête.

Un identifiant broker que l'anneau ne rouvre plus ne retombe **jamais** sur le
compte partagé : l'ordre est refusé, et `/broker_status` dit pourquoi (il affiche
aussi la version de clé qui a chiffré la ligne, ce qui rend une rotation visible
depuis le bot).

Un SDK absent non plus. Deux clés bien renseignées ne font pas un compte
utilisable : sans `alpaca-py`, aucun client ne se construit et aucun ordre ne
part. Le préflight le dit maintenant par deux champs séparés —
`alpaca_shared.keys_present` et `.sdk_installed` — et `ready` est leur ET, donc
**faux** sur une machine sans le paquet, alors qu'il ne lisait que les clés.

Un registre absent — le cas d'un dépôt fraîchement cloné — fait refuser tous les
secrets requis : c'est le rôle de `--record`, ou de `--allow-missing-rotation`
pour un tout premier déploiement qui ne doit pas être bloqué par l'absence
initiale.

### En cas de fuite

Si un secret est **compromis**, effectue la rotation puis `--record`
immédiatement : l'écart d'empreinte disparaît et l'horloge de 90 jours
repart.

Pour `ENCRYPTION_KEY`, la fuite impose un ordre : la clé compromise passe dans
`ENCRYPTION_KEYS_PREVIOUS` (sinon le chiffré existant devient illisible), puis
`rotate_encryption_key.py --apply` réécrit **tout** avec la clé neuve — c'est
seulement quand il ne reste rien que la clé fuitée peut quitter l'anneau. Tant
qu'elle y est, elle rouvre encore ce qu'elle a chiffré : la fenêtre se compte en
minutes, pas en jours.
