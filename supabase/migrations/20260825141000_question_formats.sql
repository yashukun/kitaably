-- Phase 5b — the question-format taxonomy, part 2 of 2: columns, shapes, views.
--
-- Separate file because everything here *uses* the values 20260825140000 added, and
-- a new enum value is not usable in the transaction that adds it.

-- ============================================================== what the author asked for
--
-- `generation_spec` records the request, not the result: which formats, which
-- cognitive levels, and any free-text brief. It is kept after generation for two
-- reasons -- an author regenerating a paper should get the same kind of paper, and
-- "why is this full of true/false questions" is a question the row can answer.
--
-- An empty object means *auto*: the author skipped the picker and asked the material
-- to decide. That is a first-class choice, not a missing one -- somebody drafting a
-- quiz from a novel should not have to know that `assertion_reason` exists.
alter table public.assessments
    add column generation_spec jsonb not null default '{}'::jsonb,
    add column rigor public.assessment_rigor not null default 'medium';

comment on column public.assessments.generation_spec is
    '{formats: [...], levels: [...], instructions: "..."} -- the author''s request. '
    'Empty means auto: let the material decide. Validated at generation time; it '
    'arrives from a request body, so it is a claim rather than an authorization.';

-- ================================================================= the question shape
--
-- Three new columns, and which side of the wall each falls on is the whole point.
--
--   format         sitter-visible. It is how the question is drawn.
--   prompt_items   sitter-visible. The left-hand column of a match grid; the
--                  scrambled steps of a sequence. Half the question, not the answer.
--   answer_key     NEVER sitter-visible. Every format-specific correct answer that
--                  is not already `correct_option`.
--
-- One `answer_key` rather than a column per format (`correct_options`,
-- `accepted_answers`, `correct_pairs`, `correct_order`) because the rule that
-- matters here is CLAUDE.md invariant 2: the answer key is enforced by being ABSENT
-- from `public.question_sit`, not by a serializer omitting it. One column is one
-- thing to keep out of one view. Four columns is four chances for the fifth format
-- to add a fifth column and forget.
alter table public.questions
    add column format public.question_format not null default 'mcq',
    add column prompt_items jsonb,
    add column answer_key jsonb;

comment on column public.questions.answer_key is
    'Format-specific correct answers: {correct_options:[...]} for multi_select, '
    '{accepted:[...], tolerance: n} for short_text, {pairs:{left_key:right_key}} for '
    'match, {order:[...]} for sequence. Absent from public.question_sit by '
    'construction -- RLS cannot hide a column, so the sitter never reaches the row.';

comment on column public.questions.prompt_items is
    'Sitter-visible half of a two-sided question: the left column of a match grid, '
    'the scrambled items of a sequence. [{key, text}]. Not an answer.';

-- ------------------------------------------------------------------ backfill
--
-- Every question written before this migration is one of two things, and the column
-- default can only be right about one of them. A `subjective` question left at the
-- default `format = 'mcq'` fails the family constraint added below -- so the ALTER
-- TABLE would roll back, on a table whose data was perfectly valid a moment earlier.
--
-- `short_answer` rather than `long_answer` because it is the weaker claim: it says
-- "a few sentences, marked to a rubric", which is true of every subjective question
-- ever generated here. The distinction only ever steered generation, and these are
-- already written.
update public.questions set format = 'short_answer' where type = 'subjective';

-- ---------------------------------------------------------------------- shapes
--
-- Each family gets one constraint, keyed on `type` rather than `format`, because
-- the shape is a property of how the thing is marked. Seven formats share the mcq
-- constraint and that is correct -- a true/false question really is an mcq with two
-- options, and giving it its own rule would let the two drift.
--
-- jsonb_exists(...) rather than the `?` operator: `?` is a bind-parameter marker to
-- several drivers, and a constraint that only some tooling can read is a constraint
-- that gets dropped by somebody who could not run it.

