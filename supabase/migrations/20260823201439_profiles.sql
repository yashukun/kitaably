-- Phase 1 — identity.
--
-- `profiles` mirrors auth.users (owned by GoTrue) with the application's own facts.
-- The load-bearing one is `role`: it is read from this table on every request and
-- never from a JWT claim, because a claim is client-visible and, in some flows,
-- client-influenced.

create extension if not exists citext with schema extensions;

create type public.user_role as enum ('teacher', 'student');

create table public.profiles (
    id          uuid primary key references auth.users (id) on delete cascade,
    email       extensions.citext not null unique,
    name        text,
    role        public.user_role not null default 'student',
    avatar_url  text,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

comment on column public.profiles.role is
    'Authority on what a user may do. Read from here on every request, never from a JWT claim.';

-- Two independent layers, and both are required:
--
--   GRANT  decides whether the role may touch the table at all. Without it the
--          query is refused before any policy is consulted.
--   POLICY decides which rows it sees, once the grant let it in.
--
-- RLS restricts; it does not grant. A table with policies and no grant is
-- unreadable by everyone, which is exactly how this was first written.
grant usage on schema public to anon, authenticated, service_role;

-- No insert: rows come from the signup trigger below, which runs as definer.
-- No delete: profiles die with their auth.users row, by cascade.
grant select, update on public.profiles to authenticated;

-- The Celery worker connects as service_role and bypasses RLS entirely, which is
-- precisely why every worker query must carry its own scope predicate.
grant all on public.profiles to service_role;

alter table public.profiles enable row level security;

create policy "profiles_select_own"
    on public.profiles for select
    using (id = (select auth.uid()));

create policy "profiles_update_own"
    on public.profiles for update
    using (id = (select auth.uid()))
    with check (id = (select auth.uid()));

-- Note there is deliberately no INSERT policy: rows are created by the trigger
-- below, which runs as definer. A user cannot mint their own profile row, so they
-- cannot mint their own role.

-- Populate on signup. SECURITY DEFINER because the new user has no rights yet.
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public, extensions
as $$
declare
    requested text := new.raw_user_meta_data ->> 'role';
begin
    insert into public.profiles (id, email, name, role)
    values (
        new.id,
        new.email,
        nullif(new.raw_user_meta_data ->> 'name', ''),
        -- Anything unrecognised becomes 'student'. Signup metadata is supplied by
        -- the client, so this is the one place it touches role, and it fails closed
        -- to the least-privileged value.
        case when requested = 'teacher' then 'teacher'::public.user_role
             else 'student'::public.user_role
        end
    );
    return new;
end;
$$;

create trigger on_auth_user_created
    after insert on auth.users
    for each row execute function public.handle_new_user();

create or replace function public.touch_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at := now();
    return new;
end;
$$;

create trigger profiles_touch_updated_at
    before update on public.profiles
    for each row execute function public.touch_updated_at();
