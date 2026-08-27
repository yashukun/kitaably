"""HTTP client for the embeddings service. Phase 3, pooled in Phase 7 (D21).

The whole interface to a separate deployable (DECISIONS.md D4). Collapsing this back
to an in-process import is a single-file change plus a Dockerfile edit.

Queries and documents must be embedded with the SAME model. If the model changes,
every book has to be re-embedded — vectors from different models are not comparable,
and mixing them degrades silently rather than erroring.

They must NOT, however, be embedded the same *way*. ``bge-small-en-v1.5`` is an
asymmetric retriever: passages go in bare, questions go in behind a short
instruction (:data:`app.core.config.Settings.embedding_query_prefix`). Embedding a
question as though it were a passage is the documented misuse of the model and
costs recall on every search — see :func:`embed_query`.
"""

import asyncio
import weakref

import httpx

from app.core.config import settings
from app.core.errors import UpstreamUnavailable

# Generous: the service is CPU-only and a full batch of 64 takes seconds.
_TIMEOUT = httpx.Timeout(120.0, connect=10.0)

# One warm connection per event loop.
#
# A fresh `AsyncClient` per call — which is what this module used to do — pays a TCP
# handshake on every question, on the critical path, for a service that answers in
# under ten milliseconds. Pooling removes that.
#
# Keyed by loop, and weakly, for the same reason `db/session.py` gives the worker a
# NullPool engine: a Celery task runs `asyncio.run()`, so its loop is created and
# destroyed per task, and a connection belonging to a dead loop cannot be reused. The
# API has one long-lived loop and therefore one long-lived pool; the worker gets a
# client per task, which dies with the loop that opened it. The weak key means the
# entry disappears when the loop does rather than accumulating one per task.
_clients: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient]" = (
    weakref.WeakKeyDictionary()
)


def _client() -> httpx.AsyncClient:
    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            timeout=_TIMEOUT,
            limits=httpx.Limits(max_keepalive_connections=8, max_connections=16),
        )
        _clients[loop] = client
    return client


async def aclose() -> None:
    """Close this loop's pooled client. Called from the API's lifespan shutdown."""
    loop = asyncio.get_running_loop()
    client = _clients.pop(loop, None)
    if client is not None and not client.is_closed:
        await client.aclose()


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Encode a list of PASSAGES, batched at EMBEDDING_BATCH_SIZE.

    No prefix: bge embeds documents bare. Use :func:`embed_query` for a question.

    One request per chunk is an order of magnitude slower than one request per batch.
    """
    if not texts:
        return []

    vectors: list[list[float]] = []
    size = settings.embedding_batch_size
    client = _client()

    for start in range(0, len(texts), size):
        batch = texts[start : start + size]
        try:
            response = await client.post(
                f"{settings.embeddings_url}/embed", json={"texts": batch}
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # Fail loudly. Writing zero vectors would produce a book that is
            # indexed, searchable, and silently matches nothing.
            raise UpstreamUnavailable("The embedding service is unavailable.") from exc

        payload = response.json()
        if payload.get("dim") != settings.embedding_dim:
            raise UpstreamUnavailable("The embedding service returned the wrong dimension.")
        vectors.extend(payload["embeddings"])

    return vectors


async def embed_query(text: str) -> list[float]:
    """Encode a QUESTION, with the retrieval instruction bge expects in front.

    The prefix is not decoration and it is not a prompt. ``bge-small-en-v1.5`` was
    trained with questions and passages in different forms, so a bare question lands
    slightly away from the passages that answer it — every distance comes back a
    little too high, and the ones near the threshold fall past
    ``retrieval_max_distance`` into a grounded refusal. The reader sees "your books
    don't cover this" about a page that is sitting in the index.

    It is applied HERE rather than at the call site so that every search gets it and
    no future caller has to remember. The stored passage vectors are unaffected —
    which is the point of an asymmetric model, not a mismatch.
    """
    prefix = settings.embedding_query_prefix.strip()
    # Joined with an explicit space rather than trusting the setting to carry a
    # trailing one: dotenv strips trailing whitespace from an unquoted value, which
    # would glue the instruction onto the first word of the question and change what
    # is being searched for. Strip-then-join makes the value's spacing irrelevant.
    return (await embed_texts([f"{prefix} {text}" if prefix else text]))[0]
