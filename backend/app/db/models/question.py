"""questions — one item on a paper, always traceable to material. Phase 5.

`source_chunk_ids` is not optional: a question whose passage cannot be produced is a
question the author cannot defend when a sitter disputes it.

**A sitter has no policy on this table at all.** The row carries `correct_option`,
`answer_key`, `model_answer` and `rubric`, and row-level security cannot hide a column
— so the only safe answer is that they never reach the row. They read
`public.question_sit`, which does not contain those columns. See :class:`QuestionSit`
below.

Two columns describe what a question *is*, and the split is deliberate: `type` is the
grading family (six, one marking path each) and `format` is the shape the author picked
(fourteen, one prompt and one renderer each). DECISIONS.md D25.

Mirrors supabase/migrations/. SQLAlchemy does not own this schema and never creates it.
"""

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, Integer, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey
from app.db.models.enums import (
    Difficulty,
    QuestionFormat,
    QuestionOrigin,
    QuestionType,
)


def _pg_enum(python_enum, name: str):
    return SAEnum(
        python_enum,
        name=name,
        schema="public",
        create_type=False,
        values_callable=lambda enum: [member.value for member in enum],
    )


class Question(Base, UUIDPrimaryKey, Timestamps):
    __tablename__ = "questions"

    assessment_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("assessments.id", ondelete="CASCADE"), nullable=False
    )
    index: Mapped[int] = mapped_column("index", Integer, nullable=False)

    # The grading family — one marking path each. Six of them.
    type: Mapped[QuestionType] = mapped_column(
        _pg_enum(QuestionType, "question_type"), nullable=False
    )
    # The shape the author picked and the sitter sees. Fourteen of them, mapping
    # many-to-one onto `type`; the database refuses a row where the two disagree.
    format: Mapped[QuestionFormat] = mapped_column(
        _pg_enum(QuestionFormat, "question_format"),
        nullable=False,
        server_default=text("'mcq'::public.question_format"),
    )
    stem: Mapped[str] = mapped_column(Text, nullable=False)

    # The choice list, for every format that has one: the options of an mcq, the
    # right-hand column of a match grid, the scrambled steps of a sequence.
    options: Mapped[list[dict[str, str]] | None] = mapped_column(JSONB, nullable=True)
    correct_option: Mapped[str | None] = mapped_column(String, nullable=True)

    # The sitter-visible other half of a two-sided question: the left column of a
    # match grid. An item list, not an answer.
    prompt_items: Mapped[list[dict[str, str]] | None] = mapped_column(JSONB, nullable=True)

    # Every format-specific correct answer that is not `correct_option`:
    # {correct_options: [...]} | {accepted: [...], tolerance: n} |
    # {pairs: {left: right}} | {order: [...]}.
    #
    # One column rather than four, because the rule that matters is that it is ABSENT
    # from `public.question_sit`. One column is one thing to keep out of one view.
    answer_key: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # subjective
    model_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    rubric: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)

    points: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, server_default=text("1")
    )
    difficulty: Mapped[Difficulty | None] = mapped_column(
        _pg_enum(Difficulty, "difficulty"), nullable=True
    )

    source_chunk_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    origin: Mapped[QuestionOrigin] = mapped_column(
        _pg_enum(QuestionOrigin, "question_origin"),
        nullable=False,
        server_default=text("'generated'::public.question_origin"),
    )


class QuestionSit(Base):
    """``public.question_sit`` — the sitting projection, read-only.

    A view, not a table. It exists because the columns that must not reach a sitter are
    *absent by construction* rather than dropped by a serializer: a `select *` in some
    future refactor cannot leak what the view never selected.

    Never write through this. It has no primary key in the database; ``id`` is mapped
    as one only because SQLAlchemy requires a mapper to have one.
    """

    __tablename__ = "question_sit"
    __read_only__ = True

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    assessment_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True))
    index: Mapped[int] = mapped_column("index", Integer)
    type: Mapped[QuestionType] = mapped_column(_pg_enum(QuestionType, "question_type"))
    format: Mapped[QuestionFormat] = mapped_column(_pg_enum(QuestionFormat, "question_format"))
    stem: Mapped[str] = mapped_column(Text)
    options: Mapped[list[dict[str, str]] | None] = mapped_column(JSONB)
    prompt_items: Mapped[list[dict[str, str]] | None] = mapped_column(JSONB)
    points: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    difficulty: Mapped[Difficulty | None] = mapped_column(_pg_enum(Difficulty, "difficulty"))


class QuestionKey(Base):
    """``public.question_key`` — the answer key, read-only.

    Visible to the author always, and to a sitter only once their own result has been
    released. Releasing one person's result does not open the key to everyone else
    still sitting — the view's predicate checks the caller's own attempt.
    """

    __tablename__ = "question_key"
    __read_only__ = True

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    assessment_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True))
    index: Mapped[int] = mapped_column("index", Integer)
    type: Mapped[QuestionType] = mapped_column(_pg_enum(QuestionType, "question_type"))
    format: Mapped[QuestionFormat] = mapped_column(_pg_enum(QuestionFormat, "question_format"))
    stem: Mapped[str] = mapped_column(Text)
    # The options are here as well as the key, because this view renders a MARKED
    # paper. Without them a result screen can say the answer was "B" but not what B
    # said, which is a result nobody learns anything from.
    options: Mapped[list[dict[str, str]] | None] = mapped_column(JSONB)
    prompt_items: Mapped[list[dict[str, str]] | None] = mapped_column(JSONB)
    correct_option: Mapped[str | None] = mapped_column(String)
    answer_key: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    model_answer: Mapped[str | None] = mapped_column(Text)
    rubric: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    points: Mapped[Decimal] = mapped_column(Numeric(10, 2))
