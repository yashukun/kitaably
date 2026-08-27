"""Sitting an assessment. Phase 6.

The share link is the whole access grant. There is no roster and no invitation
(DECISIONS.md D16), so possession of the URL is what admits somebody — and what the
token deliberately does NOT do is establish identity. It resolves to one assessment
id through a SECURITY DEFINER function, and the sitter is always the authenticated
caller. A token holder can start their own attempt; they can never inherit anybody
else's, and they can never see anybody else's marks.

The deadline is server-authoritative. A client clock is a suggestion.
"""

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound, ValidationFailed
from app.core.security import Principal
from app.db.models import Answer, Assessment, Attempt, QuestionKey, QuestionSit
from app.db.models.enums import AttemptStatus, Grader
from app.services import audit

logger = logging.getLogger(__name__)

_PREVIEW = text(
    """
    select id, title, type, question_count, duration_minutes,
           opens_at, closes_at, proctoring_enabled, is_open
    from public.assessment_by_share_token(cast(:token as text))
    """
)


async def preview(session: AsyncSession, principal: Principal, share_token: str) -> Any:
    """What a share link shows before somebody commits to sitting.

    Resolved through the SECURITY DEFINER function rather than a SELECT, because the
    caller cannot see this row yet — that is the whole point of the link. The function
    discloses what a share-link flow inherently must (the link is live, and what the
    paper is called) and nothing else.
    """
    row = (await session.execute(_PREVIEW, {"token": share_token.strip()})).first()
    if row is None:
        raise NotFound("That link is not valid, or the paper is no longer shared.")

    existing = await session.scalar(
        select(Attempt.id).where(
            Attempt.assessment_id == row.id, Attempt.sitter_id == principal.id
        )
    )
    return row, existing is not None


async def start(session: AsyncSession, principal: Principal, share_token: str) -> Attempt:
    """Begin a sitting, or resume the one already in progress.

    Resuming rather than refusing matters: a closed laptop mid-exam must not cost
    somebody their attempt, and the deadline was fixed when they started so resuming
    grants no extra time.
    """
    row, _ = await preview(session, principal, share_token)
    if not row.is_open:
        raise Conflict("That paper is not open right now.")

    existing = await session.scalar(
        select(Attempt).where(
            Attempt.assessment_id == row.id, Attempt.sitter_id == principal.id
        )
    )
    if existing is not None:
        if existing.status is AttemptStatus.VOIDED:
            raise Conflict("This attempt was voided. Ask the author about it.")
        return existing

    # The deadline is computed here and never again. Extending it mid-sitting would be
    # a different exam; recomputing it on read would let a slow page load buy time.
    started = datetime.now(UTC)
    deadline = None
    if row.duration_minutes:
        deadline = started + timedelta(minutes=row.duration_minutes)
    if row.closes_at:
        # A paper that closes before the clock runs out closes anyway.
        deadline = min(deadline, row.closes_at) if deadline else row.closes_at

    attempt = Attempt(
        assessment_id=row.id,
        sitter_id=principal.id,
        started_at=started,
        deadline_at=deadline,
    )
    session.add(attempt)
    try:
        await session.flush()
    except IntegrityError as exc:
        # Two tabs, one person, same instant. The unique constraint is the arbiter.
        raise Conflict("You have already started this paper.") from exc

    await session.refresh(attempt)
    return attempt


async def get_attempt(
    session: AsyncSession, principal: Principal, attempt_id: UUID
) -> Attempt:
    attempt = await session.scalar(select(Attempt).where(Attempt.id == attempt_id))
    if attempt is None:
        raise NotFound("That attempt does not exist.")
    return attempt


async def sitting_view(
    session: AsyncSession, attempt: Attempt
) -> tuple[Assessment, list[QuestionSit], list[Answer]]:
    """The paper as the person sitting it may see it.

    Questions come from ``public.question_sit``, which does not contain
    ``correct_option``, ``model_answer`` or ``rubric``. That is not a projection this
    function applies — those columns are absent from the view itself, and a sitter has
    no policy on the underlying table at all.
    """
    assessment = await session.scalar(
        select(Assessment).where(Assessment.id == attempt.assessment_id)
    )
    if assessment is None:
        raise NotFound("That assessment does not exist.")

    questions = list(
        await session.scalars(
            select(QuestionSit)
            .where(QuestionSit.assessment_id == attempt.assessment_id)
            .order_by(QuestionSit.index)
        )
    )
    answers = list(
        await session.scalars(select(Answer).where(Answer.attempt_id == attempt.id))
    )
    return assessment, questions, answers


def _expired(attempt: Attempt) -> bool:
    return attempt.deadline_at is not None and datetime.now(UTC) >= attempt.deadline_at


