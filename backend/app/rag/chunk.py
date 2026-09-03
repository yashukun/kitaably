"""Chapter detection and chunking. Phase 3, resized in Phase 7 (DECISIONS.md D21).

A chunk never spans a chapter boundary. Chapters are how someone selects material
for an assessment, so a chunk straddling two of them makes chapter-scoped generation
incoherent — the paper claims to cover chapter 3 and quietly does not.

**A chunk also never exceeds what the embedder can read.** ``bge-small-en-v1.5``
accepts 512 tokens and silently truncates past them — a longer passage returns a
vector byte-identical to the one its own first 512 tokens produce, so the remainder
is not down-weighted, it is *absent from the index*. The original 800-token cap put
79% of the library over that line, which is a retrieval bug with no symptom: search
simply never finds the tail of a chunk, and the answer comes back "your books don't
cover this" about material sitting in the database.

:func:`chunk_token_budget` is therefore the ceiling, not ``settings.chunk_tokens`` —
so a future edit to the setting cannot reintroduce the same silent failure.
"""

import re
from dataclasses import dataclass

from app.core.config import settings
from app.db.models.enums import SourceFormat
from app.rag import parse
from app.rag.parse import Page

# Token counting without a tokenizer dependency. bge-small uses a WordPiece
# vocabulary where English averages a shade under 4 characters per token; this
# approximation is within a few percent and costs nothing. Swap in the real
# tokenizer if chunk sizes ever need to be exact rather than approximately right.
CHARS_PER_TOKEN = 4


@dataclass(frozen=True, slots=True)
class Chapter:
    index: int
    title: str
    page_start: int
    page_end: int


@dataclass(frozen=True, slots=True)
class Chunk:
    index: int
    chapter_index: int
    page: int
    text: str
    token_count: int


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


# The estimate above under-counts dense technical prose (formulae, long compounds,
# citations tokenize far below four characters each), so a chunk measured at the
# model's exact limit can arrive over it. Spend a fifth of the window as margin
# rather than discover the shortfall as missing search results.
_TRUNCATION_MARGIN = 0.8


def chunk_token_budget() -> int:
    """The largest chunk that survives embedding intact.

    Configuration is a *request*, the embedder's sequence length is the fact. Where
    they disagree the fact wins, because the failure mode of losing is invisible:
    no error, no warning, just a vector that describes the first half of a passage
    and a second half nobody can ever retrieve.
    """
    ceiling = int(settings.embedding_max_tokens * _TRUNCATION_MARGIN)
    return max(1, min(settings.chunk_tokens, ceiling))


def detect_chapters(pages: list[Page], data: bytes, source_format: SourceFormat) -> list[Chapter]:
    """Structural metadata first, one synthetic chapter as the fallback.

    Trust an outline when the document carries one. Heading heuristics (font-size
    outliers, "Chapter N" patterns) are a later refinement — a wrong split is worse
    than no split, because it silently mislabels what a question covers.
    """
    last_page = pages[-1].number if pages else 1

    if source_format is SourceFormat.PDF:
        chapters = _chapters_from_pdf_outline(data, last_page)
        if chapters:
            return chapters

    if source_format is SourceFormat.ZIP:
        chapters = _chapters_from_zip_parts(pages, data, last_page)
        if chapters:
            return chapters

    return [Chapter(index=0, title="Full document", page_start=1, page_end=last_page)]


def _chapters_from_pdf_outline(data: bytes, last_page: int) -> list[Chapter]:
    import fitz

    with fitz.open(stream=data, filetype="pdf") as doc:
        outline = doc.get_toc() or []

    # Top-level entries only. Deeper nesting is section structure, not chapters.
    tops = [(title.strip(), page) for level, title, page in outline if level == 1 and page > 0]
    return _chapters_from_tops(tops, last_page)


# --- recovering a part's real title from its opening page ---------------------
#
# A zip of per-chapter PDFs whose parts carry no outline gets its chapter titles
# from the member FILENAMES, and NCERT-style downloads are named `jesc101.pdf`.
# Fifteen chapters called "jesc101 … jesc113" is a table of contents nobody can
# read, and it is what the reader picks from when scoping an assessment to a
# chapter.
#
# So: before falling back to the filename, look at the top of the part's first
# page for a heading. This does NOT move a boundary — the seams are still the
# uploaded files, exactly as before — it only labels one, which is why it is
# allowed to be a heuristic at all. `detect_chapters` refuses to guess where a
# chapter STARTS, because a wrong split silently mislabels what a question
# covers; a wrong title is visible to the reader the moment they look at it, and
# an unrecognised heading falls back to the filename we would have used anyway.

