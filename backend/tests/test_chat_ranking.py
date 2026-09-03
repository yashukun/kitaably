"""Dedupe, the book vote, and page spread.

Pure functions over rows that already came back from the database, so they pin
exactly and need neither a model nor a schema.

The property that matters most is the last one in this file: **ranking narrows and
never admits**. Everything here runs after ``build_retrieval_filter`` has decided
what the caller may see, so a bug in this module can produce a worse answer but must
never produce one drawn from material the caller was not allowed to reach.
"""

from uuid import uuid4

import pytest

from app.rag.rank import MIN_CHUNKS, dedupe, narrow, route, spread, vote

CHEM = uuid4()
BIO = uuid4()


def hit(book, page, distance, text=None):
    """One retrieved row.

    The default text is distinct per (book, page, distance) on purpose. Real chunks
    differ, and a helper that handed every hit the same string would make the overlap
    detector look like it was collapsing unrelated passages when it was correctly
    collapsing identical ones.
    """
    chunk_id = uuid4()
    return {
        "chunk_id": chunk_id,
        "book_id": book,
        "book_title": "Chemistry" if book == CHEM else "Biology",
        "page": page,
        "scope": "personal",
        "text": text if text is not None else f"passage {chunk_id} on page {page}",
        "distance": distance,
    }


# --- overlap ----------------------------------------------------------------


def test_two_chunks_from_one_page_collapse() -> None:
    """Chunks carry ~100 tokens of deliberate overlap, so neighbours across a
    boundary are near-identical. Left alone the model cites the same passage twice."""
    hits = [hit(CHEM, 12, 0.10), hit(CHEM, 12, 0.14), hit(CHEM, 40, 0.20)]
    assert [row["page"] for row in dedupe(hits)] == [12, 40]


def test_the_nearest_of_a_duplicate_pair_survives() -> None:
    hits = [hit(CHEM, 12, 0.10), hit(CHEM, 12, 0.31)]
    assert dedupe(hits)[0]["distance"] == 0.10


def test_the_same_page_number_in_two_books_is_not_a_duplicate() -> None:
    """Page 12 of the chemistry book and page 12 of the biology book have nothing to
    do with each other. Keying on page alone would silently drop one."""
    hits = [hit(CHEM, 12, 0.10), hit(BIO, 12, 0.12)]
    assert len(dedupe(hits)) == 2


def test_near_identical_text_collapses_across_pages() -> None:
    """A chunk boundary can fall across a page boundary, and the overlap survives it.
    Identity on (book, page) does not catch that; text containment does."""
    shared = "the enthalpy of a system is the sum of its internal energy and"
    hits = [
        hit(CHEM, 12, 0.10, f"{shared} the product of pressure and volume"),
        hit(CHEM, 13, 0.11, f"{shared} the product of pressure and volume plus a note"),
    ]
    assert len(dedupe(hits)) == 1


def test_unrelated_passages_from_one_book_both_survive() -> None:
    """The failure mode of a similarity-based detector: two different passages from
    the same textbook share a great deal of vocabulary, and dropping one of them
    removes material the reader needed with no error and nothing in the answer to
    suggest anything is missing."""
    hits = [
        hit(CHEM, 10, 0.10, "The enthalpy of a system is the sum of its internal "
                            "energy and the product of its pressure and volume."),
        hit(CHEM, 88, 0.14, "The entropy of a system is a measure of the number of "
                            "microstates available to it at a given energy."),
    ]
    assert len(dedupe(hits)) == 2


def test_dedupe_preserves_rank_order() -> None:
    """Ordering comes from the database. Nothing here may re-sort it."""
    hits = [hit(CHEM, 1, 0.10), hit(BIO, 2, 0.15), hit(CHEM, 3, 0.20)]
    assert [row["distance"] for row in dedupe(hits)] == [0.10, 0.15, 0.20]


# --- which book -------------------------------------------------------------


def test_the_book_with_most_of_the_evidence_wins() -> None:
    """This is the whole of "work out which book to answer from": the evidence is
    already in hand, so it costs no model call and no label written at ingest."""
    hits = [
        hit(CHEM, 1, 0.10),
        hit(CHEM, 2, 0.12),
        hit(CHEM, 3, 0.15),
        hit(CHEM, 4, 0.18),
        hit(BIO, 9, 0.33),
    ]
    kept, votes = route(hits)
    assert votes[0].book_id == CHEM
    assert {row["book_id"] for row in kept} == {CHEM}


def test_a_near_miss_in_another_book_is_dropped() -> None:
    """One passing mention in a survey text should not dilute the answer that came
    from the book actually covering the topic."""
    hits = [hit(CHEM, p, 0.10 + p / 100) for p in range(1, 6)] + [hit(BIO, 9, 0.34)]
    kept, _ = route(hits)
    assert all(row["book_id"] == CHEM for row in kept)


