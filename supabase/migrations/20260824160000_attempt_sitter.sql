-- Phase 6 — the author needs to know whose attempt they are looking at.
--
-- A gradebook of anonymous rows is not a gradebook. But the `profiles` policy
-- deliberately admits a user to their own row and no other, so joining `attempts` to
-- `profiles` from the request path returns a name for the caller and NULL for everyone
-- else -- quietly, which is the dangerous part. A join to a table the caller cannot
-- read returns fewer rows, not an error.
--
-- Widening the profiles policy would work and would be wrong: it would hand every user
-- every column of every other user's row to solve a problem about one screen. This view
-- exposes exactly the two fields a gradebook needs, to exactly the person entitled to
-- them.
--
-- security_invoker = false: the view runs as its owner, so RLS on the underlying tables
-- does not apply. The WHERE clause is therefore load-bearing rather than decorative --
-- it is the only thing scoping this to papers the caller actually wrote.
create view public.attempt_sitter
with (security_invoker = false) as
select
    t.id      as attempt_id,
    t.assessment_id,
    p.id      as sitter_id,
    p.name,
    p.email
from public.attempts t
join public.profiles p on p.id = t.sitter_id
where public.is_assessment_author(t.assessment_id);

grant select on public.attempt_sitter to authenticated;

comment on view public.attempt_sitter is
    'Name and email of the person behind an attempt, visible only to the assessment '
    'author. Exists because the profiles policy admits a user to their own row only, '
    'so the obvious join silently returns nulls instead of erroring.';
