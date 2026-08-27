"""Cross-cutting request middleware: request id, access log, latency metric.

Every route gets these for free. Features add their own counters on top; they do not
re-implement any of this.
"""

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import set_request_id
from app.core.metrics import http_request_duration_seconds, http_requests_total

logger = logging.getLogger("kitaably.access")

REQUEST_ID_HEADER = "X-Request-ID"


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

            logger.info(
                "request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "route": path,
                    "status": status,
                    "duration_ms": round(elapsed * 1000, 2),
                },
            )

        response.headers[REQUEST_ID_HEADER] = request_id
        return response
