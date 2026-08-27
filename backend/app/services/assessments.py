"""Assessment authoring: drafting, generating, editing, publishing. Phase 5.

Two rules shape everything here.

**The author's books only.** Generation draws from what the *author* may read —
canon, plus the author's own uploads whether shared or not (DECISIONS.md D29). It can
never reach anybody else's personal book: the retrieval predicate is bound to the
author's id at construction. Publishing a paper drawn from a private book is the
author's deliberate act of exposing that material through its questions.

**Generation produces a draft.** Questions land in a `draft` assessment and a human
publishes. The person publishing is the author of record; the model is a first pass
(DECISIONS.md D11).
"""

import json
import logging
import re
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients import embeddings, llm
from app.core.config import settings
from app.core.errors import Conflict, NotFound, ValidationFailed
from app.core.metrics import questions_generated_total, questions_rejected_total
from app.core.security import Principal
from app.db.models import Assessment, Attempt, Book, Chunk, Question
from app.db.models.enums import (
    AssessmentStatus,
    BookScope,
    Difficulty,
    QuestionFormat,
    QuestionOrigin,
    QuestionType,
    Role,
)
from app.rag import formats, prompts
from app.rag.retrieve import fetch_generation_chunks
from app.schemas.assessment import AssessmentCreate, AssessmentUpdate, QuestionWrite
from app.schemas.attempt import GeneratedQuestion
from app.services import audit
from app.services.generation_trace import GenerationTrace, attach_trace

logger = logging.getLogger(__name__)

# Phrases that make a stem unusable because it assumes the reader can see the passage.
_SELF_REFERENCE = re.compile(
    r"\b(the (passage|text|excerpt|extract)|according to the|as (mentioned|stated|described) "
    r"(above|in the)|in the (above|following) (passage|text))\b",
    re.IGNORECASE,
)

_BANNED_OPTIONS = {"all of the above", "none of the above", "both a and b", "all of these"}

# Canonical keys, assigned by us and never by the model. Eight, because a match grid
# and a select-all can both run past D.
_OPTION_KEYS = "ABCDEFGH"
# Match grids number their left-hand column rather than lettering it, so the two sides
# of the grid cannot be confused for each other on screen or in a stored answer.
_ITEM_KEYS = "12345678"


# ============================================================ authoring


def draft_source_clause(principal: Principal):
    """Books this caller may draw a paper from: shared, or their own (D29).

    Built pure, like the retrieval predicate in ``rag/retrieve.py``, so
    ``tests/test_generation.py`` compiles the exact SQL the check runs rather
    than a reconstruction that could drift from it. The owner branch is bound to
    ``principal.id`` at construction — there is no argument that can widen it to
    somebody else's personal book.
    """
    return or_(Book.scope == BookScope.CANON, Book.owner_id == principal.id)


async def create_draft(
    session: AsyncSession, principal: Principal, data: AssessmentCreate
) -> Assessment:
    """Create the paper and mark it generating.

    The source selection arrives in a request body, so every book id in it is checked
    here against what this caller may actually draw from: a shared book, or a book
    they own themselves (D29). A book that fails is not silently skipped — the whole
    request is refused, because an author who asked for four books and got a paper
    from two has been given something they did not ask for and has no way to notice.
    """
    if not data.source.book_ids:
        raise ValidationFailed("Choose at least one book to draw from.")

    # Runs under RLS, so a book the caller cannot see is already invisible here; the
    # explicit predicate is the second, independent half of the same rule — it must
    # hold even if this service is ever called without RLS behind it.
    visible = {
        book_id
        for book_id in await session.scalars(
            select(Book.id).where(
                Book.id.in_(data.source.book_ids),
                draft_source_clause(principal),
            )
        )
    }
    missing = [str(book_id) for book_id in data.source.book_ids if book_id not in visible]
    if missing:
        raise ValidationFailed(
            "A paper can only be built from books you uploaded or books someone has "
            "shared. One or more of the books you chose is neither, or no longer exists."
        )

    # An empty formats list means *auto*, not "none": resolve it here rather than at
    # generation time so the row records what will actually be written, and the author
    # can see it on the paper while it is still being drafted.
    chosen_formats = formats.resolve_formats(data.formats, assessment_type=data.type)
    chosen_levels = formats.resolve_levels(data.levels)

    assessment = Assessment(
        author_id=principal.id,
        title=data.title.strip(),
        # Derived from the formats rather than trusted from the client. An author who
        # ticked only `long_answer` has written a subjective paper whatever the
        # dropdown said, and the share-link preview tells a prospective sitter which
        # kind of paper they are about to open.
        type=formats.derive_type(chosen_formats),
        rigor=data.rigor,
        source_selection={
            "book_ids": [str(book_id) for book_id in data.source.book_ids],
            "chapter_ids": [str(chapter_id) for chapter_id in data.source.chapter_ids],
        },
        generation_spec={
            # The requested count, kept here because `question_count` on the row does
            # not survive: the worker overwrites it with what was actually written. So
            # after a short run there was no record of what had been asked for, and
            # "why did I only get one question" was unanswerable from the data.
            "count": data.question_count,
            "formats": [fmt.value for fmt in chosen_formats],
            "levels": [level.value for level in chosen_levels],
            "instructions": (data.instructions or "").strip() or None,
            # Whether the author picked or skipped. Worth keeping: "the model chose
            # these formats" and "the author chose these formats" are different
            # answers to the same complaint about a paper.
            "auto": not data.formats,
        },
        question_count=data.question_count,
        duration_minutes=data.duration_minutes,
        results_release=data.results_release,
        proctoring_enabled=data.proctoring_enabled,
        status=AssessmentStatus.GENERATING,
    )
    session.add(assessment)
    await session.flush()
    await session.refresh(assessment)
    return assessment


async def get_assessment(
    session: AsyncSession, principal: Principal, assessment_id: UUID
) -> Assessment:
    assessment = await session.scalar(
        select(Assessment).where(Assessment.id == assessment_id)
    )
    if assessment is None:
        # RLS already hid anything this caller may not see, so "invisible" and "absent"
        # are the same answer — which is the answer we want to give.
        raise NotFound("That assessment does not exist.")
    return assessment


async def list_authored(
    session: AsyncSession, principal: Principal, *, limit: int = 50
) -> list[Assessment]:
    """Papers this caller wrote. Not papers they have sat — those are attempts."""
    return list(
        await session.scalars(
            select(Assessment)
            .where(Assessment.author_id == principal.id)
            .order_by(Assessment.created_at.desc())
            .limit(limit)
        )
    )


async def list_questions(session: AsyncSession, assessment_id: UUID) -> list[Question]:
    """The author's view of a paper. RLS admits only the author to this table at all."""
    return list(
        await session.scalars(
            select(Question)
            .where(Question.assessment_id == assessment_id)
            .order_by(Question.index)
        )
    )


async def attempt_counts(session: AsyncSession, assessment_ids: list[UUID]) -> dict[UUID, int]:
    if not assessment_ids:
        return {}
    rows = await session.execute(
        select(Attempt.assessment_id, func.count())
        .where(Attempt.assessment_id.in_(assessment_ids))
        .group_by(Attempt.assessment_id)
    )
    return {assessment_id: count for assessment_id, count in rows.all()}


async def update_draft(
    session: AsyncSession, principal: Principal, assessment_id: UUID, data: AssessmentUpdate
) -> Assessment:
    assessment = await get_assessment(session, principal, assessment_id)
    _require_editable(assessment)

    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(assessment, field, value)

    if (
        assessment.opens_at
        and assessment.closes_at
        and assessment.opens_at >= assessment.closes_at
    ):
        raise ValidationFailed("The closing time must be after the opening time.")

    await session.flush()
    return assessment


