-- Phases 5 and 6 — assessments, questions, attempts, answers.
--
-- The shape of the thing being built:
--
--   an author generates a paper from canon chunks   -> status='draft'
--   the author fixes it and publishes               -> status='published' + share_token
--   anyone with the URL signs in and sits it        -> one attempt each
--   grading runs on the llm queue                   -> answers.awarded_points
--   the author may overrule any mark                -> grader='human', audited
--
-- **The share token is the whole access grant.** There is no roster and no invitation
-- (DECISIONS.md D16), so possession of the URL is what admits somebody. What the token
-- deliberately does NOT do is establish identity: it is resolved by a SECURITY DEFINER
-- function that returns one assessment id and nothing else, and the sitter is always
-- `auth.uid()`. A token holder can start their own attempt; they can never inherit
-- somebody else's.

-- ------------------------------------------------------------------- enums
create type public.assessment_type   as enum ('mcq', 'subjective', 'mixed');
create type public.assessment_status as enum ('draft', 'generating', 'published', 'closed');
create type public.access_mode       as enum ('link', 'link_password');
create type public.results_release   as enum ('immediate', 'on_review');
create type public.question_type     as enum ('mcq', 'subjective');
create type public.difficulty        as enum ('recall', 'understand', 'apply');
create type public.question_origin   as enum ('generated', 'edited', 'written');
create type public.attempt_status    as enum ('in_progress', 'submitted', 'auto_submitted', 'voided');
create type public.grader            as enum ('auto', 'llm', 'human');

-- ------------------------------------------------------------- share tokens
-- 128 bits of CSPRNG, hex. gen_random_bytes, not random(): random() is a seeded PRNG,
-- so tokens minted in sequence are predictable from one another, and a guessable
-- share token is unauthorised access to a paper.
--
-- Hex rather than base64 because this ends up in a URL a person may retype.
create or replace function public.generate_share_token()
returns text
language sql
volatile
set search_path = public, extensions
as $$
    select encode(extensions.gen_random_bytes(16), 'hex');
$$;

-- ------------------------------------------------------------- assessments
create table public.assessments (
    id                  uuid primary key default gen_random_uuid(),
    author_id           uuid not null references public.profiles (id) on delete cascade,
    title               text not null,
    type                public.assessment_type not null default 'mixed',

    -- {book_ids: [...], chapter_ids: [...]} — what the author chose to draw from.
    -- Validated against scope='canon' at generation time; it arrives from a request,
    -- so it is a claim rather than an authorization.
    source_selection    jsonb not null default '{}'::jsonb,

    question_count      int not null,
    duration_minutes    int,

    status              public.assessment_status not null default 'draft',
    -- Null until publish. Unique so a token names at most one paper.
    share_token         text unique,
    access_mode         public.access_mode not null default 'link',
    access_password_hash text,
    proctoring_enabled  boolean not null default false,
    results_release     public.results_release not null default 'immediate',

    opens_at            timestamptz,
    closes_at           timestamptz,

    -- Frozen at publish, from the sum of question points. Stored rather than computed
    -- so that voiding a question later cannot silently rescale a paper somebody has
    -- already sat.
    max_score           numeric(10, 2),

    -- A user-facing reason, not a stack trace, when generation fails.
    error               text,

    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),

    constraint assessments_published_has_token
        check (status <> 'published' or share_token is not null),
    constraint assessments_window_ordered
        check (opens_at is null or closes_at is null or opens_at < closes_at),
    constraint assessments_question_count_sane
        check (question_count between 1 and 100)
);

create index assessments_author_idx on public.assessments (author_id, created_at desc);
create index assessments_token_idx  on public.assessments (share_token)
    where share_token is not null;

create trigger assessments_touch_updated_at
    before update on public.assessments
    for each row execute function public.touch_updated_at();

