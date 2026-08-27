"""Grading arithmetic.

One of the four things CLAUDE.md requires tests for before merge. A silent bug here is
a fairness incident, not a broken page: it produces a number that looks like a mark,
gets released to the person who sat the paper, and nobody notices until somebody
adds it up by hand.
"""

from decimal import Decimal

import pytest

from app.db.models import Question
from app.db.models.enums import QuestionFormat, QuestionType
from app.services.grading import (
    _DETERMINISTIC,
    grade_match,
    grade_mcq,
    grade_multi_select,
    grade_sequence,
    grade_short_text,
    quantize,
    score_from_criteria,
)


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


# --- subjective: the total is recomputed, never trusted ---------------------


def test_total_is_the_sum_of_its_parts() -> None:
    awards = [{"awarded": 1.5}, {"awarded": 1}, {"awarded": 0.5}]
    assert score_from_criteria(awards, maximum=Decimal("3")) == Decimal("3.00")


def test_total_is_clamped_to_the_question_marks() -> None:
    """Models do arithmetic badly and are generous under pressure. A three-mark
    question cannot award four, whatever the breakdown claims."""
    awards = [{"awarded": 3}, {"awarded": 3}]
    assert score_from_criteria(awards, maximum=Decimal("3")) == Decimal("3.00")


def test_negative_awards_cannot_drag_a_mark_below_zero() -> None:
    assert score_from_criteria([{"awarded": -5}], maximum=Decimal("3")) == Decimal("0")


def test_a_models_own_total_is_ignored() -> None:
    """The payload carries a `total` field. It is never read — this asserts that by
    supplying a wrong one and requiring the parts to win."""
    awards = [{"awarded": 1}, {"awarded": 1}]
    payload = {"per_criterion": awards, "total": 99}
    assert score_from_criteria(payload["per_criterion"], maximum=Decimal("5")) == Decimal("2.00")


@pytest.mark.parametrize(
    "entry", [{"awarded": "not a number"}, {"awarded": None}, {}, {"awarded": [1]}]
)
def test_an_unparseable_award_is_skipped_not_fatal(entry: dict) -> None:
    """One malformed criterion must not fail the whole paper's grading run."""
    awards = [{"awarded": 2}, entry]
    assert score_from_criteria(awards, maximum=Decimal("5")) == Decimal("2.00")


def test_no_criteria_is_zero() -> None:
    assert score_from_criteria([], maximum=Decimal("3")) == Decimal("0")


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


# ============================================================================
# The structured families. Phase 5b.
#
# Three of these award partial credit, and partial credit is where fairness bugs
# hide: they do not crash, they do not look wrong in a list of marks, and they are
# only ever found by somebody adding up their own paper by hand.


def structured(
    family: QuestionType,
    fmt: QuestionFormat,
    answer_key: dict,
    points: str,
    options: list[dict] | None = None,
) -> Question:
    return Question(
        type=family,
        format=fmt,
        stem="A question long enough to be a real one.",
        options=options,
        answer_key=answer_key,
        points=Decimal(points),
    )


# --- select all that apply ---------------------------------------------------


def multi(points: str = "3") -> Question:
    return structured(
        QuestionType.MULTI_SELECT,
        QuestionFormat.MULTI_SELECT,
        {"correct_options": ["A", "C", "D"]},
        points,
        options=[{"key": k, "text": k} for k in "ABCDE"],
    )


def test_every_correct_option_and_nothing_else_scores_full_marks() -> None:
    assert grade_multi_select(multi(), '["A","C","D"]') == Decimal("3.00")


def test_two_of_three_correct_scores_two_thirds() -> None:
    """Per-option credit, because a five-option select-all is really three questions
    printed together and marking it all-or-nothing makes one slip cost all three."""
    assert grade_multi_select(multi(), '["A","C"]') == Decimal("2.00")


