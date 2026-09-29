-- Migration 009 : recherche vectorielle dans knowledge_chunks.
--
-- À exécuter dans Supabase → SQL Editor, APRÈS la migration 008. Idempotente.
--
-- La migration 008 avait créé `knowledge_chunks` pour les SEULS médias
-- (`media_id NOT NULL`) avec une colonne `embedding` pgvector(768) laissée nulle.
-- Cette migration la fait évoluer pour la recherche sémantique :
--
--   1. `media_id` devient NULLABLE et deux colonnes sont ajoutées pour indexer
--      aussi les NOTES de `knowledge_base` (`source`, `note_source`) ;
--   2. `asset` / `regime` portent les filtres, `embedding_model` trace le modèle
--      qui a produit le vecteur (768 dims = modèle Gemini d'embeddings) ;
--   3. un index couvre les filtres, et une fonction `match_knowledge_chunks`
--      fournit la recherche top-k — PostgREST ne sait pas trier par l'opérateur
--      de distance `<=>`, il faut donc une fonction SQL. Cet opérateur est
--      **qualifié** (`operator(extensions.<=>)`) : voir la note avant la
--      définition de la fonction.

-- ---------------------------------------------------------------------------
-- 1. Une ligne peut désormais décrire un média OU une note (jamais les deux).
-- ---------------------------------------------------------------------------
alter table if exists knowledge_chunks
    alter column media_id drop not null;

alter table if exists knowledge_chunks
    add column if not exists source text not null default 'telegram',
    add column if not exists note_source text,
    add column if not exists asset text,
    add column if not exists regime text,
    add column if not exists embedding_model text;

-- L'unicité (media_id, chunk_index) de 008 reste valable : en Postgres, deux
-- lignes dont `media_id` est NULL ne se heurtent pas (NULL est distinct de NULL).

-- ---------------------------------------------------------------------------
-- 2. Index des filtres de recherche (sans lui, chaque filtre fait un scan).
-- ---------------------------------------------------------------------------
create index if not exists idx_knowledge_chunks_filters
    on knowledge_chunks (source, asset, regime);

-- ---------------------------------------------------------------------------
-- 3. Recherche top-k filtrée par source, actif et régime.
-- ---------------------------------------------------------------------------
-- Sémantique des filtres d'actif/régime : `null` est un **joker**. Un morceau
-- non étiqueté (`asset`/`regime` NULL) reste donc candidat — sinon une base où
-- rien n'est étiqueté renverrait toujours zéro résultat. Le classement place en
-- tête les correspondances explicites de l'actif puis du régime, avant la
-- similarité pure.
--
-- Le type **et** l'opérateur de distance sont qualifiés (`extensions.`). Le corps
-- d'une fonction `language sql` est analysé **à sa création** : un `<=>` nu serait
-- résolu selon le `search_path` de la session qui applique ce fichier — `public,
-- extensions` sur Supabase, mais `"$user", public` sur un PostgreSQL nu, où
-- l'opérateur de pgvector serait alors introuvable (« operator does not exist:
-- extensions.vector <=> extensions.vector »). Ce fichier doit s'appliquer dans
-- les deux cas : `scripts/apply_migrations.py` et le job CI l'exécutent sur un
-- Postgres nu, quand l'éditeur SQL de Supabase, lui, a `extensions` en portée.
-- La qualification est aussi la règle déjà écrite dans `008`.
create or replace function match_knowledge_chunks(
    query_embedding extensions.vector(768),
    match_count int default 5,
    filter_source text default null,
    filter_asset text default null,
    filter_regime text default null
)
returns table (
    id bigint,
    media_id uuid,
    chunk_index int,
    content text,
    source text,
    asset text,
    regime text,
    similarity double precision
)
language sql
stable
as $$
    select
        kc.id,
        kc.media_id,
        kc.chunk_index,
        kc.content,
        kc.source,
        kc.asset,
        kc.regime,
        1 - (kc.embedding operator(extensions.<=>) query_embedding) as similarity
    from knowledge_chunks kc
    where kc.embedding is not null
      and (filter_source is null or kc.source = filter_source)
      and (filter_asset  is null or kc.asset  is null or kc.asset  = filter_asset)
      and (filter_regime is null or kc.regime is null or kc.regime = filter_regime)
    order by
        (kc.asset = filter_asset) desc nulls last,
        (kc.regime = filter_regime) desc nulls last,
        kc.embedding operator(extensions.<=>) query_embedding
    limit greatest(match_count, 1);
$$;

-- Cohérent avec le « deny by default » de 007/008 : les rôles publics n'ont pas
-- accès à la fonction (le backend utilise la clé service_role).
revoke execute on function match_knowledge_chunks from anon, authenticated;