-- ---------------------------------------------------------------- questions
create table public.questions (
    id               uuid primary key default gen_random_uuid(),
    assessment_id    uuid not null references public.assessments (id) on delete cascade,
    "index"          int not null,
    type             public.question_type not null,
    stem             text not null,

    -- mcq: [{key, text}]. Null for subjective.
    options          jsonb,
    correct_option   text,

    -- subjective: the reference answer and [{criterion, points}]. Null for mcq.
    model_answer     text,
    rubric           jsonb,

    points           numeric(10, 2) not null default 1,
    difficulty       public.difficulty,

    -- NOT optional. A question whose passage cannot be produced is a question the
    -- author cannot defend when a sitter disputes it.
    source_chunk_ids jsonb not null default '[]'::jsonb,
    origin           public.question_origin not null default 'generated',

    created_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now(),

    constraint questions_unique_index unique (assessment_id, "index"),
    constraint questions_mcq_shape check (
        type <> 'mcq' or (options is not null and correct_option is not null)
    ),
    constraint questions_subjective_shape check (
        type <> 'subjective' or model_answer is not null
    ),
    constraint questions_points_positive check (points > 0)
);

create index questions_assessment_idx on public.questions (assessment_id, "index");

create trigger questions_touch_updated_at
    before update on public.questions
    for each row execute function public.touch_updated_at();

-- ----------------------------------------------------------------- attempts
create table public.attempts (
    id                   uuid primary key default gen_random_uuid(),
    assessment_id        uuid not null references public.assessments (id) on delete cascade,
    sitter_id            uuid not null references public.profiles (id) on delete cascade,

    status               public.attempt_status not null default 'in_progress',
    started_at           timestamptz not null default now(),
    submitted_at         timestamptz,

    -- Computed server-side at start from duration_minutes and closes_at. A client
    -- clock is a suggestion; this column is the fact.
    deadline_at          timestamptz,

    score                numeric(10, 2),
    max_score            numeric(10, 2),
    graded_at            timestamptz,
    -- Null means the sitter sees no marks at all, whatever else is true.
    results_released_at  timestamptz,
    grading_error        text,

    -- One sitting per person per paper (DECISIONS.md open question 2). Lifting this
    -- means dropping the constraint and deciding which attempt counts.
    constraint attempts_one_per_sitter unique (assessment_id, sitter_id)
);

create index attempts_assessment_idx on public.attempts (assessment_id, started_at desc);
create index attempts_sitter_idx     on public.attempts (sitter_id, started_at desc);

-- ------------------------------------------------------------------ answers
create table public.answers (
    id             uuid primary key default gen_random_uuid(),
    attempt_id     uuid not null references public.attempts (id) on delete cascade,
    question_id    uuid not null references public.questions (id) on delete cascade,

    -- The option key for mcq, prose for subjective. Null means unanswered, which
    -- grades to zero without an LLM call.
    response       text,

    awarded_points numeric(10, 2),
    grader         public.grader,
    feedback       text,
    -- Kept even after a human override, so the original machine judgement stays
    -- auditable. That is what makes an override a correction rather than a cover-up.
    llm_rationale  jsonb,

    updated_at     timestamptz not null default now(),

    constraint answers_unique_per_question unique (attempt_id, question_id)
);

create index answers_attempt_idx on public.answers (attempt_id);

create trigger answers_touch_updated_at
    before update on public.answers
    for each row execute function public.touch_updated_at();

-- ==================================================== membership helpers
--
-- These break an RLS recursion. The policy on `assessments` needs to ask "does the
-- caller have an attempt here?", and the policy on `attempts` needs to ask "does the
-- caller own this assessment?". Written as plain subqueries each triggers the other's
-- policy and Postgres raises
--   ERROR: infinite recursion detected in policy for relation ...
--
-- SECURITY DEFINER runs the lookup as the function owner, for whom RLS does not apply,
-- so the cycle is cut. Each is deliberately narrow: one boolean about the *current*
-- caller, leaking nothing else.

create or replace function public.is_assessment_author(target_assessment uuid)
returns boolean
language sql stable security definer set search_path = public
as $$
    select exists (
        select 1 from public.assessments a
        where a.id = target_assessment and a.author_id = (select auth.uid())
    );
$$;

create or replace function public.has_attempt_on(target_assessment uuid)
returns boolean
language sql stable security definer set search_path = public
as $$
    select exists (
        select 1 from public.attempts t
        where t.assessment_id = target_assessment and t.sitter_id = (select auth.uid())
    );
$$;

