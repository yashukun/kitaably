"""Embedding service — bge-small-en-v1.5 on CPU, over HTTP.

A separate deployable rather than a library import (DECISIONS.md D4): its own image,
probes, resource limits, and scaling behaviour, and a ~150 MB ONNX runtime kept out
of the API image.

Endpoints
    GET  /health   liveness — the process is up. Says nothing about the model.
    GET  /ready    readiness — the model is loaded and this pod can serve.
    GET  /metrics  Prometheus.
    POST /embed    {"texts": [...]} -> {"embeddings": [[...], ...]}
"""

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from app import model
from app.config import settings
from app.schemas import EmbedRequest, EmbedResponse

logging.basicConfig(level=settings.log_level.upper(), format="%(message)s")
logger = logging.getLogger(__name__)

embed_requests_total = Counter(
    "kitaably_embed_requests_total", "Embed requests by outcome.", ["outcome"]
)
embed_texts_total = Counter("kitaably_embed_texts_total", "Texts encoded.")
embed_duration_seconds = Histogram(
    "kitaably_embed_duration_seconds",
    "Time to encode one batch.",
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Loaded before the server accepts traffic, so /ready flips exactly once and
    # Kubernetes never routes a request at a model that is still warming up.
    model.load()
    yield


app = FastAPI(title="Kitaably Embeddings", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness only. Deliberately does not check the model."""
    return {"status": "ok"}


@app.get("/ready")
async def ready(response: Response) -> dict[str, object]:
    """Readiness. False until the weights are in memory."""
    loaded = model.is_loaded()
    if not loaded:
        response.status_code = 503
    return {
        "status": "ready" if loaded else "loading",
        "model": settings.embedding_model,
        "dim": settings.embedding_dim,
    }


@app.get("/metrics")
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/embed", response_model=EmbedResponse)
async def embed(request: EmbedRequest) -> EmbedResponse:
    texts = [text[: settings.max_chars_per_text] for text in request.texts]

    started = time.perf_counter()
    try:
        vectors = await model.embed(texts)
    except Exception:
        embed_requests_total.labels("error").inc()
        raise
    embed_duration_seconds.observe(time.perf_counter() - started)
    embed_requests_total.labels("ok").inc()
    embed_texts_total.inc(len(texts))

    return EmbedResponse(
        model=settings.embedding_model,
        dim=settings.embedding_dim,
        embeddings=vectors,
    )
