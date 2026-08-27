"""The question-format taxonomy: the registry, the shape builder, and the two copies
of the format -> family mapping that must not drift.

Why a whole file for a lookup table. Fourteen formats share six marking paths, and
every wrong answer here is silent: a format with no grader is marked as if it were a
written answer and sent to an LLM with no rubric; a format whose family disagrees with
Postgres is a row the database refuses at the very end of a paid-for generation run;
a format whose family disagrees with the *grader* is a paper drawn as one thing and
marked as another, which scores zero for everybody who sat it and looks like nothing
at all went wrong.

None of those fail loudly in a browser. They fail as a mark somebody disputes.
"""

import re

import pytest

from app.core.errors import ValidationFailed
from app.db.models.enums import AssessmentType, Difficulty, QuestionFormat, QuestionType
from app.rag import formats
from app.services.assessments import build_question_fields
from app.services.grading import _DETERMINISTIC
from tests.migrations import migration_sql

# --- the registry is complete ------------------------------------------------


def test_every_format_has_a_spec() -> None:
    """A format in the enum with no spec cannot be prompted for or validated, and the
    lookup that misses it returns None at generation time — a whole format silently
    producing nothing."""
    assert set(formats.SPECS) == set(QuestionFormat)


def test_every_spec_declares_the_format_it_is_keyed_by() -> None:
    """A copy-pasted spec block that kept the previous format's `format=` field maps
    two keys onto one format, and one of them prompts for the wrong thing."""
    for key, spec in formats.SPECS.items():
        assert spec.format is key


def test_every_family_can_be_marked() -> None:
    """The failure this exists for: a family that is neither in the deterministic table
    nor the subjective path is graded as subjective by fall-through, which means an LLM
    call with no rubric and a mark nobody can defend."""
    reachable = set(_DETERMINISTIC) | {QuestionType.SUBJECTIVE}
    assert reachable == set(QuestionType)


def test_every_family_is_actually_used_by_a_format() -> None:
    """The other direction: a marking path no format reaches is dead code that will
    rot, and the next person to add a format will trust it."""
    assert {spec.family for spec in formats.SPECS.values()} == set(QuestionType)


def test_every_family_has_a_json_shape_to_ask_for() -> None:
    assert set(formats.FAMILY_SHAPE) == set(QuestionType)


@pytest.mark.parametrize(
    "fmt", list(QuestionFormat), ids=lambda fmt: fmt.value
)
def test_every_format_carries_ui_copy_and_an_instruction(fmt: QuestionFormat) -> None:
    """The label and blurb live beside the family on purpose: a format cannot be added
    without somebody deciding what to call it in front of an author."""
    spec = formats.SPECS[fmt]
    assert spec.label.strip() and spec.blurb.strip()
    assert len(spec.instruction) > 40, "an instruction this short will not steer a model"
    assert spec.default_points > 0


def test_no_format_example_stem_refers_to_the_passage() -> None:
    """The JSON shapes are handed to the model as examples, and a model copies an
    example. An example stem saying "the passage above" teaches it to write stems the
    validator then rejects — the paper comes out short and the logs blame the model.

    Only the `stem` values: the `rationale` field is *supposed* to cite the passage,
    since it is written for the author reviewing the draft and never shown to a sitter."""
    self_reference = re.compile(r"the (passage|text|excerpt)", re.IGNORECASE)
    checked = 0
    for family, shape in formats.FAMILY_SHAPE.items():
        for stem in re.findall(r'"stem":\s*"([^"]*)"', shape):
            checked += 1
            assert not self_reference.search(stem), f"{family.value} example is self-referring"
    assert checked >= len(formats.FAMILY_SHAPE), "the stem parser found nothing to check"


# --- the mapping, in both places ---------------------------------------------


def _mapping_in_the_migrations() -> dict[str, str]:
    """The format -> family CASE from `questions_format_matches_family`."""
    sql = "\n".join(migration_sql())
    start = sql.index("questions_format_matches_family")
    body = sql[start : sql.index("else false", start)]
    return {
        fmt: family
        for fmt, family in re.findall(
            r"when\s+'(\w+)'\s+then\s+type\s+=\s+'(\w+)'", body, re.IGNORECASE
        )
    }


