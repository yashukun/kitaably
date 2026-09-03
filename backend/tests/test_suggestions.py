"""Suggestions: what to offer when somebody picks a book.

Two things are being defended here, and only one of them is cosmetic.

The cosmetic one is usefulness — a suggestion nobody would click is wasted space.

The other one is not. A suggested question is a *promise that the material answers
it*, and the reader clicks it precisely because they trust that. So a suggestion the
book cannot answer is worse than an empty strip: it manufactures the grounded refusal
this feature exists to prevent, and it does so having pointed at the trap itself.
That is why the tiers degrade to nothing rather than to something invented.
"""

import re
from uuid import UUID, uuid4

from app.db.models.book import Chapter
from app.rag.brief import usable_topic
from app.services import suggestions


class _SessionReturning:
    """The narrowest possible stand-in for an AsyncSession.

    `titled_chapters` issues one `scalars()` and shapes the result; the DB round trip
    is not what is under test here and a real one would need a container. What IS
    under test is the shaping — which chapters survive, and which books drop out.
    """

    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    async def scalars(self, _query: object) -> list[object]:
        return self._rows


def _chapter(book_id: UUID, index: int, title: str | None) -> Chapter:
    return Chapter(book_id=book_id, index=index, title=title)


# A chapter end, as a textbook actually prints one.
EXERCISE_PAGE = """
Chapter 4 closed with the reaction of metals with oxygen.

EXERCISES

1. Why does a metal displace hydrogen from a dilute acid?
2. Draw a labelled diagram of the electrolytic refining of copper.
3. What happens when zinc reacts with copper sulphate solution?
4. Explain, with an example, how alloys differ from their constituent metals.
"""

# A book that prints no exercises anywhere.
PROSE_PAGE = """
The morning was cold, and she had not slept. What was there to say to a man who had
already decided? She put the kettle on and waited for the light to reach the window.
"""


# --- tier one: the book's own questions ---------------------------------------


def test_a_printed_exercise_list_is_recognised_as_one() -> None:
    """The precondition for every suggestion in tier one: `carries_questions` must
    agree that the BOOK said these are questions — a heading, or numbering — rather
    than the regex having noticed a question mark in a paragraph."""
    assert suggestions.harvest.carries_questions(
        EXERCISE_PAGE, minimum=suggestions._MIN_EXERCISE_ITEMS
    )
    assert not suggestions.harvest.carries_questions(
        PROSE_PAGE, minimum=suggestions._MIN_EXERCISE_ITEMS
    )


def test_a_question_the_tutor_cannot_answer_is_not_suggested() -> None:
    """"Draw a labelled diagram" is a perfectly good exam question and a terrible chat
    suggestion. Harvesting deliberately does not make this judgement — its header says
    so, because a paper CAN carry it: somebody sits that with a pencil. A suggestion
    cannot, because it promises an answer and the tutor has no hands."""
    found = [q.text for q in suggestions.harvest.find_questions(EXERCISE_PAGE)]
    drawing = [text for text in found if "labelled diagram" in text]
    assert drawing, "fixture no longer contains the case this test exists for"

    kept = [text for text in found if suggestions._usable_as_a_suggestion(text)]
    assert not any("labelled diagram" in text for text in kept)
    # And it removed only that one — a filter that ate the whole list would pass the
    # assertion above while making the feature useless.
    assert any("displace hydrogen" in text for text in kept)


def test_a_suggestion_stays_short_enough_to_read_at_a_glance() -> None:
    """It renders as a chip beside an input, not as a paragraph."""
    assert not suggestions._usable_as_a_suggestion("x" * 400)


def test_suggestions_are_deduplicated_case_insensitively_and_capped() -> None:
    """A book that repeats its exercises across chunks should not offer the same
    question three times, and the strip has a size."""
    items = ["What is an alloy?", "what is an alloy?", "Why do metals conduct?"]
    assert suggestions._deduped(items, limit=5) == [
        "What is an alloy?",
        "Why do metals conduct?",
    ]
    assert len(suggestions._deduped([f"q{n}" for n in range(20)], limit=5)) == 5


# --- tier two, and the honest floor -------------------------------------------


async def test_a_book_with_no_real_outline_offers_nothing() -> None:
    """`rag/chunk.py` emits a single synthetic `Chapter(title="Full document")` when no
    outline could be trusted. Turning that into "What does 'Full document' cover?"
    would point the reader at a heading nobody wrote.

    The `< 2` rule is the same one `chat.py :: _outline_for` applies. Driven through
    the real function against a fake session, so it is the shipped filter being tested
    rather than a restatement of it.
    """
    book_id = uuid4()
    outlines = await suggestions.titled_chapters(
        _SessionReturning([_chapter(book_id, 0, "Full document")]), [book_id]
    )
    assert outlines == {}


async def test_a_book_with_a_real_outline_keeps_every_titled_chapter() -> None:
    """The other direction, so the rule above cannot pass by rejecting everything."""
    book_id = uuid4()
    outlines = await suggestions.titled_chapters(
        _SessionReturning(
            [
                _chapter(book_id, 0, "Metals and Non-metals"),
                _chapter(book_id, 1, "Acids, Bases and Salts"),
                _chapter(book_id, 2, None),  # an untitled chapter contributes nothing
            ]
        ),
        [book_id],
    )
    assert outlines == {book_id: ["Metals and Non-metals", "Acids, Bases and Salts"]}


async def test_no_books_means_no_query_and_no_suggestions() -> None:
    assert await suggestions.titled_chapters(_SessionReturning([]), []) == {}


# --- the round trip that makes a focus topic worth anything --------------------


def test_a_suggested_topic_survives_the_parser_that_will_read_it_back() -> None:
    """The focus box is parsed by `brief.read()`. A suggestion its own parser would
    discard is a suggestion that silently does nothing when clicked — the worst kind
    of nothing, because the author watches it fill the box and believes it took.

    Sharing `usable_topic` between the two ends is what stops them drifting; this
    asserts the sharing actually holds.
    """
    assert usable_topic("Metals and Non-metals") == "Metals and Non-metals"
    assert usable_topic("Acids, Bases and Salts") == "Acids, Bases and Salts"
    # The filler a chapter title sometimes is, which must not become a topic.
    assert usable_topic("Chapter") is None


def test_the_stoplist_still_rejects_the_words_a_bad_topic_is_made_of() -> None:
    """If this ever passes vacuously the round-trip test above proves nothing."""
    for filler in ("the book", "questions", "everything", "the exercises"):
        assert usable_topic(filler) is None


# --- scope: the one that would be a privacy incident --------------------------


def test_the_question_search_runs_under_the_shared_retrieval_predicate() -> None:
    """Invariant 1, at the only new place that reads chunk TEXT.

    A suggestions endpoint that searched outside `build_retrieval_filter` would put
    verbatim sentences from somebody else's private book into a stranger's UI — and
    it would do it on a route nobody thinks of as a retrieval path, which is exactly
    how a chokepoint gets bypassed. This asserts the module imports the real predicate
    rather than assembling its own WHERE, which is the rule CLAUDE.md states.
    """
    source = open(suggestions.__file__).read()
    assert "build_retrieval_filter" in source
    # No hand-rolled scope logic anywhere in the module.
    assert not re.search(r"scope\s*==\s*BookScope", source)
    assert "owner_id ==" not in source
