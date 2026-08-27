-- The generation pipeline trace: what actually ran when a paper was written.
--
-- The tutor already has this (the Advanced panel on a chat answer), and the reasoning
-- transfers: an author staring at a paper that took nine minutes and came back short
-- deserves the same view the developer gets from the worker log — which they cannot
-- read. Chat's trace rides the SSE stream and is never persisted; generation runs in
-- a Celery worker long after the author's request returned, so this one is a column.
--
-- **Content-free by construction.** A sitter with an attempt can SELECT this row
-- (`assessments_select_author_or_sitter`), row-level security cannot hide a column,
-- and PostgREST is exposed -- so nothing question-shaped may ever be stored here. The
-- trace carries counts, format names, durations, reject-reason strings and exception
-- class names, and the recorder in app/services/generation_trace.py accepts nothing
-- else. Our API additionally serialises it only on the author's detail endpoint; the
-- column being sitter-readable is defence in depth being honest about its second
-- layer, not the plan.
--
-- One column, not an events table: a trace is read as a whole, belongs to exactly one
-- generation run, and is overwritten by the next run the same way the questions are.

alter table public.assessments add column generation_trace jsonb;

comment on column public.assessments.generation_trace is
    'The pipeline trace of the last generation run: steps with timings, one entry per '
    'LLM call, and a summary. Versioned (kitaably.generation-trace.v1). Deliberately '
    'content-free -- counts, formats, durations and reasons, never question text -- '
    'because a sitter can read this row and RLS cannot hide a column.';