def test_postgres_and_python_agree_about_every_format() -> None:
    """Two copies of one mapping, and they are both load-bearing: Python decides what
    to build, Postgres refuses to store a row where the two disagree. If they drift,
    generation spends every LLM call it was going to spend and then fails on the
    INSERT — the most expensive way possible to find out."""
    in_sql = _mapping_in_the_migrations()
    in_python = {fmt.value: family.value for fmt, family in formats.FAMILY_OF.items()}
    assert in_sql == in_python


def test_the_migration_parser_found_something() -> None:
    """A regex that matched nothing would make the test above pass on two empty dicts."""
    assert len(_mapping_in_the_migrations()) == len(QuestionFormat)


# --- skipping the picker is a choice -----------------------------------------


@pytest.mark.parametrize("kind", list(AssessmentType), ids=lambda k: k.value)
def test_an_empty_choice_resolves_to_a_usable_mix(kind: AssessmentType) -> None:
    """Somebody drafting a quiz from a novel should not have to know that
    `assertion_reason` exists before they can press the button."""
    resolved = formats.resolve_formats([], assessment_type=kind)
    assert resolved, "auto must never resolve to nothing"
    assert all(fmt in formats.SPECS for fmt in resolved)


def test_the_auto_mix_stays_small_enough_to_feed() -> None:
    """Every format in an auto mix is at least one LLM call, and on CPU-only Ollama a
    call is one to two minutes — so each entry here is a minute of spinner before the
    first backfill can start. Auto is the default path and must be fast on the WEAKEST
    provider; a seven-format fan-out is how a ten-question paper once took over twenty
    minutes and came back with one question. Widening a mix is a wall-clock decision,
    not a taste one: raise `assessment_max_llm_calls` in the same change or the
    backfill loses the calls this adds."""
    for kind, mix in formats.AUTO_MIX.items():
        assert len(mix) <= 4, (
            f"AUTO_MIX[{kind.value}] has {len(mix)} formats — that is {len(mix)} LLM "
            "calls before backfill even starts"
        )


def test_a_written_paper_never_auto_picks_a_multiple_choice_format() -> None:
    """The coarse choice the author *did* make still has to be honoured."""
    resolved = formats.resolve_formats([], assessment_type=AssessmentType.SUBJECTIVE)
    assert all(formats.FAMILY_OF[fmt] is QuestionType.SUBJECTIVE for fmt in resolved) or all(
        formats.FAMILY_OF[fmt] in (QuestionType.SUBJECTIVE, QuestionType.SHORT_TEXT)
        for fmt in resolved
    )


def test_an_explicit_choice_is_taken_verbatim_and_deduplicated() -> None:
    chosen = [QuestionFormat.MATCH, QuestionFormat.MCQ, QuestionFormat.MATCH]
    assert formats.resolve_formats(chosen, assessment_type=AssessmentType.MIXED) == [
        QuestionFormat.MATCH,
        QuestionFormat.MCQ,
    ]


def test_the_coarse_type_is_derived_from_the_formats_not_the_dropdown() -> None:
    """An author who ticked only `long_answer` has written a written paper whatever
    the dropdown said, and the share link tells a sitter which before they commit."""
    assert formats.derive_type([QuestionFormat.LONG_ANSWER]) is AssessmentType.SUBJECTIVE
    assert formats.derive_type([QuestionFormat.MCQ, QuestionFormat.MATCH]) is AssessmentType.MCQ
    assert (
        formats.derive_type([QuestionFormat.MCQ, QuestionFormat.SHORT_ANSWER])
        is AssessmentType.MIXED
    )


def test_the_mix_reaches_every_format_before_it_repeats_one() -> None:
    """Round-robin, not random: a five-question paper from three formats gets all
    three. Sampling would give you five of whichever came up first often enough to be
    a complaint and rarely enough to look like bad luck."""
    chosen = [QuestionFormat.MCQ, QuestionFormat.TRUE_FALSE, QuestionFormat.ONE_WORD]
    plan = formats.plan_mix(chosen, list(formats.AUTO_LEVELS), count=5)
    assert len(plan) == 5
    assert {fmt for fmt, _ in plan} == set(chosen)


