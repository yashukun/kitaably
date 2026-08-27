-- Lexical full-text search over chunk text (DECISIONS.md D22).
--
-- "Does this book mention X" and "find every mention of X" are lexical questions.
-- Vector similarity finds passages ABOUT a topic; it does not find every passage
-- NAMING one, and a mention query answered semantically misses exactly the literal
-- occurrences the reader asked to be shown. The lookup retrieval path therefore
-- runs websearch_to_tsquery against this index and fuses the result with the
-- vector hits (backend/app/rag/retrieve.py :: search_chunks_lexical).
--
-- An expression index rather than a stored tsvector column, deliberately: chunks
-- are write-once at ingest, the expression is recomputed only for rows being
-- indexed or matched, and a generated column would widen the hottest table in the
-- system for a value nothing else reads. The query side must use the identical
-- expression -- to_tsvector('english', text) -- or the index is silently unused.
--
-- No grant and no policy change: this is an index on a table whose grants and RLS
-- already say who may read it. Scope enforcement for the new query path is the
-- same as for the vector path -- build_retrieval_filter() first, RLS second.

create index chunks_text_search_idx on public.chunks
    using gin (to_tsvector('english', text));
