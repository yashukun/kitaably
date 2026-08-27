-- Cleanup: remove password-protected share links, which were modelled and never built.
--
-- `assessments.access_mode` had two values and only one of them did anything. Nothing
-- read `access_password_hash`, and nothing hashed anything into it — so a paper set to
-- `link_password` was, in practice, a paper protected by a link and a false belief.
-- That is worse than not offering the feature: the column existing is what makes it
-- look supported.
--
-- The share link is the whole access grant (DECISIONS.md D16). Removing the alternative
-- makes that true in the schema rather than merely true in practice. If password-gated
-- links are wanted later, they arrive as a migration plus the code that enforces them,
-- in the same change.

alter table public.assessments drop column access_mode;
alter table public.assessments drop column access_password_hash;

drop type public.access_mode;

comment on column public.assessments.share_token is
    'The entire access grant. Possession of the URL is what admits somebody; identity '
    'is always the authenticated caller, never the token.';
