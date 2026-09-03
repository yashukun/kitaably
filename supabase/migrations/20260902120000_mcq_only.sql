-- MCQ only. Fourteen formats and six grading families collapse to one of each.
--
-- WHY THE ENUMS ARE REPLACED RATHER THAN CONSTRAINED. Postgres has no
-- `ALTER TYPE ... DROP VALUE`. A value left in the type is a value some future
-- migration, psql session or stale application binary can still write, and the point
-- of this change is that `short_answer` stops existing -- so the type containing it
-- stops existing too. A CHECK saying `format = 'mcq'` would leave fourteen values
-- reachable and defend them with one line; this leaves one value reachable.
--
-- WHY THE RENAME DANCE. The narrowed type must end up carrying the ORIGINAL name.
-- `Question.type` and `Question.format` bind `public.question_type` and
-- `public.question_format` by name, and a stale name does not fail on SELECT -- it
-- fails the first time the column is bound as a parameter, because asyncpg renders
-- `$1::public.question_format` itself. So: rename the old type aside, create the narrow
-- one under the real name, convert, drop the old. `tests/test_enum_names.py` exists
-- because that bug already happened once, to `profiles.role`.
--
-- ONE TRANSACTION. Half of this leaves `questions` with no views on it, and the views
-- are the sitting path and the answer key.
--
-- See DECISIONS.md D32, which reverses the fourteen-format half of D25.

-- ==================================================== 1. the rows that cannot survive
--
-- A narrowed enum has no room for them, so they leave `public.questions` whichever
-- policy is chosen. The only real decision is whether they leave with a copy. They do.
--
-- Not a soft delete on `questions`: a nullable `retired_at` would leave the row in the
-- table, and the row is precisely what the new type cannot represent.
create table public.retired_questions (
    id               uuid primary key,
    -- Deliberately NOT a foreign key. If the assessment is later deleted the archive
    -- should outlive it; a cascade here would defeat the entire point of the copy.
    assessment_id    uuid not null,
    "index"          int not null,
    -- text, not the enums: the values these rows hold are the ones being dropped.
    type             text not null,
    format           text not null,
    stem             text not null,
    options          jsonb,
    correct_option   text,
    prompt_items     jsonb,
    answer_key       jsonb,
    model_answer     text,
    rubric           jsonb,
    points           numeric(10, 2) not null,
    difficulty       text,
    source_chunk_ids jsonb not null,
    origin           text not null,
    created_at       timestamptz not null,
    retired_at       timestamptz not null default now()
);

comment on table public.retired_questions is
    'Questions in formats this system no longer supports, copied out before '
    'question_format and question_type were narrowed to mcq (20260902120000). Columns '
    'are text rather than enums on purpose: the values these rows hold no longer exist '
    'as enum values. Read-only history. No grant and no policy, so nothing but the '
    'service role reaches it.';

-- RLS on with no policy at all: `authenticated` sees zero rows. Combined with the
-- absent GRANT that is two independent refusals, which is the shape CLAUDE.md asks for.
alter table public.retired_questions enable row level security;

insert into public.retired_questions (
    id, assessment_id, "index", type, format, stem, options, correct_option,
    prompt_items, answer_key, model_answer, rubric, points, difficulty,
    source_chunk_ids, origin, created_at
)
select q.id, q.assessment_id, q."index", q.type::text, q.format::text, q.stem,
       q.options, q.correct_option, q.prompt_items, q.answer_key, q.model_answer,
       q.rubric, q.points, q.difficulty::text, q.source_chunk_ids, q.origin::text,
       q.created_at
from public.questions q
where q.format <> 'mcq' or q.type <> 'mcq';

-- A paper that loses a question is no longer the paper anybody sat: `max_score` was
-- frozen at publish and now exceeds the sum of the surviving points. Close it, so no
-- NEW sitting can start against it. Attempts already taken keep their score and
-- max_score untouched -- those are historical facts about a paper that existed.
update public.assessments a
set status = 'closed',
    closes_at = coalesce(a.closes_at, now()),
    generation_note = concat_ws(' ',
        nullif(a.generation_note, ''),
        'This paper contained question types this system no longer supports. Those '
        'questions have been removed and the paper closed. Marks already awarded are '
        'unchanged.')
