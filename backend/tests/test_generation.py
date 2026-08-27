"""Question generation: coverage sampling, validation, and the author-bound boundary.

A generated question that nobody can answer correctly is worse than no question,
because it sits in a published paper looking like the others. Everything here is a
check the author would otherwise have to make by hand, twenty times.
"""

from uuid import uuid4

import pytest

from app.core.security import Principal
from app.db.models import Chunk
from app.db.models.enums import QuestionFormat, Role
from app.rag.retrieve import build_retrieval_filter
from app.schemas.attempt import GeneratedQuestion
from app.services.assessments import (
    draft_source_clause,
    parse_generated,
    select_chunks_by_coverage,
    validate_generated,
)


def chunk(
    chapter, index: int, tokens: int = 200, text: str = "photosynthesis chloroplast"
) -> Chunk:
    return Chunk(
        id=uuid4(), book_id=uuid4(), chapter_id=chapter, index=index,
        text=text, token_count=tokens,
    )


# --- the boundary -----------------------------------------------------------


def test_generation_never_reaches_another_users_personal_chunk() -> None:
    """The rule the whole phase rests on, as revised by D29: generation draws from
    canon plus the AUTHOR'S own uploads, and can never reach anybody else's personal
    book — the personal clause is bound to the author's id at construction."""
    author = Principal(id=uuid4(), role=Role.USER, email="")
    somebody_else = uuid4()
    rendered = str(
        build_retrieval_filter(author).compile(compile_kwargs={"literal_binds": True})
    )
    assert "'canon'" in rendered
    assert "'personal'" in rendered
    # The personal branch names the author, and only the author.
    assert "chunks.owner_id" in rendered
    assert author.id.hex in rendered
    assert somebody_else.hex not in rendered


def _principal() -> Principal:
    return Principal(id=uuid4(), role=Role.USER, email="")


def _compiled_source_clause(principal: Principal) -> str:
    return str(
        draft_source_clause(principal).compile(compile_kwargs={"literal_binds": True})
    )


def test_a_draft_may_name_a_shared_book_or_the_callers_own() -> None:
    """The request-time half of D29: `create_draft` validates every book id in the
    body against this clause. It admits canon and the caller's own uploads, and the
    owner branch is bound to the caller at construction."""
    caller = _principal()
    rendered = _compiled_source_clause(caller)

    assert "'canon'" in rendered
    assert "books.owner_id" in rendered
    assert caller.id.hex in rendered


def test_a_draft_can_never_name_another_users_personal_book() -> None:
    """Whatever book ids arrive in the request body, the clause admits nothing owned
    by anybody but the caller — someone else's personal book matches zero rows and
    the whole request is refused rather than silently narrowed."""
    caller, other = _principal(), _principal()
    rendered = _compiled_source_clause(caller)

    assert other.id.hex not in rendered
    # The owner test never stands alone as `scope = 'personal'`, which would match
    # every user's private uploads at once.
    assert "'personal'" not in rendered


def test_every_authors_source_clause_has_the_same_shape() -> None:
    """Two authors' clauses differ in the owner id and in nothing else — there is
    no author whose reach is wider than another's, so nothing to escalate into."""
    first, second = _principal(), _principal()
    masked_first = _compiled_source_clause(first).replace(first.id.hex, "OWNER")
    masked_second = _compiled_source_clause(second).replace(second.id.hex, "OWNER")

    assert masked_first == masked_second


# --- coverage, not similarity ----------------------------------------------


def test_sampling_spans_every_chapter() -> None:
    """A top-k similarity search clusters on the densest passage and asks about one
    section five times. This is the fix, so it is the thing to pin."""
    a, b, c = uuid4(), uuid4(), uuid4()
    pool = [chunk(a, i) for i in range(10)] + [chunk(b, i) for i in range(10)] + [
        chunk(c, i) for i in range(10)
    ]

    picked = select_chunks_by_coverage(pool, wanted=9)

    assert {c.chapter_id for c in picked} == {a, b, c}


