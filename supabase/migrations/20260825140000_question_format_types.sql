-- Phase 5b — the question-format taxonomy, part 1 of 2: the types only.
--
-- Split across two files on purpose. `alter type ... add value` may run inside a
-- transaction on Postgres 12+, but the value it adds **cannot be used in the same
-- transaction** -- and the Supabase CLI wraps each migration file in one. So a file
-- that both adds `'match'` and writes a check constraint mentioning `'match'` fails
-- with `unsafe use of new value "match"`. Types here; everything that uses them in
-- 20260825141000.
--
-- ------------------------------------------------------------------------------
-- The taxonomy has two axes, and keeping them apart is the whole design.
--
--   FORMAT  is the shape the author picks and the sitter sees: a true/false pair, a
--           fill-in-the-blank with options, a match-the-following grid, a flashcard.
--           Fourteen of them, each with its own prompt and its own renderer.
--
--   TYPE    is the grading family -- one code path each. Six. Many formats share
--           one: true_false, yes_no, fill_blank, assertion_reason, scenario and
--           flashcard are all graded exactly as an mcq is, because they all come
--           down to one correct key among several.
--
-- Collapsing them into one enum was the tempting move and it is wrong in both
-- directions: fourteen grading paths nobody can test, or six formats that cannot
-- express what a paper actually looks like. See DECISIONS.md D25.

-- --------------------------------------------------------------- grading families
-- `mcq` and `subjective` already exist. These are the four that were missing, and
-- each one buys a genuinely different marking rule, not a different presentation.
alter type public.question_type add value if not exists 'multi_select';
alter type public.question_type add value if not exists 'short_text';
alter type public.question_type add value if not exists 'match';
alter type public.question_type add value if not exists 'sequence';

-- ------------------------------------------------------------- cognitive level
-- `difficulty` was recall/understand/apply -- the bottom three rungs of Bloom's
-- ladder. A paper that can ask somebody to *evaluate an argument* or *design a
-- solution* needs the top three too, and naming them is what lets the author ask
-- for them: "three questions at `evaluate`" is a request a prompt can carry.
--
-- The column keeps its name. Renaming it to `cognitive_level` would be more honest
-- and would rewrite eleven migrations' worth of downstream references for a word.
alter type public.difficulty add value if not exists 'analyze';
alter type public.difficulty add value if not exists 'evaluate';
alter type public.difficulty add value if not exists 'create';

-- ------------------------------------------------------------------- the formats
--
-- Grouped by the family that marks them:
--
--   mcq           mcq, true_false, yes_no, fill_blank, assertion_reason,
--                 scenario, flashcard
--   multi_select  multi_select
--   match         match
--   sequence      sequence
--   short_text    one_word, numeric
--   subjective    short_answer, long_answer
--
-- Deliberately absent: `definition`, `key_term`, `acronym`, `synonym`,
-- `quote_completion`, `code_output`, `case_study` and the rest of that long tail.
-- Every one of them is one of these fourteen shapes with a different *instruction* --
-- a definition question is a `one_word` or a `short_answer`; predicting code output
-- is an `mcq`. They are steered by the cognitive level and the author's free-text
-- brief, not by a fifteenth enum value that renders identically to an existing one.
create type public.question_format as enum (
    'mcq',
    'true_false',
    'yes_no',
    'fill_blank',
    'assertion_reason',
    'scenario',
    'flashcard',
    'multi_select',
    'match',
    'sequence',
    'one_word',
    'numeric',
    'short_answer',
    'long_answer'
);

-- ---------------------------------------------------------------------- rigor
-- How hard, as distinct from what kind of thinking. `difficulty` says whether the
-- reader must recall or evaluate; this says whether they are a beginner or sitting
-- a graduate viva. The same `evaluate`-level question is written differently for
-- each, and a paper has one setting for all of it.
create type public.assessment_rigor as enum (
    'beginner',
    'easy',
    'medium',
    'hard',
    'expert',
    'competitive',
    'interview',
    'graduate',
    'research'
);
