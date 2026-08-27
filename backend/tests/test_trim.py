"""Excerpting a retrieved passage down to the part that bears on the question.

Pure functions over text, so they pin exactly and need neither a model nor a schema.

The invariant that matters is the same one `rank.py` has: **this narrows, it never
invents**. Every byte it returns came from the passage it was given, in the order the
passage had it. It is allowed to produce a worse answer by dropping a sentence the
reader needed; it is never allowed to produce a sentence the book does not contain,
because the tutor's grounding rules bind to exactly this text.
"""

from app.rag.trim import excerpt, split_sentences

PASSAGE = (
    "The cell membrane separates the interior of the cell from the outside. "
    "It is made of a phospholipid bilayer with embedded proteins. "
    "Enzymes lower the activation energy of a reaction without being consumed. "
    "They do this by stabilising the transition state. "
    "Mitochondria generate ATP through oxidative phosphorylation. "
    "The Golgi apparatus packages proteins for secretion."
)


def test_short_passage_is_returned_untouched():
    """A chunk already inside budget must not be mangled — tables and lists included."""
    text = "Short passage about enzymes."
    assert excerpt(text, "enzymes", max_tokens=500) == text


def test_zero_budget_disables_excerpting():
    assert excerpt(PASSAGE, "enzymes", max_tokens=0) == PASSAGE


def test_excerpt_keeps_the_sentences_that_answer_the_question():
    out = excerpt(PASSAGE, "how do enzymes lower activation energy", max_tokens=30)
    assert "activation energy" in out
    assert len(out) < len(PASSAGE)


def test_excerpt_is_contiguous_and_in_order():
    """Never a bag of top-scoring sentences stitched together.

    Two sentences that were paragraphs apart read as a contradiction side by side, and
    the tutor is required to answer from what the sources literally say.
    """
    out = excerpt(PASSAGE, "enzymes transition state", max_tokens=40)
    body = out.strip("… ").strip()
    assert body in PASSAGE


def test_every_returned_sentence_came_from_the_source():
    out = excerpt(PASSAGE, "mitochondria ATP", max_tokens=25)
    for sentence in split_sentences(out.replace("…", " ")):
        assert sentence.strip() in PASSAGE


def test_trimmed_text_is_marked_as_an_extract():
    """The model is told plainly it is reading an extract.

    It is being instructed in the same prompt never to invent what is not there, so
    handing it a silent truncation would be the one misleading thing in the message.
    """
    out = excerpt(PASSAGE, "golgi apparatus secretion", max_tokens=20)
    assert "…" in out


def test_unrelated_question_still_returns_material():
    """No shared vocabulary is not an error — the retriever still chose this passage."""
    out = excerpt(PASSAGE, "quantum chromodynamics", max_tokens=25)
    assert out.strip()
    assert out.strip("… ").strip() in PASSAGE


def test_single_unsplittable_sentence_is_cut_on_a_word_boundary():
    text = "word " * 400
    out = excerpt(text, "word", max_tokens=20)
    assert len(out) < len(text)
    assert "  " not in out.strip("… ")


def test_result_respects_the_budget():
    for query in ("enzymes", "mitochondria ATP", "membrane proteins", ""):
        out = excerpt(PASSAGE, query, max_tokens=25)
        # chars//4 is the same estimate the caller budgets in; allow the ellipsis.
        assert len(out) // 4 <= 25 + 2, (query, out)


def test_empty_text_does_not_raise():
    assert excerpt("", "anything", max_tokens=10) == ""
