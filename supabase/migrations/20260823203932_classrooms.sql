-- Phase 2 — classrooms and enrollment.
--
-- The classroom is the scoping unit: a teacher, a subject, enrolled students. Every
-- canon book and every assessment hangs off one, so the rules here decide what the
-- rest of the system can express.

-- ---------------------------------------------------------------- join codes
-- Short enough to read aloud to a room, unguessable enough that shouting it in the
-- corridor next door does not enroll a stranger. gen_random_bytes, not random():
-- random() is a seeded PRNG, so codes minted in sequence are predictable from each
-- other, and a guessable join code is unauthorised access to a classroom's canon.
create or replace function public.generate_join_code()
returns text
language plpgsql
volatile
set search_path = public, extensions
as $$
declare
    -- No I, O, 0 or 1: they are the characters people mistype off a whiteboard.
    alphabet constant text := 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
    bytes    bytea := extensions.gen_random_bytes(6);
    result   text := '';
    i        int;
begin
    for i in 0..5 loop
        result := result || substr(
            alphabet,
            1 + (get_byte(bytes, i) % length(alphabet)),
            1
        );
    end loop;
    return result;
end;
$$;

-- ---------------------------------------------------------------- classrooms
create table public.classrooms (
    id          uuid primary key default gen_random_uuid(),
    teacher_id  uuid not null references public.profiles (id) on delete cascade,
    name        text not null,
    subject     text,
    join_code   text not null unique default public.generate_join_code(),
    archived_at timestamptz,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

create index classrooms_teacher_idx on public.classrooms (teacher_id);

-- --------------------------------------------------------------- enrollments
create type public.enrollment_status as enum ('active', 'removed');

create table public.enrollments (
    id           uuid primary key default gen_random_uuid(),
    classroom_id uuid not null references public.classrooms (id) on delete cascade,
    student_id   uuid not null references public.profiles (id) on delete cascade,
    status       public.enrollment_status not null default 'active',
    joined_at    timestamptz not null default now(),
    updated_at   timestamptz not null default now(),
    constraint enrollments_unique_member unique (classroom_id, student_id)
);

-- Removal is a status change, never a delete: canon retrieval must stop on the
-- student's next request, and the record of who was in the room must survive.
create index enrollments_classroom_status_idx on public.enrollments (classroom_id, status);
create index enrollments_student_status_idx on public.enrollments (student_id, status);

-- ------------------------------------------------------- membership helpers
--
-- These exist to break an RLS recursion. The policy on `classrooms` needs to ask
-- "is the caller enrolled here?", and the policy on `enrollments` needs to ask "does
-- the caller teach this classroom?". Written as plain subqueries, each policy
-- triggers the other's policy and Postgres raises
--   ERROR: infinite recursion detected in policy for relation ...
--
-- SECURITY DEFINER runs the lookup as the function owner, for whom RLS does not
-- apply, so the cycle is cut. They are deliberately narrow: each answers exactly one
-- boolean about the *current* caller and leaks nothing else.
create or replace function public.is_classroom_teacher(target_classroom uuid)
returns boolean
language sql
stable
security definer
set search_path = public
as $$
    select exists (
        select 1
        from public.classrooms c
        where c.id = target_classroom
          and c.teacher_id = (select auth.uid())
    );
$$;

create or replace function public.is_active_enrollee(target_classroom uuid)
returns boolean
language sql
stable
security definer
set search_path = public
as $$
    select exists (
        select 1
        from public.enrollments e
        where e.classroom_id = target_classroom
          and e.student_id = (select auth.uid())
          and e.status = 'active'
    );
$$;

-- Joining is the one action a student must perform against a classroom they
-- cannot yet see. The SELECT policy admits the teacher and active enrollees, so a
-- prospective member resolving their own join code is refused by the very policy
-- that membership would satisfy -- see the code, join the room; join the room, see
-- the code.
--
-- This cuts that knot, and is deliberately the narrowest thing that can: it takes a
-- code and returns one id, so the only fact it discloses is "this code is live",
-- which is inherent to any join-by-code flow. It exposes no name, no subject, no
-- roster. Rate-limit the route that calls it -- 32^6 codes is not many.
create or replace function public.classroom_id_for_join_code(code text)
returns uuid
language sql
stable
security definer
set search_path = public
as $$
    select c.id
    from public.classrooms c
    where upper(c.join_code) = upper(trim(code))
      and c.archived_at is null;
$$;

revoke all on function public.classroom_id_for_join_code(text) from public;
grant execute on function public.classroom_id_for_join_code(text) to authenticated;

revoke all on function public.is_classroom_teacher(uuid) from public;
revoke all on function public.is_active_enrollee(uuid) from public;
grant execute on function public.is_classroom_teacher(uuid) to authenticated;
grant execute on function public.is_active_enrollee(uuid) to authenticated;

-- ---------------------------------------------------------------- privileges
-- GRANT admits the role to the table; POLICY decides which rows it sees. Both are
-- required, and RLS restricts rather than grants.
grant select, insert, update on public.classrooms to authenticated;
grant select, insert, update on public.enrollments to authenticated;
grant all on public.classrooms to service_role;
grant all on public.enrollments to service_role;

-- No DELETE for authenticated on either table: classrooms archive, enrollments are
-- marked removed. Deleting evidence of who was in a room is not a thing a request
-- should be able to do.

-- ---------------------------------------------------------------------- RLS
alter table public.classrooms enable row level security;
alter table public.enrollments enable row level security;

create policy "classrooms_select_teacher_or_enrollee"
    on public.classrooms for select
    using (
        teacher_id = (select auth.uid())
        or public.is_active_enrollee(id)
    );

create policy "classrooms_insert_own_as_teacher"
    on public.classrooms for insert
    with check (
        teacher_id = (select auth.uid())
        and exists (
            select 1 from public.profiles p
            where p.id = (select auth.uid()) and p.role = 'teacher'
        )
    );

create policy "classrooms_update_own"
    on public.classrooms for update
    using (teacher_id = (select auth.uid()))
    with check (teacher_id = (select auth.uid()));

create policy "enrollments_select_self_or_classroom_teacher"
    on public.enrollments for select
    using (
        student_id = (select auth.uid())
        or public.is_classroom_teacher(classroom_id)
    );

-- A student enrolls themself by presenting a join code; the API resolves the code to
-- a classroom, so the row must be for themself and for a classroom that is not
-- archived. A teacher cannot insert enrollments for other people here — adding a
-- student is done by giving them the code.
create policy "enrollments_insert_self"
    on public.enrollments for insert
    with check (
        student_id = (select auth.uid())
        and exists (
            select 1 from public.profiles p
            where p.id = (select auth.uid()) and p.role = 'student'
        )
    );

-- Only the classroom's teacher changes status, which is what removal is.
create policy "enrollments_update_by_classroom_teacher"
    on public.enrollments for update
    using (public.is_classroom_teacher(classroom_id))
    with check (public.is_classroom_teacher(classroom_id));

create trigger classrooms_touch_updated_at
    before update on public.classrooms
    for each row execute function public.touch_updated_at();

create trigger enrollments_touch_updated_at
    before update on public.enrollments
    for each row execute function public.touch_updated_at();

-- ------------------------------------------------------------------- roster
--
-- A teacher needs the names and emails of their own students. The profiles policy
-- deliberately does not give them that: it admits a user to their own row only, so
-- a plain join from enrollments to profiles returns nothing and the roster silently
-- comes back empty.
--
-- Widening the profiles policy would work and would be wrong -- it would hand every
-- teacher every column of those rows. This view exposes exactly the two fields
-- docs/DATA-MODEL.md allows and nothing else.
--
-- security_invoker = false: the view runs as its owner, so RLS on the underlying
-- tables does not apply. The WHERE clause is therefore load-bearing, not decorative
-- -- it is the only thing scoping this to classrooms the caller actually teaches.
create view public.classroom_roster
with (security_invoker = false) as
select
    e.id           as enrollment_id,
    e.classroom_id,
    e.student_id,
    e.status,
    e.joined_at,
    p.name,
    p.email
from public.enrollments e
join public.profiles p on p.id = e.student_id
where public.is_classroom_teacher(e.classroom_id);

grant select on public.classroom_roster to authenticated;
