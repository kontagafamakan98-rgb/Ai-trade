# AI Trade (AI-TR)

Bot de trading **paper** assisté par IA (validation humaine obligatoire) + moteur
d'alerte interne (EMA/ATR) + tableau de bord Android.

> ⚠️ **Aucune exécution d'ordre réel n'est possible tant que `PAPER_TRADING` est
> actif (défaut).** Le moteur ne place jamais d'ordre sans validation explicite
> de l'utilisateur.

## Architecture

| Composant | Techno | Rôle |
|---|---|---|
| **Backend** | Python 3.11 · FastAPI · python-telegram-bot | Collecte de données, moteur de décision, risk guard, exécution paper (Alpaca), API interne |
| **Données** | Supabase | Insights, signaux, credentials chiffrés, calendrier macro |
| **LLM** | Groq (+ Gemini en secours) | Analyse qualitative des news |
| **App Android** | Kotlin · Jetpack Compose · Room | Terminal/tableau de bord **de démonstration** (voir avertissement ci-dessous) |

### ⚠️ App Android = simulateur

L'application Android **n'exécute aucun ordre réel** et sa partie trading est une
maquette : les prix (`MarketService`) sont générés par marche aléatoire, les
titres d'actualité et le « backtest » sont synthétiques. Elle sert de maquette
d'interface et de démonstration, et elle est explicitement étiquetée comme telle
dans l'UI.

Une exception, et une seule : l'**écran d'administration**
(`ui/AdminSupabaseScreen.kt`), qui interroge réellement le backend
(`GET /admin/supabase/check`, `POST /admin/supabase/roundtrip`) quand un opérateur
y a saisi l'URL et la clé interne. C'est un écran de diagnostic, pas un exécuteur
d'ordres — et un rapport de vérification **simulé** ressemblerait à une base en
bon état, ce qui est exactement le silence que la sonde existe pour rompre : il ne
pouvait pas être inventé sur l'appareil. Voir « L'écran d'administration de
l'application », plus bas.

### L'identité de l'application Android (`applicationId`)

L'application installée est connue du système par son **`applicationId`**
(`com.aitrade.app`, dans `app/build.gradle.kts`), et non par son nom affiché ni
par le paquet de ses sources Kotlin (voir « Le paquet des sources » ci-dessous).
Cette identité a été reprise au gabarit d'export, qui portait le sien ; elle ne
porte plus aucune trace de l'atelier d'origine, et `metadata.json` ne déclare
plus de capacité d'atelier. `tests/test_android_identity.py` tient ce contrat —
notamment contre un **nouvel export du gabarit**, qui réécrirait les deux à
l'identique.

**Ce que changer l'`applicationId` change pour une installation existante :**

| Ce qui était installé | Ce qui se passe après |
|---|---|
| L'application | Android la voit comme une **autre** application : la nouvelle s'installe **à côté** de l'ancienne, sans la remplacer ni la mettre à jour (`INSTALL_FAILED_UPDATE_INCOMPATIBLE` si l'on tente de mettre à jour l'ancienne avec le nouvel APK) |
| Les données (base Room, préférences, fichiers) | Elles vivent dans le bac à sable de l'ancienne identité : la nouvelle application démarre **vide**. Rien n'est effacé, mais rien n'est repris — ni le portefeuille paper, ni l'historique, ni les réglages |
| Les mises à jour | Elles passent par l'ancienne identité tant que son paquet est publié. Un magasin **ne permet pas** de renommer une application déjà publiée : publier la nouvelle identité crée une **nouvelle fiche**, avec ses installations repartant de zéro |
| Les liens profonds, notifications, alarmes, raccourcis | Ils visent l'ancienne identité : ils continuent d'ouvrir l'ancienne application jusqu'à sa désinstallation |
| La signature | La même clé signe les deux, mais elle n'autorise la mise à jour que **dans une même identité** : elle ne rattrape pas un changement d'`applicationId` |
| Un `google-services.json` (Firebase) | Il est indexé par `applicationId` : il faudrait y enregistrer la nouvelle identité, sinon l'ancienne seule serait reconnue |

Autrement dit : sur une machine de développement, c'est une réinstallation (avec
les données de test à recréer) ; sur un magasin, c'est une nouvelle application.
Le `versionCode` et `versionName` ne changent rien à cela — c'est bien
l'identité qui décide.

### Le paquet des sources Kotlin (`namespace`)

Le second nom de l'application est celui de son **code** : `namespace`, dans
`app/build.gradle.kts`. Les sources vivent sous
`app/src/{main,test}/java/com/aitrade/`, et chaque fichier déclare
`package com.aitrade.…` en tête. Le gabarit d'export les avait placées dans
l'espace de noms d'exemple — celui que les outils réservent aux exemples, et que
personne ne possède ; elles portent désormais le nom du projet.

| Ce qu'on est tenté de confondre | Ce qui est vrai |
|---|---|
| `applicationId` et `namespace` | Deux choses distinctes : le premier est l'identité **du système** (installation, magasins, données), le second celui **du code** (`R`, `BuildConfig`, imports). Kotlin, contrairement à Java, **n'exige même pas** qu'une déclaration de paquet corresponde à son répertoire : rien ne le signale avant l'exécution |
| Renommer le paquet | Déplacer les sources et réécrire les déclarations d'import. **Aucun effet sur une installation existante** : l'application installée ne connaît que son `applicationId` — contrairement au changement ci-dessus, qui change tout |
| Renommer l'`applicationId` | C'est une **autre** application (voir le tableau précédent). Renommer le paquet des sources n'a pas ce coût |`tests/test_kotlin_packages.py` tient le contrat : pour chaque `.kt`, le paquet
déclaré est le chemin sous `java/` **segment pour segment**, chaque paquet est
sous le `namespace`, celui-ci porte le nom du projet, chaque composant nommé par
le manifeste est un fichier réel, et l'ancien espace de noms n'apparaît plus
nulle part — ni dans les sources, ni dans les scripts qui prennent le dossier du
paquet en argument, ni dans cette documentation, sous aucune de ses trois
écritures (déclaration, chemin d'import, chemin assemblé segment par segment).

Il fait aussi, faute de compilateur, une part de ce que la compilation aurait
faite : chaque `import com.aitrade.…` est résolu dans les sources réelles, et un
symbole qui ne désigne plus rien est signalé. C'est ce qu'un renommage mécanique
casse le plus discrètement — et la seule exemption est `R` et `BuildConfig`, que
la construction **génère** dans le paquet du projet. La CI le vérifiera de toute
façon ; ce test le dit quelques secondes plus tôt, sur une machine sans JDK.

### Localisation — chaînes dans les ressources Android

Les libellés de l'interface vivent dans `res/values*/strings.xml` :
`values/strings.xml` est la **langue par défaut (anglais)**, et chaque traduction
a son propre dossier (`values-fr`, `values-es`, `values-de`, `values-zh`,
`values-ar`, `values-ja`, `values-pt`, `values-ru`, `values-hi`).

| Élément | Rôle |
|---|---|
| `res/values*/strings.xml` | **Source de vérité** des textes : une clé, un libellé |
| `ui/StringsResources.kt` | Projette les ressources sur le modèle `AppStrings` lu par les écrans |
| `ui/Localization.kt` | `ProvideAppStrings(langue)` fournit un `Context` localisé (+ sens de lecture RTL) |
| `ui/StringMappings.kt` | Traduit les **codes** du moteur (`BUY`, `EXECUTED`, plans VIP…) en libellés |
| `tests/test_android_strings.py` | Parité des clés, échappement `aapt`, références `R.string` |

Ajouter une langue = créer `values-<code>/strings.xml` avec **exactement le même
jeu de clés** : aucun code Kotlin à modifier, et le test le vérifie. Aucun libellé
n'est assemblé par concaténation — chaque phrase est une chaîne unique, donc
relisible et traduisible dans son contexte.

État : **440 clés** dans `values/`, **414** dans chacune des neuf traductions (25
clés `translatable="false"` plus `app_name`, porté par le manifeste, n'existent
que dans `values/`). Plus aucune phrase anglaise en dur dans `ui/` : seul y
subsiste le message d'assertion de `LocalAppStrings` et les intitulés de langue
du prompt Gemini, qui ne sont pas affichés.

Les **données de démonstration** suivent la même règle : les trois analyses et les
sept signaux initiaux de `data/TradingRepository.kt` sont des ressources
(`demo_insight_*`, `demo_signal_*`, 34 clés), donc écrits dans les dix langues. Le
dépôt lit un `Context` localisé (`data/LocalizedContext.kt`, la fonction que `ui/`
utilisait déjà) et les blocs `if (isFr) … else if (isEs) …` — qui ne couvraient que
quatre langues et retombaient en anglais pour l'arabe, le japonais, le portugais,
le russe et l'hindi — ont disparu. Ce qui n'est pas un libellé reste une donnée :
identifiant, type, score, statut (`EXECUTED`…) et horodatage sont inchangés, et un
seul chemin de code sert les dix langues.

Ce contrat est **figé** dans `tests/goldens/android_localization.json` (jeu de
clés, clés non traduisibles, arguments de format, clés manquantes par langue) :
ajouter, renommer ou retirer une chaîne exige donc une régénération explicite du
golden, et laisse un diff relisible dans la revue — voir « Valeurs de référence
(goldens versionnés) ».

Cinq règles tenues par le code et vérifiées par les tests :

* **`translatable="false"`** — les marques de courtiers (`Alpaca Trading`,
  `MetaTrader`, `OANDA`…) sont identiques dans toutes les langues : la clé
  n'existe que dans `values/`, où l'onglet « traduire » d'un outil ne la verra
  pas. Les recopier dans `values-fr` recréerait un endroit où corriger une marque.
* **`%%` dans une chaîne à argument** — le `%` littéral d'un gabarit
  (`%1$s%%`) est doublé, sinon `String.format` le lit comme un spécificateur
  incomplet et lève une exception. Une valeur contenant `%` déclare par ailleurs
  `formatted="false"` : `getString` rend alors le gabarit brut, comme attendu.
* **Codes stables, libellés traduits** — un statut (`EXECUTED`, `BLOCKED_RISK`),
  une action de consensus (`STRONG_BUY`), un plan d'abonnement (`monthly`) ou
  l'état courtier (`paper_sandbox`) restent des **valeurs de protocole** écrites
  en base et comparées telles quelles. Elles sont traduites au dernier moment par
  `ui/StringMappings.kt` (`statusLabel`, `actionLabel`, `convictionLabel`,
  `vipPlanTitle`…). Traduire la valeur elle-même casserait le filtrage et ferait
  dépendre un test du texte affiché.
* **Formats dépendant de la locale** — les motifs `SimpleDateFormat` sont des
  ressources (`date_time_pattern_short`), résolues avec `rememberAppLocale()` :
  `ProvideAppStrings` localise le contexte sans toucher à la locale par défaut du
  processus, sinon les dates suivraient la langue du téléphone et non celle
  choisie dans l'application.
* **Documents légaux en français seulement** — la politique de confidentialité
  (RGPD) et les CGU ne sont pas traduites : elles vivent sous
  `translatable="false"` dans `values/`, et un utilisateur arabophone ou japonais
  lit le texte français plutôt qu'une traduction approximative. Dix rédactions
  juridiques divergeraient les unes des autres, et une erreur de droit ne se voit
  pas à la relecture d'une traduction. `ui/LegalScreen.kt` les affiche depuis
  l'onglet « Synchro Cloud & VIP » ; les modifier demande une relecture
  juridique, pas seulement linguistique.

Deux règles de présentation, tenues par des tests
(`tests/test_android_strings.py`, classe `PresentationRulesTest`) :

* **aucun emoji décoratif**, ni dans un libellé ni dans les sources Kotlin. Un
  emoji ne remplace pas une icône : il ne s'aligne pas sur la ligne, ne se teinte
  pas avec le thème et son dessin dépend de la police du système. La sévérité
  d'une ligne du journal est portée par sa **couleur** (`LiveLogFeedCard`), et le
  sélecteur de langue affiche `FR` / `EN` plutôt qu'un drapeau, qui n'est pas une
  langue ;
* **aucun tiret cadratin** dans un texte affiché : c'est une ponctuation de prose,
  pas un libellé d'interface. Les commentaires du code ne sont pas concernés, ils
  ne sont jamais affichés.

De la même famille : la remise de l'abonnement annuel est **calculée** depuis
`VipPlan.amountUsd` (`annualSavingPercent()` dans `StringMappings.kt`), jamais
écrite dans un libellé. Elle annonçait « 60 % » pour un écart réel de 58,3 %, et
serait devenue fausse au premier changement de prix — `PricingClaimTest` vérifie
que les prix affichés dans les dix langues sont bien ceux du calcul.

Le `TradingViewModel` alimente le journal, les insights et le chat hors de toute
composition : il lit les mêmes ressources via `strings(language)`, mémorisée par
langue pour que les boucles (scan de la watchlist, gestion des positions) ne
reconstruisent pas les 356 chaînes du modèle à chaque tour.

**Reste à faire** — le texte *produit* par `engine/` n'est pas encore ressourcé :
les recommandations d'agents (`MultiAgentResearchEngine`), les résumés techniques
(`DecisionEngine`, `BacktestEngine`) et les motifs de risque y restent des phrases
assemblées en Kotlin, donc anglaises quelle que soit la langue choisie. C'est le
dernier étage où un texte affiché ne passe pas par `res/values*/strings.xml`.

### Thème et échelle typographique (`ui/theme/Theme.kt`)

Aucun écran ne fixe de taille de police : la quarantaine d'écrans Compose tire
**toute** sa typographie de l'échelle du thème (`MaterialTheme.typography`).

L'échelle est celle de Material3 — `displayLarge` (40.sp) jusqu'à `labelSmall`
(11.sp) — complétée par la **frange dense**, sous le plancher de Material :

| Jeton | Taille | Usage typique |
|---|---|---|
| `MaterialTheme.typography.labelSmall` | 11.sp | libellé compact (échelle Material) |
| `LabelExtraSmall` | 10.sp | en-tête de métrique, puce de statut, chip dense |
| `LabelTiny` | 9.sp | légende de métrique, sous-titre de carte |
| `LabelMicro` | 8.sp | pastille courte (« LONG », « 87% ») — plancher de lisibilité |

Un terminal financier affiche beaucoup de texte très petit ; laisser courir des
`fontSize = 9.sp` dans 39 fichiers rendait toute la frange dense impossible à
retoucher. Les trois jetons ci-dessus sont donc **définis une seule fois** dans
`Theme.kt` (à côté de `NumericTextStyle`, réservé aux valeurs alignées en
monospace).

Deux points de méthode :

* les `fontWeight` / `fontFamily` présents à côté d'un `fontSize` ont été
  **repliés** dans le style — `style = MaterialTheme.typography.labelSmall`
  `.copy(fontWeight = FontWeight.Bold)` — et non supprimés : une migration ne
  doit pas changer la graisse d'un libellé au passage ;
* une expression dépassant les 140 caractères de `ktlint_official` est
  **découpée** (un argument par ligne, continuations `.copy(...)` indentées de
  `+4`) et non exemptée.

### Formes des boutons

Material 3 donne par défaut aux boutons la forme `corner full`, c'est-à-dire
entièrement arrondie. Les quarante boutons de l'app étaient donc des **pilules**
sans que personne ne l'ait décidé : rien dans le code ne le dit, seul l'écran le
montre. La forme commune vit désormais dans `Theme.kt` (`ButtonShape`), chaque
bouton la passe explicitement (`shape = ButtonShape`), et
`scripts/kotlin_ui_check.py` refuse celui qui reprendrait le défaut de Material.
Les pastilles de statut restent des cercles, ce sont des points, pas des boutons.

## Démarrage (backend)

```bash
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                              # puis renseigne les valeurs
python scripts/generate_secrets.py                # remplit .env, sans jamais les afficher
python scripts/pre_commit_secrets.py --install    # active le hook anti-fuite
python run.py                                     # web + auto-loop + bot Telegram
```

Le démarrage **échoue volontairement** si `WEBHOOK_SECRET` ou `INTERNAL_API_KEY`
sont absents ou laissés aux valeurs par défaut.

### Génération et vérification des secrets

Les secrets **locaux** s'écrivent dans `.env` sans jamais passer par l'écran :
`python scripts/generate_secrets.py` remplit ce qui manque (et `--check` dit
seulement ce qui manque). Il ne remplace **jamais** une valeur existante qui n'est
pas un exemple : une valeur faible est signalée, pas écrasée — la remplacer est une
rotation, et une rotation se demande :

```bash
python scripts/generate_secrets.py --rotate                # ce que chaque rotation casse
python scripts/generate_secrets.py --rotate WEBHOOK_SECRET  # l'annonce (aucune écriture)
python scripts/generate_secrets.py --rotate WEBHOOK_SECRET --apply  # écrit + horodate
python scripts/generate_secrets.py --record-only SUPABASE_SERVICE_KEY  # valeur venue d'ailleurs
```

`--rotate` n'existe pas pour les secrets qui ne se génèrent pas ici
(`SUPABASE_SERVICE_KEY`, `TELEGRAM_BOT_TOKEN`) : il les refuse en nommant leur
source. Pour `ENCRYPTION_KEY`, la clé sortante entre dans l'anneau et la nouvelle
prend la version au-dessus — la rotation reste réversible jusqu'à ce que le chiffré
soit réécrit.

La clé de chiffrement des identifiants broker n'est plus unique : `ENCRYPTION_KEY`
**chiffre**, `ENCRYPTION_KEYS_PREVIOUS` — les clés retirées — **rouvre** l'ancien
chiffré. Une rotation ne perd donc rien tant que l'ancienne clé reste dans
l'anneau, et `python scripts/rotate_encryption_key.py` dit ce qu'il reste à
réécrire avant de pouvoir l'en retirer. Un identifiant que l'anneau ne rouvre pas
fait **refuser** l'ordre, jamais exécuter sur le compte partagé.

La présence du **SDK** fait partie de la réponse, et elle est publiée :
`components.alpaca_shared` porte `ready`, `keys_present` et `sdk_installed`. Sans
`alpaca-py`, `get_alpaca_client` refuse — aucun ordre ne part, ni du compte
personnel ni du compte partagé — et `ready` est donc **faux même avec les deux
clés renseignées**, ce qu'il ne disait pas auparavant : deux clés suffisaient à
publier « prêt » sur une machine d'où rien ne pouvait sortir. La sonde
(`execution/alpaca_sdk.py`) répond par `find_spec`, sans importer le paquet :
`/health` ne se paie pas le graphe de l'exécuteur (base, chiffrement, garde-fou
de risque) pour cette question. Elle est comparée à `ALPACA_OK` — la réponse qui
fait foi à l'exécution — par `tests/test_alpaca_sdk_readiness.py`.

Avant chaque déploiement, l'audit **fail-closed** vérifie la présence, la
robustesse, la **rotation** de tous les secrets et l'absence de **fuite** de
leurs valeurs dans les fichiers du dépôt :

```bash
python scripts/verify_secrets.py            # sortie 1 = déploiement refusé
python scripts/verify_secrets.py --record   # après une rotation légitime
```

