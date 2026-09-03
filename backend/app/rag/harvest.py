"""Finding the questions a book already asks. Phase 5c.

A textbook is not only material to write questions from — it is full of questions
already, and they are better than anything a 3B model will write about the same
passage. They were written by whoever wrote the book, they sit at the difficulty the
chapter is pitched at, and a student revising from that book has met them before.

**Nothing here calls a model.** This module reads chunk text and returns the question
spans it can prove are there: a numbered exercise list, an interrogative, an
imperative that names an exercise verb. Detection is deterministic on purpose — a
model asked "are there questions in this passage" says yes to a paragraph of prose,
and a harvested question that the book does not actually contain is worse than a
generated one, because it is presented to the author as the book's own.

The model's job comes after, and it is a different job: *answering* a question we
already hold verbatim (``rag/prompts.py :: harvest_prompt``). That split is what makes
this reliable. Generation asks a small model to invent a stem, options, a key and
provenance all at once; harvesting hands it the stem and asks only for the answer.

What this deliberately does NOT do is decide whether a question is any good. Filtering
"1. Draw a labelled diagram of the human eye" out of a paper that cannot render a
diagram is the format's job downstream, not a judgement made here on a regex.
"""

import re
from dataclasses import dataclass

# ---------------------------------------------------------------- shape rules

# A heading that says, in the book's own words, that what follows is a question set.
# The strongest signal there is, and the only one that promotes a chunk on its own.
_EXERCISE_HEADING = re.compile(
    r"^\s*(?:"
    r"exercises?(?:\s+[\d.]+)?"
    r"|questions?"
    r"|review\s+questions?"
    r"|(?:multiple[- ]choice|objective|short[- ]answer|long[- ]answer|"
    r"very\s+short\s+answer)\s+questions?"
    r"|problems?(?:\s+for\s+practice)?"
    r"|assignment"
    r"|self[- ](?:assessment|check|test)"
    r"|practice\s+(?:set|questions?|problems?)"
    r"|think\s+(?:about|and\s+answer)"
    r"|activity\s+questions?"
    r"|test\s+yourself"
    r"|check\s+your\s+(?:progress|understanding)"
    r")\s*[:.\d]*\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# The marker that starts a numbered item: "12.", "12)", "Q12.", "(iv)", "iv.", "(a)".
# Anchored to the start of a line, because a "2." mid-sentence is a decimal point and
# an unanchored version turned "3.14" into two questions.
_ITEM_MARKER = re.compile(
    r"^[ \t]*(?:"
    r"(?:Q|Ques\.?|Question)?[ \t]*(?P<num>\d{1,3})[.)]"
    r"|\((?P<paren>[ivxlcIVXLC]{1,5}|[a-z])\)"
    r"|(?P<roman>[ivxlc]{1,5})[.)]"
    r")[ \t]+(?=\S)",
    re.MULTILINE,
)

# Verbs a textbook uses to set an exercise. An imperative from this list makes a line
# a question even without a question mark — "Define an isosceles triangle." is an
# exam question and has no "?" in it, and requiring one lost most of a maths book.
_EXERCISE_VERB = re.compile(
    r"^(?:"
    r"define|explain|describe|discuss|state|name|list|write|give|mention|outline"
    r"|find|calculate|compute|evaluate|solve|determine|derive|prove|show|verify"
    r"|draw|sketch|label|plot|construct|complete|fill"
    r"|compare|contrast|differentiate|distinguish|classify|identify|match|arrange"
    r"|justify|comment|examine|analyse|analyze|interpret|illustrate|summarise"
    r"|summarize|suggest|predict|expand|convert|express|prove"
    r")\b",
    re.IGNORECASE,
)

# The book printing its own choice list under a stem.
_OPTION_LINE = re.compile(
    r"^[ \t]*(?:\((?P<paren>[a-dA-D])\)|(?P<bare>[a-dA-D])[.)])[ \t]+(?P<text>\S.*)$",
    re.MULTILINE,
)

# Lines that look like questions and are not. A table-of-contents entry ends in a page
# number behind dot leaders; a figure caption starts "Fig."; a running head is a bare
# chapter title in caps.
_NOT_A_QUESTION = re.compile(
    r"(?:\.{4,}\s*\d+\s*$)"  # dot leaders into a page number
    r"|^(?:fig|figure|table|plate|chart|example|activity|note|source)\s*[.\d]"
    r"|^\s*(?:reprint|isbn|copyright|page)\b"
    r"|^[^a-z]{12,}$",  # a running head: no lower-case letters at all
    re.IGNORECASE,
)

# Bounds. Shorter than this is a fragment a bad line break produced; longer is a
# passage the numbering swallowed, and the paragraph after the last exercise is
# always the one that gets swallowed.
MIN_QUESTION_CHARS = 25
MAX_QUESTION_CHARS = 420


@dataclass(frozen=True)
class BookQuestion:
    """One question the book itself asks, as the book prints it.

    ``text`` is verbatim, whitespace-normalised and nothing else. That is the whole
    contract: it is checked against the source chunk again before anything is stored,
    so a model handed this question cannot quietly improve it into a different one.
    """

    text: str
    number: str | None = None
    options: list[dict[str, str]] | None = None

    @property
    def has_options(self) -> bool:
        return bool(self.options and len(self.options) >= 2)


