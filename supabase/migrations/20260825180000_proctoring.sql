-- Phase 7 — proctoring capture: sessions, events, and the sitter's write path.
--
-- The shape of the thing being built:
--
--   consent screen -> camera -> proctor_session (status='active')
--   browser detects -> debounces -> batches events every ~10s, heartbeats every ~15s
--   high-severity moments -> one downscaled JPEG still via a signed upload URL
--   submit/deadline closes the session -> aggregate_session recomputes the score
--   review_status='pending' -> the author's review gate (Phase 8) decides everything
--
-- Two rules carried by this schema rather than by good intentions:
--
-- **The sitter has NO POLICY AT ALL on these tables.** Not a narrow one — none.
-- A row here is the raw material of an accusation, and `released_at IS NULL` must
-- mean the sitter sees nothing whatever a future refactor forgets. Absence of a
-- policy cannot be defeated by a forgotten WHERE. The sitter's *writes* go through
-- the SECURITY DEFINER functions below, which derive everything security-relevant
-- from auth.uid() and a fixed map — they are reachable over PostgREST rpc by any
-- signed-in user, so they must defend themselves rather than trust the API.
--
-- **Severity is assigned here, not accepted.** The client sends event types and
-- timings; public.proctor_severity() is the one map from type to severity. A client
-- that reports "info" for everything changes nothing, even calling rpc directly.

-- ------------------------------------------------------------------- enums
create type public.proctor_session_status as enum ('active', 'closed', 'aborted');
create type public.review_status          as enum ('pending', 'cleared', 'flagged', 'released');
create type public.severity               as enum ('info', 'low', 'medium', 'high');
-- The author's judgement, not a "teacher's" — that vocabulary left with D16.
create type public.author_verdict         as enum ('unreviewed', 'dismissed', 'upheld');

create type public.proctor_event_type as enum (
    'session_start', 'session_end',
    'heartbeat_gap',
    'no_face', 'multiple_faces', 'face_mismatch',
    'gaze_away', 'head_pose_away', 'phone_visible',
    'tab_blur', 'window_blur', 'fullscreen_exit',
    'copy', 'paste', 'context_menu',
    'camera_denied', 'camera_stopped',
    'clock_skew'
);

-- ------------------------------------------------------------ proctor_sessions
create table public.proctor_sessions (
    id                   uuid primary key default gen_random_uuid(),
    -- One session per attempt. A reload resumes the session it finds.
    attempt_id           uuid not null unique references public.attempts (id) on delete cascade,

    status               public.proctor_session_status not null default 'active',
    started_at           timestamptz not null default now(),
    ended_at             timestamptz,

    -- The consented identity still: '{session_id}/baseline.jpg' in the evidence
    -- bucket. Set at open; the object may lag or never arrive (camera denied), and
    -- a missing object is an observation, not an error.
    baseline_path        text,

    -- Drives heartbeat_gap detection. Absence is evidence: the easiest attack on a
    -- browser-side detector is to stop reporting, so silence must be written down.
    last_heartbeat_at    timestamptz,

    -- SERVER-computed by aggregate_session, from raw events, after close. Never
    -- accepted from a client. A queue-ordering device for the author's review —
    -- never a verdict, never a grade input, never sitter-visible before release.
    integrity_score      int,

    review_status        public.review_status not null default 'pending',
    reviewed_by          uuid references public.profiles (id) on delete set null,
    reviewed_at          timestamptz,
    -- Shown to the sitter on release. Observational language only.
    reviewer_note        text,
    -- NULL means the person who sat this sees nothing at all. Reachable only by
    -- the author's explicit act — never a timer, a threshold, or a default.
    released_at          timestamptz,

    -- Retention TTL for the stills, set at close by the aggregate task from server
    -- config. The purge_evidence beat task (Phase 11) enforces it.
    evidence_purge_after timestamptz,

    constraint proctor_sessions_score_range
        check (integrity_score is null or integrity_score between 0 and 100),
    constraint proctor_sessions_closed_have_ended
        check (status = 'active' or ended_at is not null)
);

