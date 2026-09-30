# Exceptions silencieuses — le triage des replis tolérés

Un `except` qui avale une panne sans rien en dire transforme une erreur en succès
apparent, et rien ne le signale ensuite. Le contrôle
[`scripts/check_silent_handlers.py`](../scripts/check_silent_handlers.py) refuse
donc **tout** handler qui n'émet aucun signal — sauf les replis **déclarés et
enregistrés** dans l'inventaire versionné
[`tests/silent_exceptions.json`](../tests/silent_exceptions.json).

Ce document dit **pourquoi** chaque repli toléré l'est. C'est la part qu'aucun
script ne peut deviner : l'inventaire nomme les exemptions (fichier, symbole,
clause, raison), mais il ne dit pas où passe la frontière entre un repli
légitime et une **mesure fabriquée**. Ce document la dit, une catégorie à la
fois, et un test
([`tests/test_silent_handlers.py`](../tests/test_silent_handlers.py),
`DocumentationTest`) refuse toute dérive entre les deux : chaque exemption de
l'inventaire doit être triée ici, et aucune ligne de ce triage ne doit survivre à
l'exemption qu'elle décrit.

## Où c'est implémenté

| Élément | Rôle |
|---|---|
| `scripts/check_silent_handlers.py` | Détecteur + contrôle : refuse un silence non déclaré, un inventaire absent/illisible/en dérive, un arbre lu à moitié |
| `tests/silent_exceptions.json` | L'inventaire **versionné** des exemptions — c'est lui, et lui seul, qui absout un repli |
| `docs/EXCEPTIONS.md` | Ce document : le **triage** — pour chaque régime, le critère et les replis qui y tombent |
| `tests/test_silent_handlers.py` | Éprouve le détecteur, l'inventaire, le contrôle sur des arbres jetables, le câblage CI, et la cohérence inventaire ↔ triage |
| `.github/workflows/ci.yml`, job `python` | `python scripts/check_silent_handlers.py`, avant `pip install` (bibliothèque standard uniquement) |

Le mécanisme d'exemption lui-même (marqueur `# sans signal : <raison>`, puis
`--update`) est décrit dans la section « Les `except` muets » du
[README](../README.md).

## La frontière : repli accepté, mesure fabriquée

Un repli est **accepté** quand le lecteur en aval peut distinguer « la chose a
échoué » de « la chose vaut zéro / est vide ». Il est une **mesure fabriquée** —
et n'est jamais accepté — quand l'échec produit une valeur qu'une lecture
ultérieure prend pour une mesure : le solde `0` d'un compte qu'on n'a pas
interrogé, une liste vide présentée comme « rien trouvé », une métrique `0.0`
alors que la sonde est morte.

Trois questions décident du côté de la frontière :

1. **Le repli produit-il une valeur qu'un lecteur prendra pour mesurée ?** Si
   oui, il n'y a pas d'exemption possible : il faut rendre l'absence
   **distinguable** (`None`, une exception, une raison explicite, un statut).
2. **L'absence est-elle nommée ailleurs ?** Un drapeau publié (`ALPACA_OK`), une
   note affichée, un refus franc. Un repli qui ne laisse aucune trace n'est pas
   un repli, c'est un effacement.
3. **Le repli est-il borné ?** Un fichier, une ligne, une clé essayée, un
   défaut documenté. S'il peut masquer une quantité non bornée de données
   manquantes, il bascule.

L'exemple fondateur du projet est un refus, pas une exemption :
`execution/risk_guard.py` refuse d'évaluer un risque faute de référence
utilisable (« 5 % de 0 »), publie `reference_unusable_snapshot` et le nomme dans
le journal. Un `except` qui aurait rendu `0.0` aurait produit exactement la
mesure fabriquée que ce garde-fou existe pour empêcher.

## Les quatre régimes tolérés

### 1. Absence publiée

