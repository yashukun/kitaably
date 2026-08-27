"""Conversation export — the renderers, which are pure over ORM rows.

The service function does one authorized read and hands the rows to these; what
is worth pinning is the contract of the files themselves: the JSON says exactly
what the transcript API says (versioned, so a future shape change is detectable),
and the Markdown is a document a person can reread with the ``[n]`` marks still
resolving on paper.
"""

import json
from datetime import UTC, datetime
from uuid import uuid4

from app.db.models.chat import ChatMessage, ChatSession
from app.db.models.enums import MessageIntent, MessageRole
from app.services.chat import (
    _export_filename,
    render_export_json,
    render_export_markdown,
)


def conversation() -> tuple[ChatSession, list[ChatMessage]]:
    chat = ChatSession(user_id=uuid4(), title="Enzymes and activation energy")
    chat.id = uuid4()
    chat.created_at = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)
    chat.last_message_at = datetime(2026, 8, 25, 9, 5, tzinfo=UTC)

    question = ChatMessage(
        session_id=chat.id,
        role=MessageRole.USER,
        content="how do enzymes lower activation energy?\n# not a heading",
        citations=[],
        intent=MessageIntent.QUESTION,
    )
    question.created_at = datetime(2026, 8, 25, 9, 4, tzinfo=UTC)

    answer = ChatMessage(
        session_id=chat.id,
        role=MessageRole.ASSISTANT,
        content="## How enzymes lower activation energy\n\nThey stabilise the "
        "transition state [1] — думайте об этом as a shortcut [2].",
        citations=[
            {
                "chunk_id": str(uuid4()),
                "book_id": str(uuid4()),
                "book_title": "Biochemistry",
                "genre": "Biochemistry",
                "page": 12,
                "scope": "canon",
            },
            {
                "chunk_id": str(uuid4()),
                "book_id": str(uuid4()),
                "book_title": "My Notes",
                "genre": None,
                "page": None,
                "scope": "personal",
            },
        ],
    )
    answer.created_at = datetime(2026, 8, 25, 9, 5, tzinfo=UTC)

    return chat, [question, answer]


# --- the data contract ------------------------------------------------------


def test_json_export_is_versioned_and_parseable() -> None:
    chat, messages = conversation()
    payload = json.loads(render_export_json(chat, messages))

    assert payload["format"] == "kitaably.chat.v1"
    assert payload["session"]["title"] == "Enzymes and activation energy"
    assert [m["role"] for m in payload["messages"]] == ["user", "assistant"]


def test_json_export_says_no_more_than_the_transcript_api() -> None:
    """The export must never grow a field the API does not return — it is the
    same contract, downloaded. New keys here mean new keys in MessageRead first."""
    chat, messages = conversation()
    payload = json.loads(render_export_json(chat, messages))

    assert set(payload["messages"][0].keys()) == {
        "role", "content", "intent", "created_at", "citations",
    }


def test_json_export_keeps_citations_and_unicode() -> None:
    """`ensure_ascii=False`: a transcript in any script exports as itself, not as
    escape sequences nobody can read."""
    chat, messages = conversation()
    rendered = render_export_json(chat, messages)

    assert "думайте" in rendered
    payload = json.loads(rendered)
    assert payload["messages"][1]["citations"][0]["book_title"] == "Biochemistry"
    # The data file keeps column vocabulary; only the human-facing Markdown
    # translates to shared/private.
    assert payload["messages"][1]["citations"][1]["scope"] == "personal"


# --- the human document -----------------------------------------------------


def test_markdown_export_reads_as_a_transcript() -> None:
    chat, messages = conversation()
    rendered = render_export_markdown(chat, messages)

    assert rendered.startswith("# Enzymes and activation energy")
    assert "**You**" in rendered
    assert "**Tutor**" in rendered
    # The tutor's own markdown survives verbatim.
    assert "## How enzymes lower activation energy" in rendered


def test_markdown_quotes_the_reader_so_their_text_is_never_structure() -> None:
    """A question starting with `#` must read as the reader's words, not as a
    document heading."""
    chat, messages = conversation()
    rendered = render_export_markdown(chat, messages)

    assert "> # not a heading" in rendered
    assert "\n# not a heading" not in rendered


def test_markdown_sources_use_ui_vocabulary() -> None:
    """A person reads shared/private — the words the app itself shows — never the
    column values."""
    chat, messages = conversation()
    rendered = render_export_markdown(chat, messages)

    assert "[1] Biochemistry, p. 12 (shared)" in rendered
    assert "[2] My Notes (private)" in rendered
    assert "canon" not in rendered


def test_markdown_export_of_an_empty_conversation_is_still_a_document() -> None:
    chat, _ = conversation()
    rendered = render_export_markdown(chat, [])

    assert "No messages yet" in rendered


# --- the filename -----------------------------------------------------------


def test_filename_is_recognisable_and_unique() -> None:
    chat, _ = conversation()
    name = _export_filename(chat, "json")

    assert name.startswith("kitaably-enzymes-and-activation-energy-")
    assert name.endswith(".json")
    assert chat.id.hex[:8] in name


def test_filename_survives_a_missing_or_hostile_title() -> None:
    """The filename travels in a Content-Disposition header, so it is ASCII and
    quote-free by construction whatever the title held."""
    chat, _ = conversation()
    chat.title = None
    assert _export_filename(chat, "md").startswith("kitaably-conversation-")

    chat.title = 'нет ascii "quotes"/slashes\\here'
    name = _export_filename(chat, "md")
    assert '"' not in name and "/" not in name and "\\" not in name
    assert name.isascii()