def test_a_long_chapter_gets_a_bigger_share() -> None:
    short, long = uuid4(), uuid4()
    pool = [chunk(short, i, tokens=100) for i in range(2)] + [
        chunk(long, i, tokens=100) for i in range(20)
    ]

    picked = select_chunks_by_coverage(pool, wanted=11)
    from_long = sum(1 for c in picked if c.chapter_id is long)

    assert from_long > len(picked) - from_long


def test_sampling_spreads_within_a_chapter_rather_than_taking_the_front() -> None:
    """Twenty chunks and five picks should reach the end of the chapter, not stop at
    chunk four — otherwise the paper only ever examines the first few pages."""
    chapter = uuid4()
    pool = [chunk(chapter, i) for i in range(20)]

    picked = select_chunks_by_coverage(pool, wanted=5)
    indices = sorted(c.index for c in picked)

    assert max(indices) >= 12, f"sample stopped at {max(indices)}"
    assert len(set(indices)) == len(indices), "the same chunk was picked twice"


def test_sampling_never_returns_more_than_asked_for() -> None:
    pool = [chunk(uuid4(), 0) for _ in range(30)]
    assert len(select_chunks_by_coverage(pool, wanted=5)) <= 5


def test_an_empty_pool_is_empty_not_an_error() -> None:
    assert select_chunks_by_coverage([], wanted=5) == []


# --- validation: reject before persisting -----------------------------------

ALLOWED = {"chunk-1"}
TEXTS = {"chunk-1": "The Calvin cycle occurs in the stroma of the chloroplast."}


def generated(**overrides) -> GeneratedQuestion:
    base = {
        "type": "mcq",
        "stem": "Where in the chloroplast does the Calvin cycle occur?",
        "options": [
            {"key": "A", "text": "Stroma"},
            {"key": "B", "text": "Thylakoid"},
            {"key": "C", "text": "Nucleus"},
            {"key": "D", "text": "Ribosome"},
        ],
        "correct_option": "A",
        "source_chunk_id": "chunk-1",
    }
    return GeneratedQuestion.model_validate(base | overrides)


def check(item: GeneratedQuestion) -> tuple[bool, str]:
    return validate_generated(item, allowed_chunk_ids=ALLOWED, chunk_text=TEXTS)


def test_a_good_question_is_accepted() -> None:
    assert check(generated())[0] is True


def test_the_correct_answer_must_be_one_of_the_options() -> None:
    """The failure that produces a question nobody can get right."""
    assert check(generated(correct_option="Z"))[0] is False


def test_duplicate_options_are_rejected() -> None:
    """Two options saying the same thing means two correct answers, or none."""
    options = [
        {"key": "A", "text": "Stroma"},
        {"key": "B", "text": "stroma "},
        {"key": "C", "text": "Nucleus"},
    ]
    ok, reason = check(generated(options=options))
    assert ok is False and "duplicate" in reason


@pytest.mark.parametrize("banned", ["All of the above", "none of the above"])
def test_banned_options_are_rejected(banned: str) -> None:
    options = [
        {"key": "A", "text": "Stroma"},
        {"key": "B", "text": "Thylakoid"},
        {"key": "C", "text": banned},
    ]
    assert check(generated(options=options))[0] is False


@pytest.mark.parametrize(
    "stem",
    [
        "According to the passage, where does the Calvin cycle occur in a chloroplast?",
        "In the text above, which chloroplast structure hosts the Calvin cycle process?",
        "Where does the Calvin cycle occur, as mentioned in the passage about plants?",
    ],
)
def test_a_stem_that_refers_to_the_passage_is_rejected(stem: str) -> None:
    """The reader does not have the passage. A stem that assumes they do is unusable,
    and it is the single most common thing a model gets wrong here."""
    ok, reason = check(generated(stem=stem))
    assert ok is False and "passage" in reason


