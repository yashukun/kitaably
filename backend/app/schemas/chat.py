"""Chat schemas. Phase 4."""

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.db.models.enums import BookScope, MessageIntent, MessageRole


class ChatExportFormat(StrEnum):
    """What a conversation can be downloaded as.

    An API-only enum, not a Postgres type: nothing is stored, so adding a format
    is a code change, not a migration. ``json`` is the data contract (exactly the
    fields the transcript API returns, versioned); ``md`` is the same transcript
    as a document a person would reread.
    """

    JSON = "json"
    MARKDOWN = "md"


class ChatSessionCreate(BaseModel):
    """No scope field. A conversation reaches whatever its owner may lawfully
    reach, decided per request from the principal (DECISIONS.md D16)."""

    title: str | None = Field(default=None, max_length=200)


class ChatSessionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str | None
    created_at: datetime
    # Sorts the list. A conversation returned to yesterday belongs above one opened
    # last week and abandoned, which `created_at` gets backwards.
    last_message_at: datetime


class MessageCreate(BaseModel):
    """A question, and optionally which books to ask it of.

    ``book_ids`` is a *narrowing* and can only ever subtract. Scope is still derived
    server-side from the principal — these ids are applied on top of
    ``build_retrieval_filter``, so naming somebody else's book selects their chunks
    out of a set those chunks were never in. Empty means "all of my material", which
    is the default the UI sends.

    Capped at twenty because a picker that names more books than the reader owns is
    not a picker, and an unbounded IN list is a free query-planner denial of service.
    """

    content: str = Field(min_length=1, max_length=4000)
    book_ids: list[UUID] = Field(default_factory=list, max_length=20)


class CitationRead(BaseModel):
    """What the reader needs to go and check a claim themselves.

    `scope` is not decoration: they have to know whether a sentence came from the
    shared library they may be examined on, or from their own private upload.
    """

    chunk_id: UUID
    book_id: UUID
    book_title: str
    # Display only, and nullable because a book is answerable before it is classified.
    genre: str | None = None
    page: int | None
    scope: BookScope


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    role: MessageRole
    content: str
    # Set on the reader's messages, null on the tutor's — and null on anything
    # written before intent classification existed. The UI uses it to render a
    # greeting as a greeting rather than as a failed search.
    intent: MessageIntent | None = None
    # The mirror of `intent`, on the tutor's messages instead of the reader's: how the
    # turn ended. Null on user rows and on anything written before it was persisted.
    # The UI reads it to know whether to offer "the book does cover this" on a
    # transcript it has just reloaded, where the live pipeline event is long gone.
    outcome: str | None = None
    citations: list[CitationRead]
    created_at: datetime


class ContentFeedbackCreate(BaseModel):
    """Somebody reporting that the app failed them.

    Two surfaces file this: a reader answering a grounded refusal (`source="chat"`)
    and an author whose paper came back empty (`source="generation"`).

    Both ids are optional. A chat stream can be cut off before `record_answer` files
    the row, and the reader still saw the refusal and still deserves to answer it.

    Note what is NOT here: `diagnostics`. The client does not get to describe what
    went wrong — the server reads that off the assessment's own trace, so a report
    cannot be talked into carrying a story the app never told.
    """

    source: Literal["chat", "generation"] = "chat"
    message_id: UUID | None = None
    assessment_id: UUID | None = None
    question: str = Field(min_length=1, max_length=4000)
    book_ids: list[UUID] = Field(default_factory=list, max_length=20)
    # What the reader was looking at: refusal | no_mentions | loose for chat, or the
    # error the paper failed with for generation.
    outcome: str = Field(min_length=1, max_length=40)
    note: str | None = Field(default=None, max_length=2000)


class ContentFeedbackRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    question: str
    outcome: str
    note: str | None
    created_at: datetime
