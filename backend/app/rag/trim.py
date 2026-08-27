"""Excerpt a retrieved passage down to the part that answers the question. Phase 7.

Why this exists: **prompt evaluation is linear in prompt length, and on a CPU model
it is the larger half of the wait.** Measured on this stack, a 3B model reads its
prompt at ~114 tokens/second — so five 320-token passages is fourteen seconds of the
reader staring at "Reading your books…" before the first word of the answer appears.

Most of that prompt is not doing any work. A chunk is a unit of *indexing*, sized to
what the embedder can read in one go; it is not a unit of *evidence*. A passage
retrieved because two of its sentences answer the question still carries eight that
do not, and the model pays to read all ten.

So the chunk stays whole everywhere it matters — it is what was indexed, what was
scored, and what the citation points the reader at — and only the copy handed to the
model is excerpted. Nothing here changes what was retrieved, what is cited, or what
the reader can open.

Everything below is pure, deterministic and offline. There is no model call here: a
model that summarised each passage would cost more time than it saved, and would put
a paraphrase where the grounding rules require the book's own words.
"""

import re

# Rough sentence split. Deliberately not a tokenizer: this only decides where an
# excerpt may begin and end, and being one clause out costs a few tokens rather than
# a wrong answer. The lookbehind keeps common abbreviations from splitting mid-
# sentence, which is what makes a passage read as prose rather than as fragments.
_SENTENCE = re.compile(
    r"(?<![A-Z][a-z]\.)(?<!\b[A-Z]\.)(?<!\betc\.)(?<!\bi\.e\.)(?<!\be\.g\.)(?<!\bvs\.)"
    r"(?<=[.!?])\s+"
)

_WORD = re.compile(r"[a-z0-9]+")

# Words that appear in every question and therefore separate nothing. Kept short on
# purpose: an aggressive stoplist starts removing terms that carry the subject.
_STOPWORDS = frozenset(
    """
    a an and are as at be been but by can could did do does for from had has have how
    i if in into is it its me my of on or our so than that the their them then there
    these they this toup us was we were what when where which who why will with would
    you your about explain describe tell give show
    """.split()
)

CHARS_PER_TOKEN = 4


def _tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


def _terms(query: str) -> set[str]:
    return {word for word in _WORD.findall(query.lower()) if word not in _STOPWORDS}


def split_sentences(text: str) -> list[str]:
    """Sentences, in order, with blank ones dropped. Never returns empty for non-blank text."""
    parts = [part.strip() for part in _SENTENCE.split(text.strip()) if part.strip()]
    return parts or ([text.strip()] if text.strip() else [])


def _score(sentence: str, terms: set[str]) -> int:
    """How many of the question's distinct terms this sentence contains.

    Distinct, not total: a sentence that says "enzyme" nine times is about enzymes
    exactly as much as one that says it once, and counting repeats would let a
    keyword-dense but uninformative line outrank the one that actually explains.
    """
    if not terms:
        return 0
    present = {word for word in _WORD.findall(sentence.lower())}
    return len(terms & present)


def excerpt(text: str, query: str, *, max_tokens: int) -> str:
    """The best contiguous run of sentences in ``text``, within ``max_tokens``.

    Contiguous, and in the original order — never a bag of the highest-scoring
    sentences stitched together. Two sentences that were three paragraphs apart read
    as a contradiction when they are placed side by side, and the tutor is required
    to answer from what the sources literally say.

    A passage already inside the budget is returned untouched, so short chunks and
    tables are never mangled. Where the text is genuinely cut, an ellipsis marks the
    join: the model is told plainly that it is reading an extract, which is the honest
    thing to put in a prompt that also forbids inventing what is not there.

    Falls back to the head of the passage when the question shares no vocabulary with
    it — the leading sentences are the best available guess, and it is still the
    passage the retriever chose.
    """
    if max_tokens <= 0 or _tokens(text) <= max_tokens:
        return text

    sentences = split_sentences(text)
    if len(sentences) <= 1:
        # One long sentence, or an unsplittable block. Cut on a word boundary rather
        # than returning something over budget.
        limit = max_tokens * CHARS_PER_TOKEN
        head = text[:limit].rsplit(" ", 1)[0].rstrip()
        return f"{head} …" if head else text[:limit]

    costs = [_tokens(sentence) for sentence in sentences]
    scores = [_score(sentence, _terms(query)) for sentence in sentences]

    # Widest window that fits, best total score; ties go to the earlier window, which
    # keeps the excerpt near the start of the passage where context usually is.
    best = (-1, 0, 0)  # (score, start, end-exclusive)
    for start in range(len(sentences)):
        total_cost = 0
        total_score = 0
        for end in range(start, len(sentences)):
            total_cost += costs[end]
            if total_cost > max_tokens and end > start:
                break
            total_score += scores[end]
            if total_score > best[0]:
                best = (total_score, start, end + 1)

    _, start, end = best
    if start == 0 and end == len(sentences):
        return text

    body = " ".join(sentences[start:end])
    return f"{'… ' if start > 0 else ''}{body}{' …' if end < len(sentences) else ''}"
