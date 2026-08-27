"""Intent classification — the rules half.

Every chat message goes through :func:`classify_offline` first, so this is the code
path with the widest blast radius in the whole chat pipeline. It is pure and
synchronous precisely so it can be pinned by a table like this one, with no model
running and no database.

What is being defended, in order of how much it costs to get wrong:

1. **A question must never be classified as conversation.** That is the expensive
   failure — somebody's actual work goes unanswered and they are handed a cheerful
   greeting instead. Every ambiguous case is expected to come back QUESTION.
2. "hi" must not become a grounded refusal. That is the bug this module exists for.
3. A follow-up must be recognised only when there is a transcript to resolve it
   against, or condensation is handed nothing to work with.
"""

import pytest

from app.db.models.enums import MessageIntent
from app.rag.intent import classify_offline, looks_like_keysmash, normalise

Q = MessageIntent.QUESTION


# --- the thing this module was written for ----------------------------------


@pytest.mark.parametrize(
    "text",
    ["hi", "Hi!", "hello", "HELLO", "hey there", "good morning", "  hi  ", "hi!!!"],
    ids=repr,
)
def test_a_greeting_is_not_a_failed_question(text: str) -> None:
    """Before this existed, every one of these embedded to nothing, cleared no
    distance threshold, and came back "Your books don't cover that"."""
    assert classify_offline(text, has_history=False) is MessageIntent.GREETING


@pytest.mark.parametrize(
    "text", ["thanks", "thank you", "ok", "got it", "bye", "how are you"], ids=repr
)
def test_chitchat_does_not_reach_the_books(text: str) -> None:
    assert classify_offline(text, has_history=False) is MessageIntent.CHITCHAT


@pytest.mark.parametrize("text", ["qwrtplkjhg", "zzzz", "bcdfgh", "sdfghjkl"], ids=repr)
def test_a_keysmash_is_unclear(text: str) -> None:
    assert classify_offline(text, has_history=False) is MessageIntent.UNCLEAR


@pytest.mark.parametrize("text", ["asdkjhasdkjh", "qweqweqwe"], ids=repr)
def test_a_word_shaped_keysmash_is_escalated_not_refused(text: str) -> None:
    """The detector is deliberately weak, and this pins the consequence rather than
    pretending otherwise.

    These have vowels in plausible places, so they read as words to a cheap test.
    ``None`` means the rules declined to decide and the model is asked — and if the
    model is unreachable, :func:`classify` defaults to QUESTION, so the worst case is
    a search that finds nothing and an honest refusal. That is a perfectly good answer
    to a keysmash.

    The trade runs in this direction on purpose: a rule tight enough to catch these
    would also catch "strengths", and telling somebody their real question was
    gibberish is the more expensive mistake."""
    assert classify_offline(text, has_history=False) is None


@pytest.mark.parametrize("text", ["...", "???", "12345", "!!", "🙂"], ids=repr)
def test_nothing_to_retrieve_on_is_unclear(text: str) -> None:
    assert classify_offline(text, has_history=False) is MessageIntent.UNCLEAR


# --- the expensive failure: a real question treated as conversation ----------


@pytest.mark.parametrize(
    "text",
    [
        "what is enthalpy?",
        "What is enthalpy",
        "define oxidation",
        "explain the krebs cycle",
        "how do plants make food from light?",
        "why does entropy increase",
        "compare mitosis and meiosis",
        "summarise chapter three",
        "the second law of thermodynamics",
        "photosynthesis light reactions",
        "is the narrator reliable",
        "walk me through the derivation",
        # A greeting stuck to the front of a real question is still a question.
        "hi, what is an aldehyde?",
        "hello can you explain covalent bonds",
    ],
    ids=repr,
)
def test_questions_are_questions(text: str) -> None:
    assert classify_offline(text, has_history=False) is Q


@pytest.mark.parametrize(
    "text",
    ["what is enthalpy?", "explain the krebs cycle", "compare mitosis and meiosis"],
    ids=repr,
)
def test_a_transcript_does_not_turn_a_question_into_a_follow_up(text: str) -> None:
    """A question that names its own subject retrieves perfectly well on its own.
    Condensing it would only give the model a chance to narrow it wrongly."""
    assert classify_offline(text, has_history=True) is Q


# --- follow-ups, and only where there is something to follow ----------------


@pytest.mark.parametrize(
    "text",
    [
        "explain that again",
        "what about the second one",
        "tell me more",
        "more",
        "why?",
        "the second point",
        "in simpler terms",
        "give me an example",
        "go on",
        "that doesn't make sense",
    ],
    ids=repr,
)
def test_follow_ups_are_recognised_when_there_is_a_transcript(text: str) -> None:
    assert classify_offline(text, has_history=True) is MessageIntent.FOLLOW_UP


