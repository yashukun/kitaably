"""Domain exceptions and the single handler that maps them to HTTP.

Services raise these. Services never raise ``HTTPException`` — that belongs to the
router layer, and a service that knows about status codes has stopped being reusable
from a Celery task.

Messages are shown to users, so they must not reveal whether another user's resource
exists. Prefer ``NotFound`` over ``Forbidden`` when the caller has no business knowing
the resource is there at all: a 403 on someone else's book confirms the book exists.
"""

import logging

from fastapi import Request
from fastapi.responses import JSONResponse

from app.core.logging import get_request_id

logger = logging.getLogger(__name__)


class DomainError(Exception):
    """Base for every error a service is allowed to raise."""

    code: str = "internal_error"
    http_status: int = 500
    message: str = "Something went wrong."

    def __init__(self, message: str | None = None, **context: object) -> None:
        self.message = message or self.message
        self.context = context
        super().__init__(self.message)


class NotFound(DomainError):
    code = "not_found"
    http_status = 404
    message = "Not found."


class Forbidden(DomainError):
    """Nothing raises this today, and that is the point.

    Every guard answers "not yours" with :class:`NotFound`, because a 403 on somebody
    else's row confirms the row exists. This class staying unused is evidence that
    rule is actually being followed — it is the exception you reach for when the
    caller already knows the resource exists and is being refused an action on it.
    """

    code = "forbidden"
    http_status = 403
    message = "You do not have access to this."


class Unauthenticated(DomainError):
    code = "unauthenticated"
    http_status = 401
    message = "Sign in to continue."


class Conflict(DomainError):
    code = "conflict"
    http_status = 409
    message = "That conflicts with the current state."


class ValidationFailed(DomainError):
    code = "validation_failed"
    http_status = 422
    message = "That request is not valid."


class RateLimited(DomainError):
    code = "rate_limited"
    http_status = 429
    message = "Too many requests. Try again shortly."


class UpstreamUnavailable(DomainError):
    code = "upstream_unavailable"
    http_status = 503
    message = "A service this depends on is unavailable."


async def domain_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """The one place a domain error becomes a response body.

    Typed as ``Exception`` because that is the signature Starlette registers, and
    narrowed here rather than asserted: a handler is the last thing that should
    raise on its way to producing an error response.
    """
    error = exc if isinstance(exc, DomainError) else DomainError()
    return JSONResponse(
        status_code=error.http_status,
        content={
            "error": {
                "code": error.code,
                "message": error.message,
                "request_id": get_request_id(),
            }
        },
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Anything not a DomainError is a bug. Say nothing useful to an attacker.

    But say everything to the operator. Registering a handler for ``Exception``
    stops Starlette printing the traceback itself, so if this does not log, a 500
    becomes a request id and nothing else — the response is deliberately opaque, and
    without this the logs would be too.
    """
    logger.exception(
        "unhandled error",
        extra={"method": request.method, "path": request.url.path},
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "internal_error",
                "message": "Something went wrong.",
                "request_id": get_request_id(),
            }
        },
    )