# "Chemical Reactions and Equations 1 CHAPTER" — title, number, then the word,
# which is how NCERT and several other Indian school texts set a chapter opener.
# Case-sensitive on purpose: it is the display capital that distinguishes a
# heading from the "Chapter 1" of a cross-reference or an answer key.
#
# Only the ANCHOR is matched here, and the title is taken by walking back from it
# (:func:`_title_from_page`). A pattern that captured the title directly has to
# guess how much of what precedes the anchor belongs to it, and PDF text order
# routinely puts a running head and a figure label in front: the real page reads
# "Science 208 Activity 13.1 … Our Environment 13 CHAPTER", where every word
# before "Our" is furniture.
_CHAPTER_ANCHOR = re.compile(r"\s\d{1,2}\s+CHAPTER\b")

# "CHAPTER 3 — Metals and Non-metals", or the title on the following line.
_CHAPTER_LEADING = re.compile(
    r"^chapter\s+\d{1,2}\b[\s:.\u2013\u2014-]*(?P<title>.*)$", re.IGNORECASE
)

# A running head sitting in front of the title on the same line: "Science 58
# Carbon and its Compounds". Stripped only when what remains still reads as a
# title, so it can never eat the title itself.
_RUNNING_HEAD = re.compile(r"^[A-Z][A-Za-z]*\s+\d{1,4}\s+")

# How far into the page to look. A heading is at the top or it is not a heading,
# and searching further only finds prose that happens to name a chapter.
_HEAD_CHARS = 200
_HEAD_LINES = 8

# The most words a chapter title is allowed to run to, and the widest walk back
# from the anchor.
_TITLE_WORDS = 12


# "13.1", "8", "2)" — a figure or activity label, a page number, a section
# number. Real chapter titles do not carry them, and page furniture is mostly
# made of them, so one anywhere in a candidate disqualifies it. This is what
# stops the walk-back settling on "Activity 13.1 Activity 13.1 … Our
# Environment" and taking the whole run as a title.
_NUMERIC_TOKEN = re.compile(r"^\d+(?:\.\d+)*[.)]?$")


def _looks_like_title(text: str) -> bool:
    """Whether a captured span reads as a chapter title rather than as prose."""
    text = text.strip()
    if not 2 <= len(text) <= 80:
        return False
    words = text.split()
    if not 1 <= len(words) <= _TITLE_WORDS:
        return False
    if any(_NUMERIC_TOKEN.match(word) for word in words):
        return False
    letters = sum(character.isalpha() for character in text)
    if letters < len(text) * 0.6:
        return False
    return text[0].isupper()


def _title_from_page(text: str) -> str | None:
    """A chapter title read off the top of a part's first page, or ``None``.

    Returns ``None`` far more often than not, and that is the intended behaviour:
    the caller falls back to the filename, which is where it started.
    """
    if not text:
        return None

    head = " ".join(text.split())[:_HEAD_CHARS]
    anchor = _CHAPTER_ANCHOR.search(head)
    if anchor:
        before = _RUNNING_HEAD.sub("", head[: anchor.start()]).strip()
        words = before.split()
        # Longest first, so a real multi-word title is preferred over its own
        # tail, and page furniture in front of it is shed a word at a time.
        # `_looks_like_title` requiring a capital first word is what stops this
        # at the start of the title rather than in the middle of it.
        for count in range(min(len(words), _TITLE_WORDS), 0, -1):
            candidate = " ".join(words[-count:])
            if _looks_like_title(candidate):
                return candidate[:200]

    lines = [line.strip() for line in text.splitlines() if line.strip()][:_HEAD_LINES]
    for position, line in enumerate(lines):
        leading = _CHAPTER_LEADING.match(line)
        if not leading:
            continue
        # "CHAPTER 3: Metals" carries its title; a bare "CHAPTER 3" hands it to
        # the next line, which is where a two-line opener puts it.
        rest = leading.group("title").strip()
        following = lines[position + 1] if position + 1 < len(lines) else ""
        for option in (rest, following):
            if _looks_like_title(option):
                return option[:200]
    return None


