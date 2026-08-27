"""Which of these chunks actually answer the question, and from which book.

Pure functions over rows that have already come back from the database. Nothing here
touches a session, builds a predicate, or widens anybody's reach -- by the time a hit
reaches this module ``build_retrieval_filter`` has already decided what the caller may
see, and every function below can only *discard*. That is the invariant worth keeping:
**ranking narrows, it never admits**.

Three problems are solved here, in order.

1. **Overlap.** Chunks carry ~100 tokens of deliberate overlap, so the two chunks
   either side of a boundary are near-identical. Eight hits routinely mean four
   passages shown twice, and the model dutifully cites both.
2. **Which book.** A question about aldehydes should be answered from the organic
   chemistry text, not from the one paragraph in the biology book that mentions them
   in passing. The evidence for that decision is already in hand -- five hits from one
   book and one from another is the answer -- so it costs no model call and no label
   written at ingest time.
3. **Spread.** Six hits from one page is worse context than six hits from six pages,
   even when the six are individually nearer.

Ordering always comes from the database and is preserved throughout. Nothing here
re-sorts by a Python-computed score; the routing vote *chooses* books, and the chunks
of the chosen books stay in the order the vector search returned them.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

Hit = dict[str, Any]

# How much less a hit counts for each place further down the ranking. Purely a
# tie-breaker for the vote -- without it, five weak mentions in a survey text
# outvote two strong passages in the book that actually covers the topic.
_RANK_DECAY = 0.85

# Share of the total vote at which one book is treated as *the* source. Below it the
# question genuinely spans books ("compare X and Y") and narrowing would remove half
# the answer. Tuned by hand against mixed-library questions; it is a product decision,
# not a magic number, and the failure it guards against is asymmetric -- too high
# merely keeps some noise, too low silently drops the book that had the answer.
DOMINANCE = 0.62

# Never route down to fewer than this many chunks. A vote among two hits is not a
# vote, and a single chunk gives the tutor nothing to synthesise from.
MIN_CHUNKS = 3


@dataclass(frozen=True, slots=True)
class BookVote:
    """One book's share of the evidence, and why."""

    book_id: UUID
    title: str
    chunks: int
    score: float
    share: float


# Characters of a chunk's head or tail that must appear verbatim in another chunk for
# the two to count as overlapping. Chunking uses ~100 tokens of deliberate overlap,
# which is 400-600 characters, so 200 sits comfortably inside it while being long
# enough that no two unrelated passages share such a span by accident.
_OVERLAP_WINDOW = 200


def _overlaps(left: str, right: str) -> bool:
    """Whether two passages are the same material seen twice.

    Looks for a *verbatim shared span*, not for similarity. That is deliberate: the
    duplication this exists to catch has one specific cause -- chunking overlaps
    neighbours by ~100 tokens on purpose -- so the two chunks either side of a
    boundary share a literal run of text, and detecting exactly that mechanism has
    essentially no false positives.

    The obvious alternative, token-set containment, is a trap. Two unrelated
    passages from the same textbook share a great deal of vocabulary ("the", "of",
    "is", the subject's own terminology), and against a *short* chunk the containment
    ratio is computed over so few distinct tokens that it clears any reasonable
    threshold. That drops a passage the reader needed, silently, with no error and
    nothing in the answer to suggest anything is missing.
    """
    a = " ".join(left.split())
    b = " ".join(right.split())
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    for text, other in ((a, b), (b, a)):
        if len(text) >= _OVERLAP_WINDOW and (
            text[:_OVERLAP_WINDOW] in other or text[-_OVERLAP_WINDOW:] in other
        ):
            return True
    return False


# The standard reciprocal-rank-fusion constant. Large enough that rank 1 versus
# rank 3 in one list cannot outweigh appearing in both lists, which is the whole
# point of fusing: a chunk that both the words and the meaning agree on outranks a
# chunk only one of them found.
_RRF_K = 60


def fuse(*rankings: list[Hit]) -> list[Hit]:
    """Merge independently-ranked hit lists into one, by reciprocal rank. Phase 7.

    Built for the lookup path, where a mention question is searched twice — once
    lexically (the literal occurrences) and once by vector (the passages about the
    topic under other words) — and neither list alone is complete. Order of the
    *arguments* is the tie-break: a chunk appearing at the same ranks in either
    order keeps the position its earlier list gave it, so the caller passes the
    list whose ordering should win ties first (lexical, for a mention question).

    Scores are ranks, not distances, on purpose: ts_rank and cosine distance are
    incommensurable numbers, and any formula mixing them directly is a tuning
    knob nobody can defend. Rank is the one thing both lists agree on the meaning
    of. A hit's dict is kept from the first list that contained it; a ``distance``
    that list lacked is backfilled from a later one, so downstream code that reads
    it (logging, the book vote) sees the real value where one exists.

    Like everything in this module: by the time a hit reaches here the scope
    filter has already decided what the caller may see. Fusing reorders and
    merges; it never admits.
    """
    scores: dict[object, float] = {}
    merged: dict[object, Hit] = {}
    arrival: dict[object, int] = {}

    order = 0
    for ranking in rankings:
        for position, hit in enumerate(ranking):
            key = hit["chunk_id"]
            scores[key] = scores.get(key, 0.0) + 1.0 / (_RRF_K + position + 1)
            if key not in merged:
                merged[key] = hit
                arrival[key] = order
                order += 1
            elif merged[key].get("distance") is None and hit.get("distance") is not None:
                merged[key] = {**merged[key], "distance": hit["distance"]}

    return sorted(
        merged.values(),
        key=lambda hit: (-scores[hit["chunk_id"]], arrival[hit["chunk_id"]]),
    )


