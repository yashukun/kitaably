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

    There is no `formats` field and no `type`: every paper is multiple choice (D32), and
    the server does not take a request for anything else. `levels` remains **skippable**,
    and an empty list is a choice rather than a missing answer -- it means *auto*, and
    generation spreads the paper over recall, understand and apply.
    """

    title: str = Field(min_length=1, max_length=200)
    source: SourceSelection

    # Empty means auto: recall, understand, apply.
    levels: list[Difficulty] = Field(default_factory=list, max_length=6)
    rigor: AssessmentRigor = AssessmentRigor.MEDIUM

    # The author's own brief, in their words. Steers emphasis and style; it is fenced
    # off in the prompt and can never override the grounding rules.
    instructions: str | None = Field(default=None, max_length=1000)

    question_count: int = Field(default=10, ge=1, le=100)
    duration_minutes: int | None = Field(default=None, ge=1, le=600)
    # ON_REVIEW, not IMMEDIATE. A mark reaches the person who sat the paper when its
    # author says so, and the author is the one who holds authority over results -- so
    # the default is the one that asks. The old default meant a paper created without
    # touching this field released itself the moment grading finished, which made the
    # review gate opt-in for a product whose whole shape assumes it.
    #
    # Still a choice, not a rule: a five-question practice quiz where the sitter should
    # see their score immediately is a real case, and the create form offers it.
    results_release: ResultsRelease = ResultsRelease.ON_REVIEW
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

    `format` is here because it decides how the question is drawn. It is one value now
    (D32), and it stays on the wire anyway: the renderer switching on it is what makes
    a second format a frontend change rather than a protocol change. `answer_key` is
    absent, and never can be present -- the view this is built from has no such column.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    index: int
    type: QuestionType
    format: QuestionFormat
    stem: str
    options: list[OptionRead] | None
    # Always null for an mcq. Kept because the view still selects the column and rows
    # written before D32 still hold values in it.
    prompt_items: list[OptionRead] | None = None
    points: float
    difficulty: Difficulty | None


class QuestionRead(QuestionSitRead):
    """The author's view: everything, including the answer key and provenance."""

    correct_option: str | None
    # Unused since D32 -- an mcq's answer is `correct_option`. Still read back, because
    # a question written before the collapse still has one and the author's screen
    # should not silently drop what is stored.
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

    # One value, and it stays on the wire so a client that sends `"format": "mcq"`
    # still validates. `type` is derived from it by the service and never taken from
    # the client: a question drawn as one thing and marked as another scores zero for
    # everybody who sat it.
    format: QuestionFormat = QuestionFormat.MCQ
    stem: str = Field(min_length=10, max_length=2000)
    options: list[OptionRead] | None = None
    correct_option: str | None = None
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

    # What was asked for, echoed back from `generation_spec`. `formats` is always
    # ["mcq"] on a paper drafted since D32; it is still sent because an older row can
    # name something else, and the list should say what the paper actually is.
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


class AssessmentSuggestions(BaseModel):
    """What to put under the title and focus boxes once books are picked.

    Both lists are advisory and both may be empty — a book with no detected outline
    has nothing honest to suggest, and an empty strip is the right answer there. The
    author types over any of it; nothing here is stored or implied by being shown.
    """

    titles: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
