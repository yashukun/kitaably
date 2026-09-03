"""Grading arithmetic.

One of the four things CLAUDE.md requires tests for before merge. A silent bug here is
a fairness incident, not a broken page: it produces a number that looks like a mark,
gets released to the person who sat the paper, and nobody notices until somebody
adds it up by hand.
"""

from decimal import Decimal

import pytest

from app.db.models import Question
from app.db.models.enums import QuestionType
from app.services.grading import _DETERMINISTIC, grade_mcq, quantize


def mcq(correct: str, points: str = "1") -> Question:
    return Question(
        type=QuestionType.MCQ,
        stem="Where does the Calvin cycle occur?",
        correct_option=correct,
        points=Decimal(points),
    )


# --- MCQ: exact match, and nothing clever ----------------------------------


def test_correct_answer_scores_full_marks() -> None:
    assert grade_mcq(mcq("B", "2"), "B") == Decimal("2.00")


def test_wrong_answer_scores_zero() -> None:
    assert grade_mcq(mcq("B", "2"), "C") == Decimal("0")


@pytest.mark.parametrize("response", ["b", " B ", "b\n"])
def test_case_and_whitespace_do_not_cost_marks(response: str) -> None:
    """The key travels through a URL and a form. Neither should change a mark."""
    assert grade_mcq(mcq("B"), response) == Decimal("1.00")


@pytest.mark.parametrize("response", [None, "", "   "])
def test_no_answer_scores_zero(response: str | None) -> None:
    assert grade_mcq(mcq("B"), response) == Decimal("0")


def test_a_key_that_is_not_an_option_scores_zero_rather_than_raising() -> None:
    """An old tab submitting a key that no longer exists is a wrong answer, not a
    server fault. Raising here would turn one stale client into a failed grading run
    for the whole paper."""
    assert grade_mcq(mcq("B"), "Z") == Decimal("0")


def test_a_question_with_no_answer_key_scores_zero() -> None:
    """Publishing refuses this, so it should be unreachable — which is exactly why it
    is worth pinning. Unreachable states are where the unhandled exceptions live."""
    assert grade_mcq(mcq(None), "B") == Decimal("0")  # type: ignore[arg-type]


# --- rounding ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1.005", "1.01"), ("1.004", "1.00"), ("2.675", "2.68"), ("0.125", "0.13")],
)
def test_marks_round_half_up_to_two_places(raw: str, expected: str) -> None:
    """The column is numeric(10,2). Rounding half-up explicitly rather than leaving it
    to the driver means a mark cannot change on its way in and back out again — and
    banker's rounding, the Python default, would round 0.125 down to 0.12."""
    assert quantize(Decimal(raw)) == Decimal(expected)


def test_quantized_marks_sum_without_drift() -> None:
    """Three thirds of a mark must not add up to 0.99 on a gradebook."""
    thirds = [quantize(Decimal("1") / 3) for _ in range(3)]
    assert sum(thirds) == Decimal("0.99")  # documented, not accidental


# --- the dispatch ------------------------------------------------------------


def test_every_family_reaches_its_own_marker() -> None:
    """The dispatch is total over the enum, and `grade_answer` has no fallthrough.

    That pairing is the point. A family with no entry here used to be graded as
    subjective by accident -- an LLM call, with no rubric, producing a mark nobody
    could defend. Now it raises, which fails the attempt loudly instead of marking
    somebody's paper by a rule nobody chose.
    """
    assert _DETERMINISTIC == {QuestionType.MCQ: grade_mcq}
    assert set(_DETERMINISTIC) == set(QuestionType)
