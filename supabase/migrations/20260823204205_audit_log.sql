-- audit_log — the human record that a person decided, and when.
--
-- Required by CLAUDE.md invariant 7 for every consequential action: release, void,
-- grade override, enrollment removal, and any deletion of material or evidence. It
-- arrives here in Phase 2 because removing a student is the first such action the
-- system can perform.
--
-- This is not application logging. It is not sampled, not truncated, and not shipped
-- to a lossy pipeline. If a student contests an outcome, this table is the answer.

create table public.audit_log (
    id          bigserial primary key,
    -- Null for system actions (a beat task purging evidence has no actor).
    actor_id    uuid references public.profiles (id) on delete set null,
    action      text not null,
    target_type text not null,
    target_id   uuid,
    metadata    jsonb not null default '{}'::jsonb,
    -- Ties a row here to the request logs that produced it.
    request_id  text,
    created_at  timestamptz not null default now()
);

create index audit_log_target_idx on public.audit_log (target_type, target_id, created_at desc);
create index audit_log_actor_idx on public.audit_log (actor_id, created_at desc);
create index audit_log_action_idx on public.audit_log (action, created_at desc);

comment on table public.audit_log is
    'Append-only record of consequential human decisions. No update or delete policy exists for any role.';

-- Append-only, enforced by privilege rather than by convention.
--
-- Note what is NOT granted: no UPDATE and no DELETE, to anybody, including
-- service_role. Rows are written by the backend and read by nobody through this
-- API — an operator with database access can read it, which is the point. A table
-- that the application can rewrite is not an audit trail, it is a suggestion.
grant insert on public.audit_log to service_role;
grant usage, select on sequence public.audit_log_id_seq to service_role;

alter table public.audit_log enable row level security;

-- Deliberately no policies at all. RLS denies by default, so no authenticated user
-- reads or writes this table through PostgREST, ever.

-- The request path runs as `authenticated`, which has no privileges here — so writes
-- go through this function instead.
--
-- The important detail is what it does NOT accept: there is no actor_id parameter.
-- The actor is taken from auth.uid() inside the function. A caller can therefore
-- record an action, but cannot record it as somebody else — which is the one thing
-- an audit trail must not permit.
create or replace function public.write_audit_log(
    p_action      text,
    p_target_type text,
    p_target_id   uuid,
    p_metadata    jsonb default '{}'::jsonb,
    p_request_id  text default null
)
returns bigint
language plpgsql
volatile
security definer
set search_path = public
as $$
declare
    new_id bigint;
begin
    insert into public.audit_log (actor_id, action, target_type, target_id, metadata, request_id)
    values ((select auth.uid()), p_action, p_target_type, p_target_id, p_metadata, p_request_id)
    returning id into new_id;
    return new_id;
end;
$$;

revoke all on function public.write_audit_log(text, text, uuid, jsonb, text) from public;
grant execute on function public.write_audit_log(text, text, uuid, jsonb, text)
    to authenticated, service_role;