def test_a_split_question_keeps_both_books() -> None:
    """"How does the textbook's account differ from the paper's" is a real question,
    and a forced single winner answers half of it."""
    hits = [
        hit(CHEM, 1, 0.12),
        hit(BIO, 2, 0.13),
        hit(CHEM, 3, 0.14),
        hit(BIO, 4, 0.15),
        hit(CHEM, 5, 0.16),
        hit(BIO, 6, 0.17),
    ]
    kept, votes = route(hits)
    assert votes[0].share < 0.62
    assert {row["book_id"] for row in kept} == {CHEM, BIO}


def test_routing_never_starves_the_answer() -> None:
    """A vote decisive enough to act on must still leave enough to synthesise from.
    Narrowing to one chunk is worse than not narrowing at all."""
    hits = [hit(CHEM, 1, 0.10), hit(BIO, 2, 0.30), hit(BIO, 3, 0.31), hit(BIO, 4, 0.32)]
    kept, _ = route(hits)
    assert len(kept) >= min(MIN_CHUNKS, len(hits))


def test_a_nearer_passage_outvotes_more_distant_ones() -> None:
    """Similarity is weighted, not counted. Three barely-inside-threshold mentions
    should not beat two strong passages."""
    hits = [hit(CHEM, 1, 0.05), hit(CHEM, 2, 0.06)] + [
        hit(BIO, p, 0.34) for p in range(3, 6)
    ]
    assert vote(hits)[0].book_id == CHEM


def test_the_vote_reports_the_books_it_rejected() -> None:
    """"It answered from the wrong book" is undebuggable without knowing what the
    alternatives scored, so the losers come back too."""
    hits = [hit(CHEM, 1, 0.10), hit(CHEM, 2, 0.11), hit(CHEM, 3, 0.12), hit(BIO, 9, 0.30)]
    _, votes = route(hits)
    assert {v.book_id for v in votes} == {CHEM, BIO}
    assert sum(v.share for v in votes) == pytest.approx(1.0)


# --- spread -----------------------------------------------------------------


def test_the_budget_buys_breadth_not_one_page_five_times() -> None:
    hits = [hit(CHEM, 7, 0.10 + n / 100, f"passage {n} " * 12) for n in range(5)]
    hits += [hit(CHEM, 20, 0.30, "distinct material about something else entirely")]
    picked = spread(hits, limit=3)
    assert len({row["page"] for row in picked}) == 2


def test_the_nearest_hit_is_always_kept() -> None:
    hits = [hit(CHEM, 7, 0.05)] + [hit(CHEM, 7, 0.10 + n / 100) for n in range(5)]
    assert spread(hits, limit=2)[0]["distance"] == 0.05


def test_spread_returns_results_in_retrieval_order() -> None:
    """The tutor numbers sources as it sees them, so the reader must meet the
    nearest passage as [1]."""
    hits = [hit(CHEM, p, 0.10 + p / 100) for p in (1, 1, 2, 3, 4)]
    picked = spread(hits, limit=4)
    assert picked == sorted(picked, key=lambda row: row["distance"])


# --- the safety property ----------------------------------------------------


@pytest.mark.parametrize("limit", [1, 3, 8, 50], ids=lambda v: f"limit={v}")
def test_narrowing_never_invents_a_chunk(limit: int) -> None:
    """The one property this module must not break. Everything here runs *after*
    scoping, so it may return fewer chunks, in a different selection — but every
    chunk it returns has to be one the caller was already allowed to see."""
    hits = [hit(CHEM, p, 0.10 + p / 100) for p in range(1, 6)]
    hits += [hit(BIO, p, 0.20 + p / 100) for p in range(1, 4)]
    allowed = {row["chunk_id"] for row in hits}

    kept, _ = narrow(hits, limit=limit)

    assert {row["chunk_id"] for row in kept} <= allowed
    assert len(kept) <= max(limit, MIN_CHUNKS)


def test_narrowing_an_empty_result_is_empty() -> None:
    kept, votes = narrow([], limit=8)
    assert kept == [] and votes == []


# --- fusing two rankings (the lookup path, D22) -----------------------------
#
# A mention question is searched twice — lexically and by vector — and `fuse`
# merges the two rankings by reciprocal rank. Same law as everything above:
# fusing reorders and merges what the scope filter already admitted; it never adds.


def test_fuse_prefers_a_chunk_both_searches_found() -> None:
    """Appearing in both lists beats a high rank in one. That is the point of
    fusing: the words and the meaning agree on this passage."""
    from app.rag.rank import fuse

    shared = hit(CHEM, 10, None)
    lexical = [hit(CHEM, 5, None), shared]
    vector = [dict(shared), hit(BIO, 3, 0.2)]

    fused = fuse(lexical, vector)
    assert fused[0]["chunk_id"] == shared["chunk_id"]


