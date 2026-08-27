"""Operational endpoints. Unversioned — probes and scrapers are not API consumers.

The liveness/readiness distinction is the load-bearing part (DEPLOYMENT.md):

* ``/health`` answers "is this process alive". It calls nothing. A dependency being
  down must never restart this pod.
* ``/ready`` answers "can this process serve traffic". It checks Postgres, Redis and
  the embeddings service, and returns 503 when one is missing, which removes the pod
  from the load balancer without killing it.

Confusing the two is how a slow embeddings model-load turns into a restart loop.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.core.config import settings
from app.core.deps import allow_anonymous
from app.core.metrics import registry

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ops"])

PROBE_TIMEOUT_SECONDS = 3.0


@router.get("/health")
async def health(_: None = Depends(allow_anonymous)) -> dict[str, str]:
    """Liveness. No dependency calls, by design."""
    return {"status": "ok", "app": settings.app_name, "environment": settings.environment}


async def _check_postgres() -> bool:
    from app.db.session import ping

    await ping()
    return True


async def _check_redis() -> bool:
    from redis.asyncio import Redis

    client = Redis.from_url(settings.redis_url)
    try:
        await client.ping()
        return True
    finally:
        await client.aclose()


async def _check_embeddings() -> bool:
    import httpx

    async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS) as client:
        response = await client.get(f"{settings.embeddings_url}/health")
        response.raise_for_status()
    return True


@router.get("/ready")
async def ready(response: Response, _: None = Depends(allow_anonymous)) -> dict[str, object]:
    """Readiness. Every dependency is probed concurrently and reported by name."""
    checks = {
        "postgres": _check_postgres(),
        "redis": _check_redis(),
        "embeddings": _check_embeddings(),
    }

    results = await asyncio.gather(
        *(asyncio.wait_for(coro, PROBE_TIMEOUT_SECONDS) for coro in checks.values()),
        return_exceptions=True,
    )

    detail: dict[str, str] = {}
    healthy = True
    for name, result in zip(checks, results, strict=True):
        if isinstance(result, BaseException):
            healthy = False
            detail[name] = "unavailable"
            logger.warning(
                "dependency unavailable",
                extra={"dependency": name, "error": str(result)},
            )
        else:
            detail[name] = "ok"

    if not healthy:
        response.status_code = 503

    return {"status": "ready" if healthy else "degraded", "checks": detail}


@router.get("/metrics")
async def metrics(_: None = Depends(allow_anonymous)) -> Response:
    """Prometheus scrape target."""
    return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)
