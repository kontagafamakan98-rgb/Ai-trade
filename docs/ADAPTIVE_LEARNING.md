# Apprentissage adaptatif des poids (opt-in)

Le moteur de décision combine trois sous-notes pour produire une probabilité :

```
final_prob = ta_weight * ta_score + sentiment_weight * sentiment_score + macro_weight * macro_factor
```

Historiquement, ces trois poids étaient recalibrés par une **heuristique codée en
dur** : `+0.05` / `-0.05` sur un facteur selon une action catégorielle
(`boost_macro_weight`, `stricter_threshold`…), et un retour à l'équilibre
`(0.40, 0.30, 0.30)` après chaque gain.

Depuis ce chantier, les poids peuvent être **appris par descente de gradient**
sur les trades réellement réglés. C'est **désactivé par défaut**.

## Activer l'apprentissage

```bash
ADAPTIVE_GD_ENABLED="true"      # défaut : "false"
ADAPTIVE_GD_MIN_SAMPLES="20"    # trades réglés minimum par actif (défaut : 20)
```

Tant que `ADAPTIVE_GD_ENABLED` est inactif, **rien ne change** dans le
comportement nominal : la branche de descente de gradient retourne immédiatement
et aucune lecture supplémentaire de la base n'est effectuée.

## Sous-notes persistées (prérequis)

`trade_post_mortems.ta_score` / `sentiment_score` / `macro_score` étaient écrits
en dur à `0.5`. Or ces trois colonnes **sont** les variables explicatives du
modèle : avec une valeur constante, le gradient des poids par rapport aux
facteurs est identiquement nul — on n'entraînerait que le biais, c'est-à-dire du
vide.

`ai/decision_engine.py` renvoie désormais les sous-notes réellement utilisées
dans `final_prob` (`ta_score`, `sentiment_score`, `macro_score`, bornées à
`[0, 1]`), elles traversent `normalize_signal` (qui préserve les clés inconnues)
puis sont stockées telles quelles par
`core/adaptive_learning.py::record_trade_settlement_and_learn`.

Un signal produit par un chemin qui ne fournit pas ces clés (signal ancien,
signal de consensus recomposé) retombe sur la valeur **neutre `0.5`** : la ligne
reste insérable, elle porte simplement moins d'information.

## Fonctionnement

1. Le trade est réglé (`won` / `lost`) par `workers/performance_tracker.py`.
2. Le post-mortem et les sous-notes réelles sont enregistrés.
3. Les poids heuristiques sont calculés **comme avant** (les gérer ainsi garde un
   repli toujours disponible).
4. Si `ADAPTIVE_GD_ENABLED` est actif, l'historique réglé de l'actif
   (`HISTORY_LIMIT`, les 200 derniers post-mortems) est chargé et une descente de
   gradient apprend `w = softmax(θ)` — la contrainte de somme 1 est donc
   **structurelle**, jamais violée par l'optimisation. Le trade qu'on est en train
   de régler est **exclu** de cet historique (section suivante).
5. Le résultat remplace les poids heuristiques ; il est persisté dans
   `adaptive_model_weights`, lu ensuite par `get_adaptive_parameters()`. Le
   moteur de décision n'a donc rien à changer.

Le retour de la fonction expose `weight_update` (`"gradient_descent"` ou
`"heuristic"`) et `gradient_descent` (échantillons utilisés, poids appris), ce
qui rend l'apprentissage observable.

## Un signal se règle une fois

Le règlement est **idempotent par identité** (`signal_id`) : un second règlement du
même signal n'écrit rien et ne compte rien. Le doublon n'est pas théorique —
`POST /learning/feedback` peut être rappelé (retry d'un client, appel manuel
répété), et le tracker peut repasser sur un signal dont le verdict n'est pas allé
au bout. Sans ce garde-fou, le second règlement :

* ajoute une **ligne de post-mortem en double**, qui comptera deux fois dans
  l'historique de la descente de gradient ;
* incrémente une seconde fois `total_trades`, `consecutive_losses` et le taux de
  réussite — la mémoire du modèle, que rien ne permettrait plus de démêler ensuite
  (aucune trace ne dit *quels* règlements ont été comptés) ;
