-- Screen observation arrives with the screen-share gate (Phase 7b): a proctored
-- sitting now asks for the camera AND an entire-screen share, and notes when more
-- than one display is connected. Three new observations, named like their camera
-- siblings — what was observed, never a conclusion about the person:
--
--   screen_share_denied    the screen was not shared when the sitting began
--   screen_share_stopped   an active share ended mid-sitting
--   multiple_displays      more than one display was connected (the browser's
--                          `screen.isExtended`), sustained — the setup screen asks
--                          for it to be disconnected before beginning
--
-- This file ONLY adds the enum values. Postgres refuses to *use* an enum value in
-- the transaction that added it, and each migration runs in its own transaction —
-- so the severity map that classifies these lands in the next file, not here.

alter type public.proctor_event_type add value if not exists 'screen_share_denied';
alter type public.proctor_event_type add value if not exists 'screen_share_stopped';
alter type public.proctor_event_type add value if not exists 'multiple_displays';
