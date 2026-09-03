-- notifications — how a result reaches the person who has to act on it.
--
-- The gap this closes: somebody sits a paper, the worker marks it, and the author is
-- told nothing at all. The gradebook has the answer, but only for an author who
-- happens to open that page and refresh it. Everything in this product that matters
-- waits on a human decision — a mark released, a proctoring report reviewed — and a
-- decision nobody is told to make does not get made.
--
-- **Delivered to the assessment's AUTHOR, and that is the invariant this table
-- serves.** The author holds authority over an assessment's results (CLAUDE.md), so
-- they are who a submitted paper is reported to. There is no notification that
-- carries a proctoring finding to a sitter, and none that carries a mark to a sitter
-- ahead of the author releasing it: `results_released_at` remains the only gate on
-- that, and this table never front-runs it (invariant 3).
--
-- Content is deliberately thin. A notification says what happened and points at the
-- row; it does not carry the marked paper. Two reasons, and the second is the real
-- one: a payload duplicates data that can change (a grade override, a void), and
-- would have to be kept in step with it — and a notification row is a much easier
-- thing to leak than the resource it names, which is already guarded by RLS and by
-- an author guard on the route. Pointing is safe; copying is not.

create type public.notification_kind as enum (
    -- Somebody sat a paper you wrote, and it has been marked.
    'attempt_submitted',
    -- A paper you wrote finished generating, or came back short.
    'assessment_ready',
    -- A proctored sitting is waiting for your review before it can be released.
    'review_pending'
);

create table public.notifications (
    id          uuid primary key default gen_random_uuid(),
    -- Who this is for. Always the person who must act, never a broadcast.
    user_id     uuid not null references public.profiles (id) on delete cascade,
    kind        public.notification_kind not null,
    -- What it says, rendered server-side. Held here rather than composed in the
    -- client so that the wording of a past notification cannot change under it.
    title       text not null,
    body        text,
    -- What it points at. Not a foreign key: an attempt can be deleted with its
    -- assessment, and a notification about a thing that no longer exists should
    -- degrade to an un-clickable line rather than take the row with it.
    target_type text not null,
    target_id   uuid,
    read_at     timestamptz,
    created_at  timestamptz not null default now()
);

-- The unread badge is the hot query and it wants exactly this: one person's rows,
-- newest first, unread first.
create index notifications_inbox_idx
    on public.notifications (user_id, created_at desc);
create index notifications_unread_idx
    on public.notifications (user_id)
    where read_at is null;

-- One notification per (recipient, kind, target). The worker delivers at least once
-- — a retried grading task runs the whole tail again — and an author who is told four
-- times that one person sat one paper stops reading the notifications.
create unique index notifications_once_idx
    on public.notifications (user_id, kind, target_id)
    where target_id is not null;

comment on table public.notifications is
    'Server-delivered messages to one person. Written by the worker; a recipient may '
    'read their own and mark them read, and can never write one.';

-- GRANT and RLS are separate layers and both are required (CLAUDE.md). Note what is
-- absent: no INSERT to `authenticated`. A notification a user can write is a
-- notification an attacker can write, and this one carries a claim about somebody
-- else's paper. Only the worker delivers.
grant select, update on public.notifications to authenticated;
grant select, insert, update, delete on public.notifications to service_role;

alter table public.notifications enable row level security;

create policy notifications_select_own on public.notifications
    for select to authenticated
    using (user_id = (select auth.uid()));

-- Marking one read is the only write a recipient gets. The `with check` repeats the
-- ownership test on the NEW row: without it, an update could hand a row to somebody
-- else — the `using` clause only decides which rows may be touched, not what they may
-- become.
create policy notifications_mark_read on public.notifications
    for update to authenticated
    using (user_id = (select auth.uid()))
    with check (user_id = (select auth.uid()));

create policy notifications_service_all on public.notifications
    for all to service_role
    using (true) with check (true);

-- A recipient may set `read_at` and nothing else. RLS decides which rows an update
-- may touch; it cannot stop that update rewriting the title, and a notification whose
-- subject can rewrite its own text is not a record of anything.
create or replace function public.notifications_freeze_content()
returns trigger
language plpgsql
as $$
begin
    if auth.uid() is not null and auth.role() = 'authenticated' then
        if new.user_id     is distinct from old.user_id
        or new.kind        is distinct from old.kind
        or new.title       is distinct from old.title
        or new.body        is distinct from old.body
        or new.target_type is distinct from old.target_type
        or new.target_id   is distinct from old.target_id
        or new.created_at  is distinct from old.created_at then
            raise exception 'only read_at may be changed';
        end if;
    end if;
    return new;
end;
$$;

create trigger notifications_freeze_content_trg
    before update on public.notifications
    for each row execute function public.notifications_freeze_content();
