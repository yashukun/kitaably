-- The two private buckets, as rows.
--
-- Under the CLI these came from config.toml's [storage.buckets.books] and
-- [storage.buckets.evidence] sections, which compose has no equivalent for. Storage
-- keeps buckets in an ordinary table, so creating them is SQL -- and it runs after
-- the migrations because the storage schema has to exist first.
--
-- BOTH ARE PRIVATE, and that is invariant 1 and invariant 3 at the storage layer:
-- `books` holds material that is personal until its owner shares it, and `evidence`
-- holds proctoring stills no sitter may see unreviewed. A public bucket here would
-- put both a URL away from anyone who guesses an object name.
--
-- 80 MiB matches MAX_UPLOAD_MB in .env. Both caps exist on purpose: this one stops
-- the bytes arriving, the application one stops the row being written.

insert into storage.buckets (id, name, public, file_size_limit)
values
    ('books',    'books',    false, 83886080),
    ('evidence', 'evidence', false, 83886080)
on conflict (id) do update
    set public          = excluded.public,
        file_size_limit = excluded.file_size_limit;
