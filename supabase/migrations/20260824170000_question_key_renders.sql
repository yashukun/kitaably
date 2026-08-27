-- Fix: an author reading a marked paper got a result with no answers in it.
--
-- `result_view` built its list by walking `public.question_sit` and joining each
-- question to its answer. That view is scoped by `has_attempt_on()` -- it is the
-- SITTING projection -- so it is correctly empty for the author, who wrote the paper
-- and never sat it. The score came back right and the breakdown was blank.
--
-- The two audiences for a marked paper are the author and a sitter whose result has
-- been released, and `public.question_key` already admits exactly those two through
-- `may_see_answer_key()`. It simply could not render one: no ordering column and no
-- stem. Adding them makes it self-sufficient, so a marked paper has one source
-- instead of being stitched from a view that answers a different question.
--
-- DROP and CREATE rather than CREATE OR REPLACE: replace can only append columns, and
-- `index` and `stem` belong beside the row they describe.
drop view public.question_key;

create view public.question_key
with (security_invoker = false) as
select
    q.id,
    q.assessment_id,
    q."index",
    q.type,
    q.stem,
    q.correct_option,
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