create index proctor_sessions_review_idx
    on public.proctor_sessions (review_status, ended_at desc);
-- The sweep scans active sessions by silence; partial index keeps it cheap.
create index proctor_sessions_active_heartbeat_idx
    on public.proctor_sessions (last_heartbeat_at)
    where status = 'active';

comment on table public.proctor_sessions is
    'One monitoring record per attempt. released_at IS NULL means the sitter sees '
    'nothing at all; the review gate (author verdicts, then release/clear/void) is '
    'the only path to visibility.';

-- -------------------------------------------------------------- proctor_events
create table public.proctor_events (
    id                 uuid primary key default gen_random_uuid(),
    proctor_session_id uuid not null references public.proctor_sessions (id) on delete cascade,

    -- Client clock, advisory. Large divergence is itself an event (clock_skew).
    occurred_at        timestamptz not null,
    -- Server clock, authoritative for ordering.
    received_at        timestamptz not null default now(),

    type               public.proctor_event_type not null,
    -- Assigned by public.proctor_severity(), never by the client.
    severity           public.severity not null,

    -- Detector confidence, 0-1. Advisory, like everything client-supplied.
    confidence         real,
    duration_ms        int,
    -- Coalesced episode count: "tab lost focus 6 times" is one row.
    occurrences        int not null default 1,

    -- '{session_id}/{event_id}.jpg' in the evidence bucket. High severity only,
    -- at most one still per episode. Written before the object can exist, so a
    -- still with no event row cannot occur.
    evidence_path      text,

    metadata           jsonb not null default '{}'::jsonb,

    author_verdict     public.author_verdict not null default 'unreviewed',

    constraint proctor_events_duration_sane
        check (duration_ms is null or duration_ms >= 0),
    constraint proctor_events_occurrences_positive
        check (occurrences > 0),
    constraint proctor_events_confidence_range
        check (confidence is null or (confidence >= 0 and confidence <= 1))
);

create index proctor_events_session_time_idx
    on public.proctor_events (proctor_session_id, received_at);
create index proctor_events_session_severity_idx
    on public.proctor_events (proctor_session_id, severity);

comment on table public.proctor_events is
    'Observations, never accusations: what the camera and the browser saw, with '
    'duration and count. Severity comes from a fixed server-side map keyed by type.';

-- ==================================================== authorship helpers
--
-- Same pattern as is_assessment_author: a SECURITY DEFINER boolean about the
-- current caller, so policies here cannot recurse into the tables they guard and
-- cannot silently return empty through a join the caller half-sees.

create or replace function public.is_attempt_author(target_attempt uuid)
returns boolean
language sql stable security definer set search_path = public
as $$
    select exists (
        select 1
        from public.attempts t
        join public.assessments a on a.id = t.assessment_id
        where t.id = target_attempt and a.author_id = (select auth.uid())
    );
$$;

create or replace function public.is_proctor_session_author(target_session uuid)
returns boolean
language sql stable security definer set search_path = public
as $$
    select exists (
        select 1
        from public.proctor_sessions s
        join public.attempts t    on t.id = s.attempt_id
        join public.assessments a on a.id = t.assessment_id
        where s.id = target_session and a.author_id = (select auth.uid())
    );
$$;

revoke all on function public.is_attempt_author(uuid)         from public;
revoke all on function public.is_proctor_session_author(uuid) from public;
grant execute on function public.is_attempt_author(uuid)         to authenticated;
grant execute on function public.is_proctor_session_author(uuid) to authenticated;