def test_the_mix_is_stable_across_two_identical_requests() -> None:
    """Same request, same shape of paper. A generated paper is hard enough to reason
    about without the plan changing underneath it."""
    chosen = [QuestionFormat.MCQ, QuestionFormat.MATCH]
    levels = [Difficulty.RECALL, Difficulty.EVALUATE]
    assert formats.plan_mix(chosen, levels, count=7) == formats.plan_mix(
        chosen, levels, count=7
    )


# --- the shape builder --------------------------------------------------------
#
# One builder for the model's output AND the author's own typing. These are the checks
# an author would otherwise have to make by hand, twenty times, on a paper they did
# not write.


def build(fmt: QuestionFormat, **kwargs):
    return build_question_fields(fmt, **kwargs)


def test_the_family_is_set_from_the_format_and_never_from_the_caller() -> None:
    fields = build(
        QuestionFormat.TRUE_FALSE,
        stem="The Calvin cycle occurs in the stroma of the chloroplast.",
        options=[{"key": "A", "text": "True"}, {"key": "B", "text": "False"}],
        correct_option="A",
    )
    assert fields["type"] is QuestionType.MCQ
    assert fields["format"] is QuestionFormat.TRUE_FALSE


def test_a_true_false_written_the_wrong_way_round_keeps_its_answer() -> None:
    """A model that lists False first and marks it "A" is right about the answer and
    wrong about the ordering. Re-keying without re-reading the text it chose would
    invert every true/false question on the paper — the worst kind of bug, because the
    paper still looks completely normal."""
    fields = build(
        QuestionFormat.TRUE_FALSE,
        stem="The Calvin cycle occurs in the stroma of the chloroplast.",
        options=[{"key": "A", "text": "False"}, {"key": "B", "text": "True"}],
        correct_option="A",
    )
    assert fields["options"] == [
        {"key": "A", "text": "True"},
        {"key": "B", "text": "False"},
    ]
    assert fields["correct_option"] == "B"


def test_option_keys_are_ours_and_the_answer_follows_them() -> None:
    """Models key options "a"/"b", "1"/"2", or repeat one. Renumbering without
    remapping the answer in the same pass marks the paper against the wrong letters."""
    fields = build(
        QuestionFormat.MCQ,
        stem="Where in the chloroplast does the Calvin cycle occur?",
        options=[
            {"key": "1", "text": "Thylakoid"},
            {"key": "2", "text": "Stroma"},
            {"key": "3", "text": "Nucleus"},
        ],
        correct_option="2",
    )
    assert [o["key"] for o in fields["options"]] == ["A", "B", "C"]
    assert fields["correct_option"] == "B"


def test_a_multi_select_with_one_correct_answer_is_refused() -> None:
    """It is an ordinary multiple choice, and calling it "select all that apply" tells
    the sitter to look for a second answer that is not there."""
    with pytest.raises(ValidationFailed):
        build(
            QuestionFormat.MULTI_SELECT,
            stem="Which of these occur inside the chloroplast? Select all that apply.",
            options=[{"key": k, "text": k * 4} for k in "ABCD"],
            correct_options=["A"],
        )


def test_a_multi_select_where_everything_is_correct_is_refused() -> None:
    with pytest.raises(ValidationFailed):
        build(
            QuestionFormat.MULTI_SELECT,
            stem="Which of these occur inside the chloroplast? Select all that apply.",
            options=[{"key": k, "text": k * 4} for k in "ABCD"],
            correct_options=list("ABCD"),
        )


def test_a_multi_select_is_worth_one_mark_per_correct_option() -> None:
    """So partial credit divides evenly, and "two of the three" is worth two."""
    fields = build(
        QuestionFormat.MULTI_SELECT,
        stem="Which of these occur inside the chloroplast? Select all that apply.",
        options=[{"key": k, "text": k * 4} for k in "ABCDE"],
        correct_options=["A", "C", "D"],
    )
    assert fields["points"] == 3
    assert fields["answer_key"] == {"correct_options": ["A", "C", "D"]}


