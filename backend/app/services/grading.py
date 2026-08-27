"""Grading. Phases 6 and 5b.

Six marking paths, one per :class:`QuestionType`. Five are deterministic and instant;
only ``subjective`` costs an LLM call, and its result is a draft judgement the author
can overrule rather than a final grade (DECISIONS.md D11). An override lives in
``services/attempts.py`` beside the score it recomputes.

Fourteen question *formats* share those six paths — a true/false question is marked
exactly as an mcq is, because it is one. Adding a seventh path means adding a family,
and ``tests/test_formats.py`` refuses a family with no grader here.

**Partial credit is a policy, not an accident.** Three of the deterministic families
can be half-right, and each says below what it awards and why. A sitter who gets three
of four pairs in a match grid and scores zero has been marked by a bug, not a rule.

Grading arithmetic is one of the four things CLAUDE.md requires tests for before merge:
a silent bug here is a fairness incident, not a broken page.
"""

import json
import logging
import re
from collections.abc import Callable
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients import llm
from app.db.models import Answer, Question
from app.db.models.enums import Grader, QuestionFormat, QuestionType
from app.rag import prompts

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


# ===================================================== structured responses
#
# The families below are answered with something richer than a string, and it travels
# in ``answers.response`` as compact JSON — ["A","C"], {"1":"B"}, ["C","A","B"]. One
# column rather than two, so there is never a question about which one holds the
# answer.
#
# Every parser here returns an empty value rather than raising. A response that will
# not parse is an unanswered question — an old tab, a truncated save, a client that
# was rewritten badly — and it scores zero. It is not a server fault, and a grading
# run that dies on one malformed row leaves a whole cohort unmarked.


def _parse_list(response: str | None) -> list[str]:
    if not response:
        return []
    try:
        parsed = json.loads(response)
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item).strip().upper() for item in parsed if str(item).strip()]


def _parse_mapping(response: str | None) -> dict[str, str]:
    if not response:
        return {}
    try:
        parsed = json.loads(response)
    except (ValueError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        str(key).strip().upper(): str(value).strip().upper()
        for key, value in parsed.items()
        if str(value).strip()
    }


def _fraction(question: Question, earned: float, total: float) -> Decimal:
    """Turn "three of four" into marks, clamped and rounded once."""
    if total <= 0:
        return Decimal("0")
    share = max(0.0, min(1.0, earned / total))
    return quantize(question.points * Decimal(str(share)))


def grade_multi_select(question: Question, response: str | None) -> Decimal:
    """Partial credit, with a wrong tick cancelling a right one.

        (correct ticked - incorrectly ticked) / correct available

    floored at zero. The subtraction is the whole design: without it, ticking every
    option scores full marks on every select-all-that-apply question ever written,
    and the format is worth nothing. With it, a guess costs what it would gain.

    Never negative. A question cannot take marks off another question — the floor is
    what keeps this partial credit rather than negative marking, which is a different
    policy and one this product has not chosen.
    """
    correct = {
        str(key).strip().upper()
        for key in (question.answer_key or {}).get("correct_options", [])
    }
    if not correct:
        return Decimal("0")
    chosen = set(_parse_list(response))
    hits = len(chosen & correct)
    wrong = len(chosen - correct)
    return _fraction(question, hits - wrong, len(correct))


def grade_match(question: Question, response: str | None) -> Decimal:
    """One share of the marks per correctly matched pair.

    Per-pair rather than all-or-nothing because a four-pair grid is four questions
    printed together, and marking it as one makes a single slip cost four marks.
    """
    pairs = (question.answer_key or {}).get("pairs") or {}
    if not isinstance(pairs, dict) or not pairs:
        return Decimal("0")
    expected = {
        str(left).strip().upper(): str(right).strip().upper()
        for left, right in pairs.items()
    }
    given = _parse_mapping(response)
    hits = sum(1 for left, right in expected.items() if given.get(left) == right)
    return _fraction(question, hits, len(expected))


def grade_sequence(question: Question, response: str | None) -> Decimal:
    """One share of the marks per item in its correct position.

    Position-wise rather than by adjacency or inversion count, because it is the rule
    a sitter can predict from the screen in front of them: the item is where it should
    be, or it is not. Kendall's tau would be fairer to somebody who shifted the whole
    list by one, and no sitter would be able to check their own mark.

    A response that omits or invents items is not an error — the extra items simply
    match nothing and the missing positions earn nothing.
    """
    order = (question.answer_key or {}).get("order") or []
    expected = [str(key).strip().upper() for key in order]
    if not expected:
        return Decimal("0")
    given = _parse_list(response)
    hits = sum(
        1
        for position, key in enumerate(expected)
        if position < len(given) and given[position] == key
    )
    return _fraction(question, hits, len(expected))


# --------------------------------------------------------------- typed answers

# Articles and trailing punctuation, stripped before comparison. Nothing else: no
# stemming, no synonyms, no edit distance. A grader that decides "photosynthesys" is
# close enough is a grader whose decisions the author cannot predict or explain, and
# the fix for a spelling the key should accept is to add it to the key.
_ARTICLES = ("the ", "a ", "an ")
_TRAILING = " \t\r\n.,;:!?\"')"


def normalise_text_answer(value: str) -> str:
    """Casefold, collapse whitespace, drop a leading article and trailing punctuation.

    Deliberately shallow, and the shallowness is the contract: what this does is
    listed in one sentence, so an author reading it knows exactly which near-misses
    they still have to enumerate in ``accepted``.
    """
    text = " ".join(value.strip().casefold().split()).strip(_TRAILING)
    for article in _ARTICLES:
        if text.startswith(article):
            text = text[len(article) :]
            break
    return text.strip()


