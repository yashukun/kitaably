#!/bin/sh

set -eu

echo "buckets: waiting for the storage schema"
i=0
until psql "$DATABASE_URL" -tAc "select to_regclass('storage.buckets')" 2>/dev/null \
        | grep -q "storage.buckets"; do
    i=$((i + 1))
    if [ "$i" -gt 60 ]; then
        echo "buckets: storage.buckets never appeared -- is supabase-storage healthy?" >&2
        exit 1
    fi
    sleep 2
done

psql --no-psqlrc --quiet --set ON_ERROR_STOP=1 --single-transaction \
    "$DATABASE_URL" -f /buckets.sql

echo "buckets: done"