def test_a_wrong_tick_cancels_a_right_one() -> None:
    """The whole design. Without the subtraction, ticking every box scores full marks
    on every select-all question ever written and the format is worth nothing."""
    assert grade_multi_select(multi(), '["A","C","B"]') == Decimal("1.00")


def test_ticking_everything_does_not_score_full_marks() -> None:
    """Three right and two wrong out of three available, so a third of the marks —
    strictly worse than answering the two you were sure of and stopping."""
    assert grade_multi_select(multi(), '["A","B","C","D","E"]') == Decimal("1.00")


def test_a_multi_select_never_goes_negative() -> None:
    """A floor, not negative marking. A question must not be able to take marks off
    another question — that is a different policy, and not one this product chose."""
    assert grade_multi_select(multi(), '["B","E"]') == Decimal("0")


@pytest.mark.parametrize("response", [None, "", "not json", '"A"', "[]"])
def test_an_unparseable_multi_select_scores_zero_rather_than_raising(response) -> None:
    """A grading run that dies on one malformed row leaves a whole cohort unmarked."""
    assert grade_multi_select(multi(), response) == Decimal("0")


# --- match the following -----------------------------------------------------


def grid(points: str = "4") -> Question:
    return structured(
        QuestionType.MATCH,
        QuestionFormat.MATCH,
        {"pairs": {"1": "A", "2": "B", "3": "C", "4": "D"}},
        points,
        options=[{"key": k, "text": k} for k in "ABCD"],
    )


def test_a_fully_correct_grid_scores_full_marks() -> None:
    assert grade_match(grid(), '{"1":"A","2":"B","3":"C","4":"D"}') == Decimal("4.00")


def test_three_of_four_pairs_scores_three() -> None:
    """A four-pair grid is four questions printed together. Marking it as one makes a
    single slip cost four marks, which is not what the screen implies."""
    assert grade_match(grid(), '{"1":"A","2":"B","3":"C","4":"A"}') == Decimal("3.00")


def test_an_empty_grid_scores_zero() -> None:
    assert grade_match(grid(), "{}") == Decimal("0")


def test_a_grid_answered_with_a_list_scores_zero_rather_than_raising() -> None:
    assert grade_match(grid(), '["A","B"]') == Decimal("0")


# --- put in order ------------------------------------------------------------


def ordered(points: str = "4") -> Question:
    return structured(
        QuestionType.SEQUENCE,
        QuestionFormat.SEQUENCE,
        {"order": ["C", "A", "D", "B"]},
        points,
        options=[{"key": k, "text": k} for k in "ABCD"],
    )


def test_the_right_order_scores_full_marks() -> None:
    assert grade_sequence(ordered(), '["C","A","D","B"]') == Decimal("4.00")


def test_a_sequence_is_marked_by_position() -> None:
    """Position-wise rather than by inversion count, because it is the rule a sitter
    can check against the screen in front of them: the item is where it should be, or
    it is not. Here C and A are right, D and B are swapped."""
    assert grade_sequence(ordered(), '["C","A","B","D"]') == Decimal("2.00")


def test_a_reversed_sequence_scores_zero() -> None:
    assert grade_sequence(ordered(), '["B","D","A","C"]') == Decimal("0")


def test_a_short_sequence_earns_what_it_got_right() -> None:
    """A truncated save is a partly answered question, not an error."""
    assert grade_sequence(ordered(), '["C","A"]') == Decimal("2.00")


# --- typed answers -----------------------------------------------------------


def typed(fmt: QuestionFormat, key: dict, points: str = "2") -> Question:
    return structured(QuestionType.SHORT_TEXT, fmt, key, points)


@pytest.mark.parametrize(
    "response",
    ["stroma", "Stroma", "  STROMA  ", "the stroma", "Stroma.", "the  stroma"],
)
def test_a_one_word_answer_survives_case_articles_and_punctuation(response: str) -> None:
    """Everything this normaliser does is listed in one sentence, so an author knows
    exactly which near-misses they still have to enumerate in the key."""
    question = typed(QuestionFormat.ONE_WORD, {"accepted": ["stroma"]})
    assert grade_short_text(question, response) == Decimal("2.00")


