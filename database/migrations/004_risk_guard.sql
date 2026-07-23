-- À exécuter dans Supabase → SQL Editor

-- État de risque par utilisateur (équivalent du RiskManager du bot TradeLocker,
-- mais persisté en base et par utilisateur, pas juste en mémoire).
create table if not exists user_risk_state (
    user_id text primary key,
    starting_balance numeric,
    daily_start_balance numeric,
    daily_date date default current_date,
    updated_at timestamptz default now()
);

drop trigger if exists trg_user_risk_state_updated_at on user_risk_state;
create trigger trg_user_risk_state_updated_at
    before update on user_risk_state
    for each row execute function update_updated_at();

-- Nouveaux réglages de garde-fou, par utilisateur (ajoutés à la table
-- existante user_preferences créée dans 001_user_preferences.sql)
alter table user_preferences
    add column if not exists max_daily_loss_pct numeric default 5.0,
    add column if not exists max_total_drawdown_pct numeric default 10.0,
    add column if not exists max_open_trades int default 3;