Il vérifie aussi, quand l'URL du **service déployé** est connue (`--remote` ou
`DEPLOYED_URL`), que la production tourne bien **ces** valeurs : le processus
distant publie l'empreinte de ce qu'il a chargé (`GET /secrets/fingerprints`,
jamais une valeur) et la barrière la compare aux siennes. Une valeur différente,
un secret absent en production ou un service injoignable **refusent** le
déploiement ; sans URL connue, l'audit reste vert mais l'écrit (« la production
n'a PAS été vérifiée »), et `--require-remote` en fait un refus :

```bash
python scripts/verify_secrets.py --remote https://<service>.onrender.com --require-remote
```

Le rapport dit **sur quoi** il porte, sans qu'on ait à ouvrir le JSON ni à relire
la sortie complète. La ligne du **registre de rotation** en donne la synthèse
(nombre d'entrées, date de la plus ancienne et son âge, dates illisibles) : on
voit si le registre vit ou stagne. La ligne du **scan anti-fuite** donne son
périmètre (valeurs cherchées, fichiers lus, écartés par politique) et le bloc qui
suit **nomme ce qu'il n'a pas regardé** — `.env` locaux, répertoires exclus,
suffixes non analysables, fichiers vides. « Aucune fuite détectée » ne se lit donc
pas « tout le dépôt a été lu » : le périmètre est écrit, et `--json` en porte la
liste intégrale.

Détails : [`docs/SECRETS.md`](docs/SECRETS.md) et [`security/README.md`](security/README.md).

### Hook pre-commit (blocage avant le commit)

Une valeur de secret est refusée **avant d'entrer dans l'historique** : le hook
analyse le contenu **indexé** (pas la copie de travail, car `git add -p` peut
indexer autre chose que ce qui est affiché).

```bash
python scripts/pre_commit_secrets.py --install   # git config core.hooksPath .githooks
```

Sans secret configuré, le hook l'annonce explicitement : il n'a alors aucune
valeur de référence et ne détecte rien. Le détail des différences avec le scan
pre-deploy (index vs copie de travail, `.env` forcé, `--strict-rotation`) est
dans [`docs/SECRETS.md`](docs/SECRETS.md).

Ce silence-là ne survit pas au push : le job CI `secrets-audit` relit l'arbre
de la branche poussée avec les **vraies** valeurs (secrets du dépôt) et refuse un
scan qui n'aurait rien à chercher — un poste sans `.env` ne peut donc plus faire
passer une fuite. Sur une machine, la même exigence se force avec
`SECRETS_HOOK_STRICT=1`. Voir [`docs/SECRETS.md`](docs/SECRETS.md), « Le gate de
CI ».

### Contrat de sortie des deux hooks

Les wrappers `.githooks/*` rendent les trois codes de l'outil qu'ils délèguent,
plus un qu'il n'émet jamais :

| Code | Signification |
|---|---|
| `0` | L'outillage a tourné et a conclu : rien à refuser |
| `1` | L'outillage a tourné et a conclu : **refus** |
| `2` | Invocation invalide |
| `3` | L'outillage **n'a pas pu démarrer** : rien n'a été contrôlé |

`3` existe pour qu'un incident d'OS (fork MSYS2 saturé, interpréteur Python
introuvable, script de l'outil absent) ne puisse plus se lire comme un refus. Le
hook **ne laisse plus passer en silence** quand il n'a rien pu lancer : il
l'annonce et sort en `3` — et sous git, cela bloque le commit comme le push.

À savoir : `git commit` et `git push` **écrasent à `1`** le code de leurs hooks.
La distinction ne survit donc que si le wrapper est appelé directement, ce que
vérifie `tests/test_hook_tooling_contract.py` — par le code, sans lire la sortie.

## Apprentissage adaptatif

Les poids `TA / sentiment / macro` du moteur de décision peuvent être appris par
**descente de gradient** sur les trades réglés, au lieu de l'heuristique codée en
dur. Le trade qu'on règle n'entre pas dans son propre entraînement : sa ligne est
exclue de l'historique qui décide des poids écrits à cette occasion, par identifiant
(voir la section du même nom dans le document ci-dessous). Et il ne se règle
qu'**une fois** : un rejeu de `POST /learning/feedback`, comme un rattrapage du
tracker sur le même signal, n'ajoute ni post-mortem ni trade compté — quel que soit
le verdict annoncé, le premier enregistré faisant foi — et le retour le dit
(`status: "already_settled"`). C'est **désactivé par défaut** :

```bash
ADAPTIVE_GD_ENABLED="true"   # défaut : false
```

Détails (sous-notes persistées, garde-fous, mise en service progressive) :
[`docs/ADAPTIVE_LEARNING.md`](docs/ADAPTIVE_LEARNING.md).

### Migrations Supabase

Exécute les fichiers `database/migrations/*.sql` dans l'éditeur SQL Supabase,
dans l'ordre. La migration `007_row_level_security.sql` doit être appliquée pour
activer la RLS sur toutes les tables.

La migration `005_forex_factory.sql` crée les **tables du marché** : le calendrier
économique (`economic_events`, écrit par le scraper Forex Factory puis relu par le
moteur macro `core/macro_engine.py` et le garde-fou de news
`execution/news_risk_guard.py`) et le journal des décisions macro
(`macro_bias_logs`, écrit à chaque décision ; aucun `select` du dépôt ne le relit,
c'est une **trace**). Ce journal est donc en ajout seul : pas d'`updated_at`, donc
pas de trigger `updated_at` sur une ligne qu'on ne modifie jamais.

La migration `006_adaptive_learning.sql` crée les **tables de l'apprentissage** :
`trade_post_mortems` (le diagnostic de chaque trade réglé, en ajout seul lui
aussi) et `adaptive_model_weights` (un profil de poids par actif, relu à chaque
signal et réécrit à chaque apprentissage). Elle **retire** une colonne :
`min_confidence_threshold`, qu'aucun chemin de code n'écrit ni ne lit — le seuil
effectif est calculé dans `core/adaptive_learning.py`, il n'est pas réglable en
base. Une colonne morte laisse croire l'inverse.

Ces deux fichiers suivent les règles de la migration 011 — `create table if not
exists` + `add column if not exists` (sur une base en service, seule la colonne
manquante est ajoutée), aucun `alter column … type`, RLS « deny by default » avec
révocation répétée dans le fichier qui crée les tables — et leurs colonnes sont
**celles que le code utilise** : `tests/test_engine_tables_migration.py` les
relit par la **même extraction** que le contrat de la 011
(`tests/sql_columns.py`), pour que les deux ne puissent pas diverger. S'y ajoutent
les index que les requêtes justifient, et le retrait de deux index qu'aucune ne
justifiait plus (`idx_macro_bias_logs_symbol`, `idx_trade_post_mortems_asset`,
remplacé par un index composite qui sert à la fois le filtre et le tri) : un index
sans requête est un coût d'écriture.

Comme ces deux migrations ont été **complétées** après coup, relance-les dans
l'éditeur SQL sur une base déjà en service : elles sont idempotentes, elles
n'ajoutent que ce qui manque et ne touchent aucune donnée existante.

La migration `008_telegram_media.sql` ajoute les médias Telegram : elle crée le
bucket Storage privé `telegram-media` (les octets), la table `knowledge_media`
(la description de chaque fichier) et la table `knowledge_chunks` (texte
découpé + embedding pgvector, dimension 768, pour la recherche sémantique).
Comme les autres, elle est idempotente et peut être relancée.

La migration `009_knowledge_vectors.sql` ouvre `knowledge_chunks` aux **notes**
de `knowledge_base` (`media_id` devient nullable), ajoute les colonnes de filtre
`source` / `asset` / `regime` (+ `embedding_model`) et la fonction
`match_knowledge_chunks` — recherche top-k par similarité cosinus, filtrée par
actif et régime de marché. Idempotente également.

La migration `010_bot_settings.sql` crée `bot_settings`, la table clé/valeur des
**réglages d'exécution** (liste des canaux balayés, période de balayage, période
et seuil de la veille média) : elle permet de changer ces réglages sans
redéployer un worker qui tourne en continu.
Elle est **facultative** — sans elle, les réglages viennent de l'environnement,
comme avant. Idempotente également.

La migration `011_core_tables.sql` (re)construit le **schéma des trois tables
historiques** — `users`, `insights`, `pending_signals` — qui existaient
uniquement en base : elles avaient été créées à la main, et la migration 007 se
contente de dire `alter table if exists users enable row level security`. Le
schéma du projet n'était donc reproductible nulle part, et une base neuve ne
pouvait pas démarrer. Trois propriétés comptent ici :

* **les colonnes sont celles que le code utilise**, pas un schéma idéal. Chacune
  est citée avec le fichier qui l'écrit ou la lit, et
  `tests/test_core_tables_migration.py` **relit les sources** pour vérifier
  qu'aucune colonne d'une requête ne manque — et que la migration n'en déclare
  aucune que personne n'utilise ;
* elle s'applique à une base **déjà en service** : `create table if not exists` +
  `add column if not exists`, et elle ne change le type d'aucune colonne
  existante (un `alter column … type` réécrit la table et verrouille les
  écritures — à faire à part, en connaissance de cause) ;
* elle reste idempotente (triggers recréés après `drop trigger if exists`,
  index `if not exists`, contrainte ajoutée seulement si elle n'existe pas).

Elle pose aussi les index que les requêtes du code justifient (fil `insights`
trié et filtré par actif, comptage des positions ouvertes, anti-spam sur
`created_at`, rapports triés sur `validated_at`), les triggers `updated_at` des
trois tables, la clé étrangère `pending_signals.user_id → users.id` en
`not valid` (elle s'applique aux nouvelles lignes sans exiger la validation des
lignes historiques), et la RLS « deny by default » avec révocation — répétée ici
parce que ce fichier est aussi ce qui **crée** ces tables sur une base neuve.

Un choix à connaître : `pending_signals.status` porte le vocabulaire du code
(`pending`, `executed`, `rejected`, `won`, `lost`) **sans contrainte `check`**.
`update_signal_status()` accepte n'importe quelle chaîne, et une contrainte
ajoutée à une table déjà remplie échoue sur les lignes historiques : elle
refuserait un statut légitime au milieu d'un cycle. Le vocabulaire est verrouillé
par le test de contrat, pas par le schéma.

### Appliquer les migrations pour de vrai (`scripts/apply_migrations.py`)

Les contrats de migration — `tests/test_core_tables_migration.py` et
`tests/test_engine_tables_migration.py` — **relisent** les fichiers SQL contre les
sources Python. C'est utile, mais ça ne dit qu'une chose : le texte dit ce qu'il
veut dire. Rien là-dedans n'exécute le SQL, donc rien n'attrape ce qu'un `psql`
refuserait du premier coup : un `do $$` non refermé, un `alter table` qui vise une
colonne disparue, une contrainte que le type refuse, un `revoke` sur un rôle qui
n'existe pas. Ce script les exécute.

```bash
# une instance jetable — l'image fournit l'extension `vector` de la migration 008
docker run --rm -d --name ai-trade-pg -p 5432:5432 \
    -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=ai_trade_test pgvector/pgvector:pg16

MIGRATION_DATABASE_URL="postgresql://postgres:postgres@localhost:5432/ai_trade_test" \
    python scripts/apply_migrations.py --platform-stub
```

Trois choses à savoir sur l'outil :

* `--platform-stub` n'est pas une commodité de test. La migration 008 écrit dans
  `storage.buckets` et révoque des privilèges pour `anon` / `authenticated`, qui
  n'existent **que** parce que Supabase les crée. Sur un Postgres nu, ce sont les
  seules lignes qui échouent, et le stub fournit exactement ce que la plateforme
  fournit d'habitude — schémas `extensions` et `storage`, rôles
  `anon` / `authenticated` / `service_role`, table `storage.buckets` — sans créer
  aucune des tables du projet : celles-là restent le travail des migrations ;
* chaque fichier est appliqué dans **sa** transaction, dans l'ordre des numéros.
  Un échec laisse la base à l'état du dernier fichier appliqué et le script nomme
  le fichier **et la ligne** que Postgres a refusée ;
* la cible est affichée **avant** d'écrire, jamais avec son mot de passe
  (`…:***@…`), et `--dry-run` liste les migrations sans se connecter. L'URL vient
  de `--database-url` ou de `MIGRATION_DATABASE_URL`, et d'aucune autre variable :
  ce script modifie un schéma, il ne doit pas pouvoir partir sur la base d'un
  autre outil par simple héritage de l'environnement.

C'est ce que fait le job CI `migrations-postgres`, sur un service
`pgvector/pgvector:pg16` (l'image `postgres:16` n'a pas l'extension `vector` : la
008 y échouerait). Le verdict, lui, est dans
`tests/test_migrations_apply_to_postgres.py` : il **réapplique** toutes les
migrations — l'idempotence y est mesurée sur une empreinte du schéma, pas
affirmée dans un en-tête — puis interroge la base : chaque table déclarée existe,
chaque colonne **utilisée par le code** aussi (c'est le sens qui compte : un
`insert` vise une colonne, pas une déclaration), la RLS est active partout et
`pg_policies` est vide, les rôles publics n'ont aucun privilège, chaque table
portant un `updated_at` a son trigger `trg_<table>_updated_at`, la clé étrangère
`pending_signals.user_id` reste `not valid`, et `knowledge_chunks.embedding` est
bien `vector(768)` après 008 puis 009.

Un piège que l'exécution réelle a mis au jour : `pgvector` vit dans le schéma
`extensions`, et `extensions` n'est en portée que sur Supabase (`search_path =
"public, extensions"`). Or le corps d'une fonction `language sql` est analysé **à
sa création** : un `<=>` nu dans la migration 009 dépendait donc de ce réglage et
échouait sur un Postgres nu — « operator does not exist: extensions.vector <=>
extensions.vector ». Le type **et** l'opérateur y sont maintenant qualifiés
(`extensions.vector`, `operator(extensions.<=>)`), et un test le verrouille. Une
migration de ce dépôt doit s'appliquer dans les deux environnements, sans quoi le
job CI est rouge jusqu'à ce que quelqu'un édite le fichier à la main dans
l'éditeur SQL.

Deux garde-fous, parce que ce test **modifie un schéma** : sans
`MIGRATION_DATABASE_URL` il est ignoré (le job `python` n'a donc besoin d'aucune
base), et il refuse une base dont le nom ne contient pas « test », sauf
`MIGRATION_TEST_ALLOW_ANY=1`. Pour appliquer les migrations à une base réelle, on
passe par `scripts/apply_migrations.py`, qui dit toujours où il écrit.

#### Une base **déjà en service** (le cas réel)

Appliquer les migrations sur une base vide ne prouve qu'une chose : qu'elles sont
valides. Sur une base en service — les tables existent, elles contiennent des
lignes, et il leur manque des colonnes d'une version antérieure — c'est
`add column if not exists` qui rattrape le retard, et rien ne le vérifiait.
`AlreadyInServiceTest` (dans le même fichier) le fait pour de vrai :

1. les tables sont reconstruites pour avoir une référence **neuve** ;
2. les colonnes qu'une migration ajoute par `add column if not exists` sont
   retirées — c'est la définition, faite par le dépôt lui-même, de ce qu'une base
   plus ancienne n'a pas — et une colonne qu'une version antérieure de `006`
   créait est réintroduite ;
3. des **lignes** sont écrites dans cet état ancien ;
4. les migrations sont rejouées, puis tout est vérifié : les colonnes reviennent,
   les lignes sont intactes, les colonnes nouvelles prennent leur défaut **sur les
   lignes déjà là**, et une base en service converge vers le schéma d'une neuve.

Cet état ancien n'est pas inventé : il est dérivé des `add column if not exists`
des migrations, donc il suit le DDL. Ce qui compte quand on écrit une migration,
c'est la conséquence — **toute colonne nouvelle doit avoir son
`add column if not exists` en face**, sinon une base en service ne pourra jamais
rattraper son retard. Un `add column … not null` **sans défaut** est refusé par
Postgres sur une table qui a des lignes : inapplicable, donc.

Le seul écart qui subsiste est mesuré et nommé par le test : dix colonnes
(`macro_bias_logs.symbol`, `currency`, `macro_score`, `news_risk_level`,
`decision`, `economic_events.title`, `trade_post_mortems.signal_id`, `direction`,
`outcome`, `learned_lesson`) restent **nullables** après réparation, là où une base
neuve les a `not null` — impossible de faire mieux sans inventer une valeur de
remplissage pour un actif ou une leçon. Et `insights.type` porte dans son
`add column` le défaut `'geopolitical'` qui permet de poser la colonne `not null`
sur une base peuplée. Rien d'autre ne diverge.

### Comparer la base aux migrations (`scripts/check_schema_drift.py`)

Les migrations et la base peuvent diverger, **dans les deux sens**, et aucune des
deux divergences ne se voit toute seule. Une migration non appliquée ne se
manifeste qu'à la requête (`column does not exist`), jamais au démarrage ; une
colonne ajoutée **à la main** en base ne se manifeste pas du tout — jusqu'au jour
où l'on reconstruit la base depuis les fichiers et qu'elle n'est plus la même.
C'est exactement ce que la migration 011 a réparé pour les trois tables
historiques, qui n'existaient que dans la base.

Les contrats de migration relisent le SQL et les sources Python, mais ne
regardent jamais une base ; `tests/test_migrations_apply_to_postgres.py` compare
un schéma réel, mais seulement derrière `MIGRATION_DATABASE_URL` et sous forme
d'assertions. Cet outil-là se lance à la main, sur n'importe quelle base :

```bash
MIGRATION_DATABASE_URL="postgresql://postgres:motdepasse@db.<projet>.supabase.co:5432/postgres" \
    python scripts/check_schema_drift.py
python scripts/check_schema_drift.py --json      # sortie exploitable par un script

# la migration de rattrapage : elle est écrite, pas appliquée
python scripts/check_schema_drift.py --repair-sql > database/migrations/012_rattrapage.sql
```

**Il est en lecture seule** : `information_schema` pour les tables et les
colonnes, `pg_catalog` pour les index, contraintes, déclencheurs, RLS et droits
(dont le DDL des colonnes, quand il faut écrire le rattrapage) — rien d'autre, et
c'est vérifié par un test sur les requêtes elles-mêmes, pas seulement écrit ici. Il peut donc viser une **production** sans la modifier, ce qui est son
intérêt premier : c'est là que la dérive s'installe.

| Constat | Ce que ça veut dire |
|---|---|
| `table absente` | la migration qui la crée n'est pas passée |
| `colonne absente` | idem, pour une migration plus récente que la base. Le fichier qui la déclare est nommé, ainsi que le fichier de code qui l'utilise quand il y en a un : la colonne qui casse l'application se distingue de celle qui ne la touche pas |
| `colonne en base, jamais déclarée` | ajoutée à la main, ou migration oubliée : à déclarer, ou à retirer |
| `table en base, jamais déclarée` | idem, au niveau de la table |
| `colonne utilisée par le code, déclarée nulle part` | ni la base ni les migrations ne la connaissent : l'appel échouera à l'exécution |

Trois choix à connaître :

* la liste « déclaré » vient de `tests/sql_columns.py`, la **même extraction** que
  les contrats de migration, et la liste des fichiers de
  `scripts/apply_migrations.py`. Deux extractions finiraient par décrire deux
  schémas, et l'outil validerait une base que les tests refusent. C'est aussi
  pourquoi l'extraction rend les `drop column` : le schéma déclaré est celui que
  les migrations **laissent derrière elles**, pas tout ce qu'elles ont nommé (la
  006 retire `min_confidence_threshold`) ;
* cette extraction lit les requêtes **et la charge des écrivains délégués** :
  `raw_data` est rempli par `scrapers/forex_factory.py` et par la sonde de
  configuration (`scripts/check_supabase.py`), qui passent tous deux par
  `upsert_economic_events()`. Le rapport nomme donc l'**appelant** — c'est lui
  qu'il faut corriger —, pas seulement la fonction qui exécute l'insertion ;
* le schéma comparé est `public` — celui que les migrations remplissent.
  `extensions` et `storage` appartiennent à la plateforme ;
* l'URL vient de `MIGRATION_DATABASE_URL`, la même variable que l'applicateur et
  **aucune autre** : viser une base est un geste explicite, jamais un héritage de
  l'environnement. Le mot de passe n'est jamais affiché, et les codes de sortie
  sont les mêmes : `0` aucune dérive, `1` au moins un écart (ou base
  injoignable), `2` usage.

#### Les objets, pas seulement les colonnes

Une base qui a perdu un index, une contrainte, un déclencheur ou sa RLS **a
encore toutes ses colonnes** : les colonnes ne le diront jamais. L'outil compare
donc aussi ce que la base porte en dehors d'elles, dans `pg_catalog` (seuls les
index qu'aucune contrainte ne porte sont comptés : chaque `primary key` en crée
un, et le compter ferait crier l'outil sur toutes les bases justes) :

| Constat | Sens |
|---|---|
| `index absent` / `en base, jamais déclaré` / `différent` | perdu, ajouté à la main, ou recréé autrement |
| `contrainte absente` / `en base, jamais déclarée` / `différente` | une contrainte se compare par **sa forme** (`clé étrangère (media_id → knowledge_media.id) on delete cascade`), jamais par son nom — Postgres nomme lui-même `users_pkey` |
| `déclencheur absent` / `en base, jamais déclaré` / `différent` | avec la fonction qu'il appelle |
| `RLS désactivée` / `activée, jamais déclarée` | la seule comparaison dont l'absence **ouvre** la base |
| `policy en base, aucune n'est déclarée` | les migrations n'en déclarent aucune : chaque policy trouvée est une ouverture de plus |
| `privilège accordé` | un `select` rendu à `anon` sur une table que `007` révoque, un `execute` rendu sur la fonction de `009` |

Le modèle est **lu dans les `revoke` des migrations**, jamais écrit ici : `007`
révoque pour toutes les tables de `public`, `009` l'`execute` d'une fonction, les
autres table par table. `public` sur une table est donc un écart (`anon` en fait
partie), mais `public` a `execute` **par défaut** sur une fonction et aucune
migration ne le révoque : l'exiger ferait crier l'outil sur la base que la CI
vient de construire. Le rapport dit ce qu'il a comparé, en deux lignes — sinon on
croit qu'il n'a pas regardé :

```
Objets — déclarés : 17 index, 18 contraintes, 11 déclencheurs, 14 tables en RLS, 0 policy ; en base : idem
Droits — les migrations révoquent pour anon, authenticated sur 14 tables et 1 fonction ; la base en accorde 0 droit à ces rôles
```

Le **rattrapage** ne répare pas ces constats-là : un index, une contrainte, un
déclencheur et un `revoke` s'écrivent à la main (`create index if not exists`,
`alter table … add constraint`, `alter table … enable row level security`), et le
fichier produit les liste en tête plutôt que de laisser croire qu'il a tout réglé.
Les cinq familles sont vérifiées sur un vrai PostgreSQL, dans les deux sens :
**zéro** constat sur une base construite par les migrations, et chaque famille
détectée quand elle est cassée à la main (index retiré, contrainte retirée puis
reposée `not valid`, déclencheur retiré, RLS désactivée, `select` rendu à `anon`).

#### Le rattrapage (`--repair-sql`)

Un rapport qui dit « à déclarer, ou à retirer » ne dit pas **par où commencer**.
`--repair-sql` écrit la migration, et il choisit le sens **par colonne** — parce
que le sens ne se devine pas, il se lit dans l'usage :

| Ce que la base a et que les migrations ignorent | Ce que le fichier en fait |
|---|---|
| une colonne que **le code utilise** | il la **déclare** (`add column if not exists`), avec le type, la nullabilité et le défaut **lus dans la base** (`pg_catalog.format_type`, donc `vector(768)` et non `USER-DEFINED`) |
| une colonne que **rien n'utilise** | il la **retire** (`drop column if exists`) : c'est la seule façon de faire disparaître l'écart sans inscrire dans les migrations une colonne dont personne ne veut — et **le retrait détruit ses données**, ce que le fichier dit deux fois |

Le premier cas est exactement celui de la migration 011 : trois tables
existaient en base et nulle part ailleurs, et le code s'en servait. Le second
suppose qu'on sache ce qu'on jette, d'où la règle du dépôt : **ce qu'on ne sait
pas décrire, on ne le propose ni à la déclaration ni au retrait**. Sont donc
nommées, jamais inventées : une colonne `identity`, une colonne calculée, un
défaut qui appelle `nextval` (la séquence n'existe pas sur une base neuve), une
colonne dont le DDL n'a pas pu être lu, une table entière, une colonne que le
code utilise et que **rien** ne déclare (aucun type ne se déduit d'un appel),
et les colonnes déclarées mais absentes de la base — celles-là ne sont pas un
rattrapage, c'est une migration non appliquée. Tout cela est listé en tête du
fichier produit, en commentaires.

Ce que le fichier **ne fait pas** : il ne s'applique pas tout seul (l'outil reste
en lecture seule), il ne crée pas les index, contraintes et déclencheurs qui
accompagnaient une colonne déclarée, et il ne prétend pas savoir si la table a
déjà des lignes — un `not null` sans défaut est refusé par Postgres sur une table
peuplée (voir « Une base **déjà en service** »), et il le rappelle dans la section
concernée. Le SQL part sur la sortie standard et le verdict reste sur la sortie
d'erreur : `--repair-sql > fichier` donne un fichier propre **et** un rapport
lisible à l'écran, avec le numéro de migration libre suivant.

Le fichier écrit est une migration de ce dépôt : ses instructions portent leurs
garde-fous (`if exists` / `if not exists`) et il doit passer `unguarded_ddl()`
comme les autres. Un test le vérifie **par l'extracteur des contrats** — il relit
le fichier produit, l'applique aux deux côtés et exige que la comparaison
devienne vide. Et deux scénarios ont été joués sur un vrai PostgreSQL : une
colonne ajoutée à la main est retirée par un fichier généré, appliqué par
`scripts/apply_migrations.py`, jusqu'à un `Aucun écart` du même outil ; et une
récolte de migrations en retard d'une colonne utilisée par le code est réparée —
la colonne retirée de la base est **recréée avec son type** par le fichier généré.

Le job CI `migrations-postgres` l'exécute aussi, sur la base qu'il vient de
construire : la dérive y est attendue **nulle**, ce qui vérifie du même coup que
l'outil ne crie pas sur une base juste.

### La sortie d'un script de contrôle (`core/console.py`)

Les rapports de ce dépôt sont écrits en français et ponctués de symboles qui
n'existent dans **aucune page de code Windows** : `✅`, `❌`, `⚠️`, `→`, `«…»`. Sur
une sortie encodée en `cp1252` — le cas de tout terminal Windows qui n'est pas une
console Win32 native : Git Bash, mintty, un tube, une redirection vers un fichier —
`print` lève alors au **moment du verdict**, c'est-à-dire à la ligne la plus utile du
rapport :

```
UnicodeEncodeError: 'charmap' codec can't encode character '\u2705' in position 3
```

Le contrôle a travaillé pour rien, et le message affiché parle d'encodage au lieu de
parler du dépôt. Quatre scripts en portaient le pansement : deux à l'identique
(`_make_streams_utf8` dans `verify_secrets.py` et `pre_commit_secrets.py`) et deux
sous une forme plus pauvre (`calibrate_similarity.py` et `goldens.py`, qui
n'accordaient l'UTF-8 qu'à `stdout` et laissaient l'encodage tel quel). Quatre
recopies, quatre comportements : c'est ce qui a motivé un seul endroit.

`core/console.py` le porte donc une fois pour toutes, et chaque script de `scripts/`
l'appelle **en tête de son `main`** :

```python
from core import console


def main(argv=None) -> int:
    console.make_streams_utf8()
```

`sys.stdout` et `sys.stderr` passent en **UTF-8**, avec `errors="replace"` en seconde
épaisseur. L'encodage d'abord, et c'est lui qui compte : un tube, un fichier ou un
pseudo-terminal n'a pas de page de code, et tout ce qui relit un rapport — un
éditeur, git, la CI — lit UTF-8. Le remplacement ensuite, pour ce qu'on ne peut pas
promettre : un flux qui refuse la reconfiguration, la capture d'un test, un
`pythonw.exe` sans sortie — un rapport amputé d'un symbole reste un rapport, une
trace d'encodage n'en est plus un.

La page de code de la console n'est **pas** touchée (`SetConsoleOutputCP`) : depuis
Python 3.6 (PEP 528), une vraie console Windows est déjà écrite en UTF-8 par l'API
large, donc la basculer ne changerait rien pour nous et laisserait le terminal de
l'utilisateur en 65001 après la sortie du script.

`tests/test_console_output.py` tient les deux moitiés. Le défaut est **rejoué** — sur
un flux `cp1252` reconstruit, écrire `✅` lève — puis le rapport doit sortir intact et
relisible en UTF-8 (`✅`, pas `?`), un flux sans `reconfigure` ou qui la refuse ne
doit rien faire échouer, et un contrat lu dans l'**arbre** des scripts exige que
chacun règle sa sortie avant d'écrire, qu'aucun ne recopie le pansement, et qu'un
script sans `main` soit exempté **par son nom** dans une liste — un module importé
n'écrit pas de rapport.

### Vérifier les workflows GitHub (`scripts/check_github_workflows.py`)

Un workflow YAML cassé ne produit **aucune erreur visible** : GitHub ignore le
fichier. Tous les jobs disparaissent d'un coup, et comme il n'y a plus rien à
exécuter, il n'y a plus rien à échouer — une branche verte avec zéro CI. Le
fichier le plus dangereux est donc `ci.yml` lui-même, celui dont on attendrait
qu'il prévienne.

```bash
python scripts/check_github_workflows.py         # tous les workflows du dépôt
python scripts/check_github_workflows.py --json  # sortie exploitable par un script
```

Il refuse trois familles de fautes : **YAML illisible** (tabulation en
indentation, guillemet ou crochet non refermé, indentation rattachée à aucune
section), **clé définie deux fois** — un second `jobs:` ou deux fois le job
`python:` **écrase** le premier en silence, et la moitié du fichier disparaît sans
un mot — et **structure GitHub invalide** : `on` ou `jobs` absent, clé étrangère
en tête de fichier (un `one:` à la place de `on:` dispense de CI sans le dire),
job sans `runs-on` ni `uses`, `steps` vide, étape qui porte à la fois `run` et
`uses` ou ni l'un ni l'autre, `needs:` nommant un job inexistant, service sans
`image`. Les codes de sortie sont ceux des autres outils : `0` valide, `1` au
moins un refus (sur la sortie d'erreur, avec `fichier:ligne`), `2` usage.

Ce n'est **pas** un parseur YAML, et il ne prétend pas l'être : il lit le
sous-ensemble qu'emploient les workflows (mappings et séquences en blocs,
scalaires simples ou repliés, scalaires blocs `|` / `>`, collections en flux sur
une ligne) et il **refuse** ce qu'il ne sait pas lire — ancres, alias, clé
complexe, scalaire replié sur plusieurs lignes — au lieu de l'approuver à
l'aveugle. Un refus visible vaut mieux qu'un accord sans preuve, et le rapport dit
ce qu'il a réellement lu (`N fichier(s), N job(s), N étape(s)`).

Il tient en **bibliothèque standard**, comme les hooks : le job CI l'exécute
**avant** d'installer la moindre dépendance. Et comme un `ci.yml` cassé
empêcherait cette étape elle-même de tourner, la même vérification est dans la
suite de tests (`tests/test_check_workflows.py`), qui tourne localement — et qui
casse le vrai `ci.yml` d'une seule ligne pour prouver qu'il est bien lu.

### Fins de ligne (`scripts/check_line_endings.py`)

`.editorconfig` déclare `end_of_line = lf` depuis le premier jour, et le dépôt
était en dérive : **71 fichiers en CRLF**, dont tout le Kotlin de l'interface, les
dix `strings.xml` et six gros modules Python. Deux causes, aucune des deux visible
dans un diff :

* sous Windows, `core.autocrlf=true` — le défaut de Git for Windows — réécrit les
  fichiers texte **au checkout**. Ils ne contiennent pas de `\r` parce que
  quelqu'un les a écrits ainsi, mais parce que git les a restitués ainsi ;
* un script qui lit ou écrit en **mode texte** (`newline=None`) retraduit chaque
  `\n` en `\r\n`. Un script de preuve du dépôt l'a fait sur un fichier Kotlin, et
  seules les empreintes SHA-256 s'en sont aperçues : la mise en forme, elle,
  était intacte.

```bash
python scripts/check_line_endings.py         # vérifie (code 1 en cas de dérive)
python scripts/check_line_endings.py --fix   # normalise, puis re-vérifie
```

Le contrôle lit les **octets**, jamais le texte : `\r` est un caractère comme un
autre, et aucun `readline()` ne peut nous le montrer sans l'avoir déjà interprété.
Il ne recopie pas non plus la règle : il la **lit** dans `.editorconfig`
(`end_of_line`, `insert_final_newline`, `charset`), **section par section** et
fichier par fichier — `[*]` donne le défaut, une section plus précise le remplace
pour ce qu'elle vise, et la dernière déclaration l'emporte. Écrire
`end_of_line = crlf` là-bas retourne donc le contrôle au lieu d'être combattu, et
un `end_of_line = cr` (une fin de ligne que rien ne lit) le fait sortir en `2`
plutôt que de deviner. Ce qu'est un fichier **texte** n'est pas décidé ici non
plus : le vocabulaire (binaires repérés par un octet NUL, suffixes et répertoires
exclus) vient de `core.secrets_audit`, où le scan anti-fuite s'en sert déjà — une
exclusion nouvelle n'a qu'un seul endroit où être écrite.

Un fichier à fins de ligne **hétérogènes** (un `\r` isolé, ou le style mélangé)
est nommé à part et laissé tel quel, même par `--fix` : ce n'est pas une
conversion oubliée mais le signe qu'un outil a tronqué ou recollé le fichier, et
le réécrire mécaniquement effacerait la trace de ce qui l'a produit. Codes de
sortie : `0` conforme, `1` dérive, `2` configuration illisible.

Trois gardes, parce qu'un contrôle qui ne tourne nulle part ne contrôle rien :

| Garde | Ce qu'elle fait |
|---|---|
| CI, job `python` | `python scripts/check_line_endings.py`, **avant** l'installation des dépendances — il ne tient qu'à la bibliothèque standard, comme les hooks |
| `tests/test_line_endings.py` | rejoue le parcours du dépôt **réel** (c'est ce test qui rougit si un `\r` revient) et éprouve le détecteur sur des fichiers fabriqués : CRLF, styles mélangés, fin de ligne finale absente, octets non-UTF-8, binaire, suffixe exclu, section `[*.{bat,cmd}]` |
| `.gitattributes` | `* text=auto eol=lf` — **supprime la cause** au lieu de la signaler : `eol=lf` prime sur `autocrlf`, donc un clone Windows n'a plus de CRLF à réparer |

La seule exception est **déclarée**, et une seule fois :
`[*.{bat,cmd}] end_of_line = crlf` dans `.editorconfig`, recopiée par
`*.bat text eol=crlf` dans `.gitattributes`. `cmd.exe` lit mal un script de
commande dont les lignes finissent en LF (étiquettes de `goto`, blocs `if`), et
`gradlew.bat` en porte six — c'est le seul `.bat` du dépôt. Sans ces deux lignes,
le fichier serait en violation chez tout le monde **au premier checkout** : le
gate et `.gitattributes` doivent dire la même chose, et un test vérifie qu'ils le
disent (`tests/test_line_endings.py`, `GitAttributesTest`).

### Les `except` muets (`tests/test_silent_handlers.py`)

Un `except` qui avale une panne sans rien en dire transforme une erreur en succès
apparent : c'est le mode de panne le plus cher du projet, parce que rien, plus
tard, ne le signale. Le corpus `tests/test_silent_handlers.py` lit **tout** le
code versionné — application *et* tests — et refuse tout handler qui n'émet
**aucun** signal. Est un signal, au sens de ce corpus, de quoi être vu par
quelqu'un d'autre que la ligne fautive : un `raise`, un `return`, un `yield`, un
`await`, un `assert`, ou un **appel** (imprimer, journaliser, avertir, noter un
motif…).

Deux échappatoires, et deux seulement :

* **nommer la raison dans le corps** — un handler qui capture l'exception
  (`as exc`) et **s'en sert** garde la cause, il n'est donc pas muet même s'il
  n'imprime rien. Le capturer sans l'utiliser ne suffit pas : `except E as exc:
  result = None` reste muet ;
* **porter une justification déclarée** — un commentaire `# sans signal : <raison>`
  sur la ligne du `except` ou dans son corps. La raison doit être **écrite** : un
  marqueur vide, ou plus court que dix caractères, ne vaut pas mieux qu'un
  silence. Les gardes d'import (`httpx`, `feedparser`, `dotenv`…) et les replis
  d'affichage en portent un, chacun pour sa raison, au lieu d'être dispensés par
  un oubli.

Le corpus lit l'arbre **réel** (`.venv`, `.pgtest` et les caches exclus, comme le
scan anti-fuite) et refuse d'être vide : un parcours cassé ferait passer le test
sans rien lire, donc un plancher de lecture et la présence de `main.py` sont
vérifiés à part. Le détecteur, lui, est éprouvé sur des sources **fabriquées** :
`pass`, `continue`, sentinelle seule, `as exc` utilisé ou non, marqueur vide, trop
court ou hors du handler, et `except*`.

### Vérifier le projet Supabase (`scripts/check_supabase.py`)

Une erreur de configuration ici ne se voit pas. La RLS de ce projet est en
« deny by default » **sans aucune policy** (migrations 005 à 011) : une clé
`anon` / publishable ne lit **rien** et n'écrit rien — sans jamais lever — et les
fonctions de `database/supabase_client.py` rendent `{}` / `[]` dans ce cas
**comme** dans celui d'une base vide. « Clé publique » et « base vide » ont donc
le même symptôme, et le seul moyen de les distinguer est de lire le **rôle** de
la clé configurée (jamais sa valeur).

```bash
python scripts/check_supabase.py                # lecture seule
python scripts/check_supabase.py --roundtrip    # + écriture / relecture / suppression
python scripts/check_supabase.py --json         # sortie exploitable par un script

# Viser une table, ou une famille : le reste n'est plus interrogé.
python scripts/check_supabase.py --only economic_events
python scripts/check_supabase.py --roundtrip --only economic_events
python scripts/check_supabase.py --only engine        # un groupe nommé
python scripts/check_supabase.py --only users,insights
```

`--only` restreint les **trois niveaux de table** — lecture, tables facultatives,
aller-retour — à ce qui est visé. C'est ce qu'on veut quand *une* table est en
cause et qu'on ne veut pas relire les autres ; c'est aussi le seul mode où
`--roundtrip` n'écrit **pas** sur les sept tables. Un nom de **groupe** désigne
une famille (`TABLE_GROUPS` : `core`, `engine`, `knowledge`), résolue en tables
concrètes — le titre du rapport les nomme, pour qu'un groupe ne se lise pas comme
une table qui n'existe pas. Un nom **inconnu** est un refus franc : code `2`, et un
message qui nomme groupes et tables disponibles. Il n'y a **aucun repli sur
« tout »**, précisément parce qu'avec `--roundtrip` un repli silencieux écrirait là
où on a demandé de ne pas aller.

Les identifiants viennent du `.env` de la racine, chargé **comme le fait
l'application** (`config.py`), ou des variables d'environnement — qui gardent la
priorité, ce qui permet de viser une autre base sans toucher au fichier. C'est la
même source que `python run.py` : un `.env` correct fait tourner le bot **et**
cette vérification, ou ne fait ni l'un ni l'autre.

Trois niveaux, du moins cher au plus engageant :

1. **la configuration** — `SUPABASE_URL` est bien l'URL de l'**API** du projet et
   `SUPABASE_SERVICE_KEY` porte bien le rôle `service_role`. Les **deux
   générations** de clés sont acceptées : l'ancienne (`service_role`, un JWT)
   voyage en `apikey` **et** en `Authorization: Bearer` ; la nouvelle
   (`sb_secret_…`, qui n'est pas un JWT) voyage en `apikey` seulement, la
   passerelle refusant le `Bearer`. C'est `build_supabase_client`
   (`database/supabase_client.py`) qui s'en charge, parce que `supabase-py`
   refuse d'emblée une clé sans points — `docs/SECRETS.md` détaille la rotation
   qui l'exige ;
2. **la lecture** — chacune des tables que l'application utilise répond
   (`REQUIRED_TABLES`, dans `database/supabase_client.py`) ; une table absente
   signale une migration non appliquée. Une table **facultative** absente
   (`OPTIONAL_TABLES`) est rapportée sans faire échouer la vérification ;
3. **l'aller-retour** (`--roundtrip`) — écriture, relecture puis suppression sur
   les **sept** tables que l'application utilise : les trois de la migration 011
   (`users`, `insights`, `pending_signals`), puis les quatre des migrations 005 et
   006 (`economic_events`, `macro_bias_logs`, `trade_post_mortems`,
   `adaptive_model_weights`). Avec `--only`, seules les tables visées sont écrites
   — à une exception près, `users` étant écrit pour satisfaire la clé étrangère de
   `pending_signals` (et nettoyé dans les deux cas). La relecture passe par les fonctions réelles de
   l'application (`insert_insight`, `create_pending_signal`,
   `update_signal_status`, `get_economic_events`, `get_learning_summary`,
   `get_adaptive_parameters`…) plutôt que par des requêtes réécrites dans le
   script.

Chaque table a son **propre verdict** : un échec sur l'une ne masque pas les
autres, et les quatre tables des migrations 005 et 006 sont éprouvées **même si
une étape précédente a échoué** — c'est justement quand quelque chose casse qu'on
veut savoir si le reste tient.

`trade_post_mortems` et `adaptive_model_weights` font exception à la règle « tout
passe par l'application » : elles sont écrites **directement**. Leur unique chemin
applicatif, `record_trade_settlement_and_learn()`, recalcule les poids de l'actif
et **réécrit une note de `knowledge_base`** à partir des derniers post-mortems —
une sonde de configuration n'a pas à modifier une ligne qui n'est pas la sienne, et
le nettoyage ne saurait pas la restaurer. Elles sont en revanche **relues** par
l'application (`get_learning_summary`, `get_adaptive_parameters`), ce qui est la
moitié qui compte : une ligne que l'application sait relire est une ligne
utilisable.

Codes de sortie : `0` tout est bon, `1` au moins un échec, `2` mauvais usage,
`3` **nettoyage incomplet**. Le troisième ne se confond avec aucun autre : les
échecs ordinaires se corrigent et se rejouent, celui-là a **laissé des lignes de
sonde dans la base** — une automatisation qui traite `1` comme « réessayer » ne
doit pas prendre les deux pour la même chose.

Un nettoyage incomplet a donc trois signaux, et ils ne dépendent pas les uns des
autres :

* le rapport porte `leftovers` — une entrée par ligne restée
  (`table.colonne=valeur`, avec l'erreur du refus), et pas seulement une phrase à
  relire : le code de sortie, l'alerte et la route en décident sans analyser du
  texte ;
* un **journal** sur `stderr` (`[sonde supabase] nettoyage INCOMPLET — …`,
  chaque ligne restée, et quoi en faire) : `--json` garde `stdout` pour lui, un
  consommateur machine n'a donc pas à filtrer un avertissement ;
* une **alerte** au chat d'exploitation (`TELEGRAM_ADMIN_CHAT_ID`, le même que la
  veille média). L'absence de chat et un envoi refusé sont **dits** (`no_admin_chat`,
  `send_failed`) : une alerte qui n'est pas partie doit se distinguer d'une alerte
  partie, sinon le silence se lit comme une bonne nouvelle.

L'aller-retour **écrit dans la base pointée par `.env`**, y compris en
production. Il supprime ses lignes de sonde (identifiants `probe-…`, `ff_probe_…`
pour `economic_events`, et l'actif fictif `PROBE-…` **unique à l'exécution** pour
`insights`, `trade_post_mortems` et `adaptive_model_weights`) ; si une suppression
échoue, il le dit explicitement, **en nommant la table et la ligne** à retirer à la
main.

Ce suffixe n'est pas cosmétique : deux vérifications qui tournent en même temps —
deux branches en CI, une relance pendant qu'une autre tourne — visent la **même**
base. Avec un actif constant, la première à nettoyer emportait la ligne de l'autre,
qui relisait un actif disparu et se déclarait en échec alors qu'aucune sonde n'était
cassée ; `total_trades` se lisait même sur la ligne de la voisine. Chaque ligne
n'appartient donc qu'à la sonde qui l'a écrite, et
`SimultaneousRunTest` le tient : une seconde exécution complète s'y intercale entre
l'écriture et la relecture de la première.

Chaque suppression est **retentée** avant ce constat, avec une attente croissante
et courte (`CLEANUP_RETRY_DELAYS` : 0,5 s puis 1 s). Un refus de la base est le
plus souvent un transitoire — coupure, `503`, pool saturé —, et conclure sur ce
hoquet coûte deux fois : la sonde sort en `3`, le code qu'une automatisation ne
rejoue **pas**, et l'opérateur est envoyé supprimer à la main une ligne qui n'existe
déjà plus. La reprise est sûre (supprimer une ligne déjà partie réussit, rien
d'autre n'est visé), et le pire cas reste bref : 1,5 s par ligne restée. Un refus
qui se répète, lui, est un fait : le reste en base est déclaré, avec le motif de la
**dernière** tentative — c'est celui qui décrit l'état dans lequel la sonde a
abandonné.

Cette propriété est **vérifiée sur la base**, pas relue dans le rapport :
`tests/test_supabase_config.py::RoundtripLeakTest` injecte une panne à **chaque**
étape de l'aller-retour — écriture, relecture et suppression, sur les sept tables,
points lus dans le journal de la doublure partagée — et exige qu'aucune ligne de
sonde ne survive. Quand la suppression elle-même est refusée, ce qui reste en base
doit être **exactement** ce que la liste `leftovers` nomme : ni ligne cachée, ni
ligne annoncée à tort. Et parce qu'un nettoyage qui viderait la table passerait ce
contrôle, un test sème des lignes qui ne sont pas celles de la sonde et exige
qu'elles survivent : la sonde tourne sur la base de **production**, elle n'a le
droit de supprimer que ses propres lignes.

La **même** vérification se lance sans shell, depuis l'application :
`GET /admin/supabase/check` (les trois niveaux, **sans rien écrire**) et
`POST /admin/supabase/roundtrip` (le `--roundtrip`, sur des tables nommées) — voir
« Vérifier la base depuis l'application », plus bas. Le script garde la logique,
la route ne fait que l'exposer.

#### La veille périodique, en lecture seule (`workers/supabase_watch.py`)

La sonde sait répondre, mais il faut **penser** à la lancer — et personne ne pense
à lancer une vérification qui marchait hier. L'auto-loop la lance donc toute seule,
au rythme réglé, et n'alerte que sur un **changement de verdict** :

* `SUPABASE_WATCH_MINUTES` — 30 minutes par défaut, bornée à 5..1440 ; `0`
  **éteint** la veille, comme `MEDIA_RECONCILE_MINUTES`. Surchargeable en base
  (`bot_settings`, migration 010, clé `supabase_watch_minutes`) ;
* la veille appelle la sonde avec `roundtrip=False`, et c'est écrit **dans** le
  module, pas reçu en paramètre : une veille périodique qui écrirait dans la base
  de production toutes les cinq minutes serait exactement le dégât qu'on veut
  éviter. Le seul mode qui écrit reste `--roundtrip`, lancé à la main ;
* ce qui déclenche un message est un **changement** : vert→rouge (la panne à
  l'instant où elle arrive) et rouge→vert (sans quoi personne ne sait jamais si
  c'est réglé). Un état stable ne se réannonce pas — sinon une base cassée
  alerterait à chaque passage, indéfiniment, jusqu'à ce qu'on coupe la veille ;
* trois exceptions, chacune pour une raison : le **premier** passage parle des
  échecs qu'il constate (une base déjà cassée au démarrage ne doit pas se lire
  comme un silence rassurant, et le message le dit — « constat du premier
  passage ») ; une vérification **absente** d'un rapport garde son verdict connu,
  donc son retour se compare à ce qu'elle était ; une alerte qui **n'a pas pu
  partir** n'est pas comptée comme passée — chat admin absent ou Telegram en
  panne, le passage suivant redira le même changement ;
* rien n'est persisté : la mémoire des verdicts vit en processus. Un redémarrage
  repart d'un constat, et le constat d'un échec est justement ce qu'on veut
  apprendre au démarrage ;
* `component_health()["supabase_watch"]` publie `every_minutes` et `admin_chat` :
  une veille éteinte, ou qui tourne sans destinataire, se **lit** — au lieu de se
  découvrir en cherchant pourquoi aucune alerte n'arrive.

`tests/test_supabase_watch.py` tient ces propriétés ; le câblage d'`auto_loop.py`
est vérifié en relisant sa source, `feedparser` n'étant pas disponible dans
l'environnement de test.

Ce qui est refusé dans `SUPABASE_URL`, parce que ce sont les erreurs qui coûtent
une soirée de débogage :

* un `http://` — la clé `service_role` circulerait en clair ;
* une URL qui porte un chemin (`/rest/v1`, `/auth/v1`) — le client ajoute ce
  chemin lui-même, il faut coller l'URL du **projet** ;
* l'URL **Postgres** (`db.<projet>.supabase.co`) — c'est la base, pas l'API ;
* les placeholders de `.env.example` (`xxxxx`, `your-project`, `example`).

Un domaine auto-hébergé (`https://supabase.interne.local`) est en revanche
**accepté** : on ne refuse que ce qui ne peut pas marcher.

Les mêmes contrôles gardent le **démarrage** : `core/config_runtime.py` porte la
seule lecture de l'URL et du rôle de la clé, utilisée à la fois par ce script et
par `EnvConfig.required_issues()`. Une clé publique y est donc un **blocage de
démarrage**, et non un simple avertissement, et `/preflight` publie
`component_health()["supabase"]` (`ready`, `key_role`, `url_issue` — jamais la
clé). Ce contrat tourne partout : `tests/test_supabase_config.py`.

### Les réglages effectifs de l'audit, publiés par `/health` et `/preflight`

Le démarrage publie aussi ce que l'audit des secrets **appliquera**, pour qu'un
opérateur n'ait pas à ouvrir un journal pour le savoir :

* `GET /preflight` (protégé) porte `secrets_audit` : le plafond de rotation
  appliqué (`max_age_days`) et sa provenance (`env` ou `default`), le reproche
  éventuel quand la variable écrite n'a pas été appliquée telle quelle
  (`max_age_problem`), l'état du registre (`ledger_state` : `ok`, `missing`,
  `corrupt`, avec son détail et son nombre d'entrées), le chemin du registre, et
  le rôle de la clé Supabase (`supabase_key_role`) — jamais la clé ;
* `GET /health` (public, sans clé) n'en publie qu'un **résumé** : le plafond
  appliqué et l'état du registre. Ni le chemin du fichier — il révèle
  l'arborescence du serveur — ni le rôle de la clé, qui dit quelle puissance le
  service porte (`service_role` ouvre tout) : ces deux faits restent derrière la
  clé interne, sur `/preflight`.

Un plafond de rotation et un registre se lisent donc au même endroit que l'URL
Supabase, sans lancer l'audit ni lire un seul secret. Contrat :
`tests/test_secrets_audit.py` (`EffectiveSettingsTest`) et
`tests/test_supabase_config.py` (`SecretsAuditSettingsTest`).

La vérification contre la **vraie** base vit dans `tests/test_supabase_live.py`.
Elle est **dormante par défaut** et ne se réveille que sur demande explicite :

```bash
SUPABASE_LIVE="1"              python -m unittest tests.test_supabase_live  # lecture seule
SUPABASE_LIVE_ROUNDTRIP="1"    python -m unittest tests.test_supabase_live  # + écriture
```

Cet opt-in n'est pas une coquetterie : la configuration est lue à l'import du
module, et `tests/test_api_auth_integration.py` injecte une **fausse** config
Supabase dans l'environnement en s'important (il ne la restaure pas, exprès :
`config.py` fige ses constantes à l'import). Un fichier live qui se croirait
configuré à cause de ça écrirait dans une base au hasard. Le contrat est
testé : `tests/test_supabase_config.py` recharge le fichier live avec un
environnement pollué et vérifie qu'il reste endormi.

### Réparer les états de risque inutilisables (`scripts/repair_risk_state.py`)

Le garde-fou de risque persiste une référence de solde par utilisateur
(`user_risk_state.starting_balance`, `.daily_start_balance`) et **refuse** de
trader quand elle n'est pas exploitable — « 5 % de 0 » n'est pas un plafond. Ce
refus est voulu ; ce script existe pour qu'il soit **réparable**.

```bash
python scripts/repair_risk_state.py                  # liste, sans rien écrire
python scripts/repair_risk_state.py --apply          # écrit les reprises proposées
python scripts/repair_risk_state.py --set 123:starting_balance=5000 --apply
python scripts/repair_risk_state.py --json           # sortie machine
```

Deux formes du défaut, deux comportements du garde — et c'est la seconde qui est un
piège :

| Référence | Ce que fait le garde | Ce que le script répare |
|---|---|---|
| **présente mais ≤ 0** | refuse le trade en nommant le champ | écrit une valeur positive |
| **absente (`NULL`)** | la **dérive du solde courant** — il ne refuse pas, et la perte relative à cette référence disparaît en silence | écrit une valeur positive |

La reprise ne devine pas : elle repose sur le **capital configuré** de
l'utilisateur (`user_preferences.paper_equity`), c'est-à-dire la valeur que
l'initialisation aurait écrite — **jamais le solde courant**, qui remettrait le
drawdown à zéro. Quand ce capital n'est pas connu, la ligne reste `needs-value` et
le script dit quoi fournir (`--set`) : il n'invente rien. Une valeur ≤ 0 passée à
`--set` est refusée — c'est le défaut à réparer. Une journée **close** est signalée
sans écriture : le garde repart d'un compteur journalier frais au prochain solde lu.

Deux invariants : **aucune ligne n'est supprimée** (la supprimer la ferait
réinitialiser au prochain solde lu, donc effacerait la perte en silence) et **aucune
valeur non positive n'est écrite**. Codes : `0` rien à réparer, `1` il reste des
lignes, `2` usage ou base indisponible. Contrat :
`tests/test_risk_state_repair.py`.

Le refus ne reste pas muet pour l'exploitant : le garde tient un **compteur dédié**
et journalise chaque refus de référence inutilisable sous un préfixe unique
(`risk_guard.reference-unusable`), avec l'utilisateur et le champ fautif. Le
premier refus d'un utilisateur, et tout changement de champ, sortent en
**WARNING** ; les répétitions descendent en `INFO` — un compte bloqué qui retente
ne remplit pas le journal à lui seul, mais le compteur continue de monter.

```bash
curl -H "X-API-Key: $INTERNAL_API_KEY" \
  https://<service>/webhook/admin/risk/reference-unusable
```

La route rend `{"total": …, "users": {"<user_id>": {"count": …, "fields": [...],
"since": …}}}` — de quoi voir **qui** est bloqué, par **quel** champ et depuis
quand, au lieu de le découvrir dans un journal. Elle ne répare rien : la réparation
reste `scripts/repair_risk_state.py`. Contrat : `tests/test_risk_guard.py`
(`ReferenceUnusableVisibilityTest`) et `tests/test_admin_router.py`.

### Médias Telegram (`database/media_store.py`)

Le client fait le lien entre le bucket et la base :

```python
from database import media_store

row = media_store.upload_media(
    data,                                  # octets du fichier
    media_type="photo",                    # photo | video | document | audio | voice | other
    chat_id="@signals", message_id=42,
    file_name="chart.png", caption="BTC",
)

media_store.list_media(source="telegram", media_type="photo", limit=20)
url = media_store.create_signed_url(row["storage_path"], expires_in=3600)
```

Trois points de conception :

* la clé d'objet est **déterministe** dès que `chat_id` et `message_id` sont
  fournis (`telegram/<chat>/<message>-<fichier>`) : rejouer un message réécrit le
  même objet au lieu de laisser des doublons. `upsert=True` remplace alors la
  ligne de même `storage_path` ;
* si l'insertion en base échoue **après** un upload réussi, l'objet est
  supprimé : sans cela le bucket accumulerait des fichiers que plus aucune ligne
  ne référence ;
* l'**empreinte SHA-256** des octets part avec la ligne (`metadata`,
  `FINGERPRINT_KEY`) — **calculée**, jamais reçue d'un appelant, qui pourrait
  attester d'octets déposés par quelqu'un d'autre. C'est la seule référence qui
  permettra de **prouver** qu'un objet restauré est bien le fichier d'origine :
  une taille identique ne le prouve pas (voir « Réconcilier le bucket »).

Le bucket est **privé** (RLS, aucune policy) : il n'existe pas d'URL publique.
Pour exposer un fichier, passe par un lien signé temporaire
(`create_signed_url`).

Le module offre aussi un point d'entrée **orienté média** vers le texte indexé :
`replace_media_chunks()`, `list_media_chunks()` et `search_chunks()`. Ce sont de
simples **délégations** vers `database/knowledge_index.py` (qui reste propriétaire
du découpage, des embeddings et du RPC `match_knowledge_chunks`) — la logique
n'existe qu'une seule fois, mais la durée de vie d'un média (octets, description,
texte) se pilote depuis un même module.

Ce n'est pas une intention, c'est la route effective : les **six** sites qui
écrivent les morceaux d'un média prennent `replace_media_chunks` pour défaut —
`telegram_media.ingest_media` (ingestion d'un message), `ingest_album` (ingestion
d'un album), `review_media` (verdict de revue, dont `re` refait l'extraction),
`reprocess_media` (reprise depuis le fichier stocké, partagée par le bouton
`↩️ Réindexer` et `/transcribe`), `retranscribe_media` (relance après le diagnostic
de `/transcribe`) et `ai.media_indexing.extract_and_index` (l'extraction partagée
par les deux routes d'ingestion, bot et scraper public). `tests/test_media_store.py`
le tient par **trois** contrôles : site par site dans le **texte** — pour qu'un site
nouveau soit vu sans être inscrit nulle part —, par **identité de l'objet**
embarqué, ce qui distingue un défaut réellement routé d'un texte qui y ressemble, et
par l'**appariement** des deux, qui interdit qu'un site ne soit vérifié que par le
texte : une route ajoutée sans être inscrite fait rougir, une route inscrite qui
n'existe plus aussi. La **lecture** de ces morceaux est exposée telle quelle au
tableau de bord par `GET /media/{id}/text`, et la **liste** qui donne les
identifiants à demander par `GET /media` (voir « API interne »).

Chaque maillon a ses tests ; aucun ne dit que l'**ensemble** tient — qu'un objet
déposé est décrit par la ligne dont l'identifiant sert ensuite à indexer, que le
texte indexé ressort d'une recherche, et que la recherche retrouve bien **ce**
média. `tests/test_media_cycle.py` enchaîne donc `upload_media` →
`replace_media_chunks` → `search_chunks` sur un Supabase **en mémoire**, et
vérifie au passage ce qu'aucun test unitaire ne voit : le rejeu d'un message ne
duplique ni l'objet, ni la ligne, ni les morceaux ; le classement suit le sens du
texte, pas l'ordre d'insertion ; les filtres d'actif et de source font bien leur
travail ; et une indexation qui échoue laisse le média en place, sans texte
cherchable.

Cette doublure n'est pas complaisante : les écritures y **persistent**, les
identifiants y sont attribués comme Postgres les attribue, et
`match_knowledge_chunks` y est rejoué clause par clause (jokers d'actif et de
régime, classement par préférence puis par distance, `greatest(match_count, 1)`),
les vecteurs venant d'une doublure d'embeddings **déterministe** — donc la
similarité cosinus est réelle. Un RPC qui rendrait toujours tout passerait au vert
sur un cycle pourtant cassé.

Elle est **partagée** : `tests/supabase_double.py` ne sert pas qu'au cycle, et
remplace les doublures que les fichiers de tests portaient chacun —
`test_media_store.py`, `test_knowledge_index.py`, `test_media_cycle.py`, puis
`test_media_reconcile.py`, `test_telegram_scan_config.py`, `test_learning_gd.py`,
`test_api_auth_integration.py`, `test_supabase_watch.py`, puis
`test_supabase_config.py`. Plusieurs
**ignoraient les filtres** qu'elles voyaient passer (`rows` était rendu tel quel)
et servaient des pages écrites d'avance ; d'autres tenaient les réglages dans un
dictionnaire parallèle, où une écriture ratée restait lisible. La version unique
applique `eq`, `in_`, `or_`, l'ordre, la projection et `.range()` comme
PostgREST : une ligne semée sans la colonne sur laquelle la lecture filtre ne
ressort pas, et les tests de pagination exercent donc la vraie pagination. Ce que
la doublure offre est décrit dans son en-tête : le **journal** d'appels
(`client.calls`, `table.calls`), les pannes simulées (`read_error`,
`insert_error`…) et les identifiants (`id_factory`).
`tests/test_supabase_double.py` ferme la porte derrière : un fichier qui utilise
la doublure partagée ne peut plus redéfinir son propre client — c'est un contrat,
pas une convention.

Le mode **lecture seule** (`read_only=True`) en est le prolongement. Toute
écriture — insertion, mise à jour, suppression, dépôt dans le bucket — lève un
`ReadOnlyError` qui **nomme** l'opération et la table visée, et rien n'entre dans
le journal : une écriture refusée n'a pas eu lieu, donc un test ne peut pas la
confondre avec un effet de bord qui, lui, serait passé. Trois choix y sont
assumés : le refus tombe à la **construction** de l'écriture, avant tout contact
avec le serveur ; un RPC est supposé **écrire** tant qu'on ne déclare pas le
contraire (`handle_rpc(name, handler, writes=False)`), parce que la doublure ne
peut pas lire le corps d'une fonction Postgres et que laisser passer en silence
serait exactement le trou que ce mode prétend fermer ; et le bucket **suit son
client**, si bien que basculer `client.read_only` après coup le ferme aussi.

C'est ce qui rend vérifiable la promesse de `scripts/check_supabase.py` : ce
script se lance sans `--roundtrip` sur une base de **production**, et
`tests/test_supabase_config.py` exécute ce chemin-là sur un client en lecture
seule. Une écriture qui se glisserait un jour dans le chemin de lecture ne
passerait pas « parce que la base d'essai l'accepte » : elle ferait rougir le
test.

### Réconciliation bucket ↔ `knowledge_media` (`scripts/reconcile_media.py`)

Un **orphelin** est un objet présent dans le bucket mais **absent de
`knowledge_media`** : upload interrompu avant l'insertion, nettoyage automatique
qui a échoué, ou ligne supprimée à la main. Il occupe du stockage sans être
référencé nulle part — donc invisible dans l'application.

```bash
python scripts/reconcile_media.py                     # liste les orphelins (aucune suppression)
python scripts/reconcile_media.py --delete            # les supprime
python scripts/reconcile_media.py --prefix telegram/thehalalwinningteam
```

Le script **ne supprime rien par défaut** : il liste, puis indique comment
retirer. `list_orphan_objects()` parcourt le bucket **récursivement** (`list()`
n'est pas récursif : les sous-dossiers y apparaissent comme des entrées sans
`id`), croise avec les `storage_path` de la table lus **page par page**, et
`delete_objects()` retire le reste **par lots** (l'API Storage refuse les listes
trop longues). Codes de sortie : `0` réconciliation faite, `1` base ou Storage
inaccessible — de quoi alerter depuis un cron.

La même question se pose **sans shell** : `GET /admin/media/orphans` (voir
« API interne ») interroge la même fonction depuis un navigateur, en lecture
seule — la suppression reste au script, qui en garde le `--delete`.

Le **sens inverse** — une ligne dont l'objet a disparu du bucket — est traité par
`GET /admin/media/missing` et `POST /admin/media/missing/repair` :
`media_store.list_missing_objects` rend les lignes (et non leurs chemins : sans
`telegram_file_id` ni `chat_id`/`message_id`, aucune réparation n'est possible),
et `core/media_repair.py` re-télécharge les octets. Le script, lui, ne touche pas
au Storage dans ce sens : il ne fait que **lister** l'inverse (les deux sens sont
visibles dans `/admin/media/*`).

La restauration est **vérifiée**, pas supposée : l'empreinte SHA-256 enregistrée à
l'ingestion est recalculée sur les octets re-téléchargés et comparée
(`fingerprint_matches`). C'est la seule mesure qui distingue « le fichier
d'origine est revenu » de « un fichier a été retrouvé » — la voie de l'aperçu
public sert volontiers une copie réduite, et un compte d'octets juste ne dit rien
sur le contenu. Un `null` (média antérieur au champ) vaut « on ne peut pas
prouver », jamais « ça correspond ».

### Extraction du contenu des médias (`ai/media_extractor.py`)

Après l'upload, le **contenu** du média est extrait puis indexé dans
`knowledge_chunks` (texte découpé, puis **vectorisé** avec les embeddings Gemini
lorsque `GEMINI_API_KEY` est fourni ; sans elle, le texte reste indexé et
l'embedding est simplement laissé nul) :

| Média | Méthode | Dépendance |
|---|---|---|
| Image (graphique) | vision Gemini — tendance, niveaux, chiffres lisibles | `GEMINI_API_KEY` |
| PDF | `pypdf`, extraction locale (aucun réseau) | `pypdf` |
| Vidéo / note vocale / fichier audio | transcription Groq `whisper-large-v3`, avec repli **local** `faster-whisper` | `GROQ_API_KEY`, `groq` ; repli : `faster-whisper` |

Ces règles sont appliquées par **un seul** pipeline, `ai/media_indexing.py` : il
compose le texte indexable (légende d'abord), détecte l'actif depuis cette légende
puis depuis le contenu, indexe les morceaux et enregistre l'étiquette. Les deux
routes d'ingestion — les médias du bot et ceux du scraper public — l'appellent :
l'extraction ne peut donc pas diverger entre une photo envoyée au bot et la même
photo lue dans un canal.

Les dépendances sont importées **au moment de l'usage** : une installation sans
`pypdf` ou `groq` démarre et ingère quand même les médias — seule l'extraction
échoue, avec un motif explicite. Une extraction impossible (PDF scanné, clé
absente, format non pris en charge…) ne remet **jamais** en cause l'ingestion : le
fichier est déjà stocké, et le compte-rendu Telegram l'indique à l'utilisateur.

Le **résultat** de chaque tentative est enregistré sur la ligne média
(`media_store.set_extraction_outcome`, `metadata.extraction` : `ok`, `method`,
`chars`, `chunks`, `reason`, horodatage — jamais le texte, qui est déjà indexé).
C'est ce qui rend un échec **durable** : le compte-rendu défile, et sans cette
note un média jamais lu serait indiscernable d'un média lu correctement dès qu'il
porte une légende (les deux ont des morceaux indexés). Cette note est écrite par
`extract_and_index`, donc par les deux routes d'ingestion et par toutes les
reprises, et c'est elle que lit `/transcribe`. Elle est distincte du verdict de
revue : l'une dit ce que la **machine** a produit, l'autre ce qu'un **humain** en
a dit.

Ce qui manque pour qu'une extraction aboutisse est exposé séparément, par
`ai.media_extractor.extraction_readiness()` : il classe le média (image, PDF,
audio/vidéo) et rend `ready`, les `missing` (`GEMINI_API_KEY`, `pypdf`,
`GROQ_API_KEY` / `faster-whisper`) et un conseil. C'est un verdict sur les
**ingrédients**, pas une promesse de texte : les poids du modèle local, un PDF
scanné ou un quota épuisé ne se découvrent qu'en essayant.

#### Transcription sans Groq : le repli local (`faster-whisper`)

La transcription était la **seule** extraction qui exigeait à la fois un accès
réseau et un compte : sans `GROQ_API_KEY`, un vocal n'était plus transcrit du
tout. `transcribe_media()` choisit donc sa voie :

| Situation | Ce qui transcrit |
|---|---|
| `GROQ_API_KEY` configurée | Groq `whisper-large-v3` — c'est la voie nominale, plus rapide, et rien à télécharger |
| Groq a échoué (réseau coupé, quota épuisé, format refusé…) | Le repli **local**, sur le CPU : c'est le cas de la machine hors ligne qui *a* une clé |
| `GROQ_API_KEY` absente | Le repli local directement — la voie nominale est inutilisable par construction, elle n'est même pas essayée |

Deux choses se paient, et sont dites plutôt que cachées. Le repli n'est **pas**
dans `requirements.txt` : il pèse des centaines de mégaoctets (`ctranslate2`,
`av`, poids du modèle) pour un chemin que la production n'emprunte jamais —
l'installer, c'est `pip install faster-whisper`. Et les **poids** du modèle
doivent avoir été téléchargés **une fois** : hors ligne, ce premier
téléchargement est justement impossible, donc `LOCAL_WHISPER_MODEL` doit pointer
un dossier local. Le motif d'échec le dit explicitement, plutôt que de laisser
un « échec de transcription » sans cause. `/preflight` publie les deux —
`components.transcription.groq` et `.local_fallback` — pour qu'on voie d'un coup
d'œil si la transcription reste possible. Le modèle est chargé **une fois** et
gardé en mémoire (des centaines de mégaoctets, et le pipeline appelle
l'extraction depuis `asyncio.to_thread`).

Le compte-rendu montre aussi le **texte extrait** en plus du nombre de
caractères : l'utilisateur peut donc vérifier d'un coup d'œil que la vision a bien
lu le graphique ou que Whisper a transcrit la bonne langue, au lieu de se fier à
un seul compteur. Le texte est montré **même quand l'indexation a échoué** — il a
bien été extrait, c'est justement ce qu'on veut contrôler.

Deux cas, jamais de milieu — **aucun aperçu tronqué** :

| Taille du texte indexé | Ce que reçoit l'utilisateur |
|---|---|
| ≤ `EXCERPT_CHARS` (400 car.) | le texte **entier** dans le message (espaces normalisés) |
| au-delà | le texte **entier** en pièce jointe `.txt` (+ son nom dans le message) |

Un fragment qui finissait par « … » ne permettait pas de relire ce qu'on valide :
un PDF de plusieurs pages ou une transcription de vingt minutes était jugé sur
400 caractères. `attachment_needed()` est donc le **même critère** que la
troncature de l'aperçu (même normalisation, même limite) : la pièce jointe existe
si et seulement si l'aperçu perdrait du texte, et les deux ne peuvent pas
diverger. Le fichier contient le texte **indexé** — exactement la chaîne remise à
`knowledge_chunks`, donc ce que les embeddings, les prompts et `/search` liront,
pas une réextraction. Son nom est dérivé de celui du média (`photo_42.jpg` →
`photo_42-extraction.txt`), nettoyé des chemins et caractères que Telegram refuse,
avec l'identifiant du média en secours si le nom ne dit rien (`.pdf`, vide).

Si l'envoi du document échoue, le handler le **dit** et rend l'aperçu tronqué dans
un message de repli : annoncer une pièce jointe qui n'arrivera jamais laisserait
l'utilisateur sans rien pour juger. Le module média ne construit que les octets et
le nom (`attachment_for`) — il n'importe pas `python-telegram-bot`, c'est `main.py`
qui envoie le document.

Le texte **indexé** (`knowledge_chunks`) est la **légende du message suivie du
contenu extrait** — les deux sont cherchables au même titre. Point important : la
légende seule est indexée **même si l'extraction échoue** (photo sans vision, PDF
scanné, quota épuisé). Une légende « BTC support 64k » reste alors trouvable par
`/search` et remontable dans le contexte du moteur de décision, et elle nomme
souvent l'actif mieux que la description visuelle. Le compte-rendu Telegram le
distingue : `gemini_vision + légende`, ou `Légende indexée seule` avec le motif
d'échec.

Les handlers couvrent **PHOTO, VIDEO, DOCUMENT, VOICE et AUDIO**. La distinction
entre `voice` (note vocale enregistrée dans Telegram, `.ogg`) et `audio` (fichier
joint mp3/m4a, nom et type MIME conservés) compte : sans handler `AUDIO`, un mp3
envoyé par l'utilisateur était silencieusement ignoré. Les deux passent par le
même extracteur.

### Revue d'une extraction — boutons ✅ / ❌ / ↩️

Un modèle de vision peut décrire de travers un graphique, et une transcription
trahir une langue. Sans revue, ce bruit entrerait **définitivement** dans les
prompts et dans `/search` : l'aperçu du compte-rendu est donc suivi de deux
boutons.

| Bouton | Effet |
|---|---|
| `✅ Extraction correcte` | rien n'est modifié : le verdict est enregistré, et le bouton ❌ reste |
| `❌ Rejeter (retirer de l'index)` | les morceaux du média sont **supprimés** de `knowledge_chunks` |
| `↩️ Réindexer` | refait l'extraction depuis le fichier **stocké** et la réindexe (`reprocess_media`, partagé avec `/transcribe`) |

Points de conception :

- **Le fichier n'est jamais supprimé** par un rejet, seulement les morceaux
  indexés (`knowledge_index.delete_chunks`). C'est ce qui rend le verdict
  réversible, et c'est indispensable : la **déduplication** ferait qu'un renvoi du
  même fichier ne réindexerait rien (« ⏭️ Déjà stocké »). Le bouton `↩️ Réindexer`
  est donc le seul chemin retour, et il n'a besoin ni de Telegram ni de réseau
  média : le descripteur d'extraction est reconstruit depuis la ligne
  `knowledge_media` (type, MIME, nom, légende) et le fichier relu du bucket.
- **Le verdict est une annotation, pas un état de pipeline** : il vit dans
  `metadata` du média (`media_store.set_review_status`, clé `extraction_review`) —
  aucune migration, aucune colonne que rien d'autre ne lirait. L'état qui compte
  est déjà porté par les morceaux : *rejeté* veut dire « il n'y a plus de
  morceaux ». Il est fusionné dans les métadonnées existantes, jamais écrit par
  dessus, sinon l'origine Telegram et le `media_group_id` de l'album
  disparaîtraient.
- **Une réindexation est une extraction neuve** : si son texte dépasse
  `EXCERPT_CHARS`, elle arrive elle aussi en pièce jointe `.txt` — sinon on
  validerait la deuxième tentative sur le même fragment trompeur que la première.
- **Le clavier suit l'état.** Après un rejet, seul `↩️ Réindexer` subsiste ;
  après une réindexation réussie, le clavier ✅ / ❌ revient — c'est une
  extraction **neuve**, elle doit être relue comme telle ; après une validation,
  seul ❌ reste (valider n'est pas irréversible). Si l'action a échoué, le clavier
  complet est rendu pour réessayer, et il disparaît complètement si le média
  n'existe plus (aucun verdict ne pourrait aboutir). `telegram_media.follow_up_buttons()`
  porte cette politique, testée cas par cas.
- **Des boutons seulement quand il y a à relire** : `review_target()` renvoie
  `None` pour une ingestion échouée, un doublon ou une extraction qui n'a indexé
  aucun morceau — pas de bouton sans effet.
- **Le verdict se voit dans `/media`** (`✅ extraction validée` /
  `❌ extraction rejetée (non indexée)`) : un média rejeté a zéro morceau, il
  serait sinon indiscernable d'une extraction simplement échouée.
- **Préfixe `med:`** dans `callback_data`, filtré *avant* le handler générique
  (comme `rag:` et `approve:`) ; la revue se limite à `ok`/`no`/`re` pour tenir
  dans les 64 octets de Telegram avec un UUID. Un clic est attribué au compte qui
  l'a fait (`metadata.extraction_review.by`) — le bot n'a pas de modèle de rôles,
  comme pour les boutons d'approbation de signal.
- **La revue ne dépend pas d'un compte-rendu délivré** : `/media` porte les mêmes
  boutons, **ligne par ligne** (préfixe `medl:`, voir la section `/media`), et
  `/pending` la même chose pour les extractions **plus anciennes que la page** de
  `/media` (préfixe `medp:`, pagination navigable en `medpg:`, voir la section
  `/pending`). Un compte-rendu de canal
  parti vers un chat privé que personne n'a ouvert, ou noyé dans l'historique, ne
  condamne donc plus l'extraction : elle reste relisible plus tard, depuis l'une
  ou l'autre liste. C'est aussi ce qui permet de relire les médias que
  le bot n'a **pas** reçus (`scrapers/telegram_channel.py`,
  `scripts/reconcile_media.py`) — ils n'ont jamais eu de message pour porter le
  clavier, mais ils ont une ligne dans `/media`. Le fichier étant dans le bucket,
  `↩️ Réindexer` refait l'extraction depuis `knowledge_media` sans repasser par
  Telegram.

### Étiquetage d'actif — l'actif associé à un média (`/tag`)

Chaque média ingéré est étiqueté avec un actif quand c'est possible. Cette
étiquette est l'`asset` de `knowledge_chunks`, donc le filtre **préférentiel** de
`match_knowledge_chunks` :

| Cas | Effet sur la recherche filtrée |
|---|---|
| média étiqueté `BTC-USD` | remonte en tête d'une recherche `BTC-USD`, et **disparaît** de celle des autres actifs |
| média non étiqueté (`asset` NULL) | reste candidat pour **tous** les actifs, sans bonus de classement (le joker) |

Étiqueter, c'est donc dire « ce graphique parle de BTC-USD » : c'est ce qui fait
remonter le bon média dans un contexte d'analyse, et ce qui empêche un graphique
Ethereum de se glisser dans une analyse BTC-USD. Auparavant rien n'était
étiqueté : **tous** les médias étaient des jokers, et le filtre d'actif ne
tranchait rien pour eux.

L'étiquette ne fait pas que **classer** : elle **passe le seuil**. Celui-ci (voir
« Le seuil de similarité est mesuré sur la base, pas choisi ») mesure le bruit du
corpus — ce que la base répond à des questions qui ne la concernent pas —, or un
média que tu as explicitement désigné pour l'actif n'entre pas dans ce cas : sa
présence dans le prompt ne dépend donc pas d'un score cosinus. C'est le seul cas
où le seuil est ignoré, et il reste borné par `TAGGED_HARD_FLOOR` (0,2), le seul
chiffre qui veut dire « sans aucun rapport » quelle que soit la base : une
étiquette posée par erreur sur un contenu étranger à l'actif reste ainsi écartée.
Sans ce cas particulier, une base homogène (seuil mesuré à 0,9) aurait été
aveugle à **toutes** les étiquettes, y compris celles posées exprès.

**Détection automatique** — à l'ingestion, `core/asset_tags.detect_asset` cherche
l'actif dans la **légende** d'abord (elle le nomme explicitement), puis dans le
texte extrait. Le compte-rendu annonce le résultat et son origine :

```
• Actif associé : BTC-USD (légende)
• Actif : non reconnu — le média reste candidat pour tous les actifs.
  Pour l'étiqueter : `/tag BTC-USD` en réponse au message du média.
```

Les signaux sont reconnus dans cet ordre de netteté — le premier trouvé gagne :

| Signal | Exemples | Pourquoi en premier |
|---|---|---|
| marqueur explicite | `$BTC`, `#btc-usd` | c'est l'intention de l'auteur, sans ambiguïté |
| notation de paire | `BTC-USD`, `btc/usdt` | une paire est un symbole, pas un mot |
| nom en toutes lettres | `bitcoin`, `ethereum`, `tesla` | insensible à la casse |
| ticker nu, sigle qui **n'est pas un mot** | `btc`, `eth`, `aapl` | **toutes les casses** : « btc support 64k » est la forme ordinaire d'une légende |
| ticker nu, sigle qui **est un mot** | `SOL`, `DOT`, `META`, `SPY` | **majuscules exigées** : « le sol du graphique » n'est pas Solana |

Les deux dernières lignes sont un **partage du même tableau**, et c'est le point
qui décide de tout : exiger les majuscules pour *tous* les tickers courts revenait
à ne rien détecter dans la légende — celle qui nomme l'actif le mieux —, donc à
laisser `asset` NULL sur la quasi-totalité des médias, donc à n'avoir **aucun**
actif à classer dans `match_knowledge_chunks` : tous les médias restaient des
jokers, et le filtre préférentiel ne tranchait rien. À l'inverse, accepter « sol »
en minuscules étiquetterait « le sol du graphique » sur Solana — et une étiquette
fausse *exclut* le média des recherches de l'actif réel. D'où deux tableaux,
`UNAMBIGUOUS_TICKERS` et `AMBIGUOUS_TICKERS` : dans un même texte, c'est la
**position** qui tranche entre deux tickers nus (« SOL puis btc » → `SOL-USD`,
« btc puis SOL » → `BTC-USD`). Un test vérifie que toute base de `CRYPTO_BASES`
est classée dans **exactement** un des deux : ajouter une base sans dire si son
sigle est un mot est un oubli silencieux, et c'est ce test qui le dit.

Le reste du vocabulaire reste **fermé** (`core/asset_tags.py`) : ce qui n'est pas
listé n'est pas étiqueté. C'est délibéré — un actif **faux** est pire qu'une
absence, puisque NULL est un joker alors qu'une étiquette fausse *exclut* le média
des recherches du vrai actif. D'où deux garde-fous : les sigles qui sont des mots
exigent les majuscules, et une base crypto inventée n'est pas transformée en paire
(`fraisUSD` n'est pas un actif).

**Ce que le filtre fait vraiment, mesuré sur PostgreSQL + pgvector** (et non
déduit du SQL) : un morceau étiqueté `BTC-USD` mais **orthogonal** à la requête
(similarité 0) passe **avant** un morceau non étiqueté identique à la requête
(similarité 1) ; un morceau étiqueté `ETH-USD`, pourtant identique à la requête,
n'est pas renvoyé du tout ; sans filtre d'actif, ce même morceau `BTC-USD` retombe
**dernier**, derrière les plus proches. L'étiquette ne départage pas des scores
proches : elle **passe avant** la proximité, dans les deux sens.

**Choix manuel** — la commande `/tag` corrige ou pose l'étiquette :

```
/tag BTC-USD              (en réponse au message du média)
/tag bitcoin              (mêmes alias que la détection : → BTC-USD)
/tag BTC-USD 8f14e45f-…   (référence affichée au compte-rendu)
/tag --clear              (retire l'étiquette : le média redevient le joker)
```

La réponse au message est le chemin normal — rien à copier ; la référence explicite
sert pour les médias qui n'ont jamais transité par ce chat (ceux du scraper de
canal). Un actif inconnu est refusé (`/tag pas un actif` ne fabrique pas le
symbole `PASUNACTIF`) et l'aide s'affiche sur toute erreur d'invocation.

**Ce qui est écrit, et où** — deux endroits, écrits ensemble pour ne pas diverger :

* `knowledge_chunks.asset` — **le** champ que lit le filtre SQL. Réétiqueter un
  média déjà indexé ne demande donc ni réextraction ni re-vectorisation : `asset`
  n'entre pas dans l'embedding, seul le filtre change (`set_chunks_asset`) ;
* `knowledge_media.metadata.asset` — `{"value", "source"}` (`caption`,
  `extraction` ou `manual`). Sert à l'affichage dans `/media` et à **conserver**
  l'étiquette quand l'extraction est refaite : un `↩️ Réindexer` réapplique le
  choix manuel au lieu de le perdre.

Aucune migration à jouer : `knowledge_chunks.asset` existe depuis la 009, et
l'étiquette du média vit dans `metadata` — comme le verdict de revue.

Limite assumée : un média qui parle de **deux** actifs n'en reçoit qu'un (le
premier trouvé) ; `asset` est une égalité en base, et « BTC-USD ou ETH-USD » ne
serait comparable à rien. `/tag` permet de choisir le bon, à défaut de pouvoir en
porter deux.

### Albums et déduplication

Un **album** (plusieurs photos envoyées d'un coup) n'est pas un type de message
particulier : Telegram livre chaque élément comme un message **distinct**
partageant un `media_group_id`, dans l'ordre, à quelques dizaines de millisecondes
d'intervalle — et n'annonce **jamais** combien il en envoie. La seule fin
observable est donc le **silence** : `AlbumBuffer` met les éléments de côté et
livre le lot une seconde après le dernier. Envoyer quatre graphiques donne ainsi
**une** réponse, pas quatre.

Ce qui est mis en commun est la **conversation**, pas le stockage : chaque élément
garde sa ligne média, son objet Storage, son extraction et sa déduplication. Les
réunir en une seule ligne ferait perdre la reconnaissance du même fichier renvoyé
seul plus tard, la revue élément par élément, et la reprise d'un seul élément
(`/transcribe`). Le compte-rendu (`format_album_report`) tient donc sur une ligne
par élément — type, taille, état de l'extraction, étiquette, **référence** — avec
un aperçu borné quand le texte tient sur une ligne, la pièce jointe `.txt` sinon.
La réponse part sous le **dernier** élément : c'est là que Telegram l'affiche, et
c'est donc le média que `/tag` et `/transcribe` résolvent quand on répond à ce
compte-rendu ; les autres se désignent par leur référence.

La revue d'un album se fait **ligne par ligne**, avec le clavier et le préfixe des
listes (`medl:`), pas ceux d'un compte-rendu isolé : un verdict ne doit pas
remplacer le message, sinon les boutons des éléments pas encore relus
disparaîtraient avec lui. Les numéros des boutons sont ceux des lignes, tirés des
**mêmes** entrées — un élément en échec garde donc son rang, même sans bouton.

Deux limites assumées. Un album plus lent que la fenêtre (réseau qui étale la
livraison des mises à jour) part en deux réponses : sans compte annoncé par
Telegram, il n'y a rien à attendre de plus. Et la route **canal** n'est pas
regroupée : son compte-rendu part en chat privé, à des cibles qui peuvent
différer d'un élément à l'autre, et chaque publication y garde donc sa ligne.

Le même fichier n'est **jamais stocké deux fois**. Telegram attribue à chaque
fichier un `telegram_file_id` stable, et `media_store.find_media_by_telegram_file_id()`
le cherche en base **avant** le téléchargement : si le média est déjà là,
l'ingestion s'arrête immédiatement — aucun octet téléchargé, aucun objet créé,
aucune ligne réécrite. Rejouer un message, transférer un fichier déjà connu ou
renvoyer le même PDF sous un autre nom ne dupliquent donc rien (la déduplication
porte sur l'empreinte du fichier, pas sur son nom). Le compte-rendu Telegram
l'indique par un ⏭️ « Déjà stocké » avec la référence existante.

Si la recherche de doublon est impossible (Supabase non configuré, panne), elle
n'empêche **pas** l'ingestion : on laisse passer et l'upload signalera l'erreur
réelle, plutôt que de perdre le média — la déduplication est un confort, pas un
point de rupture.

### Veille média planifiée (`workers/media_reconcile.py`)

Un écart entre le bucket et `knowledge_media` ne se voit que si quelqu'un le
regarde : ni l'application ni le bot ne s'en aperçoivent. L'auto-loop le regarde
donc à intervalle régulier (`media_store.reconcile_bucket`, **un seul** parcours
pour les deux sens), et **alerte sur Telegram** au-delà d'un seuil — le message
part dans `TELEGRAM_ADMIN_CHAT_ID`, jamais à la liste des utilisateurs.

```bash
MEDIA_RECONCILE_MINUTES="360"          # 360 par défaut, borné à 30..1440 ; « 0 » éteint la veille
MEDIA_ORPHAN_ALERT_THRESHOLD="25"      # orphelins au-delà desquels on alerte ; « 0 » = dès le premier
```

Ces deux valeurs sont **surchargeables en base** (`bot_settings`, migration 010,
comme la liste des canaux) : éteindre une veille coûteuse ne doit pas demander un
redéploiement.

| Anomalie | Déclencheur |
|---|---|
| Objets sans ligne (orphelins) | `> seuil` — un orphelin isolé est banal (upload interrompu avant l'insertion) |
| Médias dont l'objet a disparu | **dès le premier** — l'application pointe dans le vide (liens en 404) |

Trois propriétés, chacune vérifiée par un test :

- **Aucune suppression automatique.** Un orphelin est un objet sans ligne, et
  rien ne prouve qu'il est inutile : upload interrompu en cours de route, ligne
  supprimée par erreur, import en cours. Le message **nomme la commande**
  (`python scripts/reconcile_media.py --delete`) au lieu de l'exécuter : la
  suppression reste une décision humaine, prise là où elle laisse une trace.
- **La même alerte ne se répète pas à chaque cycle.** L'état vit entre deux
  passages : on parle à l'apparition, on se tait tant que c'est aussi mauvais,
  on reparle si **ça empire** (en rappelant le compte précédent), et on annonce le
  **retour à la normale** — sans quoi une alerte ne se refermerait jamais. Une
  alerte qui n'a pas pu partir (chat admin absent, Telegram en panne) n'est **pas**
  notée comme envoyée, sinon la veille se tairait sur une anomalie que personne
  n'a vue.
- **Ce qui est configuré se lit.** `/preflight` publie la période, le seuil et
  `components.media_reconciliation.admin_chat` : une veille qui tourne sans chat
  admin ne peut rien signaler, et c'est exactement ce qu'on veut voir avant de se
  demander pourquoi aucune alerte n'arrive.

### Canaux balayés, période et plafond d'extractions — des réglages, pas des constantes

La liste des canaux, la fréquence à laquelle ils sont relus et le nombre
d'extractions qu'un balayage peut lancer vivaient dans `workers/auto_loop.py` (ou
n'existaient pas) : les changer demandait de modifier le worker et de le
redéployer. Ils sont maintenant **configurables**, et validés par
`core/config_runtime.py` — la même validation pour les deux sources, parce qu'une
valeur acceptée dans l'environnement et refusée en base (ou l'inverse) serait deux
vérités pour un seul réglage.

```bash
TELEGRAM_CHANNELS="thehalalwinningteam, autre_canal"   # absent = canal par défaut du projet
TELEGRAM_SCAN_MINUTES="30"                             # 30 par défaut, borné à 5..1440
TELEGRAM_SCAN_MAX_EXTRACTIONS="10"                     # 10 par défaut, 0..100 (0 = sans plafond)
```

| Source | Quand elle s'applique |
|---|---|
| table `bot_settings` (migration 010) | une valeur **utilisable** y est présente — c'est ce qui permet de changer la liste sans redéployer |
| `TELEGRAM_CHANNELS` / `TELEGRAM_SCAN_MINUTES` / `TELEGRAM_SCAN_MAX_EXTRACTIONS` | sinon |
| défaut du projet (`thehalalwinningteam`, 30 min, 10 extractions) | quand l'environnement ne dit rien **du tout** |

Points de conception :

- **Écarter n'est pas deviner.** Ce qui ne peut pas être un canal public est
  refusé *en le disant*, jamais transformé en pseudo fantôme : un titre, un
  identifiant numérique de canal privé, une phrase. Séparateurs : virgules,
  points-virgules, retours à la ligne — **pas l'espace**, car un pseudo Telegram
  n'en contient jamais, donc `Crypto Signals` est un titre, pas deux canaux. Les
  formes qu'on colle naturellement sont acceptées : `@canal`,
  `https://t.me/canal`, l'aperçu `t.me/s/canal` et même le lien d'un message
  (`t.me/canal/42`, le canal y est explicite). Casse repliée, doublons retirés —
  un canal écrit deux fois serait balayé deux fois par cycle.
- **Vide ≠ absent.** `TELEGRAM_CHANNELS=""` éteint la collecte ; ne pas définir la
  variable garde le canal par défaut. Sans cette distinction, on ne pourrait plus
  arrêter le balayage sans toucher au code — exactement ce qu'on vient de
  supprimer.
- **Une valeur illisible ne coupe pas la collecte.** Une surcharge de base
  mal orthographiée retombe sur l'environnement **et** est signalée dans le
  journal (`⚠️ canal Telegram ignoré : '…' (pseudo public attendu)`) : arrêter de
  suivre les canaux en silence serait le pire des deux mondes.
- **La période est bornée, jamais refusée.** Bornes `5..1440` minutes
  (`TELEGRAM_SCAN_MINUTES_BOUNDS`) : en dessous, on martèle l'aperçu public
  `t.me/s/<canal>` — Telegram finit par limiter ; au-dessus d'une journée, le
  canal n'est plus vraiment suivi. Une valeur illisible retombe sur la valeur
  d'environnement (puis sur 30 min) au lieu de rendre toute la configuration
  illisible.
- **La base est un confort, pas un point de rupture.** Table absente (migration
  non appliquée), Supabase non configuré, lecture impossible : `None`, et
  l'environnement reprend. Le worker ne refuse jamais de démarrer pour un réglage.
- **Le plafond d'extractions étale le balayage au lieu de l'amputer.** Un canal
  qui publie un album expose des dizaines de photos d'un coup
  (`parse_channel_html` rend **tous** les médias des dernières publications) :
  les extraire dans le même passage consommerait le quota de vision (Gemini) ou
  de transcription (Groq) en une seule fois. `max_extractions` borne donc les
  extractions **lancées par canal et par balayage** ; le surplus est **reporté**,
  pas abandonné — l'aperçu redonne les mêmes publications au cycle suivant, et
  celles déjà indexées en sont écartées sans rien coûter (`_already_indexed`),
  donc la reprise avance toute seule. Le compte des reportées est journalisé
  (`N reportée(s) au prochain balayage`), sans quoi un balayage partiel se
  lirait comme un balayage en échec. Seules les extractions **réellement
  lancées** consomment le plafond : un téléchargement ou un stockage raté
  n'utilise aucun quota, donc il ne prend pas la place d'un autre média. Bornes
  `0..100` (`TELEGRAM_SCAN_MAX_EXTRACTIONS_BOUNDS`), où `0` veut dire **sans
  plafond** — comme `MEDIA_RECONCILE_MINUTES`, borner en silence rendrait un cran
  d'arrêt impossible à retirer.
- **Ce qui est balayé est dit.** Au démarrage et à chaque période, la ligne
  `→ balayage Telegram : @canal (toutes les 30 min, au plus 10 extraction(s) par
  canal)` est journalisée, et la configuration effective est publiée dans
  `/preflight` (`telegram_channels.channels` / `.scan_minutes` /
  `.max_extractions`, plus `.invalid` pour ce qui a été écarté) — une liste
  surchargée en base n'apparaît nulle part ailleurs.

#### Le report qui ne se résorbe pas — `workers/scan_backlog.py`

Le report est donc normal, et c'est sa **répétition** qui ne l'est pas : un canal
qui reporte à chaque balayage d'affilée a forcément l'une des deux causes — il
publie plus vite que le budget du balayage, ou une extraction échoue à chaque
passage (une extraction réussie n'est plus reproposée, donc ce qui revient sans
cesse n'a jamais abouti). Le compte est bien journalisé à chaque passage, mais une
ligne parmi des centaines se lit comme la normale.

`workers/scan_backlog.py` suit donc, **par canal**, le nombre de balayages
consécutifs qui reportent, et alerte le chat d'exploitation :

| Situation | Ce qui se passe |
|---|---|
| un ou deux reports d'affilée | silence : c'est le plafond qui travaille, et le rattrapage qui avance |
| `DEFAULT_CONSECUTIVE_SWEEPS` (3) reports d'affilée | **alerte** : le canal, le nombre de balayages, le report du dernier passage, le plus haut vu, le plafond effectif, et les commandes qui agissent (`/channels`, `/channels max`, `/transcribe`) |
| un report plus haut qu'à la dernière alerte | on reparle tout de suite (« ça s'aggrave ») |
| toujours bloqué, sans changement | un rappel tous les `REMINDER_EVERY` (10) balayages — un message unique peut se perdre dans une nuit. Cette cadence-là reste **fixe** : un canal qui reporte est vivant, on ne renonce pas à en parler |
| report retombé à zéro | **retour à la normale**, en une fois, avec la durée du blocage et son plus haut |

Trois précisions, chacune un choix :

* le seuil se compte en balayages **consécutifs** : un balayage sans report solde
  la séquence, et la suivante repart de un. C'est la consécution qui est le
  symptôme, pas le total des reports ;
* un balayage **en erreur** ne compte ni comme report ni comme résolution : c'est
  un canal dont on ne sait rien, et l'annoncer « résorbé » ferait taire l'alerte
  au moment précis où le balayage échoue. Quand l'erreur est une **lecture
  impossible**, c'est la seconde famille ci-dessous qui prend le relais ;
* un canal retiré de la liste est **oublié** : sans cela, il serait annoncé bloqué
  dès son premier report s'il revenait un jour, en s'appuyant sur des balayages
  qui n'ont pas eu lieu.

Comme la veille média, une alerte qui n'a pas pu partir n'est **pas** notée comme
envoyée (`TELEGRAM_ADMIN_CHAT_ID` absent, Telegram en panne) : le balayage suivant
réessaie, sinon la veille se tairait sur un blocage que personne n'a vu. Rien n'est
modifié automatiquement, et c'est voulu : relever le plafond allège la file
d'attente mais laisse la cause intacte si une extraction échoue en boucle. Le
message dit les deux pistes, la décision reste à l'opérateur. L'état vit en
mémoire du worker — un redémarrage repart de zéro, ce qui est le comportement sûr,
puisqu'on ne peut pas annoncer bloqué un canal qu'on n'a pas encore observé — et
le seuil comme la cadence de rappel sont des paramètres nommés du suivi, jamais
recopiés dans la boucle.

Le réglage s'écrit depuis l'éditeur SQL, ou en une ligne de Python :

```python
from database import settings

settings.set_setting(settings.SETTING_TELEGRAM_CHANNELS, ["canal_un", "canal_deux"])
settings.set_setting(settings.SETTING_TELEGRAM_CHANNELS, None)   # retour à l'environnement
settings.set_setting(settings.SETTING_TELEGRAM_SCAN_MAX_EXTRACTIONS, 5)   # plafond du balayage
settings.telegram_scan_settings()
# {"channels": [...], "rejected": [...], "scan_minutes": 30, "max_extractions": 10}
```

#### Le balayage qui n'a rien lu — le même suivi, l'autre symptôme

Un canal peut aussi échouer **sans rien dire**. Si l'aperçu `t.me/s` est
injoignable (DNS, 503, page d'un format inattendu), le scraper avale l'exception
— il ne doit pas emporter le cycle — et rend des comptes à zéro : le journal
affiche la même ligne qu'un canal calme (`0 messages, 0 média(s) stocké(s)`). Le
canal peut publier toute la journée, rien n'arrive. Le suivi du report n'y voit
rien non plus : un balayage qui n'a rien lu ne reporte rien, il n'a rien à
reporter.

C'est le même angle mort que le report, avec un symptôme inverse — là où le report
dit trop, l'échec ne dit rien du tout. `workers/scan_backlog.py` suit donc les deux
familles, avec la même mécanique et des suites **séparées** :

| Situation | Ce qui se passe |
|---|---|
| un ou deux échecs de lecture d'affilée | silence : `t.me/s` limite les requêtes, un échec isolé est un hoquet réseau |
| `DEFAULT_CONSECUTIVE_SWEEPS` (3) échecs d'affilée | **alerte** : le canal, le nombre de balayages, le motif rendu par le scraper, et la marche à suivre (`/channels`, `/refresh_data`, l'URL `https://t.me/s/<canal>` à ouvrir dans un navigateur) |
| toujours illisible, sans changement | un rappel, de plus en plus **espacé** : `REMINDER_EVERY` (10) balayages, puis le double à chaque rappel |
| `ABANDON_AFTER_REMINDERS` (3) rappels restés sans réponse | **on renonce** : un dernier message dit que l'alerte s'arrête, comment rouvrir le dossier, et qu'une lecture qui repart lèvera l'abandon — puis le silence |
| lecture qui aboutit | **retour à la normale**, en une fois, avec la durée des échecs — et « l'abandon est levé » s'il y en avait un |

Quatre précisions, sur les mêmes principes que le report :

* **les deux suites ne se comptent pas ensemble** : un échec ne mesure rien du
  report, donc il ne solde pas la séquence de reports — sans quoi une panne de
  lecture ferait passer un blocage réel pour une accalmie, au moment précis où le
  balayage ne lit plus rien ;
* **un seul message par canal et par balayage** : quand une lecture repart alors
  que l'alerte de report aurait aussi quelque chose à dire, c'est la reprise qui
  part — l'alerte de report se redira au balayage suivant si elle tient encore ;
* **le motif vient du scraper, il n'est jamais deviné** :
  `fetch_and_push_telegram_channel` rend `{"…": 0, "error": "ConnectError: …"}`
  (et `error: None` quand la lecture a abouti), `workers/auto_loop.py` le transmet
  tel quel, et le message aplatit le motif sur une ligne et le tronque — une
  exception multiligne casserait l'alerte. C'est ce motif, et lui seul, qui
  distingue un canal calme d'un canal injoignable ;
* **l'abandon ne porte que sur la lecture** : un canal qui reporte est vivant, et
  son blocage se règle encore (relever le plafond, rattraper une extraction).
  Renoncer là reviendrait à éteindre un symptôme sur lequel quelqu'un peut
  encore agir ;

Un canal abandonné n'est pas retiré de la liste : il reste balayé, et l'abandon
tombe tout seul à la première lecture qui aboutit — annoncée comme telle, parce que
le silence n'a jamais été une condamnation. Ce qui s'arrête est le bruit, pas la
veille. Comme le reste de l'état, l'abandon vit en mémoire du worker : un
redémarrage repart d'une épreuve neuve (et re-signalera un canal mort), et retirer
le canal de `/channels` l'oublie complètement. Les trois compteurs se règlent à la
construction (`ScanBacklog(threshold=…, reminder_every=…, abandon_after_reminders=…)`),
et `abandoned` nomme à tout moment les canaux sur lesquels le bot s'est tu — sans
quoi l'abandon lui-même serait silencieux.

Un même canal peut mener les deux alertes de front : il publie plus vite que son
budget **et** il est injoignable. `alerted` nomme alors les canaux signalés quelle
que soit l'alerte, et `failing` ne donne que la famille de la lecture.

### Commande `/channels` — lire et régler depuis Telegram

La même configuration se lit et se change sans éditeur SQL ni redéploiement :

```
/channels                            ce qui est balayé, et d'où vient le réglage
/channels add @canal_un, @canal_deux  ajoute, sans retirer les autres
/channels remove @canal_un            retire (les retirer tous éteint la collecte)
/channels every 30                    période, en minutes (bornes 5..1440)
/channels max 10                      extractions par canal et par balayage (0 = sans plafond)
/channels reset                       retire la surcharge, retour à l'environnement
```

Ce que la commande écrit, c'est la liste **effective** modifiée : sans surcharge en
base, ajouter un canal écrit « environnement + nouveau », jamais `["nouveau"]` seul
— sinon ajouter un canal ferait disparaître les autres sans le dire. Elle ne valide
rien elle-même : `notifications/telegram_channels.py` appelle
`core.config_runtime` (`split_telegram_channels`, `clamp_scan_minutes`) et relit
l'état par `database.settings.telegram_scan_settings`, la fonction que lit le
worker — ce qui est affiché est donc ce qui sera balayé au prochain cycle, et non
l'état qu'on a voulu écrire.

Deux points de conduite :

- **Une période hors bornes est ramenée, et le dit** (« 2 est hors bornes 5..1440,
  ramené à 5 »). Une période **illisible** est en revanche refusée : la lecture de
  configuration retombe en silence sur la valeur précédente — elle ne doit pas
  rendre toute la configuration illisible —, mais une commande répond à quelqu'un
  qui attend, et « bientot » ne peut pas y passer pour « garde la valeur actuelle ».
  Le plafond d'extractions suit les mêmes règles (`/channels max 10`), à une
  différence près : `0` y est **conservé** tel quel, parce qu'il veut dire « sans
  plafond » et non « aucune extraction » — l'afficher « 0 extraction(s) » ferait
  croire à une collecte éteinte alors qu'elle tourne sans limite.
- **L'écriture est réservée au chat `TELEGRAM_ADMIN_CHAT_ID`**, et fail-closed :
  sans ce chat configuré, la commande refuse d'écrire et dit quoi renseigner. Le
  réglage est **global** — celui du worker, pas d'un chat —, donc n'importe quel
  chat ne peut pas rediriger la collecte. Un échec d'écriture est annoncé comme
  tel (« réglage NON appliqué »), jamais présenté comme une réussite.

### Médias d'un canal public (`scrapers/telegram_channel.py`)

Les canaux publics configurés ci-dessus (`TELEGRAM_CHANNELS`, surchargeables en
base) sont lus via l'aperçu web `t.me/s/<canal>` — aucun compte, aucune
authentification.
Le scraper alimente trois choses : le **texte** des publications (table
`insights`), les **médias** qu'il télécharge puis stocke via
`database.media_store` (bucket privé `telegram-media` + `knowledge_media`), avec
la source `telegram_channel` (distincte de `telegram`, réservée aux médias reçus
par le bot) et une clé d'objet déterministe `<canal>/<message>` — rejouer un
balayage réécrit le même objet au lieu d'accumuler des copies —, et le **texte de
ces médias**, extrait puis indexé dans `knowledge_chunks`.

Cette dernière étape est ce qui rend une photo de canal utile au-delà du fichier :
sans elle, un graphique publié par un canal resterait un objet dans un bucket,
absent de `/search` comme du contexte média du moteur de décision. Le parcours est
**exactement** celui du bot (`ai/media_indexing`, partagé par les deux routes) :
vision **Gemini** pour les photos, Whisper pour les vidéos, `pypdf` pour un PDF,
la **légende** (le texte de la publication) indexée en premier et **même si
l'extraction échoue**, et l'actif détecté dans cette légende étiqueté sur les
morceaux — donc les mêmes règles, la même étiquette, les mêmes morceaux.

Un balayage est répétable sans coût : un message dont le média est **déjà
indexé** est reconnu (`media_store.find_media_by_message` +
`knowledge_index.has_chunks`) et ignoré **avant** le téléchargement. Sans cette
garde, chaque passage de l'auto-loop relancerait un appel de vision par photo —
même quota, même texte — pour rien. Les médias enregistrés *avant* cette étape
n'ont aucun morceau : ils sont donc réindexés au balayage suivant, au lieu de
rester invisibles pour toujours.

Ce que l'aperçu web expose réellement, vérifié sur des canaux publics :

| Média | Source dans l'aperçu | Téléchargeable ? |
|---|---|---|
| Photo | URL du fichier dans le `style` de `a.tgme_widget_message_photo_wrap` | ✅ oui |
| Vidéo | `src` de `video.tgme_widget_message_video` | ✅ oui |
| Document | le bloc `…_document_wrap` ne porte qu'un lien `t.me/<canal>/<id>` | ❌ **non** |

Un document ne peut donc pas être récupéré par cette voie : l'aperçu n'en donne
que le nom et la taille. Le scraper les **compte et les ignore** (`skipped_documents`,
journalisé « N document(s) ignoré(s) ») plutôt que de laisser croire à une
ingestion. Pour récupérer le fichier lui-même, il faut le bot Telegram
administrateur du canal : c'est la route `channel_post` ci-dessous.

### Route `channel_post` — le fichier d'origine d'un canal où le bot est admin

Un canal où le bot est **administrateur** lui livre ses publications
(`channel_post`) : le bot reçoit le message et télécharge le fichier par l'API,
**documents compris**. C'est exactement le trou de la route précédente — l'aperçu
public ne donne jamais le fichier d'un document — et les deux routes coexistent
sans se recouvrir : celle-ci **complète** le scraper sans le remplacer. Chacune a
son domaine, et elles partagent tout le reste : le pipeline d'indexation
(`ai/media_indexing`), la clé de stockage `<canal>/<message>`, et la
déduplication par `(canal, message)` qui empêche le même message d'être ingéré
par les deux. Le scraper n'a **jamais** de compte-rendu à porter un clavier ✅/❌ :
ses médias ont en revanche une ligne dans `/media`, d'où la revue se fait aussi
(voir « Commande `/media` »).

Trois choses différentes de la route « chat », et chacune se voit dans le code :

| Sujet | Route chat (privé/groupe) | Route canal (`handle_channel_post`) |
|---|---|---|
| Réponse | dans le chat, avec les boutons ✅/❌ | **aucune** : `reply_text` sur un `channel_post` se publierait devant tous les abonnés |
| Compte-rendu | dans le chat d'origine | en **chat privé** (`channel_report_targets`) : l'admin qui a publié, sinon `TELEGRAM_ADMIN_CHAT_ID` |
| Clé de stockage | l'id du chat | le **nom du canal** (`telegram/<canal>/<message>-<fichier>`), comme le scraper public |

Le compte-rendu dit d'où vient le fichier (canal, message, auteur) et rappelle
que rien n'a été publié dans le canal : hors de la publication, « ✅ Média
enregistré » ne veut rien dire. Les cibles sont essayées dans l'ordre — Telegram
refuse d'écrire à quelqu'un qui n'a jamais ouvert la conversation avec le bot —,
et si aucune n'aboutit, le journal le dit (l'ingestion a eu lieu, c'est la
**revue** qui est reportée). Reportée, pas perdue : la publication a une ligne
dans `/media`, où les boutons de revue l'attendent.

**Pas de doublon entre les deux routes.** Le scraper public n'a pas de
`telegram_file_id` : la déduplication habituelle ne peut donc pas le reconnaître,
et la même photo serait stockée deux fois. La route canal ajoute donc un second
test, `(canal, message)` — `media_store.find_media_by_message`, servi par l'index
de la migration 008 —, fait **avant** le téléchargement. Comme les deux routes
écrivent aussi la même clé d'objet, un message déjà pris par l'aperçu public
n'est ni retéléchargé, ni réécrit, et le compte-rendu affiche ⏭️ « Message déjà
ingéré depuis ce canal » plutôt que de parler d'un doublon de fichier (ce n'est
pas le même cas). Un canal **privé** n'a pas d'équivalent public : rien à
comparer, et on ne consulte pas la base pour rien.

Côté configuration, deux points :

* `allowed_updates` doit demander `"channel_post"` (`main.py`) — sans lui,
  Telegram ne livre aucune publication de canal, et la route resterait morte même
  correctement câblée ;
* `TELEGRAM_ADMIN_CHAT_ID` (`.env`) est le chat privé de secours pour les
  publications **anonymes** (« en tant que canal », le cas par défaut) : celles-ci
  n'ont pas d'auteur à qui répondre.

### Recherche vectorielle de la base de connaissances

Le contexte de connaissances injecté dans les prompts du moteur de décision n'est
plus un **extrait fixe** : `database/knowledge_base.py` interroge
`knowledge_chunks` par similarité cosinus (`match_knowledge_chunks`, top-k),
filtrée par **actif** et **régime de marché** (`core/market_regime.py`, mêmes
seuils que l'app Android). Les morceaux non étiquetés restent candidats — le
filtre est préférentiel, pas exclusif — et les correspondances explicites
remontent en tête. Sans `GEMINI_API_KEY` (ou sans vecteurs en base), on retombe
sur l'ancien extrait des notes les plus récentes.

### Médias indexés dans le contexte du moteur de décision

Le corpus des médias n'est pas seulement ce que tu envoies au bot : les trois
routes d'ingestion y versent leurs extraits — média envoyé au bot, publication
d'un canal où il est administrateur, et **photos et vidéos des canaux publics**
(leur texte est extrait par vision/transcription puis indexé, voir « Médias d'un
canal public »). Ce que produit une photo de canal compte donc doublement :
`/search` la retrouve, et son extrait peut entrer dans le prompt.

Ces lectures de graphiques et transcriptions ne servent pas qu'à être
retrouvées à la demande : leur extrait le plus proche de l'actif remonte aussi
dans le **prompt** du moteur de décision, via `get_media_context(asset, regime)`
(composé dans `_knowledge_block`, `ai/news_analyzer.py`). Le bloc est injecté
dans une section **distincte** des règles, avec son propre en-tête :

```
Connaissances de référence (… règles/principes permanents, PAS des actualités) :
…
Médias indexés pertinents pour BTC-USD (… OBSERVATIONS DATÉES, PAS des règles
permanentes ni des actualités ; ne les généralise pas) :
### [MÉDIA · lecture de graphique · btc.png] cassure des 100k …
```

La séparation est volontaire : un graphique ponctuel n'a pas le statut d'une
règle de trading et ne doit pas être généralisé. Trois garde-fous bornent ce bloc
**par actif** — un plafond propre (`MAX_MEDIA_CONTEXT_CHARS = 900`, **en plus**
des 1200 caractères des règles), un nombre de médias limité (`DEFAULT_MEDIA_TOP_K
= 3`) et le **seuil de similarité** mesuré sur la base (voir ci-dessous) : sous ce
seuil, on préfère n'injecter aucun extrait plutôt qu'un extrait hors sujet qui
ferait dériver l'analyse. Ce seuil s'applique aux extraits que la similarité a fait
remonter ; un média **étiqueté** pour l'actif n'en dépend pas (voir « Étiquetage
d'actif »). Contrairement aux notes, **aucun repli** : sans actif connu, le bloc
média est simplement vide.

#### Le seuil de similarité est mesuré sur la base, pas choisi

Un extrait de média n'entre dans le prompt que s'il dépasse un seuil de
similarité — et ce seuil était la constante `0.5`. Or une similarité cosinus **n'a
pas d'échelle absolue** : deux documents sans rapport d'un corpus homogène (des
dizaines de graphiques et de transcriptions traitant tous de trading, avec le même
vocabulaire, le même modèle et la même troncature 768) atteignent couramment 0,6,
tandis qu'un corpus vaste et hétérogène descend à 0,3. La constante se trompait
donc dans les deux sens : elle laissait passer du bruit sur le premier et jetait
des correspondances utiles sur le second.

`knowledge_index.calibration()` mesure ce que **cette** base répond à des questions
qui ne la concernent pas du tout (`NOISE_PROBE_QUERIES` : une recette, une
randonnée, un match, une liste de courses), contre un échantillon de ses documents
— un morceau **par média**, sinon les morceaux d'un même document (qui se
chevauchent) mesureraient la redondance interne plutôt que le bruit. Le seuil est
le **95ᵉ percentile de ce bruit, plus 5 points** : une correspondance doit dépasser
presque toutes les paires sans rapport de la base, et pas seulement la moyenne (une
seule paire qui se ressemble par hasard ne doit pas fixer le seuil).

```
   [similarite] seuil calibre sur la base : 0.718 (bruit p95 0.668 + marge 0.05,
                42 documents / 168 paires)
```

| Choix | Pourquoi |
|---|---|
| Sondes en tâche `RETRIEVAL_QUERY` | les vraies recherches le sont ; une mesure document-à-document ne serait pas dans la même échelle |
| Percentile **et** marge | le percentile écarte le hasard, la marge écarte le « tout juste au-dessus » |
| Bornes `0,2 – 0,9` | en dessous on injecte du bruit ; au-dessus plus rien ne passe jamais |
| Repli `MIN_MEDIA_SIMILARITY = 0.5` | base non configurée, aucun vecteur, trop peu de documents (< `CALIBRATION_MIN_DOCS`) ou embeddings indisponibles |
| Cache de `CALIBRATION_TTL_SECONDS` (15 min) | la mesure coûte un appel d'embeddings par sonde ; la base change à chaque ingestion |

Un échec de mesure est **mémorisé comme un succès** (le coût est le même) et le
repli est appliqué : le seuil est une amélioration, jamais une dépendance — le
moteur de décision continue de tourner sans vectorisation du tout. Une base très
homogène peut légitimement donner un seuil si haut qu'**aucun** média n'est
injecté : c'est le comportement voulu (un extrait hors sujet fait dériver
l'analyse), et le journal dit lequel des deux seuils a servi.

Pour voir la distribution mesurée et l'effet d'un autre percentile :

```bash
python scripts/calibrate_similarity.py                  # la mesure de ta base
python scripts/calibrate_similarity.py --docs 80 --percentile 99
python scripts/calibrate_similarity.py --json           # comparaison machine
```

### Le flux watchlist (prompt « batch »)

L'auto-loop appelle `warm_batch()` : toute la watchlist est analysée en **un seul
appel LLM** par modèle, résultats mis en cache 1 h. Ce prompt contenait les règles
mais pas les médias — or `NewsAnalysisCache.get()` renvoie ensuite l'entrée du
cache, si bien que le chemin « par actif » (le seul qui appelait
`get_media_context`) **n'était jamais exécuté pour la watchlist**. Les
transcriptions et documents ne remontaient donc que pour un actif hors watchlist.

Le prompt batch porte maintenant un bloc « Médias indexés par actif », chaque
actif recevant une **part du budget global** :

| Réglage | Valeur | Rôle |
|---|---|---|
| `MAX_BATCH_MEDIA_CHARS` | 2400 | plafond total du bloc média du batch (~600 tokens) |
| `BATCH_MEDIA_PER_ASSET_CHARS` | 400 | part maximale d'un actif |

La part réelle vaut `min(BATCH_MEDIA_PER_ASSET_CHARS, MAX_BATCH_MEDIA_CHARS / nombre
d'actifs − en-tête − MEDIA_TRUNCATION_SLACK)` : l'en-tête `### <actif>` de chaque
bloc et la marque de troncature sont **comptés dans la part**. Les ignorer faisait
dépasser la somme des parts du budget total dès six actifs, et la sécurité de fin de
boucle écartait alors le dernier — exactement l'injustice que la répartition par
actif existe pour éviter. La somme des parts tient donc dans le plafond **par
construction**, et la sécurité ne sert plus qu'en cas de bloc plus long que demandé.
L'en-tête du bloc (« Médias indexés par actif … ») est hors budget : il ne dépend pas
du nombre d'actifs, le compter dedans réduirait la part de chacun d'un montant
arbitraire.

Le coût en appels LLM reste celui d'un batch unique, le prompt reste **borné**
quelle que soit la taille de la watchlist, et les derniers actifs ne sont pas
sacrifiés au profit des premiers. Un actif sans extrait assez proche n'occupe
aucune place — elle est laissée aux suivants.

### Commande `/search` — recherche sémantique dans le chat

```
/search niveaux de support bitcoin
/search cassure des 100k --source medias -n 8
/search gestion du risque --asset BTC-USD --regime BULL
/search_more
/search --help
```

La commande vectorise la question (embeddings **Gemini**, exactement comme
l'indexation) et interroge `knowledge_chunks` par similarité cosinus
(`match_knowledge_chunks`) : « support du bitcoin » retrouve un passage qui parle
de « zone d'achat sous 64k » sans partager un seul mot. Chaque passage remonte
avec son **score de similarité** et un aperçu tronqué :

```
🔎 passages 1 à 8 sur 20 pour « niveaux de support bitcoin » [médias · BTC-USD · 8 par page] :

1. [note · BTC-USD] — similarité 0.873
zone d'achat sous 64k, RSI survendu…

2. [média] — similarité 0.741
cassure des 100k confirmée en clôture…

↪️ 12 passage(s) de plus dans ce lot — /search_more
```

**Options** (parsées par `knowledge_base.parse_search_args`, la commande reste
un simple handler) :

| Option | Rôle |
|---|---|
| `--asset <actif>` (`-a`) | ne garder que cet actif ; normalisé en majuscules |
| `--source <où>` (`-s`) | `notes`, `medias`, `tout` (défaut) — les synonymes sont acceptés parce que la source d'un média s'appelle `telegram` en base |
| `--regime <nom>` (`-r`) | ne remonter qu'un régime de marché |
| `-n, --limit <N>` | passages par page, 1 à `MAX_SEARCH_TOP_K` (20) |
| `--help` | aide complète ; `--` met fin aux options |

Sans requête comme avec `--help`, la commande **affiche son aide** : les options
ne s'apprennent pas en lisant le code. Les filtres actifs sont rappelés en tête de
réponse (`[médias · BTC-USD · max 8]`) et, quand rien ne remonte, la piste
d'élargissement nomme **le** filtre concerné (« Essaie `--source tout` ») plutôt
qu'un conseil générique.

Deux pièges sont refusés explicitement au lieu de produire une recherche
silencieusement fausse : `--asset BTC-USD,ETH-USD` (le filtre SQL est une égalité
stricte, une liste ne trouverait rien) et une option sans valeur
(`--asset --source notes` ne doit pas passer `--source` comme actif).

`--asset` n'est pas qu'un tri : la fonction SQL **exclut** les morceaux étiquetés
d'un autre actif, tout en gardant candidats les morceaux non étiquetés (NULL =
joker). C'est pourquoi les médias reçus par le bot sont étiquetés à l'ingestion
(voir « Étiquetage d'actif ») : sans étiquette, un graphique Ethereum reste
candidat pour une analyse BTC-USD, et n'a aucun bonus de classement.

#### `/search_more` — la suite du même lot

`/search` interroge la base **une seule fois** (`top_k=SEARCH_POOL_SIZE`, soit
`MAX_SEARCH_TOP_K = 20` passages) et en affiche une page : le reste est confié à
`core/search_history.py`, et `/search_more` le déroule. C'est ce qui distingue
« voir la suite d'une recherche » de « relancer une recherche » : un second appel
coûterait un embedding, et pourrait **reclasser** les passages — donc paginer sur
un classement instable.

La numérotation continue d'une page à l'autre (`6.`, `7.`…) et le pied de page
annonce ce qu'il reste. Les deux échecs sont distincts, parce qu'ils n'ont pas le
même remède :

```
↪️ Aucune recherche récente à continuer. Lance d'abord `/search <texte>` …
↪️ Tous les passages du lot ont été affichés pour « cassure des 100k » (20 sur 20).
Le lot interrogé est borné à 20 passages : relance `/search` avec d'autres mots, ou élargis les filtres.
```

L'historique est **en mémoire**, volontairement minuscule : **une** entrée par
utilisateur (une nouvelle recherche remplace la précédente — garder davantage
d'entrées sans interface pour les choisir ne serait pas un historique, seulement
de la mémoire occupée) et au plus `MAX_USERS = 50` utilisateurs en LRU, soit
~1,5 Mo au pire. Les passages sont gardés entiers, contenu compris : c'est le prix
à payer pour ne rien redemander au réseau. Rien n'est persisté — c'est l'état
d'une conversation, pas une donnée.

Un argument sur `/search_more` (`/search_more 3`) est **annoncé comme ignoré**
plutôt qu'absorbé en silence : la taille de page est celle de la recherche
d'origine.

#### `/use` — boucle RAG : des passages proposés, puis injectés dans une analyse

Le cycle automatique construit son contexte tout seul. Cette boucle-ci fait
l'inverse : c'est l'utilisateur qui **choisit** ce que le modèle lira, et
l'analyse part **tout de suite**, sans attendre le prochain cycle.

```
/search cassure des 100k            # 1. on interroge la base (lot de 20, paginé)
🎯 Analyser BTC-USD                 #    bouton sous les résultats (ou /use BTC-USD)
/use BTC-USD 1,3                    #    sélection explicite : rangs 1 et 3
✅ LANCER L'ANALYSE                 # 2. validation humaine
🧩 Contexte injecté : 2 extrait(s)… # 3. analyse immédiate, extraits dans le prompt
→ 🚨 PROPOSITION IA               # 4. le signal repasse par la validation habituelle
```

| Étape | Détail |
|---|---|
| `/use <ACTIF> [rangs]` | les rangs sont **ceux affichés** par `/search` et `/search_more` (numérotation continue du lot) ; `1,3`, `2-4`, `1 3` acceptés ; sans rang, les 3 premiers passages |
| proposition | elle **montre les extraits** qui seront lus (même contenu, mêmes étiquettes que la recherche), leur nombre, le nombre de caractères injectés, et l'actif concerné |
| validation | bouton ✅ (ou ❌ pour annuler) ; `core/rag_loop.py` **consomme** la proposition, donc un double clic ne lance pas deux analyses — ni deux propositions de trade |
| injection | `ai/news_analyzer` reçoit un troisième bloc de contexte, étiqueté « Extraits SÉLECTIONNÉS ET VALIDÉS PAR L'UTILISATEUR … ne les généralise pas » : le modèle doit savoir qu'un extrait ponctuel n'est pas une règle générale |
| portée | il pèse sur la composante **sentiment/géo** (l'analyse LLM) ; la technique reste calculée sur les prix — des notes ne sont pas des indicateurs |
| trace | le signal persisté porte « Contexte RAG validé (extraits sélectionnés par l'utilisateur) » : rien ne distingue plus tard cette analyse du cycle auto si on ne l'écrit pas |

Trois bornes, parce qu'un contexte injecté est un coût (tokens, quota) et un risque
(dérive) : `MAX_CONTEXT_CHARS = 1500` pour le bloc, `EXCERPT_CHARS = 500` par
extrait, et un plafond en dernier ressort côté prompt
(`news_analyzer.MAX_RAG_CONTEXT_CHARS`). Le plafond rogne des extraits **entiers**
— jamais un extrait à moitié — et quand il rogne, la proposition le **dit**
(`✂️ 2 extrait(s) sur 20 demandés`) : annoncer 20 extraits pour n'en lire que 2
ferait valider une analyse sur un contexte qui n'est pas celui annoncé.

**Le cache d'analyse est contourné dans les deux sens** quand des extraits sont
fournis : servir une réponse mise en cache sans eux validerait une analyse qui ne
les contient pas, et stocker la réponse d'un utilisateur la servirait ensuite au
cycle automatique — dont le contexte est précisément celui que la recherche
vectorielle a retenu, pas ce choix-là. La conséquence assumée : une analyse RAG
coûte toujours un appel LLM, et ne dégrade jamais le contexte du cycle auto.

Enfin, un rang hors du lot est refusé avec la plage réelle (`la dernière recherche
a 12 passage(s), donc les rangs valides vont de 1 à 12`) — raboter en silence
partirait avec moins d'extraits que ce que l'utilisateur croit avoir validé. La
proposition en attente vit **en mémoire**, comme l'historique de `/search`
(`MAX_USERS = 50`, une par utilisateur) : c'est l'état d'une conversation.

Contrairement à un `LIKE`, aucun mot-clé exact n'est requis. Trois cas sont
distingués explicitement pour ne pas mentir à l'utilisateur : requête vide
(aide), **recherche vectorielle indisponible** (`GEMINI_API_KEY` absente — message
dédié, pas un « rien trouvé » trompeur, via
`knowledge_index.embeddings_available()`), et « aucun passage ». Le nombre de
résultats est borné par `DEFAULT_SEARCH_TOP_K = 5` (`MAX_SEARCH_TOP_K = 20`) pour
rester sous la limite de 4096 caractères d'un message Telegram.

### Commande `/media` — lister les médias stockés

```
/media
```

Affiche les derniers médias enregistrés (type, nom, taille, date) et, pour
chacun, un **lien signé temporaire** — le bucket est privé, il n'existe donc pas
d'URL publique : chaque fichier est exposé par un lien qui expire au bout d'une
heure (`SIGNED_URL_TTL`). La liste est bornée à `DEFAULT_MEDIA_PAGE = 10`
(`MAX_MEDIA_PAGE = 25`), sinon un lien par média ferait dépasser la limite de
4096 caractères d'un message Telegram. Un échec de signature pour un média est
signalé sur **sa** ligne sans priver les autres du leur, et une lecture en base
impossible devient un message d'erreur — jamais une exception.

La liste se **parcourt** : la dernière rangée du clavier porte « ◀️ Précédents » /
« ▶️ Suivants », comme `/pending` et `/transcribe`, et **aucun état n'est mémorisé**
entre deux clics. Le bouton porte le **rang** de la première ligne de la page
(`medlg:<rang>`, `parse_media_page`) — un rang, pas un identifiant de média : une
page se recalcule (voir `media_list_view`), il ne désigne aucune ligne. `main.py`
relit donc la vue à ce rang (`media_list_view(offset=…)`), ce qui est le **seul
geste de `/media` qui ne rend aucun verdict** : il ne touche ni la base ni l'index.
L'honnêteté du rang repose sur l'ordre **total** de `list_media` (`created_at` puis
`id`) : deux médias d'un même album partagent leur date à la seconde, et un tri non
unique exposerait une ligne sur deux pages en en cachant une autre — le sujet est
dit dans la docstring de la lecture, c'est elle qui le tient.

Deux différences avec `/pending`, toutes deux nées de la lecture **bornée** de
`/media` :

* la suite est **constatée**, pas calculée — la vue demande une ligne de plus que
  sa page, et cette ligne (`more`) décide si « ▶️ Suivants » mène quelque part. La
  table n'étant pas balayée, le message ne peut pas annoncer un reste chiffré : il
  dit « … d'autres suivent » là où `/pending` peut compter son solde ;
* un rang devenu hors bornes — la liste a changé depuis l'affichage — n'affiche pas
  « aucun média » (faux : la table n'a pas été vidée) mais **le dit**, et garde le
  bouton qui remonte. `_pending_start` peut, lui, calculer la dernière page réelle :
  il connaît le total, `/media` ne le connaît pas.

Un verdict rendu depuis une page laisse revenir à la **première** page, comme dans
`/pending` : la charge utile d'un bouton de ligne (`medl:<verdict>:<uuid>`) porte le
média, pas la page, et un rang glissé après l'identifiant serait refusé par
`parse_review_callback` plutôt que deviné — la page, elle, se retrouve d'un clic sur
« ▶️ Suivants ».

La navigation elle-même est un préfixe à part (`medlg:`), distinct de `^medl:` — qui
exige un deux-points juste après `medl` — pour la même raison que `medpg:` face à
`^medp:` : un clic sur une **ligne** juge un média, celui-ci ne fait que tourner la
page.

La ligne d'un média déjà relu porte son **verdict de revue**
(`✅ extraction validée` / `❌ extraction rejetée (non indexée)`) : c'est le seul
endroit où l'on peut voir après coup ce qui a été écarté — un média rejeté a zéro
morceau indexé, il serait sinon indiscernable d'une extraction simplement échouée
(voir « Revue d'une extraction »).

Elle porte aussi `⚠️ texte non extrait` quand la dernière extraction n'a **rien**
lu : sans cette marque, un média jamais lu serait identique à un média lu
dès qu'il porte une légende — la légende est indexée dans les deux cas. C'est la
seule différence visible entre les deux, et c'est elle qui dit qu'il reste
quelque chose à rattraper (voir « Commande `/transcribe` »).

Elle porte aussi son **actif** quand il est étiqueté (`🏷️ BTC-USD`) : un média
« neutre » n'existe pas dans la recherche filtrée — sans étiquette il est candidat
partout, avec étiquette il est exclu des autres actifs (voir « Étiquetage
d'actif »).

Elle dit enfin **d'où vient** le média, quand ce n'est pas un chat privé :
`📡 @canal · msg 42` pour une publication de canal reçue par le bot,
`📡 @canal · aperçu web` pour une image ramassée par le scraper public. Les deux
ne se valent pas — le bot a le **fichier d'origine**, le scraper n'a qu'une URL
d'aperçu — et sans cette mention on relirait le second comme s'il était complet.

**La revue se fait depuis la liste.** Chaque ligne numérotée a ses boutons, dans
une rangée de boutons par ligne de texte :

| Bouton | Quand | Ce qu'il fait |
|---|---|---|
| `✅ 3` | la ligne a du texte indexé et pas encore de verdict | valide l'extraction (verdict enregistré) |
| `❌ 3` | la ligne a du texte indexé et n'est pas rejetée | retire ses morceaux de l'index |
| `↩️ 3` | rien d'indexé, ou extraction déjà rejetée | refait l'extraction depuis le fichier stocké |

Ces boutons sont exactement `review_media` / `follow_up_buttons`, vus depuis
l'état de la ligne (`telegram_media.list_buttons()`, testé cas par cas) : une
ligne sans texte indexé n'a rien à valider, et une ligne déjà validée garde de
quoi défaire un clic de trop.

Un clic ne **remplace pas** la liste (sinon les lignes pas encore relues
perdraient leurs boutons) : il répond par une notification courte —
`telegram_media.review_toast()`, *ce qui vient de changer* — puis la liste est
réaffichée, où la ligne modifiée porte son nouveau verdict. Une réindexation
réussie envoie son `.txt` comme depuis un compte-rendu. Si la relecture de la
liste échoue à ce moment-là, le compte-rendu du verdict (`format_review_report`)
prend sa place : mieux vaut dire ce qui a bougé qu'afficher un état périmé.

`media_list_view()` est la seule fonction qui interroge la base : elle rend
`{"text", "keyboard", "entries"}`. `media_list_report()` reste disponible pour le
texte seul. Une lecture en base impossible rend un message d'erreur **et un
clavier vide** — des boutons pointant sur des lignes qu'on n'a pas pu lire
seraient pires que pas de boutons.

### Commande `/transcribe` — rattraper une extraction qui n'a pas eu lieu

```
/transcribe                     (liste des médias sans texte extrait, et ce qui manque)
/transcribe                     (en réponse au message du média : rien à copier)
/transcribe 8f14e45f-…          (par référence, telle qu'affichée par /media)
```

Une ingestion faite **sans la clef qu'il fallait** — `GEMINI_API_KEY` pour une
image, `GROQ_API_KEY` pour un vocal ou une vidéo — stocke bien le fichier, mais
sans son texte. Or renvoyer le média sur Telegram ne réindexe rien : il est
reconnu comme déjà stocké (`telegram_file_id`), donc écarté avant le
téléchargement. La commande relit le fichier **dans le bucket**, réextrait et
réindexe — même travail que `↩️ Réindexer`, mais sans avoir à retrouver le média
dans `/media` ni à retrouver le message d'origine.

**Elle vérifie ses ingrédients avant de commencer.** `ai.media_extractor.extraction_readiness()`
classe le média et dit ce qui manque *maintenant* : `GEMINI_API_KEY` (image),
`pypdf` (PDF), `GROQ_API_KEY` **ou** le repli local `faster-whisper` (audio,
vidéo). Si rien ne peut aboutir, la commande refuse **sans relire l'objet** —
relire vingt minutes de vidéo pour retrouver l'échec déjà inscrit n'apprend rien
— et nomme ce qu'il faut mettre en place. Ce qu'elle ne prétend pas savoir : les
poids du modèle local peuvent manquer paquet installé, un PDF peut être scanné,
un quota peut être épuisé ; ces échecs-là ne se découvrent qu'en essayant, et
c'est pourquoi le refus ne porte que sur des manques **prouvables**.

Sans argument et sans message en réponse, la question n'est plus « comment » mais
« lesquels » : la commande liste les médias dont la dernière extraction a échoué,
avec `⚠️` et l'ingrédient manquant (ou `🕒 repli local` quand seule la
transcription lente est possible), le motif du dernier échec, et la référence à
passer. La sélection vient du **résultat d'extraction noté sur la ligne média**
(`metadata.extraction`), pas de l'index : un média dont l'extraction a échoué a
des morceaux dès qu'il porte une légende, donc « il y a des morceaux » ne répond
pas à la question. Le parcours, lui, est **complet** : la table est balayée en
entier (`media_store.list_all_media`, la même lecture que `/pending`), parce que
borner la lecture aux derniers médias rendait un rattrapage ancien invisible — la
liste répondait « rien à rattraper » alors qu'il en restait, plus loin. Une
réserve assumée : un média d'un type que l'extraction ne sait pas lire n'y figure
jamais — le proposer promettrait une réparation que rien ne peut faire (la
commande le refuse toujours, en nommant le type).

Trop long pour tenir dans un message (chaque entrée occupe trois lignes), le
résultat est **paginé** : `TRANSCRIBE_PAGE = 10` entrées par page, les boutons
`◀️ Précédents` / `▶️ Suivants` sur une seule rangée, et le rang des lignes
(« 11–20 ») pour situer la page dans l'ensemble. Le bouton ne porte qu'un **rang**
(`medt:<rang>`) et la page est **recalculée** à chaque clic : entre deux clics, une
extraction peut avoir été relancée, et une page figée enverrait sur du travail
déjà fait. C'est un troisième préfixe de clavier — distinct de `medl:` et `medp:`
pour la même raison qu'eux : naviguer ne rend aucun verdict et ne change rien en
base.

Ce qu'une reprise **écrit** : le texte réindexé, l'étiquette d'actif reposée (un
choix manuel ne doit pas être perdu), le nouveau verdict (`validated`, comme
`↩️ Réindexer` : reprendre une extraction *est* l'accepter), et le résultat de
l'extraction à son tour noté sur la ligne. Le clavier est celui d'une extraction
**neuve** (✅ / ❌) : elle peut encore être rejetée. Chaque clé de
`metadata` reste distincte — l'outcome est ce que la **machine** a produit, le
verdict est ce qu'un **humain** en a dit.

Le travail lui-même vit dans `telegram_media.reprocess_media()`, appelé par le
bouton `↩️ Réindexer` **et** par `retranscribe_media()` : refaire une extraction
existe en un seul endroit, et un test compare les deux chemins appel par appel
pour que la commande et le bouton ne se mettent pas à diverger (type, légende,
étiquette, verdict).

```
/transcribe
🎙️ 24 média(s) à rattraper — 1–10 :

1. ⚠️ GEMINI_API_KEY · photo · chart.jpg · 812.0 Ko · 2026-09-27 · 🏷️ BTC-USD · ⚠️ texte non extrait
   Dernier échec : GEMINI_API_KEY absente : vision indisponible
   Référence : 8f14e45f-…
2. 🕒 repli local · voice · voice_42.ogg · 44.1 Ko · 2026-09-26 · ⚠️ texte non extrait
   Dernier échec : GROQ_API_KEY absente : transcription indisponible
   Référence : 1c9d7c2a-…

⏭️ 14 autre(s) : bouton « ▶️ Suivants ».

Pour relancer : `/transcribe <référence>` — ou `/transcribe` en réponse au message du média.
[◀️ Précédents] [▶️ Suivants]
```

Et le refus, quand l'ingrédient manque encore :

```
⏸️ Rien n'a été tenté : cette extraction ne peut pas aboutir en l'état.
• Média : voice · v.ogg · taille inconnue
• Il manque : GROQ_API_KEY, faster-whisper
• Ni clef Groq, ni repli local : renseigne GROQ_API_KEY ou installe `faster-whisper` (voir README).
• Le fichier est toujours dans le stockage : une fois la clef (ou le paquet) en place, `/transcribe <référence>` la refait.
```


### Commande `/pending` — la revue en attente

```
/pending
```

Liste **uniquement** les extractions qui n'ont pas encore de verdict — celles qui
attendent une relecture — avec leur canal d'origine (`📡 @canal · msg 42`) et leurs
boutons. L'âge n'est **pas** un filtre, et la lecture ne se limite pas à la page de
`/media` : `/media` borne sa requête aux derniers médias
(`DEFAULT_MEDIA_PAGE = 10`), donc une extraction ancienne jamais relue en disparaît
dès qu'il y a eu dix ingestions depuis. `/pending`, lui, parcourt la table **en
entier** (`media_store.list_pending_review`, pagination de `_read_media`) et garde
les lignes dont `media_store.review_status()` est `None`.

Le filtre est **la même fonction** que celle qui affiche le verdict dans `/media`,
et non un filtre JSONB côté Postgres : un verdict présent mais illisible (clé
absente, statut que `REVIEW_STATUSES` ne connaît pas) doit retomber dans « sans
verdict » des deux côtés. Deux prédicats différents finiraient par se contredire —
une ligne affichée sans verdict mais absente de la liste serait introuvable.

La liste est **paginée à l'affichage** (`PENDING_PAGE = 20`,
`MAX_PENDING_PAGE = 50`) parce qu'un message Telegram en porte 4096 caractères,
mais le reste n'est pas perdu : le compte total est annoncé, et comme la vue est
relue après chaque clic, une ligne qui reçoit son verdict **disparaît** et la
suivante — même plus ancienne — remonte à sa place. C'est la revue qui avance,
ligne par ligne.

Cette pagination se **parcourt** : la dernière rangée du clavier porte « ◀️
Précédents » / « ▶️ Suivants », et **aucun état n'est mémorisé** entre deux clics.
Le bouton porte le **rang** de la première ligne de la page (`medpg:<rang>`,
`parse_pending_page`) — un rang, pas un identifiant de média : une page se
recalcule, elle ne désigne aucune ligne. `main.py` relit donc la vue à ce rang
(`pending_review_view(offset=...)`), ce qui est le **seul geste de `/pending` qui
ne rend aucun verdict** : il ne touche ni la base ni l'index, il permet de
descendre jusqu'à la plus ancienne extraction sans verdict sans en traiter une
seule. Un rang devenu hors bornes — un verdict rendu ailleurs a raccourci la file
entre deux clics — n'affiche pas une page vide mais **la dernière page réelle**
(`_pending_start`) : une page vide serait exacte et muette sur ce qu'il reste. Un
clavier qui ne mène nulle part n'est jamais ajouté quand tout tient sur une page :
il ferait croire qu'il reste des lignes.

Les boutons de verdict sont ceux de `/media` (même politique : `✅`/`❌` quand il y
a du texte indexé, `↩️` seul sinon), mais leur préfixe est `medp:` et non `medl:` :
la même action, et le message à réécrire dépend de la liste d'où vient le clic. La
navigation, elle, est un quatrième préfixe (`medpg:`) : distinct de `medp:`, qui
exige un deux-points juste après, et filtré *avant* le handler générique comme
`medl:`, `medp:` et `medt:`. Les deux callbacks de verdict partagent d'ailleurs le
même corps (`_review_from_list`), qui ne diffère que par le préfixe accepté et la
vue réaffichée.

Une file qui s'accumule se solde aussi **par canal** : les rangées « ✅ @canal » /
« ↩️ @canal » visent *toutes* les attentes d'un même canal — pages suivantes
comprises — et rendent **un seul** compte-rendu. C'est le geste qu'on veut après
une panne de clef : quinze graphiques du même canal se rattrapent ensemble au lieu
de quinze fois.

Ce geste passe par **deux clics**, le seul de `/pending` dans ce cas. Le premier
(`medc:`, `bulk_preview_view`) **n'écrit rien** : il ouvre l'**aperçu** du lot — les
extractions visées, une par ligne et reconnues comme dans la liste (`_media_line`),
celles que le lot laisserait de côté marquées `⏭️` (sans texte indexé pour « ✅ »,
sans identifiant pour « ↩️ »), le plafond annoncé avec le total du canal, et une
phrase qui dit que rien n'a encore été modifié. Le second clic (`medck:`) exécute le
lot (`bulk_review_channel`) ; `medcx:` annule et rend la liste telle qu'elle est
**maintenant**. Un lot écrit sur vingt-cinq lignes d'un coup, et rien, avant,
ne les avait montrées : l'aperçu est cette liste-là, relue au clic avec la **même
règle** que le lot — même filtre de canal, même plafond, même lecture d'index —,
parce qu'un aperçu qui compterait autre chose que ce que le lot fera ferait
confirmer à l'aveugle, en croyant avoir vu. Quand la confirmation n'aurait **rien**
à faire (plus rien en attente, aucune extraction validable), son bouton n'est pas
proposé ; quand la file est illisible, l'aperçu garde « ✖️ Annuler » — un aperçu
sans sortie serait un cul-de-sac —, et quand les charges utiles ne tiennent plus
ensemble dans les 64 octets de Telegram (`medck:` et `medcx:` portent un octet de
plus que `medc:`), la rangée n'est pas proposée du tout : un bouton qui mène à une
impasse n'existe pas.

Trois choix s'y lisent :

* **la portée est la file entière**, pas la page affichée (`pending_channel_groups`
  reçoit toutes les lignes du balayage) — un lot borné à la page ferait passer un
  canal à moitié traité pour un canal soldé. L'index est donc lu en **lots
  bornés** (`INDEX_IDS_PER_QUERY`) : ses identifiants partent dans le filtre
  `in.(…)` de l'URL PostgREST, et une requête refusée pour cause de longueur
  rendrait « rien d'indexé » pour toute la liste d'un coup, sans que rien ne
  distingue ce silence d'un backlog réellement vide ;
* **la cible est relue au clic**, jamais rejouée depuis l'affichage : un verdict
  tombé entre les deux (ici, dans `/media`, ou depuis un autre message) ne doit pas
  être rouvert. Si plus rien ne reste, le compte-rendu le **dit** au lieu de ne
  rien faire en silence ;
* **ce qui est visé est nommé par son étiquette** : « ✅ @signals (3) » n'en valide
  que trois (les extractions qui ont du texte indexé — valider une extraction vide
  écrirait une relecture de rien, comme dans `list_buttons`), et « ↩️ @signals (5) »
  les cinq attentes du canal. Un canal réduit à une seule attente n'a **pas** de
  rangée : les boutons de sa ligne font déjà exactement le même geste.

Le regroupement se fait sur le **nom affiché** du canal (`channel_name`, pseudo du
bot ou nom lu par le scraper), pas sur le `channel_id` : deux lignes qui affichent
`📡 @signals` ne sont pas distinguables par qui les relit, et un bouton qui agirait
sur d'autres lignes que son étiquette ne le dit serait un piège. Un média de **chat
privé** n'a pas de canal : il n'est jamais visé par un lot. Le lot est plafonné
(`CHANNEL_BULK_MAX = 25`) parce qu'une réindexation télécharge et relit chaque
fichier : ce qui dépasse est compté et annoncé, jamais tu. Rejeter en masse n'est
pas proposé (`CHANNEL_BULK_VERDICTS` ne contient que `ok` et `re`) — un rejet
retire des morceaux de l'index, et « tout rejeter » un canal entier est le genre de
geste qu'on ne veut pas pouvoir déclencher d'un clic.


### Embeddings Gemini (`ai/embeddings.py`)

Les vecteurs viennent de l'**API Gemini** (`:embedContent`), sur le palier gratuit,
avec la **même clé `GEMINI_API_KEY`** que l'analyse de news et la vision : aucun
service ni compte supplémentaire, et `httpx` reste la seule dépendance.

| Point | Choix | Pourquoi |
|---|---|---|
| Modèle | `gemini-embedding-001` | seul modèle Gemini à produire un vecteur **par texte** ; `gemini-embedding-2` agrège plusieurs entrées en un seul vecteur |
| Dimension | 768 (`outputDimensionality`) | correspond exactement à la colonne `extensions.vector(768)` — aucune migration de schéma |
| Tâche | `RETRIEVAL_DOCUMENT` / `RETRIEVAL_QUERY` | équivalent Gemini des préfixes `query:`/`passage:` d'e5 ; l'omettre dégrade la similarité |
| Sens des textes | un appel par texte | `embedContent` accepte plusieurs parties, mais c'est le chemin qui déclenche l'agrégation sur les modèles multimodaux |
| Normalisation | L2 systématique | un vecteur tronqué (MRL) n'est plus unitaire, or la similarité cosinus le suppose |

Une **recherche de médias par actif** est exposée par
`knowledge_index.search_media_for_asset(asset)` : les morceaux remontés sont
regroupés par média, chacun représenté par son meilleur morceau, avec la
similarité et le texte correspondant — de quoi retrouver le graphique ou la note
liés à un actif. Sans question explicite, la requête porte sur le *thème* de
l'actif (tendance, niveaux, indicateurs) : c'est là tout l'intérêt d'une
recherche sémantique, qui n'exige pas de mot-clé exact.

Cette recherche passe `filter_asset` : son efficacité dépend donc de l'étiquetage.
Pour les **notes**, rien n'est étiqueté (elles restent le joker, candidat pour tout
actif) ; pour les **médias**, l'étiquette est posée à l'ingestion ou à la main
(`/tag`). La sémantique exacte, verrouillée par `tests/test_knowledge_migration.py` :
un morceau non étiqueté reste candidat, un morceau étiqueté d'un autre actif est
**exclu**, et la correspondance explicite passe devant (`(kc.asset = filter_asset)
desc nulls last`).

Changer de modèle change l'**espace vectoriel** : des vecteurs de deux modèles
dans la même colonne donneraient des similarités dénuées de sens. D'où
`reembed_chunks()` — qui recalcule les vecteurs depuis le texte **déjà stocké**,
sans re-télécharger les médias — et le script qui l'expose :

```bash
python scripts/reindex_embeddings.py --dry-run   # combien de morceaux périmés ?
python scripts/reindex_embeddings.py             # ré-indexe, par lots
```

## Moteur d'alerte interne (remplace TradingView)

Le backend ne dépend plus d'aucun service d'alertes externe. La stratégie qui
vivait dans un script Pine Script est réimplémentée en Python
(`core/alert_engine.py`) et tourne dans l'auto-loop (`workers/auto_loop.py`, une
passe toutes les 15 min) :

* EMA rapide (20) / EMA lente (50) sur les clôtures : croisement haussier →
  BUY, croisement baissier → SELL ;
* stop-loss / take-profit dérivés d'un ATR(14) de Wilder (`SL = entrée ∓ 1.5 ×
  ATR`, `TP = entrée ± 3.0 × ATR`) ;
* les prix viennent des sources **gratuites** déjà utilisées ailleurs
  (`utils/market_data.py` : Yahoo, Binance, Finnhub, Stooq, CoinGecko), sans
  compte ni abonnement.

Les alertes produites suivent exactement le même pipeline que les autres
signaux (validation `core/signal_quality.py`, anti-spam `workers/signal_guard.py`,
notification Telegram) — rien n'est exécuté sans validation humaine.

L'EMA et l'ATR sont **verrouillés par des valeurs de référence**
(`tests/test_alert_engine.py`, classes `EmaReferenceTest` / `AtrReferenceTest`) :
les résultats attendus proviennent d'un calcul indépendant (décimal, précision 50)
suivant les conventions `ta.ema` (amorçage par moyenne simple) et `ta.atr` (RMA de
Wilder), sur une série documentée reproductible à la main. Toute dérive du calcul
— amorçage, facteur de lissage, définition du true range — déplace ces nombres et
fait échouer la suite.

Des **valeurs de référence** à des **propriétés** :
`tests/test_alert_engine_properties.py` vérifie ce qui doit tenir pour *toute*
série — et pas seulement pour la série documentée. Deux propriétés, et ce sont
celles dont dépend la sûreté de l'alerte :

* **l'ATR n'est jamais négatif** (ni au-dessus du plus grand true range). La
  volatilité est une amplitude, et son signe n'est pas décoratif : c'est lui qui
  place le stop-loss du bon côté du prix. Un ATR négatif ne lève rien — il fait
  d'abord rejeter les croisements (`volatility <= 0`), donc **perdre des signaux
  sans le dire** ;
* **l'EMA reste dans l'enveloppe des clôtures** qu'elle a vues, à chaque pas et
  pas seulement à la fin. C'est une combinaison convexe : en sortir signifie un
  coefficient faux, et c'est ce coefficient qui produit les croisements.

L'énumération est aléatoire mais **à graine fixe**, sans dépendance de test à
installer, et rejouable au cas près :

```bash
python -m unittest tests.test_alert_engine_properties
ALERT_PROPERTY_CASE=137 python -m unittest tests.test_alert_engine_properties
ALERT_PROPERTY_SEED=42 python -m unittest tests.test_alert_engine_properties  # élargir
```

Les chandelles produites sont **valides** (`low ≤ min(open, close)` et
`max(open, close) ≤ high`), et les longueurs limites (1, 2, 3 chandelles) sont
énumérées exprès : ce sont les bornes (`len < span`, `len < period`) qui cassent.
La propriété a d'ailleurs trouvé un défaut réel : le true range de la **première**
chandelle valait `high - low` sans valeur absolue, donc une seule ligne mal formée
d'une source de marché partait négative dans la RMA — qui ne la résorbe qu'après
des dizaines de chandelles, le temps de faire rejeter tous les croisements. La
sonde par mutation (`.pgtest/mutate_alert2.py`, non versionné) vérifie que ces
tests rougissent bien sur l'ancienne définition, sur un coefficient d'EMA faux et
sur un poids d'ATR supérieur à 1.

## API interne

Les endpoints `/consensus/*`, `/learning/*`, `/macro/*`, `/reports/*`,
`/media/*` et `/admin/*` exigent un en-tête `X-API-Key: <INTERNAL_API_KEY>`.
Sans clé configurée côté serveur, ils refusent en 503 (fail-closed) ; la surface
complète est figée dans `tests/goldens/api_routes.json`, donc un endpoint qui
disparaît ou change de chemin se voit dans le diff.

`GET /media` **liste** les médias stockés — du plus récent au plus ancien — avec,
pour chacun, ce qu'il faut pour en **choisir** un : de quoi le reconnaître (nom,
type, taille, date, origine), son actif, son verdict de revue, si son contenu a
été extrait, et le **nombre de morceaux** de son texte. C'est la moitié amont de
`GET /media/{id}/text` : aucune ligne de média ne portait d'identifiant côté
tableau de bord, et demander un texte suppose d'en avoir un.

`limit` (1 à 100, défaut 25), `offset` et les filtres `source`/`media_type`
découpent la page ; `next_offset` est le rang de la suivante.

| Champ | Ce qu'il dit |
|---|---|
| `media` | Une entrée par média : `media_id`, `file_name`, `media_type`, `file_size`, `created_at`, `source`, `chat_id`, `message_id`, `asset`, `asset_source`, `review_status`, `extracted`, `chunks` |
| `chunks` | Combien de morceaux ce média a dans l'index — `0` est une réponse, pas une panne |
| `review_status`, `extracted` | Les deux explications d'une ligne à zéro morceau : un verdict humain (`rejected`), ou une extraction qui n'a rien produit (`extracted: false`) |
| `returned`, `truncated`, `next_offset` | La tranche renvoyée, s'il en reste, et où commence la suivante |
| `text` | Où vit la **suite** (`GET /media/{media_id}/text`) |

Quatre comportements qui ne se devinent pas : la table **n'est pas comptée** —
lire une ligne de plus que demandé dit s'il en reste, alors qu'un total exact
demanderait un second parcours de la table pour un nombre dont personne ne se sert
pour lire la page suivante ; la **légende ne sort pas de la base**, la charge
étant reconstruite champ par champ ; une table **ou un index illisible répond
502**, jamais `{"media": []}`, une liste vide voulant dire « aucun média
stocké » ; et le comptage lit **toutes** les pages de l'index (l'API ne rend
qu'une partie des lignes, donc un comptage tronqué annoncerait « 2 morceaux » à un
média qui en a 50). L'ordre de pagination est **total** (`created_at`, puis `id`) :
`offset` ne répète ni ne saute une ligne, même quand deux médias d'un album
partagent leur date à la seconde.

`GET /media/{id}/text` rend le **texte indexé** d'un média : ses morceaux, dans
l'ordre du document, par la délégation média (`media_store.list_media_chunks`).
C'est le pendant HTTP de la pièce jointe `.txt` du compte-rendu Telegram — le
tableau de bord pouvait montrer le fichier, son actif et son verdict de revue,
mais jamais ce qui a réellement été indexé, c'est-à-dire ce que lisent les
analyses, les prompts et `/search`.

| Champ | Ce qu'il dit |
|---|---|
| `chunks` | Les morceaux tels qu'ils sont stockés (`chunk_index`, `content`, `token_count`) |
| `count` | Combien de morceaux les analyses ont réellement à lire |
| `chars` | Total des contenus — le découpage **chevauche** ses morceaux, donc ce total dépasse la longueur du document d'origine |
| `asset`, `asset_source` | L'étiquette d'actif et sa provenance (`caption`, `extraction`, `manual`) |
| `review_status` | Le verdict de revue (`validated`, `rejected`), ou `null` si le média n'a jamais été relu |

Deux comportements qui ne se devinent pas : un média **introuvable** répond 404,
mais un média **sans texte** répond 200 — un média rejeté (`❌`) a zéro morceau et
reste parfaitement valide, c'est `review_status` qui l'explique ; et l'`embedding`
n'est **jamais lu**, la projection étant restreinte dans la requête — ramener le
vecteur de 768 flottants qui accompagne chaque morceau ferait traverser des
dizaines de kilo-octets par morceau, de Postgres jusqu'au tableau de bord, pour un
affichage qui n'en montre rien.

`GET /admin/media/orphans` expose la **réconciliation** bucket ↔ `knowledge_media`
(`scripts/reconcile_media.py`) pour la consulter depuis un navigateur plutôt qu'en
lançant un script sur la machine. `prefix` restreint le parcours à un dossier du
bucket, `limit` borne la réponse (1 à 1000, défaut 100) et `offset` avance dans la
liste — la suite se lit avec `next_offset` sans relever la borne.

| Champ | Ce qu'il dit |
|---|---|
| `orphans` | Les chemins sans ligne `knowledge_media`, triés |
| `count` | Le total **exact** trouvé — pas ce qui est renvoyé |
| `returned`, `truncated` | La tranche renvoyée, et si elle cache la suite |
| `offset`, `limit`, `next_offset` | Le rang demandé, la taille de page, et la page suivante (`null` à la fin) |
| `prefix`, `bucket` | Ce qui a été parcouru, et où |
| `deletion` | Où la **suppression** vit (`scripts/reconcile_media.py --delete`) |

Trois comportements qui ne se devinent pas : l'endpoint est en **lecture seule** —
le script garde son `--delete`, parce qu'un `GET` qui supprime est rejoué par un
navigateur, un cache ou une sonde de disponibilité ; un Storage injoignable répond
**502**, jamais `{"orphans": []}`, une liste vide voulant dire « bucket et base
cohérents » et confondre les deux ferait passer une panne pour une bonne nouvelle ;
et `limit` borne la **réponse, pas le travail** — `list_orphan_objects` parcourt le
bucket en entier de toute façon, donc `count` reste exact et une réponse tronquée
le dit. C'est aussi pourquoi la suite se **demande** (`offset`, `next_offset`) au
lieu de s'obtenir en relevant la borne : la même lecture complète étant faite à
chaque appel, un listing qui ne rendrait que la première tranche obligerait à
réclamer les mille d'un coup pour voir le reste. Aucun instantané n'est gardé
d'entre les pages : un nettoyage ou une ingestion survenu entre-temps déplace les
rangs, et `count` est relu plutôt que promis.

`GET /admin/media/missing` est le **sens inverse**, et le plus gênant des deux :
la ligne est complète (légende, canal, message, actif, verdict de revue, texte
indexé) mais les octets ne sont plus dans le bucket — l'application affiche un
fichier fantôme, dont les liens signés répondent 404, et aucune lecture de la base
ne s'en aperçoit.

| Champ | Ce qu'il dit |
|---|---|
| `missing` | Une entrée par ligne sans objet : `media_id`, `storage_path`, type, source, `chat_id`, `message_id`, taille, date, actif, verdict |
| `route` | La voie de retour **possible avant tout essai** : `telegram_file_id`, `channel_scraper`, ou `null` si aucune |
| `fingerprint` | Si une empreinte a été enregistrée à l'ingestion, donc si la restauration pourra être **prouvée**. `false` ne condamne pas la ligne — on ne saura dire que la taille |
| `repairable` | Combien d'entrées renvoyées ont une voie — un `chat_id` numérique et aucun `file_id` n'en donnent aucune |
| `count`, `returned`, `truncated` | Le total exact, la tranche renvoyée, et la troncature |

`POST /admin/media/missing/repair` (corps `{"media_ids": ["…"]}`, 1 à 20) tente la
réparation : `telegram_file_id` d'abord (le fichier exact, via le bot), puis
l'aperçu public du canal (la seule voie pour un média ingéré par le scraper). Le
rapport compte séparément `restored`, `failed` et `skipped` — ils n'appellent pas
la même suite : un échec se rejoue, un « non réparable » ne changera pas tout seul.
Un identifiant inconnu part dans `unknown` sans interrompre les autres.

Chaque résultat restauré dit si les octets sont **ceux de l'ingestion**
(`fingerprint_matches`), et le lot résume : `verified` (prouvé) et `diverged` (un
autre fichier). Les deux ne s'additionnent pas à `restored` — la différence est
faite des médias sans empreinte enregistrée, où l'on ne sait pas ; la compter
comme une preuve présenterait un média jamais vérifié pour un média vérifié.

Deux refus qui protègent des données, et qui expliquent pourquoi ce verbe est un
`POST` : un objet **déjà présent n'est jamais réécrit** — la voie du scraper rend
la copie de l'aperçu, possiblement réduite, donc restaurer par-dessus un original
intact l'abaisserait de qualité pour réparer une panne imaginaire ; et la **ligne
n'est jamais modifiée**, sinon `metadata` (verdict de revue, actif, note
d'extraction) serait écrasé et l'extraction repayée alors que le texte est
toujours indexé. Restaurer un fichier ne doit pas détruire ce qui a été enregistré
à côté.

### Vérifier la base depuis l'application

`GET /admin/supabase/check` et `POST /admin/supabase/roundtrip` exposent la sonde
de configuration (`scripts/check_supabase.py`) : le même rapport, la même
vérification, mais déclenchée depuis l'application plutôt qu'un terminal — utile
quand la question se pose depuis un téléphone, ou pour un bouton d'écran
d'administration.

| Appel | Ce qu'il fait |
|---|---|
| `GET /admin/supabase/check` | Les trois niveaux **sans rien écrire** : configuration, lecture des tables, sélection. `only` restreint aux tables ou groupes visés |
| `POST /admin/supabase/roundtrip` | Le `--roundtrip` : écrit, relit puis **supprime** sur les tables nommées (`{"tables": ["core", "engine"]}`, 1 à 20) |

Les noms passent par la **même résolution** que `--only` : une table ou un groupe
(`core`, `engine`, `knowledge`), insensible à la casse. Un nom inconnu est un
**400** qui liste les choix, refusé **avant la première écriture** — jamais replié
sur « toutes les tables », qui, avec l'aller-retour, écrirait précisément là où on
a demandé de ne pas aller.

Le rapport est celui du script (`sections`, `ok`), auquel s'ajoutent trois champs :

| Champ | Ce qu'il dit |
|---|---|
| `selection` | Les tables réellement visées, **groupes résolus** (et les noms demandés, tels quels) |
| `writes` | Les tables **écrites** : `null` pour la consultation, vide si la sélection ne contient aucune table que l'aller-retour sache éprouver |
| `leftovers` | Les lignes de sonde **restées en base** après le nettoyage (vide en temps normal) |
| `alert` | `null` sans reste ; sinon `{"sent": true}` — ou `{"sent": false, "reason": "no_admin_chat" / "send_failed"}`, jamais un échec silencieux |
| `command` | La ligne de terminal équivalente, à rejouer à la main |

Le verdict reste donc **dans la réponse** : une base en échec est un 200 qui le dit
(`ok: false` et la ligne fautive), parce que la forme du rapport sait l'exprimer —
c'est le contraire d'une liste vide, indiscernable d'une bonne nouvelle. Un **502**
est réservé à la sonde qui **tombe** : l'appel n'a alors rien rendu de lisible. Et
la requête ne lit **pas** le `.env` de la machine : le serveur a déjà sa
configuration, une requête ne doit pas pouvoir désigner une autre base que la
sienne.

Un **nettoyage incomplet** se voit aussi par ici : la réponse porte `leftovers`, et
la route déclenche l'**alerte** du script (l'opérateur n'a pas forcément la page
ouverte). C'est la seule entrée où l'alerte est visible dans la réponse elle-même
(`alert`), parce que l'appelant est justement celui qui regarde le rapport.

C'est le **seul** endpoint de la surface qui écrit en base. Il a le même verrou que
le reste de l'administration (la clé interne) **plus** un périmètre que l'appelant
doit écrire : un `POST` qui écrit ne déduit pas son périmètre, et un `GET` qu'un
cache ou un préchargement rejouerait ne peut pas déclencher l'aller-retour. Un
nettoyage qui échoue est annoncé dans le rapport, avec le détail à supprimer à la
main.

### Voir les comptes bloqués par une référence de solde inutilisable

`GET /admin/risk/reference-unusable` publie le compteur que le garde-fou tient en
mémoire : le total des refus pour référence inutilisable, et par utilisateur les
champs fautifs avec l'horodatage de première observation. C'est la réponse « qui est
bloqué, par quoi, depuis quand » sans relire les journaux — **lecture seule**, la
réparation restant `scripts/repair_risk_state.py` (voir plus haut).

### L'écran d'administration de l'application (`ui/AdminSupabaseScreen.kt`)

Le même rapport, depuis un téléphone. L'écran vit derrière la carte « santé
système » de l'onglet Terminal (`ui/SystemHealthObservabilityCard.kt`) : un bouton
l'ouvre, la touche retour ou « Fermer » en sortent. Il n'ajoute **pas** de sixième
onglet — Material en recommande cinq au maximum, et la barre en porte déjà cinq.

| Ce que l'écran fait | Comment |
|---|---|
| La consultation | `GET /admin/supabase/check`, sur un bouton — aucune ligne créée, modifiée ni supprimée |
| L'aller-retour | `POST /admin/supabase/roundtrip`, sur les tables **saisies**, derrière une confirmation qui les nomme |
| Le rapport | Celui du backend, tel quel : noms et détails des vérifications, tables visées, tables écrites (`writes`), lignes restées (`leftovers`), commande équivalente |

Trois choses que l'écran ne fait **pas**, et qui sont tout le sujet :

* **il ne fabrique pas de rapport.** Sans URL et sans clé enregistrées, il le dit
  et refuse de lancer quoi que ce soit — un rapport inventé sur l'appareil
  ressemblerait à une base en bon état, et c'est exactement ce qu'on cherche à
  éviter ;
* **il ne compile pas la clé.** La clé interne se saisit dans l'écran et se
  conserve **chiffrée** sur l'appareil (Android Keystore, comme les identifiants
  courtier : table Room `admin_endpoint`, `data/TradingRepository.kt`). Elle n'est
  jamais compilée dans l'APK, ni lue depuis `BuildConfig` : une clé compilée est
  une clé que quiconque décompile l'application possède, et celle-ci ouvre le seul
  endpoint qui **écrit** en base ;
* **il n'écrit pas sans confirmation.** Le bouton de l'aller-retour ne fait
  qu'ouvrir la confirmation ; l'appel réseau est déclenché par elle seule, et le
  message nomme les tables visées. Même verrou que le backend — qui, lui, refuse
  un nom inconnu avant la première écriture — et la même raison : un `POST` qui
  écrit ne doit pas déduire son périmètre.

Les libellés de l'écran sont des ressources (`admin_*`) dans les dix langues,
comme le reste de l'interface. Le **rapport**, lui, n'est pas traduit : ses
libellés sont ceux de `scripts/check_supabase.py`, et les traduire ferait diverger
l'écran de la commande équivalente qu'il affiche.

Le contrat de cet écran est tenu par `tests/test_android_admin_screen.py` (relu
dans les sources Kotlin, faute de compilateur partout) et par
`app/src/test/java/com/aitrade/ui/AdminSupabaseScreenTest.kt` (l'écran réel, sur la
JVM avec Robolectric).

L'ingestion publique d'alertes est `POST /webhook/alert` (protégée par
`WEBHOOK_SECRET` envoyé dans le corps, comparé en temps constant). Elle reste
**agnostique du fournisseur** : n'importe quelle source gratuite (un script
maison, n8n, un autre outil d'alertes) peut pousser `{ticker, action, price,
stop_loss, take_profit}` sans dépendre de TradingView.

La surface web est **découplée de Telegram** : `api/webhook` s'importe sans
`python-telegram-bot`, et le `Bot` n'est construit qu'au premier envoi. Un token
absent ou invalide dégrade les notifications (0 notification envoyée) sans
empêcher le serveur de démarrer. Le bot en polling (`main.py`) reste, lui, une
dépendance volontaire.

## Tests

```bash
pytest                                  # backend Python
./gradlew :app:testDebugUnitTest        # écrans Compose (JVM + Robolectric)
```

Trois aides sont **partagées** par les fichiers de `tests/`, pour qu'une même
question ne reçoive pas trois réponses divergentes :

| Aide | Ce qu'elle met en commun |
|---|---|
| `tests/supabase_double.py` | le client Supabase **en mémoire** (tables, filtres, RPC, Storage), avec un mode **lecture seule** qui refuse toute écriture — voir « Médias Telegram » |
| `tests/sql_columns.py` | l'extraction des colonnes et objets **déclarés** en SQL, lue par la sonde de dérive et les contrats de migration |
| `tests/hook_support.py` | l'exécution des hooks `.githooks` : wrappers, dépôt jetable, PATH restreint |

Le harnais des hooks porte une règle qui n'est pas de la commodité : **un refus
se lit dans un verdict, jamais dans un code de retour** (`git commit` et
`git push` écrasent à 1 le code de leur hook), et **une commande qui réussit
n'est jamais rejouée** — sauf quand git dit lui-même qu'il n'a pas lancé le hook
(`cannot spawn`). Rejouer un succès ne peut que fabriquer un échec : un commit
rejoué trouve « rien à committer », un push rejoué « everything up-to-date ». Le
bruit de `fork` de MSYS2, plus fréquent sur une suite complète saturée, a déjà
transformé un succès en échec par ce chemin.

Un **transport mort avant le hook** est de la même famille : git lit les
références du dépôt distant *avant* d'appeler `pre-push`, donc un
`fatal: Could not read from remote repository` — sans cause nommée — dit
seulement que son enfant n'a pas démarré, et rien du hook. Le test qui attendait
un refus rougissait alors que rien n'avait été décidé (capturé, puis reproduit en
lançant quatre exemplaires du harnais de front). C'est un incident, réessayé,
puis un test **passé**. Quand git **nomme** la cause (dépôt distant inexistant),
c'est l'échafaudage du test qui est en faute : l'échec est rendu tel quel, sans
être absorbé. Et l'environnement des sous-processus
est **construit** par le test, pas hérité : les valeurs des secrets connus, mais
aucun des réglages que le gate lit lui-même (`SECRET_ROTATION_LEDGER`,
`SECRET_MAX_AGE_DAYS`) — sans quoi un shell déciderait de la conclusion à la place
du test, une date enregistrée ailleurs faisant échouer le gate « pour cause de
temps ».

La même règle vaut quand un wrapper est invoqué **directement**
(`run_wrapper_conclusion`) : son code de retour est la réponse — « refusé » contre
« rien n'a pu être contrôlé » — mais seulement si son **enfant** a tourné. Sous
MSYS2 saturé, c'est le processus lancé qui meurt : le scan rend 127, et le wrapper
conclut `3`, honnêtement. Ce `3`-là est rejoué puis le test est **passé**. Le tri
ne tient qu'à ce que le wrapper laisse derrière lui — le bruit du runtime
(`child_copy:`, `cygheap read copy failed`, `0xC0000142`) — et à rien d'autre : un
`3` sans ce bruit reste une conclusion, et un refus accompagné de bruit reste un
refus. C'est la faute à ne pas commettre dans ce sens : absorber une conclusion
ferait passer une fuite de secret pour une machine fatiguée.

### Les épreuves de mutation — hors CI, jamais laissées en place

`.pgtest/` (non versionné) porte les épreuves de mutation : chacune casse **une**
garantie dans un fichier de production, exige que le test nommé rougisse, puis
restaure à l'octet près. Un `finally` ne s'exécute **pas** quand le processus est
tué — délai dépassé, ou le `fork: Resource temporarily unavailable` de MSYS2 sur
ce poste — et la mutation reste alors sur le disque : c'est arrivé, un
`_pending_start(0, …)` laissé dans `notifications/telegram_media.py` par une
épreuve interrompue, et c'est un test qui l'a dit avant son auteur.

Chaque épreuve commence donc par `mutation_guard.watch(__file__)`, qui **répare au
démarrage** ce qu'une exécution précédente a laissé : les octets d'origine sont
journalisés sous `.pgtest/.mutation_journal/` **avant** chaque écriture, l'entrée
tombe dès que le fichier est revenu à son état, et le journal d'une épreuve tuée se
répare au passage suivant (ou tout de suite, par
`python .pgtest/mutation_guard.py --repair`).

L'audit statique des ancres (`python .pgtest/audit_anchors.py`) lit les `MUTATIONS`
sans rien exécuter : une ancre périmée (motif à 0 occurrence), une ancre qui ne
désigne plus **un** site (motif qui vit deux fois, `replace(…, 1)` cassant le
premier venu) ou un reste journalisé y sont nommés, et les mutations qu'il ne sait
pas lire sont **dites** non lues plutôt que comptées vertes. Une mutation à **effet**
— fichiers écrits, fichier déplacé — n'est pas illisible pour autant : elle
**déclare** sa cible et son ancre, et l'audit la couvre comme les autres. Trois
formes sont lues : un couple présent/remplacé, une **ancre d'absence** quand la
mutation ne fait qu'ajouter du texte (s'il est déjà là, la mutation est vivante),
un `None` quand le fichier est lui-même le site — l'audit vérifie alors la *cible*,
et le dit ainsi plutôt que de compter une ancre qui n'existe pas. `--repair` remet
les sources en place sans lancer les tests.

Ces questions ne vivent pas seulement dans un outil qu'on pense à lancer :
`tests/test_mutation_corpus.py` les porte dans la suite — garde `__main__`,
garde-fou d'interruption, ancres qui résolvent et désignent **un** site, aucune
mutation laissée en place, et rien d'écrit par les imports — et s'ignore
proprement quand `.pgtest` n'existe pas, c'est-à-dire en CI. Le garde-fou lui-même
y est éprouvé sur des fichiers fabriqués dans un répertoire temporaire : le patch
global, lui, appartient aux épreuves.

### Tests d'interface Kotlin — sans émulateur

Les tests de `app/src/test/java/com/aitrade/ui/` composent les **écrans réels**
(`RiskSettingsScreen`, `TerminalScreen`, les tableaux de bord…) sur la JVM, avec
Robolectric pour les ressources et le cycle de vie Android : aucun appareil, aucun
émulateur, et donc exécutables en CI ordinaire. Trois réglages les rendent
possibles, et `tests/test_kotlin_test_wiring.py` les verrouille parce que leur
absence ne produit qu'un échec lointain :

| Réglage | Sans lui |
|---|---|
| `unitTests.isIncludeAndroidResources = true` | Robolectric ne voit aucune ressource |
| `ui-test-manifest` en `debugImplementation` | `createComposeRule()` ne démarre aucune activité |
| `@Config(sdk = [34])` couvert par la version de Robolectric épinglée | Robolectric refuse de démarrer |

En CI, le job `kotlin-build-and-test` lance ces tasks **à chaque push**, avec les
paquets SDK qu'il faut et pas d'autres : `platforms;android-36` (le `compileSdk`
déclaré) et `build-tools;34.0.0` (la version que réclame l'AGP 8.5.2 — l'AGP
n'utilise pas les build-tools les plus récents installés).
`gradle/actions/setup-gradle` met la distribution et les dépendances en cache, et
valide au passage le jar du wrapper contre les checksums publiés par Gradle.

### Valeurs de référence (goldens versionnés) — `scripts/goldens.py`

Un test qui écrit ses valeurs attendues **en ligne** les rend modifiables sans que
personne ne le remarque : on ajuste le chiffre, la CI repasse au vert, et la dérive
est validée par le commit qui la contient. Les valeurs de référence vivent donc dans
des fichiers JSON **versionnés** (`tests/goldens/*.json`), et la CI recalcule les
valeurs depuis le code pour les comparer :

```bash
python scripts/goldens.py                       # vérifie la dérive (code 1) — ce que fait la CI
python scripts/goldens.py --update              # régénère tous les goldens
python scripts/goldens.py --update alert_engine # régénère un seul jeu
python scripts/goldens.py --list                # liste les jeux connus
```

| Jeu | Ce qu'il fige | Pourquoi un diff y est utile |
|---|---|---|
| `alert_engine` | séries complètes EMA(20)/EMA(50) sur la série documentée, ATR de Wilder (2, 5, 14, 21), true ranges, constantes de la stratégie, et la **sortie complète de `detect_cross`** (action, prix, stop-loss, take-profit, message, EMA, ATR) — multiplicateurs par défaut, puis 1,0/2,0, puis périodes 5/10 | une dérive de calcul (amorçage, facteur de lissage, arrondi) déplace ces nombres ; un moteur qui cesse d'appliquer `sl_atr_mult`/`tp_atr_mult` rend identiques deux enregistrements qui **doivent** différer. Le diff en est la seule trace |
| `android_localization` | jeu de clés de `values/`, clés `translatable="false"`, clés manquantes ou en trop par langue, arguments de format | ajouter, renommer ou déplacer une chaîne est un acte délibéré ; une langue oubliée apparaît dans le diff |
| `api_routes` | surface HTTP publique de `api.webhook.app` (méthode + gabarit de chemin) | un endpoint renommé, supprimé ou déplacé casse des clients : on veut le voir dans la revue |
| `similarity_calibration` | seuil de similarité des médias : bruit mesuré sur la base (percentile, marge), bornes, et les extraits que la règle de sélection retient | le seuil n'est plus une constante : changer un percentile ou une marge déplace la sélection, et le diff montre **quels extraits** entrent ou sortent |
| `postgres_schema` | le schéma `public` **obtenu** par les migrations, au `pg_dump --schema-only` : corps compilés des fonctions, définitions exactes des index (`using hnsw (embedding extensions.vector_cosine_ops)`, `desc`, `where`), types et défauts que `bigserial` produit, contraintes telles que Postgres les a acceptées | les contrats de migration relisent le **texte** du SQL ; rien d'autre ne regarde ce que Postgres en a **retenu**. Un index HNSW remplacé par un `btree`, une fonction recompilée autrement ou une contrainte silencieusement refusée ne se voient que dans le dump |

`postgres_schema` n'est pas recalculable partout : sans serveur PostgreSQL joignable
(`MIGRATION_DATABASE_URL`), sans `pg_dump` et sans `psycopg`, le jeu est annoncé
**ignoré** (`[--]`, avec ce qu'il faut pour le recalculer) — le script et la suite
acceptent ce `skip`, un contrôle non exécutable n'étant pas une dérive. Le fichier,
lui, reste exigé : un jeu ignoré garde sa référence versionnée. La contrepartie est
dans la CI : le job qui a une base (`migrations-postgres`) le **compare** pour de bon,
avec le `pg_dump` de la même version majeure que celle enregistrée dans le golden
(sinon l'instantané décrirait l'outil, pas le schéma).

En cas de dérive, le gate échoue avec le `diff` du fichier et la commande exacte de
régénération. Accepter un changement, c'est régénérer le golden et le commiter **dans
le même commit** que le changement de code : le relecteur voit alors la valeur de
référence bouger, ce qu'un nombre enfoui au milieu d'un test ne montrerait pas.

Cinq choix assumés :

* **un golden manquant est un échec**, pas un « rien à comparer » : sinon supprimer le
  fichier serait la façon la plus simple de faire passer le gate ;
* la comparaison se fait **par valeur** (JSON relu, ordres triés), pas octet à octet —
  régénérer sous Windows ou sous Linux produit le même verdict ;
* `api_routes` charge l'application FastAPI dans un **sous-processus** avec un
  environnement factice : l'extraction n'exige aucun secret et ne pollue pas la
  configuration figée à l'import des autres tests. Sans FastAPI installé, le jeu est
  annoncé **ignoré** (`[--]`), jamais vert en silence ;
* le **texte** des traductions n'est pas figé dans un golden : le corriger est du
  travail éditorial légitime. `tests/test_android_strings.py` vérifie sa *correction*
  (parité des clés, échappement des apostrophes, `formatted="false"`, arguments de
  format identiques à l'anglais), et `alert_engine` ne remplace pas les vérifications
  ponctuelles de `tests/test_alert_engine.py` : il fige la série entière ;
* la série de **niveau** ne croise jamais (sa dérive positive garde EMA20 au-dessus
  d'EMA50 d'un bout à l'autre) et le golden fige ce `null` — une régression de
  l'amorçage ou du sens de l'inégalité la ferait basculer à tort. Le croisement est
  donc verrouillé sur une **seconde série documentée** : 59 chandelles plates à 100
  puis une cassure de ±20 sur la *dernière* chandelle close, la seule que
  `detect_cross` examine (`crosses.none_on_level_series` contre
  `crosses.documented_bullish` / `documented_bearish`).

## Build Android

Le projet Android cible `compileSdk 36` / JDK 21, AGP 8.5.2.

```bash
./gradlew :app:assembleDebug          # build
./gradlew :app:testDebugUnitTest      # tests Compose UI (Robolectric, sans device)
```

En CI, un **seul** job fait les deux (`:app:assembleDebug` puis
`:app:testDebugUnitTest`) et publie l'APK debug (`apk-debug`) ainsi que le rapport
de tests en artefacts. `assembleDebug` est nécessaire en plus des tests :
`testDebugUnitTest` compile le module mais n'assemble aucun APK, donc sans lui
le job ne produit aucun livrable installable.

Le wrapper est committé et **épingle Gradle 8.9**, avec le SHA-256 officiel de la
distribution (`distributionSha256Sum`) : une distribution substituée est refusée.
La version n'est pas un choix esthétique — AGP 8.5.2 exige Gradle **8.7+** et
n'est pas supporté sur Gradle 9.x (qui demande AGP 8.11+).

| Fichier | Provenance |
|---|---|
| `gradlew`, `gradlew.bat`, `gradle/wrapper/gradle-wrapper.jar` | dépôt officiel `gradle/gradle`, tag `v8.9.0` |
| `gradle/wrapper/gradle-wrapper.properties` | écrit ici (version + checksum) |

SHA-256 du jar committé :
`498495120a03b9a6ab5d155f5de3c8f0d986a449153702fb80fc80e134484f17` — c'est
exactement la valeur publiée par Gradle pour 8.9 (listée pour `8.9`, `8.9-rc-1`
et `8.9-rc-2` dans `wrapper-checksums.json` du dépôt `gradle/actions`, qui est ce
que la validation de wrapper de la CI compare). C'est aussi l'empreinte que
garde `tests/test_kotlin_lint_config.py` : un jar altéré ne se voit pas à la
lecture, mais Gradle refuse de démarrer.
Pour le **régénérer localement** (et obtenir un jar produit par ta propre
installation plutôt que téléchargé) : `gradle wrapper --gradle-version 8.9`
dans un environnement avec Gradle, ou « Sync » dans Android Studio.

L'app est une maquette de démonstration (voir plus haut).

## Lint Kotlin — un seul gate bloquant : ktlint

Toute violation de style **fait échouer la CI** :

```bash
./gradlew :app:ktlintCheck    # gate bloquant : la moindre violation échoue
./gradlew :app:ktlintFormat   # corrige ce qui est corrigeable automatiquement
```

Le gate est **ktlint** (plugin `org.jlleitschuh.gradle.ktlint`), appliqué au
module `:app`. `:app:ktlintCheck` couvre d'un coup ses source sets (`main`,
`test`) **et** son script de build (`app/build.gradle.kts`). Moteur et plugin
sont épinglés dans `gradle/libs.versions.toml` (ktlint 1.4.1, ktlint-gradle
12.3.0) : le ruleset ne bouge pas d'une machine à l'autre. `ignoreFailures` vaut
`false` dans `app/build.gradle.kts` — c'est ce qui rend le gate bloquant — et il
n'existe plus de second gate : detekt a été retiré.

Les sources ont été **formatées une fois** avec ce même moteur
(`:app:ktlintFormat`) : le gate part donc d'un état propre et ne signale que les
régressions, jamais du formatage hérité.

Deux règles sont ajustées dans `.editorconfig`, chacune pour une raison
écrite noir sur blanc :

- **`no-wildcard-imports` est désactivée.** Le projet conserve des imports
  étoile volontaires (`androidx.compose.*`, `com.aitrade.engine.*`,
  `com.aitrade.ui.theme.*`). Elle ne peut l'être que dans `.editorconfig` :
  l'option `disabledRules` de ktlint-gradle n'est plus supportée depuis le
  moteur ktlint 0.48.
- **`function-naming` ignore les fonctions annotées `@Composable`.** Compose
  nomme ses fonctions en PascalCase (`VipPaywallCard`, `AiTradeTheme`) : c'est la
  convention du framework, pas une entorse.

À noter : la limite de ligne est celle de `ktlint_official` (140). Les quelques
chaînes de traduction qui la dépassaient ont été découpées par concaténation
(`"…" + "…"`), pas exemptées.

### La même partie, sans Gradle : `scripts/kotlin_ui_check.py`

Ce projet ne peut pas être compilé partout (ni JDK, ni SDK Android, ni
distribution Gradle téléchargée sur toutes les machines). Le script couvre la
part vérifiable autrement, en quelques millisecondes, et sert de **pré-vol** au
job `kotlin-lint` avant d'installer le JDK, le SDK et Gradle :

```bash
python scripts/kotlin_ui_check.py                    # paquet ui par défaut (app/src/main/java/com/aitrade/ui)
python scripts/kotlin_ui_check.py --max-lines 400    # autre seuil d'alerte
python scripts/kotlin_ui_check.py --sources app/src/main/java/com/aitrade/engine  # réduire le périmètre
```

Deux périmètres, et c'est voulu : les contrôles structurels portent sur le
paquet `ui` (`--root`), tandis que la **mise en forme** porte sur tout le Kotlin
du module (`--sources`, `app/src` par défaut) — c'est ce que `:app:ktlintCheck`
met dans son périmètre, et une ligne trop longue ajoutée dans `engine/` ou
`data/` compte autant qu'une dans un écran.

Ce qu'il contrôle, et pourquoi :

| Contrôle | Défaut qu'il attrape |
|---|---|
| aucun import nommé inutilisé | le `no-unused-imports` de ktlint, reproduit sans JDK |
| aucune déclaration de premier niveau en double | deux `fun Foo` après un découpage de fichier — ne compile pas |
| accolades équilibrées | un bloc de composition coupé en deux au milieu d'une lambda |
| aucune taille de police en dur | un `fontSize = 10.sp` qui contourne l'échelle du thème (voir plus haut) |
| aucun libellé en dur | un `Text("Save changes")` oublié, donc jamais traduit (voir la section « Localisation ») |
| cohérence de `AppStrings` | un champ jamais projeté (donc `null` à l'exécution), un `strings.champInexistant`, ou un `ui.champ` sans `val ui` en portée — les fautes qu'un compilateur trouverait et qu'une réécriture en masse produit |
| forme explicite sur chaque bouton | un bouton qui reprend la forme par défaut de Material 3, c'est-à-dire une pilule (voir « Formes des boutons ») |
| aucune ligne au-delà de `max_line_length` | une expression qui repousse la limite de 140, avec **les mêmes exceptions que ktlint** (voir ci-dessous) |
| indentation en espaces, multiples de `indent_size` | une tabulation, ou un bloc décalé de 2 espaces par un éditeur ou un script |
| avertissement au-delà de 300 lignes | écran devenu illisible (un repère, pas une erreur : le script sort en 0) |

Les deux seuils de mise en forme sont **lus dans `.editorconfig`**, jamais
recopiés : si le fichier passe à 120 caractères ou à une indentation de 2, le
contrôle suit, et s'il est absent, le contrôle le dit au lieu d'inventer un
seuil. C'est ce qui l'empêche de dériver du gate qu'il reproduit.

Les exceptions de `max-line-length` sont celles de la règle, vérifiées dans son
code source : une ligne qui n'est **qu'**un commentaire, l'intérieur d'un
commentaire de bloc ou d'un KDoc, le contenu d'une chaîne brute, une ligne qui ne
porte qu'un littéral (éventuellement suivi d'une virgule) et une directive
`package`/`import`. Un commentaire **après** du code, lui, compte : c'est la même
limite, appliquée au même endroit. Ces exceptions ne sont pas théoriques, elles
sont la raison pour laquelle l'arbre actuel est vert — les trois lignes de plus
de 140 caractères qu'il contient sont deux contenus de chaîne brute (les prompts
Gemini) et un commentaire seul.

Ce que ce contrôle **ne** fait pas, et le dit : il ne modélise pas la profondeur
des blocs. Mesuré sur ce dépôt, un modèle « l'indentation vaut 4 × la profondeur
d'accolades » produit 792 désalignements **légitimes** (signatures étalées
sur plusieurs lignes, lambdas ouvertes en fin de continuation, `when`, `?:`) : il
rendrait la CI rouge pour de mauvaises raisons. Une désindentation franche d'un
bloc entier passe donc ici sans être vue ; seul ktlint, qui a l'arbre, la voit.

Le contrôle des libellés reconnaît une phrase à ce qu'elle contient un espace
**et** un mot de quatre lettres ou plus ; un identifiant (`BTC`, `image/jpeg`,
`chat_list`, `BLOCKED_RISK`) ne passe pas ce filtre. Les rares textes gardés en
dur sont listés nommément dans `ALLOWED_LITERAL_TEXT`, avec leur justification.


Les imports étoile sont ignorés : sans classpath, leurs symboles sont
indéterminables — et ktlint, lui aussi, ne contrôle pas la règle
`no-wildcard-imports` (désactivée). Les commentaires et le texte des chaînes sont
ignorés, mais **pas** les interpolations (`"$x"`, `"${strings.y}"`) : ce sont de
vraies références, et les ignorer ferait passer un import utilisé pour inutilisé.

Une exception à retenir : le contrôle de cohérence de `AppStrings` lit le fichier
tel quel, commentaires compris. Un commentaire qui cite `strings.quelque_chose`
y est donc signalé comme un accès à un champ inexistant — c'est arrivé, et le
remède est de reformuler le commentaire, pas de désactiver le contrôle.

Ce script est le premier pas du job CI `kotlin-lint`, avant ktlint : le
diagnostic arrive sans attendre le téléchargement de Gradle ni du SDK.

## Licence

Projet privé — aucun conseil financier. Le trading comporte un risque de perte.
