"""queue: llm. Phase 6.

Five of the six grading families deterministically; subjective against the stored
rubric with grader='llm'. That result is a draft judgement the author can override,
not a final grade.

Tasks are thin wrappers over services: argument marshalling, retry policy, and writing
terminal state to the owning row. Business logic lives in services/.

Idempotent — re-grading recomputes every answer from scratch, and deliberately leaves
a human's mark alone. An override that a re-run silently reverted would be worthless,
and at-least-once delivery means re-runs happen.
"""

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from app.core.metrics import attempts_graded_total
from app.db.models import Answer, Assessment, Attempt, Question
from app.db.models.enums import AttemptStatus, ResultsRelease
from app.db.session import WorkerSessionFactory
from app.services import grading as service
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=2, acks_late=True, queue="llm")
def grade_attempt(self, attempt_id: str) -> None:
    asyncio.run(_grade(attempt_id))


async def _grade(attempt_id: str) -> None:
    async with WorkerSessionFactory() as session:
        attempt = await session.scalar(select(Attempt).where(Attempt.id == attempt_id))
        if attempt is None:
            logger.info("attempt gone before grading", extra={"id": attempt_id})
            return
        if attempt.status is AttemptStatus.VOIDED:
            # A voided sitting is not marked. Grading it would produce a number
            # somebody could later mistake for a result.
            return

        assessment = await session.scalar(
            select(Assessment).where(Assessment.id == attempt.assessment_id)
        )
        if assessment is None:
            return

        try:
            questions = {
                question.id: question
                for question in await session.scalars(
                    select(Question).where(Question.assessment_id == attempt.assessment_id)
                )
            }
            answers = {
                answer.question_id: answer
                for answer in await session.scalars(
                    select(Answer).where(Answer.attempt_id == attempt.id)
                )
            }

            # Every question is marked, including the ones nobody answered. An
            # unanswered question that simply has no row would leave the total
            # depending on which questions happened to be visited.
            for question_id, question in questions.items():
                answer = answers.get(question_id)
                if answer is None:
                    answer = Answer(attempt_id=attempt.id, question_id=question_id)
                    session.add(answer)
                    await session.flush()
                await service.grade_answer(session, question, answer)

            await session.flush()

            total = sum(
                (
                    a.awarded_points
                    for a in await service.answers_for(session, attempt.id)
                    if a.awarded_points is not None
                ),
                Decimal("0"),
            )
            attempt.score = service.quantize(total)
            attempt.max_score = assessment.max_score
            attempt.graded_at = datetime.now(UTC)
            attempt.grading_error = None

            # `immediate` releases here; `on_review` waits for the author to say so.
            # There is no timer and no threshold in the second case — release is a
            # human act, which is the same principle the proctoring gate rests on.
            if assessment.results_release is ResultsRelease.IMMEDIATE:
                attempt.results_released_at = datetime.now(UTC)

            await session.commit()
            attempts_graded_total.labels("ok").inc()
            logger.info(
                "attempt graded",
                extra={"id": attempt_id, "score": str(attempt.score)},
            )
        except Exception as exc:
            await session.rollback()
            attempts_graded_total.labels("failed").inc()
            try:
                await _fail(attempt_id, exc)
            except Exception:
                logger.exception("could not record grading failure")
            raise


async def _fail(attempt_id: str, exc: Exception) -> None:
    """Say so on the attempt itself. A paper stuck 'submitted' with no explanation is
    somebody refreshing a page for an hour."""
    logger.warning("grading failed", extra={"id": attempt_id, "error": str(exc)})
    async with WorkerSessionFactory() as session:
        attempt = await session.scalar(select(Attempt).where(Attempt.id == attempt_id))
        if attempt is None:
            return
        attempt.grading_error = (
            "Marking this paper failed. The author has been told and can mark it by hand."
        )
        await session.commit()
