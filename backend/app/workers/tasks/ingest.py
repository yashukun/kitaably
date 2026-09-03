"""queue: ingest. Phase 3.

Tasks are thin wrappers over services: argument marshalling, retry policy, and
writing terminal state to the owning row.

Idempotent — prior output is deleted before new output is written, because
at-least-once delivery means this may run twice. Terminal failure writes a
user-facing reason onto the book, never just a log line.

The worker connects as the service role and therefore BYPASSES RLS. Every query
below carries its own scope, and the chunks it writes get their scope columns from
the database trigger rather than from anything decided here.
"""

import asyncio
import json
import logging
import re
from uuid import UUID

from sqlalchemy import delete, insert, select

from app.clients import embeddings, llm, storage
from app.core.config import settings
from app.db.models import Book, Chapter, Chunk
from app.db.models.enums import BookKind, BookStatus, SourceFormat
from app.db.session import WorkerSessionFactory
from app.rag import chunk as chunker
from app.rag import parse, prompts
from app.services import audit
from app.services.ingest_trace import IngestTrace, attach_trace
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


class IngestFailure(Exception):
    """A failure with a message fit for the book's owner to read."""


def _mb(byte_count: int) -> str:
    return f"{byte_count / (1024 * 1024):.1f} MB"


def _chapter_source(source_format: SourceFormat, chapters: list) -> str:
    """Where the chapter boundaries came from — the question the panel exists for.

    "1 · whole document" is the honest report that a book has no structure to select
    from, and it is invisible everywhere else in the UI.
    """
    if len(chapters) == 1:
        return "whole document (no outline found)"
    if source_format is SourceFormat.PDF:
        return "from the PDF outline"
    if source_format is SourceFormat.ZIP:
        return "one per archive part"
    return "detected"


@celery_app.task(bind=True, max_retries=3, acks_late=True, queue="ingest")
def ingest_book(self, book_id: str) -> None:
    try:
        asyncio.run(_ingest(book_id))
    except (IngestFailure, parse.UnparseableDocument) as exc:
        # A bad document does not get better on a retry. UnparseableDocument is the
        # parser saying the same thing with the file's own words — a damaged ZIP, a
        # password, a member that would not read — so it takes the same exit.
        asyncio.run(_mark_failed(book_id, str(exc), _trace_of(exc)))
    except Exception as exc:
        if self.request.retries < self.max_retries:
            # Exponential backoff with jitter: a provider outage must not become a
            # thundering herd of every queued book at once.
            raise self.retry(
                exc=exc, countdown=2 ** (self.request.retries + 2), jitter=True
            ) from exc
        asyncio.run(
            _mark_failed(book_id, "Ingestion failed. Try uploading again.", _trace_of(exc))
        )
        raise


def _trace_of(exc: BaseException) -> dict | None:
    """The trace the failing run carried up, if it got far enough to have one."""
    return getattr(exc, "ingest_trace", None)


async def _set_status(book_id: UUID, status: BookStatus) -> None:
    async with WorkerSessionFactory() as session:
        book = await session.get(Book, book_id)
        if book is not None:
            book.status = status
        await session.commit()


async def _mark_failed(book_id: str, reason: str, trace: dict | None = None) -> None:
    """Write the terminal state, and never raise while doing it.

    If this throws, the book sits on its last transient status forever and the user
    watches a spinner that will never resolve — a worse outcome than the original
    failure, and harder to diagnose.

    The trace is stored here rather than only on the happy path because a failed run
    is exactly the run whose trace somebody wants to read: it says which stage the
    book got to before it stopped.
    """
    try:
        async with WorkerSessionFactory() as session:
            book = await session.get(Book, UUID(book_id))
            if book is not None:
                book.status = BookStatus.FAILED
                book.error = reason
                if trace is not None:
                    book.ingest_trace = trace
            await session.commit()
    except Exception:
        logger.exception("could not record ingest failure", extra={"book_id": book_id})
    logger.error("ingest failed", extra={"book_id": book_id, "reason": reason})