def test_provenance_must_come_from_this_batch() -> None:
    """A model that invents a chunk id has usually invented the question with it."""
    ok, reason = check(generated(source_chunk_id="chunk-999"))
    assert ok is False and "provenance" in reason


def test_provenance_is_mandatory() -> None:
    assert check(generated(source_chunk_id=None))[0] is False


def test_a_stem_ungrounded_in_its_source_is_rejected() -> None:
    """Cheap hallucination check: a stem sharing no distinctive vocabulary with the
    passage it claims to come from usually came from the model's own knowledge."""
    ok, reason = check(
        generated(stem="Which composer finished writing the Jupiter symphony first?")
    )
    assert ok is False and "vocabulary" in reason


def test_a_subjective_question_needs_a_model_answer_to_grade_against() -> None:
    item = generated(
        type="subjective", options=None, correct_option=None, model_answer=None
    )
    assert check(item)[0] is False


# --- parsing ----------------------------------------------------------------


def test_a_markdown_fence_is_tolerated() -> None:
    """Small local models add one despite being told not to."""
    raw = '```json\n{"questions": [{"type": "mcq", "stem": "A stem long enough"}]}\n```'
    assert len(parse_generated(raw)) == 1


def test_leading_prose_is_tolerated() -> None:
    raw = 'Sure! Here are your questions:\n{"questions": [{"type":"mcq","stem":"A stem"}]}'
    assert len(parse_generated(raw)) == 1


@pytest.mark.parametrize("raw", ["not json at all", "", "{}", '{"questions": "nope"}'])
def test_an_unrepairable_response_raises_rather_than_being_patched(raw: str) -> None:
    """Never regex a response into shape. A repaired response is a response nobody
    validated, and the caller's job is to drop the batch and keep the others."""
    with pytest.raises((ValueError, Exception)):
        parse_generated(raw)


# ============================================================================
# The shortfall: why a paper comes back shorter than it was asked for, and what
# the author is told about it.
#
# This is the bug a real run surfaced. Ten questions were asked for across seven
# formats from a five-chunk book. Five formats produced nothing — a 3B model on CPU
# cannot reliably write a match grid, and some calls simply timed out — and each one
# silently forfeited its share. The paper came back with one question, `error` null,
# looking exactly like a one-question paper somebody meant to write.
#
# Two things were missing and both are pinned here: making up the shortfall from the
# formats that DO work, and saying so when it still falls short.

from app.db.models import Chunk as _Chunk  # noqa: E402
from app.rag.formats import SPECS  # noqa: E402
from app.services.assessments import _CallBudget, _shortfall_note  # noqa: E402


def batches(count: int, per_batch: int = 5) -> list[list[_Chunk]]:
    return [[chunk(uuid4(), i) for i in range(per_batch)] for _ in range(count)]


def test_the_call_budget_stops_after_its_limit() -> None:
    """Every call is a minute or two on CPU-only Ollama. An uncapped backfill over a
    book the model cannot write from is a task that runs for an hour, which is
    indistinguishable from one that hung."""
    budget = _CallBudget(3, batches(2))
    assert [budget.take() is not None for _ in range(4)] == [True, True, True, False]
    assert budget.spent()


def test_the_call_budget_cycles_the_passages() -> None:
    """Round-robin, so the backfill does not re-ask about the opening paragraph every
    time. A five-chunk book has exactly one batch, and cycling is what lets a second
    attempt happen at all."""
    made = batches(2)
    budget = _CallBudget(10, made)
    seen = [budget.take() for _ in range(4)]
    assert seen == [made[0], made[1], made[0], made[1]]


def test_a_budget_with_no_passages_is_spent_immediately() -> None:
    """A book whose chunks were all below the minimum token count. Better to produce
    nothing than to loop asking about an empty list."""
    assert _CallBudget(10, []).spent() is True
    assert _CallBudget(10, []).take() is None


