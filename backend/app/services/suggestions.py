"""What to suggest when somebody picks a book. Phase 7b.

Two blank boxes this fills: the chat input, and the assessment form's title and focus
fields. In both cases the server already knows enough to propose something — the book
prints its own exercise questions, and ingest already detected its chapter titles.

**Nothing here calls a model**, and that is the load-bearing decision rather than a
performance note. Three reasons, in order of how much they matter:

1. A suggested question the book cannot answer is *worse than no suggestion*. Clicking
   it produces the grounded refusal (invariant 5) that suggestions exist to reduce, and
   it does so having promised the opposite. Harvested questions cannot have this failure
   — the book printed them, so the book covers them.
2. ``rag/harvest.py`` already makes the same argument for the same reason: a model asked
   "are there questions in this passage" says yes to prose. Detection is regex, and the
   evidence is the book's own numbering.
3. Latency. A suggestion that arrives after the reader has finished typing is not a
   suggestion, and an Ollama call on this hardware is one to two minutes.

**Three tiers, degrading honestly.** The book's own exercises, then chapter titles, then
nothing. The last one is a real answer: a book with no outline gets a synthetic
``Chapter(index=0, title="Full document")`` from ``rag/chunk.py``, and inventing a
suggestion from that is how a reader is pointed at material that is not there.

Scope, as everywhere that touches chunks: the predicate comes from
``rag/retrieve.py :: build_retrieval_filter`` with the caller's own principal, and never
from anything in a request. ``chapters`` carries no ``owner_id``/``scope`` of its own —
unlike ``chunks``, which the D15 trigger denormalises — so a chapter query is scoped
through its book instead.
"""

import logging
import re
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Principal
from app.db.models.assessment import Assessment
from app.db.models.book import Book, Chapter
from app.db.models.feedback import ContentFeedback
from app.rag import harvest
from app.rag.brief import usable_topic
from app.rag.retrieve import build_retrieval_filter, search_chunks_lexical

logger = logging.getLogger(__name__)

# How a book says "what follows is a question set", in the words the chunks contain.
# A lexical search rather than a coverage sample, and the difference matters: an
# exercise block sits at the END of a chapter, and `fetch_coverage_chunks` takes one
# chunk per ntile in reading order -- so a stratified sample walks straight past the
# very chunks this wants. Searching for the heading's own vocabulary finds them.
_EXERCISE_QUERY = "exercises questions problems"

# Questions the tutor cannot answer, however faithfully the book printed them.
# Harvesting deliberately does not judge this (its header says so) because a paper
# CAN carry "draw a labelled diagram" -- somebody sits it with a pencil. A chat
# suggestion cannot: it promises an answer, and the tutor has no hands.
_NEEDS_HANDS = re.compile(
    r"\b(?:draw|sketch|plot|label(?:\s+the)?|construct|measure|trace|colour|color"
    r"|paste|cut\s+out|collect|perform\s+the\s+experiment|in\s+your\s+notebook"
    r"|on\s+graph\s+paper)\b",
    re.IGNORECASE,
)

# A suggestion is a chip somebody reads at a glance, not a paragraph.
_MAX_SUGGESTION_CHARS = 140

# Enough numbered items to believe the book meant them as a set (mirrors the
# assessment side's `assessment_min_harvest_questions`, kept local because this is a
# different judgement about a different surface).
_MIN_EXERCISE_ITEMS = 2

# How many exercise-bearing chunks to look at, and how many suggestions to return.
_CHUNK_CANDIDATES = 8
_MAX_SUGGESTIONS = 5


def _usable_as_a_suggestion(text: str) -> bool:
    """Can the tutor actually answer this from the material?"""
    if len(text) > _MAX_SUGGESTION_CHARS:
        return False
    return not _NEEDS_HANDS.search(text)


def _deduped(items: list[str], *, limit: int) -> list[str]:
    """Case-insensitive, order-preserving, capped."""
    seen: set[str] = set()
    kept: list[str] = []
    for item in items:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        kept.append(item)
        if len(kept) >= limit:
            break
    return kept


async def titled_chapters(
    session: AsyncSession, book_ids: list[UUID]
) -> dict[UUID, list[str]]:
    """Chapter titles per book, in reading order, for books that really have an outline.

    Books whose only chapter is the synthetic whole-document one are skipped, and so
    are books with a single chapter — the same ``< 2`` rule ``chat.py :: _outline_for``
    applies, for the same reason. "Full document" is not a topic anybody chose.

    No scope predicate here: every caller passes ids it has already authorised, and
    RLS on ``books`` is what makes an unauthorised id return nothing rather than an
    error. Chapters carry no scope column of their own to filter on.
    """
    if not book_ids:
        return {}

    rows = list(
        await session.scalars(
            select(Chapter)
            .where(Chapter.book_id.in_(book_ids))
            .order_by(Chapter.book_id, Chapter.index)
        )
    )

    by_book: dict[UUID, list[str]] = {}
    for chapter in rows:
        if chapter.title:
            by_book.setdefault(chapter.book_id, []).append(chapter.title)

    return {
        book_id: titles for book_id, titles in by_book.items() if len(titles) >= 2
    }


