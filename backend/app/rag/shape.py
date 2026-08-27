"""What SHAPE of retrieval a question needs. Phase 7.

Intent (``app/rag/intent.py``) decides *whether* a message goes to the books at
all. This module decides *how*, because a single top-k vector search answers only
one kind of question well — a focused one, with a subject of its own that some
passage actually discusses. Three whole families of legitimate questions embed to
nothing useful:

    "Summarize this book"            has no subject to embed. Its nearest chunks
                                     are noise, and the honest machinery returns
                                     "your books don't cover that" about a book
                                     the reader can see. A summary question is a
                                     question about ALL of the book, so it is
                                     answered from a coverage sample in reading
                                     order, not from similarity.

    "Find every mention of X"        is lexical, not semantic. Vector search finds
                                     passages ABOUT a topic; it does not find
                                     every passage NAMING one. Answered from the
                                     full-text index, fused with the vector hits.

    "Compare what these books say"   is exactly the question book routing
                                     (rank.route) exists to defeat: the dominant-
                                     book vote would throw away half the answer.
                                     Answered with a per-book quota instead.

    "Who wrote this book?"           is not in the text at all. Author, length
                                     and kind live on the ``books`` row — facts
                                     the server is already holding — so the
                                     record answers, with no search and no model.

Same architecture as intent classification (DECISIONS.md D19, D22): rules decide,
offline, on the path every question takes — and here there is no model fallback at
all, because the failure direction is safe. A shape misread as FOCUSED produces
exactly the old behaviour, which was correct code with a blind spot; nothing here
can widen what a caller may read, because every shape retrieves under the same
``build_retrieval_filter`` and differs only in how it samples inside it.
"""

import re
from dataclasses import dataclass
from enum import StrEnum

from app.rag.intent import normalise


class QueryShape(StrEnum):
    """How a question wants its material gathered.

    Not persisted — this is a routing decision inside one turn, recomputed every
    time, so it is not a Postgres enum and adding a value is not a migration.
    """

    FOCUSED = "focused"    # a subject of its own → vector search (the original path)
    OVERVIEW = "overview"  # about the book(s) as a whole → coverage sample
    LOOKUP = "lookup"      # where/whether something is mentioned → lexical + vector
    COMPARE = "compare"    # across books → per-book quota, routing disabled
    METADATA = "metadata"  # about the book as an OBJECT → the books row, no search


@dataclass(frozen=True, slots=True)
class QueryProfile:
    """The shape, plus what the rules could extract while deciding it.

    ``topic`` is the subject with the operator phrase stripped: "does this book
    mention artificial intelligence" retrieves far better on "artificial
    intelligence" than on the whole sentence, whose strongest tokens are "book"
    and "mention". ``None`` means the question has no extractable subject — an
    overview, or a holistic comparison ("which book is most beginner-friendly"),
    which is answered from a coverage sample rather than a search.

    ``all_books`` and ``single_book`` record what the reader pointed at, so the
    caller can resolve "these books" to the whole visible library and can notice
    that "this book" is ambiguous when several are visible. They are requests,
    never authorizations — resolution happens against the books the principal may
    lawfully see, exactly as ``book_ids`` narrowing always has.

    ``fact`` is set only for METADATA: which column of the record was asked for
    ("author", "pages", "genre", "title"). The server answers it from the ``books``
    row it is already holding — asking retrieval, or a model, to find something
    the process knows for certain would be inventing an opportunity to get it
    wrong, exactly the argument behind the library reply (D19).
    """

    shape: QueryShape
    topic: str | None = None
    all_books: bool = False
    single_book: bool = False
    fact: str | None = None


# Which books the reader is waving at. "these books" / "all my books" means the
# whole visible library; "this book" means one — ambiguous when several are
# visible, and the caller handles that rather than guessing.
_ALL_BOOKS = re.compile(
    r"\b(?:all|these|those|both|every) (?:of )?(?:the |my |your )?(?:uploaded )?books\b"
    r"|\ball (?:my |the )?(?:books|uploads|material)\b"
    r"|\b(?:my|our) books\b"
    r"|\bmy (?:whole |entire )?library\b"
    r"|\bacross (?:the |my |all )?books\b"
)
# "The title" is how people refer to the book as often as "the book" is — "what
# is the title about?" — so it counts as pointing at one.
_SINGLE_BOOK = re.compile(r"\b(?:this|the) (?:book|title)\b")

