-- A question can now come from the book itself, not only from the model.
--
-- A textbook is full of questions already — the numbered exercise at the end of the
-- chapter, the "what do you observe" beside the experiment — and they are better than
-- anything a 3B model writes about the same passage: written by whoever wrote the
-- book, pitched at the level the chapter is pitched at, and already familiar to
-- somebody revising from it. Generation now takes them (DECISIONS.md D31).
--
-- This is a separate origin rather than `generated` because the difference is one an
-- author must be able to see. A generated question is the model's first draft and the
-- author is its author of record (D11). A harvested one is the book's, reproduced —
-- which is a different thing to publish, a different thing to edit, and a different
-- thing to defend if a sitter disputes it. Folding the two together would make the
-- provenance column a lie about half its rows.
--
-- `edited` still wins when a human changes one, whichever it started as: the question
-- on the paper is then neither the model's nor the book's.

alter type public.question_origin add value if not exists 'harvested';

comment on type public.question_origin is
    'Where a question came from. generated: written by the model from a passage. '
    'harvested: printed in the book and reproduced, with the answer supplied. '
    'written: typed by the author. edited: changed by a human after either.';