create or replace function public.assessment_is_open(target_assessment uuid)
returns boolean
language sql stable security definer set search_path = public
as $$
    select exists (
        select 1 from public.assessments a
        where a.id = target_assessment
          and a.status = 'published'
          and (a.opens_at  is null or a.opens_at  <= now())
          and (a.closes_at is null or a.closes_at >  now())
    );
$$;

-- May this caller be shown the answer key for this paper?
--
-- The author, always. A sitter, only once their OWN attempt has been released — which
-- is the whole point of `results_released_at` being a column rather than a computed
-- guess. Note it checks the caller's own attempt: releasing one person's result does
-- not open the key to everyone else still sitting.
create or replace function public.may_see_answer_key(target_assessment uuid)
returns boolean
language sql stable security definer set search_path = public
as $$
    select
        exists (
            select 1 from public.assessments a
            where a.id = target_assessment and a.author_id = (select auth.uid())
        )
        or exists (
            select 1 from public.attempts t
            where t.assessment_id = target_assessment
              and t.sitter_id = (select auth.uid())
              and t.results_released_at is not null
        );
$$;

-- Resolving a share token is the one action somebody must perform against a paper they
-- cannot yet see. The SELECT policy admits the author and people who already have an
-- attempt, so a prospective sitter is refused by the very policy that starting would
-- satisfy -- open the link, start the attempt; start the attempt, open the link.
--
-- This cuts that knot, and is deliberately the narrowest thing that can. It discloses
-- what a share-link flow inherently must -- that the link is live, and what the paper
-- is called before you commit to sitting it -- and nothing more. No questions, no
-- author identity, no results. Rate-limit the route that calls it.
create or replace function public.assessment_by_share_token(token text)
returns table (
    id               uuid,
    title            text,
    type             public.assessment_type,
    question_count   int,
    duration_minutes int,
    opens_at         timestamptz,
    closes_at        timestamptz,
    proctoring_enabled boolean,
    is_open          boolean
)
language sql stable security definer set search_path = public
as $$
    select a.id, a.title, a.type, a.question_count, a.duration_minutes,
           a.opens_at, a.closes_at, a.proctoring_enabled,
           (a.opens_at is null or a.opens_at <= now())
             and (a.closes_at is null or a.closes_at > now()) as is_open
    from public.assessments a
    where a.share_token = trim(token)
      and a.status = 'published';
$$;

revoke all on function public.is_assessment_author(uuid)      from public;
revoke all on function public.has_attempt_on(uuid)            from public;
revoke all on function public.assessment_is_open(uuid)        from public;
revoke all on function public.may_see_answer_key(uuid)        from public;
revoke all on function public.assessment_by_share_token(text) from public;

grant execute on function public.is_assessment_author(uuid)      to authenticated;
grant execute on function public.has_attempt_on(uuid)            to authenticated;
grant execute on function public.assessment_is_open(uuid)        to authenticated;
grant execute on function public.may_see_answer_key(uuid)        to authenticated;
grant execute on function public.assessment_by_share_token(text) to authenticated;

-- ============================================================== privileges
grant select, insert, update, delete on public.assessments to authenticated;
grant select, insert, update, delete on public.questions   to authenticated;
grant select, insert, update         on public.attempts    to authenticated;
grant select, insert, update         on public.answers     to authenticated;

-- No DELETE on attempts or answers for anybody. A sitting that happened is a fact
-- about a person's result; voiding is a status change, not an erasure.
grant all on public.assessments, public.questions, public.attempts, public.answers
    to service_role;

-- ===================================================================== RLS
alter table public.assessments enable row level security;
alter table public.questions   enable row level security;
alter table public.attempts    enable row level security;
alter table public.answers     enable row level security;

-- ---------------------------------------------------------- assessments
create policy "assessments_select_author_or_sitter"
    on public.assessments for select
    using (
        author_id = (select auth.uid())
        or public.has_attempt_on(id)
    );

create policy "assessments_insert_own"
    on public.assessments for insert
    with check (author_id = (select auth.uid()));

create policy "assessments_update_own"
    on public.assessments for update
    using (author_id = (select auth.uid()))
    with check (author_id = (select auth.uid()));

-- Drafts only. Deleting a published paper would take its attempts with it by cascade,
-- which is somebody's result disappearing because its author changed their mind.
create policy "assessments_delete_own_draft"
    on public.assessments for delete
    using (author_id = (select auth.uid()) and status = 'draft');

