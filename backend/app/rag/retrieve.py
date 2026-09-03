"""Retrieval scoping — the chokepoint.

**This module contains the only function permitted to construct a predicate over
``chunks``.** Not one of several. The only one. A boundary enforced at fifteen call
sites is a boundary that will be breached at the sixteenth (DECISIONS.md D9).

    canon     a book its owner shared with everyone
              readable by every signed-in user
              usable for chat and for assessment generation

    personal  a private upload, belonging to one person
              readable by that person ONLY — never another user, at all.
              An author may draw *their own* personal books into a paper they
              write (DECISIONS.md D29); nobody can ever draw anybody else's.

Scope is derived from the authenticated principal. A ``book_id`` or ``scope``
arriving from the client is a request, never an authorization.

Note the shape of :func:`build_retrieval_filter`: the personal clause is bound to
``principal.id`` at construction and there is no argument that can widen it. The
worst any caller bug can do is show callers their own material.
"""

import re
from uuid import UUID

from sqlalchemy import (
    ColumnElement,
    Select,
    String,
    and_,
    cast,
    func,
    literal,
    literal_column,
    or_,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.core.config import settings
from app.core.security import Principal
from app.db.models import Chunk
from app.db.models.enums import BookScope


def canon_clause() -> ColumnElement[bool]:
    """Chunks of shared books.

    Takes no argument on purpose. Canon is a single platform-wide pool now, so there
    is no scoping parameter for a caller to get wrong — and no way to express
    "someone else's canon", because there is no such thing (DECISIONS.md D16).
    """
    return Chunk.scope == BookScope.CANON


def personal_clause(owner_id: UUID) -> ColumnElement[bool]:
    """Chunks of one person's own private uploads."""
    return and_(Chunk.scope == BookScope.PERSONAL, Chunk.owner_id == owner_id)


def build_retrieval_filter(principal: Principal) -> ColumnElement[bool]:
    """Build the WHERE clause for a vector search. The only place this happens.

    Args:
        principal: the authenticated caller. Scope is derived from this and nothing
            else — not from a role branch, not from a request body. The caller
            reaches canon plus their own personal uploads, and nothing more.

    Assessment generation runs under this same filter, built for the *author*
    (DECISIONS.md D29): an author may examine on their own uploads, shared or not,
    and can never reach anyone else's — the personal clause is bound to one id at
    construction and there is no argument that can widen it.

    Every caller gets the same shape, which is the point of the revised model —
    there is no account whose predicate is wider than anyone else's, so there is no
    account to escalate into.
    """
    return or_(canon_clause(), personal_clause(principal.id))


async def search_chunks(
    session: AsyncSession,
    query_embedding: list[float],
    scope_filter: ColumnElement[bool],
    *,
    top_k: int,
    max_distance: float,
    book_ids: list[UUID] | None = None,
) -> list[tuple[Chunk, float]]:
    """Cosine search over ``chunks`` under a predicate built above.

    Returns only results within ``max_distance``. An empty list is a valid, expected
    outcome and means the material does not cover the question — the caller states
    that and stops. It does not fall back to world knowledge (DECISIONS.md D13).

    Ordering comes from the database. Do not re-sort in Python, and never rehydrate
    with a bare ``WHERE id IN (...)`` that discards the ranking.

    Args:
        book_ids: an optional *narrowing*, for a reader who has picked which of their
            books to ask. It can only ever subtract. The ids arrive in a request body
            and are therefore untrusted, which is safe precisely because
            ``scope_filter`` has already run: naming somebody else's book here selects
            their chunks out of a set those chunks were never in. Same argument, same
            construction, as ``fetch_generation_chunks`` below — a request may say
            *less*, never *more*.
    """
    # pgvector POST-filters an HNSW scan: the index walk produces `ef_search`
    # candidates and the scope predicate is applied to those, so a narrow filter --
    # one personal book out of a large shared library -- can exhaust them and return
    # fewer rows than asked for. That failure is silent and reads to the reader as
    # "my book doesn't cover this", so the candidate list is widened deliberately
    # rather than left at the default 40. SET LOCAL: transaction-scoped, like the
    # RLS context beside it, so it cannot leak to the next request on this
    # connection.
    await session.execute(
        text(f"SET LOCAL hnsw.ef_search = {int(settings.retrieval_ef_search)}")
    )

    distance = Chunk.embedding.cosine_distance(query_embedding).label("distance")

    query = (
        select(Chunk, distance)
        # The 384-dimension vector is the widest column on the row and the one thing
        # no caller reads -- ranking works on `distance`, which the database has
        # already computed. Selecting it shipped ~8 KB of float text per candidate
        # across the wire to be parsed into a Python list and dropped.
        .options(defer(Chunk.embedding, raiseload=True))
        .where(scope_filter)
        .where(Chunk.embedding.is_not(None))
    )
    if book_ids:
        query = query.where(Chunk.book_id.in_(book_ids))

    rows = await session.execute(query.order_by(distance).limit(top_k))
    return [(chunk, value) for chunk, value in rows.all() if value <= max_distance]


def lexical_query(
    scope_filter: ColumnElement[bool],
    question: str,
    *,
    top_k: int,
    book_ids: list[UUID] | None = None,
) -> Select:
    """The full-text query behind :func:`search_chunks_lexical`, built pure.

    Split out so ``tests/test_scoping.py`` can compile the exact SQL the lookup
    path runs — the same treatment the vector search gets — rather than a
    reconstruction that could drift from it.

    The tsvector expression must stay byte-identical to the one in
    ``chunks_text_search_idx`` (``to_tsvector('english', text)``), or Postgres
    quietly stops using the index and every mention query becomes a sequential
    scan that still *works*, at a cost that only shows up as the corpus grows.

    ``websearch_to_tsquery`` rather than ``to_tsquery``: it takes the reader's
    words as they are — quotes, stray punctuation, whatever — and cannot raise on
    malformed syntax, which matters because the topic arrives from a request body.
    """
    # The regconfig is an inline literal, not a bind parameter, on purpose: an
    # expression index only matches an expression Postgres can fold to the index's
    # own definition, and `to_tsvector($1, text)` cannot be — the planner would
    # quietly fall back to a sequential scan. The question, by contrast, MUST stay
    # a bind: it arrives from a request body.
    config: ColumnElement[str] = literal_column("'english'")
    tsquery = func.websearch_to_tsquery(config, question)
    tsvector = func.to_tsvector(config, Chunk.text)
    score = func.ts_rank(tsvector, tsquery).label("score")

    query = (
        select(Chunk, score)
        # Same reasoning as the vector search above: nothing reads the embedding.
        .options(defer(Chunk.embedding, raiseload=True))
        .where(scope_filter)
        .where(tsvector.op("@@")(tsquery))
    )
    if book_ids:
        query = query.where(Chunk.book_id.in_(book_ids))
    # Rank first; book and position as tie-breaks so equal-rank rows come back in
    # a stable, reading-order-ish sequence rather than in heap order.
    return query.order_by(score.desc(), Chunk.book_id, Chunk.index).limit(top_k)


async def search_chunks_lexical(
    session: AsyncSession,
    question: str,
    scope_filter: ColumnElement[bool],
    *,
    top_k: int,
    book_ids: list[UUID] | None = None,
) -> list[tuple[Chunk, float]]:
    """Full-text search over ``chunks``, under a predicate built above. Phase 7.

    The lexical half of a mention question ("does this book mention X", "find
    every mention of X"). Vector similarity finds passages *about* a topic; it
    does not find every passage *naming* one, and for a mention question the
    literal occurrences are the answer (DECISIONS.md D22). The caller fuses this
    with the vector hits — neither list alone is complete.

    Scope discipline is identical to :func:`search_chunks`: the predicate comes
    from :func:`build_retrieval_filter`, ``book_ids`` only ever narrows, and an
    empty result is a valid outcome meaning the material does not name it —
    which, for "does this book mention X", is itself the honest answer.
    """
    if not question.strip():
        return []

    rows = await session.execute(
        lexical_query(scope_filter, question, top_k=top_k, book_ids=book_ids)
    )
    return [(chunk, float(score)) for chunk, score in rows.all()]


# Words worth probing the index with. Anything shorter than three characters is
# noise once the stemmer has had it, and a question long enough to need more than
# this many probes has already said what it is about several times over.
_WORD = re.compile(r"[a-z0-9][a-z0-9'\u2019-]*")
_MIN_TERM_CHARS = 3
_MAX_TERMS_PROBED = 24

# A term is SELECTIVE when it appears in no more than this share of the material
# in scope. Measured on a 496-chunk school science text, the split is stark and
# there is nothing near the line: the words that carry the question sit at 1-5
# chunks (0.2-1%) — "tyndall" 1, "sphincter" 2, "solenoid" 4, "myopia" 5 — while
# the words that merely carry the sentence sit at 15-50 (3-10%) — "rule" 15,
# "actually" 17, "muscle" 19, "keep" 28, "test" 50.
_SELECTIVITY = 0.02
# …but never fewer than this, so a two-chunk upload does not end up with a cap of
# zero and reject every word in the book.
_MIN_SELECTIVE = 3
# Rarity is also judged RELATIVE to the rarest word in the question, because the
# absolute cap alone still admits chatty words in a big book: "five" (7) and "old"
# (8) clear it beside "tyndall" (1), and a reduced query of "tyndall five old
# please" embeds to 0.49 — worse than the question it was meant to rescue. Held
# to three times the rarest term, only "tyndall" and "please" survive, and the
# reduced query lands where it should.
_RARITY_SPREAD = 3
# However selective they are, a reduced query is a handful of words or it is not
# reduced.
_MAX_TERMS_KEPT = 4
# The corpus count is bounded rather than exact. Past this the cap saturates,
# which is fine — it is a threshold, not a statistic — and it keeps the count
# from becoming a scan of the whole table on a large library.
_CORPUS_SAMPLE = 20_000


async def significant_terms(
    session: AsyncSession,
    question: str,
    scope_filter: ColumnElement[bool],
    *,
    book_ids: list[UUID] | None = None,
) -> list[str]:
    """The reader's words that actually point at something in the material.

    A full-text search ANDs its terms, which is what makes it precise enough to
    gate the salvage tier (``services/chat.py :: _salvage``) — and also what makes
    it useless on a real question. "Honestly I keep forgetting, what does the
    sphincter muscle do before my test tomorrow" ANDs *honestly*, *forgetting* and
    *tomorrow* against a science textbook and matches nothing, when the reader
    plainly asked about a word the book uses on two pages.

    Two filters, and they fail in opposite directions on purpose:

    **Absent words are dropped, not required.** A term the material has never
    heard of cannot be evidence that the material does not cover the question —
    it is evidence about the reader's phrasing. Vetoing on it is how a book gets
    asked about in a full sentence and answers as though it were empty.

    **Ubiquitous words are dropped too.** Keeping every word the book contains
    only moves the problem: "keep", "actually" and "test" are all in a science
    textbook, and a term in a tenth of the corpus says nothing about which passage
    to look at. Rarity is judged both absolutely (a share of the material) and
    relative to the rarest word present, because the absolute cap alone still lets
    "five" and "old" ride along beside "tyndall".

    What survives is the handful of words that single a passage out — which is
    close to what the reader would have typed if asked to be brief, and that is
    precisely what the caller uses it as: a re-posed query, searched at the
    ordinary threshold rather than at a relaxed one.

    An off-book question keeps whatever few words it has in common with the
    material — "friend", "confusing" — and those re-pose to a query the material
    still does not answer, which is what stops the salvage opening on "the offside
    rule in football".

    One round trip, not one per word: the counts are correlated subqueries in a
    single statement, each an index lookup on ``chunks_text_search_idx``, and each
    bounded so a common word costs the same as a rare one.

    Scope comes from :func:`build_retrieval_filter` like every other query in this
    module, and ``book_ids`` narrows it the same way. The terms are matched with
    ``plainto_tsquery`` and travel as a bound array — they arrive from a request
    body and never reach the SQL as text.
    """
    seen: dict[str, None] = {}
    for word in _WORD.findall(question.lower()):
        if len(word) >= _MIN_TERM_CHARS:
            seen.setdefault(word, None)
        if len(seen) >= _MAX_TERMS_PROBED:
            break
    words = list(seen)
    if not words:
        return []

    cap = await _selectivity_cap(session, scope_filter, book_ids=book_ids)

    config: ColumnElement[str] = literal_column("'english'")
    # `column_valued`, not `table_valued`: unnest over a scalar array yields one
    # unnamed column, and the table form renders an alias with no column list —
    # `unnest(...) AS anon_1` — which Postgres cannot resolve `anon_1.word`
    # against.
    candidates = func.unnest(cast(words, ARRAY(String))).column_valued("word")

    # A correlated scalar subquery per word, evaluated in one statement. It is
    # deliberately NOT wrapped in `.subquery()` to bound the count: wrapping moves
    # `word` into the inner FROM, the correlation is lost, and every term silently
    # scores the same — which reads as "the book contains none of these words"
    # and refuses everything.
    frequency = (
        select(func.count())
        .select_from(Chunk)
        .where(scope_filter)
        .where(
            func.to_tsvector(config, Chunk.text).op("@@")(
                func.plainto_tsquery(config, candidates)
            )
        )
    )
    if book_ids:
        frequency = frequency.where(Chunk.book_id.in_(book_ids))

    rows = (await session.execute(select(candidates, frequency.scalar_subquery()))).all()
    present = {word: found for word, found in rows if 0 < found <= cap}
    if not present:
        return []

    rarest = min(present.values())
    keep = {
        word
        for word, found in present.items()
        if found <= max(_MIN_SELECTIVE, rarest * _RARITY_SPREAD)
    }
    # Rarest first, so the cap keeps the words that carry the question. Ties break
    # on the reader's own order, which is what appears in the pipeline trace.
    ordered = sorted(
        (word for word in words if word in keep),
        key=lambda word: (present[word], words.index(word)),
    )
    return ordered[:_MAX_TERMS_KEPT]


async def _selectivity_cap(
    session: AsyncSession,
    scope_filter: ColumnElement[bool],
    *,
    book_ids: list[UUID] | None,
) -> int:
    """How many chunks a term may appear in and still single a passage out."""
    reachable = select(literal(1)).select_from(Chunk).where(scope_filter)
    if book_ids:
        reachable = reachable.where(Chunk.book_id.in_(book_ids))

    total = await session.scalar(
        select(func.count()).select_from(reachable.limit(_CORPUS_SAMPLE).subquery())
    )
    return max(_MIN_SELECTIVE, int((total or 0) * _SELECTIVITY))


async def search_chunks_corroborated(
    session: AsyncSession,
    query_embedding: list[float],
    terms: list[str],
    scope_filter: ColumnElement[bool],
    *,
    top_k: int,
    max_distance: float,
    book_ids: list[UUID] | None = None,
) -> list[tuple[Chunk, float]]:
    """Passages the words found, kept only if the question agrees. Phase 7.

    The salvage tier's one retrieval (``services/chat.py :: _salvage``), and the
    two halves do different jobs on purpose:

    **The full-text index retrieves.** ``terms`` comes from
    :func:`significant_terms`, so these are the reader's own words that single
    something out in scope, ORed — a passage naming any of them is a candidate.
    That is high recall and low precision, which is the right way round here,
    because the vector then supplies the precision.

    **The question's own embedding filters.** Every candidate is scored against
    the question *as the reader typed it* and dropped past ``max_distance``. The
    insight this rests on is that a noisy question is a poor RETRIEVER and still a
    perfectly good COMPARATOR: the filler shifts the vector enough that nothing
    lands within the strict ceiling, but a passage that genuinely answers it still
    lands much nearer than one that merely shares a word. Measured on a school
    science text, against the reader's full sentence:

        the passage naming "tyndall"     0.360   ← the answer
        the passage naming "solenoid"    0.374   ← the answer
        the passage naming "sphincter"   0.381   ← the answer
        the passage naming "confusing"   0.463   ← coincidence
        the passage naming "french"      0.531   ← coincidence
        the passage naming "kohli"       0.608   ← coincidence

    There is a real gap there, which is what ``retrieval_salvage_distance`` sits
    in — and note it is a *corroboration* ceiling over a handful of lexically
    chosen rows, never a retrieval ceiling over the whole index. Widening the
    ordinary search to 0.42 would put the "kohli" passage into unrelated answers;
    here it cannot, because nothing reaches this query that the reader's own words
    did not name.

    No second embedding: the caller's query vector is reused as-is.

    Scope is :func:`build_retrieval_filter` as everywhere else, and ``book_ids``
    only ever subtracts.
    """
    if not terms:
        return []

    config: ColumnElement[str] = literal_column("'english'")
    # `websearch_to_tsquery` reads "or" as the operator, so this stays inside the
    # one parser that cannot raise on whatever the reader typed -- the terms are
    # request-derived and must never be composed into tsquery syntax by hand.
    tsquery = func.websearch_to_tsquery(config, " or ".join(terms))
    distance = Chunk.embedding.cosine_distance(query_embedding).label("distance")

    query = (
        select(Chunk, distance)
        .options(defer(Chunk.embedding, raiseload=True))
        .where(scope_filter)
        .where(Chunk.embedding.is_not(None))
        .where(func.to_tsvector(config, Chunk.text).op("@@")(tsquery))
    )
    if book_ids:
        query = query.where(Chunk.book_id.in_(book_ids))

    rows = await session.execute(query.order_by(distance).limit(top_k))
    return [(chunk, value) for chunk, value in rows.all() if value <= max_distance]


def coverage_query(
    scope_filter: ColumnElement[bool],
    *,
    book_ids: list[UUID],
    per_book: int,
    min_tokens: int = 0,
) -> Select:
    """The stratified-sample query behind :func:`fetch_coverage_chunks`, built pure.

    ``ntile`` splits each book's chunks into ``per_book`` equal runs by position,
    and one chunk — the first — is taken from each run: an even cross-section in
    reading order, chosen by the database without ever shipping the book. A book
    with fewer chunks than tiles simply yields what it has.

    The outer SELECT rehydrates by id, which is safe here precisely because the
    ids come from the scoped inner query and the ordering — reading order — is
    reapplied explicitly rather than lost (compare the warning on
    :func:`search_chunks`).
    """
    tile = (
        func.ntile(max(1, per_book))
        .over(partition_by=Chunk.book_id, order_by=Chunk.index)
        .label("tile")
    )
    ranked = (
        select(Chunk.id.label("chunk_id"), Chunk.book_id.label("book_id"),
               Chunk.index.label("chunk_index"), tile)
        .where(scope_filter)
        .where(Chunk.book_id.in_(book_ids))
    )
    if min_tokens:
        ranked = ranked.where(Chunk.token_count >= min_tokens)
    sample = ranked.subquery("sample")

    picked = (
        select(sample.c.chunk_id)
        .distinct(sample.c.book_id, sample.c.tile)
        .order_by(sample.c.book_id, sample.c.tile, sample.c.chunk_index)
    )
    return (
        select(Chunk)
        .options(defer(Chunk.embedding))
        .where(Chunk.id.in_(picked))
        .order_by(Chunk.book_id, Chunk.index)
    )


async def fetch_coverage_chunks(
    session: AsyncSession,
    principal: Principal,
    *,
    book_ids: list[UUID],
    per_book: int,
    min_tokens: int = 0,
) -> list[Chunk]:
    """An even sample of each named book, in reading order. Phase 7.

    The overview path: "summarize this book" has no subject to embed, so its
    nearest chunks are noise — a summary question is a question about *all* of
    the book, and the answer's evidence is a cross-section, not a similarity hit
    (DECISIONS.md D22; the same insight as assessment sampling, D12).

    Scope discipline as everywhere in this module: the predicate comes from
    :func:`build_retrieval_filter` with the caller's own principal — a reader may
    summarise their *personal* books, unlike assessment generation — and
    ``book_ids`` arrives from a request body, safe because a book outside the
    caller's scope contributes zero rows rather than an error.
    """
    if not book_ids:
        return []

    return list(
        await session.scalars(
            coverage_query(
                build_retrieval_filter(principal),
                book_ids=book_ids,
                per_book=per_book,
                min_tokens=min_tokens,
            )
        )
    )


async def fetch_topic_chunks(
    session: AsyncSession,
    author: Principal,
    *,
    topic_embeddings: list[list[float]],
    book_ids: list[UUID],
    chapter_ids: list[UUID] | None = None,
    per_topic: int,
    min_tokens: int = 0,
) -> list[Chunk]:
    """The chunks nearest the topics the author's brief named. Phase 5c.

    Assessment generation's *narrowing* pass, and it lives here for the reason
    everything else does: it is a predicate over ``chunks``, so it is built in this
    module or it is not built (DECISIONS.md D9). Same author-bound scope clause as
    :func:`fetch_generation_chunks`, same reason — canon plus the author's own
    uploads, never anybody else's personal book (D29).

    **Distance is deliberately unbounded here, and that is the difference between
    this and chat retrieval.** The tutor refuses when nothing is close enough,
    because answering from far-away material would be an ungrounded claim (D13).
    A paper has no such claim to make: the author asked for questions about
    photosynthesis from a book they chose, and the nearest twenty passages in that
    book are the best available answer to that even if the book covers it thinly.
    The caller blends what comes back with a coverage sample of the whole book, so a
    topic the material barely mentions yields a paper that leans elsewhere rather
    than a paper that fails.

    Returned in reading order rather than by distance: the caller stratifies by
    position (D12), and a similarity ordering would defeat that on arrival.
    """
    if not book_ids or not topic_embeddings:
        return []

    await session.execute(
        text(f"SET LOCAL hnsw.ef_search = {int(settings.retrieval_ef_search)}")
    )

    scope = build_retrieval_filter(author)
    found: dict[UUID, Chunk] = {}
    for embedding in topic_embeddings:
        distance = Chunk.embedding.cosine_distance(embedding).label("distance")
        query = (
            select(Chunk)
            # Generation reads text, not vectors — see `fetch_generation_chunks`.
            .options(defer(Chunk.embedding))
            .where(scope)
            .where(Chunk.book_id.in_(book_ids))
            .where(Chunk.embedding.is_not(None))
        )
        if chapter_ids:
            query = query.where(Chunk.chapter_id.in_(chapter_ids))
        if min_tokens:
            query = query.where(Chunk.token_count >= min_tokens)
        for chunk in await session.scalars(query.order_by(distance).limit(per_topic)):
            # One dict across every topic: two topics that share a passage contribute
            # it once, and the paper does not quietly weight it double.
            found.setdefault(chunk.id, chunk)

    return sorted(found.values(), key=lambda chunk: (str(chunk.book_id), chunk.index))


async def fetch_generation_chunks(
    session: AsyncSession,
    author: Principal,
    *,
    book_ids: list[UUID],
    chapter_ids: list[UUID] | None = None,
    min_tokens: int = 0,
) -> list[Chunk]:
    """Every chunk the *author* may examine on, from the named books, in reading
    order. Phase 5, widened by DECISIONS.md D29.

    Assessment generation's source pool. It lives here, in the chokepoint module,
    because it is still a predicate over ``chunks`` and the rule is that there is only
    one file allowed to build one — a boundary enforced at fifteen call sites is a
    boundary that will be breached at the sixteenth (DECISIONS.md D9).

    The scope half comes from :func:`build_retrieval_filter` built for the paper's
    author — canon plus the author's own uploads, bound to one id at construction.
    ``book_ids`` only ever *narrows* that: somebody else's personal book named in the
    request contributes nothing, because the scope clause has already excluded it.
    That is deliberate — the caller passes ids that arrived in a request body, and
    this must be safe even when those ids are somebody else's. The worker calls this
    with the service role and no RLS behind it, which is exactly why the predicate is
    explicit here rather than assumed.

    Ordered by book then chunk index, so the caller can stratify across a chapter by
    position rather than by similarity (DECISIONS.md D12).
    """
    if not book_ids:
        return []

    query = (
        select(Chunk)
        # Generation reads text, not vectors. See the note in `search_chunks`; a
        # whole book's worth of 384-dimension columns is a great deal of wire for
        # something no caller looks at.
        .options(defer(Chunk.embedding))
        .where(build_retrieval_filter(author))
        .where(Chunk.book_id.in_(book_ids))
        .order_by(Chunk.book_id, Chunk.index)
    )
    if chapter_ids:
        query = query.where(Chunk.chapter_id.in_(chapter_ids))
    if min_tokens:
        # A 40-token chunk cannot support a question worth asking.
        query = query.where(Chunk.token_count >= min_tokens)

    return list(await session.scalars(query))
