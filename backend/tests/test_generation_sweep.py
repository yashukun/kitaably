"""The stale-generation sweep: a killed worker's row must come back to the author.

A worker killed mid-generation (compose down, a closed laptop) cannot run its
failure handler, so its assessment stays `generating` — a spinner that never
resolves, on a row `_require_editable` refuses to let anyone touch. The sweep is
the only way back. These tests pin the two halves: the predicate that decides
which rows are dead, and the write that revives one.

No database here, in the style of test_scoping: the predicate is asserted on the
compiled query, and the write on an in-memory row.
"""

from datetime import UTC, datetime, timedelta

from app.db.models import Assessment
from app.db.models.enums import AssessmentStatus
from app.workers.tasks.maintenance import (
    GENERATION_INTERRUPTED,
    _release_stale_generation,
    _stale_generations_query,
)

NOW = datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)
CUTOFF = NOW - timedelta(seconds=900)


# ------------------------------------------------------------- the predicate


def test_the_sweep_selects_only_generating_rows_past_the_cutoff() -> None:
    compiled = _stale_generations_query(CUTOFF).compile()
    where = str(compiled).split("WHERE")[1]

    assert "assessments.status =" in where
    assert "assessments.updated_at <" in where
    # The bound values, not just the column names: a predicate comparing the right
    # columns to the wrong values would still sweep live runs.
    assert AssessmentStatus.GENERATING in compiled.params.values()
    assert CUTOFF in compiled.params.values()


def test_the_staleness_comparison_is_strictly_older_than() -> None:
    # `<`, not `<=`: a row touched exactly at the cutoff has checkpointed within
    # the window and is not yet evidence of a dead worker.
    where = str(_stale_generations_query(CUTOFF).compile()).split("WHERE")[1]
    assert "updated_at < " in where
    assert "updated_at <= " not in where


# ----------------------------------------------------------------- the write


def _stuck_row(trace: dict | None) -> Assessment:
    row = Assessment(status=AssessmentStatus.GENERATING, generation_trace=trace)
    row.updated_at = NOW - timedelta(seconds=1200)
    return row


def test_release_writes_draft_and_a_reason_the_author_can_act_on() -> None:
    row = _stuck_row(trace=None)
    _release_stale_generation(row, NOW)

    assert row.status is AssessmentStatus.DRAFT
    assert row.error == GENERATION_INTERRUPTED
    # `error`, never `generation_note`: the note means generation worked and came
    # back short; this run did not work. The columns must not blur (assessment.py).
    assert getattr(row, "generation_note", None) is None


def test_release_stamps_a_live_trace_as_finished() -> None:
    # finished_at null is the Advanced panel's "still running" contract
    # (generation_trace.py). A revived row must not keep claiming to run.
    row = _stuck_row(trace={"finished_at": None, "steps": [{"step": "pool"}]})
    _release_stale_generation(row, NOW)

    trace = row.generation_trace
    assert trace is not None
    assert trace["finished_at"] == row.updated_at.isoformat(timespec="seconds")
    assert trace["steps"] == [{"step": "pool"}]  # evidence untouched


def test_release_leaves_a_finished_trace_alone() -> None:
    # A trace with finished_at set is a completed record from an earlier run;
    # re-stamping it would falsify when that run ended.
    row = _stuck_row(trace={"finished_at": "2026-08-27T16:53:00+00:00", "steps": []})
    _release_stale_generation(row, NOW)
    assert row.generation_trace is not None
    assert row.generation_trace["finished_at"] == "2026-08-27T16:53:00+00:00"


def test_release_survives_a_row_with_no_trace() -> None:
    # A run can die before its first checkpoint; the sweep must not.
    row = _stuck_row(trace=None)
    _release_stale_generation(row, NOW)
    assert row.generation_trace is None
    assert row.status is AssessmentStatus.DRAFT
