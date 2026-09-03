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
from dataclasses import dataclass
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
    AssessmentType,
    BookScope,
    Difficulty,
    QuestionFormat,
    QuestionOrigin,
    QuestionType,
    Role,
)
from app.rag import brief as brief_reader
from app.rag import formats, harvest, prompts
from app.rag.retrieve import fetch_generation_chunks, fetch_topic_chunks
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

# Canonical keys, assigned by us and never by the model. Eight rather than five: the
# bound belongs to the key alphabet, and a spec's max_options is what actually limits a
# question, so widening one later does not mean remembering to widen the other.
_OPTION_KEYS = "ABCDEFGH"


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

    # Recorded on the row rather than assumed at generation time, so the paper says what
    # it will be written as while it is still a draft.
    chosen_formats = formats.resolve_formats()
    chosen_levels = formats.resolve_levels(data.levels)

    assessment = Assessment(
        author_id=principal.id,
        title=data.title.strip(),
        # Not trusted from the client, and no longer derived either: every paper is
        # multiple choice since D32. The column keeps its wider enum because older rows
        # legitimately record a paper that WAS mixed, and rewriting that to tidy the
        # type would be rewriting history.
        type=AssessmentType.MCQ,
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
            # Whether the author named the levels or skipped them. Worth keeping: "the
            # server chose this spread" and "the author chose this spread" are different
            # answers to the same complaint about a paper.
            "auto": not data.levels,
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

    Ours, never the model's. A model that returns options keyed "1"/"2", or keyed
    "B"/"A" in that order, must not be able to decide what a stored answer means.
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
    correct_option: str | None = None,
    points: Decimal | None = None,
) -> dict[str, Any]:
    """Canonicalise one question into columns, or raise ``ValidationFailed`` saying why.

    The returned dict is exactly the column set: the columns this format does not use
    are explicitly ``None`` rather than left off, so a question edited in place cannot
    keep a stale `answer_key` or `model_answer` written before D32.
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

    # ------------------------------------------------------------- the answer
    #
    # One family, so this is straight-line rather than a dispatch. The dict above still
    # sets every unused column to None explicitly: a question edited in place must not
    # keep a stale `answer_key` or `model_answer` from a row written before D32, because
    # a leftover key outranks nothing and confuses everything that reads it.
    key = _remapped(correct_option, remap)
    if key is None:
        raise ValidationFailed("The correct answer must be one of the options.")
    fields["correct_option"] = key

    return fields


def _remapped(key: str | None, remap: dict[str, str]) -> str | None:
    """Translate one of the model's keys into ours, or None if it names nothing."""
    if key is None:
        return None
    return remap.get(str(key).strip().upper())


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
        correct_option=data.correct_option,
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
        if q.type is QuestionType.MCQ and not q.correct_option
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

    if question.type is QuestionType.MCQ and question.correct_option:
        return [f"**{question.correct_option}.** {options.get(question.correct_option, '')}"]

    # A question with no correct_option cannot be published (`publish` refuses it), so
    # this is reachable only for a draft the author has not finished.
    return []


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
    else:
        # Ruled space, so a printed draft has somewhere to write. A line of underscores
        # is a thematic break in Markdown, which renders as exactly the ruled line we
        # want — and stays a visible line of underscores in a plain-text viewer, so it
        # reads correctly either way. Only an unfinished draft reaches this now: a
        # published mcq always has its options.
        lines += ["_" * 60 for _ in range(4)] + [""]

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