async def save_answer(
    session: AsyncSession,
    principal: Principal,
    attempt_id: UUID,
    question_id: UUID,
    response: str | None,
) -> Answer:
    """Autosave one answer.

    Refused after the deadline. The client also counts down, but a client clock is a
    suggestion and this is the fact — otherwise the answer typed at 90:01 is worth the
    same as the one typed at 89:59.
    """
    attempt = await get_attempt(session, principal, attempt_id)
    if attempt.sitter_id != principal.id:
        # The author can read an attempt but never write an answer into it.
        raise NotFound("That attempt does not exist.")
    if attempt.status is not AttemptStatus.IN_PROGRESS:
        raise Conflict("This paper has already been submitted.")
    if _expired(attempt):
        raise Conflict("Time is up for this paper.")

    # The question must belong to this paper. It arrives in a URL, so it is a claim.
    belongs = await session.scalar(
        select(QuestionSit.id).where(
            QuestionSit.id == question_id,
            QuestionSit.assessment_id == attempt.assessment_id,
        )
    )
    if belongs is None:
        raise NotFound("That question is not on this paper.")

    answer = await session.scalar(
        select(Answer).where(
            Answer.attempt_id == attempt_id, Answer.question_id == question_id
        )
    )
    if answer is None:
        answer = Answer(attempt_id=attempt_id, question_id=question_id)
        session.add(answer)

    answer.response = response
    await session.flush()
    return answer


async def submit(
    session: AsyncSession, principal: Principal, attempt_id: UUID, *, automatic: bool = False
) -> Attempt:
    """Hand the paper in. Idempotent.

    Re-submitting returns the attempt unchanged rather than erroring: a double-clicked
    button, a retried request and a browser that fired both `beforeunload` and the
    button must all land in the same place.
    """
    attempt = await get_attempt(session, principal, attempt_id)
    if attempt.sitter_id != principal.id:
        raise NotFound("That attempt does not exist.")

    if attempt.status is not AttemptStatus.IN_PROGRESS:
        return attempt

    attempt.status = (
        AttemptStatus.AUTO_SUBMITTED if automatic or _expired(attempt) else AttemptStatus.SUBMITTED
    )
    attempt.submitted_at = datetime.now(UTC)
    await session.flush()
    return attempt


# ============================================================ results


async def result_view(
    session: AsyncSession, principal: Principal, attempt: Attempt
) -> dict[str, Any]:
    """A marked paper, if and only if it has been released.

    ``released`` is returned explicitly rather than implied by the presence of a score,
    so a UI cannot render a mark it was handed for some other reason. When it is false
    the marks are not merely hidden by the caller — they are not in the payload, and
    the answer key is not readable by this caller under RLS either.
    """
    assessment = await session.scalar(
        select(Assessment).where(Assessment.id == attempt.assessment_id)
    )
    if assessment is None:
        raise NotFound("That assessment does not exist.")

    released = attempt.results_released_at is not None
    payload: dict[str, Any] = {
        "id": attempt.id,
        "assessment_id": attempt.assessment_id,
        "title": assessment.title,
        "status": attempt.status,
        "submitted_at": attempt.submitted_at,
        "graded_at": attempt.graded_at,
        "released": released,
        "score": float(attempt.score) if released and attempt.score is not None else None,
        "max_score": (
            float(attempt.max_score)
            if released and attempt.max_score is not None
            else None
        ),
        "grading_error": attempt.grading_error if released else None,
        "answers": [],
    }
    if not released:
        return payload

    answers = {
        answer.question_id: answer
        for answer in await session.scalars(
            select(Answer).where(Answer.attempt_id == attempt.id)
        )
    }
    # Walk `question_key`, not `question_sit`. Both audiences for a marked paper can
    # read the key — the author always, this sitter because their result is released —
    # whereas `question_sit` is scoped to people who *have* an attempt and is therefore
    # correctly empty for the author. Using it here returned a score with no breakdown.
    questions = await session.scalars(
        select(QuestionKey)
        .where(QuestionKey.assessment_id == attempt.assessment_id)
        .order_by(QuestionKey.index)
    )

    for key in questions:
        answer = answers.get(key.id)
        payload["answers"].append(
            {
                "question_id": key.id,
                "stem": key.stem,
                # The format and the options come with the mark, because this is what
                # a result screen draws. Without them it could say the answer was "B"
                # and not what B said — a marked paper nobody learns anything from.
                "format": key.format,
                "options": key.options,
                "prompt_items": key.prompt_items,
                "answer_key": key.answer_key,
                "response": answer.response if answer else None,
                "awarded_points": float(answer.awarded_points)
                if answer and answer.awarded_points is not None
                else None,
                "points": float(key.points),
                "grader": answer.grader if answer else None,
                "feedback": answer.feedback if answer else None,
                "correct_option": key.correct_option,
                "model_answer": key.model_answer,
            }
        )
    return payload


