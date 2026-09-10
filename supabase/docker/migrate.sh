#!/bin/sh
# Apply supabase/migrations/*.sql in filename order.
#
# The schema half of what `supabase db reset` used to do. The first step of the
# bootstrap image (supabase/Dockerfile, dispatched by bootstrap.sh); buckets.sh and
# seed.sh are the other two, kept separate because both depend on tables that Storage
# and GoTrue create for themselves on their own first boot -- neither exists yet at
# this point in the ordering. Under compose the image runs as `supabase-bootstrap`,
# which exits 0 on success, and every other service that touches the database waits
# for that exit -- so the backend cannot start against a schema that is half applied.
#
# FORWARD-ONLY, and idempotent by ledger: each file's version (the leading
# timestamp) is recorded in supabase_migrations.schema_migrations, and a file
# already in there is skipped. Re-running this on a warm volume is therefore a
# no-op, which is what makes `docker compose up` safe to type twice.
#
# One rule inherited from CLAUDE.md: an EMPTY migration file is an error here, not
# an applied no-op. The CLI records empty files as applied and leaves the schema and
# the ledger disagreeing; refusing to start is the louder, more useful failure.

set -eu

PSQL="psql --no-psqlrc --quiet --set ON_ERROR_STOP=1 $DATABASE_URL"

echo "migrate: waiting for postgres"
until psql "$DATABASE_URL" -c 'select 1' >/dev/null 2>&1; do
    sleep 1
done

applied=$($PSQL -tAc \
    "select version from supabase_migrations.schema_migrations" 2>/dev/null || echo "")

for file in /migrations/*.sql; do
    [ -e "$file" ] || continue
    base=$(basename "$file")
    version=${base%%_*}

    if echo "$applied" | grep -qx "$version"; then
        echo "migrate: skip    $base"
        continue
    fi

    if [ ! -s "$file" ]; then
        echo "migrate: ERROR   $base is empty -- refusing to record it as applied" >&2
        exit 1
    fi

    echo "migrate: apply   $base"
    # One transaction per migration: a file that fails leaves nothing behind and is
    # not recorded, so the next run retries it from a clean state.
    $PSQL --single-transaction \
        -f "$file" \
        -c "insert into supabase_migrations.schema_migrations (version, name)
            values ('$version', '$base')
            on conflict (version) do nothing"
done

echo "migrate: done"
