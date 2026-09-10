"""Cross-cutting request middleware: request id, access log, latency metric.

Every route gets these for free. Features add their own counters on top; they do not
re-implement any of this.

The access log is deliberately not one-line-per-request. An operational endpoint
answering 200 is the single highest-volume line the backend can emit -- kubelet
probes /health and /ready every ten seconds, Prometheus scrapes /metrics -- and none
of it is information. What survives the filter is what someone would actually want
to read: every failure, every slow request, and every real API call. The metrics are
unaffected; a request that is not logged is still counted, so `http_requests_total`
remains the place to ask "how many probes succeeded".
"""

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import settings
from app.core.logging import set_request_id
from app.core.metrics import http_request_duration_seconds, http_requests_total

logger = logging.getLogger("kitaably.access")

REQUEST_ID_HEADER = "X-Request-ID"

# Unversioned operational endpoints. Matched exactly rather than by prefix, so a
# future /healthcheck-something route does not silently inherit the filter.
QUIET_PATHS = frozenset({"/health", "/ready", "/metrics"})


def _level_for(status: int) -> int:
    """A 5xx is the server's fault and is an error; a 4xx is the caller's and is a
    warning. Neither should have to be grepped out of a wall of 200s."""
    if status >= 500:
        return logging.ERROR
    if status >= 400:
        return logging.WARNING
    return logging.INFO


def _is_worth_logging(path: str, status: int, duration_ms: float) -> bool:
    """Decide whether this request is information.

    Only one class of request is dropped: an operational endpoint that succeeded
    quickly. A probe that failed, or one that took a second to answer, is exactly
    the thing worth seeing -- so the filter checks status and duration too, not
    just the path.
    """
    if not settings.log_quiet_probes:
        return True
    if path not in QUIET_PATHS:
        return True
    if status >= 400:
        return True
    return duration_ms >= settings.log_slow_request_ms


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = set_request_id(request.headers.get(REQUEST_ID_HEADER))
        started = time.perf_counter()

        try:
            response = await call_next(request)
            status = response.status_code
        except Exception:
            status = 500
            raise
        finally:
            elapsed = time.perf_counter() - started

            # Label with the route template, not the raw path: /books/{book_id}
            # keeps cardinality bounded where /books/<uuid> would not.
            route = request.scope.get("route")
            path = getattr(route, "path", request.url.path)

            http_requests_total.labels(request.method, path, str(status)).inc()
            http_request_duration_seconds.labels(request.method, path).observe(elapsed)

            duration_ms = round(elapsed * 1000, 2)
            level = _level_for(status)

            if _is_worth_logging(request.url.path, status, duration_ms):
                logger.log(
                    level,
                    "request",
                    extra={
                        "method": request.method,
                        "path": request.url.path,
                        "route": path,
                        "status": status,
                        "duration_ms": duration_ms,
                    },
                )

        response.headers[REQUEST_ID_HEADER] = request_id
        return response
