"""Grounded chat over retrieved material. Phase 4.

A turn is decided before a single token is streamed. That ordering is forced by
something specific: **the RLS context is transaction-local**, and the request's
database session is closed by its dependency the moment the route function returns.
The generator that streams the answer therefore has no database, so everything the
answer depends on -- the transcript, the intent, the retrieval, the book vote, the
titles behind the citations -- is gathered in :func:`prepare_turn` while there is
still a session to gather it with.

The pipeline:

    classify  is this even a question, or is it "hi"?
       |         not a question -> a fixed friendly reply, no embedding, no LLM
    condense  "explain that again" -> "explain how enzymes lower activation energy"
       |         only for follow-ups, and only against this reader's own transcript
     shape    HOW does this question want its material gathered? (D22)
       |         focused   -> vector search, the original path
       |         overview  -> "summarize this book": coverage sample, reading order
       |         lookup    -> "find every mention of X": lexical + vector, fused
       |         compare   -> "compare these books": per-book quota, routing off
    retrieve  under build_retrieval_filter -- the chokepoint, whatever the shape
       |         nothing found -> a grounded refusal (or, for a mention question,
       |         an honest "no mention found" -- which IS the answer), no LLM call
      rank    drop overlap, vote a dominant book, spread across pages
       |
     answer   the tutor prompt, streamed
"""

import json
import logging
import re
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients import embeddings, llm
from app.core.config import settings
from app.core.errors import NotFound, ValidationFailed
from app.core.metrics import (
    chat_query_shapes_total,
    chat_routed_total,
    retrieval_refusals_total,
    retrieval_results_total,
)
from app.core.security import Principal
from app.db.models import Book, Chapter, Chunk
from app.db.models.chat import ChatMessage, ChatSession
from app.db.models.enums import BookStatus, MessageIntent, MessageRole
from app.db.session import WorkerSessionFactory
from app.rag import intent as intent_rules
from app.rag import prompts, rank, trim
from app.rag.retrieve import (
    build_retrieval_filter,
    fetch_coverage_chunks,
    search_chunks,
    search_chunks_corroborated,
    search_chunks_lexical,
    significant_terms,
)
from app.rag.shape import QueryProfile, QueryShape
from app.rag.shape import classify as classify_shape

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------- sessions


async def create_session(
    session: AsyncSession, principal: Principal, title: str | None
) -> ChatSession:
    """Open a conversation.

    There is nothing to authorize beyond being signed in: a conversation carries no
    scope of its own, and what it can reach is recomputed from the principal on
    every question (:func:`retrieve_for`). That is deliberate — a scope frozen onto
    the session at creation would keep answering from material the caller has since
    lost access to.
    """
    chat = ChatSession(user_id=principal.id, title=title)
    session.add(chat)
    await session.flush()
    await session.refresh(chat)
    return chat


async def list_sessions(session: AsyncSession, principal: Principal) -> list[ChatSession]:
    """The caller's conversations, most recently *used* first.

    Ordered by ``last_message_at`` rather than ``created_at``: a conversation you
    returned to yesterday belongs above one you opened last week and abandoned.
    """
    return list(
        await session.scalars(
            select(ChatSession).order_by(ChatSession.last_message_at.desc()).limit(50)
        )
    )


async def get_session(
    session: AsyncSession, principal: Principal, chat_session_id: UUID
) -> ChatSession:
    chat = await session.scalar(select(ChatSession).where(ChatSession.id == chat_session_id))
    if chat is None:
        # RLS already hid anything that is not the caller's own conversation.
        raise NotFound("That conversation does not exist.")
    return chat


async def list_messages(
    session: AsyncSession, principal: Principal, chat_session_id: UUID
) -> list[ChatMessage]:
    await get_session(session, principal, chat_session_id)
    return list(
        await session.scalars(
            select(ChatMessage)
            .where(ChatMessage.session_id == chat_session_id)
            .order_by(ChatMessage.created_at.asc())
        )
    )


async def recent_history(
    session: AsyncSession, chat_session_id: UUID, *, turns: int
) -> list[prompts.Message]:
    """The last few messages, oldest first, as the prompt templates want them.

    Fetched newest-first with a LIMIT and then reversed, so a long conversation costs
    one small query rather than loading a transcript that grows without bound.
    """
    rows = list(
        await session.scalars(
            select(ChatMessage)
            .where(ChatMessage.session_id == chat_session_id)
            .order_by(ChatMessage.created_at.desc())
            .limit(turns)
        )
    )
    return [{"role": row.role.value, "content": row.content} for row in reversed(rows)]


# -------------------------------------------------------------------- the turn


@dataclass(frozen=True, slots=True)
class Turn:
    """Everything about one question, decided while a database session still exists.

    ``answer`` being set means the turn is already finished: a greeting, a refusal, or
    a question about the library itself. Those make no claim about anybody's material,
    so there is nothing to ground and no model call to make (CLAUDE.md invariant 5).
    """

    intent: MessageIntent
    # What the reader typed, verbatim. This is what the tutor is asked to answer.
    asked: str
    # What was embedded. Identical to `asked` except for a follow-up, where the
    # reference has been resolved against the transcript so it retrieves on a subject
    # instead of on the word "that".
    query: str
    retrieved: list[dict[str, Any]] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    votes: list[rank.BookVote] = field(default_factory=list)
    kind: str | None = None
    answer: str | None = None
    history: list[prompts.Message] = field(default_factory=list)
    # Set for the non-focused shapes (app/rag/shape.py): an instruction block
    # telling the model what its sources ARE — a cross-section, a mention list, a
    # per-book selection — and an optional chapter outline for orientation. Both
    # refine the prompt; neither loosens grounding.
    task: str | None = None
    outline: str | None = None
    # What the pipeline actually did, for the transcript's "Advanced" disclosure:
    # intent, shape, each stage with its timing, the book vote, the outcome. Sent
    # once over the SSE stream and deliberately NOT persisted — the transcript is
    # the record of the conversation, not of the machinery, and everything in here
    # is the caller's own retrieval over material they can already see.
    trace: dict[str, Any] | None = None


class _Stopwatch:
    """Wall-clock laps for the pipeline trace. Milliseconds, monotonic."""

    __slots__ = ("_last",)

    def __init__(self) -> None:
        self._last = time.perf_counter()

    def lap_ms(self) -> int:
        now = time.perf_counter()
        elapsed = int(round((now - self._last) * 1000))
        self._last = now
        return elapsed


def _step(steps: list[dict[str, Any]], watch: _Stopwatch, step: str, detail: str) -> None:
    steps.append({"step": step, "detail": detail, "ms": watch.lap_ms()})


def _trace(
    detected: MessageIntent,
    profile: QueryProfile | None,
    steps: list[dict[str, Any]],
    *,
    outcome: str,
    query: str | None = None,
    topic: str | None = None,
    books: list[dict[str, Any]] | None = None,
    sources: int = 0,
) -> dict[str, Any]:
    """The pipeline, as one JSON-friendly report.

    ``outcome`` names how the turn ended — ``answered``, ``refusal``,
    ``no_mentions``, ``pick_book``, ``needs_two_books``, ``conversational`` — so
    the UI can label the run without parsing step text. ``query`` is set only when
    retrieval ran on something other than what was typed (a condensed follow-up),
    because "we searched for something else" is exactly the kind of fact this
    exists to surface.
    """
    return {
        "intent": detected.value,
        "shape": profile.shape.value if profile else None,
        "topic": topic,
        "query": query,
        "steps": steps,
        "books": books or [],
        "sources": sources,
        "outcome": outcome,
    }


