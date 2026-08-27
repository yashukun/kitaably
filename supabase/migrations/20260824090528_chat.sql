-- Phase 4 — grounded chat.
--
-- A conversation is scoped to one classroom, because that is what decides which
-- material may lawfully answer it. The retrieval predicate is built from the
-- principal and the session's classroom, never from anything a message carries.

create type public.message_role as enum ('user', 'assistant');

create table public.chat_sessions (
    id           uuid primary key default gen_random_uuid(),
    user_id      uuid not null references public.profiles (id) on delete cascade,
    classroom_id uuid not null references public.classrooms (id) on delete cascade,
    title        text,
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now()
);

create index chat_sessions_user_idx on public.chat_sessions (user_id, created_at desc);

create table public.chat_messages (
    id          uuid primary key default gen_random_uuid(),
    session_id  uuid not null references public.chat_sessions (id) on delete cascade,
    role        public.message_role not null,
    content     text not null,

    -- [{chunk_id, book_id, book_title, page, scope}]. `scope` travels with the
    -- citation so the UI can say whether a claim came from the class book or the
    -- student's own upload -- a student needs to know which of their sources the
    -- class is actually examined on.
    citations   jsonb not null default '[]'::jsonb,
    token_usage jsonb,
    created_at  timestamptz not null default now()
);

create index chat_messages_session_idx on public.chat_messages (session_id, created_at);

grant select, insert, update on public.chat_sessions to authenticated;
grant select, insert on public.chat_messages to authenticated;
grant all on public.chat_sessions, public.chat_messages to service_role;

-- No update or delete on messages for anybody: a transcript that can be edited after
-- the fact is not a transcript.

alter table public.chat_sessions enable row level security;
alter table public.chat_messages enable row level security;

create policy "chat_sessions_select_own"
    on public.chat_sessions for select
    using (user_id = (select auth.uid()));

create policy "chat_sessions_insert_own"
    on public.chat_sessions for insert
    with check (user_id = (select auth.uid()));

create policy "chat_sessions_update_own"
    on public.chat_sessions for update
    using (user_id = (select auth.uid()))
    with check (user_id = (select auth.uid()));

-- Messages inherit the session's ownership. A conversation is private to the person
-- having it: a teacher does not read a student's chat, and there is no policy here
-- that would let them.
create policy "chat_messages_select_through_session"
    on public.chat_messages for select
    using (
        exists (
            select 1 from public.chat_sessions s
            where s.id = session_id and s.user_id = (select auth.uid())
        )
    );

create policy "chat_messages_insert_through_session"
    on public.chat_messages for insert
    with check (
        exists (
            select 1 from public.chat_sessions s
            where s.id = session_id and s.user_id = (select auth.uid())
        )
    );

create trigger chat_sessions_touch_updated_at
    before update on public.chat_sessions
    for each row execute function public.touch_updated_at();
