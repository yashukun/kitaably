"""assessments routes. Phase 5.

    POST   /assessments                                require_auth              -> 202
    GET    /assessments                                require_auth
    GET    /assessments/suggestions                    require_auth
    GET    /assessments/{assessment_id}                require_assessment_author
    PATCH  /assessments/{assessment_id}                require_assessment_author
    POST   /assessments/{assessment_id}/questions      require_assessment_author
    PUT    /assessments/{assessment_id}/questions/{question_id}
                                                       require_assessment_author
    DELETE /assessments/{assessment_id}/questions/{question_id}
                                                       require_assessment_author
    POST   /assessments/{assessment_id}/publish        require_assessment_author (audit)
    POST   /assessments/{assessment_id}/close          require_assessment_author (audit)
    GET    /assessments/{assessment_id}/attempts       require_assessment_author

Generation produces a draft. A human publishes.

Every route declares a guard. A route with no guard is a review failure;
a genuinely public one says Depends(allow_anonymous) so the absence is
deliberate and greppable.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import ratelimit
from app.core.config import settings
from app.core.deps import require_assessment_author, require_auth
from app.core.errors import RateLimited
from app.core.metrics import assessments_rate_limited_total
from app.core.security import Principal
from app.db.models import Assessment
from app.db.models.enums import Difficulty
from app.db.session import get_session
from app.rag import formats
from app.schemas.assessment import (
    AssessmentAccepted,
    AssessmentCreate,
    AssessmentDetail,
    AssessmentExportFormat,
    AssessmentRead,
    AssessmentSuggestions,
    AssessmentUpdate,
    QuestionRead,
    QuestionWrite,
)
from app.schemas.attempt import AttemptSummary
from app.schemas.common import Page
from app.services import assessments as service
from app.services import attempts as attempt_service
from app.services import suggestions

router = APIRouter(tags=["assessments"])


def _to_read(
    assessment: Assessment, principal: Principal, *, attempts: int = 0
) -> AssessmentRead:
    """The one place an Assessment becomes a response.

    The share URL is built here and only for the author. The token IS the access grant,
    so it is a credential: serialising it for anybody else would hand out the paper.
    """
    read = AssessmentRead.model_validate(assessment)
    share_url = None
    if assessment.share_token and assessment.author_id == principal.id:
        share_url = f"{settings.frontend_url}/exam/{assessment.share_token}"

    # Echo back what was asked for, so a list of papers can say "true/false, fill in
    # the blank" without the author opening each one. Unknown values are dropped
    # rather than raising: this row may predate a format that was later retired.
    spec = assessment.generation_spec or {}
    chosen = [
        found.format
        for found in (formats.spec_for(value) for value in spec.get("formats", []))
        if found is not None
    ]
    levels = []
    for value in spec.get("levels", []):
        try:
            levels.append(Difficulty(value))
        except ValueError:
            continue

    return read.model_copy(
        update={
            "share_url": share_url,
            "attempt_count": attempts,
            "formats": chosen,
            "levels": levels,
        }
    )


@router.post("/assessments", status_code=status.HTTP_202_ACCEPTED)
async def create_assessment(
    data: AssessmentCreate,
    principal: Principal = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> AssessmentAccepted:
    """Queue a paper for writing. Returns immediately; poll the resource for status.

    Rate limited before anything is written: generation is the most expensive thing in
    the product, and refusing it cheaply is the point of checking first.
    """
    from app.workers.tasks.assessments import generate_assessment

    try:
        await ratelimit.check(
            f"generate:{principal.id}",
            limit=settings.assessment_rate_limit_per_hour,
            window_seconds=3600,
        )
    except RateLimited:
        assessments_rate_limited_total.inc()
        raise

    assessment = await service.create_draft(session, principal, data)
    # Commit before enqueueing: a task that starts before its row is visible fails on
    # a row that does not exist yet, and at-least-once delivery will not politely wait.
    await session.commit()
    generate_assessment.delay(str(assessment.id))
    return AssessmentAccepted(id=assessment.id, status=assessment.status)


@router.get("/assessments")
async def list_assessments(
    principal: Principal = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=100),
) -> Page[AssessmentRead]:
    """Papers this caller wrote. Papers they have *sat* are under /attempts."""
    rows = await service.list_authored(session, principal, limit=limit)
    counts = await service.attempt_counts(session, [row.id for row in rows])
    return Page(
        items=[_to_read(row, principal, attempts=counts.get(row.id, 0)) for row in rows]
    )


# Declared BEFORE /assessments/{assessment_id}. FastAPI matches in declaration order,
# so the literal path has to win before the parameterised one tries to read
# "suggestions" as a UUID and 422s on it.
@router.get("/assessments/suggestions")
async def assessment_suggestions(
    book_ids: list[UUID] = Query(default_factory=list),
    principal: Principal = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> AssessmentSuggestions:
    """Titles and focus topics for a paper drawn from these books.

    `require_auth` rather than an author guard, deliberately: there is no assessment
    yet to be the author of. The books are authorised instead, by the same
    `draft_source_clause` `create_draft` uses — so this cannot suggest a chapter from a
    book the caller could not actually examine on.
    """
    return AssessmentSuggestions.model_validate(
        await suggestions.for_assessment(
            session, principal, book_ids, service.draft_source_clause(principal)
        )
    )


@router.get("/assessments/{assessment_id}")
async def get_assessment(
    assessment_id: UUID,
    principal: Principal = Depends(require_assessment_author),
    session: AsyncSession = Depends(get_session),
) -> AssessmentDetail:
    """The author's review screen: the paper with its answer key and provenance.

    Guarded to the author rather than to anyone who can see the row, because this is
    the payload that contains `correct_option`.
    """
    assessment = await service.get_assessment(session, principal, assessment_id)
    questions = await service.list_questions(session, assessment_id)
    counts = await service.attempt_counts(session, [assessment_id])

    base = _to_read(assessment, principal, attempts=counts.get(assessment_id, 0))
    return AssessmentDetail(
        **base.model_dump(),
        questions=[QuestionRead.model_validate(question) for question in questions],
        trace=assessment.generation_trace,
    )


@router.patch("/assessments/{assessment_id}")
async def update_assessment(
    assessment_id: UUID,
    data: AssessmentUpdate,
    principal: Principal = Depends(require_assessment_author),
    session: AsyncSession = Depends(get_session),
) -> AssessmentRead:
    assessment = await service.update_draft(session, principal, assessment_id, data)
    await session.commit()
    return _to_read(assessment, principal)


@router.post("/assessments/{assessment_id}/questions", status_code=status.HTTP_201_CREATED)
async def add_question(
    assessment_id: UUID,
    data: QuestionWrite,
    principal: Principal = Depends(require_assessment_author),
    session: AsyncSession = Depends(get_session),
) -> QuestionRead:
    """Write a question by hand. Lands as origin='written'."""
    question = await service.write_question(session, principal, assessment_id, None, data)
    await session.commit()
    return QuestionRead.model_validate(question)


@router.put("/assessments/{assessment_id}/questions/{question_id}")
async def edit_question(
    assessment_id: UUID,
    question_id: UUID,
    data: QuestionWrite,
    principal: Principal = Depends(require_assessment_author),
    session: AsyncSession = Depends(get_session),
) -> QuestionRead:
    """Edit one question. Lands as origin='edited'."""
    question = await service.write_question(
        session, principal, assessment_id, question_id, data
    )
    await session.commit()
    return QuestionRead.model_validate(question)


@router.delete(
    "/assessments/{assessment_id}/questions/{question_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_question(
    assessment_id: UUID,
    question_id: UUID,
    principal: Principal = Depends(require_assessment_author),
    session: AsyncSession = Depends(get_session),
) -> Response:
    await service.delete_question(session, principal, assessment_id, question_id)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/assessments/{assessment_id}/publish")
async def publish_assessment(
    assessment_id: UUID,
    principal: Principal = Depends(require_assessment_author),
    session: AsyncSession = Depends(get_session),
) -> AssessmentRead:
    """Freeze the paper and mint its share link. The response carries the URL."""
    assessment = await service.publish(session, principal, assessment_id)
    await session.commit()
    return _to_read(assessment, principal)


@router.post("/assessments/{assessment_id}/close")
async def close_assessment(
    assessment_id: UUID,
    principal: Principal = Depends(require_assessment_author),
    session: AsyncSession = Depends(get_session),
) -> AssessmentRead:
    """Stop accepting new sittings. Attempts in progress keep their deadline."""
    assessment = await service.close(session, principal, assessment_id)
    await session.commit()
    return _to_read(assessment, principal)


@router.get("/assessments/{assessment_id}/export")
async def export_assessment(
    assessment_id: UUID,
    format: AssessmentExportFormat = AssessmentExportFormat.JSON,
    principal: Principal = Depends(require_assessment_author),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """The whole paper as a downloadable file: questions, answers, rubrics, provenance.

    Guarded to the author rather than to anyone who can see the row, because this is
    the payload that contains the answer key — the same reason `GET /assessments/{id}`
    is. No audit row: an author reading that endpoint is already handed every field in
    here, so this is a reformatting of a disclosure that has already happened rather
    than a new one, and auditing the download while leaving the screen unaudited would
    be theatre.

    The share token is deliberately not in the body. It is the access grant, so it is a
    credential, and a credential inside a file that gets forwarded is how a paper leaks.
    """
    filename, media_type, body = await service.export_assessment(
        session, principal, assessment_id, format.value
    )
    return Response(
        content=body,
        media_type=media_type,
        # `attachment` so the browser saves rather than navigates; the filename is
        # ASCII by construction (see _export_filename).
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/assessments/{assessment_id}/attempts")
async def list_attempts(
    assessment_id: UUID,
    principal: Principal = Depends(require_assessment_author),
    session: AsyncSession = Depends(get_session),
) -> Page[AttemptSummary]:
    """The gradebook: every sitting of this paper, with who sat it."""
    rows = await attempt_service.gradebook(session, assessment_id)
    return Page(
        items=[
            AttemptSummary(
                id=row.id,
                sitter_name=row.sitter_name,
                sitter_email=str(row.sitter_email),
                status=row.status,
                started_at=row.started_at,
                submitted_at=row.submitted_at,
                score=float(row.score) if row.score is not None else None,
                max_score=float(row.max_score) if row.max_score is not None else None,
                graded_at=row.graded_at,
                released=row.results_released_at is not None,
                grading_error=row.grading_error,
            )
            for row in rows
        ]
    )