async def questions_for_book(
    session: AsyncSession, principal: Principal, book_id: UUID
) -> list[str]:
    """Questions worth asking about one book. Empty when there is nothing honest to say.

    Tier one is the book's own printed exercises, verbatim. Tier two turns chapter
    titles into openers. Tier three is nothing at all, which is the correct answer for
    a novel with no outline and no exercises.
    """
    chunks = await search_chunks_lexical(
        session,
        _EXERCISE_QUERY,
        build_retrieval_filter(principal),
        top_k=_CHUNK_CANDIDATES,
        book_ids=[book_id],
    )

    harvested: list[str] = []
    for chunk, _score in chunks:
        text = chunk.text
        # `carries_questions` first: it is the check that the BOOK said these are
        # questions -- a heading, or numbering -- rather than the regex having noticed
        # a question mark in prose. Without it a self-help book suggests "Would you
        # marry yourself?" (the real result that rule was written against).
        if not harvest.carries_questions(text, minimum=_MIN_EXERCISE_ITEMS):
            continue
        harvested += [
            question.text
            for question in harvest.find_questions(text)
            if _usable_as_a_suggestion(question.text)
        ]

    if harvested:
        return _deduped(harvested, limit=_MAX_SUGGESTIONS)

    outlines = await titled_chapters(session, [book_id])
    return _deduped(
        [f"What does “{title}” cover?" for title in outlines.get(book_id, [])],
        limit=_MAX_SUGGESTIONS,
    )


async def for_assessment(
    session: AsyncSession, principal: Principal, book_ids: list[UUID], clause
) -> dict[str, list[str]]:
    """Titles and focus topics for a paper drawn from these books.

    ``clause`` is the caller's source predicate — ``assessments.draft_source_clause``
    — passed in rather than imported, so this module does not have to know which of
    the two scope rules applies. Generation's is narrower than a reader's (D29), and
    a suggestion must not name a book the paper could not actually draw from.

    Topics are validated through ``brief.usable_topic``, which is the reuse that makes
    them worth anything: the focus box is parsed back by ``brief.read()``, so a
    suggestion its own parser would discard is a suggestion that quietly does nothing
    when clicked.
    """
    if not book_ids:
        return {"titles": [], "topics": []}

    books = list(
        await session.scalars(
            select(Book).where(Book.id.in_(book_ids)).where(clause)
        )
    )
    if not books:
        return {"titles": [], "topics": []}

    outlines = await titled_chapters(session, [book.id for book in books])

    titles: list[str] = []
    topics: list[str] = []
    for book in books:
        titles.append(book.title)
        for title in outlines.get(book.id, []):
            topic = usable_topic(title)
            if topic is None:
                continue
            topics.append(topic)
            titles.append(f"{topic} — chapter test")

    return {
        "titles": _deduped(titles, limit=_MAX_SUGGESTIONS),
        "topics": _deduped(topics, limit=_MAX_SUGGESTIONS * 2),
    }


async def record_gap(
    session: AsyncSession,
    principal: Principal,
    *,
    source: str,
    message_id: UUID | None,
    assessment_id: UUID | None,
    question: str,
    book_ids: list[UUID],
    outcome: str,
    note: str | None,
) -> ContentFeedback:
    """File a report that the app failed somebody, with what the app knew at the time.

    ``user_id`` comes from the principal and never from the request. The insert policy
    checks the same thing independently, so a body claiming somebody else's id is
    refused twice — but the first refusal should be here, where the caller's identity
    is a fact rather than a claim.

    **Diagnostics are read server-side, never accepted.** For a generation failure the
    summary comes off the assessment's own stored trace: call counts, timings, and the
    failure tags that say whether the model server answered at all. That is the half
    that makes a report investigable — a user can say "it failed", but only the trace
    can say every call returned 404 because a model was never pulled. Taking it from
    the client instead would let a report carry a story the app never told.

    Book ids are stored as text so the jsonb containment the owner-side read policy
    does has something to match.
    """
    diagnostics: dict[str, Any] = {}
    if assessment_id is not None:
        assessment = await session.scalar(
            select(Assessment).where(Assessment.id == assessment_id)
        )
        # RLS already decided whether this caller may see the paper, so a miss here is
        # "not yours or not there" and the report is filed without diagnostics rather
        # than refused: the complaint is still worth having.
        if assessment is not None:
            trace = assessment.generation_trace or {}
            diagnostics = {
                "model": trace.get("model"),
                "summary": trace.get("summary"),
                "error": assessment.error,
                "generation_note": assessment.generation_note,
            }

    row = ContentFeedback(
        user_id=principal.id,
        source=source,
        message_id=message_id,
        assessment_id=assessment_id,
        question=question.strip(),
        book_ids=[str(book_id) for book_id in book_ids],
        outcome=outcome,
        note=(note or "").strip() or None,
        diagnostics=diagnostics,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    logger.info(
        "failure reported (source=%s, outcome=%s): books=%d, diagnostics=%s",
        source,
        outcome,
        len(book_ids),
        "yes" if diagnostics else "no",
    )
    return row
