"""Book schemas. Phase 3."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.db.models.enums import BookKind, BookScope, BookStatus, SourceFormat


class BookRead(BaseModel):
    """Note there is no BookCreate with a `scope` field, deliberately.

    Scope is decided server-side from the principal. Accepting it here would make the
    central privacy boundary a client-supplied value.

    Note also what this does *not* carry: `owner_id`. The UI needs to know whether
    to offer Share and Delete, which is one bit — `owner_is_me` — and shipping the
    raw id instead would hand every reader of the shared library a list of user ids
    to answer a yes/no question with.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    author: str | None
    scope: BookScope
    source_format: SourceFormat
    page_count: int | None
    status: BookStatus
    error: str | None
    needs_ocr: bool

    # Written by ingest, best effort, and all three nullable — a book is fully
    # answerable before it is classified. `kind` sets the tutor's register; the other
    # two are shown to the reader. None of them narrows retrieval, so a wrong value
    # here is a cosmetic error rather than material nobody can reach.
    kind: BookKind | None = None
    genre: str | None = None
    summary: str | None = None

    created_at: datetime
    updated_at: datetime

    # Filled by the router from the authenticated principal, never from the row.
    owner_is_me: bool = False

    # What the worker did while reading this book, for the Advanced panel. Sent to
    # the book's OWNER only — the router blanks it for everybody else (`_to_read`).
    # The column itself is content-free by the recorder's construction, so this is
    # the narrower of two layers rather than the only one: a canon book's row is
    # readable by every signed-in user, and none of them uploaded it.
    ingest_trace: dict | None = None


class BookScopeUpdate(BaseModel):
    """The one request body that may influence scope — and note what it is not.

    It carries a boolean, not a ``BookScope``. The client says "share this" or
    "stop sharing this"; the server decides which enum value that means. A body
    that named the scope directly would be one refactor away from accepting a value
    nobody intended, and this is the column the whole privacy model rests on.

    Authorization is still elsewhere: the route's guard proves ownership, and the
    ``books_update_own`` policy proves it again on the write.
    """

    shared: bool


class BookAccepted(BaseModel):
    """202 response: the work is queued, poll the resource for status."""

    id: UUID
    status: BookStatus

