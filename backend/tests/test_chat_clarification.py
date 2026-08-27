"""The two-turn contract around "Which book do you mean?" (D24).

A clarifying question is only worth asking if the machinery can hear the answer.
These pin the pure pieces: recognising that the previous turn asked, recovering
the question that triggered it, telling a bare selection ("Epic Shit", "why are
you asking again. Epic Shit") from a fresh question that happens to name a book,
and recalling a book the conversation already pinned so it is never asked twice.

The fixtures are lifted from the transcript that exposed the gap.
"""

from uuid import uuid4

from app.db.models import Book
from app.rag import prompts
from app.services.chat import (
    _book_from_conversation,
    _is_bare_selection,
    _match_titles,
    _original_question,
    _pick_book_pending,
)


def book(title: str) -> Book:
    row = Book(title=title)
    row.id = uuid4()
    return row


EPIC = book("Epic Shit")
TEST = book("test")

ASK_WHICH = prompts.pick_book_reply(["Epic Shit", "test"])


def user(content: str) -> dict:
    return {"role": "user", "content": content}


def tutor(content: str) -> dict:
    return {"role": "assistant", "content": content}


# --- recognising that we just asked -----------------------------------------


def test_the_ask_which_reply_is_recognised_by_its_own_heading() -> None:
    history = [user("what is the title about?"), tutor(ASK_WHICH)]
    assert _pick_book_pending(history) is True


def test_an_ordinary_answer_is_not_mistaken_for_the_ask() -> None:
    history = [user("what is enthalpy?"), tutor("## Enthalpy\n\nIt is [1]…")]
    assert _pick_book_pending(history) is False
    assert _pick_book_pending([]) is False


def test_the_original_question_is_recovered() -> None:
    history = [user("what is the title about?"), tutor(ASK_WHICH)]
    assert _original_question(history) == "what is the title about?"


# --- a selection is a selection, not a topic --------------------------------


def test_a_bare_title_is_a_selection() -> None:
    assert _is_bare_selection("Epic Shit", [EPIC]) is True


def test_a_complaint_wrapped_around_the_title_is_still_a_selection() -> None:
    """The transcript's exact turn: the exasperated re-answer was embedded whole,
    complaint included, and refused. The complaint is filler; the title is the
    answer."""
    assert _is_bare_selection("why are you asking again.  Epic Shit", [EPIC]) is True
    assert _is_bare_selection("ok, Epic Shit please", [EPIC]) is True


def test_a_fresh_question_naming_the_book_is_not_a_selection() -> None:
    """"What does Epic Shit say about money?" answers the ask AND asks something
    new — it must be processed as the fresh question, not as a bare pick."""
    assert _is_bare_selection("what does Epic Shit say about money?", [EPIC]) is False


# --- the conversation as memory ---------------------------------------------


def test_a_book_the_reader_named_is_recalled_instead_of_re_asking() -> None:
    """The transcript's third failure: "what is the actual title of the book?"
    was met with "which book do you mean?" one turn after the reader said which."""
    history = [
        user("what is the title about?"),
        tutor(ASK_WHICH),
        user("Epic Shit"),
        tutor("## How the author defines it…"),
    ]
    recalled = _book_from_conversation(history, [EPIC, TEST])
    assert recalled is EPIC


def test_assistant_turns_never_count_as_a_selection() -> None:
    """The ask-which reply names EVERY book — reading it as a selection would
    resolve the ambiguity to whichever title happened to sort first."""
    history = [user("what is the title about?"), tutor(ASK_WHICH)]
    assert _book_from_conversation(history, [EPIC, TEST]) is None


def test_naming_several_books_settles_nothing() -> None:
    history = [user("compare Epic Shit and test")]
    assert _book_from_conversation(history, [EPIC, TEST]) is None


def test_titles_too_short_to_be_distinctive_never_match() -> None:
    """A book called "It" must not match half the English language."""
    it = book("It")
    assert _match_titles("what is it about?", [it]) == []
