"""Structured JSON logging with a request id carried across the whole request.

Every line is one JSON object on stdout, because that is what a cluster log agent
can parse. The ``request_id`` is generated at the edge (or taken from an inbound
``X-Request-ID``) and propagated into Celery task headers, so an ingest failure can
be traced back to the upload that caused it.
"""

import json
import logging
import sys
import uuid
from contextvars import ContextVar

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)

# Attributes the stdlib puts on every record; anything else is caller-supplied
# context and belongs in the JSON output.
_STANDARD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys()
) | {"message", "asctime", "taskName"}


def set_request_id(value: str | None = None) -> str:
    request_id = value or uuid.uuid4().hex
    _request_id.set(request_id)
    return request_id


def get_request_id() -> str | None:
    return _request_id.get()


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        request_id = get_request_id()
        if request_id:
            payload["request_id"] = request_id

        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS:
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Replace the root handlers with one JSON handler on stdout."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn ships its own handlers; let them fall through to ours so every line
    # is JSON rather than two formats interleaved.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
