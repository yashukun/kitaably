"""The query-shape router — how a question wants its material gathered (D22).

The fixture list below is the actual product brief: the question families that
used to fall through to a focused vector search and come back as a refusal about
a book the reader can see. Each is pinned to the shape that answers it, and the
focused cases are pinned too — because the failure direction of this router is
"everything is FOCUSED", which is safe, and the failure direction of an
over-eager rule is a real question quietly answered from the wrong machinery.
"""

import pytest

from app.rag.shape import QueryShape, classify

OVERVIEW = QueryShape.OVERVIEW
LOOKUP = QueryShape.LOOKUP
COMPARE = QueryShape.COMPARE
FOCUSED = QueryShape.FOCUSED


# --- the brief: summaries and takeaways -------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "What are the 5 most important things I should learn from these books?",
        "Summarize this book in 10 bullet points.",
        "Give me a 2-minute summary of this book.",
        "What is the main message of this book?",
        "What are the key lessons from all uploaded books?",
        "What are the biggest insights from these books?",
    ],
    ids=repr,
)
def test_summary_and_takeaway_questions_are_overviews(text: str) -> None:
    assert classify(text).shape is OVERVIEW


# --- the brief: learning and understanding ----------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Explain this book as if I'm a beginner.",
        "What concepts should I master first?",
        "What are the most important chapters to read?",
        "Explain the core ideas with simple examples.",
        "What assumptions does the author make?",
    ],
    ids=repr,
)
def test_whole_book_learning_questions_are_overviews(text: str) -> None:
    assert classify(text).shape is OVERVIEW


def test_what_the_author_means_by_a_term_is_focused() -> None:
    """A specific term has a subject of its own; vector search answers it well."""
    assert classify('What does the author mean by "flow state"?').shape is FOCUSED


# --- the brief: mention questions -------------------------------------------


@pytest.mark.parametrize(
    ("text", "topic"),
    [
        ("Does this book mention artificial intelligence?", "artificial intelligence"),
        ("What does the author say about leadership?", "leadership"),
        ("Find every mention of productivity.", "productivity"),
        ("What are the author's views on remote work?", "remote work"),
        ("Which chapters discuss investing?", "investing"),
        ("Where does the book explain recursion?", "recursion"),
    ],
    ids=repr,
)
def test_mention_questions_are_lookups_with_the_topic_extracted(
    text: str, topic: str
) -> None:
    """The topic matters as much as the shape: "does this book mention X" embeds
    dominated by "book" and "mention", and searches terribly as a whole sentence."""
    profile = classify(text)
    assert profile.shape is LOOKUP
    assert profile.topic == topic


# --- the brief: cross-book comparison ---------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Compare what these books say about leadership.",
        "Which book explains machine learning better?",
        "What ideas are common across all uploaded books?",
        "Where do these authors disagree?",
        "Which book is most beginner-friendly?",
        "Compare the writing styles of these authors.",
    ],
    ids=repr,
)
def test_cross_book_questions_are_comparisons(text: str) -> None:
    assert classify(text).shape is COMPARE


@pytest.mark.parametrize(
    ("text", "topic"),
    [
        ("Compare what these books say about leadership.", "leadership"),
        ("Which book explains machine learning better?", "machine learning"),
    ],
    ids=repr,
)
def test_topical_comparisons_extract_their_topic(text: str, topic: str) -> None:
    assert classify(text).topic == topic


@pytest.mark.parametrize(
    "text",
    ["Which book is most beginner-friendly?", "Compare the writing styles of these authors."],
    ids=repr,
)
def test_holistic_comparisons_have_no_topic(text: str) -> None:
    """Nothing to search for — style and accessibility live in a cross-section,
    not in a similarity hit, so these are answered from coverage samples."""
    assert classify(text).topic is None


def test_comparing_two_concepts_is_not_a_cross_book_comparison() -> None:
    """"Compare mitosis and meiosis" is a focused question a vector search answers
    perfectly. Only a question that points at books or authors is split by book."""
    assert classify("compare mitosis and meiosis").shape is FOCUSED


# --- pointing: which books the reader waved at ------------------------------


@pytest.mark.parametrize(
    ("text", "all_books", "single_book"),
    [
        ("What are the key lessons from all uploaded books?", True, False),
        ("What are the biggest insights from these books?", True, False),
        ("Summarize this book in 10 bullet points.", False, True),
        ("What is the main message of this book?", False, True),
    ],
    ids=repr,
)
def test_the_router_records_what_was_pointed_at(
    text: str, all_books: bool, single_book: bool
) -> None:
    profile = classify(text)
    assert profile.all_books is all_books
    assert profile.single_book is single_book


# --- the safe direction ------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "what is enthalpy?",
        "explain the krebs cycle",
        "define entropy",
        "summarise chapter three",
        "how does photosynthesis work",
        "walk me through the proof of the chain rule",
    ],
    ids=repr,
)
def test_focused_questions_stay_focused(text: str) -> None:
    """The original path is the default, and a summary that names its own subject
    ("summarise chapter three") retrieves fine as written."""
    assert classify(text).shape is FOCUSED


def test_topic_extraction_survives_quotes_and_punctuation() -> None:
    profile = classify('Does this book mention "deliberate practice"?!')
    assert profile.shape is LOOKUP
    assert profile.topic == "deliberate practice"