-- ======================================================== the severity map
--
-- The one place a type becomes a severity. IMMUTABLE: changing the map is a
-- migration, which is the point — severity is part of the schema's meaning, not a
-- tunable. tests/test_proctoring.py asserts every enum value has a row here.
create or replace function public.proctor_severity(p_type public.proctor_event_type)
returns public.severity
language sql immutable
as $$
    select case p_type
        when 'session_start'   then 'info'::public.severity
        when 'session_end'     then 'info'::public.severity
        when 'heartbeat_gap'   then 'medium'::public.severity
        when 'no_face'         then 'medium'::public.severity
        when 'multiple_faces'  then 'high'::public.severity
        when 'face_mismatch'   then 'high'::public.severity
        when 'gaze_away'       then 'low'::public.severity
        when 'head_pose_away'  then 'low'::public.severity
        when 'phone_visible'   then 'medium'::public.severity
        when 'tab_blur'        then 'medium'::public.severity
        when 'window_blur'     then 'medium'::public.severity
        when 'fullscreen_exit' then 'medium'::public.severity
        when 'copy'            then 'medium'::public.severity
        when 'paste'           then 'medium'::public.severity
        when 'context_menu'    then 'low'::public.severity
        when 'camera_denied'   then 'high'::public.severity
        when 'camera_stopped'  then 'high'::public.severity
        when 'clock_skew'      then 'low'::public.severity
    end;
$$;

-- ==================================================== the sitter's write path
--
-- The sitter can never SELECT these tables, so their writes arrive through these
-- four functions. Each verifies auth.uid() against the attempt row itself and
-- returns an `outcome` text instead of raising: the service maps outcomes to
-- domain errors, and a direct rpc caller learns nothing they did not already know.

-- Open (or resume) the session for an attempt. One per attempt, ever.
create or replace function public.open_proctor_session(p_attempt_id uuid)
returns table (outcome text, session_id uuid, already boolean)
language plpgsql volatile security definer set search_path = public
as $$
declare
    v_attempt  public.attempts%rowtype;
    v_session  public.proctor_sessions%rowtype;
    v_id       uuid;
begin
    select * into v_attempt from public.attempts t
        where t.id = p_attempt_id and t.sitter_id = (select auth.uid());
    if not found then
        return query select 'not_found'::text, null::uuid, false; return;
    end if;
    if v_attempt.status <> 'in_progress' then
        return query select 'not_in_progress'::text, null::uuid, false; return;
    end if;
    -- Only a paper whose author chose proctoring gets a session. Anything else
    -- would let a sitter file observation records into an author's review queue
    -- for a paper that never asked to be watched.
    if not exists (
        select 1 from public.assessments a
        where a.id = v_attempt.assessment_id and a.proctoring_enabled
    ) then
        return query select 'not_proctored'::text, null::uuid, false; return;
    end if;

    select * into v_session from public.proctor_sessions s
        where s.attempt_id = p_attempt_id;
    if found then
        if v_session.status <> 'active' then
            -- Proctoring for this attempt has ended; it does not restart.
            return query select 'ended'::text, v_session.id, true; return;
        end if;
        return query select 'ok'::text, v_session.id, true; return;
    end if;

    v_id := gen_random_uuid();
    insert into public.proctor_sessions (id, attempt_id, baseline_path, last_heartbeat_at)
        values (v_id, p_attempt_id, v_id || '/baseline.jpg', now());
    insert into public.proctor_events
            (proctor_session_id, occurred_at, type, severity)
        values (v_id, now(), 'session_start', public.proctor_severity('session_start'));
    return query select 'ok'::text, v_id, false;
end;
$$;

-- Record a batch of observations. Everything security-relevant — severity, the
-- evidence path, the received_at ordering, the clock-skew check — is derived
-- here; the payload contributes only advisory fields.
create or replace function public.record_proctor_events(p_attempt_id uuid, p_events jsonb)
returns table (
    outcome    text,
    client_ref text,
    event_id   uuid,
    event_type public.proctor_event_type,
    severity   public.severity,
    evidence_path text
)
language plpgsql volatile security definer set search_path = public
as $$
declare
    v_session    public.proctor_sessions%rowtype;
    v_elem       jsonb;
    v_type       public.proctor_event_type;
    v_sev        public.severity;
    v_id         uuid;
    v_occurred   timestamptz;
    v_confidence real;
    v_duration   int;
    v_count      int;
    v_has_still  boolean;
    v_path       text;
    v_recent     int;
    v_skew_ms    bigint;
    v_max_skew   bigint := 0;
