"""Grading. Phases 6 and 5b, narrowed by D32.

One marking path, for the one :class:`QuestionType`. It is arithmetic: deterministic,
instant, and free. There is **no LLM call anywhere in grading** since the subjective
family was retired — a submitted paper is marked in milliseconds rather than queueing
behind generation for the single Ollama slot.

An author can still overrule any mark (DECISIONS.md D11); the override lives in
``services/attempts.py`` beside the score it recomputes, and it is what
``Grader.HUMAN`` protects below.

Adding a marking path means adding a family, and ``tests/test_formats.py`` refuses a
family with no grader here. That check matters more now, not less: with one entry in
the table, a second family added carelessly would be the only thing standing between a
sitter and a mark produced by no rule at all.

Grading arithmetic is one of the four things CLAUDE.md requires tests for before merge:
a silent bug here is a fairness incident, not a broken page.
"""

import logging
from collections.abc import Callable
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Answer, Question
from app.db.models.enums import Grader, QuestionType

logger = logging.getLogger(__name__)

_CENTS = Decimal("0.01")


def quantize(value: Decimal) -> Decimal:
    """Two decimal places, half-up. The column is numeric(10,2).

    Explicit rather than left to the driver: a mark that rounds differently on the way
    in than the way out is a mark somebody will eventually query.
    """
    return value.quantize(_CENTS, rounding=ROUND_HALF_UP)


def grade_mcq(question: Question, response: str | None) -> Decimal:
    """Exact match on the correct option key. Instant, and never an LLM call.

    Case and whitespace are normalised because the key travels through a URL and a
    form; nothing else about the response is interpreted. A response that is not one
    of the option keys scores zero rather than erroring — an old tab submitting a key
    that no longer exists is a wrong answer, not a server fault.
    """
    if not response or not question.correct_option:
        return Decimal("0")
    if response.strip().upper() == question.correct_option.strip().upper():
        return quantize(question.points)
    return Decimal("0")


# One entry per family, and since D32 that is the whole of `QuestionType`. Marking is
# now entirely deterministic: no LLM call, no rubric, no rationale.
#
# Kept as a table rather than inlined into the one caller, because it is what
# tests/test_formats.py reads to assert that no family exists without a grader. A
# family added later with no entry here must fail loudly rather than fall through to
# something that marks it approximately.
_DETERMINISTIC: dict[QuestionType, Callable[[Question, str | None], Decimal]] = {
    QuestionType.MCQ: grade_mcq,
}


async def grade_answer(
    session: AsyncSession, question: Question, answer: Answer
) -> None:
    """Mark one answer in place. Never overwrites a human's mark.

    An override that a re-run silently reverted would make the override worthless, and
    at-least-once delivery means re-runs happen.

    Still ``async``: every caller awaits it, and grading is the layer most likely to
    need a call again if a family that cannot be marked arithmetically ever returns.
    """
    if answer.grader is Grader.HUMAN:
        return

    deterministic = _DETERMINISTIC.get(question.type)
    if deterministic is None:
        # Unreachable while the dispatch is total over QuestionType, and it must stay
        # that way: falling back to a default marker would mark somebody's paper by a
        # rule nobody chose. Raising fails the attempt loudly, which the grading task
        # writes onto the row as a user-facing reason.
        raise ValueError(f"no grader for question family {question.type!r}")

    answer.awarded_points = deterministic(question, answer.response)
    answer.grader = Grader.AUTO


async def answers_for(session: AsyncSession, attempt_id: object) -> list[Answer]:
    return list(await session.scalars(select(Answer).where(Answer.attempt_id == attempt_id)))
