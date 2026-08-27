"""The generation pipeline trace — the recorder, which is pure and content-free.

Two properties carry the weight. The **arithmetic**: the summary is what an author
evaluates performance from, and a summary that disagrees with its own steps is worse
than no trace. The **vocabulary**: the trace lands in a column a sitter can read under
RLS (`assessments_select_author_or_sitter`), and RLS cannot hide a column — so the
serialized payload may contain counts, formats, durations and fixed reason strings,
and must never contain question text. The recorder's API is what enforces that; these
tests are what notice the API growing a hole.
"""

import json

from app.db.models.enums import QuestionFormat
from app.services.generation_trace import FORMAT_TAG, GenerationTrace, attach_trace


def run_of_two_calls() -> GenerationTrace:
    tracer = GenerationTrace(model="llama3.2:3b", budget=12, target=8)
    tracer.step("pool", "5 usable chunks from 1 book(s)")
    tracer.step("sample", "5 of 5 chunks, spread for coverage, 1 batch(es)")
    tracer.call(
        QuestionFormat.MCQ,
        ms=61_000,
        wanted=3,
        returned=3,
        accepted=2,
        reasons=["duplicate options"],
    )
    tracer.call(
        QuestionFormat.FLASHCARD,
        ms=120_000,
        wanted=2,
        returned=0,
        accepted=0,
        reasons=[],
        failure="JSONDecodeError",
    )
    tracer.dedupe(2, 2)
    tracer.step("persist", "2 questions written")
    return tracer


# --- the contract -------------------------------------------------------------


def test_the_trace_is_versioned() -> None:
    """Like the export, and for the same reason: a future shape change must be
    detectable by whatever was written against this one."""
    payload = run_of_two_calls().finish(final=2)
    assert payload["format"] == FORMAT_TAG == "kitaably.generation-trace.v1"


def test_steps_carry_the_chat_trace_triple() -> None:
    """`{step, detail, ms}`, exactly — the two Advanced panels share a renderer's
    expectations, and a fourth key here would be a second schema to keep in step."""
    for step in run_of_two_calls().finish(final=2)["steps"]:
        assert set(step) == {"step", "detail", "ms"}
        assert isinstance(step["ms"], int)


def test_the_summary_arithmetic_agrees_with_the_calls() -> None:
    summary = run_of_two_calls().finish(final=2)["summary"]
    assert summary["llm_calls"] == 2
    assert summary["llm_ms"] == 181_000
    assert summary["accepted"] == 2
    assert summary["rejected"] == 1
    assert summary["final"] == 2
    assert summary["target"] == 8
    assert summary["llm_budget"] == 12


def test_per_format_tallies_separate_failure_from_rejection() -> None:
    """A call that produced unparseable output and a call whose questions were
    rejected are different findings: the first says the model cannot write this
    format at all, the second says it nearly can. Folding them together would
    hide the difference an author acts on."""
    per_format = run_of_two_calls().finish(final=2)["summary"]["per_format"]
    assert per_format["mcq"] == {
        "calls": 1, "accepted": 2, "rejected": 1, "failed_calls": 0,
    }
    assert per_format["flashcard"] == {
        "calls": 1, "accepted": 0, "rejected": 0, "failed_calls": 1,
    }


def test_a_failed_call_never_counts_questions() -> None:
    """Nothing parseable came back, so nothing was accepted or rejected — a failed
    call that inflated either number would corrupt the acceptance rate the summary
    exists to report."""
    tracer = GenerationTrace(model="m", budget=2, target=2)
    tracer.call(
        QuestionFormat.MATCH, ms=1000, wanted=2, returned=0, accepted=0,
        reasons=[], failure="APITimeoutError",
    )
    summary = tracer.finish(final=0)["summary"]
    assert summary["accepted"] == 0 and summary["rejected"] == 0


def test_dedupe_records_the_drop() -> None:
    tracer = GenerationTrace(model="m", budget=2, target=8)
    tracer.dedupe(8, 6)
    payload = tracer.finish(final=6)
    assert payload["summary"]["deduped"] == 2
    assert any(s["step"] == "dedupe" and "8 → 6" in s["detail"] for s in payload["steps"])


