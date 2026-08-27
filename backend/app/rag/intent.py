"""What was the reader actually doing? Phase 4 revisited.

Every message used to go straight to vector search. That is correct code with a
terrible product attached: "hi" embeds to nothing, clears no distance threshold, and
comes back as *"Your books don't cover that."* The refusal machinery worked perfectly
and the tutor felt broken, because **a greeting is not a failed question**.

So a turn is classified before it is retrieved on.

Two-stage, and the order matters. Rules run first and settle the overwhelming
majority -- greetings, thanks, keysmashes and plain questions are not hard, and a
local 8B model costs a second and a half to agree with a lexicon lookup. Only genuine
ambiguity reaches the model.

**The fallback never fails closed.** If the classifier is unreachable, unparseable or
slow, the answer is :data:`MessageIntent.QUESTION`. That is the safe default because
a question that was really a greeting merely gets a grounded refusal -- the old
behaviour -- whereas a question misfiled as a greeting silently stops answering
somebody's actual work. Degrade toward doing the retrieval, always.
"""

import logging
import re

from app.core.config import settings
from app.core.errors import DomainError
from app.core.metrics import chat_intents_total
from app.db.models.enums import MessageIntent

logger = logging.getLogger(__name__)

# Whole-message matches only. "hi" is a greeting; "hi, what is an aldehyde?" is a
# question with a greeting stuck to the front, and matching a prefix would throw the
# question away.
_GREETINGS = frozenset(
    {
        "hi", "hii", "hiii", "hey", "heya", "hello", "helo", "hallo", "yo",
        "good morning", "good afternoon", "good evening", "good day",
        "morning", "evening", "greetings", "hi there", "hey there",
        "hello there", "salaam", "salam", "assalamualaikum", "namaste",
        "hi kitaably", "hey kitaably", "sup", "whats up", "what's up",
    }
)

_CHITCHAT = frozenset(
    {
        "thanks", "thank you", "thank you so much", "thanks a lot", "ty", "thx",
        "cheers", "nice", "cool", "great", "awesome", "perfect", "lovely",
        "ok", "okay", "k", "got it", "understood", "makes sense", "i see",
        "bye", "goodbye", "see you", "see ya", "good night", "gn",
        "how are you", "how are you doing", "hows it going", "how's it going",
        "you are great", "you're great", "good job", "well done", "no", "yes",
    }
)

# About the tool rather than about the material. These are answerable from what the
# server already knows -- the reader's own library -- so they take no LLM content
# call and cite nothing, because there is no claim about a book being made.
_META = (
    re.compile(r"^(what|which) (books?|material|documents?) (do|have|are)"),
    re.compile(r"^(what|which) (books?|material) (can|could) (you|i)"),
    re.compile(r"^(what|who) (are|r) (you|u)\b"),
    re.compile(r"^what (can|do) you (do|know|help)"),
    re.compile(r"^how (do|does) (you|this|kitaably) work"),
    re.compile(r"^(help|what are your (capabilities|features))$"),
    re.compile(r"\bmy (library|books|uploads)\b.*\?$"),
)

# A demonstrative pointing at the MATERIAL is not anaphora on the transcript.
# "Summarize this book" is three words and starts with "summarize … this", which is
# exactly the follow-up shape — but "this book" resolves against the library, not
# against the previous turn, and condensing it would glue an unrelated question on.
# The query-shape router (app/rag/shape.py) handles where it actually points.
_MATERIAL_REFERENCE = re.compile(
    r"\b(?:this|that|these|those|the|my) (?:books?|library|material|uploads?)\b"
)

# Openers that carry no subject of their own. "explain that" is only answerable
# against the previous turn; embedded verbatim it retrieves on the word "that".
_ANAPHORIC = (
    re.compile(r"^(and|but|so|ok|okay)?\s*(what|how) about\b"),
    re.compile(r"^(explain|expand|elaborate|clarify|simplify|summari[sz]e)\b.*"
               r"\b(that|this|it|them|those|these|again|more|further)\b"),
    re.compile(r"^(tell me )?more\b"),
    re.compile(r"^(why|how|when|where)( is| was| does| did| are)?\??$"),
    re.compile(r"^(the )?(first|second|third|last|next|previous|other) (one|point|part)\b"),
    re.compile(r"^(can you )?(give|show) (me )?(an? )?example"),
    re.compile(r"^(in )?(simpler|plain|easier) (terms|english|words)"),
    re.compile(r"^(go on|continue|keep going|and\?|then\?)$"),
    re.compile(r"^(that|this|it|those|these)\b"),
    # Challenges to the previous answer. "Are you sure?" is about the turn before
    # it, not about the material — embedded verbatim it retrieves on nothing and
    # the tutor answers a confidence question with "your books don't cover this",
    # which is nonsense to the person who just read the answer being questioned.
    # As a follow-up it is condensed against the transcript, so the turn that was
    # challenged is re-run and confirmed, corrected, or re-refused — consistently.
    re.compile(r"^(are|is) (you|u|that|this|it) (sure|certain|positive|right|correct|true)\b"),
    re.compile(r"^(really|seriously|(you|u) sure)$"),
)

# A question does not need a question mark. Readers type "define enthalpy".
_INTERROGATIVE = re.compile(
    r"^(what|why|how|when|where|who|whom|whose|which|is|are|was|were|do|does|did|"
    r"can|could|should|would|will|has|have|had|may|might)\b"
)
_IMPERATIVE = re.compile(
    r"^(explain|define|describe|summari[sz]e|compare|contrast|list|outline|give|"
    r"tell|show|name|state|discuss|analyse|analyze|evaluate|derive|prove|calculate|"
    r"solve|find|write|teach|walk)\b"
)