def _clip(text: str, limit: int = 90) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


async def _visible_books(session: AsyncSession, principal: Principal) -> list[Book]:
    """The books this caller could be answered from — their own plus the shared pool.

    Which rows come back is decided by RLS, exactly as in ``books.list_books``. Only
    ``ready`` books count: one still embedding cannot answer anything, and naming it
    in a refusal ("I looked through X") would be a lie about what was searched.
    """
    return list(
        await session.scalars(
            select(Book).where(Book.status == BookStatus.READY).order_by(Book.title)
        )
    )


async def condense(question: str, history: list[prompts.Message]) -> str:
    """Rewrite a follow-up into a question that can be embedded on its own.

    "Explain that again" carries no subject, so it retrieves on the word "that" and
    comes back with noise or nothing. This resolves the reference against the
    transcript before the embedding is taken.

    Never raises and never returns empty. Every failure path falls back to gluing the
    previous question onto this one, which is crude but keeps the real subject in the
    embedded text — and a slightly worse retrieval beats an error on a valid question.
    """
    fallback = question
    for turn in reversed(history):
        if turn["role"] == MessageRole.USER.value:
            fallback = f"{turn['content']} {question}"
            break

    # The model is off by default (DECISIONS.md D21). Condensation sits on the
    # critical path *before* retrieval, so on a CPU model it is tens of seconds spent
    # turning "explain that again" into a sentence -- and the fallback above already
    # puts the real subject into the embedded text by gluing the previous question
    # on. It retrieves nearly as well, and it costs a dictionary lookup.
    if not settings.chat_condense_llm:
        return fallback

    try:
        rewritten = (await llm.complete(prompts.condense_prompt(question, history))).strip()
    except Exception as exc:  # noqa: BLE001 -- a broken condenser must not break chat
        logger.warning("condensation unavailable", extra={"error": str(exc)})
        return fallback

    rewritten = rewritten.strip().strip('"').strip()
    # A condenser that returned an essay, a refusal, or nothing has misunderstood the
    # job. Length is the cheapest tell and does not need the model to cooperate.
    if not rewritten or len(rewritten) > 400:
        return fallback
    return rewritten


async def prepare_turn(
    session: AsyncSession,
    principal: Principal,
    chat: ChatSession,
    content: str,
    *,
    book_ids: list[UUID] | None = None,
) -> Turn:
    """Decide everything about this turn while a database session still exists."""
    watch = _Stopwatch()
    steps: list[dict[str, Any]] = []

    history = await recent_history(
        session, chat.id, turns=settings.chat_history_turns
    )

    # The previous turn asked "Which book do you mean?" — a clarifying question
    # is a two-turn contract, and a reply that names a book (or says "all my
    # books") is the missing parameter, not a new topic. The ORIGINAL question is
    # resumed, narrowed to the selection; without this, "Epic Shit" typed in
    # answer gets embedded as a search for the phrase "epic shit", and the
    # question that prompted the ask is silently dropped. A reply that is itself
    # a full question ("what does Epic Shit say about money?") is left alone and
    # processed as the fresh question it is.
    if not book_ids and _pick_book_pending(history):
        visible = await _visible_books(session, principal)
        cleaned = intent_rules.normalise(content)
        original = _original_question(history)
        chosen: list[Book] = []
        if cleaned in _ALL_SELECTION:
            chosen = visible
        else:
            named = _match_titles(content, visible)
            if named and _is_bare_selection(content, named):
                chosen = named
        if chosen and original:
            _step(
                steps, watch, "resolve",
                f"“{_clip(content, 40)}” answers the book question — resuming "
                f"“{_clip(original, 60)}”",
            )
            content = original
            book_ids = [book.id for book in chosen]

    detected = await intent_rules.classify(content, has_history=bool(history))
    _step(steps, watch, "classify", detected.value)

    if not detected.needs_retrieval:
        return Turn(
            intent=detected,
            asked=content,
            query=content,
            history=history,
            answer=await _conversational_answer(session, principal, detected),
            trace=_trace(
                detected, None, steps,
                outcome="conversational",
            ),
        )

    query = content
    if detected is MessageIntent.FOLLOW_UP:
        query = await condense(content, history)
        _step(steps, watch, "condense", f"→ “{_clip(query)}”")
        logger.info(
            "condensed follow-up",
            extra={"original": content[:120], "condensed": query[:120]},
        )

    # How does this question want its material gathered? Rules only, offline, and
    # anything unrecognised is FOCUSED — the original path (app/rag/shape.py).
    profile = classify_shape(query)
    chat_query_shapes_total.labels(profile.shape.value).inc()
    _step(
        steps, watch, "route",
        profile.shape.value + (f" · topic “{profile.topic}”" if profile.topic else ""),
    )

    condensed = query if query != content else None

    if profile.shape is QueryShape.OVERVIEW:
        return await _prepare_overview(
            session, principal, detected, content, query, history, book_ids, profile,
            steps=steps, watch=watch,
        )
    if profile.shape is QueryShape.LOOKUP:
        return await _prepare_lookup(
            session, principal, detected, content, query, history, book_ids, profile,
            steps=steps, watch=watch,
        )
    if profile.shape is QueryShape.COMPARE:
        return await _prepare_compare(
            session, principal, detected, content, query, history, book_ids, profile,
            steps=steps, watch=watch,
        )
    if profile.shape is QueryShape.METADATA:
        turn = await _prepare_metadata(
            session, principal, detected, content, query, history, book_ids, profile,
            steps=steps, watch=watch,
        )
        if turn is not None:
            return turn
        # "Who wrote Hamlet" naming no book this library holds is not a record
        # question after all — but the TEXT might know (a history book knows who
        # wrote the Declaration), so it falls through to the content search
        # rather than refusing about a record that was never the right place.
        _step(steps, watch, "route", "no matching record — content search instead")

    hits = await retrieve_for(session, principal, query, book_ids=book_ids)
    loose = bool(hits) and all(hit.get("loose") for hit in hits)
    _step(
        steps, watch, "search",
        f"vector · {len(hits)} candidate(s) within {settings.retrieval_max_distance}"
        if not loose
        else (
            f"nothing within {settings.retrieval_max_distance} as asked; "
            f"\u201c{_clip(' '.join(hits[0].get('matched_terms') or []), 40)}\u201d "
            f"\u2192 {len(hits)} loose match(es) within "
            f"{settings.retrieval_salvage_distance}"
        ),
    )

    if not hits:
        library = await _visible_books(session, principal)
        return Turn(
            intent=detected,
            asked=content,
            query=query,
            history=history,
            answer=prompts.grounded_refusal([book.title for book in library]),
            trace=_trace(
                detected, profile, steps,
                outcome="refusal", query=condensed,
                books=[{"title": book.title} for book in library[:6]],
            ),
        )

    kept, votes = rank.narrow(hits, limit=settings.retrieval_top_k)
    _step(
        steps, watch, "rank",
        f"{len(hits)} → {len(kept)} after overlap, book vote, page spread",
    )
    if votes and votes[0].share >= settings.chat_book_dominance:
        chat_routed_total.inc()
        logger.info(
            "routed to a dominant book",
            extra={
                "book": votes[0].title,
                "share": round(votes[0].share, 3),
                "considered": [f"{v.title}:{round(v.share, 2)}" for v in votes[:4]],
            },
        )

    return Turn(
        intent=detected,
        asked=content,
        query=query,
        retrieved=kept,
        citations=to_citations(kept),
        votes=votes,
        kind=kept[0]["kind"] if kept else None,
        history=history,
        # Only when every source came from the salvage tier. A single strict hit
        # among them means the question was answered, not scraped together, and
        # telling the model to hedge would make a good answer sound unsure.
        task=prompts.loose_match_task() if loose else None,
        trace=_trace(
            detected, profile, steps,
            outcome="loose" if loose else "answered",
            query=condensed, sources=len(kept),
            books=[
                {"title": v.title, "chunks": v.chunks, "share": round(v.share, 2)}
                for v in votes[:6]
            ],
        ),
    )


