-- À exécuter dans Supabase → SQL Editor avant de déployer le code
-- qui utilise database/broker_credentials.py
--
-- IMPORTANT : les colonnes *_enc contiennent des données CHIFFRÉES
-- (Fernet), jamais les clés API en clair.

create table if not exists user_broker_credentials (
    user_id text primary key,
    broker text default 'alpaca',
    api_key_enc text not null,
    api_secret_enc text not null,
    paper boolean default true,
    created_at timestamptz default now(),
    updated_at timestamptz default now()
);

drop trigger if exists trg_user_broker_credentials_updated_at on user_broker_credentials;
create trigger trg_user_broker_credentials_updated_at
    before update on user_broker_credentials
    for each row execute function update_updated_at();

-- Row Level Security : personne ne doit pouvoir lire cette table via
-- l'API publique Supabase, seul le backend (clé service_role) y accède.
alter table user_broker_credentials enable row level security;
-- Aucune policy créée volontairement = accès refusé par défaut à tout
-- sauf la clé service_role (qui bypass RLS), utilisée par le backend.
