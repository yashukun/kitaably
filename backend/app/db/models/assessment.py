"""assessments — a generated or hand-written paper. Phase 5.

Mirrors supabase/migrations/. SQLAlchemy does not own this schema and never creates it.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey
from app.db.models.enums import (
    AssessmentRigor,
    AssessmentStatus,
    AssessmentType,
    ResultsRelease,
)


def _pg_enum(python_enum, name: str):
    return SAEnum(
        python_enum,
        name=name,
        schema="public",
        create_type=False,
        values_callable=lambda enum: [member.value for member in enum],
    )


class Assessment(Base, UUIDPrimaryKey, Timestamps):
    __tablename__ = "assessments"

    author_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[AssessmentType] = mapped_column(
        _pg_enum(AssessmentType, "assessment_type"),
        nullable=False,
        server_default=text("'mixed'::public.assessment_type"),
    )

    # {book_ids: [...], chapter_ids: [...]}. Arrives from a request, so it is a claim:
    # generation re-checks every id against scope='canon' before drawing from it.
    source_selection: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    # What the author asked generation for: {formats, levels, instructions}. Kept
    # after generation, because "why is this paper full of true/false questions" is a
    # question the row should be able to answer. An empty object means *auto* — the
    # author skipped the picker, which is a choice rather than a missing answer.
    generation_spec: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    # How hard, for the paper as a whole. Orthogonal to a question's cognitive level:
    # the same `evaluate` question is written one way for a beginner and another for
    # a graduate viva.
    rigor: Mapped[AssessmentRigor] = mapped_column(
        _pg_enum(AssessmentRigor, "assessment_rigor"),
        nullable=False,
        server_default=text("'medium'::public.assessment_rigor"),
    )

    question_count: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    status: Mapped[AssessmentStatus] = mapped_column(
        _pg_enum(AssessmentStatus, "assessment_status"),
        nullable=False,
        server_default=text("'draft'::public.assessment_status"),
    )

    # Null until publish. Minted by the database, never by application code — see
    # public.generate_share_token() for why random() is not good enough.
    share_token: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)

    proctoring_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    results_release: Mapped[ResultsRelease] = mapped_column(
        _pg_enum(ResultsRelease, "results_release"),
        nullable=False,
        server_default=text("'immediate'::public.results_release"),
    )

    opens_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closes_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Frozen at publish. Stored rather than summed on read so that voiding a question
    # later cannot silently rescale a paper somebody already sat.
    max_score: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)

    # A user-facing reason, not a stack trace. A spinner that never resolves is the
    # worst possible report of a known failure.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The other half of that rule, for the failure that is not a failure: a paper that
    # generated successfully but came back shorter than it was asked for. `error` means
    # there is nothing here; this means there is something here and it is not what you
    # asked for. A short paper with both columns null is a known outcome reported to
    # nobody, which is the same bug wearing different clothes.
    generation_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The pipeline trace of the last generation run — steps, per-call timings, and a
    # summary. Content-free (counts and formats, never question text): a sitter can
    # read this row under RLS, and RLS cannot hide a column. Built only by
    # services/generation_trace.py, whose API is what enforces that rule.
    generation_trace: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