# ------------------------------------------------------- the other three shapes
#
# Same contract as the focused path: everything is decided while the session is
# open, every chunk was fetched under build_retrieval_filter, and a turn that
# comes back with `answer` set makes no model call at all.

# Chunks below this are page furniture — headers, captions, stray numerals — and
# sampling one into a book's cross-section wastes a source slot on nothing.
_COVERAGE_MIN_TOKENS = 60


def _match_titles(text: str, books: list[Book]) -> list[Book]:
    """Books whose title is named in the text. Case-insensitive containment;
    titles shorter than four characters are skipped so a book called "It" does
    not match half the English language."""
    lowered = text.lower()
    return [book for book in books if len(book.title) >= 4 and book.title.lower() in lowered]


def _pick_book_pending(history: list[prompts.Message]) -> bool:
    """Whether the previous turn was the tutor asking which book.

    Detected by the reply's own heading — fixed copy this codebase owns
    (:data:`prompts.PICK_BOOK_HEADING`), so the check is exact, not a guess about
    model output.
    """
    return bool(history) and history[-1]["role"] == MessageRole.ASSISTANT.value and history[
        -1
    ]["content"].startswith(prompts.PICK_BOOK_HEADING)


def _original_question(history: list[prompts.Message]) -> str | None:
    """The question that triggered the clarifying reply: the last thing the
    reader asked before the trailing assistant turn(s)."""
    seen_assistant = False
    for turn in reversed(history):
        if turn["role"] == MessageRole.ASSISTANT.value:
            seen_assistant = True
            continue
        if seen_assistant:
            return turn["content"]
    return None


# Words that carry no selection: complaints about being asked again, politeness,
# and the noise around a bare title. What survives their removal is what the
# message actually adds beyond naming a book.
_SELECTION_FILLER = re.compile(
    r"\bwhy (?:are|r) (?:you|u) asking(?: me)?(?: again| twice)?\b"
    r"|\bi (?:already |just )?(?:said|told you|answered|named|picked|chose) (?:it|that|this)?\b"
    r"|\bas i said\b"
    r"|\b(?:again|please|pls|ok|okay|just|then|so|well|obviously|its|it's|it)\b"
    r"|\b(?:the|this|that|one|book|first|second)\b"
)

# The invited "cover them all" answers, verbatim-ish.
_ALL_SELECTION = frozenset(
    {"all", "all my books", "all of them", "all the books", "all books", "both",
     "both of them", "everything"}
)


def _is_bare_selection(content: str, chosen: list[Book]) -> bool:
    """Whether this message is only an answer to "which book?", not a new question.

    Strip the named title(s) and the filler around them; if almost nothing
    remains, the message was the selection. "Why are you asking again. Epic Shit"
    passes — the complaint is filler. "What does Epic Shit say about money?"
    fails, and is correctly processed as the fresh question it is.
    """
    text = intent_rules.normalise(content)
    for book in chosen:
        text = text.replace(book.title.lower(), " ")
    text = _SELECTION_FILLER.sub(" ", text)
    return len(re.findall(r"[a-z0-9]+", text)) <= 2


def _book_from_conversation(
    history: list[prompts.Message], candidates: list[Book]
) -> Book | None:
    """The book this conversation is already about, if the reader has named one.

    Scans the reader's own recent messages, newest first, for a candidate title —
    so "what is the actual title of the book?", asked right after the reader said
    "Epic Shit", resolves instead of re-asking a question that was just answered.
    Assistant turns are deliberately skipped: the ask-which reply names *every*
    book, which is the opposite of a selection. A user message naming several
    books settles nothing and stops the scan — ambiguity is not resolved by
    guessing among what they listed.
    """
    for turn in reversed(history):
        if turn["role"] != MessageRole.USER.value:
            continue
        matched = _match_titles(turn["content"], candidates)
        if len(matched) == 1:
            return matched[0]
        if matched:
            return None
    return None


async def _target_books(
    session: AsyncSession,
    principal: Principal,
    query: str,
    book_ids: list[UUID] | None,
) -> tuple[list[Book], list[Book], bool]:
    """Which books this whole-book or cross-book question is about.

    Returns ``(targets, visible, narrowed)``. Resolution order: an explicit
    ``book_ids`` selection wins; then a book *named by title* in the question
    ("compare Deep Work and Atomic Habits…"); else the whole visible library.
    ``narrowed`` records whether the reader pointed at specific books — an
    absence of hits from a book they named is worth reporting, an absence from
    book eleven of a library sweep is not.

    Everything resolves against ``_visible_books``, so a ``book_id`` or a title
    the caller may not read simply fails to match — a request, never an
    authorization, exactly like the ``book_ids`` narrowing it extends.
    """
    visible = await _visible_books(session, principal)
    if book_ids:
        requested = set(book_ids)
        return [book for book in visible if book.id in requested], visible, True

    named = _match_titles(query, visible)
    if named:
        return named, visible, True
    return visible, visible, False


async def _outline_for(
    session: AsyncSession, books: list[Book], *, max_chapters: int = 25
) -> str | None:
    """Chapter titles of the given books, for the prompt's orientation block.

    Books whose only chapter is the synthetic whole-document one are skipped — an
    outline of "1. (untitled)" tells the model nothing and spends prompt tokens
    saying it.
    """
    rows = list(
        await session.scalars(
            select(Chapter)
            .where(Chapter.book_id.in_([book.id for book in books]))
            .order_by(Chapter.book_id, Chapter.index)
        )
    )
    by_book: dict[UUID, list[Chapter]] = {}
    for chapter in rows:
        by_book.setdefault(chapter.book_id, []).append(chapter)

    blocks = []
    for book in books:
        titled = [chapter for chapter in by_book.get(book.id, []) if chapter.title]
        if len(titled) < 2:
            continue
        lines = [
            f"  {position}. {chapter.title}"
            + (f" (p. {chapter.page_start})" if chapter.page_start else "")
            for position, chapter in enumerate(titled[:max_chapters], 1)
        ]
        if len(titled) > max_chapters:
            lines.append(f"  … and {len(titled) - max_chapters} more")
        blocks.append(f"{book.title}:\n" + "\n".join(lines))
    return "\n".join(blocks) if blocks else None


