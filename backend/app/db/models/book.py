"""books / chapters / chunks. Phase 3.

Mirrors supabase/migrations/. SQLAlchemy does not own this schema and never
creates it.
"""

import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey
from app.db.models.enums import BookKind, BookScope, BookStatus, SourceFormat


def _pg_enum(python_enum, name: str):
    return SAEnum(
        python_enum,
        name=name,
        schema="public",
        create_type=False,
        values_callable=lambda enum: [member.value for member in enum],
    )


class Book(Base, UUIDPrimaryKey, Timestamps):
    __tablename__ = "books"

    owner_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    # The security-critical column. Set from the principal, never from a request body.
    scope: Mapped[BookScope] = mapped_column(_pg_enum(BookScope, "book_scope"), nullable=False)

    title: Mapped[str] = mapped_column(String, nullable=False)
    author: Mapped[str | None] = mapped_column(String, nullable=True)
    source_format: Mapped[SourceFormat] = mapped_column(
        _pg_enum(SourceFormat, "source_format"), nullable=False
    )
    storage_path: Mapped[str] = mapped_column(String, nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    status: Mapped[BookStatus] = mapped_column(
        _pg_enum(BookStatus, "book_status"), nullable=False
    )

    # --- what sort of book this is -------------------------------------------
    # Written by ingest from the opening chunks, best effort. All three are nullable
    # because classification is an LLM call that is allowed to fail without failing
    # the ingest: a book with no `kind` is still fully readable and answerable, and
    # the tutor simply uses a neutral register.
    #
    # None of these ever narrows retrieval. `kind` sets the register (you do not
    # explain a novel the way you explain a textbook); `genre` and `summary` are
    # shown to the reader and given to the tutor so it can introduce a citation as
    # "your organic chemistry text" instead of dumping a bare title. Which book
    # answers a question is decided from the retrieved chunks in `app/rag/rank.py`.
    kind: Mapped[BookKind | None] = mapped_column(_pg_enum(BookKind, "book_kind"), nullable=True)
    genre: Mapped[str | None] = mapped_column(String, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # A user-facing reason, not a stack trace. A spinner that never resolves is the
    # worst possible report of a known failure.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    needs_ocr: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # What the worker actually did, for the Advanced panel on the book card. Written
    # at the end of every run, successful or failed -- a failed run is precisely the
    # one somebody wants to read. Content-free by the recorder's construction
    # (app/services/ingest_trace.py): counts, durations, filenames, never page text.
    ingest_trace: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class Chapter(Base, UUIDPrimaryKey):
    __tablename__ = "chapters"

    book_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("books.id", ondelete="CASCADE"), nullable=False
    )
    index: Mapped[int] = mapped_column("index", Integer, nullable=False)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    page_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Chunk(Base, UUIDPrimaryKey):
    __tablename__ = "chunks"

    book_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("books.id", ondelete="CASCADE"), nullable=False
    )
    chapter_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("chapters.id", ondelete="CASCADE"), nullable=True
    )
    index: Mapped[int] = mapped_column("index", Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(384), nullable=True)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Denormalised from books by trigger (DECISIONS.md D15). Never written by
    # application code -- the trigger overwrites whatever is supplied.
    owner_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    scope: Mapped[BookScope] = mapped_column(_pg_enum(BookScope, "book_scope"), nullable=False)