_LETTER = re.compile(r"[a-z]")
_CONSONANT_RUN = re.compile(r"[bcdfghjklmnpqrstvwxz]{6,}")
_VOWEL = re.compile(r"[aeiouy]")


def normalise(text: str) -> str:
    """Lowercase, collapse whitespace, drop trailing punctuation.

    Trailing punctuation goes so "hi!!!" and "hi" are one lexicon entry, but internal
    punctuation stays: stripping it everywhere would turn "what's up" into "whats up"
    and "H2O?" into "h2o", and only one of those is wanted.
    """
    return re.sub(r"\s+", " ", text.strip().lower()).rstrip("!.?,;: ")


def looks_like_keysmash(text: str) -> bool:
    """A single token with no vowel structure at all.

    Deliberately weak, and the asymmetry is the reason. A keysmash that slips through
    costs one embedding and one search that finds nothing, which produces a grounded
    refusal -- a fine outcome. A real word caught here tells somebody who typed
    "strengths" or "rhythms" that their question was gibberish, which is not.

    So this only fires on what it can prove: a token with no vowel at all, or one
    with a consonant run no English word has. "asdkjhasdkjh" is not caught, and does
    not need to be -- it falls through to a search and an honest refusal.

    Only ever looks at ONE token, so a real question is never dragged in here by a
    single odd term inside it.
    """
    if " " in text:
        return False
    if len(text) < 4:
        return False
    return not _VOWEL.search(text) or bool(_CONSONANT_RUN.search(text))


def classify_offline(text: str, *, has_history: bool) -> MessageIntent | None:
    """Decide from rules alone, or return ``None`` meaning "ask the model".

    Pure and synchronous so the whole table of cases is testable without a running
    LLM, which is the point: this is the code path every single message takes.
    """
    cleaned = normalise(text)

    if not cleaned or not _LETTER.search(cleaned):
        # Digits, emoji and punctuation alone. Nothing to retrieve on.
        return MessageIntent.UNCLEAR
    if looks_like_keysmash(cleaned):
        return MessageIntent.UNCLEAR

    if cleaned in _GREETINGS:
        return MessageIntent.GREETING
    if cleaned in _CHITCHAT:
        return MessageIntent.CHITCHAT
    if any(pattern.search(cleaned) for pattern in _META):
        return MessageIntent.META

    words = cleaned.split()

    # Short and subject-less, with a transcript to resolve against. Length matters as
    # much as the opener: "what about the role of ATP in glycolysis" opens the same
    # way but names its own subject, retrieves fine as written, and is a question.
    # Six is the tightest bound that still admits "what about the second one".
    # A demonstrative aimed at the material ("this book") is a subject, not a
    # reference to the previous turn, so it never rides this branch.
    if (
        has_history
        and len(words) <= 6
        and not _MATERIAL_REFERENCE.search(cleaned)
        and any(p.search(cleaned) for p in _ANAPHORIC)
    ):
        return MessageIntent.FOLLOW_UP

    if _INTERROGATIVE.match(cleaned) or _IMPERATIVE.match(cleaned):
        return MessageIntent.QUESTION
    if cleaned.endswith("?") or text.rstrip().endswith("?"):
        return MessageIntent.QUESTION

    # A substantial noun phrase with no interrogative -- "the krebs cycle",
    # "photosynthesis light reactions". Readers type these constantly and mean
    # "tell me about this".
    if 2 <= len(words) <= 8:
        return MessageIntent.QUESTION

    # Long and declarative, with no interrogative, no imperative and no question
    # mark -- "i keep mixing up mitosis and meiosis every single time". Probably a
    # question in spirit, but odd enough that the model reads it better than a
    # length heuristic, so it escalates rather than being force-labelled. Safe in
    # both configurations: with the fallback off, `classify` resolves None to
    # QUESTION -- exactly the label this branch used to assign.
    if len(words) > 8:
        return None

    # One real word. Genuinely ambiguous: "enthalpy" is a topic, "yeah" is not.
    return None


async def classify(text: str, *, has_history: bool) -> MessageIntent:
    """Classify a message, consulting the model only when the rules cannot.

    Never raises. An unavailable or incoherent classifier yields ``QUESTION``, so the
    worst outcome of a broken classifier is the behaviour this module replaced.
    """
    decided = classify_offline(text, has_history=has_history)
    if decided is not None:
        chat_intents_total.labels(decided.value, "rules").inc()
        return decided

    if not settings.chat_intent_llm_fallback:
        chat_intents_total.labels(MessageIntent.QUESTION.value, "default").inc()
        return MessageIntent.QUESTION

    from app.clients import llm
    from app.rag import prompts

    try:
        raw = await llm.complete(prompts.intent_prompt(text, has_history=has_history))
    except (DomainError, Exception) as exc:  # noqa: BLE001 -- see module docstring
        logger.warning("intent classification unavailable", extra={"error": str(exc)})
        chat_intents_total.labels(MessageIntent.QUESTION.value, "default").inc()
        return MessageIntent.QUESTION

    token = normalise(raw).split()[0] if normalise(raw) else ""
    try:
        intent = MessageIntent(token)
    except ValueError:
        logger.info("intent classifier returned an unknown label", extra={"label": token[:40]})
        chat_intents_total.labels(MessageIntent.QUESTION.value, "default").inc()
        return MessageIntent.QUESTION

    # The model may only *narrow* into conversational intents. It is not allowed to
    # promote something into FOLLOW_UP when there is no transcript to resolve against,
    # which would send an empty condensation to retrieval.
    if intent is MessageIntent.FOLLOW_UP and not has_history:
        intent = MessageIntent.QUESTION

    chat_intents_total.labels(intent.value, "model").inc()
    return intent
