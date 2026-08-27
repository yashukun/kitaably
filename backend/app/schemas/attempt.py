"""Attempt and answer schemas. Phase 6."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.db.models.enums import (
    AssessmentType,
    AttemptStatus,
    Grader,
    QuestionFormat,
)
from app.schemas.assessment import OptionRead, QuestionSitRead


class ExamPreview(BaseModel):
    """What a share link shows before you commit to sitting.

    Deliberately thin. It is served to anyone holding the URL, so it carries what a
    share-link flow inherently must — that the link is live, and what the paper is
    called — and nothing else. No questions, no author identity, no results.
    """

    id: UUID
    title: str
    type: AssessmentType
    question_count: int
    duration_minutes: int | None
    opens_at: datetime | None
    closes_at: datetime | None
    proctoring_enabled: bool
    is_open: bool
    # Whether this caller already has an attempt, so the UI can say "resume" rather
    # than "start" and not imply a second sitting is available.
    already_started: bool = False


class AnswerWrite(BaseModel):
    """Autosave.

    One field, whatever the format: the option key for mcq, prose for subjective, and
    compact JSON for the structured families — ``["A","C"]``, ``{"1":"B"}``,
    ``["C","A","B"]``. Two columns would raise the question of which one holds the
    answer, and the grader would have to guess.
    """

    response: str | None = Field(default=None, max_length=20000)


class AnswerRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    question_id: UUID
    response: str | None


class AnswerResult(BaseModel):
    """A marked answer. Only ever served once results are released.

    Carries the options and the format as well as the key, because this is what a
    result screen draws. Without them it could say the answer was "B" and not what B
    said — a marked paper nobody learns anything from.
    """

    question_id: UUID
    stem: str
    format: QuestionFormat = QuestionFormat.MCQ
    options: list[OptionRead] | None = None
    prompt_items: list[OptionRead] | None = None
    response: str | None
    awarded_points: float | None
    points: float
    grader: Grader | None
    feedback: str | None
    correct_option: str | None = None
    answer_key: dict[str, Any] | None = None
    model_answer: str | None = None


class AttemptRead(BaseModel):
    """The sitting view: the paper, the answers so far, and the clock.

    `score` and the answer key are absent here by construction — a sitting attempt has
    neither. Marks arrive through :class:`AttemptResult`, and only after release.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    assessment_id: UUID
    title: str
    status: AttemptStatus
    started_at: datetime
    deadline_at: datetime | None
    # Tells the runner to open a proctor session and start the camera flow. The
    # flag alone: no scores, no events, nothing evaluative rides along with it.
    proctoring_enabled: bool = False
    questions: list[QuestionSitRead] = Field(default_factory=list)
    answers: list[AnswerRead] = Field(default_factory=list)


class AttemptResult(BaseModel):
    """A released result.

    `released` is explicit rather than implied by the presence of a score, so a UI
    cannot accidentally render a mark it was handed for another reason.
    """

    id: UUID
    assessment_id: UUID
    title: str
    status: AttemptStatus
    submitted_at: datetime | None
    graded_at: datetime | None
    released: bool
    score: float | None
    max_score: float | None
    grading_error: str | None = None
    answers: list[AnswerResult] = Field(default_factory=list)


class AttemptSummary(BaseModel):
    """One row of the author's gradebook."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    sitter_name: str | None
    sitter_email: str
    status: AttemptStatus
    started_at: datetime
    submitted_at: datetime | None
    score: float | None
    max_score: float | None
    graded_at: datetime | None
    released: bool
    grading_error: str | None = None


class GradeOverride(BaseModel):
    """An author correcting a mark by hand.

    `llm_rationale` is not a field, and must never become one: an override preserves
    the original machine judgement rather than replacing it.
    """

    awarded_points: float = Field(ge=0)
    feedback: str | None = Field(default=None, max_length=4000)


class GeneratedQuestion(BaseModel):
    """The strict contract a model must return. Validated before anything is stored.

    Lives here rather than in the prompt so that "what shape did we ask for" and "what
    shape do we accept" cannot drift apart.

    Every answer field is optional at this layer and required at the next. That is
    deliberate: a fill-in-the-blank has no `pairs` and a match grid has no
    `correct_option`, so a model that returns the wrong one for its format must be
    **rejected with a reason** by ``validate_generated`` rather than dying in a
    Pydantic error that takes the rest of the batch with it.

    ``format`` is what the prompt asks for. ``type`` is accepted as a fallback because
    small local models keep emitting the field the old prompt used, and throwing away
    an otherwise good question over the name of a key is a worse paper for no gain.
    """

    format: str | None = None
    type: str | None = None
    stem: str
    options: list[dict[str, str]] | None = None
    correct_option: str | None = None

    # multi_select
    correct_options: list[str] | None = None
    # match — the left-hand column, and the pairing
    prompt_items: list[dict[str, str]] | None = None
    pairs: dict[str, str] | None = None
    # sequence
    order: list[str] | None = None
    # short_text / numeric
    accepted: list[str] | None = None
    tolerance: float | None = None
    # subjective
    model_answer: str | None = None
    rubric: list[dict[str, Any]] | None = None

    difficulty: str | None = None
    source_chunk_id: str | None = None
    rationale: str | None = None

    @property
    def declared_format(self) -> str:
        """What the model says this is, preferring `format` and falling back to `type`."""
        return (self.format or self.type or "").strip().lower()
