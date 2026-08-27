"""The ingest trace: content-free by construction, and owner-only on the wire.

Two properties, both privacy ones. A canon book's row is readable by every signed-in
user and RLS cannot hide a column, so the trace must never carry anything from inside
the book — and the serializer must not hand somebody else's processing detail to a
reader who merely opened the shared library.
"""

from datetime import UTC, datetime
from uuid import uuid4

from app.api.v1.books import _to_read
from app.core.security import Principal
from app.db.models.book import Book
from app.db.models.enums import BookScope, BookStatus, Role, SourceFormat
from app.services.ingest_trace import MAX_MANIFEST_ENTRIES, IngestTrace


def _principal(user_id) -> Principal:
    return Principal(id=user_id, role=Role.USER, email="reader@kitaably.test")


def _book(owner_id, **overrides) -> Book:
    defaults = dict(
        id=uuid4(),
        owner_id=owner_id,
        scope=BookScope.CANON,
        title="Shared textbook",
        author=None,
        source_format=SourceFormat.ZIP,
        storage_path=f"{owner_id}/book/source.zip",
        byte_size=1024,
        page_count=10,
        status=BookStatus.READY,
        error=None,
        needs_ocr=False,
        kind=None,
        genre=None,
        summary=None,
        ingest_trace={"format": "kitaably.ingest-trace.v1", "steps": []},
        created_at=datetime(2026, 8, 25, tzinfo=UTC),
        updated_at=datetime(2026, 8, 25, tzinfo=UTC),
    )
    return Book(**{**defaults, **overrides})


# --- the wire ----------------------------------------------------------------


def test_the_owner_sees_their_own_trace() -> None:
    owner = uuid4()
    read = _to_read(_book(owner), _principal(owner))
    assert read.owner_is_me is True
    assert read.ingest_trace is not None


def test_another_reader_gets_no_trace_on_a_shared_book() -> None:
    """The book is canon, so this caller may read the row. How somebody else's
    upload was processed is still not theirs to see."""
    read = _to_read(_book(uuid4()), _principal(uuid4()))
    assert read.owner_is_me is False
    assert read.ingest_trace is None


# --- the recorder ------------------------------------------------------------


def test_a_trace_carries_counts_and_timings_not_text() -> None:
    trace = IngestTrace(source_format="zip", byte_size=2048)
    trace.step("parse", "zip · 276 pages")
    trace.count(pages=276, chapters=18, chunks=410, vectors=410)
    payload = trace.finish(outcome="ready")

    assert payload["summary"]["outcome"] == "ready"
    assert payload["summary"]["chunks"] == 410
    assert payload["steps"][0]["step"] == "parse"
    assert isinstance(payload["steps"][0]["ms"], int)


def test_a_failed_run_records_its_reason_and_how_far_it_got() -> None:
    """A failed run is exactly the run whose trace somebody wants to read."""
    trace = IngestTrace(source_format="zip", byte_size=2048)
    trace.step("download", "2.0 MB from storage")
    payload = trace.finish(outcome="failed", reason="That ZIP file is damaged.")

    assert payload["summary"]["outcome"] == "failed"
    assert payload["summary"]["reason"] == "That ZIP file is damaged."
    assert len(payload["steps"]) == 1


def test_the_manifest_is_capped_but_says_the_real_total() -> None:
    """A pathological archive must not put a thousand filenames in a column every
    reader of a shared book downloads — and must not lie about how many there were."""
    trace = IngestTrace(source_format="zip", byte_size=2048)
    names = [f"ch{index}.pdf" for index in range(MAX_MANIFEST_ENTRIES + 25)]
    trace.manifest(names)
    payload = trace.finish(outcome="ready")

    assert len(payload["manifest"]) == MAX_MANIFEST_ENTRIES
    assert payload["manifest_total"] == MAX_MANIFEST_ENTRIES + 25


def test_the_manifest_preserves_reading_order() -> None:
    trace = IngestTrace(source_format="zip", byte_size=2048)
    trace.manifest(["jemh101.pdf", "jemh102.pdf", "jemh110.pdf"])
    payload = trace.finish(outcome="ready")

    assert [entry["name"] for entry in payload["manifest"]] == [
        "jemh101.pdf",
        "jemh102.pdf",
        "jemh110.pdf",
    ]
