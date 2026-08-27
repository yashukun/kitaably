-- Local seed data. Applied by `supabase db reset` and by `make seed`.
--
-- Test accounts, so a fresh checkout has something to sign in with. Nothing here
-- runs against a hosted project.
--
-- Users are inserted into auth.users directly rather than through the signup API,
-- because a seed file has to be re-runnable from `db reset` with no network and no
-- running GoTrue. Inserting here still fires public.handle_new_user(), so the
-- profiles rows come from the same trigger the real signup path uses -- seeding
-- around the trigger would let the seed and production disagree.
--
-- Password for every account below: Passw0rd!123
--
-- There is one kind of account (DECISIONS.md D16), so these two differ only in who
-- they are. Two of them exist because the property worth checking by eye is that
-- Amina cannot see Ravi's personal books -- which needs two people to be visible at
-- all.

-- Fixed ids so tests and manual pokes can reference them without a lookup.
--   amina  11111111-1111-1111-1111-111111111111
--   ravi   22222222-2222-2222-2222-222222222222

insert into auth.users (
    instance_id, id, aud, role, email, encrypted_password,
    email_confirmed_at, last_sign_in_at,
    raw_app_meta_data, raw_user_meta_data,
    created_at, updated_at,
    confirmation_token, email_change, email_change_token_new, recovery_token
)
values
    (
        '00000000-0000-0000-0000-000000000000',
        '11111111-1111-1111-1111-111111111111',
        'authenticated', 'authenticated',
        'amina@kitaably.test',
        extensions.crypt('Passw0rd!123', extensions.gen_salt('bf')),
        now(), now(),
        '{"provider":"email","providers":["email"]}'::jsonb,
        '{"name":"Amina Rahman"}'::jsonb,
        now(), now(), '', '', '', ''
    ),
    (
        '00000000-0000-0000-0000-000000000000',
        '22222222-2222-2222-2222-222222222222',
        'authenticated', 'authenticated',
        'ravi@kitaably.test',
        extensions.crypt('Passw0rd!123', extensions.gen_salt('bf')),
        now(), now(),
        '{"provider":"email","providers":["email"]}'::jsonb,
        -- Deliberately claims something the trigger does not read. Signup metadata is
        -- client-supplied, and this row exists so that "nothing in it reaches
        -- authority" stays true by test rather than by nobody having tried.
        '{"name":"Ravi Menon","role":"administrator"}'::jsonb,
        now(), now(), '', '', '', ''
    );

-- GoTrue will not authenticate a password user with no matching identity row.
insert into auth.identities (
    provider_id, user_id, identity_data, provider,
    last_sign_in_at, created_at, updated_at
)
select
    u.id::text,
    u.id,
    jsonb_build_object('sub', u.id::text, 'email', u.email, 'email_verified', true),
    'email',
    now(), now(), now()
from auth.users u
where u.email in ('amina@kitaably.test', 'ravi@kitaably.test');

-- Books are not seeded: a `books` row without its storage object and its embedded
-- chunks is a book that looks ready and answers nothing. Upload one through the UI
-- instead -- that path is the thing worth exercising anyway.
--
-- Phase 5 onward this file also gets one published assessment with a handful of
-- questions, which has no such dependency.
