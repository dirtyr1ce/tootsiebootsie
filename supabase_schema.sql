-- ═══════════════════════════════════════════════════════════
-- TootsieBootsie — Supabase Schema
-- Run this in your Supabase SQL editor (supabase.com → SQL Editor)
-- ═══════════════════════════════════════════════════════════

-- ── Enable UUID extension ────────────────────────────────────
create extension if not exists "uuid-ossp";

-- ── Users (mirrors Supabase auth.users) ─────────────────────
create table public.profiles (
  id           uuid references auth.users(id) on delete cascade primary key,
  name         text,
  avatar_url   text,
  created_at   timestamptz default now()
);

-- Auto-create profile when user signs up
create or replace function public.handle_new_user()
returns trigger as $$
begin
  insert into public.profiles (id, name, avatar_url)
  values (
    new.id,
    new.raw_user_meta_data->>'full_name',
    new.raw_user_meta_data->>'avatar_url'
  );
  return new;
end;
$$ language plpgsql security definer;

create trigger on_auth_user_created
  after insert on auth.users
  for each row execute procedure public.handle_new_user();

-- ── Traces ───────────────────────────────────────────────────
create table public.traces (
  id           uuid default uuid_generate_v4() primary key,
  user_id      uuid references public.profiles(id) on delete cascade not null,
  place_name   text not null,
  place_type   text check (place_type in ('local','trip','hike')) default 'local',
  lat          float not null,
  lng          float not null,
  photo_url    text not null,
  sentence     text not null check (char_length(sentence) <= 120),
  day_story_id uuid,
  created_at   timestamptz default now()
);

-- Index for fast geo queries
create index traces_location on public.traces using btree (lat, lng);
create index traces_place on public.traces (place_name);
create index traces_created on public.traces (created_at desc);

-- ── Day Stories ──────────────────────────────────────────────
create table public.day_stories (
  id           uuid default uuid_generate_v4() primary key,
  user_id      uuid references public.profiles(id) on delete cascade not null,
  city         text not null,
  travel_date  date not null,
  title        text,
  trace_ids    uuid[] not null default '{}',
  copy_count   integer default 0,
  created_at   timestamptz default now()
);

create index stories_city on public.day_stories (city);
create index stories_created on public.day_stories (created_at desc);

-- ── Storage bucket for trace photos ─────────────────────────
-- Run this after creating the bucket in Supabase dashboard:
-- Storage → New Bucket → name: "traces" → Public: ON
insert into storage.buckets (id, name, public)
values ('traces', 'traces', true)
on conflict do nothing;

-- ── Row Level Security ───────────────────────────────────────
alter table public.profiles    enable row level security;
alter table public.traces      enable row level security;
alter table public.day_stories enable row level security;

-- Profiles: anyone can read, only you can update yours
create policy "profiles_read_all"   on public.profiles for select using (true);
create policy "profiles_update_own" on public.profiles for update using (auth.uid() = id);

-- Traces: anyone can read, only authenticated users can insert
create policy "traces_read_all"    on public.traces for select using (true);
create policy "traces_insert_auth" on public.traces for insert
  with check (auth.uid() = user_id);
create policy "traces_delete_own"  on public.traces for delete
  using (auth.uid() = user_id);

-- Day stories: anyone can read, only owner can write
create policy "stories_read_all"    on public.day_stories for select using (true);
create policy "stories_insert_auth" on public.day_stories for insert
  with check (auth.uid() = user_id);
create policy "stories_update_own"  on public.day_stories for update
  using (auth.uid() = user_id);

-- Storage: anyone can read trace photos, auth users can upload
create policy "traces_storage_read" on storage.objects
  for select using (bucket_id = 'traces');
create policy "traces_storage_upload" on storage.objects
  for insert with check (bucket_id = 'traces' and auth.role() = 'authenticated');
create policy "traces_storage_delete" on storage.objects
  for delete using (bucket_id = 'traces' and auth.uid()::text = (storage.foldername(name))[1]);

-- ── Helper views ─────────────────────────────────────────────

-- Traces with user info joined
create view public.traces_with_user as
  select
    t.*,
    p.name as user_name,
    p.avatar_url as user_avatar
  from public.traces t
  left join public.profiles p on p.id = t.user_id;

-- Recent traces per place
create view public.place_trace_counts as
  select
    place_name,
    place_type,
    count(*) as trace_count,
    max(created_at) as last_trace_at,
    avg(lat) as center_lat,
    avg(lng) as center_lng
  from public.traces
  group by place_name, place_type;

-- ── Increment copy count function ────────────────────────────
create or replace function increment_copy_count(story_id uuid)
returns void as $$
  update public.day_stories
  set copy_count = copy_count + 1
  where id = story_id;
$$ language sql security definer;
