-- Supabase schema for this backend.
--
-- The document this project stores is the allowed SFX library: one versioned
-- JSON catalog (id, label, category, keywords, FreeSound fields, status).
-- Marketplace listings and social sharing belong in the frontend database.
-- Do not add those tables here.
--
-- The FastAPI service uses SUPABASE_SERVICE_ROLE_KEY, which bypasses RLS.
-- Web and kids apps call this API. They must not use the service role key.
-- SUPABASE_ANON_KEY is not used. The story-sfx bucket is public so those
-- apps can play a mixed MP3 from the URL this API returns.
--
-- Fresh project: run this file once in the SQL editor.
-- It replaces the earlier recordings-only schema. If public.recordings
-- already exists with the wide column list (transcript_json, cues, ...),
-- drop that table first and re-run. Mix metadata is not the catalog.

-- ---------------------------------------------------------------------------
-- Allowed sound-effect library (the persisted artifact)
--
-- id       stable key. The API reads and writes the row id = 'active'.
-- version  storage revision. The app increments it on each save.
-- payload  catalog document. Same shape as assets/sfx_catalog/catalog.json:
--          { "version", "entries": [ { "id", "label", "category", "keywords",
--            "search_query", "freesound_id", "freesound_url", "preview_url",
--            "license", "duration", "status", ... } ] }
-- updated_at  last save time.
-- ---------------------------------------------------------------------------

create table if not exists public.sfx_catalog (
    id text primary key check (char_length(id) > 0),
    version integer not null check (version >= 1),
    payload jsonb not null check (jsonb_typeof(payload) = 'object'),
    updated_at timestamptz not null default now()
);

comment on table public.sfx_catalog is
    'Versioned SFX catalog JSON. Not a marketplace or social store.';

alter table public.sfx_catalog enable row level security;

-- ---------------------------------------------------------------------------
-- Mixed story MP3s (minimal)
--
-- Web and kids apps load a finished SFX MP3 from sfx_url. This row is only
-- that serving record. It is not the catalog and not a social graph.
-- Transcript text, cues, warnings, and the narrator live in meta jsonb so
-- the story API can still return them without a wide recordings table.
-- ---------------------------------------------------------------------------

create table if not exists public.recordings (
    id uuid primary key,
    story_id text,
    title text,
    status text not null default 'processing',
    duration_seconds double precision not null default 0,
    sfx_storage_path text,
    sfx_url text,
    meta jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

comment on table public.recordings is
    'Minimal row so clients can play a mixed SFX MP3. Catalog JSON is sfx_catalog.';

create index if not exists recordings_story_id_idx on public.recordings (story_id);
create index if not exists recordings_created_at_idx on public.recordings (created_at desc);

alter table public.recordings enable row level security;

-- Generated mix MP3s only. Catalog JSON stays in public.sfx_catalog.
insert into storage.buckets (id, name, public)
values ('story-sfx', 'story-sfx', true)
on conflict (id) do update set public = excluded.public;

drop policy if exists "story_sfx_public_read" on storage.objects;
create policy "story_sfx_public_read"
on storage.objects
for select
to public
using (bucket_id = 'story-sfx');
