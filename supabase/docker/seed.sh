#!/bin/sh
# Load the two test accounts, once GoTrue has built auth.users.
#
# The third step of the bootstrap image, and under compose its own service
# (supabase-seed) rather than part of supabase-bootstrap: `auth.users` gets its
# columns from GoTrue's OWN migrations, which run when that container first boots,
# and GoTrue in turn waits for the schema migrations -- so this step cannot share a
# run with migrate.sh without a depends_on cycle. Seeding during the schema step
# fails on `column "email_confirmed_at" of relation "users" does not exist`.
#
# seed.sql inserts fixed UUIDs, so it is NOT re-runnable -- a second pass fails on the
# primary key. Guarded on an empty auth.users, which makes `docker compose up` safe to
# type as often as you like.

set -eu

echo "seed: waiting for the auth schema"
i=0
until psql "$DATABASE_URL" -tAc \
        "select 1 from information_schema.columns
          where table_schema = 'auth' and table_name = 'users'
            and column_name = 'email_confirmed_at'" 2>/dev/null | grep -q 1; do
    i=$((i + 1))
    if [ "$i" -gt 60 ]; then
        echo "seed: auth.users never appeared -- is supabase-auth healthy?" >&2
        exit 1
    fi
    sleep 2
done

count=$(psql "$DATABASE_URL" -tAc "select count(*) from auth.users")
if [ "$count" != "0" ]; then
    echo "seed: skip ($count users already present)"
    exit 0
fi

echo "seed: loading test accounts"
psql --no-psqlrc --quiet --set ON_ERROR_STOP=1 --single-transaction \
    "$DATABASE_URL" -f /seed.sql

echo "seed: done"
