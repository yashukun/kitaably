"""Proctoring capture and scoring. Phase 7; the review gate arrives in Phase 8.

The client is untrusted. Severity is assigned server-side from a fixed map keyed
by event type — the authoritative copy is ``public.proctor_severity()`` in the
proctoring migration, because the definer functions are reachable over PostgREST
rpc and must not depend on this process being in the path. The map here is a
mirror, and ``tests/test_proctoring.py`` asserts the two cannot drift.

Absence is evidence. A missing heartbeat, a stopped camera, or a silent session is
itself an event, because the easiest attack is to stop reporting.

No proctoring signal reaches the person sitting an assessment before its author
releases it. There is no auto-release, no auto-void, no auto-zero, no 'suspicious'
badge — and the integrity score computed here is a queue-ordering device for the
author's review, never a verdict and never an input to a grade.

Language: report what was observed ("no face detected for 42s"), never a
conclusion about the person. The inference is the reviewer's to make.

Why the sitter path speaks to SECURITY DEFINER functions rather than the ORM: the
sitter has no RLS policy on the proctor tables at all — that absence is what makes
"released_at IS NULL means they see nothing" undefeatable — so their writes must
arrive through functions that verify ``auth.uid()`` against the attempt themselves.
"""

import json
import logging
from typing import Any, NoReturn
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients import storage
from app.core.config import settings
from app.core.errors import Conflict, NotFound, RateLimited, ValidationFailed
from app.core.metrics import proctor_events_recorded_total
from app.core.security import Principal
from app.db.models import ProctorEvent
from app.db.models.enums import AuthorVerdict, EventType, Severity
from app.schemas.proctor import (
    ProctorEventAck,
    ProctorEventBatch,
    ProctorEventBatchAck,
    ProctorSessionRead,
)

logger = logging.getLogger(__name__)

# Mirror of public.proctor_severity() — the scoring tables below key off it, and
# the test suite holds it equal to the SQL map by parsing the migration.
SEVERITY_BY_TYPE: dict[EventType, Severity] = {
    EventType.SESSION_START: Severity.INFO,
    EventType.SESSION_END: Severity.INFO,
    EventType.HEARTBEAT_GAP: Severity.MEDIUM,
    EventType.NO_FACE: Severity.MEDIUM,
    EventType.MULTIPLE_FACES: Severity.HIGH,
    EventType.FACE_MISMATCH: Severity.HIGH,
    EventType.GAZE_AWAY: Severity.LOW,
    EventType.HEAD_POSE_AWAY: Severity.LOW,
    EventType.PHONE_VISIBLE: Severity.MEDIUM,
    EventType.TAB_BLUR: Severity.MEDIUM,
    EventType.WINDOW_BLUR: Severity.MEDIUM,
    EventType.FULLSCREEN_EXIT: Severity.MEDIUM,
    EventType.COPY: Severity.MEDIUM,
    EventType.PASTE: Severity.MEDIUM,
    EventType.CONTEXT_MENU: Severity.LOW,
    EventType.CAMERA_DENIED: Severity.HIGH,
    EventType.CAMERA_STOPPED: Severity.HIGH,
    EventType.CLOCK_SKEW: Severity.LOW,
    EventType.SCREEN_SHARE_DENIED: Severity.HIGH,
    EventType.SCREEN_SHARE_STOPPED: Severity.HIGH,
    # A second display is a real observation, but docked laptops make it common
    # enough that `high` would drown review queues in hardware arrangements.
    EventType.MULTIPLE_DISPLAYS: Severity.MEDIUM,
}