def _require_editable(assessment: Assessment) -> None:
    """A published paper with attempts is frozen.

    Editing a stem mid-exam changes the paper under people who already answered it.
    The fix for a bad question after the fact is to void it and rescale, never to
    silently rewrite it.
    """
    if assessment.status is AssessmentStatus.GENERATING:
        raise Conflict("This paper is still being written. Wait for it to finish.")
    if assessment.status is not AssessmentStatus.DRAFT:
        raise Conflict("A published paper cannot be edited. Close it and write a new one.")


# ============================================================ question shape
#
# **One builder, two callers.** Everything that produces a question row goes through
# `build_question_fields` — the model's output and the author's own typing alike. A
# human can write a two-correct-answer multiple choice just as easily as a model can,
# and the consequence is identical: a question nobody can get right, sitting in a
# published paper looking like the others.
#
# It canonicalises as well as checks, because the two cannot be separated safely. The
# keys a model returns are its own — sometimes "a"/"b", sometimes "1"/"2", sometimes
# repeated — and every answer it gives refers to them. Renumbering the options without
# remapping `pairs`, `order` and `correct_options` in the same pass is how a paper ends
# up marked against the wrong letters.


def _canonical_choices(
    items: list[dict[str, str]] | None, alphabet: str
) -> tuple[list[dict[str, str]], dict[str, str]]:
    """Renumber a choice list onto our keys. Returns the list and old -> new.

    Ours, never the model's: `_OPTION_KEYS` for anything picked and `_ITEM_KEYS` for
    the left column of a match grid, so the two sides of a grid can never be confused
    for one another on screen or in a stored answer.
    """
    canonical: list[dict[str, str]] = []
    remap: dict[str, str] = {}
    for position, item in enumerate(items or []):
        if position >= len(alphabet):
            break
        text = str(item.get("text", "")).strip()
        new_key = alphabet[position]
        old_key = str(item.get("key", "")).strip().upper()
        # A duplicated key in the source is not repaired: the first wins, and the
        # answer that pointed at the second lands nowhere. Validation below catches it.
        remap.setdefault(old_key or new_key, new_key)
        canonical.append({"key": new_key, "text": text})
    return canonical, remap


def _require_usable_keys(items: list[dict[str, str]] | None, noun: str) -> None:
    """Refuse a choice list whose own keys cannot be told apart.

    Every answer a model gives — `correct_option`, `pairs`, `order` — points at these
    keys, and we are about to renumber them. Two options both keyed "A" means the
    pointer resolves to the first of them, so a question with four perfectly good
    distinct options can be marked against the wrong one. Nothing on screen looks
    wrong, which is why this is refused rather than repaired.

    Keys left blank are fine and common: with none given, position IS the key, and the
    renumbering is exactly right.
    """
    given = [str(item.get("key", "")).strip().upper() for item in (items or [])]
    named = [key for key in given if key]
    if len(set(named)) != len(named):
        raise ValidationFailed(f"Two {noun} share the same label.")


def _distinct_texts(options: list[dict[str, str]]) -> bool:
    texts = [option["text"].strip().lower() for option in options]
    return all(texts) and len(set(texts)) == len(texts)


def build_question_fields(
    fmt: QuestionFormat,
    *,
    stem: str,
    options: list[dict[str, str]] | None = None,
    prompt_items: list[dict[str, str]] | None = None,
    correct_option: str | None = None,
    correct_options: list[str] | None = None,
    accepted: list[str] | None = None,
    tolerance: float | None = None,
    pairs: dict[str, str] | None = None,
    order: list[str] | None = None,
    model_answer: str | None = None,
    rubric: list[dict[str, Any]] | None = None,
    points: Decimal | None = None,
) -> dict[str, Any]:
    """Canonicalise one question into columns, or raise ``ValidationFailed`` saying why.

    The returned dict is exactly the column set: every field a format does not use is
    explicitly ``None`` rather than left off, so a question edited from a match grid
    into a true/false does not keep a stale `answer_key` that outranks its new answer.
    """
    spec = formats.SPECS[fmt]
    stem = (stem or "").strip()
    if len(stem) < spec.min_stem:
        raise ValidationFailed(f"That {spec.label.lower()} needs a longer question.")

    # Old key -> ours. Empty for the families that have no choices at all, which is
    # why every lookup goes through `_remapped` and returns None rather than raising.
    remap: dict[str, str] = {}

    fields: dict[str, Any] = {
        "type": spec.family,
        "format": fmt,
        "stem": stem,
        "options": None,
        "correct_option": None,
        "prompt_items": None,
        "answer_key": None,
        "model_answer": None,
        "rubric": None,
        "points": points if points is not None else spec.default_points,
    }

    # ---------------------------------------------------------------- choices
    if spec.min_options:
        _require_usable_keys(options, "options")
        chosen, remap = _canonical_choices(options, _OPTION_KEYS)
        if spec.fixed_options:
            # True/false and yes/no do not get to invent their options. Rebuild them
            # in a fixed order and work out which key the given answer meant, so a
            # model that returned False first does not invert the whole paper.
            chosen, correct_option = _rebuild_fixed(
                chosen, remap, spec.fixed_options, correct_option
            )
            remap = {option["key"]: option["key"] for option in chosen}
        if not (spec.min_options <= len(chosen) <= spec.max_options):
            raise ValidationFailed(
                f"A {spec.label.lower()} needs between {spec.min_options} and "
                f"{spec.max_options} options."
            )
        if not _distinct_texts(chosen):
            raise ValidationFailed("Two options are duplicates, or one of them is empty.")
        if any(option["text"].strip().lower() in _BANNED_OPTIONS for option in chosen):
            raise ValidationFailed(
                "\u201cAll of the above\u201d and its cousins test whether somebody has "
                "seen the trick, not whether they read the material."
            )
        fields["options"] = chosen
    elif options:
        raise ValidationFailed(f"A {spec.label.lower()} does not have options.")

    # ------------------------------------------------------ family by family
    family = spec.family

    if family is QuestionType.MCQ:
        key = _remapped(correct_option, remap)
        if key is None:
            raise ValidationFailed("The correct answer must be one of the options.")
        fields["correct_option"] = key

    elif family is QuestionType.MULTI_SELECT:
        keys = [k for k in (_remapped(c, remap) for c in correct_options or []) if k]
        keys = list(dict.fromkeys(keys))
        if len(keys) < 2:
            raise ValidationFailed(
                "A select-all question needs at least two correct options — with one, "
                "it is an ordinary multiple choice and should be written as one."
            )
        if len(keys) >= len(fields["options"]):
            raise ValidationFailed("Every option cannot be correct.")
        fields["answer_key"] = {"correct_options": keys}

    elif family is QuestionType.MATCH:
        _require_usable_keys(prompt_items, "items")
        left, left_remap = _canonical_choices(prompt_items, _ITEM_KEYS)
        if len(left) < 3:
            raise ValidationFailed("A match grid needs at least three items to match.")
        if not _distinct_texts(left):
            raise ValidationFailed("Two items on the left are duplicates.")
        mapped = {}
        for old_left, old_right in (pairs or {}).items():
            new_left = left_remap.get(str(old_left).strip().upper())
            new_right = _remapped(old_right, remap)
            if new_left and new_right:
                mapped[new_left] = new_right
        if len(mapped) != len(left):
            raise ValidationFailed("Every item on the left needs exactly one match.")
        if len(left) == len(fields["options"]) and len(set(mapped.values())) != len(mapped):
            # Equal columns means a one-to-one grid. Two lefts sharing a right leaves
            # a right-hand item that pairs with nothing, which is unanswerable.
            raise ValidationFailed("Two items on the left are matched to the same answer.")
        fields["prompt_items"] = left
        fields["answer_key"] = {"pairs": mapped}

    elif family is QuestionType.SEQUENCE:
        wanted = [k for k in (_remapped(c, remap) for c in order or []) if k]
        expected = {option["key"] for option in fields["options"]}
        if set(wanted) != expected or len(wanted) != len(expected):
            raise ValidationFailed(
                "The correct order must list every item exactly once."
            )
        fields["answer_key"] = {"order": wanted}

    elif family is QuestionType.SHORT_TEXT:
        answers = [str(item).strip() for item in (accepted or []) if str(item).strip()]
        if not answers:
            raise ValidationFailed(
                "A typed answer is marked by comparison, so it needs at least one "
                "accepted answer to compare against."
            )
        if fmt is QuestionFormat.ONE_WORD and any(len(a.split()) > 4 for a in answers):
            raise ValidationFailed(
                "A one-word answer should be one or two words. Ask this as a short "
                "answer instead, so it can be marked against a rubric."
            )
        accepted_key: dict[str, Any] = {"accepted": answers}
        if tolerance:
            accepted_key["tolerance"] = abs(float(tolerance))
        fields["answer_key"] = accepted_key

    elif family is QuestionType.SUBJECTIVE:
        model_text = (model_answer or "").strip()
        if not model_text:
            raise ValidationFailed("A written question needs a model answer to grade against.")
        fields["model_answer"] = model_text
        cleaned: list[dict[str, Any]] = [
            {"criterion": str(item.get("criterion", "")).strip(),
             "points": float(item.get("points", 0) or 0)}
            for item in (rubric or [])
            if str(item.get("criterion", "")).strip()
        ]
        if cleaned:
            total = sum(item["points"] for item in cleaned)
            if total <= 0:
                raise ValidationFailed("The rubric awards no marks.")
            fields["rubric"] = cleaned
            # The rubric decides what the question is worth. A mark total that
            # disagreed with its own breakdown is the one number somebody will check
            # by hand — and the author who set `points` explicitly is telling us the
            # rubric is wrong, so their number loses to nothing.
            if points is None:
                fields["points"] = Decimal(str(total))
            elif abs(total - float(points)) > 0.01:
                raise ValidationFailed(
                    f"The rubric adds up to {total:g} but the question is worth "
                    f"{float(points):g}."
                )

    # Each independently marked part is worth one mark, so partial credit divides
    # evenly and a four-pair grid marked three-quarters right is worth three.
    if points is None:
        if family is QuestionType.MATCH:
            fields["points"] = Decimal(len(fields["answer_key"]["pairs"]))
        elif family is QuestionType.MULTI_SELECT:
            fields["points"] = Decimal(len(fields["answer_key"]["correct_options"]))
        elif family is QuestionType.SEQUENCE:
            fields["points"] = Decimal(len(fields["answer_key"]["order"]))

    if fmt is QuestionFormat.FILL_BLANK and "___" not in stem:
        raise ValidationFailed("A fill-in-the-blank needs a blank: write ____ in the sentence.")

    return fields