where a.status in ('draft', 'generating', 'published')
  and exists (select 1 from public.retired_questions r where r.assessment_id = a.id);

-- Cascades to public.answers through answers.question_id ... on delete cascade. That
-- is intended, and it is exactly why the copy above happens first.
delete from public.questions q
where q.format <> 'mcq' or q.type <> 'mcq';

-- ============================================== 2. drop what depends on the types
--
-- Both views select q.type and q.format, so both block ALTER COLUMN TYPE. They are
-- recreated verbatim at the end -- same columns in the same order, same
-- security_invoker, same predicate, same grant, same comment.
drop view public.question_sit;
drop view public.question_key;

-- The family constraint reads both columns and must go before either changes type.
-- The four family shape constraints name `type` values that are about to stop
-- existing, so a CHECK mentioning 'multi_select' could not be re-parsed against the
-- narrowed type. (These four were dropped and re-added once already, in
-- 20260825142000; the names are unchanged.)
alter table public.questions
    drop constraint questions_format_matches_family,
    drop constraint questions_multi_select_shape,
    drop constraint questions_short_text_shape,
    drop constraint questions_match_shape,
    drop constraint questions_sequence_shape;

-- From the FIRST assessments migration rather than the format one, and easy to miss
-- for exactly that reason: it names 'subjective', which is also going away.
alter table public.questions
    drop constraint questions_subjective_shape;

-- `questions_mcq_shape` (20260824150000) survives untouched and now carries the whole
-- shape rule: an mcq needs options and a correct_option.

-- ===================================================== 3. narrow question_format
--
-- The default goes first. A column default is stored already cast to the old type, and
-- ALTER COLUMN TYPE will not rewrite it.
alter table public.questions alter column format drop default;

alter type public.question_format rename to question_format_old;

create type public.question_format as enum ('mcq');

comment on type public.question_format is
    'One shape. The thirteen others were removed in 20260902120000 (DECISIONS.md D32). '
    'Adding a value back means adding a spec in app/rag/formats.py, a family branch in '
    'build_question_fields, a grader, a renderer, and a branch in '
    'questions_format_matches_family -- in the same change.';

-- Every surviving row is already 'mcq' (the DELETE above guaranteed it), so this
-- conversion cannot fail. Written as an explicit text round-trip because there is no
-- assignment cast between two distinct enum types.
alter table public.questions
    alter column format type public.question_format
    using format::text::public.question_format;

alter table public.questions
    alter column format set default 'mcq'::public.question_format;

drop type public.question_format_old;

-- ======================================================= 4. narrow question_type
alter type public.question_type rename to question_type_old;

create type public.question_type as enum ('mcq');

comment on type public.question_type is
    'The grading family: one value, one marking function in services/grading.py. Adding '
    'a value here is the expensive direction -- a family owns answers already stored, '
    'and retiring one later means rewriting somebody''s result.';

alter table public.questions
    alter column type type public.question_type
    using type::text::public.question_type;

drop type public.question_type_old;

-- ================================================ 5. the family rule, narrowed
--
-- Both columns are single-valued now, so no row Postgres accepts could violate this.
-- It stays anyway, and deliberately: it is the line a future fifteenth value has to
-- walk past. Kept as a CASE with `else false` rather than a flat equality for the
-- reason 20260825141000 gives -- a CASE with no ELSE yields NULL and a CHECK passes on
-- NULL, so `else false` is what makes an unlisted format a refused row rather than a
-- silently unconstrained one.
alter table public.questions
    add constraint questions_format_matches_family check (
        case format
            when 'mcq' then type = 'mcq'
            else false
        end
    );

-- ============================================== 6. the views, recreated verbatim
--
-- Identical to 20260825141000: same select list in the same order, same
-- security_invoker = false, same predicate, same grant, same comment. A recreated view
-- loses its grants, so both are re-granted.
--
-- `prompt_items` and `answer_key` stay in their views though an mcq populates neither.
-- The columns still exist and still hold values on rows written before this migration;
-- narrowing the projection is a separate decision from narrowing the enum, and a view
-- that quietly stopped selecting `answer_key` would change what `question_key` means to
-- the result screen.
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
