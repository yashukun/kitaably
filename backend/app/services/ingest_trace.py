"""The ingest pipeline trace. Phase 3, added alongside ZIP upload (D26).

What actually ran while a book was being read: each stage with its wall-clock lap,
the archive manifest when the upload was a ZIP, and a summary. Stored on
``books.ingest_trace``, rendered by the Advanced panel on the book card — the third
of these after the tutor's pipeline panel and ``assessments.generation_trace``, and
the same reasoning each time. Ingest happens in a Celery worker long after the
upload request returned, so it is a column rather than something riding a stream.

**The content-free rule, enforced by this API's shape.** A canon book's row is
readable by every signed-in user and RLS cannot hide a column, so nothing from
inside the book may enter a trace. Every method here takes counts, durations,
enum values, member filenames and fixed reason strings; none takes page text,
chunk text, or any other extract of the document. Adding a parameter that carries
one is how a shared book would start leaking its contents to people who have not
opened it, so do not.

Step shape matches the chat and generation traces (``{step, detail, ms}``) so all
three Advanced panels read the same way.
"""

import time
from datetime import UTC, datetime
from typing import Any

# Versioned like the others, and for the same reason: a future shape change must be
# detectable by whatever was written against this one.
FORMAT_TAG = "kitaably.ingest-trace.v1"

# A pathological archive should not put a thousand filenames in a jsonb column that
# every reader of a shared book then downloads. The manifest says what it dropped.
MAX_MANIFEST_ENTRIES = 80


class IngestTrace:
    """One run's trace. Create at the top of ``_ingest``, finish once."""

    def __init__(self, *, source_format: str, byte_size: int) -> None:
        self._started = time.monotonic()
        self._lap = self._started
        self._started_at = datetime.now(UTC)
        self._source_format = source_format
        self._byte_size = byte_size
        self._steps: list[dict[str, Any]] = []
        self._manifest: list[dict[str, Any]] = []
        self._manifest_total = 0
        self._counts: dict[str, int] = {}

    # ------------------------------------------------------------------ steps

    def step(self, step: str, detail: str) -> None:
        """One stage of the pipeline. ``ms`` is the lap since the previous step, so
        the timeline's numbers sum to the wall clock rather than overlapping."""
        now = time.monotonic()
        self._steps.append(
            {"step": step, "detail": detail, "ms": int((now - self._lap) * 1000)}
        )
        self._lap = now

    def count(self, **counts: int) -> None:
        """Record summary figures — pages, chapters, chunks, vectors."""
        self._counts.update(counts)

    def manifest(self, names: list[str], *, pages: list[int] | None = None) -> None:
        """The archive's members, in the order they were read (D26).

        Filenames only. This is the whole point of the panel for a ZIP — "did it
        find all eighteen chapters, and in what order" is not answerable from a
        page count — and a name is metadata about the upload rather than an
        extract of the book.
        """
        self._manifest_total = len(names)
        kept = names[:MAX_MANIFEST_ENTRIES]
        self._manifest = [
            {"name": name, **({"pages": pages[index]} if pages and index < len(pages) else {})}
            for index, name in enumerate(kept)
        ]

    # ---------------------------------------------------------------- payload

    def finish(self, *, outcome: str, reason: str | None = None) -> dict[str, Any]:
        """``outcome`` is ``ready`` or ``failed``; ``reason`` is the same sentence
        written to ``books.error``, which the owner is already being shown."""
        return {
            "format": FORMAT_TAG,
            "source_format": self._source_format,
            "started_at": self._started_at.isoformat(timespec="seconds"),
            "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "steps": self._steps,
            "manifest": self._manifest,
            "manifest_total": self._manifest_total,
            "summary": {
                "outcome": outcome,
                "reason": reason,
                "byte_size": self._byte_size,
                "wall_ms": int((time.monotonic() - self._started) * 1000),
                **self._counts,
            },
        }


def attach_trace[E: BaseException](exc: E, trace: dict[str, Any]) -> E:
    """Carry a trace on a failing exception so the task's failure handler can store
    it. A failed run is exactly the run whose trace somebody wants to read, and the
    recorder it was built in is about to go out of scope."""
    exc.ingest_trace = trace  # type: ignore[attr-defined]
    return exc
