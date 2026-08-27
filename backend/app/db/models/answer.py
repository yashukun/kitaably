"""answers — one response to one question. Phase 6.

An LLM grade lands with ``grader='llm'``. An override by the author sets
``grader='human'`` and never discards ``llm_rationale``, so the original machine
judgement stays auditable — that is what makes an override a correction rather than a
cover-up.

Mirrors supabase/migrations/. SQLAlchemy does not own this schema and never creates it.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Numeric, Text, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKey
from app.db.models.enums import Grader


class Answer(Base, UUIDPrimaryKey):
    __tablename__ = "answers"

    attempt_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("attempts.id", ondelete="CASCADE"), nullable=False
    )
    question_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("questions.id", ondelete="CASCADE"), nullable=False
    )

    # The option key for mcq, prose for subjective. Null means unanswered, which grades
    # to zero without an LLM call.
    response: Mapped[str | None] = mapped_column(Text, nullable=True)

    awarded_points: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    grader: Mapped[Grader | None] = mapped_column(
        SAEnum(
            Grader,
            name="grader",
            schema="public",
            create_type=False,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=True,
    )
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_rationale: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
