"""proctor_sessions and proctor_events. Phase 7 capture; Phase 8 reviews it.

``released_at IS NULL``  =>  the person sitting it sees nothing at all.

RLS: the assessment's author may SELECT, always. The sitter has NO POLICY AT ALL
on these tables — their writes go through the SECURITY DEFINER functions in the
proctoring migration, and sitter-visible data will be served through a
released-report view (Phase 8) exposing upheld events only. The absence of a
policy is the enforcement; it cannot be defeated by a forgotten WHERE.

Severity is assigned server-side by ``public.proctor_severity()`` — the fixed map
lives in the database so a direct PostgREST rpc call cannot route around it. The
Python mirror in ``services/proctoring.py`` exists for the integrity *weights*,
which are a scoring concern, not an authorization one.

Mirrors supabase/migrations/. SQLAlchemy does not own this schema and never
creates it.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Float, ForeignKey, Integer, Text, func, text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKey
from app.db.models.enums import (
    AuthorVerdict,
    EventType,
    ProctorSessionStatus,
    ReviewStatus,
    Severity,
)


def _pg_enum(python_enum, name: str):
    return SAEnum(
        python_enum,
        name=name,
        schema="public",
        create_type=False,
        values_callable=lambda enum: [member.value for member in enum],
    )


class ProctorSession(Base, UUIDPrimaryKey):
    __tablename__ = "proctor_sessions"

    # One session per attempt, ever. A reload resumes the session it finds.
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("attempts.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )

    status: Mapped[ProctorSessionStatus] = mapped_column(
        _pg_enum(ProctorSessionStatus, "proctor_session_status"),
        nullable=False,
        server_default=text("'active'::public.proctor_session_status"),
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # '{session_id}/baseline.jpg' in the evidence bucket. The object may lag or
    # never arrive (camera denied) — a missing object is an observation, not an error.
    baseline_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Drives heartbeat_gap detection in the sweep task. Absence is evidence.
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # SERVER-computed by aggregate_session from raw events, after close. A
    # queue-ordering device for the author's review — never a verdict, never a
    # grade input, never sitter-visible before release.
    integrity_score: Mapped[int | None] = mapped_column(Integer, nullable=True)

    review_status: Mapped[ReviewStatus] = mapped_column(
        _pg_enum(ReviewStatus, "review_status"),
        nullable=False,
        server_default=text("'pending'::public.review_status"),
    )
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("profiles.id", ondelete="SET NULL"),
        nullable=True,
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Shown to the sitter on release. Observational language only.
    reviewer_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Null means the sitter sees nothing at all, whatever else is true of the row.
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Retention TTL for the stills, set at close from server config and enforced by
    # the purge_evidence beat task (Phase 11).
    evidence_purge_after: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ProctorEvent(Base, UUIDPrimaryKey):
    __tablename__ = "proctor_events"

    proctor_session_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("proctor_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Client clock, advisory. Large divergence is itself an event (clock_skew).
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Server clock, authoritative for ordering.
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    type: Mapped[EventType] = mapped_column(
        _pg_enum(EventType, "proctor_event_type"), nullable=False
    )
    # Assigned by public.proctor_severity(), never by the client.
    severity: Mapped[Severity] = mapped_column(
        _pg_enum(Severity, "severity"), nullable=False
    )

    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Coalesced episode count: "tab lost focus 6 times" is one row.
    occurrences: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )

    # '{session_id}/{event_id}.jpg' in the evidence bucket. High severity only.
    evidence_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    # `metadata` is the column name (docs/DATA-MODEL.md); the attribute is renamed
    # because Declarative reserves `metadata` for the registry.
    event_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    author_verdict: Mapped[AuthorVerdict] = mapped_column(
        _pg_enum(AuthorVerdict, "author_verdict"),
        nullable=False,
        server_default=text("'unreviewed'::public.author_verdict"),
    )
