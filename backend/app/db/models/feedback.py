"""content_feedback — a reader saying the book DOES cover it. Phase 7b.

The tutor refuses when retrieval finds nothing (invariant 5), and it is right to. But
a refusal is where the product used to stop: the reader was told their books do not
cover it and had nowhere to say *"they do, it is on page 40"*. Aggregate counters said
refusals happened; nothing said which question, over which book, or told the one person
who could fix it.

Readable by its author, and by the owner of any book it names — the owner is the point.
Append-only: the migration grants SELECT and INSERT and nothing else, so a report cannot
be edited into saying something its writer did not say.

Mirrors supabase/migrations/. SQLAlchemy does not own this schema and never creates it.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKey


class ContentFeedback(Base, UUIDPrimaryKey):
    __tablename__ = "content_feedback"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("profiles.id", ondelete="CASCADE"),
        nullable=False,
    )

    # 'chat' | 'generation'. A column rather than something inferred from which id is
    # set: "no assessment_id" and "this was not a generation failure" are different
    # statements, and only one of them is safe to read off a null.
    source: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'chat'")
    )

    assessment_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("assessments.id", ondelete="SET NULL"),
        nullable=True,
    )

    # `SET NULL`, not cascade: a report outlives the conversation that produced it.
    # A gap is a fact about the book, and deleting a chat session should not quietly
    # delete the evidence of one.
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("chat_messages.id", ondelete="SET NULL"),
        nullable=True,
    )

    # The scope that was searched, denormalised rather than joined through the message
    # -- the message may be gone, and "which books did this fail over" still needs an
    # answer. Also what the owner-side RLS policy matches on.
    book_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )

    question: Mapped[str] = mapped_column(Text, nullable=False)

    # refusal | no_mentions | loose. Same vocabulary as ChatMessage.outcome.
    outcome: Mapped[str] = mapped_column(Text, nullable=False)

    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # What the app knew when it failed -- the generation trace summary, or the chat
    # turn's outcome. Captured here rather than left in logs that rotate, so a report
    # can be investigated without reproducing the failure first. Content-free by the
    # same rule the trace follows: counts, timings and failure tags, never a provider
    # error string, because those quote the prompt and the prompt quotes the book.
    diagnostics: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    # The database fills this, so the model must say so or SQLAlchemy INSERTs NULL
    # into a NOT NULL column (`tests/test_model_defaults.py` enforces this).
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
