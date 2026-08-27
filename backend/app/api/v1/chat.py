"""chat routes. Phase 4.

    POST  /chat/sessions                          require_auth
    GET   /chat/sessions                          require_auth
    GET   /chat/sessions/{session_id}/messages    require_auth
    GET   /chat/sessions/{session_id}/export      require_auth  -> file download
    POST  /chat/sessions/{session_id}/messages    require_auth  -> SSE stream

Every route declares a guard. Ownership of a conversation is enforced by RLS: a
session that is not yours is not visible, so it reads as absent rather than
forbidden.
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Coroutine
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import ratelimit
from app.core.config import settings
from app.core.deps import require_auth
from app.core.errors import DomainError, RateLimited
from app.core.logging import get_request_id
from app.core.metrics import chat_rate_limited_total
from app.core.security import Principal
from app.db.models.enums import MessageRole
from app.db.session import get_session
from app.schemas.chat import (
    ChatExportFormat,
    ChatSessionCreate,
    ChatSessionRead,
    MessageCreate,
    MessageRead,
)
from app.schemas.common import Page
from app.services import chat as service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])


# Strong references to writes that outlived their request. Without this set the only
# reference to the task is the event loop's weak one, and a garbage collection
# between scheduling and completion cancels it -- the failure mode being a chat
# message that vanishes on a slow machine and never on a fast one.
_pending: set[asyncio.Task[None]] = set()


def _detach(coro: Coroutine[Any, Any, None]) -> None:
    """Finish this write independently of the request that started it.

    Used on exactly one path: the reader closed the tab mid-answer. The generator is
    then being torn down, so awaiting anything inside it re-raises ``CancelledError``
    immediately and the write never happens -- which is precisely how a transcript
    ends up holding a question with no answer under it.

    ``create_task`` schedules rather than awaits, so it survives the teardown and runs
    on the application's loop with its own database connection.
    """
    task = asyncio.create_task(coro)
    _pending.add(task)
    task.add_done_callback(_pending.discard)


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


@router.post("/chat/sessions", status_code=201)
async def create_chat_session(
    data: ChatSessionCreate,
    principal: Principal = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> ChatSessionRead:
    chat = await service.create_session(session, principal, data.title)
    return ChatSessionRead.model_validate(chat)


@router.get("/chat/sessions")
async def list_chat_sessions(
    principal: Principal = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> Page[ChatSessionRead]:
    rows = await service.list_sessions(session, principal)
    return Page(items=[ChatSessionRead.model_validate(row) for row in rows])


@router.get("/chat/sessions/{session_id}/messages")
async def list_messages(
    session_id: UUID,
    principal: Principal = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> Page[MessageRead]:
    """The whole transcript, oldest first.

    This is what makes a conversation survive a page reload, and it existing in the
    API while nothing called it is the reason chat appeared to forget everything: the
    rows were in Postgres the entire time and the client never asked for them.
    """
    rows = await service.list_messages(session, principal, session_id)
    return Page(items=[MessageRead.model_validate(row) for row in rows])


@router.get("/chat/sessions/{session_id}/export")
async def export_chat_session(
    session_id: UUID,
    format: ChatExportFormat = ChatExportFormat.JSON,
    principal: Principal = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """The whole conversation as a downloadable file.

    Authorization is identical to reading the transcript: RLS hides anyone
    else's conversation, so it reads as absent rather than forbidden. The body
    contains exactly what ``GET .../messages`` already returns — no audit row,
    because nothing about who can read what has changed.
    """
    filename, media_type, body = await service.export_conversation(
        session, principal, session_id, format.value
    )
    return Response(
        content=body,
        media_type=media_type,
        # `attachment` so the browser saves rather than navigates; the filename
        # is ASCII by construction (see _export_filename).
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/chat/sessions/{session_id}/messages")
async def send_message(
    session_id: UUID,
    data: MessageCreate,
    principal: Principal = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """Ask a question and stream a grounded answer.

    Order matters, and all of it happens *before* the response body starts:

    1. rate limit, so an expensive call is refused cheaply
    2. authorize the conversation (RLS)
    3. decide the turn — classify, condense, retrieve, rank — under
       ``build_retrieval_filter``, the chokepoint
    4. persist the question, name the conversation if it has no name, and commit

    Only then does streaming begin. The request's database session is closed by its
    dependency once this function returns, so the generator below cannot use it —
    hence the whole turn being decided here, and a fresh session for the final write.
    """
    try:
        await ratelimit.check(
            f"chat:{principal.id}", limit=settings.chat_rate_limit_per_minute
        )
    except RateLimited:
        chat_rate_limited_total.inc()
        raise

    chat = await service.get_session(session, principal, session_id)
    turn = await service.prepare_turn(
        session, principal, chat, data.content, book_ids=data.book_ids or None
    )

    await service.persist_message(
        session,
        chat_session_id=chat.id,
        role=MessageRole.USER,
        content=data.content,
        intent=turn.intent,
    )
    await service.touch_session(session, chat, first_question=data.content)
    await session.commit()

    chat_id = chat.id
    citations = turn.citations
    request_id = get_request_id()

    async def events() -> AsyncIterator[str]:
        started = time.perf_counter()
        # Intent first, so the UI can render a greeting as a greeting rather than
        # putting "Reading your books…" under a message that was never searched for.
        yield _sse("intent", {"intent": turn.intent.value})
        # Then the pipeline trace — everything was decided before the stream began,
        # so the "Advanced" disclosure can show what is running while the answer is
        # still arriving. Ephemeral by design: it rides the stream and is never
        # persisted, because the transcript records the conversation, not the
        # machinery.
        if turn.trace is not None:
            yield _sse("pipeline", {"pipeline": turn.trace})
        # Then citations: the sources can be drawn while the answer is still arriving.
        yield _sse("citations", {"citations": citations})

        answer: list[str] = []
        filed = False
        try:
            try:
                async for token in service.stream_answer(turn):
                    answer.append(token)
                    yield _sse("token", {"text": token})
            except DomainError as exc:
                # An upstream failure is reported as a failure. It is never smoothed
                # over with an answer the model did not ground in anything.
                logger.warning("chat stream failed", extra={"error": exc.code})
                yield _sse("error", {"code": exc.code, "message": exc.message})
                return
            except Exception:
                logger.exception("chat stream crashed")
                yield _sse(
                    "error",
                    {"code": "internal_error", "message": "Something went wrong."},
                )
                return

            await service.record_answer(
                chat_session_id=chat_id, content="".join(answer), citations=citations
            )
            filed = True
            yield _sse(
                "done",
                {
                    "request_id": request_id,
                    # Streaming time only — generation, not retrieval, which the
                    # pipeline trace itemises stage by stage.
                    "ms": int((time.perf_counter() - started) * 1000),
                },
            )
        finally:
            # Reached when the reader closed the tab mid-answer, or when an upstream
            # error cut the stream short after some text had already been shown.
            # Either way they saw those words, so the transcript should hold them —
            # a question filed with no answer under it reads as lost history, which
            # is exactly the complaint this whole path exists to fix.
            if not filed and answer:
                _detach(
                    service.record_answer(
                        chat_session_id=chat_id,
                        content="".join(answer),
                        citations=citations,
                    )
                )

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            # Tells nginx and friends not to buffer, which would collect the whole
            # answer and deliver it in one lump — technically correct, and the exact
            # opposite of why this endpoint streams.
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