L'opération échoue, et **autre chose nomme l'absence** : un drapeau, une note
affichée, un état rendu par l'appelant. Le lecteur ne peut pas confondre
« absent » et « présent et vide ».

- `config.py` :: `<module>` :: `except ImportError` :: le chargement de `.env` est optionnel, l'environnement suffit
- `utils/market_data.py` :: `<module>` :: `except ImportError` :: `httpx` est optionnel ; les sondes réseau annoncent l'absence d'elles-mêmes
- `scrapers/forex_factory.py` :: `<module>` :: `except ImportError` :: `httpx` optionnel, l'appelant le voit à l'usage
- `scrapers/forex_factory.py` :: `<module>` :: `except ImportError` :: `feedparser` optionnel, l'absence est annoncée par l'appelant
- `ai/news_analyzer.py` :: `<module>` :: `except ImportError` :: `httpx` optionnel, l'appelant le voit à l'usage
- `ai/news_analyzer.py` :: `<module>` :: `except Exception` :: client Groq optionnel, l'absence est rapportée par l'appelant
- `database/settings.py` :: `<module>` :: `except Exception` :: client Supabase optionnel, `supabase_client` a déjà nommé la cause
- `execution/order_executor.py` :: `<module>` :: `except Exception` :: SDK Alpaca absent ; `ALPACA_OK` le publie et `AlpacaSDKUnavailable` le nomme
- `tests/test_telegram_filters.py` :: `<module>` :: `except Exception` :: `python-telegram-bot` absent de l'environnement, les tests se sautent d'eux-mêmes
- `main.py` :: `risk_status` :: `except AlpacaSDKUnavailable` :: le repli est **affiché** ; la note dit la vraie cause (SDK absent, pas « pas de compte »)
- `main.py` :: `risk_status` :: `except BrokerCredentialsUnreadable` :: le repli est **affiché** ; la note dit « identifiants illisibles », jamais « aucun compte »
- `core/rag_loop.py` :: `_excerpt` :: `except (TypeError, ValueError)` :: un score illisible devient `n/a` **affiché** — la valeur ne se lit pas comme un nombre

### 2. Repli sans mesure

L'opération ne produit **aucune valeur** qu'un lecteur puisse prendre pour une
mesure : elle range un chemin, applique un défaut documenté, ajuste une
permission. Il n'y a rien à confondre, parce qu'il n'y a rien à lire.

- `core/secrets_audit.py` :: `iter_scan_files` :: `except ValueError` :: sans chemin relatif, le fichier est nommé par son chemin absolu
- `core/secrets_audit.py` :: `resolve_max_age_days` :: `except ValueError` :: un plafond d'âge illisible retombe sur le défaut documenté (`DEFAULT_MAX_AGE_DAYS`)
- `scripts/generate_secrets.py` :: `_write_env` :: `except OSError` :: permissions absentes sous Windows ; l'écriture du `.env` n'est pas une mesure

### 3. Variante essayée

L'échec est la branche **attendue** d'un essai : on tente une alternative, et
son échec n'est pas une panne mais l'information qui fait passer à la suivante.
Le refus arrive quand toutes échouent.

- `utils/encryption.py` :: `KeyRing.decrypt_with_version` :: `except InvalidToken` :: cette clé ne rouvre pas le jeton, la suivante est essayée ; si aucune ne rouvre, `EncryptionError` est levée
- `core/tree_publish.py` :: `apply_plan` :: `except FileNotFoundError` :: le fichier était déjà absent, c'est le résultat voulu, et l'élagage est compté juste après

### 4. Entrée écartée

Une entrée illisible est **écartée**, et ce qui fait foi est ce qui a été
réellement lu. Ce n'est pas une valeur fabriquée : c'est une entrée retirée du
périmètre, et le parcours continue sur le reste.

