"""The chunk-size ceiling, and the book title derived from a filename.

The first half of this file guards a bug with no symptom. ``bge-small-en-v1.5``
accepts 512 tokens and TRUNCATES silently past them — a longer passage embeds to a
vector byte-identical to the one its own first 512 tokens produce — so a chunk over
the limit is indexed on its head and its tail is unreachable by search. There is no
error, no warning and nothing in a wrong answer to point at the cause: the reader
gets "your books don't cover this" about a page sitting in the database.

``chunk_tokens`` was 800 against a 512-token model, which put 79% of the library over
the line. The fix is a ceiling that configuration cannot raise past, which is what
:func:`chunk_token_budget` is, and what this asserts (DECISIONS.md D21).
"""

import pytest

from app.core.config import settings
from app.rag.chunk import chunk_token_budget, estimate_tokens
from app.services.books import title_from_filename


def test_chunk_budget_stays_under_the_embedder_limit():
    assert chunk_token_budget() < settings.embedding_max_tokens


def test_chunk_budget_leaves_margin_for_a_bad_estimate():
    """The estimate is chars//4, which under-counts dense prose.

    A chunk measured at exactly the model's limit can arrive over it, so the budget
    keeps real headroom rather than sitting on the boundary.
    """
    assert chunk_token_budget() <= int(settings.embedding_max_tokens * 0.85)


def test_configuration_cannot_raise_the_budget_past_the_model(monkeypatch):
    """The ceiling is the model's, not the setting's.

    This is the regression guard: someone raises CHUNK_TOKENS to get more context per
    passage, every number in the config still looks reasonable, and retrieval quietly
    goes back to indexing the first half of everything.
    """
    monkeypatch.setattr(settings, "chunk_tokens", 4000)
    assert chunk_token_budget() < settings.embedding_max_tokens


def test_budget_honours_a_smaller_configured_chunk(monkeypatch):
    monkeypatch.setattr(settings, "chunk_tokens", 120)
    assert chunk_token_budget() == 120


def test_estimate_tokens_never_returns_zero():
    # A zero would make an empty unit free and let the packer loop forever.
    assert estimate_tokens("") == 1
    assert estimate_tokens("a") == 1


# ----------------------------------------------------------------- title fallback


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("organic_chem-ch4.pdf", "organic chem ch4"),
        ("A Brief History of Time.pdf", "A Brief History of Time"),
        ("DNA__structure.docx", "DNA structure"),
        ("  spaced   out .txt", "spaced out"),
        # Case is left alone deliberately: title-casing would turn DNA into Dna.
        ("dna.pdf", "dna"),
        # The name arrives from the client, so a path is taken apart rather than trusted.
        ("../../etc/passwd", "passwd"),
        ("/absolute/path/notes.md", "notes"),
        ("windows\\folder\\book.pdf", "book"),
        # A bare extension is a dotfile as far as PurePosixPath is concerned, so the
        # stem is ".pdf" and the leading dot has to go or it becomes the title verbatim.
        (".pdf", "pdf"),
        (".gitignore", "gitignore"),
        # Nothing usable at all: a book must still have a title, because a citation
        # names it, and "" would render as a blank slip the reader cannot identify.
        (None, "Untitled book"),
        ("", "Untitled book"),
        ("...", "Untitled book"),
    ],
)
def test_title_from_filename(filename, expected):
    assert title_from_filename(filename) == expected


def test_title_from_filename_is_bounded():
    """`books.title` is rendered everywhere a citation appears, so it cannot be a novel."""
    assert len(title_from_filename("x" * 5000 + ".pdf")) <= 200