def _unfenced(raw: str) -> str:
    """The reply with its markdown fence and its leading apology stripped."""
    text = raw.strip()
    if "```" in text:
        blocks = re.findall(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if blocks:
            text = blocks[0].strip()
    return text


def _whole_reply(text: str) -> list[Any] | None:
    """The happy path: the entire reply is one JSON object with a `questions` list.

    Returns None — not an error — when the reply will not decode, so the caller can
    fall back to salvaging. A bare array is accepted too: a model told to return
    ``{"questions": [...]}`` sometimes returns the list on its own, and refusing a
    complete answer over its outermost bracket costs a whole call.
    """
    start, end = text.find("{"), text.rfind("}")
    array_start, array_end = text.find("["), text.rfind("]")
    for opener, closer in ((start, end), (array_start, array_end)):
        if opener == -1 or closer <= opener:
            continue
        try:
            payload = json.loads(text[opener : closer + 1])
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            items = payload.get("questions")
            if isinstance(items, list):
                return items
            if isinstance(items, dict):
                # One question, not wrapped in a list. Same object, one bracket short.
                return [items]
    return None


def _salvage_objects(text: str) -> list[Any]:
    """Every top-level JSON object in a reply, complete ones only.

    The decoder walk that :func:`_salvage` and :func:`parse_harvested` both need. It
    keeps whatever decoded and steps over whatever did not; it never edits a byte.
    """
    decoder = json.JSONDecoder()
    found: list[Any] = []
    index = text.find("{")
    while index != -1:
        try:
            value, end = decoder.raw_decode(text, index)
        except (json.JSONDecodeError, ValueError):
            # This object is the broken one. Step past its brace and try the next.
            index = text.find("{", index + 1)
            continue
        if isinstance(value, dict):
            found.append(value)
        index = text.find("{", end)
    return found


def _salvage(text: str) -> list[Any]:
    """Every *complete* question object in a reply that would not decode as a whole.

    This is not repair, and the distinction is the whole point. Nothing here edits a
    byte of the model's output: it walks the reply with a real JSON decoder, keeps the
    objects that decode cleanly and carry a ``stem``, and drops the one that broke
    along with everything the break swallowed. Each survivor then goes through exactly
    the same validation as one from a reply that parsed first time.

    The failure it answers is measured, not hypothetical. On llama3.2:3b, five
    generation calls in eight came back as un-parseable JSON — an unterminated string
    in the fourth question, a key without quotes in the third — and an all-or-nothing
    parser threw away the three good questions in front of the break along with one
    to two minutes of CPU decode. `LLM_JSON_MODE` stops most of these happening at
    all; this is what happens to the rest.
    """
    found: list[Any] = []
    for value in _salvage_objects(text):
        if isinstance(value.get("questions"), list):
            # The wrapper decoded after all. Its contents are the answer.
            found.extend(item for item in value["questions"] if isinstance(item, dict))
        elif value.get("stem"):
            found.append(value)
    return found


def parse_generated(raw: str) -> list[GeneratedQuestion]:
    """Parse the model's reply. Never regex a response into shape.

    Tolerates a markdown fence and leading prose because small local models add them
    despite being told not to. Beyond that it takes the reply apart with a JSON
    decoder rather than a regex — see :func:`_salvage` — so a batch whose fourth
    question broke still yields its first three, and a reply with nothing complete in
    it still raises. A repaired response would be a response nobody validated; a
    *partially recovered* one is a smaller response validated in full.

    An item that will not even fit :class:`GeneratedQuestion` is dropped rather than
    taken as proof the batch failed: the shape rules that matter are enforced by
    ``validate_generated``, and one malformed sibling should not cost the others.
    """
    text = _unfenced(raw)
    raw_items = _whole_reply(text)
    if raw_items is None:
        raw_items = _salvage(text)
    if not raw_items:
        raise ValueError("no usable question object in response")

    items: list[GeneratedQuestion] = []
    for item in raw_items:
        try:
            items.append(GeneratedQuestion.model_validate(item))
        except Exception:  # pydantic ValidationError, or not a mapping at all
            logger.info("generated item dropped: does not fit the reply contract")
    if not items:
        raise ValueError("no question in the response fits the reply contract")
    return items


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
            correct_option=item.correct_option,
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
        # Before rejecting: is it grounded in a DIFFERENT passage from this same
        # batch? A batch is five passages in one prompt and a small model routinely
        # writes a good question about the third while copying the id of the first.
        # Rejecting that throws away a sound question over a clerical error, and
        # taking it as cited would file it under a passage it is not about — so
        # re-attribute it to the passage it actually shares vocabulary with, and only
        # then is the provenance true. Nothing widens here: every candidate is already
        # in `allowed_chunk_ids`, which is this batch and nothing else.
        rehomed = next(
            (
                chunk_id
                for chunk_id in allowed_chunk_ids
                if chunk_id != item.source_chunk_id
                and _shares_vocabulary(fields, chunk_text.get(chunk_id, ""))
            ),
            None,
        )
        if rehomed is None:
            return False, "stem shares no vocabulary with its source passage"
        item.source_chunk_id = rehomed

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
    passage = set(re.findall(r"[a-z]{5,}", source.lower()))
    if not passage:
        return False

    parts = [fields["stem"]]

    words = set(re.findall(r"[a-z]{5,}", " ".join(parts).lower()))
    if not words:
        # Nothing distinctive to check. Fall through to the answer rather than
        # passing unconditionally — a flashcard front is one word, and one word of
        # four letters used to make this check a no-op.
        return _answer_shares_vocabulary(fields, passage)
    if words & passage:
        return True

    # The stem missed. Try the ANSWER before giving up: a numeric question reads
    # "How many pairs of chromosomes does a human cell carry?" over a passage that
    # says "23 pairs", and a true/false stem can be a short paraphrase whose only
    # long words are the ones the passage happens to spell differently. Two shared
    # words rather than one, because the answer text is where boilerplate lives.
    return _answer_shares_vocabulary(fields, passage, needed=2)


def _answer_shares_vocabulary(
    fields: dict[str, Any], passage: set[str], *, needed: int = 1
) -> bool:
    """Does the ANSWER come from the passage, when the stem did not say so?

    The weaker half of the check and deliberately second: an answer lifted verbatim
    from the passage proves the question was written from it, but options can be
    copied out of a passage the stem has nothing to do with — which is why the stem
    is tried first and this needs more than one word to agree.
    """
    answers: list[str] = []
    for option in (fields.get("options") or []) + (fields.get("prompt_items") or []):
        answers.append(option.get("text", ""))
    key = fields.get("answer_key") or {}
    answers += [str(value) for value in (key.get("accepted") or [])]
    answers.append(fields.get("model_answer") or "")
    for criterion in fields.get("rubric") or []:
        answers.append(str(criterion.get("criterion", "")))

    words = set(re.findall(r"[a-z]{5,}", " ".join(answers).lower()))
    return len(words & passage) >= needed


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
        return self.exhausted() or not self._batches

    def exhausted(self) -> bool:
        """Whether the CALL budget is gone, regardless of passages left to show.

        Distinct from :meth:`spent` because harvesting brings its own passages — it
        works from the chunks that carry the book's questions, not from the
        round-robin — and shares this counter rather than getting a second one. One
        wall-clock budget for the paper, however the paper is being filled.
        """
        return self._left <= 0

    def spend(self) -> bool:
        """Take one call for work that supplies its own passages. True if granted."""
        if self.exhausted():
            return False
        self._left -= 1
        return True

    def take(self) -> list[Chunk] | None:
        """The next batch of passages, or None once the budget is gone."""
        if self.spent():
            return None
        self._left -= 1
        batch = self._batches[self._next % len(self._batches)]
        self._next += 1
        return batch


# ------------------------------------------------------- the book's own questions
#
# Taking a question the book already asks, rather than writing one about the same
# passage (DECISIONS.md D31). Detection is deterministic and lives in `rag/harvest.py`;
# what is here is the part that needs the database and the model — which chunks to draw
# from, what context to give, and how to check that what came back is still the book's
# question rather than a rewrite of it.


@dataclass(frozen=True)
class HarvestBatch:
    """One harvesting call: questions the book asks, and the passages that answer them.

    The two are separate because they usually are in the book. A textbook prints its
    exercises at the end of a chapter and the answers throughout it, so the questions
    come from one chunk and the passages that settle them from the several before it.
    """

    questions: list[harvest.BookQuestion]
    passages: list[Chunk]
    # The chunk the questions were printed in. Provenance for every question in this
    # batch: it is where they can be found again, and it is re-checked before storing.
    source: Chunk


def _harvest_context(pool: list[Chunk], source: Chunk, *, span: int) -> list[Chunk]:
    """The exercise chunk plus the passages a reader would have just read.

    Preceding chunks of the same book, in reading order, because that is where the
    answers are: an exercise at the end of a chapter is answered by the chapter. The
    exercise chunk itself comes last, so the model reads the material and then the
    questions — the same ordering as generation, for the same measured reason (D30).
    """
    before = [
        chunk
        for chunk in pool
        if chunk.book_id == source.book_id
        and chunk.index < source.index
        and chunk.index >= source.index - span
    ]
    return sorted(before, key=lambda chunk: chunk.index) + [source]


def plan_harvest(
    pool: list[Chunk], *, minimum: int, per_call: int, span: int
) -> list[HarvestBatch]:
    """Every batch of book questions this material can offer, in reading order.

    One batch per exercise chunk rather than one per question: the questions in an
    exercise set share a subject and their answers share passages, so asking about six
    of them together costs one call instead of six. ``per_call`` bounds that, because
    a reply that runs past the token ceiling is a truncated JSON that costs the whole
    call — the same ceiling arithmetic generation does (D30).
    """
    batches: list[HarvestBatch] = []
    for chunk in pool:
        found = harvest.find_questions(chunk.text)
        if not found or not harvest.carries_questions(chunk.text, minimum=minimum):
            continue
        for start in range(0, len(found), per_call):
            batches.append(
                HarvestBatch(
                    questions=found[start : start + per_call],
                    passages=_harvest_context(pool, chunk, span=span),
                    source=chunk,
                )
            )
    return batches


# What a harvesting reply is allowed to contribute. Everything else on the question —
# the stem, the format, the provenance — is OURS, taken from the book and from the
# batch, never from the reply. A model that returns a "stem" field has it ignored
# rather than honoured, which is what makes "harvested" a checkable claim instead of
# a hopeful one.
_HARVEST_ANSWER_FIELDS = frozenset(
    {
        "options",
        "correct_option",
        "correct_options",
        "prompt_items",
        "pairs",
        "order",
        "accepted",
        "tolerance",
        "model_answer",
        "rubric",
        "difficulty",
    }
)


def parse_harvested(raw: str) -> list[dict[str, Any]]:
    """Parse a harvesting reply into answers keyed by the number they were asked under.

    Same discipline as :func:`parse_generated` and for the same reason: recover the
    entries that decoded, drop the one that broke, never repair. An entry with no
    usable ``n`` is dropped here — it cannot be joined back to a question, and a
    harvested answer with no question is not something to guess at.
    """
    text = _unfenced(raw)
    payload: Any = None
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            payload = json.loads(text[start : end + 1])
        except (json.JSONDecodeError, ValueError):
            payload = None

    items: list[Any]
    if isinstance(payload, dict) and isinstance(payload.get("answers"), list):
        items = payload["answers"]
    elif isinstance(payload, list):
        items = payload
    else:
        items = [
            item
            for item in _salvage_objects(text)
            if isinstance(item, dict) and "n" in item
        ]

    answers: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            number = int(item["n"])
        except (KeyError, TypeError, ValueError):
            continue
        answers.append({**item, "n": number})
    if not answers:
        raise ValueError("no usable answer object in response")
    return answers


# A question that points at something the sitter cannot see. Harvested questions need
# this and generated ones do not: the model is told not to reference a figure, whereas
# the book is under no such obligation and its exercises reference figures constantly
# ("In Fig. 6.17, (i) and (ii), DE || BC. Find EC"). Reproducing one of those on a
# paper asks somebody to read a diagram that was never sent to them.
_NEEDS_WHAT_IS_NOT_THERE = re.compile(
    r"\b(?:fig(?:ure)?\.?\s*\d|table\s*\d|graph\s*\d|diagram|photograph|"
    r"the\s+(?:figure|diagram|graph|picture|image|adjoining|alongside)|"
    r"given\s+(?:figure|diagram|below)|shown\s+(?:in|above|below)|"
    r"above|below|following\s+(?:figure|diagram|table))\b",
    re.IGNORECASE,
)


def usable_book_question(question: harvest.BookQuestion, source_text: str) -> tuple[bool, str]:
    """Is this a question that can honestly be put on a paper? ``(ok, reason)``.

    Two checks, and the first is the one that makes harvesting trustworthy at all:
    **the question must still be findable in the passage it is claimed to come from.**
    Not similar to it, not about it — in it. That is a far stronger guarantee than
    anything available for a generated question, and it is what stops "harvested from
    the book" becoming a label on something the book never said.
    """
    if not _appears_in(question.text, source_text):
        return False, "not found verbatim in its source passage"
    if _NEEDS_WHAT_IS_NOT_THERE.search(question.text):
        return False, "refers to a figure or diagram the sitter cannot see"
    if _SELF_REFERENCE.search(question.text):
        return False, "refers to the passage"
    return True, ""


def _appears_in(needle: str, haystack: str) -> bool:
    """Is this question printed in this passage, allowing for the PDF's whitespace?

    Whitespace-insensitive and case-insensitive, and nothing else. A PDF extractor
    breaks lines wherever the column ended, so the question we hold has had its
    whitespace collapsed and the chunk's has not — but every other character must
    still match, in order. Loosening this any further would defeat the point.
    """
    flat = re.sub(r"\s+", " ", haystack).casefold()
    return re.sub(r"\s+", " ", needle).casefold() in flat


def _harvest_target(
    preference: brief_reader.BookQuestions, *, target: int, available: int
) -> int:
    """How many of the paper's questions should come from the book itself.

    A ceiling, never a quota. Every number here is capped by ``available`` — what the
    material was actually found to contain — so a brief that asks for the book's
    questions from a book that has none produces a written paper rather than a failure.
    The author is told which happened in the trace either way.

    ``AUTO`` is the interesting case: the author said nothing, so a *share* of the
    paper is harvested where the material supports it and the rest is written. Not all
    of it, even when the book could fill it — a paper that is entirely the
    back-of-chapter exercises is a photocopy, and the author asked for an assessment.
    """
    if available <= 0 or preference is brief_reader.BookQuestions.NEVER:
        return 0
    if preference is brief_reader.BookQuestions.ONLY:
        return min(target, available)
    if preference is brief_reader.BookQuestions.PREFER:
        # They asked for them. Take as many as the material offers, still capped by
        # the size of the paper.
        return min(target, available)
    share = max(0.0, min(1.0, settings.assessment_book_question_share))
    return min(int(target * share), available)


async def _with_topics(
    session: AsyncSession,
    author: Principal,
    topics: list[str],
    sampled: list[Chunk],
    *,
    book_ids: list[UUID],
    chapter_ids: list[UUID],
    tracer: GenerationTrace,
) -> list[Chunk]:
    """Put the passages nearest the author's topics at the front of the sample.

    Failure here is not failure of the run. Embeddings being down means the paper is
    drawn from the coverage sample alone — which is what every paper before this layer
    existed was drawn from — and that is a far better outcome than refusing to write a
    paper because a brief mentioned a subject.
    """
    try:
        vectors = await embeddings.embed_texts(topics[:6])
        found = await fetch_topic_chunks(
            session,
            author,
            topic_embeddings=vectors,
            book_ids=book_ids,
            chapter_ids=chapter_ids or None,
            per_topic=settings.assessment_topic_chunks,
            min_tokens=settings.assessment_min_chunk_tokens,
        )
    except Exception:
        logger.warning("topic narrowing skipped", exc_info=True)
        tracer.step("topics", "could not be searched; drawing from the whole selection")
        return sampled

    if not found:
        tracer.step(
            "topics",
            "nothing found for "
            + ", ".join(topics[:3])
            + "; drawing from the whole selection",
        )
        return sampled

    seen = {chunk.id for chunk in found}
    merged = found + [chunk for chunk in sampled if chunk.id not in seen]
    tracer.step(
        "topics",
        f"{len(found)} passage(s) matched {len(topics)} topic(s), "
        f"sampled first out of {len(merged)}",
    )
    return merged


def _failure_tag(exc: BaseException) -> str:
    """A failed call, named well enough to act on and safely enough to store.

    The trace is content-free by contract (see `GenerationTrace.call`): a provider's
    error string can quote the request, and the request quotes the book, so the
    message itself must never land here. But `UpstreamUnavailable` on its own told an
    author nothing -- it reads identically whether the model server is overloaded, the
    request timed out, or the model was never pulled, and those need different actions
    from different people.

    So: the exception class, plus the HTTP status when the cause carries one. A status
    is a fixed vocabulary the provider chose, not text we passed it, so it discloses
    nothing about the material. `404` is the one worth having -- it means the model
    named in the config does not exist on the server.
    """
    name = type(exc).__name__
    status = getattr(exc.__cause__, "status_code", None)
    return f"{name} {status}" if status is not None else name


def _shortfall_note(
    *, target: int, produced: dict[QuestionFormat, int], final: int
) -> str | None:
    """What to tell the author when the paper came back short. None when it did not.

    The question this answers is "why did I only get one question?", and before this
    existed nothing answered it: the paper arrived with `error` null, looking exactly
    like a one-question paper somebody meant to write.

    It used to name the formats that produced nothing, which was the actionable half
    when there were fourteen of them. With one format (D32) that list would always read
    "Multiple choice" — naming the only thing the author could have asked for is not
    advice — so the barren case says the plainer true thing instead: nothing usable came
    back from this material at all.
    """
    if final >= target:
        return None

    note = f"You asked for {target} questions and this paper has {final}."
    if final == 0:
        note += (
            " No questions could be written from the material you chose. A passage has "
            "to state something definite for a question to have an answer."
        )
    note += (
        " A thin book supports fewer questions than a long one, and a passage the "
        "material does not settle returns nothing rather than something invented. "
        "Try fewer questions, or a book with more in it."
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
        model=settings.generation_model,
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

    # --- layer one: understand the brief ------------------------------------
    #
    # Before anything is fetched, because what the author asked for decides what to
    # fetch. "Focus on acids and bases" is a retrieval instruction; pasting it into a
    # prompt and sampling the whole book regardless is how it used to be honoured.
    instructions = spec.get("instructions")
    brief = await brief_reader.read(instructions)
    tracer.step(
        "brief",
        (
            "topics: " + ", ".join(brief.topics[:4])
            if brief.topics
            else ("no topics named" if brief.text else "none given")
        )
        + (f" · avoiding {', '.join(brief.avoid[:3])}" if brief.avoid else "")
        + f" · book questions: {brief.book_questions.value}",
    )

    # --- layer two: pull the context ----------------------------------------
    #
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
    # The spec written by `create_draft` may name formats that no longer exist -- it is
    # old data by the time the worker reads it, and D32 retired thirteen of them. So the
    # format is not read back from the spec at all: a paper queued before the collapse
    # still generates, as multiple choice. Only the levels survive the round trip.
    chosen_levels = formats.resolve_levels(
        [level for level in (_difficulty(v) for v in spec.get("levels", [])) if level]
    )

    # How wide to sample. Sized by the CALL BUDGET, not by the question count, and
    # that is the fix for the failure this comment used to describe wrongly: a
    # 461-chunk book asked for five questions sampled seven chunks — 1.5% of the book
    # — which is two batches, and the twelve-call budget then cycled those same two
    # batches twelve times. The model was shown the same passages over and over and
    # duly wrote the same questions, which the deduper rejected, which triggered more
    # backfill over the same passages. Give every call in the budget its own batch of
    # fresh material and the duplicates stop being generated rather than being caught.
    batch_size = settings.assessment_batch_chunks
    sampled = select_chunks_by_coverage(
        pool,
        wanted=max(
            target,
            int(target * settings.assessment_oversample),
            settings.assessment_max_llm_calls * batch_size,
        ),
    )

    # The topics the brief named, pulled in on top of the coverage sample rather than
    # instead of it. Blended, not substituted, and the failure that forces the blend is
    # this: a topic the book barely covers retrieves four thin chunks, and a paper
    # drawn from four chunks is four questions about one paragraph. Topical chunks go
    # to the FRONT of the sample, so the earliest batches — the ones the first pass
    # always reaches — are the on-topic ones, and the coverage sample carries the rest.
    if brief.topics:
        sampled = await _with_topics(
            session,
            author,
            brief.topics,
            sampled,
            book_ids=book_ids,
            chapter_ids=chapter_ids,
            tracer=tracer,
        )

    chunk_text = {str(chunk.id): chunk.text for chunk in sampled}

    # --- layer three: what the material actually holds ----------------------
    #
    # Whether this book carries questions of its own, measured rather than assumed.
    # A novel carries none; an NCERT science textbook carries seventy. The plan below
    # is built from the answer, so a paper never promises harvested questions that the
    # material cannot supply.
    harvest_batches = plan_harvest(
        pool,
        minimum=settings.assessment_min_harvest_questions,
        per_call=max(1, settings.assessment_reply_max_tokens // 160),
        span=settings.assessment_batch_chunks,
    )
    available = sum(len(batch.questions) for batch in harvest_batches)

    # --- layer four: plan generate vs. select -------------------------------
    #
    # How many questions at each level to ask for. Round-robin, so a five-question paper
    # across three levels gets all three rather than five of whichever came first.
    plan = formats.plan_mix(chosen_levels, count=target)
    per_format: dict[QuestionFormat, int] = {}
    for fmt, _level in plan:
        per_format[fmt] = per_format.get(fmt, 0) + 1

    harvest_target = _harvest_target(
        brief.book_questions, target=target, available=available
    )
    tracer.step(
        "book questions",
        f"{available} found in {len(harvest_batches)} passage(s) · "
        + (
            f"taking up to {harvest_target}"
            if harvest_target
            else "writing all questions instead"
        ),
    )

    # Strided, not sliced. `sampled` comes back grouped by chapter, so consecutive
    # slices make a batch that is five passages from one chapter — and a call shown
    # five passages about the same thing writes five questions about the same thing.
    # Striding gives every batch a cross-section of the whole book instead.
    batch_count = max(1, (len(sampled) + batch_size - 1) // batch_size)
    batches = [batch for i in range(batch_count) if (batch := sampled[i::batch_count])]
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
    # A format that produced nothing has told us one of two very different things,
    # and treating them alike is what turned a JSON glitch into an empty paper: a
    # format whose questions were *generated and rejected* cannot be written from
    # this material, while a format whose calls died before anything was validated
    # has told us nothing at all. Only the first is a reason to stop asking.
    reached_validation: dict[QuestionFormat, bool] = dict.fromkeys(per_format, False)
    deduper = _StemDeduper(settings.assessment_dedupe_similarity)
    # Which accepted stems came from the book, so the persist step can mark their
    # provenance. Stems rather than indices because the two passes append to one list
    # and the list is re-sorted by nothing — matching on the value is what survives
    # that, and a stem that is both harvested and written is the same question anyway.
    harvested_stems: set[str] = set()

    # Which formats the book's questions are asked in. Only the author's own choices,
    # because harvesting is a different SOURCE for a question, never a licence to put
    # a format on the paper that was not asked for. A book question that cannot be
    # honestly asked as any chosen format is dropped by the model saying so.
    # Every remaining format can be harvested: a multiple choice printed in a book is a
    # reproduction of that question, which is what the provenance label claims. The
    # two-sided formats that could not honestly claim it are gone (D32).
    harvest_formats = list(per_format)

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
                # Constrain the reply to JSON at the provider. The single biggest
                # source of lost calls was a reply that was nearly right — a key
                # without quotes in the third question, a string left open in the
                # fourth — and a minute of CPU decode died with it. See the client.
                json_object=True,
                model=settings.generation_model,
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
                failure=_failure_tag(exc),
            )
            await checkpoint()
            return 0

        # The call came back and its reply parsed: whatever happens below is a fact
        # about this format on this material, not about the transport.
        reached_validation[fmt] = True
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

    # --- pass zero: take the questions the book already asks -----------------
    #
    # First, and the ordering is the design (D31). These questions were written by
    # whoever wrote the book, they sit at the level the chapter is pitched at, and
    # somebody revising from that book has met them before — so they get first claim
    # on the paper, and the writing pass fills whatever they leave.
    #
    # The model is not asked for a question here. It is asked for the ANSWER to a
    # question we already hold verbatim, which is a far better-conditioned task for a
    # 3B model than inventing a stem, four distractors, a key and a provenance id at
    # once. The stem cannot drift, because the model never returns one.
    harvest_cursor = 0

    async def take_from_book(fmt: QuestionFormat, *, wanted: int) -> int:
        """One call: answer the next batch of the book's own questions as `fmt`.

        Returns how many were accepted. Like `ask`, a batch that will not parse or
        will not validate is logged and skipped — it costs its call and nothing more.
        """
        nonlocal rejected, duplicates, harvest_cursor

        # Find the next batch with something usable in it. Questions are checked
        # BEFORE the call is spent: a batch that is all figure references costs
        # nothing rather than a minute.
        batch: HarvestBatch | None = None
        usable: list[harvest.BookQuestion] = []
        skipped: list[str] = []
        while harvest_cursor < len(harvest_batches):
            candidate = harvest_batches[harvest_cursor]
            harvest_cursor += 1
            fit: list[harvest.BookQuestion] = []
            for question in candidate.questions:
                ok, reason = usable_book_question(question, candidate.source.text)
                if ok:
                    fit.append(question)
                else:
                    skipped.append(reason)
            if fit:
                batch, usable = candidate, fit
                break
        if batch is None or not budget.spend():
            return 0

        source_id = str(batch.source.id)
        numbered = list(enumerate(usable[:wanted], start=1))
        by_number = {number: question for number, question in numbered}
        call_started = time.monotonic()

        try:
            raw = await llm.complete(
                prompts.harvest_prompt(
                    [
                        (number, question.text, question.options)
                        for number, question in numbered
                    ],
                    [(str(chunk.id), chunk.text) for chunk in batch.passages],
                    fmt=fmt,
                    rigor=assessment.rigor,
                    instructions=instructions,
                ),
                max_tokens=settings.assessment_reply_max_tokens,
                request_timeout=settings.assessment_llm_timeout_seconds,
                retries=0,
                json_object=True,
                model=settings.generation_model,
            )
            answers = parse_harvested(raw)
        except Exception as exc:
            logger.warning(
                "harvest batch failed (%s): %s: %s", fmt.value, type(exc).__name__, exc
            )
            tracer.call(
                fmt,
                ms=int((time.monotonic() - call_started) * 1000),
                wanted=len(numbered),
                returned=0,
                accepted=0,
                reasons=[],
                failure=_failure_tag(exc),
                harvested=True,
            )
            await checkpoint()
            return 0

        got = 0
        reject_reasons = list(skipped)
        fresh: list[GeneratedQuestion] = []
        for answer in answers:
            asked = by_number.get(answer["n"])
            if asked is None:
                # An answer to a question we did not ask. There is no stem to attach
                # it to, and inventing one is exactly what this path exists to avoid.
                rejected += 1
                reject_reasons.append("answer to a question that was not asked")
                continue
            if not answer.get("answerable", True) or not answer.get("fits", True):
                # The model was given a way to say "the passages do not settle this"
                # and used it. That is the grounded-or-silent rule working, not a
                # failure — counted, but not as a rejection of the model's output.
                reject_reasons.append("the material does not settle this question")
                continue

            # Our stem, their answer. `format` and `source_chunk_id` are ours too:
            # the model is never asked for provenance it could get wrong.
            item = GeneratedQuestion.model_validate(
                {
                    key: value
                    for key, value in answer.items()
                    if key in _HARVEST_ANSWER_FIELDS
                }
                | {
                    "format": fmt.value,
                    "stem": asked.text,
                    "source_chunk_id": source_id,
                }
            )
            ok, reason = validate_generated(
                item,
                allowed_chunk_ids={source_id},
                chunk_text={source_id: batch.source.text},
                expected_format=fmt,
            )
            if not ok:
                rejected += 1
                reject_reasons.append(reason)
                questions_rejected_total.labels(reason).inc()
                logger.info("book question rejected (%s): %s", fmt.value, reason)
                continue
            fresh.append(item)

        for item, novel in zip(
            fresh, await deduper.keep([item.stem for item in fresh]), strict=True
        ):
            if novel:
                accepted.append((item, fmt))
                harvested_stems.add(item.stem)
                got += 1
            else:
                rejected += 1
                duplicates += 1
                reason = "near-duplicate of an accepted question"
                reject_reasons.append(reason)
                questions_rejected_total.labels(reason).inc()
        produced[fmt] = produced.get(fmt, 0) + got
        tracer.call(
            fmt,
            ms=int((time.monotonic() - call_started) * 1000),
            wanted=len(numbered),
            returned=len(answers),
            accepted=got,
            reasons=reject_reasons,
            harvested=True,
        )
        await checkpoint()
        return got

    if harvest_target > 0:
        tracer.step(
            "harvest",
            f"answering up to {harvest_target} question(s) the book asks, "
            f"across {len(harvest_formats)} format(s)",
        )
        await checkpoint()
        position = 0
        while (
            len(accepted) < harvest_target
            and harvest_cursor < len(harvest_batches)
            and not budget.exhausted()
        ):
            fmt = harvest_formats[position % len(harvest_formats)]
            position += 1
            before = len(accepted)
            await take_from_book(fmt, wanted=harvest_target - len(accepted))
            if len(accepted) == before and harvest_cursor >= len(harvest_batches):
                break

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
    # Formats whose every call died before a single question was looked at — a
    # timeout, an un-parseable reply. **These are retried too, and that is the second
    # half of the fix.** A run whose two multiple-choice calls both came back as
    # malformed JSON used to leave `working` empty, skip the backfill entirely and
    # fail the whole paper with "the model could not write usable questions from that
    # material" — a diagnosis about the book, for a fault in the transport. Retrying
    # them is not substituting a format the author did not ask for; it is finishing
    # the ask that never got a hearing.
    unproven = [
        fmt
        for fmt, count in produced.items()
        if count == 0 and not reached_validation[fmt]
    ]
    retry_order = working + unproven
    if len(accepted) < target and retry_order and not budget.spent():
        tracer.step(
            "backfill",
            f"{target - len(accepted)} short after the first pass · retrying "
            + ", ".join(fmt.value for fmt in retry_order),
        )
        await checkpoint()
    while len(accepted) < target and retry_order and not budget.spent():
        before = len(accepted)
        for fmt in retry_order:
            if len(accepted) >= target or budget.spent():
                break
            await ask(fmt, wanted=target - len(accepted))
        # Drop the ones that have now had their hearing and produced nothing: a
        # format that reached validation and was rejected every time is a format this
        # material does not support, and another round of it is another two minutes.
        retry_order = [
            fmt
            for fmt in retry_order
            if produced[fmt] > 0 or not reached_validation[fmt]
        ]
        if len(accepted) == before and not retry_order:
            break
        if len(accepted) == before and all(produced[fmt] == 0 for fmt in retry_order):
            # A full round produced nothing new and nothing left has ever worked.
            # Another round will not either, and each one costs minutes.
            break

    if not accepted:
        # WHY the paper is empty, not a guess at it. These two failures need opposite
        # actions from the author and used to be reported with the same sentence:
        # "the model could not write questions from that material" sent somebody off
        # to change chapters when the truth was that the model server never answered
        # at all -- a missing model, a dead container, a timeout. Blaming the book for
        # an outage is worse than saying nothing, because it is actionable and wrong.
        trace = tracer.finish(final=0)
        tallies = trace["summary"]["per_format"].values()
        calls = sum(tally["calls"] for tally in tallies)
        failed_calls = sum(tally["failed_calls"] for tally in tallies)
        rejected = sum(tally["rejected"] for tally in tallies)

        if calls and failed_calls == calls:
            reason = (
                "The question writer did not respond, so no questions were written. "
                "This is a problem with the service rather than with your books — "
                "nothing you change about the paper will help. Try again shortly, and "
                "if it keeps happening the Advanced panel below names the failure."
            )
        elif rejected:
            reason = (
                "The question writer answered, but nothing it wrote could be used — "
                f"{rejected} question(s) were rejected as ungrounded or malformed. "
                "Try different chapters, or fewer questions."
            )
        else:
            reason = (
                "No questions could be written from the material you chose. A passage "
                "has to state something definite for a question to have an answer."
            )

        raise attach_trace(
            ValidationFailed(reason),
            trace,
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
        "generation complete: %d accepted (%d from the book), %d rejected, "
        "%d deduped, %d final (%s)",
        len(accepted),
        len(harvested_stems),
        rejected,
        duplicates,
        len(final),
        assessment.id,
    )

    questions_generated_total.inc(len(final))
    questions = []
    for index, (item, fmt) in enumerate(final):
        built = _to_question(
            assessment.id,
            index,
            item,
            fmt,
            # Provenance the author can see before they publish (D31). A harvested
            # question is the book's, reproduced; a generated one is the model's
            # first draft. Different things to publish, and different things to
            # defend if a sitter disputes one.
            origin=(
                QuestionOrigin.HARVESTED
                if item.stem in harvested_stems
                else QuestionOrigin.GENERATED
            ),
        )
        if built is not None:
            questions.append(built)
    from_book = sum(
        1 for question in questions if question.origin is QuestionOrigin.HARVESTED
    )
    tracer.step(
        "persist",
        f"{len(questions)} questions written"
        + (f" · {from_book} taken from the book" if from_book else ""),
    )
    assessment.generation_trace = tracer.finish(final=len(questions))
    # Delete-and-replace together, in the transaction the caller commits: a re-run
    # that dies before this line leaves the previous paper standing, and the
    # checkpoints above never commit a half-swapped one.
    await session.execute(delete(Question).where(Question.assessment_id == assessment.id))
    session.add_all(questions)
    await session.flush()
    return questions


def _to_question(
    assessment_id: UUID,
    index: int,
    item: GeneratedQuestion,
    fmt: QuestionFormat,
    *,
    origin: QuestionOrigin = QuestionOrigin.GENERATED,
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
            correct_option=item.correct_option,
        )
    except ValidationFailed:
        logger.exception("validated question failed to build", extra={"format": fmt.value})
        return None

    return Question(
        assessment_id=assessment_id,
        index=index,
        difficulty=_difficulty(item.difficulty),
        source_chunk_ids=[item.source_chunk_id] if item.source_chunk_id else [],
        origin=origin,
        **fields,
    )


def _difficulty(value: str | None) -> Difficulty | None:
    try:
        return Difficulty(value) if value else None
    except ValueError:
        return None