def test_a_one_word_answer_is_not_marked_by_guesswork() -> None:
    """No stemming, no edit distance. A grader that decides "stromma" is close enough
    is a grader whose decisions the author cannot predict or explain — and the fix for
    a spelling the key should accept is to add it to the key."""
    question = typed(QuestionFormat.ONE_WORD, {"accepted": ["stroma"]})
    assert grade_short_text(question, "stromma") == Decimal("0")
    assert grade_short_text(question, "chloroplast") == Decimal("0")


def test_every_spelling_in_the_key_earns_the_mark() -> None:
    question = typed(QuestionFormat.ONE_WORD, {"accepted": ["colour", "color"]})
    assert grade_short_text(question, "Color") == Decimal("2.00")


def test_a_numeric_answer_ignores_units_and_separators() -> None:
    question = typed(QuestionFormat.NUMERIC, {"accepted": ["1200"]})
    assert grade_short_text(question, "1,200 kJ") == Decimal("2.00")


def test_a_numeric_answer_respects_its_tolerance() -> None:
    """A derived answer is not a quoted one. Without a tolerance, 3.14 is wrong for
    everybody who did the arithmetic correctly and rounded."""
    question = typed(QuestionFormat.NUMERIC, {"accepted": ["3.14159"], "tolerance": 0.01})
    assert grade_short_text(question, "3.14") == Decimal("2.00")
    assert grade_short_text(question, "3.2") == Decimal("0")


def test_a_numeric_answer_with_no_tolerance_is_exact() -> None:
    question = typed(QuestionFormat.NUMERIC, {"accepted": ["42"]})
    assert grade_short_text(question, "42") == Decimal("2.00")
    assert grade_short_text(question, "43") == Decimal("0")


def test_a_correct_answer_of_zero_is_reachable() -> None:
    """A relative tolerance around zero is zero wide. The absolute floor is what stops
    "the answer is 0" from being unanswerable."""
    question = typed(QuestionFormat.NUMERIC, {"accepted": ["0"], "tolerance": 0.05})
    assert grade_short_text(question, "0") == Decimal("2.00")


def test_a_numeric_question_answered_in_words_still_checks_the_key() -> None:
    question = typed(QuestionFormat.NUMERIC, {"accepted": ["42", "forty-two"]})
    assert grade_short_text(question, "forty-two") == Decimal("2.00")


@pytest.mark.parametrize("response", [None, "", "   "])
def test_a_blank_typed_answer_scores_zero(response) -> None:
    question = typed(QuestionFormat.ONE_WORD, {"accepted": ["stroma"]})
    assert grade_short_text(question, response) == Decimal("0")


def test_a_typed_question_with_no_key_scores_zero_rather_than_full_marks() -> None:
    """The failure mode worth pinning: an empty key must never mean "anything goes"."""
    question = typed(QuestionFormat.ONE_WORD, {})
    assert grade_short_text(question, "stroma") == Decimal("0")


# --- the dispatch ------------------------------------------------------------


def test_every_family_reaches_its_own_marker() -> None:
    """A family missing from the table is graded as subjective by fall-through: an LLM
    call, with no rubric, producing a mark nobody can defend."""
    assert _DETERMINISTIC[QuestionType.MCQ] is grade_mcq
    assert _DETERMINISTIC[QuestionType.MULTI_SELECT] is grade_multi_select
    assert _DETERMINISTIC[QuestionType.SHORT_TEXT] is grade_short_text
    assert _DETERMINISTIC[QuestionType.MATCH] is grade_match
    assert _DETERMINISTIC[QuestionType.SEQUENCE] is grade_sequence
    assert QuestionType.SUBJECTIVE not in _DETERMINISTIC
