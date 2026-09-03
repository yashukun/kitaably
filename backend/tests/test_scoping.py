"""The scoping tests — the boundary everything downstream assumes is right.

One of the four things CLAUDE.md requires tests for before merge, because a silent
bug here is a privacy incident rather than a broken page.

These compile the predicate and assert on its SQL. That is deliberate: the question
is "what can this clause possibly match", and a clause that cannot name a column
cannot leak through it, whatever the data happens to be on any given day.

The model these pin (DECISIONS.md D16):

    canon     shared with every signed-in user
    personal  private to its owner. No exceptions, and no account that outranks it.
"""

from uuid import UUID, uuid4

import pytest

from app.core.security import Principal
from app.db.models.enums import Role
from app.rag.retrieve import build_retrieval_filter, canon_clause, personal_clause

ALICE = Principal(id=uuid4(), role=Role.USER, email="alice@example.test")
BOB = Principal(id=uuid4(), role=Role.USER, email="bob@example.test")


def sql(clause) -> str:
    return str(clause.compile(compile_kwargs={"literal_binds": True}))


def uid(value: UUID) -> str:
    """SQLAlchemy renders a UUID literal without hyphens."""
    return value.hex


# --- nobody reaches anybody else's personal material ------------------------


@pytest.mark.parametrize("principal", [ALICE, BOB], ids=["alice", "bob"])
def test_personal_is_always_bound_to_the_caller(principal: Principal) -> None:
    """The failure this guards against is `scope = 'personal'` standing alone, which
    would match every user's private uploads at once."""
    rendered = sql(build_retrieval_filter(principal))

    assert "'personal'" in rendered
    assert uid(principal.id) in rendered
    # The personal branch never appears without an owner test beside it.
    assert "chunks.owner_id" in rendered


@pytest.mark.parametrize("principal", [ALICE, BOB], ids=["alice", "bob"])
def test_filter_never_names_another_user(principal: Principal) -> None:
    other = ALICE if principal is BOB else BOB
    rendered = sql(build_retrieval_filter(principal))

    assert uid(other.id) not in rendered


def test_every_caller_gets_the_same_shape() -> None:
    """Two filters differ in the owner id and in nothing else.

    This is the whole access model in one assertion: there is no account whose
    predicate is wider than anyone else's, so there is no account to escalate into.
    """
    alice_sql = sql(build_retrieval_filter(ALICE))
    bob_sql = sql(build_retrieval_filter(BOB))

    assert alice_sql.replace(uid(ALICE.id), "OWNER") == bob_sql.replace(uid(BOB.id), "OWNER")


# --- assessment generation is bound to the author (D29) ---------------------
#
# Generation draws from what the AUTHOR may read: canon plus their own uploads.
# The rule that survives the widening is the one that matters — the pool can never
# contain anyone ELSE'S personal book, because the filter is the same author-bound
# clause every reader gets, and there is no argument that can widen it.


@pytest.mark.parametrize("author", [ALICE, BOB], ids=["alice", "bob"])
def test_generation_pool_never_reaches_another_users_personal_book(
    author: Principal,
) -> None:
    """An author examines on their own material and everyone's shared material —
    never on somebody else's private upload."""
    other = ALICE if author is BOB else BOB
    rendered = sql(build_retrieval_filter(author))

    assert "'canon'" in rendered
    # The personal branch exists, and it names the author alone.
    assert "'personal'" in rendered
    assert "chunks.owner_id" in rendered
    assert uid(author.id) in rendered
    assert uid(other.id) not in rendered


# --- the building blocks ----------------------------------------------------


def test_canon_clause_takes_no_scoping_argument() -> None:
    """Canon is one platform-wide pool. There is no parameter to get wrong, and no
    way to express "someone else's canon" because there is no such thing."""
    rendered = sql(canon_clause())

    assert "'canon'" in rendered
    assert "owner_id" not in rendered


def test_personal_clause_is_bound_to_one_owner() -> None:
    owner = uuid4()
    rendered = sql(personal_clause(owner))

    assert "'personal'" in rendered
    assert uid(owner) in rendered


def test_or_clause_is_grouped_when_combined_with_other_filters() -> None:
    """SQL binds AND tighter than OR, so an ungrouped `A OR B AND C` combined with a
    further AND would silently widen the match. SQLAlchemy parenthesises it; this
    pins that behaviour, because losing it is a leak rather than an error."""
    from sqlalchemy import select

    from app.db.models import Chunk

    query = (
        select(Chunk.id)
        .where(build_retrieval_filter(ALICE))
        .where(Chunk.embedding.is_not(None))
    )
    where = str(query.compile(compile_kwargs={"literal_binds": True})).split("WHERE")[1]

    assert where.strip().startswith("(")
    assert ") AND chunks.embedding IS NOT NULL" in where


