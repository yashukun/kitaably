"""proctoring routes. Phase 7 — the capture path.

    POST /attempts/{attempt_id}/proctor-session            require_attempt_sitter  -> 201
    POST /attempts/{attempt_id}/proctor-session/events     require_attempt_sitter
    POST /attempts/{attempt_id}/proctor-session/heartbeat  require_attempt_sitter  -> 204

Addressed by attempt rather than by session id, deliberately: the sitter has no
SELECT on proctor tables at all, so a session-id path would need a definer lookup
just to authorize itself — whereas the caller's relationship to the *attempt* is
exactly what ``require_attempt_sitter`` already verifies. One session per attempt
makes the two addresses equivalent.

There is no close route: the session closes inside the submit transaction
(api/v1/attempts.py), and an abandoned one is closed by the sweep task. There is
NO sitter-facing read of events, scores or review state anywhere — sitter-visible
proctoring data will be served only through a released-report view, upheld events
only (Phase 8).

The author's review routes (verdicts, release, clear) are Phase 8 and land here
with their own guards when the review gate is built.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_attempt_sitter
from app.core.security import Principal
from app.db.session import get_session
from app.schemas.proctor import (
    ProctorEventBatch,
    ProctorEventBatchAck,
    ProctorSessionRead,
)
from app.services import proctoring as service

router = APIRouter(tags=["proctoring"])


@router.post(
    "/attempts/{attempt_id}/proctor-session", status_code=status.HTTP_201_CREATED
)
async def open_proctor_session(
    attempt_id: UUID,
    principal: Principal = Depends(require_attempt_sitter),
    session: AsyncSession = Depends(get_session),
) -> ProctorSessionRead:
    """Open the monitoring session, or resume the active one after a reload.

    Returns the capture cadence and a signed upload URL for the consented baseline
    still. Nothing evaluative is in this payload, ever — it is served to the
    person being observed.
    """
    return await service.open_session(session, principal, attempt_id)


@router.post("/attempts/{attempt_id}/proctor-session/events")
async def record_proctor_events(
    attempt_id: UUID,
    batch: ProctorEventBatch,
    principal: Principal = Depends(require_attempt_sitter),
    session: AsyncSession = Depends(get_session),
) -> ProctorEventBatchAck:
    """~10 seconds of debounced observations.

    The server assigns severity from the fixed map and stamps its own clock;
    everything in the body is advisory. High-severity events the client holds a
    still for come back with a one-shot signed upload URL.
    """
    return await service.record_events(session, principal, attempt_id, batch)


@router.post(
    "/attempts/{attempt_id}/proctor-session/heartbeat",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def proctor_heartbeat(
    attempt_id: UUID,
    principal: Principal = Depends(require_attempt_sitter),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Proof of life, every ~15s. Silence becomes a heartbeat_gap event server-side
    — the easiest attack on a browser detector is to stop reporting, so absence is
    recorded as evidence rather than read as calm."""
    await service.heartbeat(session, principal, attempt_id)
