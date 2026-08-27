"""Per-user rate limiting, in Redis.

Chat and generation both spend money or CPU on every call, so both are capped per
user (ARCHITECTURE.md, Security notes). Redis rather than a table: the counter is
disposable by design — losing it on a restart costs one extra allowed request, and
that is a much better trade than a write to Postgres on every message.
"""

import logging

from redis.asyncio import Redis

from app.core.config import settings
from app.core.errors import RateLimited

logger = logging.getLogger(__name__)


async def check(key: str, *, limit: int, window_seconds: int = 60) -> None:
    """Raise :class:`RateLimited` once the caller passes ``limit`` in the window.

    A fixed window, not a sliding one: it is a handful of Redis operations and the
    imprecision at the boundary does not matter for a limit whose job is stopping
    runaway cost rather than enforcing a contract.

    Redis being down must not take chat down with it. The limiter fails OPEN and says
    so loudly — the alternative is that a Redis blip becomes a total outage of the
    feature it was only ever meant to meter.
    """
    client = Redis.from_url(settings.redis_url)
    bucket = f"ratelimit:{key}"
    try:
        used = await client.incr(bucket)
        if used == 1:
            await client.expire(bucket, window_seconds)
    except Exception:
        logger.warning("rate limiter unavailable; allowing request", extra={"key": key})
        return
    finally:
        await client.aclose()

    if used > limit:
        raise RateLimited("You are sending messages too quickly. Wait a moment.")