# --- the reader's book picker only ever subtracts ---------------------------
#
# `search_chunks(..., book_ids=[...])` lets someone focus a question on chosen books.
# Those ids arrive in a request body, so they are untrusted, and the whole safety
# argument is that they are applied *on top of* the scope clause rather than instead
# of it. These pin that argument in the compiled SQL, where it either holds or does
# not — the same reason the tests above assert on a clause rather than on rows.


def compiled_search(principal: Principal, book_ids: list[UUID] | None) -> str:
    """The SQL `search_chunks` would run, without a database or an embedding."""
    from sqlalchemy import select

    from app.db.models import Chunk

    query = (
        select(Chunk.id)
        .where(build_retrieval_filter(principal))
        .where(Chunk.embedding.is_not(None))
    )
    if book_ids:
        query = query.where(Chunk.book_id.in_(book_ids))
    return str(query.compile(compile_kwargs={"literal_binds": True}))


def test_a_picked_book_is_added_with_and_never_or() -> None:
    """The whole safety argument in one assertion. `AND book_id IN (...)` can only
    remove rows; `OR book_id IN (...)` would hand the caller every chunk of any book
    they can name — including somebody else's private upload."""
    rendered = compiled_search(ALICE, [uuid4()])
    where = rendered.split("WHERE")[1]

    assert " AND chunks.book_id IN " in where
    assert "OR chunks.book_id IN" not in where


def test_naming_someone_elses_book_does_not_widen_the_filter() -> None:
    """Bob asks about a book id he found somewhere. The scope clause still names only
    Bob, so the narrowing intersects a set those chunks were never in."""
    somebody_elses_book = uuid4()
    rendered = compiled_search(BOB, [somebody_elses_book])

    assert uid(BOB.id) in rendered
    assert uid(ALICE.id) not in rendered
    # The personal branch is still bound to the caller, not loosened by the request.
    assert "chunks.owner_id" in rendered


def test_the_scope_clause_is_identical_with_and_without_a_picker() -> None:
    """`book_ids` must not change what scope means — only how much of it is searched.
    If these ever diverge, the picker has become an authorization input."""
    without = compiled_search(ALICE, None).split("WHERE")[1]
    with_picker = compiled_search(ALICE, [uuid4(), uuid4()]).split("WHERE")[1]

    scope_part = without.split("AND chunks.embedding")[0]
    assert with_picker.startswith(scope_part)


@pytest.mark.parametrize("book_ids", [None, []], ids=["none", "empty"])
def test_no_picked_books_means_all_of_the_caller_s_material(book_ids) -> None:
    """Empty is the default the UI sends, and it must mean "everything I may see"
    rather than "nothing" — an empty IN list would refuse every question."""
    assert "chunks.book_id IN" not in compiled_search(ALICE, book_ids)


# --- the two other query paths share the boundary (D22) ---------------------
#
# The lookup path (full-text) and the overview path (coverage sample) are new ways
# of *sampling inside* the caller's scope, never new scopes. Both queries are
# built pure in `app/rag/retrieve.py` precisely so these tests can compile the
# exact SQL the production path runs, not a reconstruction of it.


