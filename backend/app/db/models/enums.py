"""Enums, mirroring the Postgres enum types in ``supabase/migrations/``.

Values are the contract between Python, Postgres, and the UI. Adding a value means a
migration — the Python side alone is not the source of truth, and a value Postgres
does not know is a write that fails at runtime.

Vocabulary is fixed by CLAUDE.md and docs/DATA-MODEL.md. If a column and the UI
disagree on a word, one of them is wrong; fix it rather than translating in three
places.
"""

from enum import StrEnum


class Role(StrEnum):
    """One kind of account. Mirrors ``public.app_role``.

    Deliberately still an enum with one member rather than a column that was
    deleted: reintroducing a distinction later is an ``alter type ... add value``
    plus the policies that read it, not a new identity model. Nothing in the
    application branches on this today, and nothing should start to without a
    migration landing first.
    """

    USER = "user"


class BookScope(StrEnum):
    """The security-critical column.

    ``canon``    a book its owner has shared with everyone. The only pool
                 assessment generation may draw from.
    ``personal`` a private upload. Visible to its owner alone, and never a source
                 for a shared test.

    Every book starts ``personal``. Sharing is a second, deliberate act by the
    owner, so nothing becomes readable by every user as a side effect of a file
    picker (DECISIONS.md D16).
    """

    CANON = "canon"
    PERSONAL = "personal"


