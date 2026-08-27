"""queue: llm. Phase 5.

Tasks are thin wrappers over services: argument marshalling, retry policy, and writing
terminal state to the owning row. Business logic lives in services/.

Idempotent — prior questions are deleted before new ones are written, because
at-least-once delivery means this may run twice. Terminal failure writes a user-facing
reason onto the assessment, never just a log line: a spinner that never resolves is the
worst possible report of a known failure.

The worker connects as the service role and therefore BYPASSES RLS. Generation reads
its pool through `fetch_generation_chunks`, which carries the scope predicate —
canon plus the author's own uploads, never anyone else's personal book (D29) —
explicitly for exactly that reason.
"""

import asyncio
import logging

from sqlalchemy import select

from app.core.errors import DomainError
from app.db.models import Assessment
from app.db.models.enums import AssessmentStatus
from app.db.session import WorkerSessionFactory
from app.services import assessments as service
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=2, acks_late=True, queue="llm")
def generate_assessment(self, assessment_id: str) -> None:
    asyncio.run(_generate(assessment_id))


async def _generate(assessment_id: str) -> None:
    async with WorkerSessionFactory() as session:
        assessment = await session.scalar(
            select(Assessment).where(Assessment.id == assessment_id)
        )
        if assessment is None:
            # Deleted between enqueue and run. Nothing to do, and nothing wrong.
            logger.info("assessment gone before generation", extra={"id": assessment_id})
            return

        try:
            questions = await service.generate_questions(session, assessment)
            assessment.status = AssessmentStatus.DRAFT
            assessment.question_count = len(questions)
            assessment.error = None
            await session.commit()
            logger.info(
                "assessment generated",
                extra={"id": assessment_id, "questions": len(questions)},
            )
        except Exception as exc:
            await session.rollback()
            # The failure handler must never itself raise, or the row stays
            # `generating` for ever and the author is told nothing at all.
            try:
                await _fail(assessment_id, exc)
            except Exception:
                logger.exception("could not record generation failure")
            raise


async def _fail(assessment_id: str, exc: Exception) -> None:
    """Write a reason a person can act on, in its own transaction."""
    reason = (
        exc.message
        if isinstance(exc, DomainError)
        else "Writing this paper failed. Try again, or choose different chapters."
    )
    logger.warning("generation failed", extra={"id": assessment_id, "error": str(exc)})
    async with WorkerSessionFactory() as session:
        assessment = await session.scalar(
            select(Assessment).where(Assessment.id == assessment_id)
        )
        if assessment is None:
            return
        assessment.status = AssessmentStatus.DRAFT
        assessment.error = reason
        # The trace rides the exception out of the rolled-back session, because a
        # failed run is exactly the run whose trace somebody wants to read — "no
        # usable questions" with the per-call breakdown beside it is a diagnosis,
        # alone it is a shrug.
        trace = getattr(exc, "generation_trace", None)
        if trace is not None:
            assessment.generation_trace = trace
        await session.commit()