def compiled_lexical(principal: Principal, book_ids: list[UUID] | None) -> str:
    from sqlalchemy.dialects import postgresql

    from app.rag.retrieve import lexical_query

    query = lexical_query(
        build_retrieval_filter(principal), "productivity", top_k=20, book_ids=book_ids
    )
    return str(
        query.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


def compiled_coverage(principal: Principal, book_ids: list[UUID]) -> str:
    from sqlalchemy.dialects import postgresql

    from app.rag.retrieve import coverage_query

    query = coverage_query(
        build_retrieval_filter(principal), book_ids=book_ids, per_book=5, min_tokens=60
    )
    return str(
        query.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


def _uid_in(principal: Principal, rendered: str) -> bool:
    """The pg dialect may render a UUID literal with or without hyphens."""
    return uid(principal.id) in rendered or str(principal.id) in rendered


@pytest.mark.parametrize("principal", [ALICE, BOB], ids=["alice", "bob"])
def test_lexical_search_runs_under_the_same_boundary(principal: Principal) -> None:
    """A mention search is a new way of ordering the caller's own material, never a
    way past the filter: personal still bound to the caller, canon still the pool."""
    rendered = compiled_lexical(principal, None)
    other = ALICE if principal is BOB else BOB

    assert "'canon'" in rendered
    assert "'personal'" in rendered
    assert "chunks.owner_id" in rendered
    assert _uid_in(principal, rendered)
    assert not _uid_in(other, rendered)


def test_lexical_picker_is_added_with_and_never_or() -> None:
    where = compiled_lexical(ALICE, [uuid4()]).split("WHERE")[1]

    assert " AND chunks.book_id IN " in where
    assert "OR chunks.book_id IN" not in where


def test_lexical_search_matches_the_index_expression() -> None:
    """The GIN index is on to_tsvector('english', text). A drifted expression does
    not error — Postgres just stops using the index, silently, at sequential-scan
    cost. The expression is the contract; this pins it."""
    rendered = compiled_lexical(ALICE, None)

    assert "to_tsvector('english', chunks.text)" in rendered
    assert "websearch_to_tsquery" in rendered


@pytest.mark.parametrize("principal", [ALICE, BOB], ids=["alice", "bob"])
def test_coverage_sample_runs_under_the_same_boundary(principal: Principal) -> None:
    """"Summarize this book" samples inside the caller's scope. A book id the
    caller may not read contributes zero rows, because the scope clause sits on
    the same inner query that does the sampling."""
    rendered = compiled_coverage(principal, [uuid4()])
    other = ALICE if principal is BOB else BOB

    assert "'canon'" in rendered
    assert "'personal'" in rendered
    assert _uid_in(principal, rendered)
    assert not _uid_in(other, rendered)
    # The named books are a narrowing AND-ed onto the scope, exactly as the picker.
    assert "chunks.book_id IN" in rendered
    assert "OR chunks.book_id IN" not in rendered


async def _nothing(session, scored):
    """Stand-in for `_enrich`, which needs a real session to hydrate a hit."""
    return []


# --- the salvage tier changes the index, never the predicate -----------------
#
# When the strict search finds nothing, retrieval takes a second look before it
# refuses (D31). That is the one place in the pipeline that deliberately relaxes a
# retrieval parameter, so it is the one place worth proving relaxes only the
# parameter it meant to.


async def test_salvage_searches_under_the_callers_own_filter(monkeypatch) -> None:
    """Both salvage passes get the predicate the strict pass was given.

    A second look that built its own filter — or dropped it, being "only a
    fallback" — would be a scope bypass reachable from any question the material
    does not cover, which is to say from any question at all.
    """
    from app.services import chat

    seen: list[dict] = []

    async def fake_terms(session, question, scope_filter, **kwargs):
        seen.append({"kind": "terms", "scope": scope_filter, **kwargs})
        return ["sphincter"]

    async def fake_corroborated(session, vector, terms, scope_filter, **kwargs):
        seen.append({"kind": "corroborated", "scope": scope_filter, **kwargs})
        return []

    monkeypatch.setattr(chat, "significant_terms", fake_terms)
    monkeypatch.setattr(chat, "search_chunks_corroborated", fake_corroborated)

    scope = build_retrieval_filter(ALICE)
    assert await chat._salvage(None, "sphincter", [0.0] * 384, scope, book_ids=None) == []

    assert [call["kind"] for call in seen] == ["terms", "corroborated"]
    for call in seen:
        # The identical predicate object, not a rebuilt equivalent.
        assert call["scope"] is scope
        assert uid(ALICE.id) in sql(call["scope"])


async def test_salvage_relaxes_only_the_corroboration_ceiling(monkeypatch) -> None:
    """The one number that moves, and it is not a retrieval ceiling.

    Measured, ``bge-small-en-v1.5`` scores questions the book has nothing on at
    0.36–0.45 and questions it covers well at 0.17–0.29 — the bands touch, so no
    wider SEARCH can separate them. This ceiling is applied to passages the
    reader's own words already named, where the same scoring does separate
    cleanly (0.36–0.38 against 0.46–0.61).
    """
    from app.core.config import settings
    from app.services import chat

    seen: list[dict] = []

    async def fake_terms(session, question, scope_filter, **kwargs):
        return ["sphincter"]

    async def fake_corroborated(session, vector, terms, scope_filter, **kwargs):
        seen.append(kwargs)
        return []

    monkeypatch.setattr(chat, "significant_terms", fake_terms)
    monkeypatch.setattr(chat, "search_chunks_corroborated", fake_corroborated)

    await chat._salvage(
        None, "sphincter", [0.0] * 384, build_retrieval_filter(ALICE), book_ids=None
    )

    assert seen[0]["max_distance"] == settings.retrieval_salvage_distance
    assert settings.retrieval_salvage_distance > settings.retrieval_max_distance


async def test_salvage_reuses_the_query_vector(monkeypatch) -> None:
    """No second embedding. The question is a poor retriever here and a perfectly
    good comparator, which is the whole trick — and it costs nothing extra."""
    from app.services import chat

    vector = [0.5] * 384
    seen: list[list[float]] = []

    async def fake_terms(session, question, scope_filter, **kwargs):
        return ["sphincter"]

    async def fake_corroborated(session, query_vector, terms, scope_filter, **kwargs):
        seen.append(query_vector)
        return []

    async def unexpected_embed(text):
        raise AssertionError("the salvage must not embed a second time")

    monkeypatch.setattr(chat, "significant_terms", fake_terms)
    monkeypatch.setattr(chat, "search_chunks_corroborated", fake_corroborated)
    monkeypatch.setattr(chat.embeddings, "embed_query", unexpected_embed)

    await chat._salvage(
        None, "sphincter", vector, build_retrieval_filter(ALICE), book_ids=None
    )

    assert seen == [vector]


async def test_salvage_narrowing_still_only_subtracts(monkeypatch) -> None:
    """`book_ids` reaches every pass, so a reader's book selection is honoured on
    the fallback path exactly as it is on the first one."""
    from app.services import chat

    chosen = [uuid4()]
    seen: list[list | None] = []

    async def fake_terms(session, question, scope_filter, **kwargs):
        seen.append(kwargs.get("book_ids"))
        return ["sphincter"]

    async def fake_corroborated(session, vector, terms, scope_filter, **kwargs):
        seen.append(kwargs.get("book_ids"))
        return []

    monkeypatch.setattr(chat, "significant_terms", fake_terms)
    monkeypatch.setattr(chat, "search_chunks_corroborated", fake_corroborated)

    await chat._salvage(
        None, "sphincter", [0.0] * 384, build_retrieval_filter(ALICE), book_ids=chosen
    )

    assert seen == [chosen, chosen]


async def test_salvage_stops_when_the_corpus_knows_none_of_the_words(monkeypatch) -> None:
    """No selective word means nothing to look up, and nothing else runs."""
    from app.services import chat

    ran: list[str] = []

    async def no_terms(session, question, scope_filter, **kwargs):
        return []

    async def fake_corroborated(*args, **kwargs):
        ran.append("corroborated")
        return []

    monkeypatch.setattr(chat, "significant_terms", no_terms)
    monkeypatch.setattr(chat, "search_chunks_corroborated", fake_corroborated)

    result = await chat._salvage(
        None, "the offside rule", [0.0] * 384, build_retrieval_filter(ALICE), book_ids=None
    )

    assert result == []
    assert ran == []


async def test_a_word_the_book_merely_contains_is_not_coverage(monkeypatch) -> None:
    """"What were the causes of the French Revolution" keeps "french", which a
    chemistry passage happens to use. Scored against the question that passage is
    at 0.531 — outside the ceiling — so the refusal stands."""
    from app.services import chat

    async def fake_terms(session, question, scope_filter, **kwargs):
        return ["french"]

    async def nothing_survived(*args, **kwargs):
        return []

    monkeypatch.setattr(chat, "significant_terms", fake_terms)
    monkeypatch.setattr(chat, "search_chunks_corroborated", nothing_survived)

    assert (
        await chat._salvage(
            None,
            "what were the causes of the french revolution",
            [0.0] * 384,
            build_retrieval_filter(ALICE),
            book_ids=None,
        )
        == []
    )


async def test_an_empty_salvage_is_still_a_refusal(monkeypatch) -> None:
    """The refusal did not go away — it moved behind a second look.

    "Nothing within 0.35 for the sentence as typed" and "the material does not
    cover this" are different statements, and only the second is worth telling a
    reader. When the second look comes back empty the second statement is true,
    and it is still made with no model call at all (CLAUDE.md invariant 5).
    """
    from app.services import chat

    async def nothing(*args, **kwargs):
        return []

    async def fake_embed(text):
        return [0.0] * 384

    monkeypatch.setattr(chat, "search_chunks", nothing)
    monkeypatch.setattr(chat, "significant_terms", nothing)
    monkeypatch.setattr(chat.embeddings, "embed_query", fake_embed)

    assert await chat.retrieve_for(None, ALICE, "quantum chromodynamics") == []
