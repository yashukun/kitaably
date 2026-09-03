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
from app.db.models.enums import Difficulty, QuestionFormat, QuestionType
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
    """The failure this exists for: a family with no entry in the dispatch table.

    It used to be able to fall through to the subjective grader -- an LLM call with no
    rubric, and a mark nobody could defend. Since D32 there is no fallthrough at all
    and `grade_answer` raises instead, so this assertion is the strictly stronger one:
    the table must cover the enum exactly, not merely between it and a default.
    """
    assert set(_DETERMINISTIC) == set(QuestionType)


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
    """The format -> family CASE from the LAST `questions_format_matches_family`.

    `rindex`, not `index`. Migrations are forward-only and this constraint has been
    dropped and re-added: the first occurrence is the fourteen-branch version from
    20260825141000, which stopped being true the moment 20260902120000 replaced it.
    Reading the first one compares Python against a mapping no database holds, and the
    failure reads as drift rather than as a stale parser.

    Anchored on "add constraint ..." rather than the bare name, because the bare name
    also appears in that migration's `drop constraint` list -- and the text after a
    drop contains no `when` clauses at all.
    """
    sql = "\n".join(migration_sql())
    start = sql.rindex("add constraint questions_format_matches_family")
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


def test_the_enum_in_postgres_holds_exactly_what_python_holds() -> None:
    """The narrowed type is only narrow if the migration actually replaced it.

    Postgres has no `ALTER TYPE ... DROP VALUE`, so narrowing means creating a new type
    and swapping the column onto it. A migration that narrowed Python but left the type
    alone would pass every other test here and still let a stale binary -- or a psql
    session -- write a format nothing in this codebase can render or mark.
    """
    sql = "\n".join(migration_sql())
    start = sql.rindex("create type public.question_format as enum")
    body = sql[start : sql.index(")", start)]
    assert set(re.findall(r"'(\w+)'", body)) == {fmt.value for fmt in QuestionFormat}


# --- what a paper is drawn as ------------------------------------------------


def test_every_paper_is_drawn_as_multiple_choice() -> None:
    """One format since D32, and `resolve_formats` is the single place that says so.

    Everything downstream -- the prompt, the trace, the per-format quota, the renderer
    -- keys off what this returns, so a paper that came back as something else would
    have got there by a second code path deciding formats, which is exactly the
    arrangement the registry exists to prevent.
    """
    resolved = formats.resolve_formats()
    assert resolved == [QuestionFormat.MCQ]
    assert all(fmt in formats.SPECS for fmt in resolved)


def test_the_plan_reaches_every_level_before_it_repeats_one() -> None:
    """Round-robin, not random: a five-question paper across three levels gets all
    three. Sampling would give you five of whichever came up first often enough to be
    a complaint and rarely enough to look like bad luck.

    The levels are the axis this still steers, now that the format does not vary.
    """
    levels = [Difficulty.RECALL, Difficulty.UNDERSTAND, Difficulty.APPLY]
    plan = formats.plan_mix(levels, count=5)
    assert len(plan) == 5
    assert {level for _, level in plan} == set(levels)
    assert {fmt for fmt, _ in plan} == {QuestionFormat.MCQ}


def test_an_empty_level_choice_still_produces_a_plan() -> None:
    """Skipping the level picker is a choice, not a missing answer: it means the
    default spread. A plan of nothing would be a paper of nothing."""
    plan = formats.plan_mix([], count=4)
    assert len(plan) == 4
    assert {level for _, level in plan} == set(formats.AUTO_LEVELS)


def test_the_mix_is_stable_across_two_identical_requests() -> None:
    """Same request, same shape of paper. A generated paper is hard enough to reason
    about without the plan changing underneath it."""
    levels = [Difficulty.RECALL, Difficulty.EVALUATE]
    assert formats.plan_mix(levels, count=7) == formats.plan_mix(levels, count=7)


# --- the shape builder --------------------------------------------------------
#
# One builder for the model's output AND the author's own typing. These are the checks
# an author would otherwise have to make by hand, twenty times, on a paper they did
# not write.


def build(fmt: QuestionFormat, **kwargs):
    return build_question_fields(fmt, **kwargs)


def test_the_family_is_set_from_the_format_and_never_from_the_caller() -> None:
    """`type` is derived, never accepted. A question drawn as one thing and marked as
    another scores zero for everybody who sat it, and the client does not get a say."""
    fields = build(
        QuestionFormat.MCQ,
        stem="Where in the chloroplast does the Calvin cycle occur?",
        options=[
            {"key": "A", "text": "Stroma"},
            {"key": "B", "text": "Thylakoid"},
            {"key": "C", "text": "Nucleus"},
        ],
        correct_option="A",
    )
    assert fields["type"] is QuestionType.MCQ
    assert fields["format"] is QuestionFormat.MCQ


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


def test_the_builder_clears_every_column_an_mcq_does_not_use() -> None:
    """The unused columns come back as None rather than being left alone.

    They still exist on the table and rows written before D32 still hold values in
    them, so editing such a question in place must clear what it no longer uses. A
    leftover `answer_key` or `model_answer` beside a `correct_option` is a question
    with two answers, and nothing downstream would agree about which one counts.
    """
    fields = build(
        QuestionFormat.MCQ,
        stem="Where in the chloroplast does the Calvin cycle occur?",
        options=[
            {"key": "A", "text": "Stroma"},
            {"key": "B", "text": "Thylakoid"},
            {"key": "C", "text": "Nucleus"},
        ],
        correct_option="A",
    )
    assert fields["correct_option"] == "A"
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
# parse -> validate -> row, for a well-formed reply in every format there is. Each
# stage is covered above; this is the one that catches them disagreeing. A format
# whose prompt asks for a field the validator does not read, or whose validator
# accepts something the row builder cannot store, passes every test above and fails
# once — in a worker, after the LLM call has been paid for.
#
# Parametrised over the enum rather than written out once, so a format added later
# gets this coverage by being added to the fixtures below and nowhere else.

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

    # A format the model might name that this system does not have. With one format
    # left there is no second REAL value to ask for, and a retired name is the case
    # that actually happens: an old prompt, a fine-tune that remembers `true_false`,
    # or a model free-associating from the passage.
    item = GeneratedQuestion.model_validate(
        {"format": "true_false", "source_chunk_id": "chunk-1", **MODEL_REPLIES[fmt]}
    )
    ok, reason = validate_generated(
        item,
        allowed_chunk_ids={"chunk-1"},
        chunk_text={"chunk-1": PASSAGE},
        expected_format=fmt,
    )
    assert ok is False and reason


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
