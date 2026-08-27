"""queue: maintenance, driven by beat. Phase 7 (sweep); purge_evidence is Phase 11.

sweep_stale_sessions is the server half of "absence is evidence": the easiest
attack on a browser-side detector is to stop reporting, so silence is written
down rather than read as calm. Every minute it:

* records a ``heartbeat_gap`` event on active sessions silent past
  HEARTBEAT_GAP_SECONDS — one event per silence episode, not one per sweep;
* closes sessions as 'aborted' when the silence has outlived
  PROCTOR_ABANDON_SECONDS or the attempt's own deadline has passed, then queues
  aggregation — an abandoned attempt still reaches the author's review queue.

purge_evidence (delete stills past evidence_purge_after) arrives in Phase 11 with
the rest of operations hardening; the deadline column is already being written by
aggregate_session so nothing accumulates unbounded meaning in the meantime.

Runs as the service role and bypasses RLS, so every query carries its scope
predicate explicitly. Idempotent: the gap check keys on last_heartbeat_at, so a
sweep that runs twice records one gap, and closing a closed session is a no-op.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, exists, or_, select

from app.core.config import settings
from app.db.models import Attempt, ProctorEvent, ProctorSession
from app.db.models.enums import EventType, ProctorSessionStatus, Severity
from app.db.session import WorkerSessionFactory
from app.services.proctoring import SEVERITY_BY_TYPE
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, acks_late=True, queue="maintenance")
def sweep_stale_sessions(self) -> None:
    asyncio.run(_sweep())


async def _sweep() -> None:
    now = datetime.now(UTC)
    gap_before = now - timedelta(seconds=settings.heartbeat_gap_seconds)
    abandon_before = now - timedelta(seconds=settings.proctor_abandon_seconds)

    async with WorkerSessionFactory() as db:
        # ---- silence becomes an event ------------------------------------
        # One gap event per silence episode: recorded only if nothing has been
        # heard since the last heartbeat AND no gap event already marks this
        # episode (i.e. none received after that heartbeat).
        silent = list(
            await db.scalars(
                select(ProctorSession).where(
                    ProctorSession.status == ProctorSessionStatus.ACTIVE,
                    ProctorSession.last_heartbeat_at.is_not(None),
                    ProctorSession.last_heartbeat_at < gap_before,
                    ~exists(
                        select(ProctorEvent.id).where(
                            ProctorEvent.proctor_session_id == ProctorSession.id,
                            ProctorEvent.type == EventType.HEARTBEAT_GAP,
                            ProctorEvent.received_at > ProctorSession.last_heartbeat_at,
                        )
                    ),
                )
            )
        )
        for session_row in silent:
            last_beat = session_row.last_heartbeat_at
            if last_beat is None:  # excluded by the query; mypy cannot see the SQL
                continue
            gap_ms = int((now - last_beat).total_seconds() * 1000)
            db.add(
                ProctorEvent(
                    proctor_session_id=session_row.id,
                    occurred_at=last_beat,
                    type=EventType.HEARTBEAT_GAP,
                    severity=SEVERITY_BY_TYPE[EventType.HEARTBEAT_GAP],
                    duration_ms=gap_ms,
                    event_metadata={"detected_by": "sweep"},
                )
            )
        if silent:
            await db.commit()
            logger.info("heartbeat gaps recorded", extra={"count": len(silent)})

        # ---- abandonment becomes a close ---------------------------------
        # Silence past the abandon threshold, or an attempt whose own deadline
        # has passed: observation is over either way. 'aborted' rather than
        # 'closed' so the timeline says how it ended — an observation, as ever.
        deadline_slack = now - timedelta(minutes=5)
        abandoned = list(
            await db.scalars(
                select(ProctorSession)
                .join(Attempt, Attempt.id == ProctorSession.attempt_id)
                .where(
                    ProctorSession.status == ProctorSessionStatus.ACTIVE,
                    or_(
                        ProctorSession.last_heartbeat_at < abandon_before,
                        and_(
                            Attempt.deadline_at.is_not(None),
                            Attempt.deadline_at < deadline_slack,
                        ),
                    ),
                )
            )
        )
        for session_row in abandoned:
            session_row.status = ProctorSessionStatus.ABORTED
            session_row.ended_at = now
            db.add(
                ProctorEvent(
                    proctor_session_id=session_row.id,
                    occurred_at=now,
                    type=EventType.SESSION_END,
                    severity=Severity.INFO,
                    event_metadata={"closed_by": "sweep"},
                )
            )
        if abandoned:
            await db.commit()

    # Enqueue after commit, like every task: aggregation must see the closed row.
    from app.workers.tasks.proctoring import aggregate_session

    for session_row in abandoned:
        aggregate_session.delay(str(session_row.id))
    if abandoned:
        logger.info("stale proctor sessions closed", extra={"count": len(abandoned)})
