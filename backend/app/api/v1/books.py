"""books routes. Phase 3, revised in Phase 2 (DECISIONS.md D16).

    POST   /books                     require_auth        -> always personal, 202
    GET    /books                     require_auth        (?scope=canon|personal)
    GET    /books/{book_id}           require_auth        (poll for status)
    PATCH  /books/{book_id}/scope     require_book_owner  -> share/unshare, audit_log
    POST   /books/{book_id}/retry     require_book_owner  -> 202
    DELETE /books/{book_id}           require_book_owner  -> 202, audit_log

There is one upload route and it takes no scope. Everything lands personal;
sharing is a separate, deliberate act by the book's owner.

Every route declares a guard. A route with no guard is a review failure;
a genuinely public one says Depends(allow_anonymous) so the absence is
deliberate and greppable.
"""

from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_auth, require_book_owner
from app.core.security import Principal
from app.db.models.enums import BookScope
from app.db.session import get_session
from app.schemas.book import BookAccepted, BookRead, BookScopeUpdate
from app.schemas.common import Page
from app.services import books as service

router = APIRouter(tags=["books"])


def _to_read(book, principal: Principal) -> BookRead:
    """The one place a Book becomes a response.

    `owner_is_me` is computed here from the authenticated principal rather than
    serialised off the row, so there is a single site to audit and no path where a
    client-supplied value could reach it. One construction site means one thing to
    check when the schema next changes.

    `ingest_trace` is blanked for everyone but the owner. It is content-free by the
    recorder's construction, so this is not the only thing standing between a shared
    book and its archive manifest — but how somebody else's upload was processed is
    not a reader's business, and the narrow rule is the one worth writing down.
    """
    read = BookRead.model_validate(book)
    mine = book.owner_id == principal.id
    return read.model_copy(
        update={"owner_is_me": mine, "ingest_trace": book.ingest_trace if mine else None}
    )


STREAM_BLOCK = 1024 * 1024


async def _measure(upload: UploadFile) -> int:
    """Size without reading the body into memory.

    Starlette spools an upload to a temporary file past ~1 MB, so seeking to the end
    costs nothing and never materialises the document.
    """
    if upload.size is not None:
        return upload.size

    # Fallback for a client that sent no content-length. UploadFile has no tell(),
    # so this uses the spooled file underneath — a seek on a local temp file, not a
    # read of the document.
    handle = upload.file
    handle.seek(0, 2)
    size = handle.tell()
    handle.seek(0)
    return size


async def _blocks(upload: UploadFile) -> AsyncIterator[bytes]:
    # Rewind first: sniffing seeks around the same handle.
    await upload.seek(0)
    while block := await upload.read(STREAM_BLOCK):
        yield block


@router.post("/books", status_code=status.HTTP_202_ACCEPTED)
async def upload_book(
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
    author: str | None = Form(default=None),
    principal: Principal = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> BookAccepted:
    """Upload a document. It lands private to the caller.

    Sharing is PATCH /books/{id}/scope afterwards, which is the point of them being
    two routes: uploading is not publishing, and the file picker is not the place
    where "everyone can now read this" gets decided.

    ``title`` is optional; omitted, it is taken from the file's own name. The service
    decides that, not this router — deriving a default is a fact about what a book is
    called, which is business meaning and belongs a layer down.
    """
    from app.workers.tasks.ingest import ingest_book

    # The cap is checked against the declared size before anything is read, and the
    # format is sniffed from the first few KB. The body itself is streamed straight
    # through to Storage — an 80 MB cap held in memory per concurrent upload is a
    # memory event waiting for the first busy afternoon of term.
    size = await _measure(file)

    book = await service.create_book(
        session,
        principal,
        probe=file.file,
        size=size,
        stream=_blocks(file),
        filename=file.filename,
        title=title,
        author=author,
    )

    # Commit before enqueueing. A task that starts before its row is visible fails on
    # a row that does not exist yet, and at-least-once delivery means it will not
    # politely wait. The session dependency's own commit afterwards is a no-op.
    await session.commit()
    ingest_book.delay(str(book.id))

    return BookAccepted(id=book.id, status=book.status)


@router.get("/books")
async def list_books(
    scope: BookScope | None = Query(
        default=None,
        description="Narrow to one shelf. Cannot widen what RLS already allows.",
    ),
    principal: Principal = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> Page[BookRead]:
    rows = await service.list_books(session, principal, scope=scope)
    return Page(items=[_to_read(book, principal) for book in rows])


@router.get("/books/{book_id}")
async def get_book(
    book_id: UUID,
    principal: Principal = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> BookRead:
    """Poll this for ingest status. `failed` carries a user-facing `error`."""
    return _to_read(await service.get_book(session, principal, book_id), principal)


@router.patch("/books/{book_id}/scope")
async def set_book_scope(
    book_id: UUID,
    data: BookScopeUpdate,
    principal: Principal = Depends(require_book_owner),
    session: AsyncSession = Depends(get_session),
) -> BookRead:
    """Share this book with everyone, or take it back.

    The guard proves the book is the caller's, and the `books_update_own` policy
    proves it again on the write. Both directions are audited: this is the only
    action in the product that changes who can read something.
    """
    book = await service.set_scope(session, principal, book_id, shared=data.shared)
    await session.commit()
    return _to_read(book, principal)


@router.post("/books/{book_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry_book(
    book_id: UUID,
    principal: Principal = Depends(require_book_owner),
    session: AsyncSession = Depends(get_session),
) -> BookAccepted:
    """Re-queue a book whose ingest failed.

    A failure is a state a user can act on, not a dead end — which is the whole
    reason `books.error` carries a sentence rather than a stack trace.
    """
    from app.workers.tasks.ingest import ingest_book

    book = await service.retry_ingest(session, principal, book_id)
    await session.commit()
    ingest_book.delay(str(book.id))
    return BookAccepted(id=book.id, status=book.status)


@router.delete("/books/{book_id}", status_code=status.HTTP_202_ACCEPTED)
async def delete_book(
    book_id: UUID,
    principal: Principal = Depends(require_book_owner),
    session: AsyncSession = Depends(get_session),
) -> BookAccepted:
    """Queue the book for deletion.

    202 rather than 204: storage, chapters, chunks and the row all go in one task,
    and the actor is carried across so the audit row names a person rather than the
    worker that happened to run it.
    """
    from app.workers.tasks.ingest import delete_book as delete_task

    book = await service.delete_book(session, principal, book_id)
    await session.commit()
    delete_task.delay(str(book.id), str(principal.id))
    return BookAccepted(id=book.id, status=book.status)
