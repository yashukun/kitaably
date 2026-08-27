"""The generation pipeline trace. Phase 5b.

What actually ran when a paper was written: each stage with its wall-clock lap, one
entry per LLM call, and a summary an author can evaluate performance from. Stored on
``assessments.generation_trace``, rendered by the Advanced panel on the review screen
— the same disclosure the tutor's answers already have, persisted because generation
happens in a worker long after the author's request has returned.

**The content-free rule, enforced by this API's shape.** A sitter with an attempt can
read the ``assessments`` row under RLS, and RLS cannot hide a column — so nothing
question-shaped may ever enter a trace. Every method here accepts counts, enum values,
durations and fixed reason strings; none accepts a stem, an option, or any other free
text a model produced. Adding a parameter that carries model output is how question
content would leak to somebody sitting the paper, so do not.

Step shape matches the chat pipeline trace (``{step, detail, ms}``) so the two
Advanced panels read the same way.
"""

import copy
import time
from datetime import UTC, datetime
from typing import Any

from app.db.models.enums import QuestionFormat

# Versioned like the export, and for the same reason: a future shape change must be
# detectable by whatever was written against this one.
FORMAT_TAG = "kitaably.generation-trace.v1"


class GenerationTrace:
    """One run's trace. Create at the top of ``generate_questions``, snapshot as
    often as you like while the run is live, finish once."""

    def __init__(self, *, model: str, budget: int, target: int) -> None:
        self._started = time.monotonic()
        self._lap = self._started
        self._started_at = datetime.now(UTC)
        self._model = model
        self._budget = budget
        self._target = target
        self._steps: list[dict[str, Any]] = []
        self._calls = 0
        self._llm_ms = 0
        self._per_format: dict[str, dict[str, int]] = {}
        self._totals = {"accepted": 0, "rejected": 0, "deduped": 0, "final": 0}

    # ------------------------------------------------------------------ steps

    def step(self, step: str, detail: str) -> None:
        """One stage of the pipeline. ``ms`` is the lap since the previous step,
        so the timeline's numbers sum to the wall clock rather than overlapping."""
        now = time.monotonic()
        self._steps.append(
            {"step": step, "detail": detail, "ms": int((now - self._lap) * 1000)}
        )
        self._lap = now

    def call(
        self,
        fmt: QuestionFormat,
        *,
        ms: int,
        wanted: int,
        returned: int,
        accepted: int,
        reasons: list[str],
        failure: str | None = None,
    ) -> None:
        """One LLM call. ``failure`` is an exception class name, never its message —
        a provider error string can quote the prompt, and the prompt quotes the book.
        """
        self._calls += 1
        self._llm_ms += ms
        tally = self._per_format.setdefault(
            fmt.value, {"calls": 0, "accepted": 0, "rejected": 0, "failed_calls": 0}
        )
        tally["calls"] += 1

        if failure is not None:
            tally["failed_calls"] += 1
            detail = f"{fmt.value} · failed: {failure}"
        else:
            tally["accepted"] += accepted
            tally["rejected"] += len(reasons)
            self._totals["accepted"] += accepted
            self._totals["rejected"] += len(reasons)
            detail = f"{fmt.value} · asked {wanted}, returned {returned}, accepted {accepted}"
            if reasons:
                detail += f" · rejected: {'; '.join(reasons)}"
        # The lap is the call itself, so `ms` is passed through rather than re-lapped:
        # validation time is real but the model time is the number worth reading.
        now = time.monotonic()
        self._steps.append({"step": "llm", "detail": detail, "ms": ms})
        self._lap = now

    def dedupe(self, before: int, after: int) -> None:
        self._totals["deduped"] = before - after
        self.step(
            "dedupe",
            f"{before} → {after}"
            + (f" ({before - after} near-duplicates dropped)" if before != after else ""),
        )

    # ---------------------------------------------------------------- payload

    def snapshot(self) -> dict[str, Any]:
        """The trace as it stands, mid-run.

        Same shape as :meth:`finish` with ``finished_at`` null — that null is the
        whole contract: the Advanced panel reads it as *still running*. Generation
        checkpoints this onto the row after every stage and every LLM call, so an
        author can watch the pipeline work instead of watching a spinner. The final
        :meth:`finish` payload overwrites it, so a persisted trace whose
        ``finished_at`` is null means the run is live (or died without its failure
        handler — the same stuck state the row's ``status`` already shows).
        """
        return self._payload(finished_at=None)

    def finish(self, *, final: int) -> dict[str, Any]:
        self._totals["final"] = final
        return self._payload(
            finished_at=datetime.now(UTC).isoformat(timespec="seconds")
        )

    def _payload(self, *, finished_at: str | None) -> dict[str, Any]:
        # Deep-copied so a payload is a value, not a window onto this recorder:
        # a snapshot assigned to the row must not silently grow steps in memory
        # between the checkpoint that wrote it and the next.
        wall_ms = int((time.monotonic() - self._started) * 1000)
        return copy.deepcopy(
            {
                "format": FORMAT_TAG,
                "model": self._model,
                "started_at": self._started_at.isoformat(timespec="seconds"),
                "finished_at": finished_at,
                "steps": self._steps,
                "summary": {
                    "target": self._target,
                    "wall_ms": wall_ms,
                    "llm_ms": self._llm_ms,
                    "llm_calls": self._calls,
                    "llm_budget": self._budget,
                    **self._totals,
                    "per_format": self._per_format,
                },
            }
        )


def attach_trace[E: BaseException](exc: E, trace: dict[str, Any]) -> E:
    """Carry a trace on a failing exception, so the task's failure handler can store
    it. A failed run is exactly the run whose trace somebody wants to read, and the
    session it was built in is about to be rolled back."""
    exc.generation_trace = trace  # type: ignore[attr-defined]
    return exc