def _remapped(key: str | None, remap: dict[str, str]) -> str | None:
    """Translate one of the model's keys into ours, or None if it names nothing."""
    if key is None:
        return None
    return remap.get(str(key).strip().upper())


def _rebuild_fixed(
    chosen: list[dict[str, str]],
    remap: dict[str, str],
    fixed: tuple[str, ...],
    correct_option: str | None,
) -> tuple[list[dict[str, str]], str]:
    """Force true/false and yes/no onto their two options, in their fixed order.

    A model that returns "False" first and marks it "A" is not wrong about the answer,
    only about the ordering — so read which *text* it chose and re-key that, rather
    than trusting a letter whose meaning we are about to change.
    """
    if len(chosen) != len(fixed):
        raise ValidationFailed(f"This question needs exactly {len(fixed)} options.")

    picked = _remapped(correct_option, remap)
    chosen_text = next(
        (option["text"] for option in chosen if option["key"] == picked), ""
    ).strip().lower()

    rebuilt = [{"key": _OPTION_KEYS[i], "text": text} for i, text in enumerate(fixed)]
    for index, text in enumerate(fixed):
        if chosen_text == text.lower():
            return rebuilt, _OPTION_KEYS[index]
    raise ValidationFailed(
        f"The answer must be one of {' or '.join(fixed)}."
    )


# ============================================================ questions


async def write_question(
    session: AsyncSession,
    principal: Principal,
    assessment_id: UUID,
    question_id: UUID | None,
    data: QuestionWrite,
) -> Question:
    """Edit an existing question, or write a new one by hand.

    ``origin`` is set here rather than accepted from the client: an edit is `edited`,
    something written from scratch is `written`. Keeping the distinction honest is what
    makes "how much of this paper did the model actually earn" an answerable question.
    """
    assessment = await get_assessment(session, principal, assessment_id)
    _require_editable(assessment)

    # The same builder the model's output goes through, and the same rules. A human
    # can write a two-correct-answer multiple choice just as easily as a model can,
    # and the result — a question nobody can get right — is identical.
    fields = build_question_fields(
        data.format,
        stem=data.stem,
        options=[option.model_dump() for option in data.options] if data.options else None,
        prompt_items=(
            [item.model_dump() for item in data.prompt_items] if data.prompt_items else None
        ),
        correct_option=data.correct_option,
        correct_options=(data.answer_key or {}).get("correct_options"),
        accepted=(data.answer_key or {}).get("accepted"),
        tolerance=(data.answer_key or {}).get("tolerance"),
        pairs=(data.answer_key or {}).get("pairs"),
        order=(data.answer_key or {}).get("order"),
        model_answer=data.model_answer,
        rubric=data.rubric,
        points=Decimal(str(data.points)),
    )

    if question_id is not None:
        question = await session.scalar(
            select(Question).where(
                Question.id == question_id, Question.assessment_id == assessment_id
            )
        )
        if question is None:
            raise NotFound("That question does not exist.")
        question.origin = QuestionOrigin.EDITED
    else:
        next_index = await session.scalar(
            select(func.coalesce(func.max(Question.index), -1) + 1).where(
                Question.assessment_id == assessment_id
            )
        )
        question = Question(
            assessment_id=assessment_id,
            index=next_index or 0,
            origin=QuestionOrigin.WRITTEN,
            source_chunk_ids=[],
        )
        session.add(question)

    # Every column the format does not use is written as None rather than left alone,
    # so a match grid edited into a true/false does not keep a stale `answer_key` that
    # outranks its new answer.
    for column, value in fields.items():
        setattr(question, column, value)
    question.difficulty = data.difficulty

    await session.flush()
    await session.refresh(question)
    return question


async def delete_question(
    session: AsyncSession, principal: Principal, assessment_id: UUID, question_id: UUID
) -> None:
    assessment = await get_assessment(session, principal, assessment_id)
    _require_editable(assessment)

    question = await session.scalar(
        select(Question).where(
            Question.id == question_id, Question.assessment_id == assessment_id
        )
    )
    if question is None:
        raise NotFound("That question does not exist.")

    await session.delete(question)
    await session.flush()
    await _reindex(session, assessment_id)


async def _reindex(session: AsyncSession, assessment_id: UUID) -> None:
    """Close the gap a delete leaves.

    Two passes with an offset, because ``(assessment_id, index)`` is unique and
    renumbering in place collides with the row that currently holds the target index.
    """
    questions = await list_questions(session, assessment_id)
    for offset, question in enumerate(questions):
        question.index = 10_000 + offset
    await session.flush()
    for offset, question in enumerate(questions):
        question.index = offset
    await session.flush()