def _as_number(value: str) -> float | None:
    """A bare number, tolerating thousands separators and a stray unit or symbol."""
    cleaned = value.strip().replace(",", "").replace("%", "")
    match = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", cleaned)
    if match is None:
        return None
    try:
        return float(match.group())
    except ValueError:
        return None


def grade_short_text(question: Question, response: str | None) -> Decimal:
    """All or nothing against an enumerated key. Never an LLM call.

    The key lists every answer worth the mark, because that is what makes this format
    honest: a one-word question whose marking rule is "whatever the model thinks is
    close" cannot be defended to somebody who got it wrong by a hyphen.

    ``numeric`` questions compare as numbers within an optional relative tolerance,
    so 3.14159 and 3.14 can both be right when the author says how close is close.
    """
    key = question.answer_key or {}
    accepted = [str(item) for item in key.get("accepted", []) if str(item).strip()]
    if not accepted or not (response or "").strip():
        return Decimal("0")

    if question.format is QuestionFormat.NUMERIC:
        given = _as_number(response or "")
        if given is not None:
            tolerance = abs(float(key.get("tolerance") or 0))
            for candidate in accepted:
                target = _as_number(candidate)
                if target is None:
                    continue
                # Relative to the expected magnitude, with an absolute floor so that
                # a correct answer of exactly zero is still reachable.
                slack = max(abs(target) * tolerance, tolerance)
                if abs(given - target) <= slack:
                    return quantize(question.points)
        # Fall through: a numeric question answered in words ("forty two") is still
        # compared as text, because the key may well list it.

    given_text = normalise_text_answer(response or "")
    if any(given_text == normalise_text_answer(candidate) for candidate in accepted):
        return quantize(question.points)
    return Decimal("0")


def score_from_criteria(
    per_criterion: list[dict[str, Any]], *, maximum: Decimal
) -> Decimal:
    """Recompute the total from its parts and clamp it. Never trust the model's sum.

    Models do arithmetic badly, and a total that does not equal its own breakdown is
    the one number in this system that somebody will definitely check by hand.
    """
    total = Decimal("0")
    for entry in per_criterion:
        try:
            total += Decimal(str(entry.get("awarded", 0)))
        except (ArithmeticError, ValueError, TypeError):
            continue
    if total < 0:
        total = Decimal("0")
    if total > maximum:
        total = maximum
    return quantize(total)


def parse_grading(raw: str) -> dict[str, Any]:
    """Parse the grader's reply. Tolerates a fence; repairs nothing else."""
    text = raw.strip()
    if "```" in text:
        blocks = re.findall(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if blocks:
            text = blocks[0].strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in grading response")
    payload = json.loads(text[start : end + 1])
    if not isinstance(payload.get("per_criterion"), list):
        raise ValueError("`per_criterion` is missing or not a list")
    return payload


async def grade_subjective(
    question: Question, response: str | None
) -> tuple[Decimal, str | None, dict | None]:
    """One LLM call per answer, against the model answer and rubric.

    Returns ``(points, feedback, rationale)``.

    A blank answer is zero without a call — there is nothing to mark, and asking a
    model to mark nothing invites it to award something for effort.

    The grader is given the question, the model answer and the rubric. It is NOT given
    the book: an unbounded grader invents criteria that were never on the paper.
    """
    if not (response or "").strip():
        return Decimal("0"), "No answer was given.", None

    rubric = question.rubric or []
    messages = prompts.grading_prompt(
        stem=question.stem,
        model_answer=question.model_answer or "",
        rubric=rubric,
        response=response or "",
    )

    raw = await llm.complete(messages)
    payload = parse_grading(raw)

    points = score_from_criteria(payload["per_criterion"], maximum=question.points)
    feedback = str(payload.get("feedback") or "").strip() or None
    return points, feedback, payload


# One entry per family that marks without an LLM. `subjective` is deliberately absent:
# it is the only one that costs a call, and the dispatch below reads better as "is
# there a deterministic marker for this, or do we have to ask the model".
#
# A family missing from here AND from the subjective path would be marked as
# subjective by accident and sent to an LLM with no rubric. tests/test_formats.py
# asserts every family is reachable.
_DETERMINISTIC: dict[QuestionType, Callable[[Question, str | None], Decimal]] = {
    QuestionType.MCQ: grade_mcq,
    QuestionType.MULTI_SELECT: grade_multi_select,
    QuestionType.SHORT_TEXT: grade_short_text,
    QuestionType.MATCH: grade_match,
    QuestionType.SEQUENCE: grade_sequence,
}


async def grade_answer(
    session: AsyncSession, question: Question, answer: Answer
) -> None:
    """Mark one answer in place. Never overwrites a human's mark.

    An override that a re-run silently reverted would make the override worthless, and
    at-least-once delivery means re-runs happen.
    """
    if answer.grader is Grader.HUMAN:
        return

    deterministic = _DETERMINISTIC.get(question.type)
    if deterministic is not None:
        answer.awarded_points = deterministic(question, answer.response)
        answer.grader = Grader.AUTO
        return

    points, feedback, rationale = await grade_subjective(question, answer.response)
    answer.awarded_points = points
    answer.feedback = feedback
    answer.llm_rationale = rationale
    answer.grader = Grader.LLM if rationale is not None else Grader.AUTO


async def answers_for(session: AsyncSession, attempt_id: object) -> list[Answer]:
    return list(await session.scalars(select(Answer).where(Answer.attempt_id == attempt_id)))
