-- Migration 005 : tables du calendrier économique et du journal macro.
--
-- À exécuter dans Supabase → SQL Editor, APRÈS la migration 004. Idempotente :
-- elle peut être relancée sans risque, y compris sur une base déjà en service.
--
-- Les colonnes ci-dessous sont **celles que le code écrit ou lit**, pas un
-- schéma idéal. `economic_events` est écrite par `upsert_economic_events()`
-- (`database/supabase_client.py`) avec le dictionnaire construit par
-- `scrapers/forex_factory.py`, relue par `get_economic_events()` et consommée
-- par `core/macro_engine.py` et `execution/news_risk_guard.py`.
-- `macro_bias_logs` n'est écrite que par `log_macro_decision()`.
-- `tests/test_engine_tables_migration.py` **relit les sources** pour vérifier
-- qu'aucune colonne d'une requête ne manque ici — et, dans l'autre sens, que ce
-- fichier ne déclare aucune colonne que personne n'utilise.
--
-- Comme la migration 011, ce fichier doit pouvoir être appliqué à une base
-- **déjà en service** :
--
--   1. `create table if not exists` + `add column if not exists` : sur une base
--      existante la migration n'ajoute que ce qui manque (les colonnes de
--      réparation ne forcent pas `not null` : la table peut déjà contenir des
--      lignes, et un `not null` sans défaut les refuserait) ;
--   2. elle ne change le **type** d'aucune colonne existante : un
--      `alter column … type` réécrit la table et verrouille les écritures, c'est
--      une opération à faire à part, en connaissance de cause.

-- --------------------------------------------------------------------------- #
-- 1. `economic_events` — une ligne du calendrier économique (Forex Factory)
-- --------------------------------------------------------------------------- #
create table if not exists economic_events (
    id text primary key,                          -- `ff_<md5>` : identifiant déterministe calculé par le scraper (on_conflict)
    event_id text,                                -- identifiant de la source, conservé tel quel
    title text not null,                          -- intitulé de l'événement (« Non-Farm Payrolls »)
    country text,                                 -- pays de publication (« US »)
    currency text not null,                       -- devise concernée (« USD »)
    event_date timestamptz not null,              -- horodatage de publication (ISO, UTC)
    impact text not null,                         -- High | Medium | Low | Non-Economic
    forecast text,                                -- consigne telle qu'affichée (« 180K », « 2.9% ») …
    previous text,
    actual text,
    forecast_num numeric,                         -- … et sa valeur numérique (`parse_numeric_value`)
    previous_num numeric,
    actual_num numeric,
    raw_data jsonb,                               -- charge brute de la source, pour ne rien perdre du détail
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()  -- réécrit par `upsert_economic_events()` à chaque réupsert
);

alter table if exists economic_events add column if not exists event_id text;
alter table if exists economic_events add column if not exists title text;
alter table if exists economic_events add column if not exists country text;
alter table if exists economic_events add column if not exists currency text;
alter table if exists economic_events add column if not exists event_date timestamptz;
alter table if exists economic_events add column if not exists impact text;
alter table if exists economic_events add column if not exists forecast text;
alter table if exists economic_events add column if not exists previous text;
alter table if exists economic_events add column if not exists actual text;
alter table if exists economic_events add column if not exists forecast_num numeric;
alter table if exists economic_events add column if not exists previous_num numeric;
alter table if exists economic_events add column if not exists actual_num numeric;
alter table if exists economic_events add column if not exists raw_data jsonb;
alter table if exists economic_events add column if not exists created_at timestamptz not null default now();
alter table if exists economic_events add column if not exists updated_at timestamptz not null default now();

-- Un index par requête, pas un index par colonne. `get_economic_events()` filtre
-- par devise puis trie par date — un index composite sert les deux, là où un
-- index sur `currency` seul laisserait un tri à faire :
create index if not exists idx_economic_events_currency_date
    on economic_events (currency, event_date);

-- Elle peut aussi filtrer sur le seul niveau d'impact :
create index if not exists idx_economic_events_impact
    on economic_events (impact);

-- Ou pas filtrer du tout : c'est l'appel le plus fréquent (le biais macro et le
-- garde-fou de news demandent les 100 derniers événements sans filtre), et il ne
-- trie que sur `event_date` décroissant — aucun des deux index précédents ne
-- peut le servir.
create index if not exists idx_economic_events_event_date
    on economic_events (event_date desc);

-- `updated_at` existe dans le code (le scraper le réécrit à chaque réupsert) :
-- le trigger le rend vrai même pour une écriture qui l'oublierait.
drop trigger if exists trg_economic_events_updated_at on economic_events;
create trigger trg_economic_events_updated_at
    before update on economic_events
    for each row execute function update_updated_at();

-- --------------------------------------------------------------------------- #
-- 2. `macro_bias_logs` — le journal des décisions macro
-- --------------------------------------------------------------------------- #
-- Écrite par `log_macro_decision()` à chaque décision du moteur macro. Aucun
-- `select` du dépôt ne la relit : c'est une **trace**, destinée à un humain qui
-- regarde la table. Elle est donc en ajout seul — pas de `updated_at`, et donc
-- pas de trigger `updated_at` : une ligne n'est jamais modifiée.
create table if not exists macro_bias_logs (
    id bigserial primary key,                     -- clé technique : jamais lue ni comparée par le code
    symbol text not null,                         -- actif analysé (« EURUSD »)
    currency text not null,                       -- devise porteuse de l'événement
    macro_score numeric not null,                 -- score de biais macro (arrondi à 4 décimales par l'appelant)
    news_risk_level text not null,                -- niveau rendu par `execution/news_risk_guard.py`
    decision text not null,                       -- décision du moteur (BUY / SELL / HOLD…)
    reasoning text,                               -- justification, écrite pour être relue
    created_at timestamptz not null default now()  -- fourni explicitement par `log_macro_decision()`
);

alter table if exists macro_bias_logs add column if not exists symbol text;
alter table if exists macro_bias_logs add column if not exists currency text;
alter table if exists macro_bias_logs add column if not exists macro_score numeric;
alter table if exists macro_bias_logs add column if not exists news_risk_level text;
alter table if exists macro_bias_logs add column if not exists decision text;
alter table if exists macro_bias_logs add column if not exists reasoning text;
alter table if exists macro_bias_logs add column if not exists created_at timestamptz not null default now();

-- L'ancien index sur `symbol` est retiré : aucune requête du dépôt ne filtre sur
-- cette colonne (la table n'est qu'**écrite**). Un index sans requête est un coût
-- d'écriture, pas une optimisation — il ralentit chaque `insert` pour rien.
drop index if exists idx_macro_bias_logs_symbol;

-- --------------------------------------------------------------------------- #
-- 3. RLS « deny by default »
-- --------------------------------------------------------------------------- #
-- La migration 007 l'a déjà fait pour ces deux tables ; on le refait ici parce
-- que ce fichier est aussi ce qui les **crée** sur une base neuve : sans ces
-- lignes, une base installée à partir des migrations seules aurait un calendrier
-- économique et un journal de décisions lisibles avec la clé `anon`. Aucune
-- policy n'est créée : `anon` / `authenticated` n'ont aucun accès, et
-- `service_role` (le backend) contourne la RLS.
alter table if exists economic_events enable row level security;
alter table if exists macro_bias_logs enable row level security;

revoke all on economic_events from anon, authenticated;
revoke all on macro_bias_logs from anon, authenticated;
