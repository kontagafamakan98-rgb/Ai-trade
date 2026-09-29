-- Migration 007 : Row Level Security sur TOUTES les tables applicatives.
--
-- Principe « deny by default » : on active la RLS et on NE crée AUCUNE policy
-- pour les rôles publics (anon / authenticated). Résultat :
--   * la clé anon/authenticated ne peut rien lire ni écrire,
--   * la clé service_role utilisée par le backend contourne la RLS et continue
--     de fonctionner normalement.
--
-- À exécuter dans Supabase → SQL Editor. Idempotent : peut être relancé.
--
-- ⚠️ NE PAS appliquer ceci si tu exposes volontairement certaines tables via
-- l'API publique Supabase (dans ce cas, ajoute des policies explicites).

alter table if exists users                    enable row level security;
alter table if exists insights                 enable row level security;
alter table if exists pending_signals          enable row level security;
alter table if exists user_preferences         enable row level security;
alter table if exists user_risk_state          enable row level security;
alter table if exists user_broker_credentials  enable row level security;
alter table if exists knowledge_base           enable row level security;
alter table if exists economic_events          enable row level security;
alter table if exists macro_bias_logs          enable row level security;
alter table if exists trade_post_mortems       enable row level security;
alter table if exists adaptive_model_weights   enable row level security;

-- Défense supplémentaire : révoque les privilèges par défaut sur ces tables
-- pour les rôles anonymes/authentifiés (au cas où des GRANT auraient été
-- accordés précédemment).
revoke all on all tables in schema public from anon, authenticated;
