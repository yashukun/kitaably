"""Re-ingest every book. An operator action, not a route.

Run after anything that changes what a stored vector *means* or how a passage is cut:

    CHUNK_TOKENS / CHUNK_OVERLAP_TOKENS   the passages change shape
    EMBEDDING_MODEL / EMBEDDING_DIM       every existing vector becomes incomparable

Deliberately not an API endpoint. Re-chunking the whole library is a schema-shaped
change to the index rather than something a reader does to their own book, and
``books.retry_ingest`` refuses anything that is not ``failed`` on purpose — two
workers writing chunks for one book is the exact race that guard exists to prevent.

Safe to run on a live system. Each book is queued as an ordinary ``ingest_book``
task, which deletes that book's prior chunks inside the same transaction that writes
the new ones, so a book is answerable from its old passages right up until it is
answerable from its new ones. There is no window where it has none.

    make reingest
    docker compose exec worker python -m app.workers.reingest
"""

import asyncio
import sys

from sqlalchemy import select

from app.db.models import Book
from app.db.models.enums import BookStatus
from app.db.session import WorkerSessionFactory


async def _book_ids() -> list[tuple[str, str]]:
    """Every book worth re-reading, oldest first.

    Skips books that never finished: one still parsing already has a task in flight,
    and queueing a second is the double-write this script otherwise avoids. A
    ``failed`` book is skipped too — it has a reason on it that its owner should see
    and act on, and quietly re-queueing it would erase that.
    """
    async with WorkerSessionFactory() as session:
        rows = await session.execute(
            select(Book.id, Book.title)
            .where(Book.status == BookStatus.READY)
            .order_by(Book.created_at)
        )
        return [(str(row.id), row.title) for row in rows]


def main() -> int:
    from app.workers.tasks.ingest import ingest_book

    books = asyncio.run(_book_ids())
    if not books:
        print("No ready books to re-ingest.")
        return 0

    print(f"Queueing {len(books)} book(s) for re-ingest:")
    for book_id, title in books:
        ingest_book.delay(book_id)
        print(f"  queued  {book_id}  {title}")

    print("\nWatch progress with:  make logs  (or the Books page, which polls).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
