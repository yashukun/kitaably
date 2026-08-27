"""attempts routes. Phase 6.

    GET   /exam/{share_token}                     require_auth  (the link is the grant)
    POST  /exam/{share_token}/start               require_auth              -> 201
    GET   /attempts                               require_auth  (papers I have sat)
    GET   /attempts/{attempt_id}                  require_attempt_sitter    (sitting view)
    PUT   /attempts/{attempt_id}/answers/{question_id}
                                                  require_attempt_sitter    (autosave)
    POST  /attempts/{attempt_id}/submit           require_attempt_sitter    (idempotent)
    GET   /attempts/{attempt_id}/result           require_attempt_participant
    POST  /attempts/{attempt_id}/release          require_attempt_author    (audit_log)
    PATCH /attempts/{attempt_id}/answers/{question_id}/grade
                                                  require_attempt_author    (audit_log)
    POST  /attempts/{attempt_id}/void             require_attempt_author    (audit_log)

**Why the entry route is `require_auth` and not `allow_anonymous`.** The share link is
the entire access grant — there is no roster, no invitation, and nobody has to be let
in. But a result has to belong to somebody in order to come back to the author, so the
token grants *access to attempt* and the session supplies *who*. A token holder starts
their own attempt and can never inherit anyone else's.

Every route declares a guard. A route with no guard is a review failure;
a genuinely public one says Depends(allow_anonymous) so the absence is
deliberate and greppable.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import (
    require_attempt_author,
    require_attempt_participant,
    require_attempt_sitter,
    require_auth,
)
from app.core.security import Principal
from app.db.session import get_session
from app.schemas.assessment import QuestionSitRead
from app.schemas.attempt import (
    AnswerRead,
    AnswerWrite,
    AttemptRead,
    AttemptResult,
    AttemptSummary,
    ExamPreview,
    GradeOverride,
)
from app.schemas.common import Page
from app.services import attempts as service

router = APIRouter(tags=["attempts"])


@router.get("/exam/{share_token}")
async def exam_preview(
    share_token: str,
    principal: Principal = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> ExamPreview:
    """What the link shows before you commit to sitting.

    Deliberately thin: the paper's name, its shape and whether it is open. No
    questions, no author identity, no results. Everything a share-link flow inherently
    has to disclose, and nothing beyond it.
    """
    row, already = await service.preview(session, principal, share_token)
    return ExamPreview(
        id=row.id,
        title=row.title,
        type=row.type,
        question_count=row.question_count,
        duration_minutes=row.duration_minutes,
        opens_at=row.opens_at,
        closes_at=row.closes_at,
        proctoring_enabled=row.proctoring_enabled,
        is_open=row.is_open,
        already_started=already,
    )


@router.post("/exam/{share_token}/start", status_code=status.HTTP_201_CREATED)
async def start_attempt(
    share_token: str,
    principal: Principal = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> AttemptRead:
    """Begin, or resume the sitting already in progress.

    Resuming rather than refusing matters: a closed laptop must not cost somebody
    their attempt. The deadline was fixed when they started, so resuming buys no time.
    """
    attempt = await service.start(session, principal, share_token)
    # Deliberately NOT committing here. `question_sit` is scoped by a SECURITY
    # DEFINER predicate over `auth.uid()`, and the RLS context is transaction-local:
    # committing first would start a new transaction with no identity and return a
    # paper with zero questions on it. The service has flushed, so the definer sees
    # the attempt within this transaction, and the session dependency commits at the
    # end. See `core.security.apply_rls_context`.
    return await _sitting(session, attempt)


async def _sitting(session: AsyncSession, attempt) -> AttemptRead:
    """Build the sitting payload.

    Questions come from `public.question_sit`, a view that does not contain
    `correct_option`, `model_answer` or `rubric` — so this function is not applying a
    projection it could get wrong. The columns are simply not there.
    """
    assessment, questions, answers = await service.sitting_view(session, attempt)
    return AttemptRead(
        id=attempt.id,
        assessment_id=attempt.assessment_id,
        title=assessment.title,
        status=attempt.status,
        started_at=attempt.started_at,
        deadline_at=attempt.deadline_at,
        proctoring_enabled=assessment.proctoring_enabled,
        questions=[QuestionSitRead.model_validate(question) for question in questions],
        answers=[AnswerRead.model_validate(answer) for answer in answers],
    )


@router.get("/attempts")
async def list_my_attempts(
    principal: Principal = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=100),
) -> Page[AttemptSummary]:
    """Papers this caller has sat. Marks appear only where released."""
    rows = await service.sitter_attempts(session, principal, limit=limit)
    return Page(
        items=[
            AttemptSummary(
                id=attempt.id,
                sitter_name=assessment.title,  # the paper's name is what a sitter needs
                sitter_email="",
                status=attempt.status,
                started_at=attempt.started_at,
                submitted_at=attempt.submitted_at,
                score=float(attempt.score)
                if attempt.results_released_at and attempt.score is not None
                else None,
                max_score=float(attempt.max_score)
                if attempt.results_released_at and attempt.max_score is not None
                else None,
                graded_at=attempt.graded_at,
                released=attempt.results_released_at is not None,
            )
            for attempt, assessment in rows
        ]
    )


@router.get("/attempts/{attempt_id}")
async def get_attempt(
    attempt_id: UUID,
    principal: Principal = Depends(require_attempt_sitter),
    session: AsyncSession = Depends(get_session),
) -> AttemptRead:
    """The paper as the person sitting it may see it."""
    attempt = await service.get_attempt(session, principal, attempt_id)
    return await _sitting(session, attempt)


@router.put("/attempts/{attempt_id}/answers/{question_id}")
async def save_answer(
    attempt_id: UUID,
    question_id: UUID,
    data: AnswerWrite,
    principal: Principal = Depends(require_attempt_sitter),
    session: AsyncSession = Depends(get_session),
) -> AnswerRead:
    """Autosave. Refused after the deadline — a client clock is a suggestion."""
    answer = await service.save_answer(
        session, principal, attempt_id, question_id, data.response
    )
    await session.commit()
    return AnswerRead.model_validate(answer)


@router.post("/attempts/{attempt_id}/submit")
async def submit_attempt(
    attempt_id: UUID,
    principal: Principal = Depends(require_attempt_sitter),
    session: AsyncSession = Depends(get_session),
) -> AttemptResult:
    """Hand the paper in and queue it for marking. Idempotent."""
    from app.services import proctoring
    from app.workers.tasks.grading import grade_attempt
    from app.workers.tasks.proctoring import aggregate_session

    attempt = await service.submit(session, principal, attempt_id)
    newly = attempt.graded_at is None

    # Submitting ends observation: close the proctor session (if this paper had
    # one) inside the same transaction, while it still carries the sitter's
    # identity. Idempotent, like submit itself.
    proctor_session_id = await proctoring.close_for_attempt(session, attempt_id)

    # Built before the commit, while this transaction still carries the caller's
    # identity — afterwards `auth.uid()` is NULL and the answer-key view is empty.
    payload = await service.result_view(session, principal, attempt)

    # Commit before enqueueing: a task that starts before its row is visible fails on
    # a row that does not exist yet, and at-least-once delivery will not politely wait.
    await session.commit()
    if newly:
        grade_attempt.delay(str(attempt.id))
    if proctor_session_id is not None:
        # Scoring is idempotent, so re-submits re-scoring an already-scored
        # session converge on the same row.
        aggregate_session.delay(str(proctor_session_id))
    return AttemptResult.model_validate(payload)


@router.get("/attempts/{attempt_id}/result")
async def get_result(
    attempt_id: UUID,
    principal: Principal = Depends(require_attempt_participant),
    session: AsyncSession = Depends(get_session),
) -> AttemptResult:
    """A marked paper, if and only if it has been released.

    `released` is explicit in the payload rather than implied by a score being present,
    so a UI cannot render a mark it was handed for some other reason.
    """
    attempt = await service.get_attempt(session, principal, attempt_id)
    return AttemptResult.model_validate(
        await service.result_view(session, principal, attempt)
    )


@router.post("/attempts/{attempt_id}/release")
async def release_result(
    attempt_id: UUID,
    principal: Principal = Depends(require_attempt_author),
    session: AsyncSession = Depends(get_session),
) -> AttemptResult:
    """Make a graded result visible to the person who sat it. Audited.

    There is no auto-release and no timer. The author decides when a mark is ready to
    be seen — the same principle the proctoring review gate rests on.
    """
    attempt = await service.release(session, principal, attempt_id)
    payload = await service.result_view(session, principal, attempt)
    await session.commit()
    return AttemptResult.model_validate(payload)


@router.patch("/attempts/{attempt_id}/answers/{question_id}/grade")
async def override_grade(
    attempt_id: UUID,
    question_id: UUID,
    data: GradeOverride,
    principal: Principal = Depends(require_attempt_author),
    session: AsyncSession = Depends(get_session),
) -> AttemptResult:
    """Correct a mark by hand. Preserves the model's original judgement. Audited."""
    await service.override_grade(
        session,
        principal,
        attempt_id,
        question_id,
        awarded_points=data.awarded_points,
        feedback=data.feedback,
    )
    attempt = await service.get_attempt(session, principal, attempt_id)
    payload = await service.result_view(session, principal, attempt)
    await session.commit()
    return AttemptResult.model_validate(payload)


@router.post("/attempts/{attempt_id}/void", status_code=status.HTTP_200_OK)
async def void_attempt(
    attempt_id: UUID,
    principal: Principal = Depends(require_attempt_author),
    session: AsyncSession = Depends(get_session),
) -> AttemptSummary:
    """Invalidate a sitting. A human act, never automatic, always audited."""
    attempt = await service.void(session, principal, attempt_id, reason="voided by author")
    await session.commit()
    return AttemptSummary(
        id=attempt.id,
        sitter_name=None,
        sitter_email="",
        status=attempt.status,
        started_at=attempt.started_at,
        submitted_at=attempt.submitted_at,
        score=None,
        max_score=None,
        graded_at=attempt.graded_at,
        released=attempt.results_released_at is not None,
    )
