"""The model holder.

Loading takes seconds, which is the entire reason this service has a readiness probe
distinct from its liveness probe (DEPLOYMENT.md): a liveness probe that fired during
model load would restart the pod forever, and it would never get far enough to serve
anything.

Encoding is CPU-bound, so it runs in a worker thread. Doing it inline would block the
event loop and stall the health endpoints of the very service being probed.
"""

import asyncio
import logging
import threading
import time
from typing import TYPE_CHECKING

from app.config import settings

if TYPE_CHECKING:
    from fastembed import TextEmbedding

logger = logging.getLogger(__name__)

_model: "TextEmbedding | None" = None
_lock = threading.Lock()


def is_loaded() -> bool:
    return _model is not None


def load() -> None:
    """Load the ONNX model. Called once from the lifespan hook, before serving."""
    global _model

    with _lock:
        if _model is not None:
            return

        from fastembed import TextEmbedding

        logger.info(
            "loading model",
            extra={
                "model": settings.embedding_model,
                "cache_dir": settings.model_cache_dir,
                "cpu_mem_arena": settings.embedding_cpu_mem_arena,
            },
        )
        started = time.perf_counter()
        _model = TextEmbedding(
            model_name=settings.embedding_model,
            cache_dir=settings.model_cache_dir,
            # Passed through fastembed to the ONNX session. See the setting: with the
            # arena on, this process grew to 4.95 GiB and starved the model server
            # sharing its VM.
            enable_cpu_mem_arena=settings.embedding_cpu_mem_arena,
        )
        # How long this took is the number that explains a slow rollout: it is what
        # /ready waits on, and a cold cache (a ~130 MB download) takes tens of
        # seconds where a warm one takes about one. Logging it turns "the pod was
        # slow to come up" into "the model cache volume was empty".
        logger.info(
            "model loaded",
            extra={
                "model": settings.embedding_model,
                "dim": settings.embedding_dim,
                "load_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )


def _encode(texts: list[str]) -> list[list[float]]:
    if _model is None:
        raise RuntimeError("model not loaded")
    vectors = _model.embed(texts, batch_size=settings.embedding_batch_size)
    return [vector.tolist() for vector in vectors]


async def embed(texts: list[str]) -> list[list[float]]:
    """Encode a batch off the event loop."""
    return await asyncio.to_thread(_encode, texts)