# A cross-book question has to actually point at books or authors. Without this,
# "compare mitosis and meiosis" — a focused question a vector search answers
# perfectly — would be dragged into the compare path and split by book for no
# reason.
_BOOKISH = r"(?:books?|authors?|texts?|uploads?|sources?)"

# --------------------------------------------------------------------- compare

_COMPARE = (
    re.compile(r"^compare\b.*\b" + _BOOKISH + r"\b"),
    re.compile(
        r"\bwhich (?:book|one|text|author)\b.{0,60}"
        r"\b(?:better|best|easier|clearer|more clearly|most|friendly|prefer)"
    ),
    re.compile(r"\bwhich book (?:is|explains?|covers?|teaches|describes?|discusses)\b"),
    re.compile(
        r"\b(?:common|shared|similar|overlap\w*)\b.{0,60}"
        r"\b(?:across|between|among|in)\b.{0,40}\b" + _BOOKISH
    ),
    re.compile(_BOOKISH + r".{0,40}\bin common\b|\bin common\b.{0,40}" + _BOOKISH),
    re.compile(_BOOKISH + r".{0,40}\b(?:disagree|differ|conflict|contradict)"),
    re.compile(r"\b(?:disagree|differ|conflict|contradict)\w*.{0,40}" + _BOOKISH),
    re.compile(r"\bdifference between\b.{0,40}" + _BOOKISH),
    re.compile(r"\bwriting styles?\b.{0,40}" + _BOOKISH),
)

_COMPARE_TOPIC = (
    re.compile(r"\bbooks? say about\s+(?P<topic>.+)$"),
    re.compile(r"\bauthors? say about\s+(?P<topic>.+)$"),
    re.compile(
        r"\bwhich book (?:explains?|covers?|teaches|describes?|discusses)\s+(?P<topic>.+?)"
        r"(?:\s+(?:better|best|more clearly|most clearly|more|most|well))?$"
    ),
    re.compile(r"\b(?:disagree|differ|conflict|contradict)\w* (?:about|on|over)\s+(?P<topic>.+)$"),
    re.compile(r"^compare\b.{0,40}\b(?:on|about)\s+(?P<topic>.+)$"),
)

# -------------------------------------------------------------------- metadata

# Questions about the book as an OBJECT — who wrote it, how long it is, what kind
# of thing it is. The answers live on the `books` row, not in any chunk, so a
# vector search over the text literally cannot contain them and honestly refuses
# about a fact the server is holding. Each pattern names the record column asked
# for; an optional <title> capture distinguishes "who wrote this book" (implicit
# subject — the book at hand) from "who wrote Hamlet" (a named work, which is only
# a metadata question if the library actually holds a book by that name — the
# caller falls back to a content search when it does not).
_METADATA: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bwho (?:wrote|authored)\b(?:\s+(?P<title>.+))?$"), "author"),
    (re.compile(r"\bwho(?:'s| is) the (?:author|writer)\b(?: of\s+(?P<title>.+))?$"), "author"),
    (re.compile(r"\bwritten by whom\b"), "author"),
    (
        re.compile(
            r"\bhow many pages\b(?:\s+(?:is|does|are|in|has))?"
            r"(?:\s+(?P<title>.+?))?(?:\s+have)?$"
        ),
        "pages",
    ),
    (re.compile(r"\bhow long is (?:this|that|the) book\b"), "pages"),
    (
        re.compile(
            r"\bwhat (?:genre|kind of book|type of book|category) (?:is|are)\b"
            r"(?:\s+(?P<title>.+))?$"
        ),
        "genre",
    ),
    (
        re.compile(
            r"\bwhat(?:'s| is) (?:this|that|the) book(?:'s)? (?:called|titled|name|title)\b"
        ),
        "title",
    ),
    (re.compile(r"\b(?:name|title) of (?:this|that|the) book\b"), "title"),
)