# Integrity weights: (points per occurrence, points per minute of duration).
#
# Tuned generous to the sitter, deliberately. gaze/head-pose are the noisiest
# detectors in the set and phone_visible the weakest (D10), so they carry weights
# that cannot sink a score on their own; camera_denied and multiple_faces are the
# observations an author most needs surfaced first. The score orders the review
# queue — it must never gate a grade or reach a sitter unreleased, so a weight
# here is a triage priority, not a penalty.
SCORE_WEIGHTS: dict[EventType, tuple[float, float]] = {
    EventType.SESSION_START: (0.0, 0.0),
    EventType.SESSION_END: (0.0, 0.0),
    EventType.HEARTBEAT_GAP: (6.0, 4.0),
    EventType.NO_FACE: (3.0, 6.0),
    EventType.MULTIPLE_FACES: (12.0, 10.0),
    EventType.FACE_MISMATCH: (15.0, 0.0),
    EventType.GAZE_AWAY: (0.5, 1.0),
    EventType.HEAD_POSE_AWAY: (0.5, 1.0),
    EventType.PHONE_VISIBLE: (4.0, 4.0),
    EventType.TAB_BLUR: (3.0, 4.0),
    EventType.WINDOW_BLUR: (2.0, 3.0),
    EventType.FULLSCREEN_EXIT: (2.0, 0.0),
    EventType.COPY: (1.0, 0.0),
    EventType.PASTE: (3.0, 0.0),
    EventType.CONTEXT_MENU: (0.5, 0.0),
    EventType.CAMERA_DENIED: (20.0, 0.0),
    EventType.CAMERA_STOPPED: (12.0, 0.0),
    EventType.CLOCK_SKEW: (1.0, 0.0),
    # The screen siblings sit just under the camera ones: an unshared screen is a
    # sitting partly unobserved, which is what the author needs surfaced first.
    EventType.SCREEN_SHARE_DENIED: (15.0, 0.0),
    EventType.SCREEN_SHARE_STOPPED: (10.0, 0.0),
    EventType.MULTIPLE_DISPLAYS: (6.0, 4.0),
}

# One event, however long or repeated, cannot dominate the whole score. A single
# four-hour no_face (a camera pointed at a wall) reads the same as a very eventful
# session would anyway — and everything past the cap is visible in the timeline.
_PER_EVENT_CAP = 25.0


def compute_integrity_score(
    events: list[ProctorEvent], *, include_dismissed: bool = True
) -> int:
    """100 − Σ (weight[type] × f(duration, occurrences)), clamped to [0, 100].

    Pure, so the arithmetic is testable without a database. Called with
    ``include_dismissed=False`` after author verdicts (Phase 8): a dismissed
    observation is excluded from any released score as well as any released report.
    """
    penalty = 0.0
    for event in events:
        if not include_dismissed and event.author_verdict is AuthorVerdict.DISMISSED:
            continue
        per_occurrence, per_minute = SCORE_WEIGHTS[event.type]
        minutes = (event.duration_ms or 0) / 60_000
        contribution = per_occurrence * event.occurrences + per_minute * minutes
        penalty += min(contribution, _PER_EVENT_CAP)
    return max(0, min(100, round(100 - penalty)))


# ---------------------------------------------------------------- sitter path
#
# Each wrapper calls its SECURITY DEFINER function and maps the returned outcome
# to a domain error. The functions verify auth.uid() themselves; the route guard
# (require_attempt_sitter) is the first line, this is the one that also binds a
# caller who speaks PostgREST directly.

_OPEN = text("select * from public.open_proctor_session(cast(:attempt_id as uuid))")
_RECORD = text(
    "select * from public.record_proctor_events("
    "cast(:attempt_id as uuid), cast(:events as jsonb))"
)
_HEARTBEAT = text("select public.proctor_heartbeat(cast(:attempt_id as uuid))")
_CLOSE = text("select * from public.close_proctor_session(cast(:attempt_id as uuid))")


def _refuse(outcome: str) -> NoReturn:
    if outcome == "not_found":
        raise NotFound("That attempt does not exist.")
    if outcome == "not_proctored":
        raise Conflict("This paper is not proctored.")
    if outcome in ("not_in_progress", "ended"):
        raise Conflict("Proctoring for this attempt has ended.")
    if outcome == "rate_limited":
        raise RateLimited("Too many observations. Slow down and retry.")
    if outcome == "rejected":
        raise ValidationFailed("That batch is not valid.")
    # An outcome the SQL function can emit but this map does not know is a bug,
    # not a refusal a sitter should ever see dressed up as one.
    raise RuntimeError(f"unhandled proctoring outcome: {outcome}")