async def _ingest(raw_book_id: str) -> None:
    book_id = UUID(raw_book_id)

    async with WorkerSessionFactory() as session:
        book = await session.scalar(select(Book).where(Book.id == book_id))
        if book is None:
            logger.warning("book vanished before ingest", extra={"book_id": raw_book_id})
            return
        storage_path = book.storage_path
        source_format = book.source_format
        byte_size = book.byte_size

    # Every `raise` below carries this up on the exception, so the Advanced panel can
    # show which stage a failed book reached (:func:`_mark_failed`).
    trace = IngestTrace(source_format=source_format.value, byte_size=byte_size)

    def fail(exc: Exception, outcome: str = "failed") -> Exception:
        return attach_trace(exc, trace.finish(outcome=outcome, reason=str(exc)))

    # --- parse ---------------------------------------------------------------
    await _set_status(book_id, BookStatus.PARSING)
    data = await storage.download(settings.bucket_books, storage_path)
    trace.step("download", f"{_mb(len(data))} from storage")

    # CPU-bound, so off the event loop even here in the worker.
    try:
        pages = await asyncio.to_thread(parse.parse, data, source_format)
    except parse.UnparseableDocument as exc:
        raise fail(exc) from exc

    if source_format is SourceFormat.ZIP:
        # The manifest is the whole point of this panel for a ZIP: a page count
        # cannot tell you whether chapter 7 made it in (D26).
        names = await asyncio.to_thread(parse.zip_member_names, data)
        trace.manifest(names)
        trace.step("unzip", f"{len(names)} parts combined in reading order")

    trace.step("parse", f"{source_format.value} · {len(pages)} pages")

    if len(pages) > settings.max_page_count:
        raise fail(
            IngestFailure(
                f"That document has {len(pages)} pages; "
                f"the limit is {settings.max_page_count}."
            )
        )

    if parse.looks_scanned(pages):
        async with WorkerSessionFactory() as session:
            scanned = await session.get(Book, book_id)
            if scanned is not None:
                scanned.needs_ocr = True
            await session.commit()
        raise fail(
            IngestFailure(
                "This looks like a scanned document with no selectable text. "
                "Upload a text-based file instead."
            )
        )

    # --- chapters + chunks ---------------------------------------------------
    await _set_status(book_id, BookStatus.CHUNKING)
    chapters = await asyncio.to_thread(chunker.detect_chapters, pages, data, source_format)
    trace.step("chapters", f"{len(chapters)} · {_chapter_source(source_format, chapters)}")

    chunks = await asyncio.to_thread(chunker.chunk_pages, pages, chapters)
    trace.step(
        "chunk", f"{len(chunks)} passages · {chunker.chunk_token_budget()}-token budget"
    )

    if not chunks:
        raise fail(IngestFailure("No readable text was found in that document."))

    # --- embed ---------------------------------------------------------------
    await _set_status(book_id, BookStatus.EMBEDDING)
    vectors = await embeddings.embed_texts([c.text for c in chunks])
    batches = -(-len(chunks) // max(1, settings.embedding_batch_size))
    trace.step(
        "embed",
        f"{len(vectors)} vectors · {settings.embedding_model} · {batches} batches",
    )
    if len(vectors) != len(chunks):
        raise fail(IngestFailure("The embedding service returned an unexpected result."))

    # --- store ---------------------------------------------------------------
    async with WorkerSessionFactory() as session:
        book = await session.get(Book, book_id)
        if book is None:
            return

        # Idempotency: a retry re-runs from the start, so prior output goes first.
        # A partially re-indexed book would serve a mix of two chunkings.
        await session.execute(delete(Chunk).where(Chunk.book_id == book.id))
        await session.execute(delete(Chapter).where(Chapter.book_id == book.id))

        # One flush for every chapter rather than one per chapter. The ids are needed
        # to hang chunks off, so this cannot be a bare executemany, but SQLAlchemy
        # batches the INSERTs behind a single flush and returns the ids together.
        chapter_rows = [
            Chapter(
                book_id=book.id,
                index=chapter.index,
                title=chapter.title,
                page_start=chapter.page_start,
                page_end=chapter.page_end,
            )
            for chapter in chapters
        ]
        session.add_all(chapter_rows)
        await session.flush()
        chapter_ids: dict[int, UUID] = {
            chapter.index: row.id for chapter, row in zip(chapters, chapter_rows, strict=True)
        }

        # A Core executemany rather than N `session.add()` calls. A 600-page book is
        # thousands of chunks, and the unit-of-work path costs an identity-map entry
        # and a per-row statement for each of them; this is one statement. The
        # `chunks_sync_scope` trigger still fires per row -- it is a BEFORE INSERT
        # trigger and that is what makes the denormalised scope columns trustworthy --
        # but it now fires inside the database instead of once per network round trip.
        await session.execute(
            insert(Chunk),
            [
                {
                    "book_id": book.id,
                    "chapter_id": chapter_ids.get(piece.chapter_index),
                    "index": piece.index,
                    "text": piece.text,
                    "embedding": vector,
                    "page": piece.page,
                    "token_count": piece.token_count,
                    # Overwritten by the chunks_sync_scope trigger from the book.
                    # Supplied only because the columns are NOT NULL.
                    "owner_id": book.owner_id,
                    "scope": book.scope,
                }
                for piece, vector in zip(chunks, vectors, strict=True)
            ],
        )

        trace.step("store", f"{len(chapter_rows)} chapters, {len(chunks)} passages written")
        trace.count(
            pages=len(pages),
            chapters=len(chapters),
            chunks=len(chunks),
            vectors=len(vectors),
        )

        book.page_count = len(pages)
        book.status = BookStatus.READY
        book.error = None
        book.ingest_trace = trace.finish(outcome="ready")
        await session.commit()

    logger.info(
        "ingest complete",
        extra={"book_id": raw_book_id, "pages": len(pages), "chunks": len(chunks)},
    )

    # Deliberately after the commit that set READY. The book is fully answerable at
    # this point; what follows only fills in how it should be *described*, so it runs
    # where a failure costs three null columns and nothing else.
    await _classify(book_id, [piece.text for piece in chunks[:4]])


async def _classify(book_id: UUID, passages: list[str]) -> None:
    """Fill in kind, genre and summary. Best effort, and never raises.

    Runs on the ingest queue rather than the llm queue because it is one short call
    at the tail of a task that has already run for minutes, and moving it would mean
    a second task, a second row lookup and a queue hop to save nothing.

    Nothing here narrows retrieval. `kind` sets the tutor's register, `genre` and
    `summary` are shown to the reader, and a book with all three null is answered
    exactly as well as one with them filled -- which is why this is allowed to fail
    quietly rather than being retried or marking the book bad.
    """
    if not settings.book_classification_enabled or not passages:
        return

    async with WorkerSessionFactory() as session:
        book = await session.get(Book, book_id)
        title = book.title if book is not None else None
    if title is None:
        return

    try:
        raw = await llm.complete(
            prompts.classify_book_prompt(title, passages),
            # `_json_object` parses this reply, so ask the provider to constrain it --
            # the same reason every other parsing caller does. And this runs in a
            # worker at the end of an ingest, so it takes generation's model.
            json_object=True,
            model=settings.generation_model,
        )
        payload = _json_object(raw)
        kind = BookKind(str(payload.get("kind", "")).strip().lower())
        genre = str(payload.get("genre") or "").strip()[:80] or None
        summary = str(payload.get("summary") or "").strip()[:400] or None
    except Exception as exc:  # noqa: BLE001 -- see docstring
        logger.info(
            "book classification skipped",
            extra={"book_id": str(book_id), "error": str(exc)[:200]},
        )
        return

    async with WorkerSessionFactory() as session:
        book = await session.get(Book, book_id)
        if book is not None:
            book.kind = kind
            book.genre = genre
            book.summary = summary
        await session.commit()

    logger.info(
        "book classified",
        extra={"book_id": str(book_id), "kind": kind.value, "genre": genre},
    )


def _json_object(raw: str) -> dict:
    """The one JSON object in a model response, fence or no fence.

    Extracts; it does not repair. A repaired response is a response nobody validated.
    """
    text = raw.strip()
    if "```" in text:
        blocks = re.findall(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if blocks:
            text = blocks[0].strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in response")
    payload = json.loads(text[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("response was not a JSON object")
    return payload


@celery_app.task(bind=True, max_retries=3, acks_late=True, queue="ingest")
def delete_book(self, book_id: str, actor_id: str | None = None) -> None:
    """Remove a book, everything derived from it, and its stored file.

    One task, because a half-deleted book is worse than either state: chunks whose
    vectors still answer questions about material the owner believes is gone.
    """
    asyncio.run(_delete(book_id, actor_id))


async def _delete(raw_book_id: str, actor_id: str | None) -> None:
    book_id = UUID(raw_book_id)

    async with WorkerSessionFactory() as session:
        book = await session.get(Book, book_id)
        if book is None:
            # Already gone. Deletion is idempotent by design: at-least-once delivery
            # means this task can arrive twice for the same book.
            return
        storage_path, title, scope = book.storage_path, book.title, book.scope.value

    # Storage first. If the row went first and this failed, the object would become
    # unreachable garbage that nothing names.
    await storage.delete(settings.bucket_books, storage_path)

    async with WorkerSessionFactory() as session:
        book = await session.get(Book, book_id)
        if book is not None:
            # chapters and chunks -- text and vectors together -- go by cascade.
            # Because the vector is a column of the chunk row rather than a document
            # in a second store, there is no separate index to clean afterwards.
            await session.delete(book)

        await audit.record_as(
            session,
            actor_id=UUID(actor_id) if actor_id else None,
            action="book.deleted",
            target_type="book",
            target_id=book_id,
            metadata={"title": title, "scope": scope},
        )
        await session.commit()

    logger.info("book deleted", extra={"book_id": raw_book_id})
