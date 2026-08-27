"""Book upload, status, and deletion. Phase 3."""

import logging
import re
from collections.abc import AsyncIterator
from pathlib import PurePosixPath
from typing import BinaryIO
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients import storage
from app.core.config import settings
from app.core.errors import Conflict, NotFound, ValidationFailed
from app.core.security import Principal
from app.db.models.book import Book
from app.db.models.enums import BookScope, BookStatus, SourceFormat
from app.rag.parse import sniff_format
from app.services import audit

logger = logging.getLogger(__name__)


async def create_book(
    session: AsyncSession,
    principal: Principal,
    *,
    probe: BinaryIO,
    size: int,
    stream: AsyncIterator[bytes],
    filename: str | None,
    title: str | None,
    author: str | None,
) -> Book:
    """Store an uploaded document and queue it for ingestion.

    **Every upload starts personal**, whoever made it. What you are reading is not
    automatically what everyone else should be examined on, and making sharing a
    second, deliberate act means nothing becomes readable by every user as a side
    effect of a file picker (:func:`set_scope`).

    Scope is therefore not a parameter at all. There is no request body field for
    it, and no branch here that could read one — the privacy boundary is not
    something a client gets to ask about.

    ``title`` is optional and falls back to the file's own name
    (:func:`title_from_filename`). A book must have *a* title — it is how the reader
    finds it again and how a citation names its source — but making them type one
    before they may upload is a toll booth in front of the thing they came to do.
    """
    if size == 0:
        raise ValidationFailed("That file is empty.")
    if size > settings.max_upload_bytes:
        raise ValidationFailed(f"Files must be under {settings.max_upload_mb} MB.")

    # Sniffed from the leading bytes. The filename only breaks the txt/md tie.
    source_format = sniff_format(probe, filename)
    if source_format is None or source_format.value not in settings.allowed_source_formats_list:
        allowed = ", ".join(settings.allowed_source_formats_list)
        raise ValidationFailed(f"That file type is not supported. Allowed: {allowed}.")

    book = Book(
        owner_id=principal.id,
        scope=BookScope.PERSONAL,
        title=(title or "").strip() or title_from_filename(filename),
        author=author.strip() if author else None,
        source_format=source_format,
        storage_path="",  # set below, once the id exists
        byte_size=size,
        status=BookStatus.UPLOADED,
    )
    session.add(book)
    await session.flush()  # assigns the id

    book.storage_path = f"{book.owner_id}/{book.id}/source.{source_format.value}"
    await storage.upload_stream(
        settings.bucket_books,
        book.storage_path,
        stream,
        _content_type(source_format),
        size,
    )

    return book


# Separators a filename uses where prose would use a space. Everything else in the
# name is left exactly as the reader had it -- this cleans up `organic_chem-ch4.pdf`,
# it does not try to guess what the book is called.
_FILENAME_SEPARATORS = re.compile(r"[_\-\s]+")


def title_from_filename(filename: str | None) -> str:
    """A readable book title from an uploaded file's name.

    Deliberately conservative. It strips the directory and the extension, turns
    underscores and hyphens into spaces, and collapses runs of whitespace — and then
    stops. No title-casing: "DNA" would become "Dna" and "a Brief History" would
    become "A Brief History", and one of those is a correction the reader did not ask
    for while the other is vandalism of an acronym.

    The name arrives from the client and is untrusted, so the path is taken apart
    with ``PurePosixPath`` rather than trusted to contain no separators, and the
    result is capped — ``books.title`` is displayed everywhere a citation appears.
    """
    stem = PurePosixPath((filename or "").replace("\\", "/")).stem
    # A name that is nothing but an extension (".pdf") has no suffix as far as
    # PurePosixPath is concerned -- it is a dotfile -- so the stem comes back as
    # ".pdf" and would become the book's title verbatim. Leading dots go.
    cleaned = _FILENAME_SEPARATORS.sub(" ", stem).strip().lstrip(".").strip()
    return cleaned[:200] or "Untitled book"


def _content_type(source_format: SourceFormat) -> str:
    return {
        SourceFormat.PDF: "application/pdf",
        SourceFormat.DOCX: (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        SourceFormat.PPTX: (
            "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        ),
        SourceFormat.TXT: "text/plain; charset=utf-8",
        SourceFormat.MD: "text/markdown; charset=utf-8",
        SourceFormat.ZIP: "application/zip",
    }[source_format]


async def get_book(session: AsyncSession, principal: Principal, book_id: UUID) -> Book:
    book = await session.scalar(select(Book).where(Book.id == book_id))
    if book is None:
        # RLS already hid anything the caller may not see, so "invisible" and
        # "absent" are the same answer here — which is the answer we want to give.
        raise NotFound("That book does not exist.")
    return book


async def list_books(
    session: AsyncSession, principal: Principal, *, scope: BookScope | None = None
) -> list[Book]:
    """Books the caller may see: their own, plus the shared library.

    Which rows come back is decided by RLS, not by a role branch here. ``scope`` is
    a view filter for the UI's two tabs and narrows that set — it can never widen
    it, because the policy has already run by the time this returns.
    """
    query = select(Book).order_by(Book.created_at.desc())
    if scope is not None:
        query = query.where(Book.scope == scope)
    return list(await session.scalars(query))


async def set_scope(
    session: AsyncSession, principal: Principal, book_id: UUID, *, shared: bool
) -> Book:
    """Share a book with everyone, or take it back.

    The owner decides, and only the owner: ``require_book_owner`` proved that before
    this ran, and the ``books_update_own`` policy asserts it again on the write.

    Consequential in both directions — sharing makes private material readable by
    every signed-in user, and unsharing removes material other people may be relying
    on — so it writes an audit row either way. This is the one action in the product
    that changes who can read something, which is exactly the kind of thing the
    audit log exists to remember.
    """
    book = await get_book(session, principal, book_id)

    target = BookScope.CANON if shared else BookScope.PERSONAL
    if book.scope is target:
        return book

    if shared and book.status is not BookStatus.READY:
        # Sharing a half-ingested book would put a partial set of chunks into the
        # pool assessments are generated from.
        raise Conflict("Wait until the book has finished processing, then share it.")

    previous = book.scope
    book.scope = target
    # The chunks follow by database trigger, in this same transaction. Nothing here
    # touches chunks.scope directly — a second writer is a second thing to drift.
    await session.flush()

    await audit.record(
        session,
        action="book.shared" if shared else "book.unshared",
        target_type="book",
        target_id=book.id,
        metadata={"from": previous.value, "to": target.value, "title": book.title},
    )
    return book


async def retry_ingest(session: AsyncSession, principal: Principal, book_id: UUID) -> Book:
    """Put a failed book back on the queue.

    Only from `failed`. Re-running a book that is mid-ingest would have two workers
    writing chunks for the same book, and the task is idempotent against a retry of
    itself, not against a second concurrent run.
    """
    book = await get_book(session, principal, book_id)

    if book.status is not BookStatus.FAILED:
        raise Conflict("Only a book that failed can be retried.")

    book.status = BookStatus.UPLOADED
    book.error = None
    book.needs_ocr = False
    await session.flush()
    return book


async def delete_book(session: AsyncSession, principal: Principal, book_id: UUID) -> Book:
    """Confirm the book is deletable and hand it to the delete task.

    The work itself -- storage object, chapters, chunks, the row, the audit entry --
    happens in one task, so a partial deletion cannot leave chunks whose vectors go
    on answering questions about material the owner believes is gone.
    """
    # Ownership was proved by require_book_owner before this ran.
    return await get_book(session, principal, book_id)
