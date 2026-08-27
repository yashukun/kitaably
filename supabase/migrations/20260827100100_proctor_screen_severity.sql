-- The severity map, extended for the screen observations added in the previous
-- migration (which had to commit first — an enum value cannot be used in the
-- transaction that added it).
--
-- The whole CASE is restated, not just the new rows: `create or replace` swaps the
-- body atomically, and tests/test_proctoring.py parses every map out of the
-- migrations (later definitions overriding earlier) to hold the Python mirror equal.
--
-- Severity describes the strength of the observation, never the guilt of a person:
--   screen_share_denied / screen_share_stopped — high, like their camera siblings:
--     the sitting ran (partly) unobserved, which is the fact the author most needs
--     surfaced.
--   multiple_displays — medium: a second display is a real observation, but docked
--     laptops make it common enough that treating it as high would drown review
--     queues in hardware arrangements.

create or replace function public.proctor_severity(p_type public.proctor_event_type)
returns public.severity
language sql immutable
as $$
    select case p_type
        when 'session_start'        then 'info'::public.severity
        when 'session_end'          then 'info'::public.severity
        when 'heartbeat_gap'        then 'medium'::public.severity
        when 'no_face'              then 'medium'::public.severity
        when 'multiple_faces'       then 'high'::public.severity
        when 'face_mismatch'        then 'high'::public.severity
        when 'gaze_away'            then 'low'::public.severity
        when 'head_pose_away'       then 'low'::public.severity
        when 'phone_visible'        then 'medium'::public.severity
        when 'tab_blur'             then 'medium'::public.severity
        when 'window_blur'          then 'medium'::public.severity
        when 'fullscreen_exit'      then 'medium'::public.severity
        when 'copy'                 then 'medium'::public.severity
        when 'paste'                then 'medium'::public.severity
        when 'context_menu'         then 'low'::public.severity
        when 'camera_denied'        then 'high'::public.severity
        when 'camera_stopped'       then 'high'::public.severity
        when 'clock_skew'           then 'low'::public.severity
        when 'screen_share_denied'  then 'high'::public.severity
        when 'screen_share_stopped' then 'high'::public.severity
        when 'multiple_displays'    then 'medium'::public.severity
    end;
$$;