begin
    select s.* into v_session
        from public.proctor_sessions s
        join public.attempts t on t.id = s.attempt_id
        where s.attempt_id = p_attempt_id and t.sitter_id = (select auth.uid());
    if not found then
        return query select 'not_found'::text, null::text, null::uuid,
                            null::public.proctor_event_type, null::public.severity, null::text;
        return;
    end if;
    if v_session.status <> 'active' then
        return query select 'ended'::text, null::text, null::uuid,
                            null::public.proctor_event_type, null::public.severity, null::text;
        return;
    end if;

    if p_events is null or jsonb_typeof(p_events) <> 'array'
       or jsonb_array_length(p_events) > 50 then
        return query select 'rejected'::text, null::text, null::uuid,
                            null::public.proctor_event_type, null::public.severity, null::text;
        return;
    end if;

    -- A stuck or hostile client must not flood the record. The browser rate-caps
    -- per type; this is the cap that holds when the browser is the problem.
    select count(*) into v_recent from public.proctor_events e
        where e.proctor_session_id = v_session.id
          and e.received_at > now() - interval '1 minute';
    if v_recent >= 120 then
        return query select 'rate_limited'::text, null::text, null::uuid,
                            null::public.proctor_event_type, null::public.severity, null::text;
        return;
    end if;

    -- A batch is proof of life, whatever else it says.
    update public.proctor_sessions set last_heartbeat_at = now() where id = v_session.id;

    for v_elem in select * from jsonb_array_elements(p_events) loop
        -- Every cast below reads attacker-controllable text. One malformed element
        -- is rejected alone; it must not take the rest of the batch with it.
        begin
            v_type := (v_elem->>'type')::public.proctor_event_type;

            -- Server-detected types cannot be claimed by a client.
            if v_type in ('heartbeat_gap', 'clock_skew') then
                raise exception 'server-detected type';
            end if;

            v_occurred := coalesce(nullif(v_elem->>'occurred_at', '')::timestamptz, now());
            v_skew_ms := abs(extract(epoch from (v_occurred - now())) * 1000)::bigint;
            v_max_skew := greatest(v_max_skew, v_skew_ms);
            -- The claim is kept (it is advisory by definition) unless it is absurd,
            -- where "absurd" is beyond any real clock drift.
            if v_occurred > now() + interval '1 day'
               or v_occurred < now() - interval '1 day' then
                v_occurred := now();
            end if;

            -- Clamp when present, stay null when absent: a missing duration is
            -- "unknown", which is not the same observation as "instantaneous".
            v_confidence := nullif(v_elem->>'confidence', '')::real;
            if v_confidence is not null then
                v_confidence := least(greatest(v_confidence, 0), 1);
            end if;
            v_duration := nullif(v_elem->>'duration_ms', '')::int;
            if v_duration is not null and v_duration < 0 then
                v_duration := 0;
            end if;
            v_count := greatest(coalesce(nullif(v_elem->>'occurrences', '')::int, 1), 1);
            v_has_still := coalesce(nullif(v_elem->>'has_still', '')::boolean, false);
        exception when others then
            outcome := 'rejected'; client_ref := v_elem->>'client_ref';
            event_id := null; event_type := null; severity := null; evidence_path := null;
            return next; continue;
        end;

        v_sev := public.proctor_severity(v_type);
        v_id  := gen_random_uuid();

        -- A still is allowed only for high severity, and the path is minted here so
        -- the event row always precedes its object.
        v_path := case
            when v_sev = 'high' and v_has_still
            then v_session.id || '/' || v_id || '.jpg'
        end;

        insert into public.proctor_events
            (id, proctor_session_id, occurred_at, type, severity,
             confidence, duration_ms, occurrences, evidence_path, metadata)
        values
            (v_id, v_session.id, v_occurred, v_type, v_sev,
             v_confidence, v_duration, v_count, v_path,
             coalesce(v_elem->'metadata', '{}'::jsonb));

        outcome := 'ok'; client_ref := v_elem->>'client_ref';
        event_id := v_id; event_type := v_type; severity := v_sev; evidence_path := v_path;
        return next;
    end loop;

    -- Clock skew is observed server-side, from the divergence the client cannot
    -- hide: it is the comparison of its own claim against our clock.
    if v_max_skew > 120000 then
        insert into public.proctor_events
                (proctor_session_id, occurred_at, type, severity, metadata)
            values (v_session.id, now(), 'clock_skew',
                    public.proctor_severity('clock_skew'),
                    jsonb_build_object('max_skew_ms', v_max_skew));
    end if;
    return;
