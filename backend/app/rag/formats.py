"""The question-format registry. Phase 5b, narrowed by D32.

One format, one grading family, and the table that says which is which.

Everything that needs to know about a format reads it here: the prompt that asks for
one, the validator that rejects a malformed one, the grader that marks it, the
serializer that sends it, and the UI copy that names it. Spreading that across four
files is how a format ends up generated in a shape nothing can mark.

The registry survives the collapse to one entry rather than being inlined, because it
is the shape that made the collapse cheap -- and would make a fifteenth format cheap
again. What it cost when there were fourteen was wall clock: generation makes at least
one LLM call per format, and on CPU-only Ollama that is a minute or two each.

Postgres holds the format -> family mapping a second time, as a check constraint last
rewritten in ``20260902120000_mcq_only.sql``. That is not duplication for its own sake:
a paper drawn as one thing and marked as another scores zero for everybody who sat it,
so the database refuses the row rather than trusting this file.
``tests/test_formats.py`` pins the two together.

Why one and not fourteen: DECISIONS.md D32, which revises D25.
"""

from dataclasses import dataclass
from decimal import Decimal

from app.db.models.enums import (
    Difficulty,
    QuestionFormat,
    QuestionType,
)


@dataclass(frozen=True)
class FormatSpec:
    """Everything the rest of the system needs to know about one format.

    ``instruction`` and ``shape`` are prompt fragments. ``family`` is the marking
    path. ``label`` and ``blurb`` are UI copy, kept here rather than in the frontend
    so that a format cannot be added without somebody deciding what to call it.
    """

    format: QuestionFormat
    family: QuestionType
    label: str
    blurb: str
    instruction: str
    default_points: Decimal
    # Bounds on the choice list the model must return. (0, 0) means the format has
    # no options at all — kept, because it is what makes `build_question_fields`
    # refuse a reply with no choices rather than storing an unanswerable question.
    min_options: int = 0
    max_options: int = 0
    # Shortest usable stem.
    min_stem: int = 20
    # The bounds are wider than what the prompt asks for on purpose. The prompt
    # says "four options"; this says what a reply is measured against, and
    # discarding a sound three-option question to enforce house style costs the
    # paper a question and buys nothing.

    # Roughly what one question of this format costs in reply tokens. An estimate,
    # deliberately generous: it only has to keep `asked × reply_tokens` inside the
    # reply ceiling, because a reply that hits the ceiling mid-array is a truncated
    # JSON that fails the WHOLE call — a real run spent 170 seconds decoding five
    # multi-selects that arrived as un-parseable JSON (D30). Precision buys nothing;
    # headroom saves the call.
    reply_tokens: int = 120


# The JSON fragment each family must return, appended to the prompt verbatim. One per
# family rather than one per format, because a true/false question and an mcq are the
# same object with a different number of options.
#
# Every field asked for here is persisted or validated against. There used to be a
# `rationale` field, parsed and then discarded — at the ~7.5 tokens/second a CPU
# model decodes, it was four to six seconds of wall clock per question spent writing
# something nothing ever read (D30). `GeneratedQuestion` still tolerates the field,
# so a model that volunteers one costs nothing but its own time.
FAMILY_SHAPE: dict[QuestionType, str] = {
    QuestionType.MCQ: (
        '{"format": "mcq", "stem": "...", '
        '"options": [{"key": "A", "text": "..."}, {"key": "B", "text": "..."}], '
        '"correct_option": "A", "difficulty": "recall", '
        '"source_chunk_id": "..."}'
    ),
}


SPECS: dict[QuestionFormat, FormatSpec] = {
    # ------------------------------------------------- one correct key (mcq)
    QuestionFormat.MCQ: FormatSpec(
        format=QuestionFormat.MCQ,
        family=QuestionType.MCQ,
        label="Multiple choice",
        blurb="Four options, one right.",
        instruction=(
            "Four options, exactly one correct. Distractors must be plausible to "
            "somebody who half-remembers the passage and wrong to somebody who read "
            "it — a distractor nobody would pick is a question with three options."
        ),
        default_points=Decimal("1"),
        min_options=3,
        max_options=5,
    ),
}


FAMILY_OF: dict[QuestionFormat, QuestionType] = {
    fmt: spec.family for fmt, spec in SPECS.items()
}


def spec_for(value: str | QuestionFormat) -> FormatSpec | None:
    """Look up a format by value, tolerating anything. Returns None if unknown.

    Tolerant because one caller is a model's JSON reply, where an unknown value is a
    rejected question rather than a crashed batch.
    """
    try:
        return SPECS[QuestionFormat(value)]
    except (ValueError, KeyError):
        return None


# ============================================================ the level spread
#
# There is no longer a format mix to resolve: one format, so every paper is drawn as
# multiple choice and `resolve_formats` says so. What the author still steers is the
# cognitive level and the rigor -- "identify the logical fallacy" is a multiple choice
# at `evaluate`, not a fifteenth format (DECISIONS.md D32).

# The spread a paper gets when the author does not name cognitive levels either. The
# bottom three rungs: a paper that is all `create` is a coursework brief, not a test,
# and a paper that is all `recall` is a vocabulary quiz.
AUTO_LEVELS: tuple[Difficulty, ...] = (
    Difficulty.RECALL,
    Difficulty.UNDERSTAND,
    Difficulty.APPLY,
)


def resolve_formats() -> list[QuestionFormat]:
    """What to actually generate: multiple choice, always.

    Kept as a function rather than inlined at the two call sites so that the answer to
    "what is this paper drawn as" has exactly one place to change, which is what let
    this collapse be a small diff rather than a search-and-replace.
    """
    return [QuestionFormat.MCQ]


def resolve_levels(chosen: list[Difficulty] | None) -> list[Difficulty]:
    if chosen:
        return list(dict.fromkeys(chosen))
    return list(AUTO_LEVELS)


def batch_ask_cap(fmt: QuestionFormat, *, max_reply_tokens: int) -> int:
    """How many questions of this format one call may ask for.

    The failure this prevents is the truncated batch: a reply that hits the token
    ceiling mid-array is un-parseable JSON, and the WHOLE call — minutes of CPU
    decode — is thrown away. Asking for fewer questions per call and letting the
    backfill make up the difference turns that dead call into a smaller live one.
    Never below one: a format whose single question may not fit is still asked for
    one, because a truncated reply and no reply cost the same.
    """
    return max(1, max_reply_tokens // SPECS[fmt].reply_tokens)


def plan_mix(
    levels: list[Difficulty], *, count: int
) -> list[tuple[QuestionFormat, Difficulty]]:
    """Deal `count` (format, level) slots, round-robin over the levels.

    Still returns pairs, because the format is what the prompt and the trace are keyed
    on even now that there is only one of it. Round-robin rather than random so a
    five-question paper across three levels gets all three, and so the same request
    twice produces the same shape of paper. The model still decides what each question
    says; this only decides what is asked for.
    """
    if count <= 0:
        return []
    levels = levels or list(AUTO_LEVELS)
    return [(QuestionFormat.MCQ, levels[i % len(levels)]) for i in range(count)]