def _chapters_from_zip_parts(
    pages: list[Page], data: bytes, last_page: int
) -> list[Chapter]:
    """One chapter per part, unless a part brought its own outline.

    The seams between the uploaded files are ground truth — whoever split the book
    split it *somewhere* deliberate, usually at chapters (D26). A part whose own
    PDF outline names at least two chapters contributes those instead: an
    NCERT-style zip of one-chapter files gets one chapter per file, while a zip of
    two half-books keeps the halves' real chapter lists. A chunk therefore never
    spans two of the uploaded files.

    Where a part has no outline, its title is read off its opening page
    (:func:`_title_from_page`) before falling back to the member's filename. The
    boundaries are identical either way — only the label changes.
    """
    by_page = {page.number: page.text for page in pages}
    tops: list[tuple[str, int]] = []
    for part in parse.zip_outline(data):
        if len(part.outline) >= 2:
            tops.extend(part.outline)
        else:
            recovered = _title_from_page(by_page.get(part.page_start, ""))
            tops.append((recovered or part.title, part.page_start))
    return _chapters_from_tops([(t, p) for t, p in tops if 1 <= p <= last_page], last_page)


def _chapters_from_tops(tops: list[tuple[str, int]], last_page: int) -> list[Chapter]:
    """Chapter spans from an ordered list of (title, first page)."""
    if len(tops) < 2:
        return []

    chapters = []
    for index, (title, start) in enumerate(tops):
        end = tops[index + 1][1] - 1 if index + 1 < len(tops) else last_page
        chapters.append(
            Chapter(index=index, title=title, page_start=start, page_end=max(start, end))
        )
    return chapters


_PARAGRAPH = re.compile(r"\n\s*\n")


def chunk_pages(pages: list[Page], chapters: list[Chapter]) -> list[Chunk]:
    """Split into overlapping chunks, one chapter at a time."""
    max_tokens = chunk_token_budget()
    # Overlap has to stay comfortably under the chunk itself, or `tail()` carries the
    # whole previous chunk forward and the split stops making progress.
    overlap_tokens = min(settings.chunk_overlap_tokens, max_tokens // 3)
    by_page = {page.number: page.text for page in pages}
    chunks: list[Chunk] = []

    def emit(chapter: Chapter, page: int, body: str) -> None:
        chunks.append(
            Chunk(
                index=len(chunks),
                chapter_index=chapter.index,
                page=page,
                text=body,
                token_count=estimate_tokens(body),
            )
        )

    def tail(buffer: list[tuple[int, str]]) -> tuple[list[tuple[int, str]], int]:
        """The trailing paragraphs to carry into the next chunk, so a passage split
        across a boundary is still retrievable from one side of it."""
        carried: list[tuple[int, str]] = []
        carried_tokens = 0
        for unit in reversed(buffer):
            unit_tokens = estimate_tokens(unit[1])
            if carried_tokens + unit_tokens > overlap_tokens:
                break
            carried.insert(0, unit)
            carried_tokens += unit_tokens
        return carried, carried_tokens

    for chapter in chapters:
        # Paragraphs carry their page with them, so a chunk knows where it started.
        units: list[tuple[int, str]] = []
        for number in range(chapter.page_start, chapter.page_end + 1):
            for paragraph in _PARAGRAPH.split(by_page.get(number, "")):
                cleaned = paragraph.strip()
                if _is_worth_keeping(cleaned):
                    units.append((number, cleaned))

        buffer: list[tuple[int, str]] = []
        tokens = 0

        for page_number, paragraph in units:
            paragraph_tokens = estimate_tokens(paragraph)

            # A single paragraph larger than the cap cannot be packed; split it.
            if paragraph_tokens > max_tokens:
                if buffer:
                    emit(chapter, buffer[0][0], "\n\n".join(t for _, t in buffer))
                for piece in _split_oversized(paragraph, max_tokens):
                    emit(chapter, page_number, piece)
                buffer, tokens = [], 0
                continue

            if buffer and tokens + paragraph_tokens > max_tokens:
                emit(chapter, buffer[0][0], "\n\n".join(t for _, t in buffer))
                buffer, tokens = tail(buffer)

            buffer.append((page_number, paragraph))
            tokens += paragraph_tokens

        # Chapter boundary: flush without carrying overlap into the next chapter.
        if buffer:
            emit(chapter, buffer[0][0], "\n\n".join(t for _, t in buffer))

    return chunks


def _is_worth_keeping(text: str) -> bool:
    """Drop page furniture: bare numerals, running heads, near-empty lines."""
    if len(text) < 20:
        return False
    letters = sum(character.isalpha() for character in text)
    return letters >= len(text) * 0.5


def _split_oversized(paragraph: str, max_tokens: int) -> list[str]:
    limit = max_tokens * CHARS_PER_TOKEN
    return [paragraph[start : start + limit] for start in range(0, len(paragraph), limit)]