* réécrit la note du savoir pour dire la même chose qu'avant.

Le contrôle est une lecture (`trade_post_mortems.signal_id`) faite **avant**
l'insertion et avant tout compteur, et le retour le dit : `status:
"already_settled"`, `weight_update: "unchanged"`, plus `duplicate_of` (le premier
règlement : son issue, sa leçon, sa date). Le diagnostic `post_mortem` est, lui,
recalculé — c'est une fonction pure du même signal, donc la leçon annoncée reste
vraie, et l'appelant qui n'affiche qu'elle (le tracker) continue d'afficher quelque
chose de juste en sachant que rien n'a été appris.

Le doublon se reconnaît à l'**identité seule**, jamais à l'issue : un rejeu qui
**contredit** le premier verdict (« perdu » alors que « gagné » est enregistré) est
un rejeu quand même. Reconnaître le doublon par l'issue rendrait la mémoire du
modèle dépendante de qui parle en dernier — le tracker repasse tant que son verdict
n'est pas allé au bout — et ferait d'un rejeu une correction silencieuse, alors que
c'est la même ligne de `trade_post_mortems` qui serait comptée deux fois. Le premier
verdict enregistré fait donc foi (`duplicate_of` dit lequel et `outcome` celui qu'on
proposait) ; une correction réelle est un geste explicite en base, pas un
enregistrement.

Deux cas sont assumés, et écrits plutôt que devinés :

* un signal **sans identifiant** est réglé à chaque fois. Le repli `sig_0` est
  partagé par tous les règlements sans identifiant : s'en servir pour dire « déjà
  réglé » ferait du premier d'entre eux le doublon de tous les suivants, et ces
  signaux ne seraient plus jamais appris. Mieux vaut compter deux fois un signal
  qu'on ne sait pas nommer que de n'apprendre d'aucun ;
* si la **lecture de contrôle échoue**, le règlement est appliqué quand même — et
  journalisé en avertissement. Perdre un règlement réel parce qu'une lecture a
  hoqueté coûterait plus que le doublon qu'elle évite : un doublon se répare (une
  ligne de trop se supprime, le compteur se corrige), un trade jamais appris ne se
  voit nulle part.

Les lignes en double déjà présentes (données antérieures à ce garde-fou, écriture
manuelle) restent écartées de l'entraînement : la fenêtre qui décide des poids rejette
**toutes** les lignes du signal en cours de règlement (section suivante).

## Le trade réglé n'entre pas dans son propre entraînement

L'ordre des étapes ci-dessus est celui du code : le post-mortem est **inséré**
(étape 2) avant que l'historique ne soit **relu** (étape 4). Sans précaution, la
ligne que le règlement vient d'écrire serait donc le premier échantillon du fit qui
décide des poids écrits à l'étape 5 : le modèle ajusterait son adaptation sur
l'issue qu'elle est censée expliquer, et ce trade-là pèserait d'autant plus lourd
que l'historique est court. C'est la **cible qui fuit dans ses propres variables
explicatives**, sur un échantillon où une ligne compte.

La décision est donc l'**exclusion**, et elle est écrite là où elle s'applique
(`core/adaptive_learning.py`, `record_trade_settlement_and_learn` → `_fetch_settled_post_mortems`) :

* l'exclusion se fait **par identifiant de signal** (`signal_id`), jamais par date :
  `created_at` est posé par le serveur, deux règlements peuvent tomber dans la même
  seconde, et « la ligne la plus récente » retirerait la ligne de quelqu'un d'autre.
  L'identité est fournie par l'appelant : `workers/performance_tracker.py` transmet
  `pending_signals.id` (`{**signal, "id": sig_id}` — le payload du moteur ne la
  porte pas), et `POST /learning/feedback` transmet le `signal_id` de la requête ;
* **toutes** les lignes de cet identifiant s'en vont : un signal réglé deux fois
  (retour `/learning/feedback` rejoué, rattrapage du tracker) a deux lignes pour
  **un** trade, qui ne doit pas être compté deux fois ;
* le seuil `ADAPTIVE_GD_MIN_SAMPLES` se compte **après** l'exclusion, donc un trade
  ne peut pas s'accorder l'entraînement à lui tout seul. Conséquence assumée : avec
  `min_samples = 20` et 19 trades antérieurs, le règlement garde l'heuristique et la
  descente de gradient se déclenche au règlement **suivant** ;
* la fenêtre ne se raccourcit pas pour autant — `HISTORY_LIMIT + 1` lignes sont lues,
  donc `HISTORY_LIMIT` trades antérieurs quand l'identifiant est bien dans le
  tableau. S'il n'y est pas (règlement ancien, insertion ratée), l'exclusion ne
  retire rien : la fenêtre reste celle des `HISTORY_LIMIT` derniers règlements ;
* un signal **sans identifiant** n'est pas exclu : le repli `sig_0` n'est pas une
  identité (tous les règlements sans identifiant le porteraient), donc exclure sur
  lui retirerait les lignes d'un autre trade. Mieux vaut une fenêtre qui contient ce
  trade qu'une fenêtre qui écarte celle d'un autre.

Le retour du règlement le dit : `gradient_descent.excluded_signal_id` porte
l'identifiant tenu hors de l'échantillon (`null` quand le signal n'en avait pas), et
`gradient_descent.samples` est le nombre de trades **antérieurs** réellement
utilisés — c'est ce chiffre qui est comparé au seuil. La journalisation aussi :
`Descente de gradient ignorée pour EURUSD : 19 trades réglés hors sig_20 < 20 requis`.

## Garde-fous

| Risque | Mesure |
|---|---|
| Sur-apprentissage sur peu d'exemples | Seuil `ADAPTIVE_GD_MIN_SAMPLES` (défaut 20 par actif) ; en dessous, l'heuristique est conservée |
| Entraînement sur sa propre issue | Le trade réglé est exclu de l'historique qui décide des poids écrits pour son règlement (par identifiant) ; `samples` et le seuil se comptent hors de lui |
| Règlement rejoué (retry, rattrapage) | Un signal se règle une fois : le second règlement n'écrit ni ligne ni compteur, et le dit (`status: "already_settled"`) |
| Régularisation | L2 vers le **prior d'équilibre** `(0.40, 0.30, 0.30)`, exprimée dans l'espace des logits |
| Facteur dominant | Projection sur le simplexe **borné** : chaque poids reste dans `[0.10, 0.60]`, somme = 1 |
| Panne de l'optimisation | Tout échec est journalisé et retombe sur l'heuristique ; le règlement du trade n'échoue jamais |
| Comportement en production | Désactivé par défaut : aucune modification tant que le flag n'est pas levé |

## Mise en service progressive

1. Laisser `ADAPTIVE_GD_ENABLED=false` le temps d'accumuler des post-mortems
   porteurs de vraies sous-notes (les anciennes lignes à `0.5` n'apportent rien).
2. Activer le flag sur un actif liquide, comparer pendant quelques semaines le
   **Brier score** des poids appris contre celui des poids heuristiques.
3. N'étendre qu'après confirmation hors échantillon.

L'ancien comportement reste lisible dans l'historique Git et dans
`record_trade_settlement_and_learn` : le repli n'est pas un vestige, il est exercé
à chaque fois que le flag est inactif ou l'historique trop court.

## Développement

```bash
python -m unittest tests.test_learning_gd        # dont SelfTrainingExclusionTest
python -m unittest tests.test_gradient_descent   # gradient analytique vs numérique
```

`core/adaptive_learning.learn_weights_from_history` est **pur** (aucun accès
réseau) : c'est le point d'entrée à utiliser pour tout test ou toute simulation.
Elle ne fait aucune exclusion elle-même, et ne le peut pas : elle ignore quel trade
est en train d'être réglé. La fenêtre qu'on lui donne est celle de l'appelant, et
c'est `_fetch_settled_post_mortems` qui garantit qu'elle ne contient pas le trade en
cours. Un contrat le vérifie sans chiffre attendu (`SelfTrainingExclusionTest`) :
les poids appris sont comparés à ceux du fit **sans** ce trade.