def dedupe(hits: list[Hit]) -> list[Hit]:
    """Drop hits that repeat material already kept, preserving rank order.

    Two passes over the same problem. Same book and same page is an exact-enough
    duplicate to drop on identity alone; anything else is compared by text, because
    a chunk boundary can fall across a page boundary and the overlap survives it.
    """
    kept: list[Hit] = []
    seen_pages: set[tuple[UUID, int]] = set()

    for hit in hits:
        page = hit.get("page")
        if page is not None:
            key = (hit["book_id"], page)
            if key in seen_pages:
                continue

        if any(
            other["book_id"] == hit["book_id"] and _overlaps(other["text"], hit["text"])
            for other in kept
        ):
            continue

        if page is not None:
            seen_pages.add((hit["book_id"], page))
        kept.append(hit)

    return kept


def vote(hits: list[Hit]) -> list[BookVote]:
    """Score each book by the evidence it contributed. Strongest first.

    A book's score is the sum of its hits' similarities, each discounted by where the
    hit landed in the ranking. Similarity is ``1 - distance``, so a chunk at distance
    0.12 counts roughly twice what one at distance 0.34 does -- which is the whole
    point, since the second is barely inside the threshold.
    """
    totals: dict[UUID, list[float]] = {}
    titles: dict[UUID, str] = {}

    for rank, hit in enumerate(hits):
        similarity = max(0.0, 1.0 - float(hit["distance"]))
        totals.setdefault(hit["book_id"], []).append(similarity * (_RANK_DECAY**rank))
        titles.setdefault(hit["book_id"], hit["book_title"])

    grand = sum(sum(scores) for scores in totals.values()) or 1.0

    votes = [
        BookVote(
            book_id=book_id,
            title=titles[book_id],
            chunks=len(scores),
            score=sum(scores),
            share=sum(scores) / grand,
        )
        for book_id, scores in totals.items()
    ]
    return sorted(votes, key=lambda v: v.score, reverse=True)


def route(hits: list[Hit], *, dominance: float = DOMINANCE) -> tuple[list[Hit], list[BookVote]]:
    """Narrow to the book (or two) the evidence actually points at.

    Returns the surviving hits in their original order, plus the full vote including
    the books that lost -- the caller logs that, because "it answered from the wrong
    book" is undebuggable without knowing what the alternatives scored.

    When no book clears ``dominance`` the top two are kept rather than one. Questions
    that span books are common and legitimate, and a forced single winner answers half
    of "how does the textbook's account differ from the paper's".
    """
    if len(hits) <= MIN_CHUNKS:
        return hits, vote(hits)

    votes = vote(hits)
    if not votes:
        return hits, votes

    chosen = {votes[0].book_id}
    if votes[0].share < dominance:
        chosen.update(v.book_id for v in votes[1:2])

    narrowed = [hit for hit in hits if hit["book_id"] in chosen]

    # Routing must never starve the answer. If narrowing left too little to work with,
    # the vote was not decisive enough to act on and everything is kept.
    if len(narrowed) < MIN_CHUNKS:
        return hits, votes
    return narrowed, votes


def spread(hits: list[Hit], *, limit: int) -> list[Hit]:
    """Take ``limit`` hits, preferring coverage over a cluster on one page.

    A first pass takes at most one chunk per page, in rank order; a second fills any
    remaining room from what was skipped. So the strongest hit is always kept and the
    rest of the budget buys breadth instead of five views of one paragraph.
    """
    if len(hits) <= limit:
        return hits

    picked: list[Hit] = []
    deferred: list[Hit] = []
    seen: set[tuple[UUID, Any]] = set()

    for hit in hits:
        key = (hit["book_id"], hit.get("page"))
        if key in seen:
            deferred.append(hit)
            continue
        seen.add(key)
        picked.append(hit)
        if len(picked) == limit:
            return picked

    picked.extend(deferred[: limit - len(picked)])
    # Back into retrieval order: the tutor numbers sources as it sees them, and the
    # reader should meet the nearest passage as [1].
    order = {id(hit): index for index, hit in enumerate(hits)}
    return sorted(picked, key=lambda hit: order[id(hit)])


def narrow(hits: list[Hit], *, limit: int) -> tuple[list[Hit], list[BookVote]]:
    """The whole pipeline: drop repeats, route to a book, then spread the budget."""
    deduped = dedupe(hits)
    routed, votes = route(deduped)
    return spread(routed, limit=limit), votes