# A captured "title" that is really a demonstrative — "who wrote this book" puts
# "this book" in the capture — means the subject is implicit, not named.
_DEMONSTRATIVE_TITLE = re.compile(
    r"^(?:this|that|these|those|the|it|my)\b.*\bbooks?$|^(?:this|that|it)$"
)


def _metadata_match(cleaned: str) -> tuple[str, str | None] | None:
    """``(fact, named_title)`` when this is a question about the record itself."""
    for pattern, fact in _METADATA:
        match = pattern.search(cleaned)
        if not match:
            continue
        claimed = (match.groupdict().get("title") or "").strip()
        if claimed and not _DEMONSTRATIVE_TITLE.match(claimed):
            return fact, _clean_topic(claimed)
        return fact, None
    return None


# ---------------------------------------------------------------------- lookup

# Each pattern both recognises the shape and captures the subject. The operator
# words ("mention", "find every", "which chapters discuss") are what make these
# lexical: the reader is asking where something appears, and the literal
# occurrences are the answer.
_LOOKUP = (
    re.compile(
        r"\b(?:does|do|did) (?:this|the|these|those|that|it|any(?: of)?(?: my| the| these)?|my) "
        r"books? (?:ever )?(?:mention|cover|discuss|talk about|say anything about"
        r"|include|contain|touch on)\s+(?P<topic>.+)$"
    ),
    re.compile(
        r"\bfind (?:me )?(?:every|all|each|any) "
        r"(?:mention|reference|instance|occurrence)s? (?:of|to)\s+(?P<topic>.+)$"
    ),
    re.compile(r"\b(?:every|all) (?:mention|reference)s? (?:of|to)\s+(?P<topic>.+)$"),
    re.compile(
        r"\b(?:what|which) (?:chapter|section|part|page)s? "
        r"(?:discuss|cover|mention|talk about|deal with|explain|contain)s?\s+(?P<topic>.+)$"
    ),
    re.compile(
        r"\bwhere (?:does|do) (?:the |this |these |my )?books? "
        r"(?:explain|discuss|cover|mention|introduce|define|talk about)\s+(?P<topic>.+)$"
    ),
    re.compile(
        r"\bwhere (?:is|are)\s+(?P<topic>.+?)\s+"
        r"(?:explained|discussed|mentioned|covered|defined|introduced)\b"
    ),
    re.compile(
        r"\bwhat (?:does|do) (?:the |this )?(?:authors?|books?|text|it) say about\s+"
        r"(?P<topic>.+)$"
    ),
    re.compile(
        r"\b(?:the )?authors?['’]?s? (?:views?|opinions?|stance|position|take|thoughts?) on\s+"
        r"(?P<topic>.+)$"
    ),
)

# -------------------------------------------------------------------- overview

# Whole-book questions. A "summary" is only an overview when it is a summary OF
# the material — "summarise chapter three" and "summarize the krebs cycle" name
# their own subject and retrieve fine as focused questions.
_OVERVIEW = (
    re.compile(
        r"\bsummar\w*\b.{0,60}\b(?:book|books|this|it|everything|library|material|uploads?)\b"
    ),
    re.compile(r"\b(?:book|books)\b.{0,40}\bsummar\w*\b"),
    re.compile(r"\btl;?dr\b"),
    # "the title" and bare "it"/"this" both mean the book at hand — "what is the
    # title about?" refused about a book it could summarize, purely on vocabulary.
    re.compile(r"\bwhat (?:is|are) (?:this|the|these) (?:books?|title) about\b"),
    re.compile(r"\bwhat(?:'s| is) (?:it|this) about\b"),
    re.compile(r"\bmain (?:message|idea|point|argument|thesis|theme|takeaway)s?\b"),
    re.compile(
        r"\b(?:key|biggest|most important|top \d+|core|main|central) "
        r"(?:lessons?|takeaways?|insights?|ideas?|concepts?|points?|things?|messages?|themes?)\b"
    ),
    re.compile(r"\bmost important things?\b"),
    re.compile(r"^(?:explain|describe) (?:this|the) book\b"),
    re.compile(r"\bwhat should i (?:learn|read|know|master|study|focus on)\b"),
    re.compile(
        r"\b(?:concepts?|chapters?|topics?|things?) (?:should|do) i "
        r"(?:master|learn|read|study|know|focus on)\b"
    ),
    re.compile(r"\b(?:most )?important chapters?\b"),
    re.compile(r"\bwhich chapters? (?:should i read|are (?:the )?(?:most )?important|to read)\b"),
    re.compile(r"\bassumptions? (?:does|do) the authors? make\b"),
    re.compile(r"\bauthors?['’]?s? assumptions?\b"),
    re.compile(r"\bwhere (?:should|do) i (?:start|begin)\b"),
)

