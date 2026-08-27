"""Assessment and question schemas. Phase 5.

Two separate question Read models rather than one with optional fields. An optional
field is one careless serializer away from being populated, and the field in question
is the answer to an exam somebody is currently sitting.

The database enforces the same split independently: a sitter has no policy on
``questions`` at all and reads ``public.question_sit``, which does not contain the
answer columns. If this file were wrong, the query would still return nothing to leak.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.db.models.enums import (
    AssessmentRigor,
    AssessmentStatus,
    AssessmentType,
    Difficulty,
    QuestionFormat,
    QuestionOrigin,
    QuestionType,
    ResultsRelease,
)


class AssessmentExportFormat(StrEnum):
    """What a paper can be downloaded as.

    An API-only enum, not a Postgres type: nothing is stored, so adding a format is a
    code change rather than a migration.

    ``json`` is the data contract — every field the author's own detail endpoint
    already returns, versioned, so a future shape change is detectable by whatever was
    written against this one. ``md`` is the same paper as a document: the questions
    first, the answer key after, so the first half can be printed for a room.
    """

    JSON = "json"
    MD = "md"


class SourceSelection(BaseModel):
    """What the author chose to draw from.

    A claim, not an authorization: generation re-checks every id against what this
    caller may actually draw from — a shared book, or one they own themselves (D29).
    """

    book_ids: list[UUID] = Field(default_factory=list)
    chapter_ids: list[UUID] = Field(default_factory=list)


class AssessmentCreate(BaseModel):
    """What the author asked for.

    `formats` and `levels` are both **skippable**, and an empty list is a choice
    rather than a missing answer: it means *auto*, and generation picks a mix that
    suits the coarse `type` the author did choose. Somebody drafting a quiz from a
    novel should not have to know that `assertion_reason` exists before they can
    press the button.
    """

    title: str = Field(min_length=1, max_length=200)
    source: SourceSelection
    type: AssessmentType = AssessmentType.MIXED

    # Empty means auto. See app/rag/formats.py :: resolve_formats().
    formats: list[QuestionFormat] = Field(default_factory=list, max_length=14)
    # Empty means auto: recall, understand, apply.
    levels: list[Difficulty] = Field(default_factory=list, max_length=6)
    rigor: AssessmentRigor = AssessmentRigor.MEDIUM

    # The author's own brief, in their words. Steers emphasis and style; it is fenced
    # off in the prompt and can never override the grounding rules.
    instructions: str | None = Field(default=None, max_length=1000)

    question_count: int = Field(default=10, ge=1, le=100)
    duration_minutes: int | None = Field(default=None, ge=1, le=600)
    results_release: ResultsRelease = ResultsRelease.IMMEDIATE
    # Camera proctoring for everyone who sits this paper. Off by default: watching
    # people is a deliberate choice the author makes, never a side effect.
    proctoring_enabled: bool = False


class AssessmentUpdate(BaseModel):
    """Draft-time edits. Nothing here can change `status` — publishing is its own route,
    because it is the act that freezes a paper and mints a token."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    duration_minutes: int | None = Field(default=None, ge=1, le=600)
    results_release: ResultsRelease | None = None
    proctoring_enabled: bool | None = None
    opens_at: datetime | None = None
    closes_at: datetime | None = None


class OptionRead(BaseModel):
    key: str
    text: str


class QuestionSitRead(BaseModel):
    """What somebody sitting the paper sees. Note the absent fields.

    `format` is here because it decides how the question is drawn — a match grid and
    a flashcard are not radio buttons. `answer_key` is not, and never can be: the
    view this is built from does not contain the column.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    index: int
    type: QuestionType
    format: QuestionFormat
    stem: str
    options: list[OptionRead] | None
    # The left column of a match grid. Half the question, not the answer.
    prompt_items: list[OptionRead] | None = None
    points: float
    difficulty: Difficulty | None


class QuestionRead(QuestionSitRead):
    """The author's view: everything, including the answer key and provenance."""

    correct_option: str | None
    # {correct_options: [...]} | {accepted: [...], tolerance: n} |
    # {pairs: {...}} | {order: [...]} — whichever this format uses.
    answer_key: dict[str, Any] | None = None
    model_answer: str | None
    rubric: list[dict[str, Any]] | None
    source_chunk_ids: list[str]
    origin: QuestionOrigin


class QuestionWrite(BaseModel):
    """An author editing or writing a question by hand.

    `origin` is not a field: it is set by the service to `edited` or `written`
    depending on whether a row already existed. Letting a client claim `generated`
    would make "how much of this paper did the model actually earn" unanswerable.
    """

    format: QuestionFormat = QuestionFormat.MCQ
    # Derived from `format` by the service, never taken from the client: a question
    # drawn as one thing and marked as another scores zero for everybody who sat it.
    # Kept on the model for the shape checks below.
    stem: str = Field(min_length=10, max_length=2000)
    options: list[OptionRead] | None = None
    correct_option: str | None = None
    prompt_items: list[OptionRead] | None = None
    answer_key: dict[str, Any] | None = None
    model_answer: str | None = None
    rubric: list[dict[str, Any]] | None = None
    points: float = Field(default=1, gt=0, le=100)
    difficulty: Difficulty | None = None


class AssessmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    type: AssessmentType
    rigor: AssessmentRigor
    question_count: int
    duration_minutes: int | None
    status: AssessmentStatus
    results_release: ResultsRelease
    proctoring_enabled: bool = False
    opens_at: datetime | None
    closes_at: datetime | None
    max_score: float | None
    error: str | None
    # Set when generation succeeded but produced fewer questions than asked for. The UI
    # shows it as a notice, not a failure — the paper is real, it is just short.
    generation_note: str | None = None
    created_at: datetime
    updated_at: datetime

    # What was asked for, echoed back so the list can say "true/false, fill in the
    # blank" rather than making the author open the paper to find out. Derived from
    # `generation_spec`; empty means it was generated on auto.
    formats: list[QuestionFormat] = Field(default_factory=list)
    levels: list[Difficulty] = Field(default_factory=list)

    # Filled by the router for the author only. The token IS the access grant, so it
    # is a credential: it is never serialised for anybody else.
    share_url: str | None = None
    attempt_count: int = 0


class AssessmentDetail(AssessmentRead):
    """The author's review screen: the paper plus its answer key."""

    questions: list[QuestionRead] = Field(default_factory=list)

    # The generation pipeline trace, for the Advanced panel. On the detail payload
    # only, not the list: it is a diagnostic somebody opens, not a field every row
    # pays for. The detail route is author-guarded, which is the first line; the
    # trace being content-free is the second.
    trace: dict[str, Any] | None = None


class AssessmentAccepted(BaseModel):
    """202: generation is queued. Poll the resource for status."""

    id: UUID
    status: AssessmentStatus
