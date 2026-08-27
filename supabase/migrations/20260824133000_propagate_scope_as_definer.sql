-- Fix: sharing a book failed with "permission denied for table chunks".
--
-- `propagate_book_scope()` is a trigger on `books` that carries a scope change down
-- to that book's chunks. It was written as a plain plpgsql function, so it runs as
-- the INVOKER -- and the invoker on the request path is `authenticated`, which holds
-- SELECT on `chunks` and nothing more. The trigger's UPDATE was refused, and with it
-- the whole transaction.
--
-- It had never fired under that role before. Scope used to be fixed at insert time
-- and never changed afterwards, so the only writer was the worker, which connects as
-- service_role and bypasses everything. Making sharing a thing a user does turned a
-- dormant path into the main one.
--
-- The wrong fix is `grant update on public.chunks to authenticated`. Two reasons:
--
--   1. RLS has no UPDATE policy on `chunks`, deliberately -- the denormalised scope
--      columns are trigger-maintained and no request should write them. A grant
--      without a policy does not error, it matches zero rows. The share would appear
--      to succeed and the chunks would silently keep the old scope, which is a stale
--      access grant: material the owner believes is private, still answering.
--
--   2. It would hand every user a write privilege on the retrieval table to fix a
--      problem that is really about which principal one trigger runs as.
--
-- SECURITY DEFINER is the narrow fix. The function takes no arguments, derives
-- everything from the row being updated, and can only be reached by an UPDATE that
-- `books_update_own` has already restricted to that book's owner. A trigger function
-- returns `trigger` and cannot be called directly, so there is no second way in.
create or replace function public.propagate_book_scope()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
    -- Sharing or unsharing a book changes who may read its chunks. The denormalised
    -- copy has to move in the same transaction or it becomes a stale access grant.
    if new.scope is distinct from old.scope then
        update public.chunks
           set scope = new.scope
         where book_id = new.id;
    end if;
    return new;
end;
$$;

comment on function public.propagate_book_scope() is
    'SECURITY DEFINER because it writes chunks, which authenticated may only read. '
    'Authorization happened before it ran: books_update_own restricts the UPDATE '
    'that fires it to the book owner, and this function adds no reach beyond that '
    'one book.';
