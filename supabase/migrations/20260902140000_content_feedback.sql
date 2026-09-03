-- Phase 7b — telling somebody the book DOES cover it.
--
-- Two changes, one feature.
--
-- 1. `chat_messages.outcome` — how a turn ended, persisted.
--
--    Until now the outcome lived only on the `pipeline` SSE event, which is built
--    fresh per turn and deliberately never stored. That was fine while it was a
--    diagnostic. It stops being fine the moment a refusal has to offer the reader an
--    action, because after a reload the refusal is indistinguishable from a good
--    answer and the action silently disappears — and reloading is exactly what
--    somebody does on their way to check the book.
--
--    Text rather than an enum. The outcome vocabulary is a UI and telemetry label
--    that the database never dispatches on; making it an enum would mean a migration
--    every time the pipeline learns a new way to end, which is the wrong cost for a
--    column nothing branches on. Nullable, because every row written before today
--    has no answer to give and guessing one would be inventing history.
--
-- 2. `content_feedback` — the reader's report that a refusal was wrong.
--
--    The counter `retrieval_refusals_total` already says refusals happen. It cannot
--    say WHICH question, over WHICH book, and the person who could actually fix it —
--    the owner of the book — never hears about it at all. This is that channel.

alter table public.chat_messages
    add column outcome text;

comment on column public.chat_messages.outcome is
    'How the turn ended: answered, loose, refusal, no_mentions, book_facts, '
    'pick_book, needs_two_books, conversational. Written for assistant turns only. '
    'Text and not an enum on purpose -- nothing in the database dispatches on it, '
    'and a new outcome should not cost a migration. Null on rows written before '
    '20260902140000, and on user turns.';

-- ===================================================================== the report
--
-- What a row means: "this did not work, and here is everything the app knew about
-- why". Two things file one: a reader answering a grounded refusal, and an author
-- whose paper came back empty.
create table public.content_feedback (
    id         uuid primary key default gen_random_uuid(),

    user_id    uuid not null references public.profiles (id) on delete cascade,

    -- Which surface failed: 'chat' or 'generation'. Kept as a column rather than
    -- inferred from which id is populated, because "no assessment_id" and "this was
    -- not a generation problem" are different statements and only one of them is
    -- safe to conclude from a null.
    source     text not null default 'chat',

    -- Set when an assessment failed. Same nullability reasoning as message_id below.
    assessment_id uuid references public.assessments (id) on delete set null,

    -- Nullable and NOT cascading: a report outlives the conversation it came from.
    -- Deleting a chat session should not quietly delete the evidence that a book has
    -- a gap in it, because the gap is a fact about the book rather than about the
    -- conversation.
    message_id uuid references public.chat_messages (id) on delete set null,

    -- The scope that was actually searched, as ids. Denormalised rather than joined
    -- through the message, because the message may be gone and the question "which
    -- books did this fail over" still has to have an answer.
    book_ids   jsonb not null default '[]'::jsonb,

    -- The question that found nothing, as the reader typed it.
    question   text not null,

    -- Which failure this was: refusal, no_mentions, or loose. Same vocabulary as
    -- chat_messages.outcome above and the same reasoning about enums.
    outcome    text not null,

    -- The reader's own words. Optional: "this is in chapter 4" is worth having and so
    -- is a bare report with nothing added.
    note       text,

    -- What the APP knew, captured at the moment of the failure rather than
    -- reconstructed later from logs that have since rotated.
    --
    -- This is the half that makes a report investigable. A user writing "generation
    -- failed" cannot say that every call 404'd because a model was never pulled, or
    -- that each one timed out at 180s -- but the generation trace already knows, and
    -- without it somebody has to reproduce the failure to learn anything.
    --
    -- Content-free by the same rule the trace itself follows: call counts, timings,
    -- failure tags and status codes, never a provider's error string, because those
    -- can quote the prompt and the prompt quotes the book.
    diagnostics jsonb not null default '{}'::jsonb,

    created_at timestamptz not null default now(),

    constraint content_feedback_source_known
        check (source in ('chat', 'generation')),
    constraint content_feedback_question_not_empty check (length(btrim(question)) > 0),
    constraint content_feedback_note_bounded check (note is null or length(note) <= 2000)
);

create index content_feedback_user_idx
    on public.content_feedback (user_id, created_at desc);

-- The owner-side read below is a jsonb containment test over `book_ids`, and it runs
-- inside an RLS policy -- so it is evaluated for every row the reader might see, not
-- once. GIN is what keeps that from being a sequential scan as reports accumulate.
create index content_feedback_books_idx
    on public.content_feedback using gin (book_ids);

comment on table public.content_feedback is
    'Somebody reporting that the app failed them: a reader answering a grounded '
    'refusal, or an author whose paper came back empty. Carries the diagnostics the '
    'app held at the time, so a report can be investigated without reproducing it. '
    'Readable by the person who wrote it and by the owner of any book it names. '
    'Append-only: no update or delete grant for anybody.';

-- ================================================================== privileges
--
-- SELECT and INSERT only. A report is a record of something somebody said at a
-- moment, and a record that can be edited afterwards is not one -- the same rule
-- chat_messages states in this file's ancestor. Deletion is the service role's
-- (retention), not the reporter's.
grant select, insert on public.content_feedback to authenticated;
grant all on public.content_feedback to service_role;

-- ========================================================================== RLS
--
-- Grant and policy are separate layers and both are required: the grant above gives
-- the verb, these decide the rows.
alter table public.content_feedback enable row level security;

-- Reading a report about a book you own means reading `books` from inside a policy on
-- `content_feedback`. That is safe in this direction -- `books`'s own policies do not
-- query `content_feedback`, so there is no cycle to recurse through -- but it is
-- exactly the shape CLAUDE.md warns about, so it is stated rather than left to be
-- rediscovered. A definer helper would be the fix if `books` ever gains a policy that
-- reads back this way.
create policy "content_feedback_select_own_or_my_books"
    on public.content_feedback for select
    using (
        user_id = (select auth.uid())
        or exists (
            select 1
            from public.books b
            where b.owner_id = (select auth.uid())
              and book_ids @> to_jsonb(b.id::text)
        )
    );

-- You may only file a report as yourself. Without the `with check` the grant would
-- let a caller write a row attributed to somebody else, which is a fabricated
-- complaint about a stranger's book.
create policy "content_feedback_insert_own"
    on public.content_feedback for insert
    with check (user_id = (select auth.uid()));
