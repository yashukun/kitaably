-- The ingest pipeline trace: what actually ran while a book was being read.
--
-- The third of these, and the reasoning is the same one every time (the tutor's
-- Advanced panel, then assessments.generation_trace): work that happens in a
-- Celery worker is invisible to the person waiting on it, and "Indexing…" for
-- four minutes is not a report. The owner of a book that took nine minutes, or
-- came back with a chapter missing, deserves the view the developer gets from
-- `docker compose logs worker` -- which they cannot read.
--
-- ZIP uploads (D26) are what made this urgent rather than nice: a ZIP is N files
-- becoming one book, and the questions it raises -- did it find all eighteen
-- chapters, in what order, did it skip one -- are answerable only by whatever
-- walked the archive.
--
-- **Content-free by construction.** A canon book's row is readable by every
-- signed-in user and row-level security cannot hide a column, so nothing from
-- inside the book may be stored here. The recorder in app/services/ingest_trace.py
-- accepts counts, durations, format names, member FILENAMES and fixed reason
-- strings, and nothing else -- never page text, never chunk text. Our API is
-- stricter still and serialises this only to the book's owner; the column being
-- readable by others is the second layer being honest, not the plan.
--
-- One column, not an events table: a trace is read as a whole, belongs to exactly
-- one ingest run, and is replaced wholesale by the next run the way chunks are.

alter table public.books add column ingest_trace jsonb;

comment on column public.books.ingest_trace is
    'The pipeline trace of the last ingest run: steps with timings, the archive '
    'manifest for a ZIP, and a summary. Versioned (kitaably.ingest-trace.v1). '
    'Deliberately content-free -- counts, durations, filenames and reasons, never '
    'page or chunk text -- because a canon book is readable by every signed-in '
    'user and RLS cannot hide a column.';
