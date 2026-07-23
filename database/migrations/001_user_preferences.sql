-- À exécuter dans Supabase → SQL Editor avant de déployer le code
-- qui utilise database/preferences.py

create table if not exists user_preferences (
    user_id text primary key,
    watchlist text[] default array['AAPL','MSFT','GOOGL','BTC-USD','ETH-USD'],
    risk_pct numeric default 1.0,
    paper_equity numeric default 100000,
    notify_enabled boolean default true,
    min_confidence numeric default 0.55,
    updated_at timestamptz default now()
);

-- Met à jour updated_at automatiquement à chaque modification
create or replace function update_updated_at()
returns trigger as $$
begin
    new.updated_at = now();
    return new;
end;
$$ language plpgsql;

drop trigger if exists trg_user_preferences_updated_at on user_preferences;
create trigger trg_user_preferences_updated_at
    before update on user_preferences
    for each row execute function update_updated_at();
