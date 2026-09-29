-- Migration 011 : DDL des tables « historiques » — users, insights, pending_signals.
--
-- À exécuter dans Supabase → SQL Editor, APRÈS la migration 010. Idempotente.
--
-- Ces trois tables existaient **avant** ce dossier de migrations : elles ont été
-- créées à la main, et la migration 007 se contente de dire
-- `alter table if exists users enable row level security` — elle suppose donc
-- qu'elles sont là. Résultat : le schéma du projet n'était reproductible nulle
-- part, et une base neuve ne pouvait pas démarrer.
--
-- Les colonnes ci-dessous sont **celles que le code utilise réellement**, pas un
-- schéma idéal. Chacune est justifiée par la requête qui la lit ou l'écrit (les
-- fichiers sont cités), et `tests/test_core_tables_migration.py` relit le code
-- pour vérifier qu'aucune colonne d'une requête ne manque ici — et, dans l'autre
-- sens, que ce fichier n'invente pas de colonne que personne n'utilise.
--
-- Deux règles pour pouvoir l'appliquer à une base **déjà en service** :
--
--   1. `create table if not exists` + `alter table ... add column if not exists` :
--      sur une base existante la migration **n'ajoute que ce qui manque** ;
--   2. elle ne change le **type** d'aucune colonne existante. `alter column ...
--      type` réécrit la table et verrouille les écritures : c'est une opération
--      à faire à part, en connaissance de cause, pas en passant dans une
--      migration que l'on relance.

-- --------------------------------------------------------------------------- #
-- 1. `users` — un utilisateur Telegram ayant fait `/start`
-- --------------------------------------------------------------------------- #
-- Écrite par `/start` (`main.py`) : id, username, first_name, telegram_chat_id,
-- paper_mode. Lus par `database/preferences.py`, `workers/auto_loop.py` et
-- `api/webhook.py` (qui sélectionnent `id, telegram_chat_id` filtrés par
-- `paper_mode`), et par `notifications/notify.py` (chat_id typé `int`).
create table if not exists users (
    id text primary key,                          -- identifiant Telegram, en texte (str(user.id))
    username text,                                -- @pseudo, absent si l'utilisateur n'en a pas
    first_name text,
    telegram_chat_id bigint,                      -- chat privé où notifier (int64 côté Telegram)
    paper_mode boolean not null default true,     -- seul mode supporté par ce projet
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- Colonnes manquantes sur une base existante (créations antérieures au projet).
alter table if exists users add column if not exists username text;
alter table if exists users add column if not exists first_name text;
alter table if exists users add column if not exists telegram_chat_id bigint;
alter table if exists users add column if not exists paper_mode boolean not null default true;
alter table if exists users add column if not exists created_at timestamptz not null default now();
alter table if exists users add column if not exists updated_at timestamptz not null default now();

-- Le seul prédicat du code est `paper_mode = true` (`/start` met tout le monde
-- dans ce mode) : un index partiel suffit, et ne mémorise pas les lignes
-- inutiles. La table est minuscule (un utilisateur = une ligne), on n'indexe
-- rien d'autre : `telegram_chat_id` n'est jamais un critère de recherche.
create index if not exists idx_users_paper_mode
    on users (paper_mode) where paper_mode;

drop trigger if exists trg_users_updated_at on users;
create trigger trg_users_updated_at
    before update on users
    for each row execute function update_updated_at();

-- --------------------------------------------------------------------------- #
-- 2. `insights` — le fil collectif (news, sentiment, recherche web, canaux)
-- --------------------------------------------------------------------------- #
-- Écrite par `insert_insight()` (`database/supabase_client.py`), depuis
-- `scrapers/news_geo.py` (types `geopolitical`, `sentiment`, `web_research`) et
-- `scrapers/telegram_channel.py` (type `telegram_channel`). Les valeurs de
-- `data` sont celles des appelants : score/class/normalized (Fear & Greed),
-- published (flux RSS), channel (canal), watchlist (recherche web).
--
-- Lue par `get_recent_insights()` (ordre `created_at desc`, filtre `asset`),
-- consommée par `ai/decision_engine.py` (`type`, `title`, `data.normalized`) et
-- purgée par `prune_insights()` (comptage `id`, coupure sur `created_at`).
-- `id` est un compteur : rien ne le manipule côté code — il n'est lu que pour
-- compter —, et cette table est un journal en ajout seul.
create table if not exists insights (
    id bigserial primary key,
    type text not null,                              -- geopolitical | sentiment | web_research | telegram_channel
    asset text not null default 'GLOBAL',            -- actif concerné, 'GLOBAL' pour le collectif
    title text,
    summary text,                                    -- texte borné par les appelants (1200 car.)
    source text,                                     -- URL du flux, nom du fournisseur LLM…
    url text,                                        -- lien de l'article, quand il y en a un
    confidence double precision,                     -- confiance de la source (0..1)
    data jsonb not null default '{}'::jsonb,         -- charge utile propre au type (voir ci-dessus)
    created_at timestamptz not null default now(),   -- fourni par `insert_insight` (série ISO)
    updated_at timestamptz not null default now()
);

alter table if exists insights add column if not exists type text not null default 'geopolitical';
alter table if exists insights add column if not exists asset text not null default 'GLOBAL';
alter table if exists insights add column if not exists title text;
alter table if exists insights add column if not exists summary text;
alter table if exists insights add column if not exists source text;
alter table if exists insights add column if not exists url text;
alter table if exists insights add column if not exists confidence double precision;
alter table if exists insights add column if not exists data jsonb not null default '{}'::jsonb;
alter table if exists insights add column if not exists created_at timestamptz not null default now();
alter table if exists insights add column if not exists updated_at timestamptz not null default now();

-- Le fil « 20 derniers insights » (le plus fréquent) et la purge rétention :
create index if not exists idx_insights_created_at
    on insights (created_at desc);

-- `get_recent_insights(asset=…)` filtre **et** trie : un index composite sert les
-- deux, là où un index sur `asset` seul laisserait un tri à faire.
create index if not exists idx_insights_asset_created_at
    on insights (asset, created_at desc);

-- Pas d'index sur `type` : le filtrage par type se fait **en Python**
-- (`_score_geo`, `_score_sentiment`), jamais en SQL.

drop trigger if exists trg_insights_updated_at on insights;
create trigger trg_insights_updated_at
    before update on insights
    for each row execute function update_updated_at();

-- --------------------------------------------------------------------------- #
-- 3. `pending_signals` — un signal proposé, et son devenir
-- --------------------------------------------------------------------------- #
-- Écrite par `create_pending_signal()` (`database/supabase_client.py`) :
-- `user_id`, `signal`, `status = 'pending'`. Le reste est mis à jour ensuite :
--
--   * `validated_at` et `execution_result` par `update_signal_status()` — c'est
--     l'horodatage de la décision humaine (boutons ✅ / ❌ de `main.py`) ;
--   * `status = 'won' | 'lost'` + `execution_result.closed_price` par
--     `workers/performance_tracker.py`, quand le TP ou le SL est touché ;
--   * `"id, signal, created_at, user_id"` par l'anti-spam
--     (`workers/signal_guard.py`), filtré sur `created_at`.
--
-- Statuts écrits par le code : `pending`, `executed`, `rejected`, `won`, `lost`.
-- Volontairement **sans contrainte `check`** : `update_signal_status()` accepte
-- n'importe quelle chaîne, et une contrainte ajoutée à une table déjà remplie
-- échoue sur les lignes historiques — elle refuserait alors l'écriture d'un
-- statut légitime au milieu d'un cycle. Le vocabulaire est verrouillé côté code
-- (et par le test de contrat), pas par le schéma.
--
-- `id` est un uuid : il voyage dans le `callback_data` Telegram (`approve:<id>`,
-- 44 octets sur les 64 autorisés) et il est comparé comme une **chaîne** par
-- `main.py` — un uuid est donc le bon type, pas un compteur.
create table if not exists pending_signals (
    id uuid primary key default gen_random_uuid(),
    user_id text not null,
    signal jsonb not null default '{}'::jsonb,
    status text not null default 'pending',
    execution_result jsonb,
    created_at timestamptz not null default now(),
    validated_at timestamptz,                        -- décision humaine (✅ ou ❌)
    updated_at timestamptz not null default now()
);

alter table if exists pending_signals add column if not exists user_id text;
alter table if exists pending_signals add column if not exists signal jsonb not null default '{}'::jsonb;
alter table if exists pending_signals add column if not exists status text not null default 'pending';
alter table if exists pending_signals add column if not exists execution_result jsonb;
alter table if exists pending_signals add column if not exists created_at timestamptz not null default now();
alter table if exists pending_signals add column if not exists validated_at timestamptz;
alter table if exists pending_signals add column if not exists updated_at timestamptz not null default now();

-- Clé étrangère vers `users`, mais **`not valid`** : sur une base en service, des
-- lignes historiques peuvent référencer un utilisateur jamais enregistré (les
-- signaux sont créés par l'auto-loop pour tous les utilisateurs actifs, mais un
-- `/start` a pu être manqué). `not valid` fait respecter la contrainte aux
-- **nouvelles** lignes sans exiger la validation des anciennes — une migration
-- ne doit pas supprimer ni refuser de l'historique pour ranger le schéma.
do $$
begin
    if not exists (
        select 1 from pg_constraint where conname = 'pending_signals_user_id_fkey'
    ) then
        alter table pending_signals
            add constraint pending_signals_user_id_fkey
            foreign key (user_id) references users (id) on delete cascade not valid;
    end if;
end $$;

-- Nombre de positions ouvertes d'un utilisateur (`execution/risk_guard.py`) et
-- toutes les lectures par utilisateur (rapports, auto-loop) :
create index if not exists idx_pending_signals_user_status
    on pending_signals (user_id, status);

-- Positions ouvertes à surveiller, tous utilisateurs confondus
-- (`workers/performance_tracker.py`), et trades réglés (`execution/self_review.py`) :
create index if not exists idx_pending_signals_status
    on pending_signals (status);

-- Anti-spam : « y a-t-il eu un signal récent ? » (`workers/signal_guard.py`) —
-- filtre et tri sur `created_at`, donc l'index porte les deux :
create index if not exists idx_pending_signals_created_at
    on pending_signals (created_at desc);

-- Rapports de performance et leçons (`reports/performance_report.py`,
-- `execution/self_review.py`) : triés sur `validated_at`, souvent avec peu de
-- lignes réglées — l'index évite le tri complet à chaque `/report`.
create index if not exists idx_pending_signals_validated_at
    on pending_signals (validated_at desc);

drop trigger if exists trg_pending_signals_updated_at on pending_signals;
create trigger trg_pending_signals_updated_at
    before update on pending_signals
    for each row execute function update_updated_at();

-- --------------------------------------------------------------------------- #
-- 4. RLS « deny by default »
-- --------------------------------------------------------------------------- #
-- La migration 007 l'a déjà fait pour ces trois tables ; on le refait ici parce
-- que ce fichier est aussi ce qui les **crée** sur une base neuve : sans ces
-- lignes, une base installée à partir des migrations seules aurait trois tables
-- accessibles en lecture/écriture avec la clé anon. Aucune policy n'est créée :
-- `anon` / `authenticated` n'ont aucun accès, `service_role` (le backend)
-- contourne la RLS.
alter table if exists users           enable row level security;
alter table if exists insights        enable row level security;
alter table if exists pending_signals enable row level security;

revoke all on users           from anon, authenticated;
revoke all on insights        from anon, authenticated;
revoke all on pending_signals from anon, authenticated;

-- Les index de `id` par défaut (PK) sont conservés : c'est ce que PostgREST
-- utilise pour `.eq("id", …)` (les boutons ✅ / ❌).