# --- what the author is told --------------------------------------------------


def test_a_full_paper_says_nothing() -> None:
    """The note is for a paper that fell short. A complete one needs no explanation,
    and a notice on every paper is a notice nobody reads."""
    produced = {QuestionFormat.MCQ: 5, QuestionFormat.TRUE_FALSE: 5}
    assert _shortfall_note(target=10, produced=produced, final=10) is None


def test_an_over_full_paper_says_nothing() -> None:
    assert _shortfall_note(target=5, produced={QuestionFormat.MCQ: 9}, final=9) is None


def test_a_short_paper_says_how_short() -> None:
    """The exact question a real author asked: why did I only get one question?"""
    note = _shortfall_note(target=10, produced={QuestionFormat.MCQ: 1}, final=1)
    assert note is not None
    assert "10" in note and "1" in note


def test_a_short_paper_names_the_formats_that_produced_nothing() -> None:
    """The actionable half. A book about one person's life supports multiple choice and
    short answers and does not support a four-item ordering — and the fix is for the
    author to stop asking for one, not to try again and hope."""
    produced = {
        QuestionFormat.MCQ: 1,
        QuestionFormat.MATCH: 0,
        QuestionFormat.SEQUENCE: 0,
        QuestionFormat.ONE_WORD: 0,
    }
    note = _shortfall_note(target=10, produced=produced, final=1)
    assert SPECS[QuestionFormat.MATCH].label in note
    assert SPECS[QuestionFormat.SEQUENCE].label in note
    assert SPECS[QuestionFormat.ONE_WORD].label in note
    # The one that worked is not on the list of things to stop asking for.
    assert SPECS[QuestionFormat.MCQ].label not in note


def test_the_note_never_blames_the_author_or_the_model() -> None:
    """It is a report of what happened, in the same register as the rest of the
    product. "The model failed" is not something a reader can act on, and neither is
    being told they chose badly."""
    note = _shortfall_note(
        target=10, produced={QuestionFormat.MCQ: 1, QuestionFormat.MATCH: 0}, final=1
    )
    for word in ("failed", "error", "sorry", "unfortunately", "invalid"):
        assert word not in note.lower()


def test_the_note_tells_the_author_what_to_do_next() -> None:
    """A notice that only says something went wrong leaves the author where they
    started."""
    note = _shortfall_note(target=10, produced={QuestionFormat.MCQ: 1}, final=1)
    assert "Try" in note


# ============================================================================
# Wall clock (D30). On a CPU model every reply token is ~130ms, so the rules that
# keep a paper from taking ten minutes are token rules: ask only for fields that
# are stored, never ask for more questions than the reply ceiling can hold, reject
# a duplicate before the next call is priced rather than after the run, and keep
# the varying part of the prompt at the end so the passage prefill can be reused.

from app.db.models.enums import AssessmentRigor, Difficulty  # noqa: E402
from app.rag import formats, prompts  # noqa: E402
from app.services import assessments as assessments_service  # noqa: E402
from app.services.assessments import _StemDeduper  # noqa: E402


def test_the_reply_shape_asks_only_for_fields_that_are_stored() -> None:
    """A `rationale` used to be demanded, parsed, and discarded — four to six
    seconds of decode per question spent writing something nothing ever read."""
    for shape in formats.FAMILY_SHAPE.values():
        assert "rationale" not in shape


def test_the_ask_never_overflows_the_reply_ceiling() -> None:
    """A reply that hits `max_tokens` mid-array is un-parseable JSON and the whole
    call is thrown away — a real run spent 170 seconds on exactly that."""
    for fmt, spec in formats.SPECS.items():
        cap = formats.batch_ask_cap(fmt, max_reply_tokens=800)
        assert cap >= 1
        if cap > 1:
            assert cap * spec.reply_tokens <= 800


