-- Review before release becomes the default.
--
-- The column has defaulted to 'immediate' since Phase 6, which made the review gate
-- opt-in: a paper created without naming a policy released its marks the moment
-- grading finished. That is backwards for a product whose stated shape is that the
-- author holds authority over results -- the sitter saw their score before anybody
-- had looked at the proctoring evidence, and the gate only applied to authors who
-- knew to ask for it.
--
-- Existing rows are deliberately NOT backfilled. A live paper somebody is part-way
-- through sitting should not change its rules underneath them, and 'immediate' is
-- what those authors got -- by omission, but it is still what their sitters were
-- told. New papers get the safer default; old ones keep their promise.
--
-- Still a choice: `immediate` remains a valid value and the create form offers it,
-- because a low-stakes practice quiz that withholds its own score is just annoying.

alter table public.assessments
    alter column results_release set default 'on_review'::public.results_release;

comment on column public.assessments.results_release is
    'When a graded attempt becomes visible to the person who sat it. Defaults to '
    '`on_review` (20260902160000): the author releases it explicitly. `immediate` '
    'releases at grading time and is the author''s deliberate opt-out.';
