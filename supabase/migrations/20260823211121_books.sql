-- Phase 3 — material, and the scoping boundary.
--
-- The one rule everything else depends on:
--
--   canon     teacher's book, classroom_id set
--             -> the teacher + every ACTIVE enrollee of that classroom
--
--   personal  student's book
--             -> that student ONLY. Never a teacher, never a classmate,
--                never a source for a shared assessment.

create extension if not exists vector with schema extensions;

create type public.book_scope     as enum ('canon', 'personal');
create type public.source_format  as enum ('pdf', 'docx', 'pptx', 'txt', 'md');
create type public.book_status    as enum
    ('uploaded', 'parsing', 'chunking', 'embedding', 'ready', 'failed');

-- ------------------------------------------------------------------- books
create table public.books (
    id            uuid primary key default gen_random_uuid(),
    owner_id      uuid not null references public.profiles (id) on delete cascade,
    classroom_id  uuid references public.classrooms (id) on delete cascade,
    scope         public.book_scope not null,
    title         text not null,
    author        text,
    source_format public.source_format not null,
    storage_path  text not null,
    byte_size     bigint not null,
    page_count    int,
    status        public.book_status not null default 'uploaded',
    error         text,
    needs_ocr     boolean not null default false,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),

    constraint books_canon_needs_classroom
        check (scope <> 'canon' or classroom_id is not null)
);

create index books_classroom_scope_status_idx on public.books (classroom_id, scope, status);
create index books_owner_scope_idx on public.books (owner_id, scope);

-- A canon book must belong to a teacher. The check spans two tables, so it is a
-- trigger rather than a CHECK constraint.
create or replace function public.enforce_canon_owner_is_teacher()
returns trigger
language plpgsql
set search_path = public
as $$
begin
    if new.scope = 'canon' then
        if not exists (
            select 1 from public.profiles p
            where p.id = new.owner_id and p.role = 'teacher'
        ) then
            raise exception 'canon books must be owned by a teacher';
        end if;
    end if;
    return new;
end;
$$;

create trigger books_canon_owner_is_teacher
    before insert or update on public.books
    for each row execute function public.enforce_canon_owner_is_teacher();

create trigger books_touch_updated_at
    before update on public.books
    for each row execute function public.touch_updated_at();

-- ---------------------------------------------------------------- chapters
create table public.chapters (
    id         uuid primary key default gen_random_uuid(),
    book_id    uuid not null references public.books (id) on delete cascade,
    "index"    int not null,
    title      text,
    page_start int,
    page_end   int,
    constraint chapters_unique_index unique (book_id, "index")
);

-- ------------------------------------------------------------------ chunks
create table public.chunks (
    id          uuid primary key default gen_random_uuid(),
    book_id     uuid not null references public.books (id) on delete cascade,
    chapter_id  uuid references public.chapters (id) on delete cascade,
    "index"     int not null,
    text        text not null,
    embedding   extensions.vector(384),
    page        int,
    token_count int,

    -- Denormalised from books by trigger (DECISIONS.md D15). The retrieval query is
    -- the hottest and most security-critical query in the system; carrying scope
    -- here removes a join from it and lets the RLS policy be expressed on the table
    -- actually being queried. Application code must never write these three.
    classroom_id uuid,
    owner_id     uuid not null,
    scope        public.book_scope not null,

    constraint chunks_unique_index unique (book_id, "index")
);

create index chunks_embedding_idx on public.chunks
    using hnsw (embedding extensions.vector_cosine_ops);
create index chunks_book_index_idx on public.chunks (book_id, "index");
create index chunks_classroom_scope_idx on public.chunks (classroom_id, scope);
create index chunks_personal_owner_idx on public.chunks (owner_id) where scope = 'personal';

create or replace function public.sync_chunk_scope()
returns trigger
language plpgsql
set search_path = public
as $$
begin
    select b.classroom_id, b.owner_id, b.scope
      into new.classroom_id, new.owner_id, new.scope
      from public.books b
     where b.id = new.book_id;
    return new;
end;
$$;

-- Whatever the caller supplied for these columns is overwritten by the book's own
-- values. A chunk cannot be inserted into a scope its book does not have.
create trigger chunks_sync_scope
    before insert or update on public.chunks
    for each row execute function public.sync_chunk_scope();

-- If a book moves classroom or changes scope, its chunks follow in the same
-- transaction. Otherwise the denormalised copy becomes a stale access grant.
create or replace function public.propagate_book_scope()
returns trigger
language plpgsql
set search_path = public
as $$
begin
    if new.classroom_id is distinct from old.classroom_id
       or new.scope is distinct from old.scope then
        update public.chunks
           set classroom_id = new.classroom_id, scope = new.scope
         where book_id = new.id;
    end if;
    return new;
end;
$$;

create trigger books_propagate_scope
    after update on public.books
    for each row execute function public.propagate_book_scope();

-- ------------------------------------------------------------- privileges
grant select, insert, update, delete on public.books to authenticated;
grant select on public.chapters to authenticated;
grant select on public.chunks to authenticated;
grant all on public.books, public.chapters, public.chunks to service_role;

-- --------------------------------------------------------------------- RLS
alter table public.books enable row level security;
alter table public.chapters enable row level security;
alter table public.chunks enable row level security;

-- The boundary, in SQL. Note there is no branch that lets a teacher reach a
-- personal row: the only paths in are "it is mine" and "it is canon in a classroom
-- I teach or am actively enrolled in".
create policy "books_select_owner_or_canon_member"
    on public.books for select
    using (
        owner_id = (select auth.uid())
        or (
            scope = 'canon'
            and (
                public.is_classroom_teacher(classroom_id)
                or public.is_active_enrollee(classroom_id)
            )
        )
    );

create policy "books_insert_own"
    on public.books for insert
    with check (owner_id = (select auth.uid()));

create policy "books_update_own"
    on public.books for update
    using (owner_id = (select auth.uid()))
    with check (owner_id = (select auth.uid()));

create policy "books_delete_own"
    on public.books for delete
    using (owner_id = (select auth.uid()));

-- Chapters inherit their book's visibility. The subquery is itself filtered by the
-- policy above, so this stays one rule rather than a copy of it.
create policy "chapters_follow_book"
    on public.chapters for select
    using (exists (select 1 from public.books b where b.id = book_id));

-- Chunks express the same rule directly on their own columns -- which is the whole
-- point of denormalising them.
create policy "chunks_select_owner_or_canon_member"
    on public.chunks for select
    using (
        owner_id = (select auth.uid())
        or (
            scope = 'canon'
            and (
                public.is_classroom_teacher(classroom_id)
                or public.is_active_enrollee(classroom_id)
            )
        )
    );