# ============================================================ publishing


async def publish(
    session: AsyncSession, principal: Principal, assessment_id: UUID
) -> Assessment:
    """Freeze the paper, mint its share token, and make it sittable.

    ``max_score`` is frozen from the sum of question points rather than computed on
    read. Voiding a question later must not silently rescale a paper somebody has
    already sat.
    """
    assessment = await get_assessment(session, principal, assessment_id)

    if assessment.status is AssessmentStatus.GENERATING:
        raise Conflict("This paper is still being written.")
    if assessment.status is not AssessmentStatus.DRAFT:
        raise Conflict("This paper is already published.")

    questions = await list_questions(session, assessment_id)
    if not questions:
        raise ValidationFailed("A paper needs at least one question before it can be shared.")

    # An answer to mark against, whatever the family holds it in. Checked at publish
    # rather than only at write time because a paper can be assembled over days and
    # the question that was never finished is the one nobody remembers.
    missing_key = [
        q.index + 1
        for q in questions
        if (q.type is QuestionType.MCQ and not q.correct_option)
        or (q.type is QuestionType.SUBJECTIVE and not q.model_answer)
        or (
            q.type
            in (
                QuestionType.MULTI_SELECT,
                QuestionType.SHORT_TEXT,
                QuestionType.MATCH,
                QuestionType.SEQUENCE,
            )
            and not q.answer_key
        )
    ]
    if missing_key:
        raise ValidationFailed(
            f"Question {missing_key[0]} has no answer to grade against. Fix it first."
        )

    assessment.status = AssessmentStatus.PUBLISHED
    assessment.max_score = sum((q.points for q in questions), Decimal("0"))
    assessment.question_count = len(questions)

    # Minted by the database, never here — see public.generate_share_token().
    token = await session.scalar(select(func.public.generate_share_token()))
    assessment.share_token = token
    await session.flush()

    await audit.record(
        session,
        action="assessment.published",
        target_type="assessment",
        target_id=assessment.id,
        metadata={
            "title": assessment.title,
            "questions": len(questions),
            "max_score": str(assessment.max_score),
        },
    )
    return assessment


async def close(session: AsyncSession, principal: Principal, assessment_id: UUID) -> Assessment:
    """Stop accepting new sittings. Attempts already in progress keep their deadline."""
    assessment = await get_assessment(session, principal, assessment_id)
    if assessment.status is not AssessmentStatus.PUBLISHED:
        raise Conflict("Only a published paper can be closed.")

    assessment.status = AssessmentStatus.CLOSED
    await session.flush()
    await audit.record(
        session,
        action="assessment.closed",
        target_type="assessment",
        target_id=assessment.id,
        metadata={"title": assessment.title},
    )
    return assessment


# ============================================================ export
#
# A paper as a file the author can keep, print, or hand to something else.
#
# **The author only, and that is not a formality.** This payload carries
# `correct_option`, `answer_key`, `model_answer` and `rubric` — it is the answer key to
# an exam other people are going to sit. The route is guarded by
# `require_assessment_author`, which is the same guard on the same rows as the review
# screen.
#
# **No audit row**, deliberately, and for the same reason the chat export writes none:
# an author reading `GET /assessments/{id}` is already handed every field in here. This
# is a reformatting of a disclosure that has already happened, not a new one. Auditing
# the download while leaving the screen unaudited would be theatre.
#
# **The share token is not in it.** That is the one field an author can see which this
# deliberately omits: the token IS the access grant (D16), so it is a credential, and a
# credential inside a file that gets emailed around is how a paper leaks. The author
# copies the link from the review screen, where it is visibly a link rather than one
# more field in a document.
#
# **Attempts are not in it either.** Who sat the paper and what they scored is a
# different document about different people, and it is not what "export the assessment"
# asks for.

# Versioned so a future shape change is detectable by whatever was written against this
# one, instead of silently reading differently.
_EXPORT_FORMAT_TAG = "kitaably.assessment.v1"

_SLUG = re.compile(r"[^a-z0-9]+")


def _export_filename(assessment: Assessment, extension: str) -> str:
    """A filename a person can recognise in a downloads folder.

    The title slug says which paper; the id suffix keeps two exports of "photosynthesis"
    from overwriting each other. ASCII by construction, because this string travels in a
    Content-Disposition header.
    """
    slug = _SLUG.sub("-", (assessment.title or "paper").lower()).strip("-")[:40] or "paper"
    return f"kitaably-{slug}-{assessment.id.hex[:8]}.{extension}"


def _question_json(question: Question) -> dict[str, Any]:
    """One question, with everything about it.

    The answer fields are written per family rather than all four always — a match grid
    with `"correct_option": null` beside its real key invites a reader to believe the
    null means something. Only the fields this format actually uses are present.
    """
    spec = formats.SPECS[question.format]
    payload: dict[str, Any] = {
        "index": question.index,
        # Both, because they are different things and an importer needs both: `format`
        # is the shape, `type` is the family that marks it (DECISIONS.md D25).
        "format": question.format.value,
        "format_label": spec.label,
        "type": question.type.value,
        "stem": question.stem,
        "points": float(question.points),
        "difficulty": question.difficulty.value if question.difficulty else None,
        "origin": question.origin.value,
        # Provenance travels with the question. A question an author cannot trace to a
        # passage is one they cannot defend, and that stays true outside the database.
        "source_chunk_ids": question.source_chunk_ids or [],
    }
    if question.options:
        payload["options"] = question.options
    if question.prompt_items:
        payload["prompt_items"] = question.prompt_items

    answer: dict[str, Any] = {}
    if question.correct_option:
        answer["correct_option"] = question.correct_option
    if question.answer_key:
        answer.update(question.answer_key)
    if question.model_answer:
        answer["model_answer"] = question.model_answer
    if question.rubric:
        answer["rubric"] = question.rubric
    payload["answer"] = answer
    return payload


def render_export_json(assessment: Assessment, questions: list[Question]) -> str:
    """The paper as data: every field the author's detail endpoint already returns."""
    spec = assessment.generation_spec or {}
    return json.dumps(
        {
            "format": _EXPORT_FORMAT_TAG,
            "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "assessment": {
                "id": str(assessment.id),
                "title": assessment.title,
                "type": assessment.type.value,
                "rigor": assessment.rigor.value,
                "status": assessment.status.value,
                "question_count": len(questions),
                "duration_minutes": assessment.duration_minutes,
                "results_release": assessment.results_release.value,
                "max_score": float(assessment.max_score)
                if assessment.max_score is not None
                else None,
                # Why this paper is the length it is, when it is not the length that
                # was asked for. It belongs in the file: an export is what somebody
                # reads six months later when they cannot remember.
                "generation_note": assessment.generation_note,
                "opens_at": _stamp(assessment.opens_at),
                "closes_at": _stamp(assessment.closes_at),
                "created_at": _stamp(assessment.created_at),
                "updated_at": _stamp(assessment.updated_at),
                # What was asked for, beside what came out. "Why is this paper full of
                # true/false questions" is answerable from the file itself.
                "requested": {
                    "count": spec.get("count"),
                    "formats": spec.get("formats", []),
                    "levels": spec.get("levels", []),
                    "instructions": spec.get("instructions"),
                    "auto": bool(spec.get("auto", False)),
                },
                # Absent on purpose: share_token. It is the access grant, so it is a
                # credential, and a credential in a file that gets forwarded is how a
                # paper leaks.
            },
            "questions": [_question_json(question) for question in questions],
        },
        indent=2,
        ensure_ascii=False,
    )


