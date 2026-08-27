"""The question-format registry. Phase 5b.

Fourteen formats, six grading families, one table that says which is which.

Everything that needs to know about a format reads it here: the prompt that asks for
one, the validator that rejects a malformed one, the grader that marks it, the
serializer that sends it, and the UI copy that names it. Spreading that across four
files is how a format ends up generated in a shape nothing can mark.

Postgres holds the format -> family mapping a second time, as a check constraint in
``20260825141000_question_formats.sql``. That is not duplication for its own sake: a
paper drawn as a match grid and marked by string equality scores zero for everybody
who sat it, so the database refuses the row rather than trusting this file.
``tests/test_formats.py`` pins the two together.

Why fourteen and not two hundred: DECISIONS.md D25.
"""

from dataclasses import dataclass
from decimal import Decimal

from app.db.models.enums import (
    AssessmentType,
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
    # no options at all — a one-word answer is typed, not picked.
    min_options: int = 0
    max_options: int = 0
    # true_false and yes_no do not get to invent their options.
    fixed_options: tuple[str, ...] | None = None
    # match and sequence are two-sided: half the question is the item list.
    needs_prompt_items: bool = False
    # Shortest usable stem. Twenty characters everywhere except a flashcard, whose
    # front is supposed to be a term rather than a sentence -- "Chloroplast" is a
    # perfectly good card and eleven characters long.
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
        '{"format": "...", "stem": "...", '
        '"options": [{"key": "A", "text": "..."}, {"key": "B", "text": "..."}], '
        '"correct_option": "A", "difficulty": "recall", '
        '"source_chunk_id": "..."}'
    ),
    QuestionType.MULTI_SELECT: (
        '{"format": "multi_select", "stem": "...", '
        '"options": [{"key": "A", "text": "..."}, {"key": "B", "text": "..."}, '
        '{"key": "C", "text": "..."}, {"key": "D", "text": "..."}, '
        '{"key": "E", "text": "..."}], '
        '"correct_options": ["A", "C"], "difficulty": "understand", '
        '"source_chunk_id": "..."}'
    ),
    QuestionType.MATCH: (
        '{"format": "match", "stem": "Match each term to its description.", '
        '"prompt_items": [{"key": "1", "text": "left-hand item"}, '
        '{"key": "2", "text": "left-hand item"}], '
        '"options": [{"key": "A", "text": "right-hand item"}, '
        '{"key": "B", "text": "right-hand item"}], '
        '"pairs": {"1": "B", "2": "A"}, "difficulty": "recall", '
        '"source_chunk_id": "..."}'
    ),
    QuestionType.SEQUENCE: (
        '{"format": "sequence", "stem": "Put these steps in the order they occur.", '
        '"options": [{"key": "A", "text": "step"}, {"key": "B", "text": "step"}, '
        '{"key": "C", "text": "step"}], '
        '"order": ["B", "A", "C"], "difficulty": "understand", '
        '"source_chunk_id": "..."}'
    ),
    QuestionType.SHORT_TEXT: (
        '{"format": "...", "stem": "...", '
        '"accepted": ["the answer", "an equally correct spelling"], '
        '"difficulty": "recall", "source_chunk_id": "..."}'
    ),
    QuestionType.SUBJECTIVE: (
        '{"format": "...", "stem": "...", "model_answer": "...", '
        '"rubric": [{"criterion": "...", "points": 2}], "difficulty": "understand", '
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
    QuestionFormat.TRUE_FALSE: FormatSpec(
        format=QuestionFormat.TRUE_FALSE,
        family=QuestionType.MCQ,
        label="True / false",
        blurb="One statement, true or false.",
        instruction=(
            "Write ONE statement that the passage settles either way, then mark it. "
            "Options are exactly True and False, in that order. Do not hedge the "
            "statement with 'sometimes', 'usually' or 'may' — a statement that is "
            "true under one reading and false under another has no answer."
        ),
        default_points=Decimal("1"),
        min_options=2,
        max_options=2,
        fixed_options=("True", "False"),
        reply_tokens=70,
    ),
    QuestionFormat.YES_NO: FormatSpec(
        format=QuestionFormat.YES_NO,
        family=QuestionType.MCQ,
        label="Yes / no",
        blurb="A closed question, answered yes or no.",
        instruction=(
            "Ask a closed question the passage answers outright. Options are exactly "
            "Yes and No, in that order."
        ),
        default_points=Decimal("1"),
        min_options=2,
        max_options=2,
        fixed_options=("Yes", "No"),
        reply_tokens=70,
    ),
    QuestionFormat.FILL_BLANK: FormatSpec(
        format=QuestionFormat.FILL_BLANK,
        family=QuestionType.MCQ,
        label="Fill in the blank",
        blurb="A sentence with a gap, filled from four options.",
        instruction=(
            "Write a sentence from the passage with ONE word or short phrase replaced "
            "by exactly four underscores: ____. The blank must fall on the load-bearing "
            "word, never on 'the' or a number that appears three times. Give four "
            "options that all fit the sentence grammatically — an option that reads "
            "wrong is eliminated without knowing anything."
        ),
        default_points=Decimal("1"),
        min_options=3,
        max_options=5,
    ),
    QuestionFormat.ASSERTION_REASON: FormatSpec(
        format=QuestionFormat.ASSERTION_REASON,
        family=QuestionType.MCQ,
        label="Assertion and reason",
        blurb="Two statements; judge each and the link between them.",
        instruction=(
            "Write the stem as exactly two labelled lines:\n"
            "Assertion (A): <a claim>\nReason (R): <a second claim>\n"
            "Then give exactly these four options, in this order:\n"
            "A. Both A and R are true, and R explains A\n"
            "B. Both A and R are true, but R does not explain A\n"
            "C. A is true but R is false\n"
            "D. A is false but R is true\n"
            "Pick the one the passage supports. This format is worthless unless the "
            "two statements are genuinely separable — do not write R as a restatement "
            "of A."
        ),
        default_points=Decimal("1"),
        min_options=4,
        max_options=4,
        reply_tokens=170,
    ),
    QuestionFormat.SCENARIO: FormatSpec(
        format=QuestionFormat.SCENARIO,
        family=QuestionType.MCQ,
        label="Scenario",
        blurb="A short situation, then the best course of action.",
        instruction=(
            "Open with two or three sentences of concrete situation that is NOT in the "
            "passage, then ask what follows, what to do, or what went wrong. The "
            "passage must supply the principle; the situation must be new, or this is "
            "a recall question wearing a costume. Four options."
        ),
        default_points=Decimal("2"),
        min_options=3,
        max_options=5,
        # A situation of two or three sentences plus four options: four of these
        # truncated at 190-per, two parsed cleanly. 210 caps the ask at three.
        reply_tokens=210,
    ),
    QuestionFormat.FLASHCARD: FormatSpec(
        format=QuestionFormat.FLASHCARD,
        family=QuestionType.MCQ,
        label="Flashcard",
        blurb="A prompt on the front, the answer picked from four backs.",
        instruction=(
            "The stem is the FRONT of a card: a term, a name, a formula, a date — a "
            "few words, not a sentence. The correct option is its back: the definition "
            "or value. The other three are backs that belong to neighbouring ideas in "
            "the same passage, so the card tests the distinction rather than "
            "recognition of the odd one out."
        ),
        default_points=Decimal("1"),
        min_options=3,
        max_options=5,
        min_stem=3,
    ),
    # --------------------------------------------------------- several keys
    QuestionFormat.MULTI_SELECT: FormatSpec(
        format=QuestionFormat.MULTI_SELECT,
        family=QuestionType.MULTI_SELECT,
        label="Select all that apply",
        blurb="Five options, more than one right.",
        instruction=(
            "Five options, of which two or three are correct — never one, never all "
            "five. Say 'Select all that apply.' at the end of the stem. Each correct "
            "option must be independently supported by the passage; each wrong one "
            "must be wrong on its own terms rather than merely unmentioned."
        ),
        default_points=Decimal("2"),
        min_options=4,
        max_options=6,
        # Measured three times upward: five multi-selects truncated at 150-per, four
        # truncated at 200-per. Five options of sentence length cost what they cost,
        # and 220 caps the ask at three, which leaves the headroom the model uses.
        reply_tokens=220,
    ),
    # ------------------------------------------------------------ two-sided
    QuestionFormat.MATCH: FormatSpec(
        format=QuestionFormat.MATCH,
        family=QuestionType.MATCH,
        label="Match the following",
        blurb="Pair each item on the left with one on the right.",
        instruction=(
            "THREE or four items on the left (numbered 1, 2, 3) and the same number on "
            "the right (lettered A, B, C), one correct pairing each. Three is the "
            "minimum and a grid with two is not a question — if the passage only "
            "supports two pairings, return no question at all rather than a short "
            "grid. Put the SHORT side on the left — terms, names, symbols — and the "
            "long side on the right. Every right-hand item must be a plausible partner "
            "for more than one left-hand item, or the grid solves itself by length."
        ),
        default_points=Decimal("4"),
        min_options=3,
        max_options=6,
        needs_prompt_items=True,
        reply_tokens=170,
    ),
    QuestionFormat.SEQUENCE: FormatSpec(
        format=QuestionFormat.SEQUENCE,
        family=QuestionType.SEQUENCE,
        label="Put in order",
        blurb="Arrange steps or events into the right sequence.",
        instruction=(
            "THREE to five items the passage places in a definite order — steps of a "
            "process, events on a timeline, stages of an argument. Give them lettered "
            "keys in SCRAMBLED order and put the correct order in `order`. Only use "
            "this when the passage really does fix the order; an order the reader has "
            "to guess at is three marks of noise."
        ),
        default_points=Decimal("3"),
        min_options=3,
        max_options=6,
        reply_tokens=130,
    ),
    # --------------------------------------------------- typed, marked exactly
    QuestionFormat.ONE_WORD: FormatSpec(
        format=QuestionFormat.ONE_WORD,
        family=QuestionType.SHORT_TEXT,
        label="One word",
        blurb="Typed answer, one or two words, marked exactly.",
        instruction=(
            "`stem` is a QUESTION and `accepted` is its answer. Do not put the answer "
            "in the stem: \"Missile Man\" is not a question, \"What was he "
            "popularly called?\" is. The stem is a full sentence ending in a question "
            "mark; the answer is the one or two words that answer it — a term, a name, "
            "a unit.\n"
            "This is marked by string comparison, so `accepted` must list every answer "
            "you would give the mark for: the singular and the plural, the abbreviation "
            "and the expansion, the British and American spellings. If you cannot "
            "enumerate them, the question belongs in another format."
        ),
        default_points=Decimal("1"),
        reply_tokens=60,
    ),
    QuestionFormat.NUMERIC: FormatSpec(
        format=QuestionFormat.NUMERIC,
        family=QuestionType.SHORT_TEXT,
        label="Numeric",
        blurb="Typed number, marked with a tolerance.",
        instruction=(
            "Ask for a number the passage states or that follows from it in one or two "
            "steps. Put the bare number in `accepted` — no units, no thousands "
            "separators, no words — and name the unit in the stem instead. Add "
            "`tolerance` as a decimal fraction (0.01 for one percent) when the answer "
            "is derived rather than quoted."
        ),
        default_points=Decimal("2"),
        reply_tokens=60,
    ),
    # ------------------------------------------------- typed, marked by rubric
    QuestionFormat.SHORT_ANSWER: FormatSpec(
        format=QuestionFormat.SHORT_ANSWER,
        family=QuestionType.SUBJECTIVE,
        label="Short answer",
        blurb="A few sentences, marked against a rubric.",
        instruction=(
            "Answerable in two to four sentences. The rubric has two or three criteria "
            "totalling the marks, and each criterion names something checkable — 'says "
            "that X causes Y', not 'shows understanding'."
        ),
        default_points=Decimal("3"),
        reply_tokens=200,
    ),
    QuestionFormat.LONG_ANSWER: FormatSpec(
        format=QuestionFormat.LONG_ANSWER,
        family=QuestionType.SUBJECTIVE,
        label="Long answer",
        blurb="An extended answer, marked against a rubric.",
        instruction=(
            "Worth several paragraphs: compare, evaluate, argue, or explain at length. "
            "The rubric has four or five criteria totalling the marks, and it must "
            "reward a well-argued answer that reaches a different conclusion from the "
            "model answer — otherwise it marks agreement rather than reasoning."
        ),
        default_points=Decimal("6"),
        reply_tokens=320,
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


# ============================================================== the auto mix
#
# What "I skipped the picker" means. Somebody drafting a quiz from a novel should not
# have to know that `assertion_reason` exists, and refusing to write the paper until
# they choose would be a worse product than picking sensibly on their behalf.
#
# Keyed on the coarse `type` the author already chose, so skipping the format picker
# still respects "make it written" or "make it multiple choice".

# **Kept deliberately small, and wall clock is why.** Generation makes at least one
# LLM call per format here, and on CPU-only Ollama a call is one to two minutes — so
# every entry in these tuples is a minute of spinner before the first backfill can
# start. Auto is also the path that must work on the WEAKEST provider: a real run
# showed a 3B model failing match, sequence and one_word outright, each failure a
# paid call that produced nothing. So auto sticks to the formats that model writes
# reliably, and the two-sided and typed formats stay reachable the honest way — an
# author who wants a match grid ticks it, and accepts that it may come back short.
AUTO_MIX: dict[AssessmentType, tuple[QuestionFormat, ...]] = {
    AssessmentType.MCQ: (
        QuestionFormat.MCQ,
        QuestionFormat.TRUE_FALSE,
        QuestionFormat.FILL_BLANK,
        QuestionFormat.SCENARIO,
    ),
    AssessmentType.SUBJECTIVE: (
        QuestionFormat.SHORT_ANSWER,
        QuestionFormat.LONG_ANSWER,
    ),
    AssessmentType.MIXED: (
        QuestionFormat.MCQ,
        QuestionFormat.TRUE_FALSE,
        QuestionFormat.FILL_BLANK,
        QuestionFormat.SHORT_ANSWER,
    ),
}

# The spread a paper gets when the author does not name cognitive levels either. The
# bottom three rungs: a paper that is all `create` is a coursework brief, not a test,
# and a paper that is all `recall` is a vocabulary quiz.
AUTO_LEVELS: tuple[Difficulty, ...] = (
    Difficulty.RECALL,
    Difficulty.UNDERSTAND,
    Difficulty.APPLY,
)


def resolve_formats(
    chosen: list[QuestionFormat] | None, *, assessment_type: AssessmentType
) -> list[QuestionFormat]:
    """What to actually generate. An empty choice is *auto*, not an error."""
    if chosen:
        return list(dict.fromkeys(chosen))
    return list(AUTO_MIX[assessment_type])


def resolve_levels(chosen: list[Difficulty] | None) -> list[Difficulty]:
    if chosen:
        return list(dict.fromkeys(chosen))
    return list(AUTO_LEVELS)


def derive_type(formats: list[QuestionFormat]) -> AssessmentType:
    """The coarse label, derived from the formats rather than trusted from the client.

    An author who ticked `long_answer` and nothing else has written a subjective
    paper whatever the dropdown said, and the share-link preview tells a prospective
    sitter which it is before they commit.
    """
    families = {FAMILY_OF[fmt] for fmt in formats}
    if families == {QuestionType.SUBJECTIVE}:
        return AssessmentType.SUBJECTIVE
    if QuestionType.SUBJECTIVE not in families:
        return AssessmentType.MCQ
    return AssessmentType.MIXED


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
    formats: list[QuestionFormat], levels: list[Difficulty], *, count: int
) -> list[tuple[QuestionFormat, Difficulty]]:
    """Deal `count` (format, level) slots round-robin over both lists.

    Round-robin rather than random so a five-question paper from three formats gets
    all three, and so the same request twice produces the same shape of paper. The
    model still decides what each question says; this only decides what is asked for.
    """
    if not formats or count <= 0:
        return []
    levels = levels or list(AUTO_LEVELS)
    return [(formats[i % len(formats)], levels[i % len(levels)]) for i in range(count)]
