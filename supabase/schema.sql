-- Run this in the Supabase SQL editor for the project that hosts the API.
-- The FastAPI service uses SUPABASE_SERVICE_ROLE_KEY, which bypasses RLS.
-- The web and kids apps should call this API. They should not use the service role key.
-- SUPABASE_ANON_KEY is not used by the server. The SFX bucket is public so those
-- apps can load the MP3 from the URL returned by the API.

create table if not exists public.recordings (
    id uuid primary key,
    story_id text,
    title text,
    narrator text,
    source_audio_url text,
    transcript_text text not null default '',
    transcript_json jsonb not null default '{}'::jsonb,
    duration_seconds double precision not null default 0,
    status text not null default 'processing',
    error_message text,
    warnings jsonb not null default '[]'::jsonb,
    sfx_storage_path text,
    sfx_url text,
    cues jsonb not null default '[]'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists recordings_story_id_idx on public.recordings (story_id);
create index if not exists recordings_created_at_idx on public.recordings (created_at desc);

alter table public.recordings enable row level security;

insert into storage.buckets (id, name, public)
values ('story-sfx', 'story-sfx', true)
on conflict (id) do update set public = excluded.public;

drop policy if exists "story_sfx_public_read" on storage.objects;
create policy "story_sfx_public_read"
on storage.objects
for select
to public
using (bucket_id = 'story-sfx');