def _stamp(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _answer_in_words(question: Question) -> list[str]:
    """The correct answer for one question, as lines a person reads.

    Spelled out rather than given as bare keys: "B" is not an answer key, it is a
    pointer to one, and a printed key nobody can check against is not worth printing.
    """
    options = {option["key"]: option.get("text", "") for option in (question.options or [])}
    items = {item["key"]: item.get("text", "") for item in (question.prompt_items or [])}
    key = question.answer_key or {}

    if question.type is QuestionType.MCQ and question.correct_option:
        return [f"**{question.correct_option}.** {options.get(question.correct_option, '')}"]

    if question.type is QuestionType.MULTI_SELECT:
        return [
            f"- **{option_key}.** {options.get(option_key, '')}"
            for option_key in key.get("correct_options", [])
        ]

    if question.type is QuestionType.MATCH:
        return [
            f"- **{left}.** {items.get(left, '')} → **{right}.** {options.get(right, '')}"
            for left, right in (key.get("pairs") or {}).items()
        ]

    if question.type is QuestionType.SEQUENCE:
        # An ordered list, because the answer IS an order. A renderer renumbering it
        # cannot change what it says.
        return [
            f"{position}. {options.get(option_key, '')}"
            for position, option_key in enumerate(key.get("order", []), start=1)
        ]

    if question.type is QuestionType.SHORT_TEXT:
        accepted = ", ".join(f"`{answer}`" for answer in key.get("accepted", []))
        if not accepted:
            return []
        line = f"Accepted: {accepted}"
        if key.get("tolerance"):
            # On the same line: two plain lines are one paragraph anyway, and a
            # tolerance that reads as a separate sentence looks like a separate rule.
            line += f" (within {float(key['tolerance']) * 100:g}%)"
        return [line]

    lines = []
    if question.model_answer:
        # Blank line before the rubric, so the list is a list rather than the tail of
        # the model answer's paragraph.
        lines += [question.model_answer, ""]
    for entry in question.rubric or []:
        lines.append(f"- {entry.get('criterion', '')} ({entry.get('points', 0):g})")
    return lines


def _question_markdown(question: Question) -> list[str]:
    """One question as it appears on the paper — without its answer."""
    spec = formats.SPECS[question.format]
    marks = f"{float(question.points):g} {'mark' if question.points == 1 else 'marks'}"
    lines = [
        f"**{question.index + 1}.** ({spec.label} · {marks})",
        "",
        # Assertion-and-reason keeps its two lines; everything else is one paragraph.
        question.stem,
        "",
    ]

    # List syntax, not bare lines. Consecutive plain lines are ONE paragraph in
    # Markdown, so `A. …` / `B. …` renders as a single run-on sentence — which is
    # exactly the half of this file somebody prints and hands out.
    if question.prompt_items:
        lines += [
            f"- **{item['key']}.** {item.get('text', '')}" for item in question.prompt_items
        ]
        lines += ["", "Match with:", ""]

    if question.options:
        lines += [
            f"- **{option['key']}.** {option.get('text', '')}" for option in question.options
        ]
        lines.append("")
    elif question.type is QuestionType.SHORT_TEXT:
        lines += ["Answer: ______________________", ""]
    else:
        # Ruled space, so a printed paper has somewhere to write. A line of underscores
        # is a thematic break in Markdown, which renders as exactly the ruled line we
        # want — and stays a visible line of underscores in a plain-text viewer, so it
        # reads correctly either way. Long answers get more of it, which is the only
        # thing that distinguishes them on paper.
        rules = 10 if question.format is QuestionFormat.LONG_ANSWER else 4
        lines += ["_" * 60 for _ in range(rules)] + [""]

    return lines


def render_export_markdown(assessment: Assessment, questions: list[Question]) -> str:
    """The paper as a document: the questions, then the answer key.

    Two sections rather than answers printed inline, because that is what makes the
    file usable — the first half can be printed for a room and the second kept back.
    An export that interleaved them would be a document an author cannot hand to
    anybody.
    """
    exported = datetime.now(UTC).strftime("%d %b %Y, %H:%M UTC")
    total = sum((question.points for question in questions), Decimal("0"))

    lines = [f"# {assessment.title}", ""]
    facts = [f"{len(questions)} questions", f"{float(total):g} marks"]
    if assessment.duration_minutes:
        facts.append(f"{assessment.duration_minutes} minutes")
    lines += [" · ".join(facts), "", f"*Exported from Kitaably on {exported}.*", ""]
    if assessment.generation_note:
        lines += [f"> {assessment.generation_note}", ""]

    if not questions:
        return "\n".join(lines + ["*This paper has no questions yet.*", ""])

    lines += ["---", "", "## The paper", ""]
    for question in questions:
        lines += _question_markdown(question)

    lines += ["---", "", "## Answer key", ""]
    for question in questions:
        lines.append(f"**{question.index + 1}.**")
        lines.append("")
        answer = _answer_in_words(question)
        lines += answer or ["*No answer recorded.*"]
        lines.append("")

    return "\n".join(lines)


async def export_assessment(
    session: AsyncSession, principal: Principal, assessment_id: UUID, fmt: str
) -> tuple[str, str, str]:
    """One paper as a downloadable file: ``(filename, media_type, body)``."""
    assessment = await get_assessment(session, principal, assessment_id)
    questions = await list_questions(session, assessment_id)

    if fmt == "json":
        return (
            _export_filename(assessment, "json"),
            "application/json",
            render_export_json(assessment, questions),
        )
    if fmt == "md":
        return (
            _export_filename(assessment, "md"),
            "text/markdown; charset=utf-8",
            render_export_markdown(assessment, questions),
        )
    # Unreachable behind the route's enum, kept for any future caller that isn't.
    raise ValidationFailed("That export format isn't supported. Use json or md.")


# ============================================================ generation
#
# Everything below runs on the `llm` queue, never in a request.


def select_chunks_by_coverage(chunks: list[Chunk], *, wanted: int) -> list[Chunk]:
    """Spread the sample across the material instead of clustering on one passage.

    The instinct is a top-k similarity search against the topic. Do not: similarity
    clusters on the densest passage and yields a twenty-question paper that asks about
    one section five times (DECISIONS.md D12).

    Each chapter gets a share of the sample proportional to its length, and within a
    chapter the picks are spread evenly by position rather than taken from the front.
    """
    if not chunks or wanted <= 0:
        return []

    groups: dict[object, list[Chunk]] = {}
    for chunk in chunks:
        groups.setdefault(chunk.chapter_id or chunk.book_id, []).append(chunk)

    total_tokens = sum(chunk.token_count or 0 for chunk in chunks) or len(chunks)
    picked: list[Chunk] = []

    for group in groups.values():
        group_tokens = sum(chunk.token_count or 0 for chunk in group) or len(group)
        share = max(1, round(wanted * group_tokens / total_tokens))
        take = min(share, len(group))
        # Evenly spaced positions across the group, endpoints included.
        step = len(group) / take
        picked.extend(group[min(len(group) - 1, int(i * step))] for i in range(take))

    # Deduplicate by identity, then trim: rounding up per group can overshoot.
    seen: set = set()
    unique: list[Chunk] = []
    for chunk in picked:
        if chunk.id not in seen:
            seen.add(chunk.id)
            unique.append(chunk)
    return unique[:wanted]


def parse_generated(raw: str) -> list[GeneratedQuestion]:
    """Parse the model's reply. Never regex a response into shape.

    Tolerates a markdown fence and leading prose because small local models add them
    despite being told not to; anything beyond that is a failed batch, not something
    to repair. A repaired response is a response nobody validated.
    """
    text = raw.strip()
    if "```" in text:
        blocks = re.findall(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if blocks:
            text = blocks[0].strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in response")

    payload = json.loads(text[start : end + 1])
    items = payload.get("questions")
    if not isinstance(items, list):
        raise ValueError("`questions` is missing or not a list")
    return [GeneratedQuestion.model_validate(item) for item in items]


def validate_generated(
    item: GeneratedQuestion,
    *,
    allowed_chunk_ids: set[str],
    chunk_text: dict[str, str],
    expected_format: QuestionFormat | None = None,
) -> tuple[bool, str]:
    """Reject before persisting. Returns ``(ok, reason)``.

    Three kinds of check, in the order they are cheapest to fail:

    * **Provenance and grounding** — is this question about the passage it claims to
      be about. Only generation can fail these; a human writing a question by hand
      has no chunk to cite.
    * **Self-reference** — a stem that says "the passage above" is unusable to
      somebody who does not have the passage above.
    * **Shape** — delegated to ``build_question_fields``, the same builder the
      author's own typing goes through. Two lists of rules would drift; one does not.

    Every reason returned here is counted by format in ``questions_rejected_total``,
    because "the model cannot write match grids from this book" is the finding that
    tells an author to pick different formats.
    """
    stem = (item.stem or "").strip()
    declared = item.declared_format
    spec = formats.spec_for(declared)
    if spec is None:
        return False, f"unknown format {declared!r}"
    if expected_format is not None and spec.format is not expected_format:
        # The batch asked for one format. A model that answers with another has
        # ignored the instruction, and taking it anyway silently rewrites the paper
        # the author asked for.
        return False, f"wrong format: asked for {expected_format.value}, got {declared}"

    if _SELF_REFERENCE.search(stem):
        return False, "stem refers to the passage"

    if not item.source_chunk_id or item.source_chunk_id not in allowed_chunk_ids:
        # Provenance that cannot be traced is no provenance. A model that invents an
        # id has probably invented the question with it.
        return False, "provenance missing or not from this batch"

    try:
        fields = build_question_fields(
            spec.format,
            stem=stem,
            options=item.options,
            prompt_items=item.prompt_items,
            correct_option=item.correct_option,
            correct_options=item.correct_options,
            accepted=item.accepted,
            tolerance=item.tolerance,
            pairs=item.pairs,
            order=item.order,
            model_answer=item.model_answer,
            rubric=item.rubric,
        )
    except ValidationFailed as exc:
        return False, exc.message

    # Cheap groundedness check: a question sharing no distinctive vocabulary with its
    # source passage is usually one the model brought from somewhere else.
    #
    # Against the whole question, not only the stem — a match grid's stem is "Match
    # each term to its description" and shares nothing with anything, while its
    # sixteen items are the entire question. Checking the stem alone rejected every
    # two-sided question ever generated.
    if not _shares_vocabulary(fields, chunk_text.get(item.source_chunk_id, "")):
        return False, "stem shares no vocabulary with its source passage"

    return True, ""


def _shares_vocabulary(fields: dict[str, Any], source: str) -> bool:
    """Does the question appear to be about the passage it claims to come from?

    The stem alone, for every family whose stem carries the question — which is most
    of them, and checking the stem alone is what makes this strict: a stem about the
    Jupiter symphony attached to a passage about chloroplasts is caught even when its
    four options are lifted verbatim from that passage.

    Match grids and sequences are the exception, and not an arbitrary one: their stems
    are boilerplate ("Match each term to its description") and the items ARE the
    question. Checking their stems rejected every two-sided question ever generated.

    Words of five letters or more, because "the", "which" and "between" appear in
    every passage ever written and would make this pass unconditionally.
    """
    parts = [fields["stem"]]
    if fields["type"] in (QuestionType.MATCH, QuestionType.SEQUENCE):
        parts += [
            item.get("text", "")
            for item in (fields.get("options") or []) + (fields.get("prompt_items") or [])
        ]

    words = set(re.findall(r"[a-z]{5,}", " ".join(parts).lower()))
    if not words:
        return True
    return bool(words & set(re.findall(r"[a-z]{5,}", source.lower())))


class _StemDeduper:
    """Drops near-duplicate stems as they arrive. Models re-ask the same question.

    Incremental rather than a single pass at the end, and the timing is the point
    (D30): dedupe used to run after the last call, so duplicates counted toward the
    target while the run was paying for them — a real run spent 214 seconds on a
    backfill call whose four questions were three duplicates, stopped at "ten
    accepted", and still shipped a seven-question paper. Rejecting a duplicate the
    moment it arrives keeps the accepted count honest, so the backfill keeps working
    toward questions the paper will actually contain.

    An embeddings call per LLM call, which is milliseconds against minutes. A failure
    is not fatal: everything in the failed batch is kept, because an un-deduped paper
    is worse than a deduped one and much better than no paper.
    """

    def __init__(self, threshold: float) -> None:
        self._threshold = threshold
        self._kept: list[list[float]] = []

    async def keep(self, stems: list[str]) -> list[bool]:
        """One flag per stem: True to keep, False for a near-duplicate."""
        if not stems:
            return []
        try:
            vectors = await embeddings.embed_texts(stems)
        except Exception:
            logger.warning("dedupe skipped: embeddings unavailable", exc_info=True)
            return [True] * len(stems)

        flags: list[bool] = []
        for vector in vectors:
            novel = all(_cosine(vector, kept) <= self._threshold for kept in self._kept)
            if novel:
                self._kept.append(vector)
            flags.append(novel)
        return flags


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


class _CallBudget:
    """How many LLM calls a paper may cost, and which passages the next one sees.

    Two jobs in one small object because they are the same decision. Handing out
    batches round-robin is what stops the backfill re-asking about the opening
    paragraph every time; counting the calls is what stops it asking for ever.

    The cap matters more than it looks: on CPU-only Ollama a single call is a minute or
    two, so an uncapped backfill over a book the model cannot write from is a task that
    runs for an hour and is indistinguishable from one that hung.
    """

    def __init__(self, limit: int, batches: list[list[Chunk]]) -> None:
        self._left = limit
        self._batches = batches
        self._next = 0

    def spent(self) -> bool:
        return self._left <= 0 or not self._batches

    def take(self) -> list[Chunk] | None:
        """The next batch of passages, or None once the budget is gone."""
        if self.spent():
            return None
        self._left -= 1
        batch = self._batches[self._next % len(self._batches)]
        self._next += 1
        return batch


def _shortfall_note(
    *, target: int, produced: dict[QuestionFormat, int], final: int
) -> str | None:
    """What to tell the author when the paper came back short. None when it did not.

    The question this answers is "why did I only get one question?", and before this
    existed nothing answered it: the paper arrived with `error` null, looking exactly
    like a one-question paper somebody meant to write.

    It names the formats that produced nothing, because that is the actionable half —
    a book about one person's life supports multiple choice and short answers and does
    not support a four-item ordering, and the fix is for the author to stop asking for
    one rather than to try again and hope.
    """
    if final >= target:
        return None

    barren = [formats.SPECS[fmt].label for fmt, count in produced.items() if count == 0]
    note = f"You asked for {target} questions and this paper has {final}."
    if barren:
        note += (
            " These formats could not be written from the material you chose: "
            + ", ".join(barren)
            + "."
        )
    note += (
        " A thin book supports fewer questions than a long one, and asking for a format "
        "the material cannot support returns nothing rather than something invented. "
        "Try fewer question types, fewer questions, or a book with more in it."
    )
    return note


async def generate_questions(
    session: AsyncSession, assessment: Assessment
) -> list[Question]:
    """Draw the author's chunks, ask the model format by format, validate, dedupe,
    persist.

    Idempotent: prior questions are deleted in the same transaction that writes
    their replacements, because at-least-once delivery means this may run twice and
    a paper with two draftings of itself is worse than a paper that was regenerated.
    The delete sits at the persist step rather than up front so the mid-run trace
    checkpoints below never commit it early — a re-run that fails leaves the old
    paper standing instead of an empty one.

    **The run is watchable while it happens.** After every stage and every LLM
    call, the trace-so-far is committed onto the row (``checkpoint`` below) with
    ``finished_at`` null, and the author's Advanced panel polls it. On a CPU model
    a call is a minute or two, so without this the row sits unchanged for ten
    minutes and "is it working" has no answer.

    **One call per (format, batch of passages).** Batching by passage is what lets the
    model see it has already asked about photosynthesis; batching by format is what
    stops a 3B model local to a laptop from having to hold three JSON shapes in its
    head at once. It also bounds the blast radius: a batch that will not parse costs
    one format's worth of questions, not the paper's.

    One failed batch must not fail the paper. A batch that will not parse or will not
    validate is logged and skipped, and the remaining batches still produce questions.
    """
    selection = assessment.source_selection or {}
    book_ids = [UUID(value) for value in selection.get("book_ids", [])]
    chapter_ids = [UUID(value) for value in selection.get("chapter_ids", [])]

    spec = assessment.generation_spec or {}
    # What was asked for, which `question_count` stops being the moment this task
    # finishes and writes the actual count back onto it. A re-run must aim at the
    # original request, not at what the previous run happened to manage.
    target = int(spec.get("count") or assessment.question_count)

    # The pipeline trace: every stage below records what it did and how long it took,
    # and the payload lands on the row for the Advanced panel. Content-free by the
    # recorder's own API — counts and formats, never question text — because a sitter
    # can read this row and RLS cannot hide a column.
    tracer = GenerationTrace(
        model=settings.llm_model,
        budget=settings.assessment_max_llm_calls,
        target=target,
    )

    async def checkpoint() -> None:
        """Land the trace-so-far on the row, so the author can watch the run live.

        A mid-run commit is safe and cheap here: the worker session does not
        expire objects on commit, this task holds the only writes to the row
        while it is `generating`, and nothing else is pending on the session —
        the delete-and-replace of questions happens together at the persist step.
        An LLM call costs minutes; a commit costs milliseconds.
        """
        assessment.generation_trace = tracer.snapshot()
        await session.commit()

    # The pool is scoped to the AUTHOR: canon plus their own uploads, never anyone
    # else's personal book (D29). This runs in the worker as the service role with
    # no RLS behind it, which is exactly why the predicate is built explicitly here.
    author = Principal(id=assessment.author_id, role=Role.USER, email="")
    pool = await fetch_generation_chunks(
        session,
        author,
        book_ids=book_ids,
        chapter_ids=chapter_ids or None,
        min_tokens=settings.assessment_min_chunk_tokens,
    )
    tracer.step(
        "pool",
        f"{len(pool)} usable chunks from {len(book_ids)} book(s)"
        + (f", {len(chapter_ids)} chapter(s)" if chapter_ids else ""),
    )
    if not pool:
        # The trace rides the exception: the session this ran in is about to be
        # rolled back, and a failed run is exactly the one worth reading.
        raise attach_trace(
            ValidationFailed(
                "Those books have no readable material yet. Wait for them to finish "
                "processing, then try again."
            ),
            tracer.finish(final=0),
        )
    # The spec was written by `create_draft`, but the row is old data by the time the
    # worker reads it: a format could have been retired between the two. Unknown values
    # are dropped, and dropping all of them falls back to auto rather than to nothing.
    chosen_formats = formats.resolve_formats(
        [
            found.format
            for found in (formats.spec_for(value) for value in spec.get("formats", []))
            if found is not None
        ],
        assessment_type=assessment.type,
    )
    chosen_levels = formats.resolve_levels(
        [level for level in (_difficulty(v) for v in spec.get("levels", [])) if level]
    )
    instructions = spec.get("instructions")

    sampled = select_chunks_by_coverage(
        pool, wanted=max(target, int(target * settings.assessment_oversample))
    )
    chunk_text = {str(chunk.id): chunk.text for chunk in sampled}

    # How many of each format to ask for. Round-robin, so a five-question paper from
    # three formats gets all three rather than five of whichever came first.
    plan = formats.plan_mix(chosen_formats, chosen_levels, count=target)
    per_format: dict[QuestionFormat, int] = {}
    for fmt, _level in plan:
        per_format[fmt] = per_format.get(fmt, 0) + 1

    batch_size = settings.assessment_batch_chunks
    batches = [sampled[i : i + batch_size] for i in range(0, len(sampled), batch_size)]
    tracer.step(
        "sample",
        f"{len(sampled)} of {len(pool)} chunks, spread for coverage, "
        f"{len(batches)} batch(es)",
    )
    tracer.step(
        "plan",
        f"{target} questions · "
        + ", ".join(f"{fmt.value} ×{count}" for fmt, count in per_format.items())
        + " · levels "
        + ", ".join(level.value for level in chosen_levels),
    )
    # First checkpoint: pool, sample and plan land together, so the panel has a
    # timeline before the first (minutes-long) model call even starts.
    await checkpoint()
    accepted: list[tuple[GeneratedQuestion, QuestionFormat]] = []
    rejected = 0
    duplicates = 0
    produced: dict[QuestionFormat, int] = dict.fromkeys(per_format, 0)
    deduper = _StemDeduper(settings.assessment_dedupe_similarity)

    # A hard ceiling on LLM calls. Every one costs a minute or two on CPU-only Ollama,
    # and the backfill below would otherwise let a thin book and a stubborn model spin
    # until the task looks hung.
    budget = _CallBudget(settings.assessment_max_llm_calls, batches)

    async def ask(fmt: QuestionFormat, *, wanted: int) -> int:
        """One call: ask `fmt` for `wanted` questions from the next batch of passages.

        Returns how many were accepted. A batch that will not parse or will not
        validate is logged and skipped — one bad batch must never fail the paper.
        """
        nonlocal rejected, duplicates
        batch = budget.take()
        if batch is None:
            return 0
        allowed = {str(chunk.id) for chunk in batch}
        passages = [(str(chunk.id), chunk.text) for chunk in batch]
        # Three ceilings on the ask, and the reply-budget one is what keeps the call
        # alive: a reply that hits `max_tokens` mid-array is un-parseable JSON, and
        # the whole call — minutes of decode — is thrown away with it. Ask for what
        # fits and let the backfill make up the rest with another, smaller call.
        asked = min(
            len(batch),
            wanted + 1,
            formats.batch_ask_cap(
                fmt, max_reply_tokens=settings.assessment_reply_max_tokens
            ),
        )
        call_started = time.monotonic()

        try:
            raw = await llm.complete(
                prompts.generation_prompt(
                    passages,
                    fmt=fmt,
                    levels=chosen_levels,
                    wanted=asked,
                    rigor=assessment.rigor,
                    instructions=instructions,
                    # What the paper already asks. Steering the model away from a
                    # duplicate costs a few prompt tokens; generating one and
                    # rejecting it costs a question's worth of decode.
                    avoid_stems=[item.stem for item, _ in accepted],
                ),
                # The three wall-clock rules for a CPU model, in one call: a hard
                # reply ceiling (a runaway reply costs minutes writing output the
                # validator rejects), a longer timeout (one honest attempt with room
                # to finish), and NO SDK retries — the backfill pass is the retry
                # policy, and retrying a timeout on a saturated model just queues
                # another timeout behind it.
                max_tokens=settings.assessment_reply_max_tokens,
                request_timeout=settings.assessment_llm_timeout_seconds,
                retries=0,
            )
            items = parse_generated(raw)
        except Exception as exc:
            # Usually a timeout on a slow local model. Counted as a rejection for this
            # format so the note can say the format produced nothing, rather than
            # vanishing into a log line the author never sees.
            # The format and the reason go in the MESSAGE, not only in `extra`: the
            # Celery worker uses its own formatter and drops structured extras, so
            # "generation batch failed" with the detail in `extra` is a line that
            # tells nobody which format failed or why. CLAUDE.md flags this trap for
            # the completion line; it applies just as hard to the failure line, which
            # is the one somebody is reading when a paper comes back short.
            logger.warning(
                "generation batch failed (%s): %s: %s",
                fmt.value,
                type(exc).__name__,
                exc,
            )
            # The class name only, never the message: a provider error string can
            # quote the prompt, and the prompt quotes the book.
            tracer.call(
                fmt,
                ms=int((time.monotonic() - call_started) * 1000),
                wanted=asked,
                returned=0,
                accepted=0,
                reasons=[],
                failure=type(exc).__name__,
            )
            await checkpoint()
            return 0

        got = 0
        reject_reasons: list[str] = []
        # A runaway reply — eighteen items when two were asked for — is a model that
        # has lost the plot, and every one of them will be rejected for the same
        # reason. Cap the inspection so the log stays readable.
        fresh: list[GeneratedQuestion] = []
        for item in items[: max(wanted * 3, 8)]:
            if len(fresh) >= wanted:
                break
            ok, reason = validate_generated(
                item, allowed_chunk_ids=allowed, chunk_text=chunk_text, expected_format=fmt
            )
            if ok:
                fresh.append(item)
            else:
                rejected += 1
                reject_reasons.append(reason)
                questions_rejected_total.labels(reason).inc()
                logger.info("question rejected (%s): %s", fmt.value, reason)

        # Near-duplicates are rejected HERE, not in a pass at the end of the run, so
        # the accepted count the backfill steers by is the count the paper will keep
        # — dedupe-at-the-end let a run stop at "ten accepted" and ship seven (D30).
        for item, novel in zip(
            fresh, await deduper.keep([item.stem for item in fresh]), strict=True
        ):
            if novel:
                accepted.append((item, fmt))
                got += 1
            else:
                rejected += 1
                duplicates += 1
                reason = "near-duplicate of an accepted question"
                reject_reasons.append(reason)
                questions_rejected_total.labels(reason).inc()
                logger.info("question rejected (%s): %s", fmt.value, reason)
        produced[fmt] = produced.get(fmt, 0) + got
        tracer.call(
            fmt,
            ms=int((time.monotonic() - call_started) * 1000),
            wanted=asked,
            returned=len(items),
            accepted=got,
            reasons=reject_reasons,
        )
        await checkpoint()
        return got

    # --- pass one: every format gets its share ------------------------------
    #
    # One call per format per batch. A format is given at most as many attempts as
    # there are batches, so a book with one batch of passages gets one attempt at each
    # — which is the case that made this whole pass insufficient on its own.
    for fmt, wanted_total in per_format.items():
        remaining = wanted_total
        for _ in range(len(batches)):
            if remaining <= 0 or budget.spent():
                break
            remaining -= await ask(fmt, wanted=remaining)

    # --- pass two: make up the shortfall from whatever is actually working ---
    #
    # **This is the part that was missing.** Before it, a format that produced nothing
    # simply forfeited its share of the paper: ask for ten questions across seven
    # formats, have five of them fail on a small local model, and get a three-question
    # paper with no explanation. The formats a model cannot write from this material
    # are not knowable in advance — they depend on the book, the model and the chunk —
    # so the only honest answer is to find out and then ask the ones that work for more.
    working = [fmt for fmt, count in produced.items() if count > 0]
    if len(accepted) < target and working and not budget.spent():
        tracer.step(
            "backfill",
            f"{target - len(accepted)} short after the first pass · retrying "
            + ", ".join(fmt.value for fmt in working),
        )
        await checkpoint()
    while len(accepted) < target and working and not budget.spent():
        before = len(accepted)
        for fmt in working:
            if len(accepted) >= target or budget.spent():
                break
            await ask(fmt, wanted=target - len(accepted))
        if len(accepted) == before:
            # A full round over every working format produced nothing new. Another
            # round will not either, and each one costs minutes.
            break

    if not accepted:
        raise attach_trace(
            ValidationFailed(
                "The model could not write usable questions from that material in the "
                "formats you chose. Try different chapters, fewer questions, or a "
                "simpler format such as multiple choice."
            ),
            tracer.finish(final=0),
        )

    # Deduped as it was accepted, so `accepted` is already unique — the trace still
    # records the totals so "how much did the model repeat itself" stays answerable.
    final = accepted[:target]
    tracer.dedupe(len(accepted) + duplicates, len(accepted))

    # Say so when the paper is short. Written onto the row rather than only logged: the
    # author is the only person who can act on it, and they are not reading the worker's
    # stdout.
    assessment.generation_note = _shortfall_note(
        target=target, produced=produced, final=len(final)
    )

    # The counts go in the message, not only in `extra`: the Celery worker uses its
    # own formatter and drops structured extras, so an extras-only line reads as
    # "generation complete" and tells nobody whether the validator did anything.
    logger.info(
        "generation complete: %d accepted, %d rejected, %d deduped, %d final (%s)",
        len(accepted),
        rejected,
        duplicates,
        len(final),
        assessment.id,
    )

    questions_generated_total.inc(len(final))
    questions = []
    for index, (item, fmt) in enumerate(final):
        built = _to_question(assessment.id, index, item, fmt)
        if built is not None:
            questions.append(built)
    tracer.step("persist", f"{len(questions)} questions written")
    assessment.generation_trace = tracer.finish(final=len(questions))
    # Delete-and-replace together, in the transaction the caller commits: a re-run
    # that dies before this line leaves the previous paper standing, and the
    # checkpoints above never commit a half-swapped one.
    await session.execute(delete(Question).where(Question.assessment_id == assessment.id))
    session.add_all(questions)
    await session.flush()
    return questions


def _to_question(
    assessment_id: UUID, index: int, item: GeneratedQuestion, fmt: QuestionFormat
) -> Question | None:
    """Build the row. Returns None if the shape no longer holds.

    It was already validated, so None here means a bug rather than a bad model — but
    raising would take the whole paper down at the last step, after every LLM call has
    been paid for. Log it and drop the one question.
    """
    try:
        fields = build_question_fields(
            fmt,
            stem=item.stem,
            options=item.options,
            prompt_items=item.prompt_items,
            correct_option=item.correct_option,
            correct_options=item.correct_options,
            accepted=item.accepted,
            tolerance=item.tolerance,
            pairs=item.pairs,
            order=item.order,
            model_answer=item.model_answer,
            rubric=item.rubric,
        )
    except ValidationFailed:
        logger.exception("validated question failed to build", extra={"format": fmt.value})
        return None

    return Question(
        assessment_id=assessment_id,
        index=index,
        difficulty=_difficulty(item.difficulty),
        source_chunk_ids=[item.source_chunk_id] if item.source_chunk_id else [],
        origin=QuestionOrigin.GENERATED,
        **fields,
    )


def _difficulty(value: str | None) -> Difficulty | None:
    try:
        return Difficulty(value) if value else None
    except ValueError:
        return None
