-- A ZIP upload is one book in parts (DECISIONS.md D26). NCERT-style downloads
-- arrive as an archive of per-chapter PDFs, and asking people to merge them by
-- hand before uploading is a toll booth in front of the thing they came to do.
-- The archive stays the stored source object; its members are combined at parse
-- time in reading order, so 'zip' is a real source_format the books row must be
-- able to name.
--
-- ADD VALUE appends to the enum. Nothing in this file uses the new value, which
-- is what allows it to run inside the migration's transaction.
alter type public.source_format add value if not exists 'zip';