def test_a_match_grid_numbers_its_left_column_and_letters_its_right() -> None:
    """So a stored answer of {"1": "B"} can never be read the other way round, and the
    two sides of the grid cannot be confused for one another on screen either."""
    fields = build(
        QuestionFormat.MATCH,
        stem="Match each structure to what happens there.",
        prompt_items=[{"key": "i", "text": "Stroma"}, {"key": "ii", "text": "Thylakoid"},
                      {"key": "iii", "text": "Nucleus"}],
        options=[{"key": "a", "text": "Calvin cycle"}, {"key": "b", "text": "Light reactions"},
                 {"key": "c", "text": "Transcription"}],
        pairs={"i": "a", "ii": "b", "iii": "c"},
    )
    assert [item["key"] for item in fields["prompt_items"]] == ["1", "2", "3"]
    assert [option["key"] for option in fields["options"]] == ["A", "B", "C"]
    assert fields["answer_key"] == {"pairs": {"1": "A", "2": "B", "3": "C"}}
    # One mark per pair, so partial credit divides evenly.
    assert fields["points"] == 3


def test_a_match_grid_needs_every_item_matched() -> None:
    with pytest.raises(ValidationFailed):
        build(
            QuestionFormat.MATCH,
            stem="Match each structure to what happens there.",
            prompt_items=[{"key": str(i), "text": f"item {i}"} for i in range(1, 4)],
            options=[{"key": k, "text": f"answer {k}"} for k in "ABC"],
            pairs={"1": "A", "2": "B"},
        )


def test_a_square_match_grid_refuses_a_reused_answer() -> None:
    """Equal columns means one-to-one. Two lefts sharing a right leaves a right-hand
    item that pairs with nothing, which the sitter cannot resolve."""
    with pytest.raises(ValidationFailed):
        build(
            QuestionFormat.MATCH,
            stem="Match each structure to what happens there.",
            prompt_items=[{"key": str(i), "text": f"item {i}"} for i in range(1, 4)],
            options=[{"key": k, "text": f"answer {k}"} for k in "ABC"],
            pairs={"1": "A", "2": "A", "3": "B"},
        )


def test_a_sequence_must_list_every_item_exactly_once() -> None:
    items = [{"key": k, "text": f"step {k}"} for k in "ABCD"]
    with pytest.raises(ValidationFailed):
        build(
            QuestionFormat.SEQUENCE,
            stem="Put these steps in the order they occur.",
            options=items,
            order=["A", "B", "C"],
        )
    with pytest.raises(ValidationFailed):
        build(
            QuestionFormat.SEQUENCE,
            stem="Put these steps in the order they occur.",
            options=items,
            order=["A", "A", "B", "C"],
        )


def test_a_fill_in_the_blank_without_a_blank_is_refused() -> None:
    """It reads as an ordinary multiple choice with a missing word, and the sitter
    cannot tell which word is missing."""
    with pytest.raises(ValidationFailed):
        build(
            QuestionFormat.FILL_BLANK,
            stem="The Calvin cycle occurs in the of the chloroplast.",
            options=[{"key": k, "text": k * 4} for k in "ABCD"],
            correct_option="A",
        )


def test_a_one_word_question_needs_an_enumerated_key() -> None:
    """It is marked by string comparison. Without a key there is nothing to compare
    against, and every answer scores zero."""
    with pytest.raises(ValidationFailed):
        build(
            QuestionFormat.ONE_WORD,
            stem="Name the structure in which the Calvin cycle occurs.",
            accepted=[],
        )


def test_a_one_word_question_refuses_a_sentence_as_its_answer() -> None:
    """A key that is a sentence cannot be matched by string comparison, so the question
    is unanswerable however well somebody understood it. Ask it as a short answer."""
    with pytest.raises(ValidationFailed):
        build(
            QuestionFormat.ONE_WORD,
            stem="Name the structure in which the Calvin cycle occurs.",
            accepted=["it happens in the stroma of the chloroplast organelle"],
        )


def test_a_rubric_decides_what_a_written_question_is_worth() -> None:
    fields = build(
        QuestionFormat.SHORT_ANSWER,
        stem="Explain why the Calvin cycle does not require light directly.",
        model_answer="It uses ATP and NADPH produced by the light reactions.",
        rubric=[{"criterion": "names ATP and NADPH", "points": 2},
                {"criterion": "says they come from the light reactions", "points": 1}],
    )
    assert fields["points"] == 3