_ARTICLE = re.compile(r"^(?:the|a|an)\s+")


def _clean_topic(raw: str) -> str | None:
    """The captured subject, made searchable.

    Strips quotes, trailing punctuation and a leading article; collapses
    whitespace; caps the length so a runaway capture cannot smuggle a whole
    paragraph into a tsquery. Returns ``None`` when nothing survives, which the
    caller treats as "no topic" rather than as an error.
    """
    topic = re.sub(r"\s+", " ", raw.strip().strip("\"'“”‘’")).rstrip("?.!,;: ")
    topic = _ARTICLE.sub("", topic)
    topic = topic.strip()
    if not topic:
        return None
    return topic[:120]


def _first_topic(patterns: tuple[re.Pattern[str], ...], cleaned: str) -> str | None:
    for pattern in patterns:
        match = pattern.search(cleaned)
        if match and "topic" in match.groupdict() and match.group("topic"):
            topic = _clean_topic(match.group("topic"))
            if topic:
                return topic
    return None


def classify(text: str) -> QueryProfile:
    """Decide how this question wants its material gathered. Pure and offline.

    Order matters and is deliberate: compare before lookup, because "compare what
    these books say about leadership" contains a lookup-shaped tail ("say about
    leadership") that must not capture the whole question; lookup before overview,
    because "which chapters discuss investing" wants the mentions, not the book's
    shape. Anything unrecognised is FOCUSED — the original path, safe by
    construction, and the reason this router never needs a model fallback.
    """
    cleaned = normalise(text)
    all_books = bool(_ALL_BOOKS.search(cleaned))
    single_book = bool(_SINGLE_BOOK.search(cleaned)) and not all_books

    if any(pattern.search(cleaned) for pattern in _COMPARE):
        return QueryProfile(
            shape=QueryShape.COMPARE,
            topic=_first_topic(_COMPARE_TOPIC, cleaned),
            all_books=all_books,
            single_book=False,
        )

    record = _metadata_match(cleaned)
    if record is not None:
        fact, named = record
        # A subjectless ask — "who is the author?", "how many pages?" — means the
        # book at hand, so it rides the single-book resolution (picker, matched
        # title, ask-which). A NAMED work stays unflagged: whether it is a record
        # question or a content question depends on whether the library holds a
        # book by that name, which only the caller can check.
        implicit = named is None and not all_books
        return QueryProfile(
            shape=QueryShape.METADATA,
            topic=named,
            all_books=all_books,
            single_book=single_book or implicit,
            fact=fact,
        )

    topic = _first_topic(_LOOKUP, cleaned)
    if topic is not None:
        return QueryProfile(
            shape=QueryShape.LOOKUP,
            topic=topic,
            all_books=all_books,
            single_book=single_book,
        )

    if any(pattern.search(cleaned) for pattern in _OVERVIEW):
        return QueryProfile(
            shape=QueryShape.OVERVIEW,
            topic=None,
            all_books=all_books,
            single_book=single_book,
        )

    return QueryProfile(
        shape=QueryShape.FOCUSED,
        topic=None,
        all_books=all_books,
        single_book=single_book,
    )