@pytest.mark.parametrize(
    "text", ["explain that again", "what about the second one", "tell me more"], ids=repr
)
def test_a_follow_up_without_a_transcript_is_never_a_follow_up(text: str) -> None:
    """There is nothing to resolve the reference against, so condensation would be
    handed an empty transcript and return the pronoun unchanged. Better to retrieve
    on it and let the distance threshold refuse."""
    assert classify_offline(text, has_history=False) is not MessageIntent.FOLLOW_UP


@pytest.mark.parametrize(
    "text",
    [
        "what about the role of ATP in glycolysis",
        "tell me more about the structure of benzene rings",
    ],
    ids=repr,
)
def test_a_long_follow_up_that_names_its_subject_is_a_question(text: str) -> None:
    """It carries its own subject, so it embeds fine as-is. Length is doing real
    work here, not just the opener."""
    assert classify_offline(text, has_history=True) is Q


# --- about the tool rather than about the material --------------------------


@pytest.mark.parametrize(
    "text",
    [
        "what books do you have",
        "which books can i ask about",
        "what can you do",
        "who are you",
        "how do you work",
        "help",
    ],
    ids=repr,
)
def test_meta_questions_are_answered_from_the_library(text: str) -> None:
    assert classify_offline(text, has_history=False) is MessageIntent.META


# --- the safety property ----------------------------------------------------


def test_only_questions_and_follow_ups_reach_the_books() -> None:
    """`needs_retrieval` is what decides whether an embedding is taken at all. If a
    new intent is added without thinking about this, the greeting path silently
    starts costing a vector search again."""
    reaching = {intent for intent in MessageIntent if intent.needs_retrieval}
    assert reaching == {MessageIntent.QUESTION, MessageIntent.FOLLOW_UP}


@pytest.mark.parametrize(
    "text",
    ["enthalpy", "mitochondria", "photosynthesis", "benzene", "strengths", "rhythms"],
    ids=repr,
)
def test_a_real_single_word_is_never_called_a_keysmash(text: str) -> None:
    """The keysmash heuristic looks at one token, so the cost of it being wrong lands
    on exactly the readers who type a bare topic — which is a very common way to ask."""
    assert not looks_like_keysmash(text)


def test_normalise_keeps_internal_punctuation() -> None:
    """Trailing punctuation goes so "hi!!!" is one lexicon entry. Internal punctuation
    stays, or "what's up" and "H2O" both get quietly mangled."""
    assert normalise("  Hi!!!  ") == "hi"
    assert normalise("What's up?") == "what's up"
    assert normalise("Is H2O polar?") == "is h2o polar"


@pytest.mark.parametrize(
    "text",
    ["summarize this book", "explain this book", "summarise these books", "describe my library"],
    ids=repr,
)
def test_a_demonstrative_aimed_at_the_material_is_not_a_follow_up(text: str) -> None:
    """"Summarize this book" is three words and opens exactly like a follow-up —
    but "this book" points at the library, not at the previous turn, and condensing
    it would glue an unrelated question onto a whole-book request (D22)."""
    assert classify_offline(text, has_history=True) is Q


@pytest.mark.parametrize(
    "text",
    [
        "i keep mixing up mitosis and meiosis every single time",
        "my notes on thermodynamics are a mess and nothing is sticking",
    ],
    ids=repr,
)
def test_a_long_declarative_escalates_to_the_model(text: str) -> None:
    """No interrogative, no imperative, no question mark, more than eight words:
    odd enough that the model reads it better than a length heuristic (D23).
    Escalation is safe both ways — with the fallback off, `classify` resolves
    None to QUESTION, exactly the label this used to be force-given."""
    assert classify_offline(text, has_history=False) is None


def test_a_long_question_with_its_opener_never_escalates() -> None:
    """Length alone is not oddness. An interrogative or imperative opener settles
    it offline no matter how long it runs."""
    text = "how does the calvin cycle differ between c3 and c4 plants in hot climates"
    assert classify_offline(text, has_history=False) is Q


@pytest.mark.parametrize(
    "text",
    ["are you sure?", "Are you certain?", "is that right", "really?", "you sure?"],
    ids=repr,
)
def test_a_challenge_to_the_previous_answer_is_a_follow_up(text: str) -> None:
    """"Are you sure?" is about the turn before it, not about the material.
    Embedded verbatim it retrieves on nothing, and the tutor answers a confidence
    question with "your books don't cover this" — nonsense to the person who just
    read the answer being questioned. As a follow-up, the challenged turn is
    condensed back in and re-run: confirmed, corrected, or re-refused."""
    assert classify_offline(text, has_history=True) is MessageIntent.FOLLOW_UP


def test_a_challenge_with_no_transcript_is_not_a_follow_up() -> None:
    """Nothing was said, so there is nothing to be sure about — it degrades to a
    question and an honest refusal, the safe direction as always."""
    assert classify_offline("are you sure?", has_history=False) is Q
