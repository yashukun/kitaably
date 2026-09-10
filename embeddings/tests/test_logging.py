"""What this service's logs say, and what they throw away.

Two properties are under test. First, that the formatter is structured: the old
``basicConfig(format="%(message)s")`` silently discarded every ``extra`` field, so
"loading model" printed without the model name it was given -- a log line that
looked fine and carried nothing. Second, that the probe filter drops only a
SUCCESSFUL probe: a failing /ready is the event most worth seeing here, because it
is what a slow model load looks like from outside.
"""

import json
import logging

import pytest

from app.logging import QUIET_PATHS, JsonFormatter, _QuietProbeFilter


def _record(**kwargs: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="app.main", level=logging.INFO, pathname="", lineno=0,
        msg="embedded", args=None, exc_info=None,
    )
    for key, value in kwargs.items():
        setattr(record, key, value)
    return record


def _access(path: str, status: int, method: str = "GET") -> logging.LogRecord:
    """A uvicorn.access record: it formats lazily, so the path lives in args."""
    return logging.LogRecord(
        name="uvicorn.access", level=logging.INFO, pathname="", lineno=0,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("10.0.0.1", method, path, "1.1", status),
        exc_info=None,
    )


# --- the formatter carries context ------------------------------------------


def test_extra_fields_are_promoted_to_top_level_keys() -> None:
    """The whole reason this replaced basicConfig: extras must survive."""
    payload = json.loads(JsonFormatter().format(_record(texts=8, duration_ms=91.4)))
    assert payload["texts"] == 8
    assert payload["duration_ms"] == 91.4
    assert payload["message"] == "embedded"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "app.main"


def test_every_line_is_one_json_object() -> None:
    """A log agent parses per line; a multi-line record would break the stream."""
    line = JsonFormatter().format(_record(model="BAAI/bge-small-en-v1.5"))
    assert "\n" not in line
    assert json.loads(line)["model"] == "BAAI/bge-small-en-v1.5"


def test_unserialisable_values_do_not_crash_the_logger() -> None:
    """A logger that raises while reporting a failure hides the failure."""
    payload = json.loads(JsonFormatter().format(_record(obj=object())))
    assert isinstance(payload["obj"], str)


# --- the probe filter drops only successful probes ---------------------------


@pytest.mark.parametrize("path", sorted(QUIET_PATHS))
def test_a_successful_probe_is_dropped(path: str) -> None:
    assert _QuietProbeFilter().filter(_access(path, 200)) is False


@pytest.mark.parametrize("status", [400, 404, 500, 503])
def test_a_failing_probe_is_kept(status: int) -> None:
    """A 503 from /ready is the model still loading -- the one probe worth reading."""
    assert _QuietProbeFilter().filter(_access("/ready", status)) is True


def test_a_real_embed_call_is_kept() -> None:
    assert _QuietProbeFilter().filter(_access("/embed", 200, method="POST")) is True


def test_quiet_paths_are_matched_exactly_not_by_prefix() -> None:
    assert _QuietProbeFilter().filter(_access("/healthz", 200)) is True
    assert _QuietProbeFilter().filter(_access("/ready/detail", 200)) is True


def test_a_query_string_does_not_defeat_the_filter() -> None:
    """/health?probe=1 is still a probe; matching the raw arg would leak it through."""
    assert _QuietProbeFilter().filter(_access("/health?probe=1", 200)) is False


def test_an_unrecognised_record_shape_is_kept() -> None:
    """If uvicorn ever changes its args, fail toward MORE logging, not less."""
    odd = logging.LogRecord(
        name="uvicorn.access", level=logging.INFO, pathname="", lineno=0,
        msg="something else", args=None, exc_info=None,
    )
    assert _QuietProbeFilter().filter(odd) is True