alter table public.questions
    add constraint questions_multi_select_shape check (
        type <> 'multi_select'
        or (options is not null and jsonb_exists(answer_key, 'correct_options'))
    ),
    add constraint questions_short_text_shape check (
        type <> 'short_text' or jsonb_exists(answer_key, 'accepted')
    ),
    add constraint questions_match_shape check (
        type <> 'match'
        or (prompt_items is not null and options is not null
            and jsonb_exists(answer_key, 'pairs'))
    ),
    add constraint questions_sequence_shape check (
        type <> 'sequence'
        or (options is not null and jsonb_exists(answer_key, 'order'))
    );

-- `format` and `type` must agree, or a paper renders as one thing and marks as
-- another -- a `match` drawn as a grid and marked by string equality scores zero for
-- everybody. The mapping lives in app/rag/formats.py as well; this is the copy that
-- holds when the application is wrong, and tests/test_formats.py pins them together.
alter table public.questions
    add constraint questions_format_matches_family check (
        case format
            when 'mcq'              then type = 'mcq'
            when 'true_false'       then type = 'mcq'
            when 'yes_no'           then type = 'mcq'
            when 'fill_blank'       then type = 'mcq'
            when 'assertion_reason' then type = 'mcq'
            when 'scenario'         then type = 'mcq'
            when 'flashcard'        then type = 'mcq'
            when 'multi_select'     then type = 'multi_select'
            when 'match'            then type = 'match'
            when 'sequence'         then type = 'sequence'
            when 'one_word'         then type = 'short_text'
            when 'numeric'          then type = 'short_text'
            when 'short_answer'     then type = 'subjective'
            when 'long_answer'      then type = 'subjective'
            -- A CASE with no ELSE yields NULL, and a CHECK passes on NULL. A
            -- fifteenth format added without a branch here would be silently
            -- unconstrained, which is exactly the drift this rule exists to stop.
            else false
        end
    );

comment on column public.answers.response is
    'The option key for mcq, prose for subjective, and compact JSON for the '
    'structured families: ["A","C"] for multi_select, {"L1":"A"} for match, '
    '["C","A","B"] for sequence. One column rather than two, so there is never a '
    'question about which one holds the answer.';

-- ==================================================== the sitter''s view, widened
--
-- DROP and CREATE rather than CREATE OR REPLACE: replace can only append columns,
-- and `format` belongs beside `type` where a reader will look for it.
--
-- What is still not here: correct_option, model_answer, rubric, and now answer_key.
-- The absence is the enforcement.
drop view public.question_sit;

create view public.question_sit
with (security_invoker = false) as
select
    q.id,
    q.assessment_id,
    q."index",
    q.type,
    q.format,
    q.stem,
    q.options,
    q.prompt_items,
    q.points,
    q.difficulty
from public.questions q
where public.has_attempt_on(q.assessment_id);

grant select on public.question_sit to authenticated;

comment on view public.question_sit is
    'The sitting projection. correct_option, model_answer, rubric and answer_key are '
    'absent by construction -- RLS cannot hide a column, so a sitter has no policy on '
    'questions at all and reaches a question only through here.';

-- ----------------------------------------------------------------- the answer key
--
-- Gains `options` and `prompt_items` as well as `answer_key`, because this view is
-- what renders a MARKED paper. Without the options it could say the right answer was
-- "B" and not what B said, which is a result screen nobody can learn anything from.
drop view public.question_key;

create view public.question_key
with (security_invoker = false) as
select
    q.id,
    q.assessment_id,
    q."index",
    q.type,
    q.format,
    q.stem,
    q.options,
    q.prompt_items,
    q.correct_option,
    q.answer_key,
    q.model_answer,
    q.rubric,
    q.points
from public.questions q
where public.may_see_answer_key(q.assessment_id);

grant select on public.question_key to authenticated;

comment on view public.question_key is
    'A marked paper, for the two audiences entitled to one: the author always, and a '
    'sitter only after their OWN result is released. Releasing one person''s result '
    'does not open the key to anybody else still sitting.';