class SourceFormat(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    PPTX = "pptx"
    TXT = "txt"
    MD = "md"
    # One book uploaded in parts. The archive stays the stored source object and
    # its members are combined at parse time, in reading order, with page numbers
    # continuing across the seams (DECISIONS.md D26).
    ZIP = "zip"


class BookKind(StrEnum):
    """What sort of book this is. Mirrors ``public.book_kind``.

    Four values, and the shortness is the design. This exists to set the tutor's
    *register* -- you do not explain a novel the way you explain a thermodynamics
    textbook -- and nothing else. It is never a retrieval filter: which book answers
    a question is decided from the retrieved chunks (``app/rag/rank.py``), where the
    evidence actually is, not from a label written at ingest time.

    A finer taxonomy lives in ``books.genre`` as free text, for display only. That
    split is deliberate: a wrong ``genre`` is cosmetic, whereas anything that filtered
    retrieval would turn a misclassification into material nobody can reach again.
    """

    FICTION = "fiction"
    NONFICTION = "nonfiction"
    ACADEMIC = "academic"
    REFERENCE = "reference"


class BookStatus(StrEnum):
    UPLOADED = "uploaded"
    PARSING = "parsing"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    READY = "ready"
    FAILED = "failed"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class MessageIntent(StrEnum):
    """What the reader was doing. Mirrors ``public.message_intent``.

    This decides the shape of a whole turn, which is why it is a column and not a
    transient. Before it existed every message went to vector search, and "hi" came
    back as "Your books don't cover that" -- correct grounded-refusal code producing
    a product that felt broken. A greeting is not a failed question.

    ``FOLLOW_UP`` is here rather than being folded into ``QUESTION`` because it takes
    a different path: "explain that more simply" has to be condensed against the
    transcript *before* it is embedded, or it retrieves on the word "that".
    """

    QUESTION = "question"
    FOLLOW_UP = "follow_up"
    GREETING = "greeting"
    CHITCHAT = "chitchat"
    META = "meta"
    UNCLEAR = "unclear"

    @property
    def needs_retrieval(self) -> bool:
        """Whether this turn goes to the books at all.

        The three that do not are answered from fixed copy or from what the server
        already knows, with no embedding call and no LLM content call. That is not a
        shortcut: there is no claim about the material being made, so there is nothing
        to ground (CLAUDE.md invariant 5).
        """
        return self in (MessageIntent.QUESTION, MessageIntent.FOLLOW_UP)


class AssessmentType(StrEnum):
    MCQ = "mcq"
    SUBJECTIVE = "subjective"
    MIXED = "mixed"


class AssessmentStatus(StrEnum):
    DRAFT = "draft"
    GENERATING = "generating"
    PUBLISHED = "published"
    CLOSED = "closed"


class ResultsRelease(StrEnum):
    """When a graded attempt becomes visible to the person who sat it."""

    IMMEDIATE = "immediate"
    ON_REVIEW = "on_review"


class QuestionType(StrEnum):
    """The **grading family** -- one marking code path each. One, now (D32).

    Kept as an enum beside :class:`QuestionFormat` rather than collapsed away, because
    the split is what D25 got right even though the fourteen formats were not: the
    shape a sitter sees and the code path that marks it are different questions, and
    marking is the one place here where a silent bug is a fairness incident.

    Adding a member means adding a marking function in ``services/grading.py``, and it
    is the expensive direction -- a family owns answers already stored, so retiring one
    later means rewriting somebody's result. ``tests/test_formats.py`` refuses a family
    with no grader.
    """

    MCQ = "mcq"


class QuestionFormat(StrEnum):
    """The **shape** the author picks and the sitter sees. Mirrors
    ``public.question_format``.

    One shape. The other thirteen were removed in 20260902120000 (DECISIONS.md D32):
    each cost an LLM call of its own per paper, and on a CPU-only box that fan-out was
    the difference between a paper arriving in a minute and in twenty. The remaining
    expressiveness lives in :class:`Difficulty` and the author's free-text brief --
    "identify the logical fallacy" is a multiple choice at ``evaluate``.

    The format -> family mapping lives in ``app/rag/formats.py``, and Postgres holds
    the same mapping as a check constraint. Neither is decoration: a paper drawn as
    one thing and marked as another scores zero for everybody who sat it.
    """

    MCQ = "mcq"


class Difficulty(StrEnum):
    """What kind of thinking the question asks for -- Bloom's ladder, bottom to top.

    The column is still called ``difficulty`` because renaming it would rewrite
    eleven migrations' worth of references for a word. What it means is the
    cognitive level: ``RECALL`` is stated in the passage, ``EVALUATE`` weighs an
    argument, ``CREATE`` asks for something new built out of the material.

    How *hard* the paper is, as distinct from what it asks for, is
    :class:`AssessmentRigor` -- one setting for the whole paper.
    """

    RECALL = "recall"
    UNDERSTAND = "understand"
    APPLY = "apply"
    ANALYZE = "analyze"
    EVALUATE = "evaluate"
    CREATE = "create"


class AssessmentRigor(StrEnum):
    """How hard, for the paper as a whole. Mirrors ``public.assessment_rigor``.

    Orthogonal to :class:`Difficulty`: the same ``EVALUATE``-level question is
    written one way for a beginner and another for a graduate viva. It steers the
    prompt's register and nothing else -- it is never a retrieval filter, for the
    same reason ``BookKind`` is not.
    """

    BEGINNER = "beginner"
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"
    EXPERT = "expert"
    COMPETITIVE = "competitive"
    INTERVIEW = "interview"
    GRADUATE = "graduate"
    RESEARCH = "research"


class QuestionOrigin(StrEnum):
    """Where a question came from. Provenance a sitter can be shown if they dispute it.

    ``HARVESTED`` is separate from ``GENERATED`` because the difference is one an
    author must be able to see before they publish (D31). A generated question is the
    model's first draft over which the author is author of record; a harvested one is
    printed in the book and reproduced, with only its answer supplied. Those are
    different things to publish and different things to defend.

    ``EDITED`` wins over either the moment a human changes one: the question on the
    paper is then neither the model's nor the book's.
    """

    GENERATED = "generated"
    HARVESTED = "harvested"
    EDITED = "edited"
    WRITTEN = "written"


class AttemptStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    AUTO_SUBMITTED = "auto_submitted"
    VOIDED = "voided"


class Grader(StrEnum):
    """Who produced a mark. ``HUMAN`` is the assessment's author overriding a
    machine grade -- which is always allowed and always recorded (D11)."""

    AUTO = "auto"
    LLM = "llm"
    HUMAN = "human"


class NotificationKind(StrEnum):
    """What a notification is about. Mirrors ``public.notification_kind``.

    Every one of these is addressed to somebody who has to *act*: an author whose
    paper was sat, an author whose paper finished generating, an author with a
    proctoring review waiting. None of them is addressed to a sitter, and that is not
    an oversight — a mark reaches a sitter when its author releases it, and a
    proctoring finding reaches them only through a reviewed report (invariant 3).
    A notification must never become the channel that outruns either gate.
    """

    ATTEMPT_SUBMITTED = "attempt_submitted"
    ASSESSMENT_READY = "assessment_ready"
    REVIEW_PENDING = "review_pending"


class ProctorSessionStatus(StrEnum):
    ACTIVE = "active"
    CLOSED = "closed"
    ABORTED = "aborted"


class ReviewStatus(StrEnum):
    """The review gate. ``released`` is reachable only by the assessment author's
    explicit act -- never by a timer, a threshold, or a default."""

    PENDING = "pending"
    CLEARED = "cleared"
    FLAGGED = "flagged"
    RELEASED = "released"


class EventType(StrEnum):
    """What the camera and the browser observed. Observations, never accusations."""

    SESSION_START = "session_start"
    SESSION_END = "session_end"
    HEARTBEAT_GAP = "heartbeat_gap"
    NO_FACE = "no_face"
    MULTIPLE_FACES = "multiple_faces"
    FACE_MISMATCH = "face_mismatch"
    GAZE_AWAY = "gaze_away"
    HEAD_POSE_AWAY = "head_pose_away"
    PHONE_VISIBLE = "phone_visible"
    TAB_BLUR = "tab_blur"
    WINDOW_BLUR = "window_blur"
    FULLSCREEN_EXIT = "fullscreen_exit"
    COPY = "copy"
    PASTE = "paste"
    CONTEXT_MENU = "context_menu"
    CAMERA_DENIED = "camera_denied"
    CAMERA_STOPPED = "camera_stopped"
    CLOCK_SKEW = "clock_skew"
    # The screen-share gate (Phase 7b). Named like their camera siblings.
    SCREEN_SHARE_DENIED = "screen_share_denied"
    SCREEN_SHARE_STOPPED = "screen_share_stopped"
    # More than one display connected, sustained — the setup screen asks for the
    # extra one to be disconnected before the sitting begins.
    MULTIPLE_DISPLAYS = "multiple_displays"


class Severity(StrEnum):
    """Assigned server-side from a fixed map keyed by ``EventType``.

    A client that reports ``info`` for everything changes nothing.
    """

    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AuthorVerdict(StrEnum):
    """The author's judgement on one observed event. Mirrors ``public.author_verdict``.

    Named for the author, not a "teacher" — that vocabulary was removed with the
    role model (D16). ``DISMISSED`` events are excluded from any released report
    and from any released score; ``UPHELD`` is what a sitter eventually sees.
    """

    UNREVIEWED = "unreviewed"
    DISMISSED = "dismissed"
    UPHELD = "upheld"