def test_a_rubric_that_disagrees_with_an_explicit_mark_is_refused() -> None:
    """A mark total that disagrees with its own breakdown is the one number somebody
    will check by hand."""
    from decimal import Decimal

    with pytest.raises(ValidationFailed):
        build(
            QuestionFormat.SHORT_ANSWER,
            stem="Explain why the Calvin cycle does not require light directly.",
            model_answer="It uses ATP and NADPH from the light reactions.",
            rubric=[{"criterion": "names ATP and NADPH", "points": 2}],
            points=Decimal("5"),
        )


def test_changing_a_question_format_clears_the_answer_it_no_longer_uses() -> None:
    """Every column a format does not use comes back as None rather than being left
    alone, so a match grid edited into a true/false does not keep a stale `answer_key`
    that outranks its new answer."""
    fields = build(
        QuestionFormat.TRUE_FALSE,
        stem="The Calvin cycle occurs in the stroma of the chloroplast.",
        options=[{"key": "A", "text": "True"}, {"key": "B", "text": "False"}],
        correct_option="A",
    )
    assert fields["answer_key"] is None
    assert fields["prompt_items"] is None
    assert fields["model_answer"] is None
    assert fields["rubric"] is None


# --- the migration itself -----------------------------------------------------
#
# Two checks that only the SQL can answer, and both of them are about failures that
# happen once, on somebody else's machine, at `supabase migration up`.


def test_the_migration_backfills_format_before_it_constrains_it() -> None:
    """A `format` column defaulting to 'mcq' lands on every existing row — including
    every subjective question ever written, whose family is `subjective`. Adding the
    family constraint before the backfill validates those rows, fails, and rolls back
    an ALTER TABLE on data that was perfectly valid a moment earlier.

    Order is the whole fix, so order is what this pins."""
    sql = "\n".join(migration_sql())
    backfill = sql.index("update public.questions set format")
    constraint = sql.index("questions_format_matches_family")
    assert backfill < constraint, (
        "the family constraint is added before existing rows are backfilled — "
        "`supabase migration up` will fail on any database that already has a "
        "subjective question in it"
    )


def _last_view_body(name: str) -> str:
    """The final definition of a view, after every drop-and-recreate."""
    sql = "\n".join(migration_sql())
    start = sql.rindex(f"create view public.{name}")
    return sql[start : sql.index(";", start)].lower()


@pytest.mark.parametrize(
    "column", ["correct_option", "answer_key", "model_answer", "rubric"]
)
def test_the_sitting_view_never_selects_an_answer(column: str) -> None:
    """CLAUDE.md invariant 2, checked against the SQL rather than trusted.

    A sitter has no policy on `questions` at all, because row-level security cannot
    hide a column. The enforcement is that the answer columns are ABSENT from
    `public.question_sit` — so the thing that must be true is a fact about the view's
    select list, and it is exactly the fact a careless `select q.*` in a future
    refactor would break.

    `answer_key` is on this list because Phase 5b added it, and a new answer column
    that nobody remembered to keep out of this view is the whole failure mode."""
    assert column not in _last_view_body("question_sit")


def test_the_sitting_view_still_selects_the_question() -> None:
    """The test above passes trivially if the parser found the wrong block, or if
    somebody deleted the view. This is the other half."""
    body = _last_view_body("question_sit")
    for column in ("stem", "options", "format", "prompt_items", "points"):
        assert column in body


def test_the_sitting_model_maps_no_answer_column() -> None:
    """And the same rule one layer up. The view is the enforcement, but a mapper that
    declared `answer_key` would fail at query time with a confusing error about a
    column that does not exist — after somebody had already written code assuming a
    sitter could read it."""
    from sqlalchemy import inspect

    from app.db.models import QuestionSit

    mapped = {column.key for column in inspect(QuestionSit).columns}
    assert not mapped & {"correct_option", "answer_key", "model_answer", "rubric"}


