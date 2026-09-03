"""queue: proctor. Phase 7.

aggregate_session recomputes the integrity score from RAW EVENTS, server-side. A
client-reported score is advisory input and is never stored as truth — nothing in
this codebase even accepts one.

Then review_status stays 'pending' — and nothing is visible to the person who sat
it. Release is the author's act alone (Phase 8), never this task's.

Bursty: an exam ending means every session in the room aggregates at once, which
is why this queue exists apart from ingest and llm (DECISIONS.md D6).

Idempotent: the score and the purge deadline are pure functions of the rows, so a
retry converges on the same values. Runs as the service role and bypasses RLS, so
every query carries its scope predicate explicitly.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.config import settings
from app.core.metrics import proctor_sessions_aggregated_total
from app.db.models import ProctorEvent, ProctorSession
from app.db.models.enums import ProctorSessionStatus
from app.db.session import WorkerSessionFactory
from app.services.proctoring import compute_integrity_score
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=3, acks_late=True, queue="proctor")
def aggregate_session(self, session_id: str) -> None:
    asyncio.run(_aggregate(session_id))


async def _aggregate(session_id: str) -> None:
    async with WorkerSessionFactory() as db:
        proctor_session = await db.scalar(
            select(ProctorSession).where(ProctorSession.id == session_id)
        )
        if proctor_session is None:
            logger.info("proctor session gone before aggregation", extra={"id": session_id})
            return
        if proctor_session.status is ProctorSessionStatus.ACTIVE:
            # Still being sat (a stray or early enqueue). Closing is the submit
            # route's or the sweep's act, not this task's — scoring a live session
            # would freeze a number that is still being earned.
            logger.info("proctor session still active, skipping", extra={"id": session_id})
            return

        events = list(
            await db.scalars(
                select(ProctorEvent)
                .where(ProctorEvent.proctor_session_id == proctor_session.id)
                .order_by(ProctorEvent.received_at)
            )
        )

        # The sitting's wall clock, so duration is scored as a SHARE of it rather
        # than in absolute seconds -- being absent for most of a short sitting and
        # briefly absent from a long one are not the same finding.
        ended_at = proctor_session.ended_at or datetime.now(UTC)
        sitting_seconds = max(
            0.0, (ended_at - proctor_session.started_at).total_seconds()
        )
        proctor_session.integrity_score = compute_integrity_score(
            events, sitting_seconds=sitting_seconds
        )
        # Retention runs from when observation ended, set here from server config —
        # deliberately not settable by anything a sitter can call.
        ended = proctor_session.ended_at or datetime.now(UTC)
        proctor_session.evidence_purge_after = ended + timedelta(
            days=settings.evidence_retention_days
        )

        await db.commit()
        proctor_sessions_aggregated_total.labels(proctor_session.status.value).inc()
        logger.info(
            "proctor session aggregated",
            extra={
                "id": session_id,
                "events": len(events),
                "score": proctor_session.integrity_score,
            },
        )
