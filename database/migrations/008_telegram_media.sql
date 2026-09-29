-- Migration 008 : médias Telegram — Storage + base de connaissances vectorielle.
--
-- À exécuter dans Supabase → SQL Editor, APRÈS les migrations 001..007.
-- Idempotente : peut être relancée sans erreur.
--
-- Elle met en place trois choses :
--   1. le bucket Storage privé `telegram-media` — il contient les OCTETS ;
--   2. la table `knowledge_media` — elle décrit chaque fichier (canal, message,
--      type MIME, taille, légende…) ;
--   3. la table `knowledge_chunks` — elle porte les morceaux de texte et leur
--      embedding pgvector, pour la recherche sémantique.
--
-- Le client Python `database/media_store.py` est l'interface de (1) et (2).

-- ---------------------------------------------------------------------------
-- 1. pgvector (extension `vector`)
-- ---------------------------------------------------------------------------
-- Supabase installe ses extensions dans le schéma `extensions`. On qualifie donc
-- explicitement le type plus bas (`extensions.vector`) : le script ne dépend
-- alors pas du `search_path` de la session qui l'exécute.
create extension if not exists vector with schema extensions;

-- ---------------------------------------------------------------------------
-- 2. Bucket Storage privé `telegram-media`
-- ---------------------------------------------------------------------------
-- `public = false` : aucun accès anonyme. Le backend utilise la clé
-- `service_role`, qui contourne la RLS de Storage. On ne crée AUCUNE policy :
-- les rôles `anon` / `authenticated` restent donc sans accès, conformément au
-- principe « deny by default » de la migration 007. Pour partager un fichier,
-- le code passe par un lien signé temporaire (voir `create_signed_url()`).
insert into storage.buckets (id, name, public)
values ('telegram-media', 'telegram-media', false)
on conflict (id) do nothing;

-- ---------------------------------------------------------------------------
-- 3. `knowledge_media` — un enregistrement par fichier stocké
-- ---------------------------------------------------------------------------
create table if not exists knowledge_media (
    id uuid primary key default gen_random_uuid(),
    source text not null default 'telegram',   -- provenance du média
    chat_id text,                              -- canal / chat Telegram d'origine
    message_id bigint,                         -- id du message Telegram
    media_type text not null default 'other',  -- photo | video | document | audio | voice | other
    mime_type text,
    file_name text,
    storage_path text not null unique,         -- clé de l'objet dans le bucket
    file_size bigint,
    caption text,
    width int,
    height int,
    duration_seconds numeric,
    telegram_file_id text,                     -- re-téléchargement sans re-parser le canal
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- Un post Telegram peut porter PLUSIEURS médias (album photo/vidéo) : on veut
-- pouvoir retrouver « les médias d'un message » sans parcourir toute la table.
create index if not exists idx_knowledge_media_chat_message
    on knowledge_media (chat_id, message_id);

create index if not exists idx_knowledge_media_created_at
    on knowledge_media (created_at desc);

drop trigger if exists trg_knowledge_media_updated_at on knowledge_media;
create trigger trg_knowledge_media_updated_at
    before update on knowledge_media
    for each row execute function update_updated_at();

-- ---------------------------------------------------------------------------
-- 4. `knowledge_chunks` — texte découpé + embedding (pgvector)
-- ---------------------------------------------------------------------------
-- Dimension 768 : c'est la taille par défaut des embeddings Gemini
-- (`text-embedding-004`, `gemini-embedding-001`), et ce projet est
-- Gemini-centré. ⚠️ Changer de modèle impose une NOUVELLE migration : Postgres
-- ne modifie pas la dimension d'une colonne `vector` en place sans réécrire la
-- table, et la dimension doit correspondre exactement au modèle utilisé.
create table if not exists knowledge_chunks (
    id bigserial primary key,
    media_id uuid not null references knowledge_media (id) on delete cascade,
    chunk_index int not null,                  -- ordre du morceau dans le média
    content text not null,
    token_count int,
    embedding extensions.vector(768),
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    unique (media_id, chunk_index)
);

create index if not exists idx_knowledge_chunks_media
    on knowledge_chunks (media_id);

-- Index de similarité cosinus (HNSW) : c'est celui qu'utiliseront les requêtes
-- `order by embedding <=> :vecteur`. Créé sur une table vide, donc instantané.
create index if not exists idx_knowledge_chunks_embedding
    on knowledge_chunks
    using hnsw (embedding extensions.vector_cosine_ops);

-- ---------------------------------------------------------------------------
-- 5. RLS « deny by default » (cohérent avec la migration 007)
-- ---------------------------------------------------------------------------
alter table if exists knowledge_media  enable row level security;
alter table if exists knowledge_chunks enable row level security;

-- AUCUNE policy n'est créée : `anon` / `authenticated` n'ont aucun accès, et
-- `service_role` (le backend) contourne la RLS. On RÉVOQUE en plus les
-- privilèges par défaut : sur Supabase, une table nouvellement créée dans
-- `public` hérite de GRANT pour ces rôles, que la migration 007 n'a purgés que
-- pour les tables qui existaient alors.
revoke all on knowledge_media  from anon, authenticated;
revoke all on knowledge_chunks from anon, authenticated;
