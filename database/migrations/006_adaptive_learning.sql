-- Migration 006 : moteur d'apprentissage — post-mortems de trades et poids adaptatifs.
--
-- À exécuter dans Supabase → SQL Editor, APRÈS la migration 005. Idempotente :
-- elle peut être relancée sans risque, y compris sur une base déjà en service.
--
-- Les colonnes ci-dessous sont **celles que le code écrit ou lit**, pas un
-- schéma idéal. Tout vient de `core/adaptive_learning.py` :
--
--   * `trade_post_mortems` est écrite par `record_trade_settlement_and_learn()`
--     (le post-mortem d'un trade réglé) et relue par
--     `_fetch_settled_post_mortems()` — qui alimente la descente de gradient —,
--     `_refresh_knowledge_base_lessons()` et `get_learning_summary()` ;
--   * `adaptive_model_weights` est lue par `get_adaptive_parameters()` (poids par
--     actif, pertes consécutives, taux de réussite) et écrite par le même
--     `record_trade_settlement_and_learn()`.
--
-- `tests/test_engine_tables_migration.py` **relit les sources** pour vérifier
-- qu'aucune colonne d'une requête ne manque ici — et que ce fichier ne déclare
-- aucune colonne que personne n'utilise. Les deux propriétés d'application
-- décrites dans la migration 011 valent ici aussi : `create table if not exists`
-- + `add column if not exists` (les colonnes de réparation ne forcent pas
-- `not null`, la table pouvant déjà contenir des lignes), et aucun
-- `alter column … type`.

-- --------------------------------------------------------------------------- #
-- 1. `trade_post_mortems` — le diagnostic d'un trade réglé (ajout seul)
-- --------------------------------------------------------------------------- #
-- Une ligne par trade réglé, jamais modifiée : le code ne fait qu'y **insérer**
-- puis la relire triée sur `created_at`. Il n'y a donc pas d'`updated_at` ici
-- (l'ajouter ne servirait qu'à entretenir un trigger inutile).
create table if not exists trade_post_mortems (
    id bigserial primary key,                     -- clé technique : jamais lue ni comparée par le code
    signal_id text not null,                      -- signal réglé (`sig_0` quand l'identifiant manque)
    asset text not null,                          -- actif concerné
    direction text not null,                      -- BUY | SELL, tel que porté par le signal
    outcome text not null,                        -- won | lost (voir la note « vocabulaire » plus bas)
    entry_price numeric,                          -- prix d'entrée du signal
    exit_price numeric,                           -- prix de sortie constaté (absent si non fourni)
    pnl numeric,                                  -- résultat en devise du compte (0.0 si absent du signal)
    ta_score numeric,                             -- sous-note technique réellement utilisée dans le calcul du signal
    sentiment_score numeric,                      -- sous-note sentiment …
    macro_score numeric,                          -- … sous-note macro (les trois alimentent le gradient)
    confidence numeric,                           -- confiance du signal au moment de l'émission
    error_type text,                              -- diagnostic du post-mortem (macro_divergence, tight_sl…)
    learned_lesson text not null,                 -- leçon rédigée par le post-mortem, réinjectée dans la base de connaissances
    created_at timestamptz not null default now()
);

alter table if exists trade_post_mortems add column if not exists signal_id text;
alter table if exists trade_post_mortems add column if not exists asset text;
alter table if exists trade_post_mortems add column if not exists direction text;
alter table if exists trade_post_mortems add column if not exists outcome text;
alter table if exists trade_post_mortems add column if not exists entry_price numeric;
alter table if exists trade_post_mortems add column if not exists exit_price numeric;
alter table if exists trade_post_mortems add column if not exists pnl numeric;
alter table if exists trade_post_mortems add column if not exists ta_score numeric;
alter table if exists trade_post_mortems add column if not exists sentiment_score numeric;
alter table if exists trade_post_mortems add column if not exists macro_score numeric;
alter table if exists trade_post_mortems add column if not exists confidence numeric;
alter table if exists trade_post_mortems add column if not exists error_type text;
alter table if exists trade_post_mortems add column if not exists learned_lesson text;
alter table if exists trade_post_mortems add column if not exists created_at timestamptz not null default now();

-- `_fetch_settled_post_mortems(asset)` filtre **et** trie (`asset`, puis
-- `created_at` décroissant, avec une limite) : l'index composite sert les deux.
-- Il remplace l'ancien index sur `asset` seul, qui devient redondant — la
-- première colonne d'un index composite sert déjà les recherches sur `asset`.
drop index if exists idx_trade_post_mortems_asset;
create index if not exists idx_trade_post_mortems_asset_created_at
    on trade_post_mortems (asset, created_at desc);

-- Les deux lectures globales (`_refresh_knowledge_base_lessons()` : les 10
-- derniers post-mortems ; `get_learning_summary()` : les 20 derniers) ne filtrent
-- pas et trient sur `created_at` décroissant.
create index if not exists idx_trade_post_mortems_created_at
    on trade_post_mortems (created_at desc);

-- Pas d'index sur `outcome` : le tri entre gagnants et perdants se fait **en
-- Python** (`sum(1 for p in post_mortems if p.get("outcome") == "lost")`), jamais
-- en SQL. Même raison pour `error_type`, agrégé côté application.

-- --------------------------------------------------------------------------- #
-- 2. `adaptive_model_weights` — un profil de poids par actif
-- --------------------------------------------------------------------------- #
-- Une ligne par actif (la clé primaire est l'actif lui-même : `get_adaptive_parameters()`
-- fait un `select *` filtré sur `asset`). Les valeurs par défaut sont l'équilibre
-- du code (`ta 0.40 / sentiment 0.30 / macro 0.30`, multiplicateurs ATR 1.5 et
-- 3.0, 50 % de réussite) : une ligne créée sans valeur explicite se comporte
-- comme l'actif jamais appris, au lieu d'être lue comme zéro.
create table if not exists adaptive_model_weights (
    asset text primary key,
    ta_weight numeric not null default 0.40,          -- poids du facteur technique
    sentiment_weight numeric not null default 0.30,   -- poids du facteur sentiment
    macro_weight numeric not null default 0.30,       -- poids du facteur macro
    sl_atr_multiplier numeric not null default 1.5,   -- multiplicateur ATR du stop loss
    tp_atr_multiplier numeric not null default 3.0,   -- multiplicateur ATR du take profit
    consecutive_losses integer not null default 0,    -- pertes consécutives (pilote le seuil de confiance)
    win_rate_pct numeric not null default 50.0,       -- taux de réussite en pourcentage
    total_trades integer not null default 0,          -- nombre de trades réglés
    updated_at timestamptz not null default now()     -- réécrit à chaque apprentissage
);

alter table if exists adaptive_model_weights add column if not exists ta_weight numeric not null default 0.40;
alter table if exists adaptive_model_weights add column if not exists sentiment_weight numeric not null default 0.30;
alter table if exists adaptive_model_weights add column if not exists macro_weight numeric not null default 0.30;
alter table if exists adaptive_model_weights add column if not exists sl_atr_multiplier numeric not null default 1.5;
alter table if exists adaptive_model_weights add column if not exists tp_atr_multiplier numeric not null default 3.0;
alter table if exists adaptive_model_weights add column if not exists consecutive_losses integer not null default 0;
alter table if exists adaptive_model_weights add column if not exists win_rate_pct numeric not null default 50.0;
alter table if exists adaptive_model_weights add column if not exists total_trades integer not null default 0;
alter table if exists adaptive_model_weights add column if not exists updated_at timestamptz not null default now();

-- La colonne `min_confidence_threshold`, créée par une version antérieure de ce
-- fichier, est retirée : **aucun chemin de code ne l'écrit ni ne la lit**. Le
-- seuil réel est calculé dans `core/adaptive_learning.py` à partir des pertes
-- consécutives et du taux de réussite (0.55 / 0.63 / 0.68 selon l'état), et il
-- n'est donc pas réglable en base. Une colonne morte est pire qu'une colonne
-- absente : elle laisse croire qu'on règle quelque chose qui n'est jamais lu.
-- Le `if exists` la rend sans effet sur une base neuve.
alter table if exists adaptive_model_weights drop column if exists min_confidence_threshold;

drop trigger if exists trg_adaptive_model_weights_updated_at on adaptive_model_weights;
create trigger trg_adaptive_model_weights_updated_at
    before update on adaptive_model_weights
    for each row execute function update_updated_at();

-- --------------------------------------------------------------------------- #
-- 3. RLS « deny by default »
-- --------------------------------------------------------------------------- #
-- Déjà posée par la migration 007, répétée ici parce que ce fichier est aussi ce
-- qui **crée** ces deux tables sur une base neuve. Aucune policy : sans elle,
-- `anon` / `authenticated` n'ont aucun accès, et `service_role` (le backend)
-- contourne la RLS.
alter table if exists trade_post_mortems enable row level security;
alter table if exists adaptive_model_weights enable row level security;

revoke all on trade_post_mortems from anon, authenticated;
revoke all on adaptive_model_weights from anon, authenticated;

-- --------------------------------------------------------------------------- #
-- 4. Vocabulaire verrouillé côté code, pas par le schéma
-- --------------------------------------------------------------------------- #
-- `outcome` porte `won` ou `lost`, et `error_type` l'un des diagnostics de
-- `analyze_trade_error()` (`macro_divergence`, `overbought_entry`, `tight_sl`,
-- `high_volatility`). Volontairement **sans contrainte `check`**, comme
-- `pending_signals.status` (migration 011) : `record_trade_settlement_and_learn()`
-- accepte la chaîne qu'on lui donne, et une contrainte ajoutée à une table déjà
-- remplie échouerait sur les lignes historiques — elle refuserait alors un
-- résultat légitime au milieu d'un cycle. Le vocabulaire est verrouillé par le
-- test de contrat, pas par le schéma.