async def _prepare_metadata(
    session: AsyncSession,
    principal: Principal,
    detected: MessageIntent,
    content: str,
    query: str,
    history: list[prompts.Message],
    book_ids: list[UUID] | None,
    profile: QueryProfile,
    *,
    steps: list[dict[str, Any]],
    watch: _Stopwatch,
) -> Turn | None:
    """"Who wrote this book?", "how many pages is it?" — the record, not the text.

    The answer lives on the ``books`` row, which this process is already holding,
    so there is no search and no model call: retrieval over chunk text literally
    cannot contain it, which is why these questions used to end in an honest
    refusal about a fact the server knew (the same failure D19 fixed for
    greetings, one layer up).

    Returns ``None`` — meaning *fall through to the content search* — when the
    question names a work the library does not hold: "who wrote Hamlet" is only a
    record question if a book called Hamlet is actually here; otherwise the text
    is the right place to look, and a history book may well know.
    """
    targets, visible, narrowed = await _target_books(session, principal, query, book_ids)

    if not targets:
        return Turn(
            intent=detected, asked=content, query=query, history=history,
            answer=prompts.grounded_refusal([book.title for book in visible]),
            trace=_trace(detected, profile, steps, outcome="refusal"),
        )

    # A named work with no matching record, and no "this book" to resolve: not
    # ours to answer from metadata. The caller runs the focused search instead.
    if not narrowed and not profile.single_book and not profile.all_books:
        return None

    if profile.single_book and not narrowed and len(targets) > 1:
        # The conversation may already have pinned the book — a reader who named
        # one two turns ago should not be asked again.
        recalled = _book_from_conversation(history, targets)
        if recalled is not None:
            _step(steps, watch, "resolve", f"“this book” = “{recalled.title}”, named earlier")
            targets = [recalled]
        else:
            _step(
                steps, watch, "record",
                f"“this book” is ambiguous across {len(targets)} — asking which",
            )
            return Turn(
                intent=detected, asked=content, query=query, history=history,
                answer=prompts.pick_book_reply([book.title for book in targets]),
                trace=_trace(
                    detected, profile, steps, outcome="pick_book",
                    books=[{"title": book.title} for book in targets[:6]],
                ),
            )

    if profile.fact == "chapters":
        return await _chapters_turn(
            session, detected, content, query, history, profile, targets,
            steps=steps, watch=watch,
        )

    _step(
        steps, watch, "record",
        f"{profile.fact or 'title'} · {len(targets)} book(s) · from the library "
        "record, no search",
    )
    rows = [
        {
            "title": book.title,
            "author": book.author,
            "genre": book.genre,
            "kind": book.kind.value if book.kind else None,
            "pages": book.page_count,
        }
        for book in targets
    ]
    return Turn(
        intent=detected,
        asked=content,
        query=query,
        history=history,
        answer=prompts.book_facts_reply(profile.fact or "title", rows),
        trace=_trace(
            detected, profile, steps,
            outcome="book_facts", topic=profile.topic,
            books=[{"title": book.title} for book in targets[:6]],
        ),
    )


async def _chapters_turn(
    session: AsyncSession,
    detected: MessageIntent,
    content: str,
    query: str,
    history: list[prompts.Message],
    profile: QueryProfile,
    targets: list[Book],
    *,
    steps: list[dict[str, Any]],
    watch: _Stopwatch,
) -> Turn:
    """"How many chapters, and what are they called?" — from ``chapters``.

    The one metadata fact that is not a ``books`` column. It is still the same
    kind of fact: rows this transaction can read, holding an answer no passage
    contains, which the focused path used to refuse about (see
    ``prompts.chapters_reply``).

    A book's synthetic whole-document chapter is filtered out here rather than
    reported as chapter one. ``rag/chunk.py`` emits exactly one untitled-ish
    chapter spanning the whole book when the upload carried no outline it could
    trust, and listing that back as "1. Full document" would dress an absence up
    as structure.
    """
    rows = list(
        await session.scalars(
            select(Chapter)
            .where(Chapter.book_id.in_([book.id for book in targets]))
            .order_by(Chapter.book_id, Chapter.index)
        )
    )
    by_book: dict[UUID, list[Chapter]] = {}
    for chapter in rows:
        by_book.setdefault(chapter.book_id, []).append(chapter)

    listed: list[dict[str, Any]] = []
    for book in targets:
        chapters = by_book.get(book.id, [])
        # One chapter is the whole-document fallback, not a table of contents.
        real = chapters if len(chapters) > 1 else []
        listed.append(
            {
                "title": book.title,
                "pages": book.page_count,
                "chapters": [
                    {
                        "title": chapter.title,
                        "page_start": chapter.page_start,
                        "page_end": chapter.page_end,
                    }
                    for chapter in real
                ],
            }
        )

    found = sum(len(book["chapters"]) for book in listed)
    _step(
        steps, watch, "record",
        f"chapters · {found} across {len(targets)} book(s) · from the library "
        "record, no search",
    )
    return Turn(
        intent=detected,
        asked=content,
        query=query,
        history=history,
        answer=prompts.chapters_reply(listed),
        trace=_trace(
            detected, profile, steps, outcome="book_facts",
            books=[{"title": book.title} for book in targets[:6]],
        ),
    )


