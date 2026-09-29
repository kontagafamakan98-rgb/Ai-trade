-- Migration 010 : réglages d'exécution (`bot_settings`).
--
-- À exécuter dans Supabase → SQL Editor, APRÈS la migration 009. Idempotente.
--
-- Jusqu'ici, la liste des canaux balayés et la période de balayage étaient des
-- constantes de `workers/auto_loop.py` : les changer demandait de modifier le
-- worker et de le redéployer. Elles sont désormais lues dans l'environnement
-- (`TELEGRAM_CHANNELS`, `TELEGRAM_SCAN_MINUTES`, validées par
-- `core/config_runtime.py`) et peuvent être **surchargées** par cette table,
-- sans redéployer un worker qui tourne en continu.
--
-- Une table clé/valeur générique plutôt qu'une colonne par réglage : ces
-- réglages n'ont ni la même nature (liste de textes, entier) ni le même
-- propriétaire, et une colonne par futur réglage imposerait une migration à
-- chaque fois. `jsonb` parce que la valeur peut être une liste.
--
-- Le client Python `database/settings.py` est l'interface de cette table. Il la
-- traite comme un **confort** : table absente (cette migration non appliquée),
-- lecture impossible ou valeur illisible → retour à l'environnement, jamais une
-- exception, jamais un worker qui refuse de démarrer.

create table if not exists bot_settings (
    key text primary key,
    value jsonb not null default 'null'::jsonb,
    updated_at timestamptz not null default now()
);

-- Le trigger `update_updated_at()` vient de la migration 001 (`if not exists`
-- impossible sur un trigger : on le supprime avant de le recréer, comme ailleurs).
drop trigger if exists trg_bot_settings_updated_at on bot_settings;
create trigger trg_bot_settings_updated_at
    before update on bot_settings
    for each row execute function update_updated_at();

-- --------------------------------------------------------------------------- #
-- RLS « deny by default » (cohérent avec les migrations 007 et 008)
-- --------------------------------------------------------------------------- #
-- Aucune policy : `anon` / `authenticated` n'ont aucun accès, et `service_role`
-- (le backend) contourne la RLS. On révoque en plus les privilèges par défaut,
-- dont hérite toute table nouvellement créée dans `public`.
alter table if exists bot_settings enable row level security;
revoke all on bot_settings from anon, authenticated;
