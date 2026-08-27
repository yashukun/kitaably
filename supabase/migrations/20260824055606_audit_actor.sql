-- Attributing an audit row from a background task.
--
-- public.write_audit_log() takes the actor from auth.uid() precisely so a caller
-- cannot record an action as somebody else. That is right for the request path and
-- useless for a Celery task: the worker has no JWT, auth.uid() is null, and a book
-- deletion would land in the audit trail as an anonymous system action.
--
-- This variant accepts the actor explicitly and is granted to service_role ALONE.
-- The trade is deliberate and narrow: service_role already bypasses RLS entirely, so
-- it is trusted by construction, and the alternative -- an unattributed deletion --
-- defeats the point of having the table at all.

create or replace function public.write_audit_log_as(
    p_actor_id    uuid,
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
    values (p_actor_id, p_action, p_target_type, p_target_id, p_metadata, p_request_id)
    returning id into new_id;
    return new_id;
end;
$$;

revoke all on function public.write_audit_log_as(uuid, text, text, uuid, jsonb, text) from public;

-- Note the absence of `authenticated` here. An authenticated caller must go through
-- write_audit_log(), which will not let them name themselves as somebody else.
grant execute on function public.write_audit_log_as(uuid, text, text, uuid, jsonb, text)
    to service_role;
