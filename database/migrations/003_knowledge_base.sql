-- À exécuter dans Supabase → SQL Editor
-- Base de connaissances PERMANENTE (règles de trading, notes Obsidian),
-- séparée de la table `insights` qui contient des actualités éphémères.
-- Une ligne par fichier synchronisé, mise à jour en place (pas de doublons).

create table if not exists knowledge_base (
    source text primary key,        -- ex: "obsidian:strategies/risk-management.md"
    title text,
    content text not null,
    char_count int,
    updated_at timestamptz default now()
);

drop trigger if exists trg_knowledge_base_updated_at on knowledge_base;
create trigger trg_knowledge_base_updated_at
    before update on knowledge_base
    for each row execute function update_updated_at();