-- ------------------------------------------------------------ questions
--
-- **There is no sitter policy on this table, deliberately.** A row here carries
-- `correct_option`, `model_answer` and `rubric`; row-level security cannot hide a
-- column, so the only safe answer is that a sitter never reaches the row at all.
-- They read `public.question_sit` below, which does not contain those columns —
-- the absence is the enforcement, and it cannot be defeated by a forgotten
-- projection in a Pydantic schema or a `select *` in a future refactor.
create policy "questions_author_only"
    on public.questions for all
    using (public.is_assessment_author(assessment_id))
    with check (public.is_assessment_author(assessment_id));

-- ------------------------------------------------------------- attempts
create policy "attempts_select_own_or_author"
    on public.attempts for select
    using (
        sitter_id = (select auth.uid())
        or public.is_assessment_author(assessment_id)
    );

-- You start your own attempt, on a paper that is published and inside its window.
-- The service checks the window too and returns a better message; this is the layer
-- that holds when the service is wrong.
create policy "attempts_insert_self_when_open"
    on public.attempts for insert
    with check (
        sitter_id = (select auth.uid())
        and public.assessment_is_open(assessment_id)
    );

-- The sitter submits; the author voids, grades and releases.
create policy "attempts_update_own_or_author"
    on public.attempts for update
    using (
        sitter_id = (select auth.uid())
        or public.is_assessment_author(assessment_id)
    )
    with check (
        sitter_id = (select auth.uid())
        or public.is_assessment_author(assessment_id)
    );

-- -------------------------------------------------------------- answers
-- Answers inherit their attempt's visibility. The subquery is itself filtered by the
-- policy above, so this stays one rule rather than a copy of it.
create policy "answers_select_through_attempt"
    on public.answers for select
    using (exists (select 1 from public.attempts t where t.id = attempt_id));

-- Writing an answer is only ever the sitter, and only while the attempt is open.
-- A submitted attempt is closed to its author's own edits — that is what submitting
-- means, and enforcing it here rather than only in the service is why a bug in the
-- deadline logic cannot become an answer changed after the fact.
create policy "answers_insert_own_in_progress"
    on public.answers for insert
    with check (
        exists (
            select 1 from public.attempts t
            where t.id = attempt_id
              and t.sitter_id = (select auth.uid())
              and t.status = 'in_progress'
        )
    );

create policy "answers_update_own_in_progress_or_author_grading"
    on public.answers for update
    using (
        exists (
            select 1 from public.attempts t
            where t.id = attempt_id
              and (
                  (t.sitter_id = (select auth.uid()) and t.status = 'in_progress')
                  or public.is_assessment_author(t.assessment_id)
              )
        )
    )
    with check (
        exists (
            select 1 from public.attempts t
            where t.id = attempt_id
              and (
                  (t.sitter_id = (select auth.uid()) and t.status = 'in_progress')
                  or public.is_assessment_author(t.assessment_id)
              )
        )
    );

-- ======================================================= the sitter's views
--
-- Two views, both `security_invoker = false` so they run as their owner and the
-- WHERE clause is load-bearing rather than decorative.

-- What somebody sitting the paper is allowed to see of a question. Note which columns
-- are simply not here: correct_option, model_answer, rubric.
create view public.question_sit
with (security_invoker = false) as
select
    q.id,
    q.assessment_id,
    q."index",
    q.type,
    q.stem,
    q.options,
    q.points,
    q.difficulty
from public.questions q
where public.has_attempt_on(q.assessment_id);

-- The answer key. The author always; a sitter only after their own result is released.
create view public.question_key
with (security_invoker = false) as
select
    q.id,
    q.assessment_id,
    q.type,
    q.correct_option,
    q.model_answer,
    q.rubric,
    q.points
from public.questions q
where public.may_see_answer_key(q.assessment_id);

grant select on public.question_sit to authenticated;
grant select on public.question_key to authenticated;

comment on view public.question_sit is
    'The sitting projection. correct_option, model_answer and rubric are absent by '
    'construction -- RLS cannot hide a column, so a sitter has no policy on questions '
    'at all and reaches a question only through here.';
