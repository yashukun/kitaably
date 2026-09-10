"""What the access log chooses to say.

The property under test is that the log stays *informative*: a successful probe is
dropped, and everything a reader would actually want -- failures, slow requests,
real API calls -- survives. The risk being guarded against is a filter that widens
over time until it swallows a genuine error, so the failing cases are asserted just
as explicitly as the quiet ones.

These are unit tests over the two predicates rather than over a live app, for the
same reason test_guards.py tests guards directly: the question is "does this filter
drop the right thing", and that answer must not depend on which routes exist today.
"""

import logging

import pytest

from app.core.config import settings
from app.core.middleware import QUIET_PATHS, _is_worth_logging, _level_for


@pytest.fixture
def quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deployed default: probe filtering on, one-second slow threshold."""
    monkeypatch.setattr(settings, "log_quiet_probes", True)
    monkeypatch.setattr(settings, "log_slow_request_ms", 1000.0)


# --- what gets dropped -------------------------------------------------------


@pytest.mark.parametrize("path", sorted(QUIET_PATHS))
def test_a_fast_successful_probe_is_not_logged(path: str, quiet: None) -> None:
    """The whole point: kubelet hits these every ten seconds and a 200 says nothing."""
    assert _is_worth_logging(path, 200, 5.0) is False


# --- what always survives ----------------------------------------------------


@pytest.mark.parametrize("status", [400, 401, 403, 404, 500, 503])
def test_a_failing_probe_is_always_logged(status: int, quiet: None) -> None:
    """A probe that FAILS is the event worth seeing. Dropping it would hide exactly
    the moment readiness broke."""
    assert _is_worth_logging("/ready", status, 5.0) is True


def test_a_slow_probe_is_logged_even_though_it_succeeded(quiet: None) -> None:
    """A /ready that takes a second is a dependency going bad, not a healthy pod."""
    assert _is_worth_logging("/ready", 200, 1500.0) is True


def test_the_slow_threshold_is_inclusive(quiet: None) -> None:
    assert _is_worth_logging("/health", 200, 1000.0) is True
    assert _is_worth_logging("/health", 200, 999.9) is False


def test_an_ordinary_api_call_is_logged(quiet: None) -> None:
    """Only operational endpoints are ever dropped. A real request always speaks."""
    assert _is_worth_logging("/api/v1/books", 200, 12.0) is True


def test_quiet_paths_are_matched_exactly_not_by_prefix(quiet: None) -> None:
    """/health is quiet; a route that merely starts with it is not, so a future
    /health-detail cannot silently inherit the filter."""
    assert _is_worth_logging("/healthz", 200, 5.0) is True
    assert _is_worth_logging("/health/detail", 200, 5.0) is True
    assert _is_worth_logging("/api/v1/ready", 200, 5.0) is True


def test_the_filter_can_be_turned_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """LOG_QUIET_PROBES=false restores one line per request for debugging routing."""
    monkeypatch.setattr(settings, "log_quiet_probes", False)
    assert _is_worth_logging("/health", 200, 1.0) is True


# --- severity ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (200, logging.INFO),
        (204, logging.INFO),
        (302, logging.INFO),
        (400, logging.WARNING),
        (401, logging.WARNING),
        (404, logging.WARNING),
        (499, logging.WARNING),
        (500, logging.ERROR),
        (503, logging.ERROR),
    ],
)
def test_status_decides_severity(status: int, expected: int) -> None:
    """A 5xx is ours and is an error; a 4xx is the caller's and is a warning. The
    split is what makes `level=ERROR` a usable filter in a log search."""
    assert _level_for(status) == expected
