-- Phase 4 revisited — the tutor learns to hold a conversation.
--
-- WHAT LANDS HERE
--   books.kind / genre / summary   what sort of book this is, filled by ingest
--   chat_messages.intent           what the reader was doing; "hi" is not a question
--   chat_sessions.last_message_at  so the session list sorts by activity, not birth
--
-- WHAT DELIBERATELY DOES NOT LAND: a genre filter on retrieval.
--
-- The temptation is to classify a book into a library taxonomy and then narrow the
-- vector search to the matching category. Do not. A reader's library is tens of
-- books, not millions, and the embedding already separates a chunk about enthalpy
-- from a chunk about a wedding. What a category filter adds is a new way to lose:
-- misclassify one book and its material becomes unreachable, with no error and no
-- empty result to notice -- just a tutor that has started refusing questions it used
-- to answer. That is the most expensive failure this system has, because it looks
-- exactly like correct grounded-refusal behaviour (CLAUDE.md invariant 5).
--
-- `kind` therefore has exactly one job: register. You do not explain a novel the way
-- you explain a thermodynamics textbook. Four values, because four can be classified
-- reliably by a small local model and sixty cannot. Which book answers a question is
-- decided from the retrieved chunks themselves (app/rag/rank.py), where the evidence
-- is, and never from a label written months earlier.

create type public.book_kind as enum ('fiction', 'nonfiction', 'academic', 'reference');

-- Nullable on purpose: a book is readable and answerable before it is classified, and
-- classification is a best-effort LLM call that is allowed to fail without failing the
-- ingest. NULL means "not classified", which the prompt reads as a neutral register.
alter table public.books
    add column kind    public.book_kind,
    -- Free text, not an enum. This is the fine-grained label from your taxonomy
    -- ("Organic chemistry", "Historical fiction"). It is shown to the reader and used
    -- in the "where to read more" block. Nothing branches on it, nothing filters on
    -- it, so a wrong value is a cosmetic error rather than a retrieval hole -- which
    -- is precisely why it is allowed to be free text.
    add column genre   text,
    -- One or two sentences, written from the opening chunks. Two jobs: the book
    -- picker in chat shows it, and the tutor is told what the book is about so a
    -- citation can be introduced ("from your organic chemistry text") rather than
    -- dumped as a bare title.
    add column summary text;

-- What the reader was doing. `chat_messages.intent` is set on the user's row, and it
-- decides the shape of the whole turn:
--
--   question     retrieve, then answer from what came back
--   follow_up    condense against the transcript FIRST, then retrieve
--   greeting     no retrieval, no LLM content call, a warm fixed reply
--   chitchat     as above, and a nudge back toward the books
--   meta         about Kitaably itself; answered from what the server knows
--   unclear      empty of intent; ask for a real question rather than refusing
--
-- Before this existed, every one of those went through vector search, found nothing
-- above threshold, and got "Your books don't cover that." Correct code, terrible
-- product: a greeting is not a failed question.
create type public.message_intent as enum (
    'question', 'follow_up', 'greeting', 'chitchat', 'meta', 'unclear'
);

alter table public.chat_messages
    add column intent public.message_intent;

-- Sessions sort by their most recent message, not their creation. A conversation you
-- returned to yesterday belongs above one you opened last week and abandoned.
alter table public.chat_sessions
    add column last_message_at timestamptz not null default now();

create index chat_sessions_recent_idx
    on public.chat_sessions (user_id, last_message_at desc);

-- New columns on an existing table inherit that table's grants, so `authenticated`
-- can already read all of the above. Nothing to add -- but note that chat_messages
-- still has no UPDATE or DELETE grant for anyone, which is the point: a transcript
-- that can be edited after the fact is not a transcript. The assistant's row is
-- therefore written ONCE, complete, after the answer finishes.
