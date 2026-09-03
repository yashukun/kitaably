"""chat_sessions and chat_messages. Phase 4.

A conversation is private to the person having it. Nobody reads anybody else's,
and the RLS policies say so — there is no account that can.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey
from app.db.models.enums import MessageIntent, MessageRole


class ChatSession(Base, UUIDPrimaryKey, Timestamps):
    __tablename__ = "chat_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    # No scope column: a conversation's lawful reach is the caller's own, derived
    # from the principal at retrieval time (DECISIONS.md D16). Storing it on the
    # session would freeze an access grant that is supposed to be re-evaluated.
    title: Mapped[str | None] = mapped_column(String, nullable=True)

    # Sorts the session list. A conversation you returned to yesterday belongs above
    # one you opened last week and abandoned, which `created_at` gets backwards.
    # NOT NULL with a database default, so it needs `server_default` or SQLAlchemy
    # INSERTs NULL into it (tests/test_model_defaults.py).
    last_message_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ChatMessage(Base, UUIDPrimaryKey):
    __tablename__ = "chat_messages"

    session_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[MessageRole] = mapped_column(
        SAEnum(
            MessageRole,
            name="message_role",
            schema="public",
            create_type=False,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # What the reader was doing, set on the user's row and null on the assistant's.
    # Nullable because messages written before this column existed have no answer,
    # and inventing `question` for them would be a lie in a transcript.
    intent: Mapped[MessageIntent | None] = mapped_column(
        SAEnum(
            MessageIntent,
            name="message_intent",
            schema="public",
            create_type=False,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=True,
    )

    # How the turn ended -- the mirror of `intent`, on the assistant's row instead of
    # the reader's: answered, loose, refusal, no_mentions, book_facts, pick_book,
    # needs_two_books, conversational.
    #
    # Persisted because the UI has to act on it after a reload. Until this column the
    # outcome existed only on the ephemeral `pipeline` SSE event, so a refusal read
    # back from the transcript looked exactly like a good answer -- and the offer to
    # report a gap vanished the moment somebody refreshed on their way to check the
    # book. Text and not an enum: nothing in the database dispatches on it.
    outcome: Mapped[str | None] = mapped_column(Text, nullable=True)

    # [{chunk_id, book_id, page, scope}] — scope travels with the citation so the UI
    # can label a claim as coming from the shared library or the reader's own upload.
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    token_usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
