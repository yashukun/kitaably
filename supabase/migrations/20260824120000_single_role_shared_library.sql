-- Phase 1 and 2, revised — roles and classrooms are both removed.
--
-- The product changed shape twice in one sitting, and this migration lands both
-- because neither had reached a database yet.
--
--   WAS   a teacher owns a classroom; students join it by code; canon books are
--         shared with that room; a teacher may do things a student may not.
--
--   NOW   there is one kind of account. Everyone uploads, shares, generates an
--         assessment, and sits one. There is one library, and the only distinction
--         left in the system is the one that matters:
--
--           personal  private to its owner. Nobody else, ever.
--           canon     shared with every signed-in user.
--
-- What survives unchanged is the part worth protecting: a personal book is visible
-- to its owner alone, scope is still derived server-side from the authenticated
-- principal, and `build_retrieval_filter()` is still the only place a predicate over
-- `chunks` is constructed. Roles were never what enforced that -- ownership was.
--
-- What is genuinely weaker: `canon` was "shared with one classroom, by the teacher
-- who owns it" and is now "shared with everyone, by anyone". That is a deliberate
-- product decision, not an oversight. Rationale and reversal cost: DECISIONS.md D16.
--
-- Forward-only, and destructive: `classrooms` and `enrollments` are dropped rather
-- than archived. Nothing downstream of them was ever built, so there is no history
-- to keep.

-- ======================================================= 1. classrooms go
-- ------------------------------------------------------- dependents first
-- The roster view reads enrollments and calls is_classroom_teacher(); both are
-- about to go. It has no replacement -- nobody has "their students" any more.
drop view if exists public.classroom_roster;

-- These two policies call the membership helpers. They are recreated below without
-- them. Dropping first means the helper drop does not need a CASCADE, which would
-- silently take anything else that happened to reference them.
drop policy if exists "books_select_owner_or_canon_member"  on public.books;
drop policy if exists "chunks_select_owner_or_canon_member" on public.chunks;

-- ------------------------------------------------- the scope-carrying triggers
-- Rewritten before the columns disappear underneath them. plpgsql resolves column
-- references at execution rather than at creation, so a stale body here would fail
-- on the next ingest rather than on this migration -- the worst possible timing.
create or replace function public.sync_chunk_scope()
returns trigger
language plpgsql
set search_path = public
as $$
begin
    -- Whatever the caller supplied for these is overwritten by the book's own
    -- values. A chunk cannot be inserted into a scope its book does not have.
    select b.owner_id, b.scope
      into new.owner_id, new.scope
      from public.books b
     where b.id = new.book_id;
    return new;
end;
$$;

create or replace function public.propagate_book_scope()
returns trigger
language plpgsql
set search_path = public
as $$
begin
    -- Sharing or unsharing a book changes who may read its chunks. The denormalised
    -- copy has to move in the same transaction or it becomes a stale access grant --
    -- material the owner believes is private, still answering other people.
    if new.scope is distinct from old.scope then
        update public.chunks
           set scope = new.scope
         where book_id = new.id;
    end if;
    return new;
end;
$$;

-- ------------------------------------------------------------ the columns go
alter table public.chat_sessions drop column classroom_id;

alter table public.books drop constraint books_canon_needs_classroom;

drop index if exists public.books_classroom_scope_status_idx;
drop index if exists public.chunks_classroom_scope_idx;

alter table public.books  drop column classroom_id;
alter table public.chunks drop column classroom_id;

-- Canon is selected by scope alone now, so scope leads the index.
create index books_scope_status_idx on public.books (scope, status);
create index chunks_canon_idx on public.chunks (scope) where scope = 'canon';

-- ------------------------------------------------------------- the tables go
drop table public.enrollments;
drop table public.classrooms;
drop type  public.enrollment_status;

drop function public.is_classroom_teacher(uuid);
drop function public.is_active_enrollee(uuid);
drop function public.classroom_id_for_join_code(text);
drop function public.generate_join_code();


-- ============================================================ 2. roles go
--
-- One kind of account. The column stays, holding a single value, because "for now"
-- is doing real work in that sentence: when a distinction comes back it is
-- `alter type public.app_role add value '...'` plus the policies that read it,
-- rather than re-deriving an identity model from scratch.
--
-- A new type rather than editing the old one: Postgres can add an enum value but
-- cannot remove one, so reusing `user_role` would leave 'teacher' and 'student'
-- permanently valid and permanently meaningless -- exactly the kind of value that
-- gets written by accident and read as authority later.
create type public.app_role as enum ('user');

alter table public.profiles
    alter column role drop default,
    alter column role type public.app_role using 'user'::public.app_role,
    alter column role set default 'user';

drop type public.user_role;

comment on column public.profiles.role is
    'One value today. Kept as a column so that reintroducing a distinction is an '
    'ALTER TYPE plus policies, not a new identity model. Nothing branches on it.';

-- The signup trigger stops reading a role out of client-supplied metadata. That
-- read was the one place signup metadata touched authority, and there is no longer
-- any authority for it to touch.
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public, extensions
as $$
begin
    insert into public.profiles (id, email, name)
    values (
        new.id,
        new.email,
        nullif(new.raw_user_meta_data ->> 'name', '')
    );
    return new;
end;
$$;

-- Anyone may share a book, so the "canon implies a teacher owns it" rule has no
-- subject left. Dropping the trigger is what makes that true at the table rather
-- than only in the service -- leaving it would refuse every share.
drop trigger books_canon_owner_is_teacher on public.books;
drop function public.enforce_canon_owner_is_teacher();


-- ==================================================== 3. the new boundary
--
-- Read these as the whole access model, because they are. Two ways in and no
-- third: the row is mine, or the row is shared.
--
-- Note what is NOT here: any test of who the caller is, beyond being somebody.
-- Ownership is the boundary; it always was.
create policy "books_select_own_or_shared"
    on public.books for select
    using (
        owner_id = (select auth.uid())
        or scope = 'canon'
    );

create policy "chunks_select_own_or_shared"
    on public.chunks for select
    using (
        owner_id = (select auth.uid())
        or scope = 'canon'
    );

-- books_insert_own, books_update_own and books_delete_own are unchanged and still
-- apply: only the owner writes the row at all. Sharing is that owner updating
-- `scope` on their own row, which those policies already permit and constrain.

comment on column public.books.scope is
    'personal = private to owner_id, visible to nobody else. canon = shared with '
    'every signed-in user, and the only pool assessment generation may draw from.';
