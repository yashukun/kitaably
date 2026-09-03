"""Exporting a paper — the renderers, which are pure over ORM rows.

The service does one authorized read and hands the rows to these. What is worth
pinning is the contract of the files themselves, and one thing that is not in them.

**The share token must never appear.** It is the whole access grant to a paper (D16),
so it is a credential, and an export is a file that gets emailed, synced and forwarded.
Everything else in here is a field the author's own detail endpoint already returns;
that one is not, and the difference is the point.
"""

import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from app.db.models import Assessment, Question
from app.db.models.enums import (
    AssessmentRigor,
    AssessmentStatus,
    AssessmentType,
    Difficulty,
    QuestionFormat,
    QuestionOrigin,
    QuestionType,
    ResultsRelease,
)
from app.services.assessments import (
    _export_filename,
    render_export_json,
    render_export_markdown,
)

TOKEN = "8a7fce4bdce23435a661ed8aa7f04d6a"


def paper() -> tuple[Assessment, list[Question]]:
    assessment = Assessment(
        author_id=uuid4(),
        title="Photosynthesis — end of chapter",
        type=AssessmentType.MIXED,
        rigor=AssessmentRigor.COMPETITIVE,
        status=AssessmentStatus.PUBLISHED,
        question_count=5,
        duration_minutes=30,
        results_release=ResultsRelease.ON_REVIEW,
        max_score=Decimal("12"),
        source_selection={"book_ids": [], "chapter_ids": []},
        generation_spec={
            "count": 10,
            "formats": ["mcq", "multi_select", "match", "sequence", "one_word"],
            "levels": ["recall", "evaluate"],
            "instructions": "Concentrate on the light-independent reactions.",
            "auto": False,
        },
    )
    assessment.id = uuid4()
    assessment.share_token = TOKEN
    assessment.created_at = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)
    assessment.updated_at = datetime(2026, 8, 25, 9, 30, tzinfo=UTC)

    def question(index: int, **kwargs) -> Question:
        base = {
            "assessment_id": assessment.id,
            "index": index,
            "points": Decimal("1"),
            "difficulty": Difficulty.RECALL,
            "origin": QuestionOrigin.GENERATED,
            "source_chunk_ids": [f"chunk-{index}"],
        }
        row = Question(**{**base, **kwargs})
        row.id = uuid4()
        return row

    return assessment, [
        question(
            0,
            type=QuestionType.MCQ,
            format=QuestionFormat.MCQ,
            stem="Where in the chloroplast does the Calvin cycle occur?",
            options=[{"key": "A", "text": "Stroma"}, {"key": "B", "text": "Thylakoid"}],
            correct_option="A",
        ),
        question(
            1,
            type=QuestionType.MCQ,
            format=QuestionFormat.MCQ,
            stem="Which stage of the Calvin cycle comes first?",
            options=[{"key": k, "text": t} for k, t in
                     (("A", "Fixation"), ("B", "Reduction"), ("C", "Regeneration"))],
            correct_option="A",
            points=Decimal("2"),
        ),
        question(
            2,
            type=QuestionType.MCQ,
            format=QuestionFormat.MCQ,
            stem="Why is the Calvin cycle no longer called a dark reaction?",
            options=[{"key": "A", "text": "It depends on products of the light "
                                          "reactions."},
                     {"key": "B", "text": "It happens only at night."}],
            correct_option="A",
            points=Decimal("3"),
            difficulty=Difficulty.EVALUATE,
        ),
    ]


# --- the credential that must not travel -------------------------------------


@pytest.mark.parametrize("render", [render_export_json, render_export_markdown])
def test_the_share_token_is_never_in_an_export(render) -> None:
    """The one field an author can see that this deliberately omits.

    The token IS the access grant, so it is a credential — and an export is a file that
    gets emailed, synced and forwarded. Everything else here is a field the detail
    endpoint already returns; putting the token beside them would turn "here is my
    paper" into "here is the way in to my paper"."""
    assessment, questions = paper()
    assert TOKEN not in render(assessment, questions)