def test_a_choice_list_with_two_of_the_same_key_is_refused() -> None:
    """Every answer the model gives points at these keys, and we are about to renumber
    them. Two options both keyed "A" means the pointer resolves to the first of them —
    so a question with four perfectly good distinct options gets marked against the
    wrong one, and nothing on screen looks wrong. Refuse rather than repair."""
    with pytest.raises(ValidationFailed):
        build(
            QuestionFormat.MCQ,
            stem="Where in the chloroplast does the Calvin cycle occur?",
            options=[
                {"key": "A", "text": "Stroma"},
                {"key": "A", "text": "Thylakoid"},
                {"key": "C", "text": "Nucleus"},
            ],
            correct_option="A",
        )


def test_a_choice_list_with_no_keys_at_all_is_numbered_by_position() -> None:
    """The other half, and the reason the check above looks only at non-blank keys:
    with no keys given, position IS the key and renumbering is exactly right."""
    fields = build(
        QuestionFormat.MCQ,
        stem="Where in the chloroplast does the Calvin cycle occur?",
        options=[{"text": "Thylakoid"}, {"text": "Stroma"}, {"text": "Nucleus"}],
        correct_option="B",
    )
    assert fields["correct_option"] == "B"
    assert fields["options"][1]["text"] == "Stroma"


# --- the whole path, per format ------------------------------------------------
#
# parse -> validate -> row, for a well-formed reply in each of the fourteen. Each
# stage is covered above; this is the one that catches them disagreeing. A format
# whose prompt asks for a field the validator does not read, or whose validator
# accepts something the row builder cannot store, passes every test above and fails
# once — in a worker, after the LLM call has been paid for.

PASSAGE = (
    "The Calvin cycle occurs in the stroma of the chloroplast, using ATP and NADPH "
    "produced by the light-dependent reactions in the thylakoid membrane. Carbon "
    "fixation happens first, then reduction, then regeneration of ribulose "
    "bisphosphate. A chloroplast contains roughly 3000 thylakoid discs."
)