def test_a_runaway_topic_is_capped() -> None:
    profile = classify("find every mention of " + "very " * 200 + "long topics")
    assert profile.shape is LOOKUP
    assert profile.topic is not None
    assert len(profile.topic) <= 120


# --- questions about the record, not the text (D23) --------------------------
#
# "Who wrote this book" is not in any chunk — author, length and kind live on the
# `books` row, so these route to the record and never to a search. The one subtle
# case is a NAMED work: "who wrote Hamlet" is only a record question if the
# library holds a book by that name, so it carries the name out for the caller to
# check, and the caller falls back to a content search when nothing matches.

METADATA = QueryShape.METADATA


@pytest.mark.parametrize(
    ("text", "fact"),
    [
        ("Who wrote this book?", "author"),
        ("who authored this book", "author"),
        ("Who is the author?", "author"),
        ("How many pages is this book?", "pages"),
        ("how many pages?", "pages"),
        ("how long is the book", "pages"),
        ("What genre is this book?", "genre"),
        ("what kind of book is this", "genre"),
        ("what is this book called?", "title"),
    ],
    ids=repr,
)
def test_record_questions_route_to_metadata(text: str, fact: str) -> None:
    profile = classify(text)
    assert profile.shape is METADATA
    assert profile.fact == fact


@pytest.mark.parametrize(
    "text",
    ["Who wrote this book?", "Who is the author?", "how many pages?"],
    ids=repr,
)
def test_a_subjectless_record_question_means_the_book_at_hand(text: str) -> None:
    """"Who is the author?" names nothing, so it rides the single-book resolution
    — the picker, a matched title, or the ask-which reply."""
    profile = classify(text)
    assert profile.single_book is True
    assert profile.topic is None


def test_a_named_work_carries_its_name_out_for_the_caller_to_check() -> None:
    """Whether "who wrote Hamlet" is a record question depends on whether the
    library holds a Hamlet — only the caller can know, so the name travels."""
    profile = classify("who wrote hamlet?")
    assert profile.shape is METADATA
    assert profile.topic == "hamlet"
    assert profile.single_book is False


def test_who_questions_about_content_stay_focused() -> None:
    """"Who discovered penicillin" is a content question a vector search answers;
    only wrote/authored/author phrasings reach for the record."""
    assert classify("who discovered penicillin?").shape is FOCUSED


# --- "the title" is how people say "the book" --------------------------------


@pytest.mark.parametrize(
    "text",
    ["what is the title about?", "What's it about?", "what is this about"],
    ids=repr,
)
def test_the_title_and_bare_it_mean_the_book_at_hand(text: str) -> None:
    """"What is the title about?" refused about a book it could summarize, purely
    because the overview vocabulary knew "book" but not "title" or bare "it"."""
    assert classify(text).shape is OVERVIEW


def test_the_title_rides_single_book_resolution() -> None:
    """With several books visible, "the title" is as ambiguous as "this book" —
    it asks which, rather than averaging the library."""
    assert classify("what is the title about?").single_book is True


# --- the brief: the book's own table of contents -----------------------------
#
# The question that sent this whole family to a refusal: the chapter list is
# `public.chapters`, written at ingest, and no passage contains it. Measured on
# the NCERT Science 10th upload, "how many chapters does this book have" has its
# nearest passage at cosine distance 0.43 against a 0.35 ceiling — so the tutor
# refused about fifteen rows it was holding.


@pytest.mark.parametrize(
    "text",
    [
        "how many chapter does this book have what are the names?",
        "How many chapters does this book have?",
        "What are the chapter names?",
        "What are the chapter titles?",
        "List the chapters",
        "list all the chapters",
        "Give me the chapter titles.",
        "Show me its chapters",
        "Names of all the chapters",
        "Table of contents",
        "toc",
        "What chapters are in this book?",
        "chapter names",
    ],
    ids=repr,
)
def test_chapter_list_questions_are_metadata(text: str) -> None:
    profile = classify(text)
    assert profile.shape is QueryShape.METADATA
    assert profile.fact == "chapters"


@pytest.mark.parametrize(
    "text",
    [
        "which chapters discuss photosynthesis",
        "what chapters cover acids and bases",
        "which chapters mention the krebs cycle",
        "what sections talk about enthalpy",
    ],
    ids=repr,
)
def test_a_chapter_question_with_a_subject_is_a_lookup(text: str) -> None:
    """The reader wants the passages, not the outline.

    This is the boundary the record answer must not cross. "Which chapters
    discuss X" names a subject, and answering it with a table of contents would
    be a confidently wrong answer to a question the search path handles well.
    """
    assert classify(text).shape is LOOKUP


@pytest.mark.parametrize(
    "text",
    [
        "which chapters should I read",
        "what are the most important chapters",
        "which chapters are the most important",
    ],
    ids=repr,
)
def test_chapter_advice_questions_stay_overviews(text: str) -> None:
    """"Which should I read" is a judgement about the material, not its index."""
    assert classify(text).shape is OVERVIEW


def test_a_chapter_question_points_at_one_book() -> None:
    """So it rides the same single-book resolution as "who wrote this book"."""
    assert classify("how many chapters does this book have?").single_book is True