@pytest.mark.parametrize("render", [render_export_json, render_export_markdown])
def test_no_export_carries_anybody_elses_result(render) -> None:
    """Who sat the paper and what they scored is a different document about different
    people. "Export the assessment" does not ask for it, and a paper handed to a
    colleague should not come with a gradebook attached."""
    assessment, questions = paper()
    body = render(assessment, questions).lower()
    # `max_score` is a property of the paper, not of anybody's sitting, so it stays.
    # These are the words that could only come from one: a person, or their mark.
    for word in ("attempt", "sitter", "gradebook", "awarded", "graded_at", "released"):
        assert word not in body, f"{word!r} leaked into an export of the paper"


# --- the data contract --------------------------------------------------------


def test_the_json_is_versioned() -> None:
    """So a future shape change is detectable by whatever was written against this one,
    instead of silently reading differently."""
    assessment, questions = paper()
    assert json.loads(render_export_json(assessment, questions))["format"] == (
        "kitaably.assessment.v1"
    )


def test_the_json_carries_every_question_with_both_of_its_kinds() -> None:
    """`format` is the shape and `type` is the family that marks it. An importer needs
    both; one without the other cannot reconstruct the question."""
    assessment, questions = paper()
    payload = json.loads(render_export_json(assessment, questions))
    assert len(payload["questions"]) == len(questions)
    for exported, original in zip(payload["questions"], questions, strict=True):
        assert exported["format"] == original.format.value
        assert exported["type"] == original.type.value
        assert exported["format_label"]
        assert exported["stem"] == original.stem


def test_every_exported_question_carries_its_answer() -> None:
    """An export whose answer key is empty for one family is an export somebody
    discovers is useless after they have already deleted the paper."""
    assessment, questions = paper()
    for exported in json.loads(render_export_json(assessment, questions))["questions"]:
        assert exported["answer"], f"{exported['format']} exported with no answer"


def test_the_answer_holds_only_the_fields_that_format_uses() -> None:
    """A match grid with `correct_option: null` sitting beside its real key invites a
    reader to believe the null means something."""
    assessment, questions = paper()
    by_format = {
        q["format"]: q for q in json.loads(render_export_json(assessment, questions))["questions"]
    }
    assert set(by_format["mcq"]["answer"]) == {"correct_option"}


def test_every_exported_question_carries_its_provenance() -> None:
    """A question an author cannot trace to a passage is one they cannot defend when a
    sitter disputes it, and that stays true outside the database."""
    assessment, questions = paper()
    for exported in json.loads(render_export_json(assessment, questions))["questions"]:
        assert exported["source_chunk_ids"]


def test_an_export_carries_the_note_about_a_short_paper() -> None:
    """An export is what somebody reads six months later, when they cannot remember
    why the paper is four questions long. The note is the answer, so it travels."""
    assessment, questions = paper()
    assessment.generation_note = "You asked for 10 questions and this paper has 2."
    assert assessment.generation_note in json.dumps(
        json.loads(render_export_json(assessment, questions))
    )
    assert assessment.generation_note in render_export_markdown(assessment, questions)


def test_a_full_paper_exports_without_a_note() -> None:
    assessment, questions = paper()
    assert assessment.generation_note is None
    payload = json.loads(render_export_json(assessment, questions))
    assert payload["assessment"]["generation_note"] is None


def test_the_json_says_what_was_asked_for_beside_what_came_out() -> None:
    """"Why is this paper full of true/false questions" should be answerable from the
    file itself."""
    assessment, questions = paper()
    requested = json.loads(render_export_json(assessment, questions))["assessment"]["requested"]
    # The count especially: `question_count` on the row is overwritten with what was
    # actually written, so without this the file cannot say what was asked for — which
    # is the first thing anybody wants to know about a short paper.
    assert requested["count"] == 10
    assert requested["formats"] == assessment.generation_spec["formats"]
    assert requested["levels"] == ["recall", "evaluate"]
    assert requested["auto"] is False


def test_the_json_is_actually_json() -> None:
    """Decimal and enum both raise from json.dumps if they reach it unconverted, and a
    `numeric(10,2)` column is a Decimal on the way out."""
    assessment, questions = paper()
    payload = json.loads(render_export_json(assessment, questions))
    assert payload["assessment"]["max_score"] == 12.0
    assert payload["questions"][1]["points"] == 2.0


