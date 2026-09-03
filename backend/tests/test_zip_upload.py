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


# --- chapter titles ----------------------------------------------------------
#
# A zip of per-chapter PDFs with no outlines took its titles from the member
# FILENAMES, and NCERT-style downloads are named jesc101.pdf. Fifteen chapters
# called "jesc101 … jesc113" is a table of contents nobody can read — and it is
# what the author picks from when scoping an assessment to a chapter.


def test_a_part_is_titled_from_its_opening_page_not_its_filename() -> None:
    """The heading NCERT-style school texts set: title, number, then CHAPTER."""
    data = _zip(
        {
            "jesc101.pdf": _pdf(
                ["Chemical Reactions and Equations 1 CHAPTER Consider the following"]
            ),
            "jesc102.pdf": _pdf(
                ["Acids, Bases and Salts 2 CHAPTER You have learnt in your previous"]
            ),
        }
    )
    pages = parse.parse(data, SourceFormat.ZIP)
    chapters = chunker.detect_chapters(pages, data, SourceFormat.ZIP)

    assert [chapter.title for chapter in chapters] == [
        "Chemical Reactions and Equations",
        "Acids, Bases and Salts",
    ]


def test_a_running_head_is_stripped_from_a_recovered_title() -> None:
    """Textbooks print "Science 58" in front of the opener on the same line."""
    data = _zip(
        {
            "a.pdf": _pdf(["Science 58 Carbon and its Compounds 4 CHAPTER In the last"]),
            "b.pdf": _pdf(["Life Processes 5 CHAPTER How do we tell the difference"]),
        }
    )
    pages = parse.parse(data, SourceFormat.ZIP)
    chapters = chunker.detect_chapters(pages, data, SourceFormat.ZIP)

    assert [chapter.title for chapter in chapters] == [
        "Carbon and its Compounds",
        "Life Processes",
    ]


def test_an_unrecognised_opening_page_keeps_the_filename() -> None:
    """The fallback is where this started, so failing to recognise a heading costs
    nothing. Front matter and answer keys are exactly the parts that have none."""
    data = _zip(
        {
            "jesc1an.pdf": _pdf(["Answers Chapter 1 1. (i) 2. (d) 3. (a) Chapter 2"]),
            "jesc1ps.pdf": _pdf(["SCIENCE TEXTBOOK FOR CLASS X Reprint 2026-27"]),
        }
    )
    pages = parse.parse(data, SourceFormat.ZIP)
    chapters = chunker.detect_chapters(pages, data, SourceFormat.ZIP)

    assert [chapter.title for chapter in chapters] == ["jesc1an", "jesc1ps"]


def test_recovering_a_title_never_moves_a_boundary() -> None:
    """Only the label changes. The seams are still the uploaded files, which is
    what keeps a chunk from ever spanning two of them."""
    members = {
        "one.pdf": _pdf(["Chemical Reactions and Equations 1 CHAPTER text", "more one"]),
        "two.pdf": _pdf(["Acids, Bases and Salts 2 CHAPTER text"]),
    }
    data = _zip(members)
    pages = parse.parse(data, SourceFormat.ZIP)
    chapters = chunker.detect_chapters(pages, data, SourceFormat.ZIP)

    assert [(c.page_start, c.page_end) for c in chapters] == [(1, 2), (3, 3)]


def test_a_part_with_its_own_outline_still_wins() -> None:
    """A part naming two or more chapters keeps them; page-head recovery is only
    ever the fallback for a part that named none."""
    data = _zip(
        {
            "half.pdf": _pdf(
                ["Something Else 9 CHAPTER opening", "second", "third"],
                toc=[[1, "Real Chapter One", 1], [1, "Real Chapter Two", 3]],
            )
        }
    )
    pages = parse.parse(data, SourceFormat.ZIP)
    chapters = chunker.detect_chapters(pages, data, SourceFormat.ZIP)

    assert [chapter.title for chapter in chapters] == [
        "Real Chapter One",
        "Real Chapter Two",
    ]


def test_page_furniture_in_front_of_the_heading_is_shed() -> None:
    """PDF text order does not match reading order.

    The real page for one chapter extracts as "Science 208 Activity 13.1 …
    Our Environment 13 CHAPTER" — a running head and five figure labels ahead of
    the title. The walk-back from the anchor has to stop at the title rather than
    swallowing the furniture, which is what the numeric-token rule is for.
    """
    data = _zip(
        {
            "a.pdf": _pdf(
                [
                    "Science 208 Activity 13.1 Activity 13.1 Activity 13.1 "
                    "Our Environment 13 CHAPTER We have heard the word environment"
                ]
            ),
            "b.pdf": _pdf(["Electricity 12 CHAPTER Electricity has an important place"]),
        }
    )
    pages = parse.parse(data, SourceFormat.ZIP)
    chapters = chunker.detect_chapters(pages, data, SourceFormat.ZIP)

    assert [chapter.title for chapter in chapters] == ["Our Environment", "Electricity"]
