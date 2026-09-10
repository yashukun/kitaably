"""Structured JSON logging, matching the backend's format.

This service is scraped by the same log agent as everything else, so it emits the
same shape: one JSON object per line on stdout, with caller-supplied ``extra``
fields promoted to top-level keys. The previous ``basicConfig(format="%(message)s")``
printed the message and silently DISCARDED every ``extra`` -- so "loading model"
appeared without the model name, the cache directory, or the arena setting it was
passed, which is most of why that line exists.

It is a deliberate copy of ``backend/app/core/logging.py`` rather than a shared
import: this service is a separate deployable whose whole point is that it does not
depend on the backend package (DECISIONS.md D4). The duplication is the price of
that boundary, and it is about forty lines.
"""

import json
import logging
import sys

# Attributes the stdlib puts on every record; anything else is caller-supplied
# context and belongs in the JSON output.
_STANDARD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys()) | {
    "message",
    "asctime",
    "taskName",
}

# Operational endpoints. A successful probe is not information -- kubelet hits
# /health every ten seconds, which is what this service's logs were almost
# entirely made of. A FAILING probe still logs.
QUIET_PATHS = frozenset({"/health", "/ready", "/metrics"})


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS:
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


class _QuietProbeFilter(logging.Filter):
    """Drop uvicorn's access line for a successful probe.

    uvicorn.access formats its record lazily, so the path is read from the record
    args rather than the rendered message: ``(client, method, path, http_version,
    status)``. A non-2xx probe is kept -- that is the event worth seeing.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) < 5:
            return True
        path, status = args[2], args[4]
        if not isinstance(path, str) or path.split("?", 1)[0] not in QUIET_PATHS:
            return True
        try:
            return int(status) >= 400
        except (TypeError, ValueError):
            return True


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

    # Unlike the backend -- which has its own middleware logging every request with
    # a duration -- this service has no such middleware, so uvicorn.access is the
    # only per-request line and is kept. It is filtered rather than disabled: the
    # probe flood goes, real /embed calls stay.
    logging.getLogger("uvicorn.access").addFilter(_QuietProbeFilter())
