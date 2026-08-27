-- A paper that came back shorter than it was asked for must say so.
--
-- `assessments.error` already exists and is the wrong column for this: it means
-- generation FAILED, the UI renders it as a failure, and the row lands back in `draft`
-- with nothing in it. A paper that was asked for ten questions and produced one did not
-- fail — it produced a paper, and the author is entitled to know it is short and why.
--
-- Before this column there was no way to tell them. A one-question paper arrived with
-- `error` null, looking exactly like a one-question paper somebody meant to write, and
-- the only signal that anything had happened was a number the author had to remember
-- they had typed. That is the same class of bug as a spinner that never resolves: a
-- known failure reported to nobody.
--
-- Two columns, one meaning each. `error`: it failed, there is nothing here. `note`: it
-- worked, and here is what you should know about the result.

alter table public.assessments add column generation_note text;

comment on column public.assessments.generation_note is
    'A user-facing note about a SUCCESSFUL generation that fell short -- which formats '
    'the material could not support, and how many questions came back. Null when the '
    'paper is what was asked for. Distinct from `error`, which means it failed.';
