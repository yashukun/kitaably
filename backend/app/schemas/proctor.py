"""Proctoring schemas. Phase 7 — the capture path.

What is deliberately NOT here matters more than what is:

* ``ProctorEventIn`` has no ``severity`` field. The server assigns severity from a
  fixed map keyed by type; a client that wants everything to be "info" has no field
  to say so in.
* No inbound schema anywhere carries an ``integrity_score``. The score is computed
  server-side from raw events, after close, by the aggregate task.
* There is no sitter-facing *read* schema in this phase. A sitter sees nothing of
  a session beyond the acks needed to run the capture loop; the released report
  (upheld events only) is a different model arriving with Phase 8.

Copy stays observational throughout: an event is "no face detected for 42s",
never a conclusion about the person.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.db.models.enums import EventType, ProctorSessionStatus, Severity

# Types the browser may report. Server-detected types are absent: heartbeat_gap is
# written by the sweep task from silence, clock_skew by the record function from
# the divergence between claimed and actual time.
CLIENT_EVENT_TYPES = frozenset(EventType) - {EventType.HEARTBEAT_GAP, EventType.CLOCK_SKEW}


class ProctorEventIn(BaseModel):
    """One observed episode, already debounced and coalesced by the browser."""

    # Echoed back in the ack so the client can pair stills with upload URLs.
    # Never stored.
    client_ref: str = Field(max_length=64)
    type: EventType
    # The client's clock, advisory. The server stamps received_at itself.
    occurred_at: datetime
    confidence: float | None = Field(default=None, ge=0, le=1)
    duration_ms: int | None = Field(default=None, ge=0)
    occurrences: int = Field(default=1, ge=1, le=1000)
    # "I captured a still for this episode." Honored only for high severity —
    # the server mints the path and the upload URL, or neither.
    has_still: bool = False
    metadata: dict[str, Any] | None = None


class ProctorEventBatch(BaseModel):
    """~10 seconds of observations. The definer function re-checks the cap, because
    this schema does not bind a caller who speaks PostgREST directly."""

    events: list[ProctorEventIn] = Field(min_length=1, max_length=50)


class ProctorEventAck(BaseModel):
    """What the server made of one reported episode.

    ``upload_url`` is a signed Storage URL (path-relative, resolved by the browser
    against its own Supabase URL) present only when the event is high severity and
    the client said it holds a still. It is the only way an evidence object can
    come into being, so a still with no event row cannot occur.
    """

    client_ref: str | None
    accepted: bool
    event_id: UUID | None = None
    type: EventType | None = None
    severity: Severity | None = None
    upload_url: str | None = None


class ProctorEventBatchAck(BaseModel):
    results: list[ProctorEventAck] = Field(default_factory=list)


class ProctorSessionRead(BaseModel):
    """The capture loop's view of its own session — cadence and upload targets,
    nothing evaluative. Scores, verdicts and review state are not here and must
    never be: this schema is served to the person being observed."""

    id: UUID
    attempt_id: UUID
    status: ProctorSessionStatus
    already_active: bool
    # Signed upload URL for the consented baseline still; null once the session
    # is no longer active.
    baseline_upload_url: str | None = None
    # Cadence is server-decided so tuning it is not a client release.
    heartbeat_interval_seconds: int
    event_batch_interval_seconds: int