def _tidy(raw: str) -> str:
    """Collapse the whitespace a PDF extractor leaves behind, and nothing else."""
    return re.sub(r"\s+", " ", raw).strip(" \t\n·•-—")


def _looks_like_a_question(line: str) -> bool:
    if not (MIN_QUESTION_CHARS <= len(line) <= MAX_QUESTION_CHARS):
        return False
    if _NOT_A_QUESTION.search(line):
        return False
    if "?" in line:
        return True
    # No question mark: the line must be an IMPERATIVE from the exercise-verb list.
    # An interrogative opener is not enough without one, and that costs nothing worth
    # keeping — a numbered list of advice in a business book ("6. When in your 20s,
    # live like a pauper.") opens on "When" and is not a question anybody can be set.
    return bool(_EXERCISE_VERB.match(line))


def _split_options(body: str) -> tuple[str, list[dict[str, str]] | None]:
    """Separate a stem from the choice list the book printed under it.

    Returns the stem and the options, or the body unchanged and None. Only a run of
    at least two consecutive markers counts, and they must run in order from (a): a
    single "(b)" inside a sentence is a cross-reference to part (b), not a choice.
    """
    matches = list(_OPTION_LINE.finditer(body))
    if len(matches) < 2:
        return body, None

    keys = [(m.group("paren") or m.group("bare") or "").lower() for m in matches]
    expected = [chr(ord("a") + i) for i in range(len(keys))]
    if keys != expected:
        return body, None

    options = [
        {"key": key.upper(), "text": _tidy(match.group("text"))}
        for key, match in zip(keys, matches, strict=True)
    ]
    if any(not option["text"] for option in options):
        return body, None
    return body[: matches[0].start()], options


def _from_numbered_items(text: str) -> list[BookQuestion]:
    """Split a numbered exercise list into its items.

    Each marker owns everything up to the next marker, which is what keeps a
    two-sentence question whole — "9. What does one mean by exothermic and endothermic
    reactions? Give examples." is one exercise, not two.
    """
    markers = list(_ITEM_MARKER.finditer(text))
    if not markers:
        return []

    found: list[BookQuestion] = []
    for position, marker in enumerate(markers):
        end = markers[position + 1].start() if position + 1 < len(markers) else len(text)
        body = text[marker.end() : end]
        stem_text, options = _split_options(body)
        stem = _tidy(stem_text)
        if not _looks_like_a_question(stem):
            continue
        number = marker.group("num") or marker.group("paren") or marker.group("roman")
        found.append(BookQuestion(text=stem, number=number, options=options))
    return found


def _from_loose_lines(text: str) -> list[BookQuestion]:
    """Questions that are not in a numbered list — an in-text "What do you observe?".

    Stricter than the numbered path on purpose: without a marker, the only evidence
    that a line is a question is the line itself, so it must actually end in a
    question mark. An exercise verb is not enough here — half a textbook's prose
    begins "Note that" and "Consider".
    """
    found: list[BookQuestion] = []
    for raw_line in text.splitlines():
        line = _tidy(raw_line)
        if not line.endswith("?"):
            continue
        # A line that starts mid-sentence is the tail of one a bad line break cut in
        # half — "he same in both the test tubes?" is what a PDF extractor makes of
        # "Is the colour of the solution the same in both the test tubes?" and it is
        # not a question anybody can be asked.
        if not line[:1].isupper():
            continue
        if not _looks_like_a_question(line):
            continue
        found.append(BookQuestion(text=line))
    return found


def find_questions(text: str) -> list[BookQuestion]:
    """Every question this passage can be shown to contain, in the order it prints them.

    Numbered items first, because an exercise list is the reliable case and its
    numbering is the evidence. Loose interrogatives are added only when they are not
    already inside one of those items, so a question that ends an exercise is not
    harvested twice.
    """
    if not text or not text.strip():
        return []

    numbered = _from_numbered_items(text)
    seen = {question.text.lower() for question in numbered}
    loose = [
        question
        for question in _from_loose_lines(text)
        if question.text.lower() not in seen
        and not any(question.text.lower() in known for known in seen)
    ]
    return numbered + loose


def carries_questions(text: str, *, minimum: int) -> bool:
    """Is this chunk worth spending a harvesting call on?

    Two ways to qualify, and both require the *book* to have said "these are
    questions" rather than the regex having noticed a question mark:

    * an exercise heading, which is the book saying so in words; or
    * ``minimum`` NUMBERED items, which is the book saying so in numbering.

    Loose interrogatives never qualify a chunk on their own, however many there are,
    and that rule was written against a real result. A self-help book in the library
    scored fourteen chunks on lines like "Would you marry yourself?" — genuinely
    questions, genuinely the book's, and useless on an exam paper. The numbering and
    the heading are what separate an exercise from a rhetorical flourish.

    Loose questions are still *harvested* from a chunk that qualified some other way:
    an exercise set that ends with an unnumbered "What do you observe?" should keep it.
    """
    found = find_questions(text)
    if not found:
        return False
    if _EXERCISE_HEADING.search(text):
        return True
    return sum(1 for question in found if question.number) >= minimum