async def _prepare_overview(
    session: AsyncSession,
    principal: Principal,
    detected: MessageIntent,
    content: str,
    query: str,
    history: list[prompts.Message],
    book_ids: list[UUID] | None,
    profile: QueryProfile,
    *,
    steps: list[dict[str, Any]],
    watch: _Stopwatch,
) -> Turn:
    """"Summarize this book", "key lessons from all my books" — the whole, not a part.

    No vector search happens at all: there is no subject to embed, and a summary
    grounded in the five nearest chunks to the word "summarize" is grounded in
    noise. The evidence is a coverage sample in reading order — the same insight
    that keeps a generated paper from asking about one section five times (D12) —
    plus the chapter outline, so "which chapters matter" has the actual chapters
    in front of it.
    """
    targets, visible, narrowed = await _target_books(session, principal, query, book_ids)

    if not targets:
        return Turn(
            intent=detected, asked=content, query=query, history=history,
            answer=prompts.grounded_refusal([book.title for book in visible]),
            trace=_trace(detected, profile, steps, outcome="refusal"),
        )

    # "This book", several visible, nothing selected: asking beats guessing, and
    # averaging twelve books into one summary answers a question nobody asked.
    if profile.single_book and not narrowed and len(targets) > 1:
        # Same conversational memory as the record path: only ask when the
        # transcript has not already answered.
        recalled = _book_from_conversation(history, targets)
        if recalled is not None:
            _step(steps, watch, "resolve", f"“this book” = “{recalled.title}”, named earlier")
            targets = [recalled]
        else:
            _step(
                steps, watch, "scope",
                f"“this book” is ambiguous across {len(targets)} — asking which",
            )
            return Turn(
                intent=detected, asked=content, query=query, history=history,
                answer=prompts.pick_book_reply([book.title for book in targets]),
                trace=_trace(
                    detected, profile, steps, outcome="pick_book",
                    books=[{"title": book.title} for book in targets[:6]],
                ),
            )

    included = targets[: settings.chat_multibook_max]
    left_out = [book.title for book in targets[settings.chat_multibook_max :]]
    per_book = max(2, settings.chat_overview_sources // len(included))
    _step(
        steps, watch, "scope",
        f"{len(included)} book(s): " + ", ".join(_clip(b.title, 40) for b in included),
    )

    chunks = await fetch_coverage_chunks(
        session,
        principal,
        book_ids=[book.id for book in included],
        per_book=per_book,
        min_tokens=_COVERAGE_MIN_TOKENS,
    )
    _step(
        steps, watch, "sample",
        f"{len(chunks)} passage(s) · up to {per_book}/book · reading order, no embedding",
    )
    if not chunks:
        retrieval_refusals_total.inc()
        return Turn(
            intent=detected, asked=content, query=query, history=history,
            answer=prompts.grounded_refusal([book.title for book in included]),
            trace=_trace(
                detected, profile, steps, outcome="refusal",
                books=[{"title": book.title} for book in included],
            ),
        )

    retrieval_results_total.inc()
    hits = await _enrich(session, [(chunk, None) for chunk in chunks])
    outline = await _outline_for(session, included)
    _step(
        steps, watch, "outline",
        "chapter outline attached" if outline else "no usable chapter outline",
    )

    counted: dict[UUID, int] = {}
    for hit in hits:
        counted[hit["book_id"]] = counted.get(hit["book_id"], 0) + 1

    return Turn(
        intent=detected,
        asked=content,
        query=query,
        retrieved=hits,
        citations=to_citations(hits),
        # One book sets its own register; a mixed sample gets the neutral one.
        kind=included[0].kind.value if len(included) == 1 and included[0].kind else None,
        history=history,
        task=prompts.overview_task(
            [book.title for book in included], left_out=left_out or None
        ),
        outline=outline,
        trace=_trace(
            detected, profile, steps,
            outcome="answered", sources=len(hits),
            books=[
                {"title": book.title, "chunks": counted.get(book.id, 0)}
                for book in included
            ],
        ),
    )


async def _prepare_lookup(
    session: AsyncSession,
    principal: Principal,
    detected: MessageIntent,
    content: str,
    query: str,
    history: list[prompts.Message],
    book_ids: list[UUID] | None,
    profile: QueryProfile,
    *,
    steps: list[dict[str, Any]],
    watch: _Stopwatch,
) -> Turn:
    """"Does this book mention X", "find every mention of X" — where, not what.

    Searched twice on the extracted topic: lexically, because for a mention
    question the literal occurrences are the answer, and by vector, because a
    book can discuss an idea under another name. The two rankings are fused by
    rank — lexical first, so ties go to the passage that actually says the word.

    Zero hits from both is not a failure here: for "does this book mention X" it
    IS the answer, delivered as fixed copy with no model call (invariant 5).
    """
    topic = profile.topic or query
    scope = build_retrieval_filter(principal)

    lexical = await search_chunks_lexical(
        session, topic, scope, top_k=settings.retrieval_lexical_k, book_ids=book_ids
    )
    _step(steps, watch, "lexical", f"full-text “{_clip(topic, 50)}” · {len(lexical)} match(es)")

    vector = await search_chunks(
        session,
        await embeddings.embed_query(topic),
        scope,
        top_k=settings.retrieval_candidate_k,
        max_distance=settings.retrieval_max_distance,
        book_ids=book_ids,
    )
    _step(
        steps, watch, "vector",
        f"{len(vector)} within {settings.retrieval_max_distance}",
    )

    if not lexical and not vector:
        retrieval_refusals_total.inc()
        library = await _visible_books(session, principal)
        searched = (
            [book.title for book in library if book.id in set(book_ids)]
            if book_ids
            else [book.title for book in library]
        )
        return Turn(
            intent=detected, asked=content, query=query, history=history,
            answer=prompts.no_mentions_reply(topic, searched),
            trace=_trace(
                detected, profile, steps,
                outcome="no_mentions", topic=topic,
                books=[{"title": title} for title in searched[:6]],
            ),
        )

    retrieval_results_total.inc()

    # The ts_rank score is deliberately dropped: it is not a distance, and a hit
    # dict carrying a number that only sometimes means "cosine distance" is a bug
    # waiting for a reader. Ordering already encodes the lexical ranking.
    lexical_hits = await _enrich(session, [(chunk, None) for chunk, _ in lexical])
    vector_hits = await _enrich(session, vector)

    fused = rank.dedupe(rank.fuse(lexical_hits, vector_hits))
    kept = rank.spread(fused, limit=settings.chat_lookup_sources)
    _step(
        steps, watch, "fuse",
        f"{len(fused)} distinct passage(s) → kept {len(kept)}",
    )

    counted: dict[Any, dict[str, Any]] = {}
    for hit in fused:
        entry = counted.setdefault(hit["book_id"], {"title": hit["book_title"], "chunks": 0})
        entry["chunks"] += 1

    return Turn(
        intent=detected,
        asked=content,
        query=query,
        retrieved=kept,
        citations=to_citations(kept),
        kind=kept[0]["kind"] if kept else None,
        history=history,
        task=prompts.lookup_task(topic, found=len(fused), shown=len(kept)),
        trace=_trace(
            detected, profile, steps,
            outcome="answered", topic=topic, sources=len(kept),
            books=list(counted.values())[:6],
        ),
    )


async def _prepare_compare(
    session: AsyncSession,
    principal: Principal,
    detected: MessageIntent,
    content: str,
    query: str,
    history: list[prompts.Message],
    book_ids: list[UUID] | None,
    profile: QueryProfile,
    *,
    steps: list[dict[str, Any]],
    watch: _Stopwatch,
) -> Turn:
    """"Compare what these books say about X" — the question routing exists to defeat.

    The dominant-book vote (rank.route) is deliberately not run: collapsing to one
    book is precisely wrong when the question is about the difference. Instead
    every target book gets a quota of the prompt. With a topic, the quota is
    filled from a vector search across the targets; without one ("which is most
    beginner-friendly", "compare the writing styles") there is nothing to search
    *for*, so each book contributes a coverage cross-section instead.
    """
    targets, visible, narrowed = await _target_books(session, principal, query, book_ids)

    if len(targets) < 2:
        _step(steps, watch, "scope", f"only {len(targets)} book(s) to compare")
        return Turn(
            intent=detected, asked=content, query=query, history=history,
            answer=prompts.compare_needs_books_reply([book.title for book in targets]),
            trace=_trace(
                detected, profile, steps, outcome="needs_two_books",
                topic=profile.topic,
                books=[{"title": book.title} for book in targets],
            ),
        )

    _step(
        steps, watch, "scope",
        f"comparing across {len(targets)} book(s)"
        + (" (named)" if narrowed else " (whole library)"),
    )

    if profile.topic:
        hits = await retrieve_for(
            session, principal, profile.topic, book_ids=[book.id for book in targets]
        )
        _step(
            steps, watch, "search",
            f"vector “{_clip(profile.topic, 50)}” · {len(hits)} candidate(s)",
        )
        if not hits:
            return Turn(
                intent=detected, asked=content, query=query, history=history,
                answer=prompts.grounded_refusal([book.title for book in targets]),
                trace=_trace(
                    detected, profile, steps, outcome="refusal", topic=profile.topic,
                    books=[{"title": book.title} for book in targets[:6]],
                ),
            )

        compare_loose = all(hit.get("loose") for hit in hits)
        deduped = rank.dedupe(hits)
        votes = rank.vote(deduped)
        chosen = [vote.book_id for vote in votes[: settings.chat_multibook_max]]
        per_book = max(2, settings.chat_overview_sources // len(chosen))
        kept: list[dict[str, Any]] = []
        for chosen_id in chosen:
            kept.extend(
                [hit for hit in deduped if hit["book_id"] == chosen_id][:per_book]
            )
        _step(
            steps, watch, "quota",
            f"up to {per_book} passage(s) per book · routing off",
        )

        # Books the reader named that contributed nothing are listed in the task,
        # so the answer says "X had nothing on this" instead of silently comparing
        # fewer books than were asked about. An unnamed library sweep lists only
        # the books that showed up — absence from book eleven is not a finding.
        contributing = {hit["book_id"] for hit in kept}
        listed = (
            [book.title for book in targets]
            if narrowed
            else [book.title for book in targets if book.id in contributing]
        )
        logger.info(
            "compared across books",
            extra={"considered": [f"{v.title}:{round(v.share, 2)}" for v in votes[:6]]},
        )
        return Turn(
            intent=detected,
            asked=content,
            query=query,
            retrieved=kept,
            citations=to_citations(kept),
            votes=votes,
            kind=None,
            history=history,
            # A comparison drawn entirely from salvaged passages still has to say
            # so: the shape block tells the model what its sources ARE, and
            # "loose" is part of what they are.
            task=prompts.compare_task(listed, topic=profile.topic)
            + (" " + prompts.loose_match_task() if compare_loose else ""),
            trace=_trace(
                detected, profile, steps,
                outcome="loose" if compare_loose else "answered",
                topic=profile.topic, sources=len(kept),
                books=[
                    {"title": v.title, "chunks": v.chunks, "share": round(v.share, 2)}
                    for v in votes[:6]
                ],
            ),
        )

    included = targets[: settings.chat_multibook_max]
    per_book = max(2, settings.chat_overview_sources // len(included))
    chunks = await fetch_coverage_chunks(
        session,
        principal,
        book_ids=[book.id for book in included],
        per_book=per_book,
        min_tokens=_COVERAGE_MIN_TOKENS,
    )
    _step(
        steps, watch, "sample",
        f"{len(chunks)} passage(s) · up to {per_book}/book · reading order, no embedding",
    )
    if not chunks:
        retrieval_refusals_total.inc()
        return Turn(
            intent=detected, asked=content, query=query, history=history,
            answer=prompts.grounded_refusal([book.title for book in included]),
            trace=_trace(
                detected, profile, steps, outcome="refusal",
                books=[{"title": book.title} for book in included],
            ),
        )

    retrieval_results_total.inc()
    kept = await _enrich(session, [(chunk, None) for chunk in chunks])
    counted: dict[UUID, int] = {}
    for hit in kept:
        counted[hit["book_id"]] = counted.get(hit["book_id"], 0) + 1
    return Turn(
        intent=detected,
        asked=content,
        query=query,
        retrieved=kept,
        citations=to_citations(kept),
        kind=None,
        history=history,
        task=prompts.compare_task([book.title for book in included]),
        trace=_trace(
            detected, profile, steps,
            outcome="answered", sources=len(kept),
            books=[
                {"title": book.title, "chunks": counted.get(book.id, 0)}
                for book in included
            ],
        ),
    )


async def _conversational_answer(
    session: AsyncSession, principal: Principal, detected: MessageIntent
) -> str:
    """Fixed copy for the turns that are not about the material.

    ``META`` is answered from the reader's own library, which this process already
    holds — asking a model to describe it would only create an opportunity to get it
    wrong about something the server knows for certain.
    """
    if detected is MessageIntent.GREETING:
        return prompts.GREETING_REPLY
    if detected is MessageIntent.CHITCHAT:
        return prompts.CHITCHAT_REPLY
    if detected is MessageIntent.META:
        library = await _visible_books(session, principal)
        return prompts.library_reply([(book.title, book.genre) for book in library])
    return prompts.UNCLEAR_REPLY


# ------------------------------------------------------------------- retrieval


async def retrieve_for(
    session: AsyncSession,
    principal: Principal,
    question: str,
    *,
    book_ids: list[UUID] | None = None,
) -> list[dict[str, Any]]:
    """Embed the question and search the material this caller may lawfully see.

    The predicate comes from ``build_retrieval_filter`` and nothing else. Note what
    does not influence scope: the question, the session, and anything the client
    sent. ``book_ids`` is a *narrowing* applied on top of that predicate — the
    reader picking which of their books to ask — and it can only ever subtract, so
    naming somebody else's book selects their chunks out of a set those chunks were
    never in.

    More candidates are fetched than will be kept. Chunk overlap means the top eight
    raw hits are routinely four passages shown twice, so dedupe, the book vote and
    page spread all need room to work; the caller trims with ``rank.narrow``.

    **Two tiers, and only the second one's silence is a refusal** (DECISIONS.md
    D31). The first pass is the strict cosine search this always was. When it
    comes back empty the question goes to :func:`_salvage`, which looks the
    reader's own selective words up in the full-text index and checks the passages
    they name against the question — because "nothing within 0.35 for the sentence
    as typed" and "this material does not cover it" are not the same statement,
    and the tutor was making the second one on the evidence of the first.

    Salvaged hits are marked ``loose`` so the caller can tell the model its
    evidence is weaker (``prompts.loose_match_task``). That marking is the whole
    reason this is a second tier rather than a change to the first one: a question
    that searched well is never sent down it, so a good answer is never diluted.

    Grounding is unchanged. Every returned passage is still a real chunk the
    caller may lawfully read, under the same predicate; there is still no
    world-knowledge fallback; and an empty return still means a refusal with no
    model call (CLAUDE.md invariant 5). What changed is how hard the server looks
    before saying so.
    """
    query_vector = await embeddings.embed_query(question)
    scope = build_retrieval_filter(principal)

    hits = await search_chunks(
        session,
        query_vector,
        scope,
        top_k=settings.retrieval_candidate_k,
        max_distance=settings.retrieval_max_distance,
        book_ids=book_ids,
    )

    if hits:
        retrieval_results_total.inc()
        return await _enrich(session, hits)

    salvaged = await _salvage(session, question, query_vector, scope, book_ids=book_ids)
    if not salvaged:
        retrieval_refusals_total.inc()
        return []

    retrieval_results_total.inc()
    return salvaged


async def _salvage(
    session: AsyncSession,
    question: str,
    query_vector: list[float],
    scope: Any,
    *,
    book_ids: list[UUID] | None,
) -> list[dict[str, Any]]:
    """The second look, run only when the strict search found nothing.

    **It does not lower the bar; it changes which index does the finding.** The
    obvious salvage — re-run the same search with a wider distance ceiling — was
    measured on a real corpus and does not work, because ``bge-small-en-v1.5``
    compresses everything into a narrow band. On the NCERT Science 10th upload:

        on-topic questions                0.17 – 0.29
        the strict ceiling                       0.35
        questions the book has NOTHING on 0.36 – 0.45

    There is no gap. A ceiling wide enough to rescue a half-covered question also
    admits "the offside rule in football" (0.41) and "who is Virat Kohli" (0.46)
    against a school science text.

    What actually fails on a question like "honestly I keep forgetting, what does
    the sphincter muscle do before my test tomorrow" is not the threshold — it is
    that thirteen of the sixteen words are throat-clearing, so the *retrieval*
    vector is noise. The book has the answer on two pages.

    So the two indexes swap jobs. :func:`significant_terms` picks out the words
    that actually name something in scope ("sphincter"), the full-text index finds
    the passages carrying them, and the question's own embedding — a poor
    retriever here, but still a fine comparator — decides which of those the
    reader actually asked about (:func:`search_chunks_corroborated`).

    Nothing survives means the material genuinely does not cover it, and the
    caller refuses with no model call, exactly as before.

    Scope discipline is the caller's ``scope_filter``, passed straight through.
    This changes what is ASKED and which index answers, never what may be read:
    there is no argument here that could reach a chunk the first pass could not.

    Cost is two indexed queries and no model call — and no second embedding, since
    the query vector the caller already paid for is reused as the comparator.
    """
    terms = await significant_terms(session, question, scope, book_ids=book_ids)
    if not terms:
        return []

    found = await search_chunks_corroborated(
        session,
        query_vector,
        terms,
        scope,
        top_k=settings.retrieval_candidate_k,
        max_distance=settings.retrieval_salvage_distance,
        book_ids=book_ids,
    )
    if not found:
        return []

    hits = await _enrich(session, found)
    for hit in hits:
        hit["loose"] = True
        hit["matched_terms"] = terms
    return hits


async def _enrich(
    session: AsyncSession, scored: Sequence[tuple[Chunk, float | None]]
) -> list[dict[str, Any]]:
    """Chunks plus the display facts every downstream consumer needs, as hit dicts.

    ``distance`` is ``None`` for hits that were not produced by a vector search —
    a coverage sample has no query to be distant from, and a lexical rank is not a
    distance. The paths that read ``distance`` (the book vote, routing logs) only
    run on the focused path, where it is always real.

    The chapter title rides along so a lookup answer can say *"chapter 4
    discusses investing"* — display only, like ``genre``; it narrows nothing.
    """
    if not scored:
        return []

    rows = (
        await session.execute(
            select(Book.id, Book.title, Book.kind, Book.genre).where(
                Book.id.in_({chunk.book_id for chunk, _ in scored})
            )
        )
    ).all()
    meta = {row.id: row for row in rows}

    chapter_ids = {chunk.chapter_id for chunk, _ in scored if chunk.chapter_id}
    chapters: dict[Any, str | None] = {}
    if chapter_ids:
        chapters = {
            row.id: row.title
            for row in (
                await session.execute(
                    select(Chapter.id, Chapter.title).where(Chapter.id.in_(chapter_ids))
                )
            ).all()
        }

    return [
        {
            "chunk_id": chunk.id,
            "book_id": chunk.book_id,
            "book_title": meta[chunk.book_id].title if chunk.book_id in meta else "Untitled",
            "kind": (
                meta[chunk.book_id].kind.value
                if chunk.book_id in meta and meta[chunk.book_id].kind is not None
                else None
            ),
            "genre": meta[chunk.book_id].genre if chunk.book_id in meta else None,
            "chapter": chapters.get(chunk.chapter_id),
            "page": chunk.page,
            "scope": chunk.scope.value,
            "text": chunk.text,
            "distance": distance,
        }
        for chunk, distance in scored
    ]


def to_sources(retrieved: list[dict[str, Any]], query: str) -> list[prompts.Source]:
    """The retrieved passages as the model will see them — excerpted to what matters.

    ``query`` is the *retrieval* form of the question (condensed, for a follow-up),
    because that is the text whose vocabulary the passage was matched on and so the
    right thing to select sentences against.

    Only the prompt copy is trimmed. ``turn.retrieved`` and ``turn.citations`` still
    carry the whole chunk, so what the reader opens is the passage that was actually
    indexed and scored, not this extract (see app/rag/trim.py).
    """
    budget = settings.retrieval_source_tokens
    return [
        prompts.Source(
            number=index + 1,
            book_title=hit["book_title"],
            page=hit["page"],
            scope=hit["scope"],
            text=(
                trim.excerpt(hit["text"], query, max_tokens=budget) if budget else hit["text"]
            ),
            genre=hit.get("genre"),
            chapter=hit.get("chapter"),
        )
        for index, hit in enumerate(retrieved)
    ]


def to_citations(retrieved: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The citation payload persisted with the message and sent to the client."""
    return [
        {
            "chunk_id": str(hit["chunk_id"]),
            "book_id": str(hit["book_id"]),
            "book_title": hit["book_title"],
            "genre": hit.get("genre"),
            "page": hit["page"],
            "scope": hit["scope"],
        }
        for hit in retrieved
    ]


# --------------------------------------------------------------------- answering


async def stream_answer(turn: Turn) -> AsyncIterator[str]:
    """Stream the tutor's answer.

    A turn that already has an ``answer`` yields it and never reaches the model.
    Three cases land there — a greeting, a question about the library, and a
    grounded refusal — and the last is the important one: with no sources there is
    nothing to ground an answer in, so there is nothing to generate, and a model
    asked to answer with no sources will happily invent some.
    """
    if turn.answer is not None:
        yield turn.answer
        return

    # `asked`, not `query`. The condensed form exists to be *embedded*; handing it to
    # the tutor as well throws away everything about the request that was not a
    # subject. "Explain that more simply" condenses to a topic question, and answering
    # the topic question produces a longer, more technical answer than the one before
    # it — the exact opposite of what was asked for. The tutor gets the reader's own
    # words and the transcript, and resolves "that" itself.
    async for token in llm.stream(
        prompts.tutor_prompt(
            to_sources(turn.retrieved, turn.query),
            turn.asked,
            history=turn.history,
            kind=turn.kind,
            reply_chars=settings.chat_history_reply_chars,
            task=turn.task,
            outline=turn.outline,
        ),
        max_tokens=settings.llm_max_answer_tokens,
    ):
        yield token


# ------------------------------------------------------------------ persistence


async def persist_message(
    session: AsyncSession,
    *,
    chat_session_id: UUID,
    role: MessageRole,
    content: str,
    citations: list[dict[str, Any]] | None = None,
    intent: MessageIntent | None = None,
    outcome: str | None = None,
) -> ChatMessage:
    message = ChatMessage(
        session_id=chat_session_id,
        role=role,
        content=content,
        citations=citations or [],
        intent=intent,
        outcome=outcome,
    )
    session.add(message)
    await session.flush()
    await session.refresh(message)
    return message


def _derive_title(question: str) -> str:
    """A session's name, taken from the first thing asked in it.

    Not a model call. Naming a conversation is not worth a second of a local 8B
    model, and the first question is what the reader would have called it anyway.
    """
    condensed = " ".join(question.split())
    if len(condensed) <= 60:
        return condensed
    return condensed[:60].rsplit(" ", 1)[0] + "…"


async def touch_session(
    session: AsyncSession, chat: ChatSession, *, first_question: str
) -> None:
    """Mark the conversation used, and name it if it has no name yet.

    An UPDATE rather than an attribute assignment because this also runs from the
    worker session after streaming, where the ORM identity map holds a instance
    loaded in a transaction that has since closed.
    """
    values: dict[str, Any] = {"last_message_at": func.now()}
    if not chat.title:
        values["title"] = _derive_title(first_question)
    await session.execute(
        update(ChatSession).where(ChatSession.id == chat.id).values(**values)
    )


async def record_answer(
    *,
    chat_session_id: UUID,
    content: str,
    citations: list[dict[str, Any]],
    outcome: str | None = None,
) -> None:
    """File the assistant's message after the stream has finished.

    Uses the worker session factory, which bypasses RLS. That is safe here and only
    here: authorization already happened on the request path, and ``chat_session_id``
    names a row the caller was proved to own before a single token was sent.

    Written once, complete. ``chat_messages`` has no UPDATE grant for anybody — a
    transcript that can be edited after the fact is not a transcript — so there is no
    placeholder row to fill in later, which is exactly why this runs at the end.

    ``outcome`` rides along for the same reason the row exists at all: after a reload
    the transcript is all there is, and a refusal that reads back as an ordinary answer
    is a refusal the reader can no longer report.
    """
    if not content.strip():
        return
    try:
        async with WorkerSessionFactory() as writer:
            await persist_message(
                writer,
                chat_session_id=chat_session_id,
                role=MessageRole.ASSISTANT,
                content=content,
                citations=citations,
                outcome=outcome,
            )
            await writer.commit()
    except Exception:
        # The reader has already seen the answer. Failing to file it must not
        # retroactively turn a good response into an error.
        logger.exception("could not persist assistant message")


# --------------------------------------------------------------------- export

# The machine format is versioned so a future shape change is detectable by
# whatever was written against this one, instead of silently reading differently.
_EXPORT_FORMAT_TAG = "kitaably.chat.v1"

_SLUG = re.compile(r"[^a-z0-9]+")


def _export_filename(chat: ChatSession, extension: str) -> str:
    """A filename a person can recognise in a downloads folder.

    The title slug says which conversation; the id suffix keeps two exports of
    "enzymes" from overwriting each other. ASCII by construction, because this
    string travels in a Content-Disposition header.
    """
    slug = _SLUG.sub("-", (chat.title or "conversation").lower()).strip("-")[:40] or "conversation"
    return f"kitaably-{slug}-{chat.id.hex[:8]}.{extension}"


def render_export_json(chat: ChatSession, messages: list[ChatMessage]) -> str:
    """The transcript as data: everything the transcript API itself returns.

    Same fields as ``MessageRead`` — role, content, intent, citations,
    timestamp — and nothing more, so the export can never say more than the API
    does. ``scope`` stays ``canon``/``personal`` here because this is the data
    contract; the human-facing Markdown says shared/private, per the vocabulary
    rule.
    """
    return json.dumps(
        {
            "format": _EXPORT_FORMAT_TAG,
            "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "session": {
                "id": str(chat.id),
                "title": chat.title,
                "created_at": chat.created_at.isoformat() if chat.created_at else None,
                "last_message_at": (
                    chat.last_message_at.isoformat() if chat.last_message_at else None
                ),
            },
            "messages": [
                {
                    "role": message.role.value,
                    "content": message.content,
                    "intent": message.intent.value if message.intent else None,
                    "created_at": (
                        message.created_at.isoformat() if message.created_at else None
                    ),
                    "citations": message.citations or [],
                }
                for message in messages
            ],
        },
        indent=2,
        ensure_ascii=False,
    )


def _citation_line(citations: list[dict[str, Any]]) -> str:
    parts = []
    for index, citation in enumerate(citations, start=1):
        where = citation.get("book_title") or "Untitled"
        if citation.get("page") is not None:
            where += f", p. {citation['page']}"
        # UI vocabulary, not column vocabulary: a person reading this file sees
        # "shared"/"private", the same words the app itself shows.
        origin = "private" if citation.get("scope") == "personal" else "shared"
        parts.append(f"[{index}] {where} ({origin})")
    return " · ".join(parts)


def render_export_markdown(chat: ChatSession, messages: list[ChatMessage]) -> str:
    """The transcript as a document a person would actually reread.

    The tutor's answers are already Markdown — headings, bold, ``[n]`` citation
    marks — so they are exported verbatim, with each answer's source list printed
    beneath it so the ``[n]`` marks still resolve on paper.
    """
    exported = datetime.now(UTC).strftime("%d %b %Y, %H:%M UTC")
    lines = [
        f"# {chat.title or 'Conversation'}",
        "",
        f"Exported from Kitaably on {exported}.",
        "",
    ]

    if not messages:
        lines += ["*No messages yet.*", ""]

    for message in messages:
        stamp = (
            message.created_at.strftime("%d %b %Y, %H:%M")
            if message.created_at
            else ""
        )
        lines.append("---")
        lines.append("")
        if message.role is MessageRole.USER:
            lines.append(f"**You** · {stamp}".rstrip(" ·"))
            lines.append("")
            # Quoted so a question that happens to start with `#` or `-` reads as
            # the reader's words, not as document structure.
            lines += [f"> {line}" for line in message.content.splitlines() or [""]]
        else:
            lines.append(f"**Tutor** · {stamp}".rstrip(" ·"))
            lines.append("")
            lines.append(message.content)
            if message.citations:
                lines.append("")
                lines.append(f"*Sources: {_citation_line(message.citations)}*")
        lines.append("")

    return "\n".join(lines)


async def export_conversation(
    session: AsyncSession,
    principal: Principal,
    chat_session_id: UUID,
    fmt: str,
) -> tuple[str, str, str]:
    """One conversation as a downloadable file: ``(filename, media_type, body)``.

    Authorization is the same as reading the transcript — RLS hides anyone
    else's conversation, so it reads as absent (404) rather than forbidden. The
    export contains exactly what ``list_messages`` already returns; there is
    nothing to audit, because nothing about who can read what has changed.
    """
    chat = await get_session(session, principal, chat_session_id)
    messages = await list_messages(session, principal, chat_session_id)

    if fmt == "json":
        return (
            _export_filename(chat, "json"),
            "application/json",
            render_export_json(chat, messages),
        )
    if fmt == "md":
        return (
            _export_filename(chat, "md"),
            "text/markdown; charset=utf-8",
            render_export_markdown(chat, messages),
        )
    # Unreachable behind the route's enum, kept for any future caller that isn't.
    raise ValidationFailed("That export format isn't supported. Use json or md.")