def test_a_tight_ceiling_still_asks_for_one_question() -> None:
    """A truncated reply and no reply cost the same, so a format whose single
    question may not fit is still worth one attempt."""
    assert formats.batch_ask_cap(QuestionFormat.LONG_ANSWER, max_reply_tokens=100) == 1


def test_a_verbose_format_is_asked_for_fewer_per_call_than_a_terse_one() -> None:
    budget = 800
    assert formats.batch_ask_cap(
        QuestionFormat.LONG_ANSWER, max_reply_tokens=budget
    ) < formats.batch_ask_cap(QuestionFormat.TRUE_FALSE, max_reply_tokens=budget)


def _generation_prompt(**overrides) -> str:
    kwargs = dict(
        fmt=QuestionFormat.MCQ,
        levels=[Difficulty.RECALL],
        wanted=2,
        rigor=AssessmentRigor.MEDIUM,
    )
    kwargs.update(overrides)
    messages = prompts.generation_prompt(
        [("chunk-1", "A passage about chloroplasts.")], **kwargs
    )
    return messages[1]["content"]


def test_the_prompt_puts_the_ask_before_the_passages() -> None:
    """Measured, not aesthetic. Passages-first (so the provider's prompt cache could
    reuse the stable prefix) was tried and reverted: with the ask at the end the 3B
    model returned one question when asked for four and produced un-parseable JSON
    three calls out of four. A cached prefill saves seconds; a failed call wastes
    the whole call."""
    content = _generation_prompt()
    assert content.index("Write 2 question(s)") < content.index("Passages:")
    assert content.index("JSON shape") < content.index("Passages:")


def test_the_prompt_still_demands_one_format_and_bare_json() -> None:
    """Reordering must not lose the contract: the same format for every question in
    the call, and a reply that is only JSON."""
    content = _generation_prompt()
    assert "all in the MULTIPLE CHOICE format" in content
    assert '"format"' in content
    system = prompts.generation_prompt(
        [("chunk-1", "text")],
        fmt=QuestionFormat.MCQ,
        levels=[Difficulty.RECALL],
        wanted=1,
        rigor=AssessmentRigor.MEDIUM,
    )[0]["content"]
    assert "SAME format" in system
    assert "ONLY a JSON object" in system


def test_accepted_stems_are_fed_back_as_do_not_repeat() -> None:
    """Steering the model away from a duplicate costs a few prompt tokens;
    generating one and rejecting it costs a question's worth of decode."""
    content = _generation_prompt(avoid_stems=["What pigment makes leaves green?"])
    assert "What pigment makes leaves green?" in content
    assert "Do not repeat" in content


def test_no_avoid_list_renders_no_avoid_block() -> None:
    assert "Do not repeat" not in _generation_prompt(avoid_stems=[])


async def test_a_repeated_stem_is_dropped_the_moment_it_arrives(monkeypatch) -> None:
    """Incremental, not end-of-run: dedupe-at-the-end let a run stop at 'ten
    accepted' and ship seven, with the duplicates' decode time already spent."""

    async def fake_embed(texts):
        return [[1.0, 0.0] if "pigment" in text else [0.0, 1.0] for text in texts]

    monkeypatch.setattr(assessments_service.embeddings, "embed_texts", fake_embed)
    deduper = _StemDeduper(0.92)
    assert await deduper.keep(["What pigment makes leaves green?"]) == [True]
    # The same question re-asked by a later call is refused; a genuinely new one is not.
    assert await deduper.keep(
        ["Which pigment turns a leaf green?", "How does osmosis move water?"]
    ) == [False, True]


async def test_dedupe_failure_keeps_the_batch(monkeypatch) -> None:
    """An un-deduped paper is worse than a deduped one and much better than no
    paper."""

    async def broken_embed(texts):
        raise RuntimeError("embeddings unavailable")

    monkeypatch.setattr(assessments_service.embeddings, "embed_texts", broken_embed)
    deduper = _StemDeduper(0.92)
    assert await deduper.keep(["a stem", "another stem"]) == [True, True]