def test_the_trace_rides_a_failing_exception() -> None:
    """The session a failed run was built in is rolled back, so the trace leaves on
    the exception and the task's failure handler stores it — a failed run is exactly
    the run whose trace somebody wants to read."""
    from app.core.errors import ValidationFailed

    payload = run_of_two_calls().finish(final=0)
    exc = attach_trace(ValidationFailed("nothing usable"), payload)
    assert exc.generation_trace is payload


# --- the live snapshot --------------------------------------------------------
#
# Generation checkpoints `snapshot()` onto the row after every stage and call, so
# the author's Advanced panel can watch a run while it happens. The contract is
# one field: `finished_at` null means live, and the final `finish()` overwrites it.


def test_a_snapshot_is_the_same_shape_with_finished_at_null() -> None:
    tracer = run_of_two_calls()
    live = tracer.snapshot()
    done = tracer.finish(final=2)

    assert live["finished_at"] is None
    assert done["finished_at"] is not None
    assert set(live) == set(done)
    assert live["steps"] == done["steps"]
    assert live["summary"]["llm_calls"] == done["summary"]["llm_calls"]


def test_a_snapshot_is_a_value_not_a_window() -> None:
    """The snapshot lands on an ORM row and sits there until the next checkpoint.
    If it aliased the recorder's own lists, the row would silently grow steps in
    memory between commits and the persisted trace would depend on flush timing."""
    tracer = GenerationTrace(model="m", budget=4, target=4)
    tracer.step("pool", "5 usable chunks from 1 book(s)")
    live = tracer.snapshot()
    tracer.call(
        QuestionFormat.MCQ, ms=1000, wanted=2, returned=2, accepted=2, reasons=[]
    )

    assert len(live["steps"]) == 1
    assert live["summary"]["llm_calls"] == 0


def test_a_snapshot_keeps_the_content_free_contract() -> None:
    """Same strict key walk the finished payload gets: a snapshot lands in the same
    sitter-readable column, mid-run, so it earns the same scrutiny."""
    payload = run_of_two_calls().snapshot()

    allowed_keys = {
        "format", "model", "started_at", "finished_at", "steps", "summary",
        "step", "detail", "ms",
        "target", "wall_ms", "llm_ms", "llm_calls", "llm_budget",
        "accepted", "rejected", "deduped", "final", "per_format",
        "calls", "failed_calls",
    } | {fmt.value for fmt in QuestionFormat}

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                assert key in allowed_keys, f"unexpected trace key {key!r}"
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    assert json.loads(json.dumps(payload)) == payload


# --- the content-free rule ----------------------------------------------------


def test_the_serialized_trace_contains_no_question_text() -> None:
    """The invariant the whole design rests on. This payload lands in a column a
    sitter with an attempt can SELECT, and RLS cannot hide a column — so if a stem
    can reach the trace, a sitter can read a question before they sit the paper.

    The recorder's API takes counts, enums, durations and fixed reason strings;
    nothing accepts model output. This test feeds the recorder everything its API
    accepts and then checks the payload holds only known keys and no long prose —
    so an API change that starts accepting free text fails here before it ships."""
    payload = run_of_two_calls().finish(final=2)

    allowed_keys = {
        "format", "model", "started_at", "finished_at", "steps", "summary",
        "step", "detail", "ms",
        "target", "wall_ms", "llm_ms", "llm_calls", "llm_budget",
        "accepted", "rejected", "deduped", "final", "per_format",
        "calls", "failed_calls",
    } | {fmt.value for fmt in QuestionFormat}

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                assert key in allowed_keys, f"unexpected trace key {key!r}"
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)

    # And no value long enough to be a stem. Step details are built from counts and
    # enum values; the longest legitimate one is a plan line over every format.
    for step in payload["steps"]:
        assert len(step["detail"]) < 300

    # Round-trips as plain JSON — it is a jsonb column, not a pickle.
    assert json.loads(json.dumps(payload)) == payload


def test_a_call_failure_is_a_class_name_not_a_message() -> None:
    """An exception MESSAGE from a provider can quote the prompt, and the prompt
    quotes the book. The recorder takes the class name only; this pins the detail
    line to that."""
    tracer = GenerationTrace(model="m", budget=1, target=1)
    tracer.call(
        QuestionFormat.MCQ, ms=10, wanted=1, returned=0, accepted=0,
        reasons=[], failure="JSONDecodeError",
    )
    (step,) = tracer.finish(final=0)["steps"]
    assert step["detail"] == "mcq · failed: JSONDecodeError"