- `core/tree_publish.py` :: `collect` :: `except OSError` :: disparu entre le parcours et la lecture ; il n'y a plus rien à publier
- `core/tree_publish.py` :: `plan` :: `except OSError` :: illisible d'un côté ou de l'autre, donc on recopie — et c'est la copie qui nomme son échec
- `utils/market_data.py` :: `_closes_stooq` :: `except Exception` :: une ligne CSV illisible est sautée, les suivantes restent lues (voir les cas limites)
- `tests/test_android_identity.py` :: `NoTraceInTheRepositoryTest.tracked_texts` :: `except (UnicodeDecodeError, OSError)` :: un fichier non UTF-8 ou illisible est écarté du corpus de textes, le suivant est analysé
- `tests/test_kotlin_packages.py` :: `repository_texts` :: `except (UnicodeDecodeError, OSError)` :: idem : un fichier illisible n'entre pas dans le corpus, le suivant est analysé

## Les cas limites, dits

Trois exemptions frôlent la mesure fabriquée. Elles restent du bon côté, et la
raison de cette limite est écrite ici plutôt que tue — c'est le point où le
triage doit rester lisible.

- **`core/secrets_audit.py` :: `resolve_max_age_days`** — le défaut s'applique à
  un plafond illisible **et le code d'audit le dit** (`DEFAULT_MAX_AGE_DAYS` est
  documenté). Si ce défaut devenait permissif au point de faire passer un audit
  en échec pour un succès, l'exemption serait à retirer : un plafond qu'on ne
  sait pas lire ne doit pas se traduire par « aucun plafond ».
- **`utils/market_data.py` :: `_closes_stooq`** — seule une **ligne** CSV est
  sautée ; l'échec de la lecture entière n'est pas dans ce handler, il est
  **annoncé** par le `except` extérieur (`stooq closes fail …`) qui rend une
  liste vide visible comme un échec. La limite à surveiller : la série lue
  compte des points en moins sans que personne ne le sache. Si la consommation
  devenait sensible au nombre de chandelles, il faudrait publier ce compte.
- **`tests/test_android_identity.py` / `test_kotlin_packages.py`** — les fichiers
  illisibles sont écartés du corpus de textes. Ces tests ne prétendent pas avoir
  lu **tout** le dépôt : ils éprouvent les fichiers qu'ils ont lus. Un test qui
  passerait au vert parce qu'il n'a rien lu serait le défaut ; c'est pourquoi le
  corpus des `except`, lui, **refuse** un fichier illisible au lieu de l'écarter.

## Ce qui n'est jamais toléré

- **Une valeur neutre à la place d'une mesure** : `0`, `[]`, `0.0`, `""` rendus
  après un échec, sans que rien ne distingue le repli de la mesure. Le compte
  qu'on n'a pas interrogé ne vaut pas zéro.
- **Un repli qui éteint l'alarme** : un `except` qui empêche le refus, la note
  ou le journal de se produire. Le repli peut *ajouter* une information, jamais
  en retirer une.
- **Une absence non bornée** : un handler qui peut masquer n'importe quelle
  quantité de données manquantes (tout un fichier, toute une série) sans le dire.
- **Un silence sans raison écrite** : le marqueur vide ou trop court ne vaut pas
  mieux que le silence, et une raison absente de l'inventaire n'existe pas.

## Ajouter ou retirer une exemption

1. écrire le repli et déclarer sa raison dans le code — `# sans signal : <raison>`
   sur la ligne du `except` ou dans son corps ;
2. enregistrer l'exemption — `python scripts/check_silent_handlers.py --update` ;
3. la **trier ici**, dans l'un des quatre régimes, avec la même ligne
   `\`fichier\` :: \`symbole\` :: \`clause\` :: pourquoi` et une raison en clair ;
4. lancer `tests.test_silent_handlers` : une exemption non triée, ou une ligne de
   triage sans exemption, fait échouer le test.

Retirer une exemption suit le même chemin à l'envers : corriger le code (nommer
l'échec, rendre l'absence distinguable), `--update`, et supprimer sa ligne ici.
