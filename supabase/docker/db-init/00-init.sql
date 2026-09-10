-- Runs ONCE, on an empty data volume, before anything else connects.
--
-- The supabase/postgres image already ships the roles (anon, authenticated,
-- authenticator, service_role, supabase_auth_admin, supabase_storage_admin) and the
-- auth/storage/extensions schemas in its own initdb. What it does NOT do is give
-- those roles passwords, and GoTrue and Storage connect over TCP with one -- so
-- without this file both crash-loop on "password authentication failed" against a
-- database that otherwise looks healthy.
--
-- Deliberately much smaller than the equivalent the CLI runs: everything it adds for
-- edge functions, analytics and the connection pooler is absent, because config.toml
-- turns all three off. See DECISIONS.md D33.

\set pgpass `echo "$POSTGRES_PASSWORD"`
\set jwt_secret `echo "$JWT_SECRET"`

-- PostgREST reads these two off the database when it starts.
alter database postgres set "app.settings.jwt_secret" to :'jwt_secret';
alter database postgres set "app.settings.jwt_exp" to '3600';

alter user postgres                    with password :'pgpass';
alter user authenticator               with password :'pgpass';
alter user supabase_auth_admin         with password :'pgpass';
alter user supabase_storage_admin      with password :'pgpass';
alter user supabase_replication_admin  with password :'pgpass';
alter user supabase_read_only_user     with password :'pgpass';

-- pgvector, and the crypto functions seed.sql uses to hash the test passwords.
-- `supabase db reset` got these from the image's own bootstrap; on this path we ask
-- explicitly. `extensions` is on the search path for every role that matters.
create extension if not exists "vector"    with schema extensions;
create extension if not exists "pgcrypto"  with schema extensions;
create extension if not exists "uuid-ossp" with schema extensions;

-- The ledger `supabase migration up` writes to. Created here so the migration
-- runner can record against it on a completely fresh volume.
create schema if not exists supabase_migrations;
create table if not exists supabase_migrations.schema_migrations (
    version     text primary key,
    statements  text[],
    name        text
);
-- This file runs as supabase_admin, so both objects are owned by it, and the
-- migration runner connects as `postgres` -- which is a DIFFERENT role here and
-- gets "permission denied for table schema_migrations" on the very first insert.
-- Handing ownership over is what `supabase db reset` effectively did by running
-- everything as the owner.
alter schema supabase_migrations owner to postgres;
alter table supabase_migrations.schema_migrations owner to postgres;