end;
$$;

-- Proof of life, every ~15s. Silence past HEARTBEAT_GAP_SECONDS becomes a
-- heartbeat_gap event, written by the sweep task — absence is evidence.
create or replace function public.proctor_heartbeat(p_attempt_id uuid)
returns text
language plpgsql volatile security definer set search_path = public
as $$
declare
    v_status public.proctor_session_status;
begin
    select s.status into v_status
        from public.proctor_sessions s
        join public.attempts t on t.id = s.attempt_id
        where s.attempt_id = p_attempt_id and t.sitter_id = (select auth.uid());
    if not found then return 'not_found'; end if;
    if v_status <> 'active' then return 'ended'; end if;

    update public.proctor_sessions set last_heartbeat_at = now()
        where attempt_id = p_attempt_id;
    return 'ok';
end;
$$;

-- Close on submit. Idempotent: closing a closed session reports where it ended up.
-- evidence_purge_after and the integrity score are written by aggregate_session,
-- from server config — deliberately NOT parameters here, because this function is
-- callable by any signed-in user over rpc and retention must not be theirs to set.
create or replace function public.close_proctor_session(p_attempt_id uuid)
returns table (outcome text, session_id uuid)
language plpgsql volatile security definer set search_path = public
as $$
declare
    v_session public.proctor_sessions%rowtype;
begin
    select s.* into v_session
        from public.proctor_sessions s
        join public.attempts t on t.id = s.attempt_id
        where s.attempt_id = p_attempt_id and t.sitter_id = (select auth.uid());
    if not found then
        return query select 'none'::text, null::uuid; return;
    end if;
    if v_session.status <> 'active' then
        return query select 'ok'::text, v_session.id; return;
    end if;

    update public.proctor_sessions
        set status = 'closed', ended_at = now()
        where id = v_session.id;
    insert into public.proctor_events
            (proctor_session_id, occurred_at, type, severity)
        values (v_session.id, now(), 'session_end',
                public.proctor_severity('session_end'));
    return query select 'ok'::text, v_session.id;
end;
$$;

revoke all on function public.proctor_severity(public.proctor_event_type)   from public;
revoke all on function public.open_proctor_session(uuid)                    from public;
revoke all on function public.record_proctor_events(uuid, jsonb)            from public;
revoke all on function public.proctor_heartbeat(uuid)                       from public;
revoke all on function public.close_proctor_session(uuid)                   from public;

grant execute on function public.proctor_severity(public.proctor_event_type)
    to authenticated, service_role;
grant execute on function public.open_proctor_session(uuid)      to authenticated;
grant execute on function public.record_proctor_events(uuid, jsonb) to authenticated;
grant execute on function public.proctor_heartbeat(uuid)         to authenticated;
grant execute on function public.close_proctor_session(uuid)     to authenticated;

-- ============================================================== privileges
--
-- SELECT only. The sitter-facing writes go through the definer functions above;
-- the author's review verdicts (Phase 8) will arrive with their own policies in
-- that phase's migration. The worker runs as service_role and bypasses RLS, so
-- every worker query carries its scope predicate explicitly.
grant select on public.proctor_sessions to authenticated;
grant select on public.proctor_events   to authenticated;
grant all on public.proctor_sessions, public.proctor_events to service_role;

-- ===================================================================== RLS
alter table public.proctor_sessions enable row level security;
alter table public.proctor_events   enable row level security;

-- The assessment's author, always. The sitter: no policy at all — see the header.
create policy "proctor_sessions_select_author"
    on public.proctor_sessions for select
    using (public.is_attempt_author(attempt_id));

create policy "proctor_events_select_author"
    on public.proctor_events for select
    using (public.is_proctor_session_author(proctor_session_id));