def test_fuse_breaks_ties_toward_the_first_ranking() -> None:
    """The caller passes the list whose ordering should win first — lexical, for a
    mention question, because the literal occurrence is the answer."""
    from app.rag.rank import fuse

    lex_only = hit(CHEM, 5, None)
    vec_only = hit(BIO, 7, 0.15)

    fused = fuse([lex_only], [vec_only])
    assert [row["chunk_id"] for row in fused] == [lex_only["chunk_id"], vec_only["chunk_id"]]


def test_fuse_emits_each_chunk_once() -> None:
    from app.rag.rank import fuse

    shared = hit(CHEM, 10, None)
    fused = fuse([shared, hit(CHEM, 12, None)], [dict(shared)])

    assert len([row for row in fused if row["chunk_id"] == shared["chunk_id"]]) == 1


def test_fuse_backfills_a_distance_the_first_list_lacked() -> None:
    """A lexical hit has no distance; if the vector search also found the chunk,
    the real distance rides along so logging and the book vote see it."""
    from app.rag.rank import fuse

    shared_lex = hit(CHEM, 10, None)
    shared_vec = {**shared_lex, "distance": 0.12}

    fused = fuse([shared_lex], [shared_vec])
    assert fused[0]["distance"] == 0.12


def test_fuse_never_admits() -> None:
    """The union of what came in is the ceiling of what comes out."""
    from app.rag.rank import fuse

    lexical = [hit(CHEM, 1, None), hit(CHEM, 2, None)]
    vector = [hit(BIO, 3, 0.1)]

    admitted = {row["chunk_id"] for row in fuse(lexical, vector)}
    assert admitted == {row["chunk_id"] for row in lexical + vector}


# --- the same passage in two books ------------------------------------------


def test_an_identical_passage_in_two_books_collapses() -> None:
    """The same book uploaded twice must not fill the source budget twice.

    Observed live: two copies of one 232-page science text returned every
    passage at an identical distance, so a five-source answer saw two or three
    distinct passages and the dominance vote split 50/50 between the copies.
    """
    shared = "Sodium reacts vigorously with water to form sodium hydroxide. " * 6
    kept = dedupe([hit(CHEM, 12, 0.11, shared), hit(BIO, 40, 0.11, shared)])

    assert len(kept) == 1
    # The strongest instance survives, so the citation still points at the top hit.
    assert kept[0]["book_id"] == CHEM


def test_different_passages_in_two_books_both_survive() -> None:
    """Collapsing is about identical text, never about two books agreeing."""
    kept = dedupe(
        [
            hit(CHEM, 12, 0.11, "Sodium reacts vigorously with water. " * 8),
            hit(BIO, 40, 0.12, "Photosynthesis converts light into chemical energy. " * 8),
        ]
    )
    assert len(kept) == 2


def test_collapsing_a_duplicate_lets_one_book_win_the_vote() -> None:
    """The point of the collapse: routing can fire again.

    With the duplicate kept, each copy scored half the evidence and no book
    cleared the dominance threshold, so `route` kept both and spent the budget
    showing the reader the same pages twice.
    """
    shared = "Sodium reacts vigorously with water to form sodium hydroxide. " * 6
    hits = [
        hit(CHEM, 12, 0.11, shared),
        hit(BIO, 40, 0.11, shared),
        hit(CHEM, 13, 0.14, "Potassium is even more reactive than sodium. " * 8),
        hit(CHEM, 14, 0.16, "Calcium reacts less vigorously with cold water. " * 8),
    ]
    votes = vote(dedupe(hits))
    assert votes[0].book_id == CHEM
    assert votes[0].share > 0.9


def test_a_hit_with_no_distance_still_votes() -> None:
    """Lexical hits carry a ts_rank, not a cosine distance.

    The focused path fuses them in from the salvage tier, and `vote` used to call
    `float(None)` on the first one — a crash on the exact path that exists to
    rescue a question that would otherwise have been refused.
    """
    hits = [hit(CHEM, 12, None), hit(CHEM, 13, 0.2), hit(BIO, 40, None)]
    votes = vote(hits)

    assert {v.book_id for v in votes} == {CHEM, BIO}
    assert all(v.share > 0 for v in votes)
    # Chemistry supplied two passages to Biology's one, so it leads.
    assert votes[0].book_id == CHEM


def test_narrow_survives_a_fused_lexical_hit() -> None:
    """The whole pipeline, on the shape the salvage tier actually produces."""
    hits = [hit(CHEM, 12, None), hit(CHEM, 13, 0.2), hit(CHEM, 14, 0.3)]
    kept, votes = narrow(hits, limit=2)

    assert len(kept) == 2
    assert votes[0].book_id == CHEM
