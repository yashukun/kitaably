-- Fix: the four Phase 5b shape constraints passed on a NULL answer key.
--
-- 20260825141000 wrote them like this:
--
--     check (type <> 'match' or (prompt_items is not null and options is not null
--                                and jsonb_exists(answer_key, 'pairs')))
--
-- `jsonb_exists(NULL, 'pairs')` is **NULL, not false** — it is a strict function and a
-- NULL input gives a NULL output. So for a match question the expression evaluates to
-- `false OR (true AND true AND NULL)` = NULL, and **a CHECK constraint passes on NULL**.
-- A match grid, a select-all, an ordering or a typed answer with no answer key at all
-- was accepted by the database.
--
-- It was demonstrated by inserting one. Nothing in the application can produce such a
-- row — `build_question_fields` refuses every one of them, with a reason — but that is
-- exactly the argument these constraints exist to not have to make. They are the second
-- line, for when the application is wrong; a second line that evaluates to NULL is
-- decorative.
--
-- The neighbouring `questions_mcq_shape` from 20260824150000 was never affected, and the
-- difference is instructive: it is written entirely in `is not null` tests, which are
-- themselves never NULL. Three-valued logic only bites once a strict function is in the
-- chain.
--
-- The fix is an explicit `answer_key is not null` in front of every `jsonb_exists`.
-- `X is not null` yields false rather than NULL, and `false AND NULL` is false — so the
-- chain short-circuits to a definite false and the row is refused.

alter table public.questions
    drop constraint questions_multi_select_shape,
    drop constraint questions_short_text_shape,
    drop constraint questions_match_shape,
    drop constraint questions_sequence_shape;

alter table public.questions
    add constraint questions_multi_select_shape check (
        type <> 'multi_select'
        or (options is not null
            and answer_key is not null
            and jsonb_exists(answer_key, 'correct_options'))
    ),
    add constraint questions_short_text_shape check (
        type <> 'short_text'
        or (answer_key is not null and jsonb_exists(answer_key, 'accepted'))
    ),
    add constraint questions_match_shape check (
        type <> 'match'
        or (prompt_items is not null
            and options is not null
            and answer_key is not null
            and jsonb_exists(answer_key, 'pairs'))
    ),
    add constraint questions_sequence_shape check (
        type <> 'sequence'
        or (options is not null
            and answer_key is not null
            and jsonb_exists(answer_key, 'order'))
    );