MODEL_REPLIES: dict[QuestionFormat, dict] = {
    QuestionFormat.MCQ: {
        "stem": "Where in the chloroplast does the Calvin cycle take place?",
        "options": [{"key": k, "text": t} for k, t in zip(
            "ABCD", ["Stroma", "Thylakoid membrane", "Nucleus", "Ribosome"], strict=True)],
        "correct_option": "A",
    },
    QuestionFormat.TRUE_FALSE: {
        "stem": "The Calvin cycle takes place in the stroma of the chloroplast.",
        "options": [{"key": "A", "text": "True"}, {"key": "B", "text": "False"}],
        "correct_option": "A",
    },
    QuestionFormat.YES_NO: {
        "stem": "Does the Calvin cycle depend on NADPH from the light reactions?",
        "options": [{"key": "A", "text": "Yes"}, {"key": "B", "text": "No"}],
        "correct_option": "A",
    },
    QuestionFormat.FILL_BLANK: {
        "stem": "The Calvin cycle occurs in the ____ of the chloroplast.",
        "options": [{"key": k, "text": t} for k, t in
                    zip("ABCD", ["stroma", "thylakoid", "nucleus", "cytosol"], strict=True)],
        "correct_option": "A",
    },
    QuestionFormat.ASSERTION_REASON: {
        "stem": "Assertion (A): The Calvin cycle needs no light.\n"
                "Reason (R): It consumes ATP made by the light reactions.",
        "options": [{"key": k, "text": t} for k, t in zip("ABCD", [
            "Both A and R are true, and R explains A",
            "Both A and R are true, but R does not explain A",
            "A is true but R is false",
            "A is false but R is true"], strict=True)],
        "correct_option": "A",
    },
    QuestionFormat.SCENARIO: {
        "stem": "A grower keeps a crop in total darkness but supplies it with ATP and "
                "NADPH directly. Which process can still run in the chloroplast?",
        "options": [{"key": k, "text": t} for k, t in
                    zip("ABCD", ["The Calvin cycle", "Photolysis", "Photosystem II",
                                 "Electron transport"], strict=True)],
        "correct_option": "A",
    },
    QuestionFormat.FLASHCARD: {
        "stem": "Stroma",
        "options": [{"key": k, "text": t} for k, t in zip("ABCD", [
            "Where the Calvin cycle runs",
            "Where the light reactions run",
            "The chloroplast's outer boundary",
            "The site of transcription"], strict=True)],
        "correct_option": "A",
    },
    QuestionFormat.MULTI_SELECT: {
        "stem": "Which of these are stages of the Calvin cycle? Select all that apply.",
        "options": [{"key": k, "text": t} for k, t in zip("ABCDE", [
            "Carbon fixation", "Reduction", "Regeneration", "Photolysis",
            "Electron transport"], strict=True)],
        "correct_options": ["A", "B", "C"],
    },
    QuestionFormat.MATCH: {
        "stem": "Match each chloroplast structure to what happens there.",
        "prompt_items": [{"key": "1", "text": "Stroma"},
                         {"key": "2", "text": "Thylakoid membrane"},
                         {"key": "3", "text": "Ribulose bisphosphate"}],
        "options": [{"key": "A", "text": "The Calvin cycle"},
                    {"key": "B", "text": "The light-dependent reactions"},
                    {"key": "C", "text": "Regenerated at the end of the cycle"}],
        "pairs": {"1": "A", "2": "B", "3": "C"},
    },
    QuestionFormat.SEQUENCE: {
        "stem": "Put these stages of the Calvin cycle into the order they occur.",
        "options": [{"key": "A", "text": "Reduction"},
                    {"key": "B", "text": "Regeneration"},
                    {"key": "C", "text": "Carbon fixation"},
                    {"key": "D", "text": "ATP is consumed"}],
        "order": ["C", "A", "D", "B"],
    },
    QuestionFormat.ONE_WORD: {
        "stem": "Name the region of the chloroplast in which the Calvin cycle occurs.",
        "accepted": ["stroma"],
    },
    QuestionFormat.NUMERIC: {
        "stem": "Roughly how many thylakoid discs does one chloroplast contain?",
        "accepted": ["3000"],
        "tolerance": 0.1,
    },
    QuestionFormat.SHORT_ANSWER: {
        "stem": "Explain why the Calvin cycle is not called a dark reaction any more.",
        "model_answer": "It depends on ATP and NADPH from the light reactions, so it "
                        "cannot run indefinitely in darkness.",
        "rubric": [{"criterion": "names ATP and NADPH", "points": 2},
                   {"criterion": "links them to the light reactions", "points": 1}],
    },
    QuestionFormat.LONG_ANSWER: {
        "stem": "Evaluate the claim that the Calvin cycle is independent of light.",
        "model_answer": "It is light-independent only in the sense that photons are "
                        "not absorbed by it; it consumes the products of the light "
                        "reactions and stops without them.",
        "rubric": [{"criterion": "states the narrow sense in which it is true", "points": 3},
                   {"criterion": "states the dependency on ATP and NADPH", "points": 3}],
    },
}


def test_the_reply_fixtures_cover_every_format() -> None:
    """Otherwise a format added without a fixture is silently untested end to end."""
    assert set(MODEL_REPLIES) == set(QuestionFormat)


@pytest.mark.parametrize("fmt", list(QuestionFormat), ids=lambda fmt: fmt.value)
def test_a_well_formed_reply_survives_parse_validate_and_build(fmt: QuestionFormat) -> None:
    import json
    from uuid import uuid4

    from app.services.assessments import _to_question, parse_generated, validate_generated

    chunk_id = "chunk-1"
    payload = {"questions": [
        {"format": fmt.value, "source_chunk_id": chunk_id, "difficulty": "understand",
         "rationale": "…", **MODEL_REPLIES[fmt]}
    ]}
    # Through a markdown fence, because that is how a small local model actually
    # replies however firmly it was told not to.
    items = parse_generated(f"```json\n{json.dumps(payload)}\n```")
    assert len(items) == 1

    ok, reason = validate_generated(
        items[0],
        allowed_chunk_ids={chunk_id},
        chunk_text={chunk_id: PASSAGE},
        expected_format=fmt,
    )
    assert ok, f"{fmt.value} rejected: {reason}"

    question = _to_question(uuid4(), 0, items[0], fmt)
    assert question is not None
    assert question.format is fmt
    assert question.type is formats.FAMILY_OF[fmt]
    assert question.points > 0
    assert question.source_chunk_ids == [chunk_id]
    # Whatever the family, there is something to mark against — the check `publish`
    # makes before it will freeze a paper.
    assert question.correct_option or question.answer_key or question.model_answer


