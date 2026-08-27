"""attempts — one person's sitting of one assessment. Phase 6.

The deadline is server-authoritative. A client clock is a suggestion.

Mirrors supabase/migrations/. SQLAlchemy does not own this schema and never creates it.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric, Text, func, text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKey
from app.db.models.enums import AttemptStatus


class Attempt(Base, UUIDPrimaryKey):
    __tablename__ = "attempts"

    assessment_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("assessments.id", ondelete="CASCADE"), nullable=False
    )
    sitter_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )

    status: Mapped[AttemptStatus] = mapped_column(
        SAEnum(
            AttemptStatus,
            name="attempt_status",
            schema="public",
            create_type=False,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
        server_default=text("'in_progress'::public.attempt_status"),
    )

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Computed server-side at start. Never accepted from a client, never recomputed
    # afterwards — extending it mid-sitting would be a different exam.
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    score: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    max_score: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    graded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Null means the sitter sees no marks at all, whatever else is true of the row.
    results_released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    grading_error: Mapped[str | None] = mapped_column(Text, nullable=True)
