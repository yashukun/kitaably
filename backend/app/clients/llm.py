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
    json_object: bool = False,
    model: str | None = None,
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
        json_object: ask the provider to constrain the reply to a single JSON object.
            Every caller that parses the reply passes this, and the reason is
            measured rather than tidy: on llama3.2:3b five generation calls in eight
            came back as un-parseable JSON — an unterminated string, a trailing
            comma, a key without quotes — and each one threw away a minute or two of
            CPU decode. Ollama constrains sampling to the JSON grammar for this, so
            the failure stops happening rather than being recovered from.

            Providers that do not know the field 400 on it. That is handled here
            rather than by the caller: the request is retried once without it, so a
            provider swap degrades to the old behaviour instead of failing.
        model: override the configured model for this call. The two workloads want
            opposite things and one setting could not serve both — chat has a person
            waiting and wants the fastest model that is good enough, while generation
            runs in a worker where nobody is watching and wants the best model that
            finishes. See `settings.generation_model`.
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
    want_json = json_object and settings.llm_json_mode
    chosen = model or settings.llm_model

    async def open_stream(with_json: bool) -> AsyncStream[ChatCompletionChunk]:
        # cast because the SDK's return type is a union over stream=True/False and
        # mypy cannot narrow it from a literal keyword.
        return cast(
            AsyncStream[ChatCompletionChunk],
            await client.chat.completions.create(
                model=chosen,
                messages=messages,  # type: ignore[arg-type]
                stream=True,
                temperature=0.2,
                # omit, not None: the SDK leaves an omitted field out of the request
                # entirely, whereas an explicit None is serialised as
                # `"max_tokens": null` and an OpenAI-compatible server is under no
                # obligation to read that as "unlimited".
                max_tokens=omit if max_tokens is None else max_tokens,
                response_format={"type": "json_object"} if with_json else omit,
            ),
        )

    try:
        try:
            response = await open_stream(want_json)
        except APIError as exc:
            # A provider that does not know `response_format` refuses the whole
            # request. Fall back once, unconstrained, rather than failing the call —
            # the parser downstream still has to survive a free-form reply anyway.
            if not want_json or not _rejected_json_mode(exc):
                raise
            logger.info("provider rejected json mode; retrying without it")
            response = await open_stream(False)

        async for chunk in response:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta
    except APIError as exc:
        llm_calls_total.labels(chosen, "error").inc()
        # The model name and the provider's own words go in the MESSAGE, not only in
        # `extra`: the Celery worker uses its own formatter and drops structured
        # extras, so an extras-only line reads as "llm call failed" and tells nobody
        # which model or why. A 404 naming a model that was never pulled is a
        # one-command fix that used to be invisible in the logs.
        detail = str(exc).strip() or exc.__class__.__name__
        logger.warning(
            "llm call failed (model=%s): %s", chosen, detail, extra={"model": chosen}
        )
        raise UpstreamUnavailable("The tutor is unavailable right now.") from exc

    llm_calls_total.labels(chosen, "ok").inc()


def _rejected_json_mode(exc: APIError) -> bool:
    """Did the provider refuse the request *because of* `response_format`?

    Narrow on purpose. A 500 from a model that is loading, or a timeout, must not be
    retried here — the caller's own retry policy owns those, and generation's policy
    is deliberately "no retry, the backfill is the retry" (D30). Only a 4xx that
    names the field is a provider that does not support it.
    """
    status = getattr(exc, "status_code", None)
    if status is not None and not 400 <= int(status) < 500:
        return False
    return "response_format" in str(exc).lower()


async def complete(
    messages: list[Message],
    *,
    max_tokens: int | None = None,
    request_timeout: float | None = None,
    retries: int | None = None,
    json_object: bool = False,
    model: str | None = None,
) -> str:
    parts = [
        token
        async for token in stream(
            messages,
            max_tokens=max_tokens,
            request_timeout=request_timeout,
            retries=retries,
            json_object=json_object,
            model=model,
        )
    ]
    return "".join(parts)