@pytest.mark.parametrize("fmt", list(QuestionFormat), ids=lambda fmt: fmt.value)
def test_a_reply_in_the_wrong_format_is_rejected(fmt: QuestionFormat) -> None:
    """The batch asked for one format. A model that answers with another has ignored
    the instruction, and taking it anyway silently rewrites the paper the author
    asked for."""
    from app.schemas.attempt import GeneratedQuestion
    from app.services.assessments import validate_generated

    other = QuestionFormat.MCQ if fmt is not QuestionFormat.MCQ else QuestionFormat.MATCH
    item = GeneratedQuestion.model_validate(
        {"format": fmt.value, "source_chunk_id": "chunk-1", **MODEL_REPLIES[fmt]}
    )
    ok, reason = validate_generated(
        item,
        allowed_chunk_ids={"chunk-1"},
        chunk_text={"chunk-1": PASSAGE},
        expected_format=other,
    )
    assert ok is False and "wrong format" in reason


def _surviving_check_constraints() -> dict[str, str]:
    """The LAST definition of every named check constraint, with its body.

    Last, not every, because migrations are forward-only: an earlier file that added a
    constraint a later file dropped and replaced is an accurate record of a schema that
    no longer exists. Reading them all would report bugs that have already been fixed —
    and worse, would make fixing one impossible without rewriting history.
    """
    sql = "\n".join(migration_sql())
    found: dict[str, str] = {}
    for match in re.finditer(r"add\s+constraint\s+(\w+)\s+check\s*\(", sql, re.IGNORECASE):
        # Walk to the matching paren; the bodies nest and a regex cannot count.
        depth, index = 1, match.end()
        while depth and index < len(sql):
            depth += {"(": 1, ")": -1}.get(sql[index], 0)
            index += 1
        found[match.group(1).lower()] = sql[match.end() : index - 1]
    return found


def test_the_constraint_parser_found_the_shape_rules() -> None:
    """Both tests below read from this. A parser that matched nothing would pass."""
    surviving = _surviving_check_constraints()
    for name in ("questions_match_shape", "questions_short_text_shape",
                 "questions_multi_select_shape", "questions_sequence_shape",
                 "questions_format_matches_family"):
        assert name in surviving, f"{name} was not parsed out of the migrations"


def test_no_check_constraint_leaves_a_strict_function_unguarded() -> None:
    """Three-valued logic, which is how the shape constraints shipped broken once.

    `jsonb_exists(NULL, 'pairs')` is NULL rather than false — it is strict, so a NULL
    input gives a NULL output — and **a CHECK constraint passes on NULL**. Written as

        check (type <> 'match' or (... and jsonb_exists(answer_key, 'pairs')))

    a match grid with no answer key at all evaluated to `false OR NULL` = NULL and was
    accepted by the database. Nothing in the application can produce such a row, but
    that is exactly the argument these constraints exist so nobody has to make: they
    are the second line, and a second line that evaluates to NULL is decorative.

    The neighbouring `questions_mcq_shape` was never affected, and the difference is
    the rule: it is written entirely in `is not null` tests, which are themselves never
    NULL. So the rule is that every strict call is preceded by a null test on the same
    column, which yields a definite false and short-circuits the AND.
    """
    unguarded = []
    for name, body in _surviving_check_constraints().items():
        for match in re.finditer(r"jsonb_exists\(\s*(\w+)\s*,", body, re.IGNORECASE):
            column = match.group(1).lower()
            if f"{column} is not null" not in body[: match.start()].lower():
                unguarded.append(f"{name} -> jsonb_exists({column}, ...)")
    assert not unguarded, (
        f"{unguarded} calls a strict function with no `is not null` guard before it. "
        "A NULL column makes the whole CHECK evaluate to NULL, and a CHECK passes on "
        "NULL — so the constraint silently accepts the row it exists to refuse."
    )


def test_the_strict_function_sweep_found_something_to_check() -> None:
    """A regex that matched nothing would make the test above pass on an empty list."""
    bodies = "".join(_surviving_check_constraints().values())
    assert len(re.findall(r"jsonb_exists\(", bodies, re.IGNORECASE)) >= 4
