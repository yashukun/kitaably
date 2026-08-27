"""A ZIP upload is one book in parts (DECISIONS.md D26).

The properties worth pinning: parts combine in reading order with page numbers
that keep counting across the seams, every part seam becomes a chapter boundary
(so no chunk ever spans two uploaded files), and an archive that cannot be
ingested fails with a sentence its owner can act on — never a silent skip that
ships a book missing chapter 7.
"""

import io
import zipfile

import pytest

from app.core.config import settings
from app.db.models.enums import SourceFormat
from app.rag import chunk as chunker
from app.rag import parse


def _pdf(page_texts: list[str], toc: list[list] | None = None) -> bytes:
    import fitz

    doc = fitz.open()
    for text in page_texts:
        page = doc.new_page()
        page.insert_text((72, 72), text)
    if toc:
        doc.set_toc(toc)
    return doc.tobytes()


def _zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


# --- sniffing ----------------------------------------------------------------


def test_a_zip_of_documents_sniffs_as_zip() -> None:
    data = _zip({"ch1.pdf": _pdf(["chapter one"])})
    assert parse.sniff_format(io.BytesIO(data)) is SourceFormat.ZIP


def test_docx_is_still_told_apart_from_a_generic_zip() -> None:
    """DOCX is a ZIP container too. The entry names must keep deciding first, or
    every Word document becomes a one-part 'book in parts'."""
    data = _zip({"[Content_Types].xml": b"<xml/>", "word/document.xml": b"<xml/>"})
    assert parse.sniff_format(io.BytesIO(data)) is SourceFormat.DOCX


def test_a_zip_of_junk_is_not_a_book() -> None:
    # Leading bytes with NULs in them: image-like, nothing a parser reads.
    data = _zip({"photo.png": b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 64})
    assert parse.sniff_format(io.BytesIO(data)) is None


# --- combining ---------------------------------------------------------------


def test_parts_combine_in_natural_order_with_continuous_pages() -> None:
    """`ch2` before `ch10`, and page numbers that keep counting across the seams —
    a citation must point at exactly one place in the combined book."""
    data = _zip(
        {
            "ch10.pdf": _pdf(["chapter ten text"]),
            "ch2.pdf": _pdf(["chapter two opening", "chapter two closing"]),
            "ch1.pdf": _pdf(["chapter one text"]),
        }
    )
    pages = parse.parse(data, SourceFormat.ZIP)

    assert [page.number for page in pages] == [1, 2, 3, 4]
    assert "chapter one text" in pages[0].text
    assert "chapter two closing" in pages[2].text
    assert "chapter ten text" in pages[3].text


def test_junk_members_are_ignored_when_documents_exist() -> None:
    data = _zip(
        {
            "__MACOSX/._ch1.pdf": b"resource fork",
            ".DS_Store": b"\x00\x01junk",
            "book/ch1.pdf": _pdf(["the actual content"]),
        }
    )
    pages = parse.parse(data, SourceFormat.ZIP)
    assert len(pages) == 1
    assert "the actual content" in pages[0].text


def test_a_nested_zip_is_not_unpacked() -> None:
    """One level of 'in parts' is the product. An archive inside the archive is
    skipped, not recursed into."""
    inner = _zip({"hidden.pdf": _pdf(["hidden text"])})
    data = _zip({"inner.zip": inner, "ch1.pdf": _pdf(["visible text"])})
    pages = parse.parse(data, SourceFormat.ZIP)
    assert len(pages) == 1
    assert "visible text" in pages[0].text


# --- chapters ----------------------------------------------------------------


def test_each_part_becomes_a_chapter() -> None:
    data = _zip(
        {
            "ch1.pdf": _pdf(["one"]),
            "ch2.pdf": _pdf(["two, first page", "two, second page"]),
        }
    )
    pages = parse.parse(data, SourceFormat.ZIP)
    chapters = chunker.detect_chapters(pages, data, SourceFormat.ZIP)

    assert [(c.title, c.page_start, c.page_end) for c in chapters] == [
        ("ch1", 1, 1),
        ("ch2", 2, 3),
    ]


def test_a_part_with_its_own_outline_contributes_its_chapters() -> None:
    """A zip of two half-books keeps the halves' real chapter lists; only parts
    without structure of their own fall back to their filename."""
    front = _pdf(
        ["front matter", "alpha body", "beta body"],
        toc=[[1, "Alpha", 2], [1, "Beta", 3]],
    )
    data = _zip({"a_front.pdf": front, "b_tail.pdf": _pdf(["tail text"])})
    pages = parse.parse(data, SourceFormat.ZIP)
    chapters = chunker.detect_chapters(pages, data, SourceFormat.ZIP)

    assert [(c.title, c.page_start, c.page_end) for c in chapters] == [
        ("Alpha", 2, 2),
        ("Beta", 3, 3),
        ("b tail", 4, 4),
    ]


def test_chunks_never_cross_a_part_seam() -> None:
    """The chunker's chapter-boundary rule, exercised through zip parts: every
    chunk's page sits inside the span of the chapter it claims."""
    body = "This paragraph carries enough ordinary prose to be worth keeping. " * 6
    data = _zip({"ch1.pdf": _pdf([body, body]), "ch2.pdf": _pdf([body])})
    pages = parse.parse(data, SourceFormat.ZIP)
    chapters = chunker.detect_chapters(pages, data, SourceFormat.ZIP)
    chunks = chunker.chunk_pages(pages, chapters)

    assert chunks, "the fixture text should produce at least one chunk"
    spans = {chapter.index: (chapter.page_start, chapter.page_end) for chapter in chapters}
    for piece in chunks:
        start, end = spans[piece.chapter_index]
        assert start <= piece.page <= end


# --- refusals, each with a reason the owner can act on -----------------------


def test_a_zip_with_no_documents_fails_with_a_reason() -> None:
    data = _zip({"blob.bin": b"\x00\x01\x02\x03" * 64})
    with pytest.raises(parse.UnparseableDocument, match="No readable documents"):
        parse.parse(data, SourceFormat.ZIP)


def test_a_password_protected_archive_is_refused() -> None:
    data = _zip({"ch1.pdf": _pdf(["one"])})
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        # The stdlib cannot write an encrypted zip, so flip the flag on the live
        # ZipInfo the walk will read. What matters is the refusal, not the crypto.
        archive.infolist()[0].flag_bits |= 0x1
        with pytest.raises(parse.UnparseableDocument, match="password"):
            parse._document_members(archive)


def test_the_decompressed_size_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """The size a ZIP header declares is attacker-controlled; the cap is enforced
    on the bytes actually produced."""
    monkeypatch.setattr(settings, "zip_max_uncompressed_mb", 0)
    data = _zip({"ch1.txt": b"perfectly ordinary text " * 64})
    with pytest.raises(parse.UnparseableDocument, match="unpacks past"):
        parse.parse(data, SourceFormat.ZIP)


def test_the_member_count_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "zip_max_members", 2)
    data = _zip({f"ch{i}.txt": b"some readable text here" for i in range(3)})
    with pytest.raises(parse.UnparseableDocument, match="limit is 2"):
        parse.parse(data, SourceFormat.ZIP)


def test_a_damaged_part_is_named_not_skipped() -> None:
    """A member that should parse and does not fails loudly with its filename — a
    book silently missing chapter 7 is worse than one that failed."""
    data = _zip({"ch1.pdf": b"%PDF-1.4 but nothing a reader could open"})
    with pytest.raises(parse.UnparseableDocument, match="ch1.pdf"):
        parse.parse(data, SourceFormat.ZIP)
