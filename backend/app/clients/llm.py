"""One OpenAI-compatible client. Phases 4-6.

Pointed by OPENAI_BASE_URL: Ollama locally, OpenAI in deploy (DECISIONS.md D5). One
code path, one retry policy, one place that counts tokens. Swapping providers is an
environment change, not a code change.
"""

import logging
from collections.abc import AsyncIterator
from typing import cast

from openai import APIError, AsyncOpenAI, AsyncStream, omit
from openai.types.chat import ChatCompletionChunk

from app.core.config import settings
from app.core.errors import UpstreamUnavailable
from app.core.metrics import llm_calls_total

logger = logging.getLogger(__name__)

Message = dict[str, str]

_client = AsyncOpenAI(
    base_url=settings.openai_base_url,
    api_key=settings.openai_api_key,
    timeout=settings.llm_timeout_seconds,
    # The SDK's own retry, with backoff. A provider outage must not become a
    # thundering herd of every queued request at once.
    max_retries=settings.llm_max_retries,
)


async def stream(
    messages: list[Message],
    *,
    max_tokens: int | None = None,
    request_timeout: float | None = None,
    retries: int | None = None,
) -> AsyncIterator[str]:
    """Stream a completion token by token.

    On failure this raises rather than yielding an apology. An ungrounded answer is a
    bug; so is a fabricated one produced because the model was unreachable.

    Args:
        max_tokens: hard ceiling on the reply, enforced by the provider (Ollama maps
            it to num_predict). No ceiling by default. Callers that parse structured
            output choose their own trade: grading passes none, because a truncated
            grade is unusable and the replies are small anyway; assessment generation
            passes one, because a truncated batch is merely a skipped batch its
            backfill recovers from, while the observed failure is the opposite — a
            runaway reply (eighteen questions for two asked) that costs minutes of
            CPU generating output the validator then rejects.
        request_timeout: per-call override of the client-wide request timeout,
            applied at the HTTP layer by the SDK — not an asyncio timeout.
        retries: per-call override of the client-wide retry count. Generation passes
            0: its backfill pass IS its retry policy, and an SDK retry of a call that
            timed out on a saturated CPU model just queues another timeout behind it.
    """
    client = _client
    if request_timeout is not None or retries is not None:
        client = _client.with_options(
            timeout=(
                request_timeout
                if request_timeout is not None
                else settings.llm_timeout_seconds
            ),
            max_retries=retries if retries is not None else settings.llm_max_retries,
        )
    try:
        # cast because the SDK's return type is a union over stream=True/False and
        # mypy cannot narrow it from a literal keyword.
        response = cast(
            AsyncStream[ChatCompletionChunk],
            await client.chat.completions.create(
                model=settings.llm_model,
                messages=messages,  # type: ignore[arg-type]
                stream=True,
                temperature=0.2,
                # omit, not None: the SDK leaves an omitted field out of the request
                # entirely, whereas an explicit None is serialised as
                # `"max_tokens": null` and an OpenAI-compatible server is under no
                # obligation to read that as "unlimited".
                max_tokens=omit if max_tokens is None else max_tokens,
            ),
        )
        async for chunk in response:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta
    except APIError as exc:
        llm_calls_total.labels(settings.llm_model, "error").inc()
        logger.warning("llm call failed", extra={"error": str(exc)})
        raise UpstreamUnavailable("The tutor is unavailable right now.") from exc

    llm_calls_total.labels(settings.llm_model, "ok").inc()


async def complete(
    messages: list[Message],
    *,
    max_tokens: int | None = None,
    request_timeout: float | None = None,
    retries: int | None = None,
) -> str:
    parts = [
        token
        async for token in stream(
            messages,
            max_tokens=max_tokens,
            request_timeout=request_timeout,
            retries=retries,
        )
    ]
    return "".join(parts)