async def gradebook(session: AsyncSession, assessment_id: UUID) -> list[Any]:
    """Every sitting of one paper, for its author.

    Reads names through a join to ``profiles``, which under RLS returns only the
    caller's own row — so the join yields nulls for everybody else, quietly. That is
    the "silent empties" failure CLAUDE.md warns about, and it is why the name and
    email here come from the definer view rather than the table.
    """
    rows = await session.execute(
        text(
            """
            select t.id, t.status, t.started_at, t.submitted_at, t.score, t.max_score,
                   t.graded_at, t.results_released_at, t.grading_error,
                   s.name as sitter_name, s.email as sitter_email
            from public.attempts t
            join public.attempt_sitter s on s.attempt_id = t.id
            where t.assessment_id = cast(:assessment_id as uuid)
            order by t.started_at desc
            """
        ),
        {"assessment_id": str(assessment_id)},
    )
    return list(rows.all())


async def release(
    session: AsyncSession, principal: Principal, attempt_id: UUID
) -> Attempt:
    """Make a graded result visible to the person who sat it.

    A deliberate human act, and audited. There is no auto-release and no timer: the
    author decides when a mark is ready to be seen, which is the same principle the
    proctoring review gate rests on.
    """
    attempt = await get_attempt(session, principal, attempt_id)
    if attempt.graded_at is None:
        raise Conflict("This paper has not been graded yet.")
    if attempt.results_released_at is not None:
        return attempt

    attempt.results_released_at = datetime.now(UTC)
    await session.flush()
    await audit.record(
        session,
        action="attempt.released",
        target_type="attempt",
        target_id=attempt.id,
        metadata={"score": str(attempt.score), "max_score": str(attempt.max_score)},
    )
    return attempt


async def override_grade(
    session: AsyncSession,
    principal: Principal,
    attempt_id: UUID,
    question_id: UUID,
    *,
    awarded_points: float,
    feedback: str | None,
) -> Answer:
    """The author correcting a mark by hand.

    Sets ``grader='human'`` and **preserves** ``llm_rationale``. Keeping the original
    machine judgement is what makes an override a correction rather than a cover-up —
    if the mark is later disputed, both what the model said and what the human decided
    are on the record.

    ``attempts.score`` is recomputed from the answers afterwards. Never store a score
    that cannot be re-derived from its parts.
    """
    attempt = await get_attempt(session, principal, attempt_id)

    key = await session.scalar(
        select(QuestionKey).where(
            QuestionKey.id == question_id,
            QuestionKey.assessment_id == attempt.assessment_id,
        )
    )
    if key is None:
        raise NotFound("That question is not on this paper.")

    points = Decimal(str(awarded_points))
    if points < 0 or points > key.points:
        raise ValidationFailed(f"A mark for this question must be between 0 and {key.points:g}.")

    answer = await session.scalar(
        select(Answer).where(
            Answer.attempt_id == attempt_id, Answer.question_id == question_id
        )
    )
    if answer is None:
        answer = Answer(attempt_id=attempt_id, question_id=question_id)
        session.add(answer)

    previous = answer.awarded_points
    answer.awarded_points = points
    answer.grader = Grader.HUMAN
    if feedback is not None:
        answer.feedback = feedback
    await session.flush()

    await recompute_score(session, attempt)

    await audit.record(
        session,
        action="grade.overridden",
        target_type="answer",
        target_id=answer.id,
        metadata={
            "attempt_id": str(attempt_id),
            "question_id": str(question_id),
            "from": str(previous) if previous is not None else None,
            "to": str(points),
        },
    )
    return answer


async def recompute_score(session: AsyncSession, attempt: Attempt) -> None:
    """Re-derive ``attempts.score`` from its answers. The only place it is written."""
    awarded = await session.scalars(
        select(Answer.awarded_points).where(Answer.attempt_id == attempt.id)
    )
    attempt.score = sum((value for value in awarded if value is not None), Decimal("0"))
    await session.flush()


async def void(
    session: AsyncSession, principal: Principal, attempt_id: UUID, reason: str
) -> Attempt:
    """Invalidate a sitting. A human act, never automatic, always audited."""
    attempt = await get_attempt(session, principal, attempt_id)
    if attempt.status is AttemptStatus.VOIDED:
        return attempt

    attempt.status = AttemptStatus.VOIDED
    await session.flush()
    await audit.record(
        session,
        action="attempt.voided",
        target_type="attempt",
        target_id=attempt.id,
        metadata={"reason": reason},
    )
    return attempt


async def sitter_attempts(
    session: AsyncSession, principal: Principal, *, limit: int = 50
) -> list[tuple[Attempt, Assessment]]:
    """Papers this caller has sat. RLS scopes it to their own without a WHERE here."""
    rows = await session.execute(
        select(Attempt, Assessment)
        .join(Assessment, Assessment.id == Attempt.assessment_id)
        .where(Attempt.sitter_id == principal.id)
        .order_by(Attempt.started_at.desc())
        .limit(limit)
    )
    return [(attempt, assessment) for attempt, assessment in rows.all()]