async def open_session(
    session: AsyncSession, principal: Principal, attempt_id: UUID
) -> ProctorSessionRead:
    """Open the session for an attempt, or resume the active one.

    The baseline upload URL comes back on every open of an active session: the
    still is written to a fixed per-session path with upsert, so a resumed sitting
    re-consenting its camera refreshes the same image rather than forking history.
    """
    row = (await session.execute(_OPEN, {"attempt_id": str(attempt_id)})).first()
    if row is None or row.outcome not in ("ok",):
        _refuse(row.outcome if row is not None else "not_found")

    upload_url = await storage.create_signed_upload_url(
        settings.bucket_evidence, f"{row.session_id}/baseline.jpg"
    )
    return ProctorSessionRead(
        id=row.session_id,
        attempt_id=attempt_id,
        status="active",
        already_active=row.already,
        baseline_upload_url=upload_url,
        heartbeat_interval_seconds=settings.heartbeat_interval_seconds,
        event_batch_interval_seconds=settings.proctor_event_batch_seconds,
    )


async def record_events(
    session: AsyncSession,
    principal: Principal,
    attempt_id: UUID,
    batch: ProctorEventBatch,
) -> ProctorEventBatchAck:
    """Store one batch of observations and mint upload URLs for their stills.

    The upload URL is minted only for events the database created with an
    evidence_path — high severity, still offered — so the event row always exists
    before its object can, and nothing else can bring an evidence object into being.
    """
    payload = [
        {
            "client_ref": event.client_ref,
            "type": event.type.value,
            "occurred_at": event.occurred_at.isoformat(),
            "confidence": event.confidence,
            "duration_ms": event.duration_ms,
            "occurrences": event.occurrences,
            "has_still": event.has_still,
            "metadata": event.metadata or {},
        }
        for event in batch.events
    ]

    rows = (
        await session.execute(
            _RECORD, {"attempt_id": str(attempt_id), "events": json.dumps(payload)}
        )
    ).all()

    # A whole-batch refusal comes back as a single row with no event id.
    if len(rows) == 1 and rows[0].outcome not in ("ok", "rejected"):
        _refuse(rows[0].outcome)

    acks: list[ProctorEventAck] = []
    for row in rows:
        if row.outcome != "ok":
            acks.append(ProctorEventAck(client_ref=row.client_ref, accepted=False))
            continue
        upload_url = None
        if row.evidence_path:
            upload_url = await storage.create_signed_upload_url(
                settings.bucket_evidence, row.evidence_path
            )
        proctor_events_recorded_total.labels(row.event_type).inc()
        acks.append(
            ProctorEventAck(
                client_ref=row.client_ref,
                accepted=True,
                event_id=row.event_id,
                type=row.event_type,
                severity=row.severity,
                upload_url=upload_url,
            )
        )
    return ProctorEventBatchAck(results=acks)


async def heartbeat(
    session: AsyncSession, principal: Principal, attempt_id: UUID
) -> None:
    """Proof of life. Its absence, not its presence, is the signal that matters."""
    outcome = await session.scalar(_HEARTBEAT, {"attempt_id": str(attempt_id)})
    if outcome != "ok":
        _refuse(outcome or "not_found")


async def close_for_attempt(session: AsyncSession, attempt_id: UUID) -> UUID | None:
    """Close the attempt's session, if one exists. Idempotent.

    Called inside the submit transaction, while it still carries the sitter's
    identity — the definer function checks auth.uid() like every other write.
    Returns the session id so the caller can enqueue aggregation after commit,
    or None when the attempt was never proctored.
    """
    row = (await session.execute(_CLOSE, {"attempt_id": str(attempt_id)})).first()
    if row is None or row.outcome == "none":
        return None
    return row.session_id


def observation_summary(events: list[ProctorEvent]) -> list[dict[str, Any]]:
    """Human-readable lines for a timeline, observational by construction.

    Kept next to the scoring so the two ways of describing a session cannot drift:
    both read the same rows, and neither may conclude anything about the person.
    """
    lines = []
    for event in events:
        seconds = round((event.duration_ms or 0) / 1000)
        parts = [event.type.value.replace("_", " ")]
        if seconds:
            parts.append(f"for {seconds}s")
        if event.occurrences > 1:
            parts.append(f"({event.occurrences} occurrences)")
        lines.append(
            {
                "event_id": event.id,
                "occurred_at": event.occurred_at,
                "severity": event.severity,
                "text": " ".join(parts),
            }
        )
    return lines
