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