# --- the document -------------------------------------------------------------


def test_the_markdown_separates_the_paper_from_the_key() -> None:
    """This is what makes the file usable: the first half can be printed for a room and
    the second kept back. Interleaved answers would be a document an author cannot hand
    to anybody."""
    body = render_export_markdown(*paper())
    assert body.index("## The paper") < body.index("## Answer key")


def test_the_markdown_answer_key_spells_the_answer_out() -> None:
    """"B" is not an answer, it is a pointer to one. A printed key nobody can check
    against is not worth printing."""
    body = render_export_markdown(*paper())
    key = body[body.index("## Answer key") :]
    # The option TEXT, not just the letter that points at it.
    assert "Stroma" in key
    assert "Fixation" in key


def test_the_questions_half_does_not_give_the_answer_away() -> None:
    """The half an author prints for a room. A model answer leaking into it is the
    whole paper leaking."""
    body = render_export_markdown(*paper())
    questions_half = body[body.index("## The paper") : body.index("## Answer key")]
    # Every option is printed -- that IS the question. What must not appear is any mark
    # of which one is right. The key half writes the answer as an unbulleted
    # "**A.** Stroma"; the paper half writes every option as a "- **A.** " list item,
    # so the absence of the unbulleted form is the absence of the answer.
    for question_number in ("**1.**", "**2.**", "**3.**"):
        assert question_number in questions_half
    assert "\n**A.** Stroma" not in questions_half


def test_every_option_is_printed_on_the_paper() -> None:
    """A question missing an option is not the question the author wrote.

    List syntax, not bare lines: consecutive plain lines are ONE paragraph in Markdown,
    so an option list written as `A. …` / `B. …` renders as a single run-on sentence —
    in exactly the half of the file somebody prints and hands out."""
    questions_half = render_export_markdown(*paper()).split("## Answer key")[0]
    assert "- **A.** Stroma" in questions_half
    assert "- **B.** Thylakoid" in questions_half


def test_an_empty_paper_exports_rather_than_crashing() -> None:
    """A draft whose generation produced nothing is exactly when somebody hits export
    to find out what happened."""
    assessment, _ = paper()
    body = render_export_markdown(assessment, [])
    assert "no questions yet" in body.lower()
    assert json.loads(render_export_json(assessment, []))["questions"] == []


# --- the filename -------------------------------------------------------------


def test_the_filename_is_recognisable_and_unique() -> None:
    """The slug says which paper; the id suffix keeps two exports of "photosynthesis"
    from overwriting each other in a downloads folder."""
    assessment, _ = paper()
    name = _export_filename(assessment, "json")
    assert name.startswith("kitaably-photosynthesis-end-of-chapter-")
    assert name.endswith(".json")
    assert assessment.id.hex[:8] in name


def test_the_filename_is_ascii_and_quote_safe() -> None:
    """It travels in a Content-Disposition header wrapped in double quotes. A title
    with a quote in it would end the header value early."""
    assessment, _ = paper()
    assessment.title = 'Ravi\'s "hard" paper — 中文 / slashes\\and\\backslashes'
    name = _export_filename(assessment, "md")
    assert name.isascii()
    assert not (set(name) & set('"\\/'))


def test_every_choice_is_a_markdown_list_item() -> None:
    """The run-on bug, pinned.

    Consecutive plain lines are ONE paragraph in Markdown. Options written as bare
    `A. …` / `B. …` lines render as a single run-on sentence — and they do it in
    exactly the half of the file somebody prints and hands to a room, which is the
    half nobody re-reads in a Markdown viewer before printing.

    Every choice, on both halves, must therefore be a list item."""
    body = render_export_markdown(*paper())
    offenders = [
        line
        for line in body.splitlines()
        # Opens with a bare option or item key, and is not already a list item.
        if re.match(r"^(?:\*\*)?[A-H]\.\s+\S", line) and not line.startswith("- ")
    ]
    assert not offenders, (
        f"{offenders} are bare lines, so Markdown will run them together into one "
        "paragraph instead of a list of choices."
    )
