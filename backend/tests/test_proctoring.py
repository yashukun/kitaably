"""Proctoring capture: the severity map, the scoring arithmetic, and the shapes
that keep client claims advisory.

One of the four areas CLAUDE.md requires tests for before merge — a silent bug
here is a fairness incident, not a broken page. What a unit suite can hold is
covered here; the RLS behaviour (the sitter's missing policy, the definer
functions refusing another user's attempt) follows the same hand-verified pattern
as the rest of the suite until DB-backed integration tests exist.
"""

import re
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.db.models import ProctorEvent
from app.db.models.enums import AuthorVerdict, EventType, Severity
from app.schemas.proctor import (
    CLIENT_EVENT_TYPES,
    ProctorEventBatch,
    ProctorEventIn,
    ProctorSessionRead,
)
from app.services.proctoring import (
    SCORE_WEIGHTS,
    SEVERITY_BY_TYPE,
    compute_integrity_score,
    observation_summary,
)
from tests.migrations import migration_sql


def _event(
    type_: EventType,
    *,
    duration_ms: int | None = None,
    occurrences: int = 1,
    verdict: AuthorVerdict = AuthorVerdict.UNREVIEWED,
) -> ProctorEvent:
    return ProctorEvent(
        id=uuid4(),
        proctor_session_id=uuid4(),
        occurred_at=datetime.now(UTC),
        type=type_,
        severity=SEVERITY_BY_TYPE[type_],
        duration_ms=duration_ms,
        occurrences=occurrences,
        author_verdict=verdict,
        event_metadata={},
    )


# ------------------------------------------------------------- the fixed map


def test_every_event_type_has_a_severity_and_a_weight() -> None:
    """A type without a mapping is an event the server cannot classify — it would
    fail at insert (SQL) or at scoring (here), in production, on the first sitting
    that produces it."""
    assert set(SEVERITY_BY_TYPE) == set(EventType)
    assert set(SCORE_WEIGHTS) == set(EventType)


def test_the_python_map_mirrors_the_sql_map() -> None:
    """The authoritative severity map is ``public.proctor_severity()`` — it has to
    be, because the definer functions are callable over PostgREST rpc without this
    process in the path. This parses the migration and holds the mirror equal, so
    the two cannot drift apart silently."""
    when = re.compile(
        r"when\s+'(\w+)'\s+then\s+'(\w+)'::public\.severity", re.IGNORECASE
    )
    sql_map: dict[str, str] = {}
    for sql in migration_sql():
        if "proctor_severity" not in sql:
            continue
        for event_type, severity in when.findall(sql):
            sql_map[event_type] = severity

    assert sql_map, "no proctor_severity CASE found in the migrations"
    assert sql_map == {
        event_type.value: severity.value
        for event_type, severity in SEVERITY_BY_TYPE.items()
    }


def test_bookend_events_are_informational() -> None:
    """Starting and finishing a session are not observations about conduct."""
    assert SEVERITY_BY_TYPE[EventType.SESSION_START] is Severity.INFO
    assert SEVERITY_BY_TYPE[EventType.SESSION_END] is Severity.INFO
    assert SCORE_WEIGHTS[EventType.SESSION_START] == (0.0, 0.0)
    assert SCORE_WEIGHTS[EventType.SESSION_END] == (0.0, 0.0)


def test_noisy_detectors_carry_low_weight() -> None:
    """Generous to the sitter, structurally: gaze and head-pose are the noisiest
    signals and phone_visible the weakest detector (D10), so none of them may
    outweigh the observations an author actually needs surfaced first."""
    for noisy in (EventType.GAZE_AWAY, EventType.HEAD_POSE_AWAY):
        assert SEVERITY_BY_TYPE[noisy] is Severity.LOW
        assert SCORE_WEIGHTS[noisy][0] < SCORE_WEIGHTS[EventType.MULTIPLE_FACES][0]
    assert (
        SCORE_WEIGHTS[EventType.PHONE_VISIBLE][0]
        < SCORE_WEIGHTS[EventType.CAMERA_DENIED][0]
    )


# ------------------------------------------------------- the screen-share gate


def test_screen_events_mirror_their_camera_siblings() -> None:
    """An unshared screen is a sitting partly unobserved — high, like the camera
    pair — but triaged just under it: the camera says who sat, the screen only
    what was on display."""
    assert SEVERITY_BY_TYPE[EventType.SCREEN_SHARE_DENIED] is Severity.HIGH
    assert SEVERITY_BY_TYPE[EventType.SCREEN_SHARE_STOPPED] is Severity.HIGH
    assert (
        SCORE_WEIGHTS[EventType.SCREEN_SHARE_DENIED][0]
        < SCORE_WEIGHTS[EventType.CAMERA_DENIED][0]
    )
    assert (
        SCORE_WEIGHTS[EventType.SCREEN_SHARE_STOPPED][0]
        < SCORE_WEIGHTS[EventType.CAMERA_STOPPED][0]
    )


def test_multiple_displays_is_an_observation_not_an_alarm() -> None:
    """Docked laptops make a second display common; medium keeps it in the review
    queue without drowning the queue in hardware arrangements."""
    assert SEVERITY_BY_TYPE[EventType.MULTIPLE_DISPLAYS] is Severity.MEDIUM
    assert (
        SCORE_WEIGHTS[EventType.MULTIPLE_DISPLAYS][0]
        < SCORE_WEIGHTS[EventType.MULTIPLE_FACES][0]
    )


def test_screen_events_are_client_reportable() -> None:
    """The browser is the only place a share's absence can be observed, so all
    three must be reportable — unlike heartbeat_gap and clock_skew, which only
    the server can honestly derive."""
    for event_type in (
        EventType.SCREEN_SHARE_DENIED,
        EventType.SCREEN_SHARE_STOPPED,
        EventType.MULTIPLE_DISPLAYS,
    ):
        assert event_type in CLIENT_EVENT_TYPES


def test_screen_events_produce_known_arithmetic() -> None:
    """15 for a never-shared screen; a second display carries occurrence and
    duration weight (6 + 4/min), capped like everything else."""
    assert compute_integrity_score([_event(EventType.SCREEN_SHARE_DENIED)]) == 85
    three_minutes = [_event(EventType.MULTIPLE_DISPLAYS, duration_ms=180_000)]
    assert compute_integrity_score(three_minutes) == 82  # 100 - (6 + 4*3)


# ------------------------------------------------- client claims stay advisory


def test_inbound_events_have_no_severity_and_no_score_field() -> None:
    """The client has no field in which to claim 'info' — the absence is the
    enforcement, same principle as the sitter's missing view columns."""
    fields = set(ProctorEventIn.model_fields)
    assert "severity" not in fields
    assert "integrity_score" not in fields
    assert not any("score" in name for name in fields)


def test_server_detected_types_are_not_client_types() -> None:
    """heartbeat_gap is derived from silence and clock_skew from divergence; a
    client claiming either would be writing the server's observations for it."""
    assert EventType.HEARTBEAT_GAP not in CLIENT_EVENT_TYPES
    assert EventType.CLOCK_SKEW not in CLIENT_EVENT_TYPES
    assert CLIENT_EVENT_TYPES | {EventType.HEARTBEAT_GAP, EventType.CLOCK_SKEW} == set(
        EventType
    )


def test_a_batch_is_bounded() -> None:
    event = {
        "client_ref": "r1",
        "type": "tab_blur",
        "occurred_at": datetime.now(UTC).isoformat(),
    }
    with pytest.raises(ValidationError):
        ProctorEventBatch(events=[])
    with pytest.raises(ValidationError):
        ProctorEventBatch(events=[event] * 51)
    assert len(ProctorEventBatch(events=[event] * 50).events) == 50


def test_the_session_read_carries_nothing_evaluative() -> None:
    """This schema is served to the person being observed. Cadence and upload
    targets only — a score, verdict or review field appearing here is the
    beginning of the live suspicion meter the design forbids."""
    fields = set(ProctorSessionRead.model_fields)
    for forbidden in ("integrity_score", "review_status", "released_at", "reviewer_note"):
        assert forbidden not in fields


# ----------------------------------------------------------------- the score


def test_an_uneventful_session_scores_100() -> None:
    assert compute_integrity_score([]) == 100
    bookends = [_event(EventType.SESSION_START), _event(EventType.SESSION_END)]
    assert compute_integrity_score(bookends) == 100


def test_known_events_produce_known_arithmetic() -> None:
    """3 points per tab_blur occurrence + 4 per minute away: four blurs totalling
    90s -> 100 - (3*4 + 4*1.5) = 82."""
    events = [_event(EventType.TAB_BLUR, duration_ms=90_000, occurrences=4)]
    assert compute_integrity_score(events) == 82


def test_occurrences_and_duration_both_count() -> None:
    one = compute_integrity_score([_event(EventType.NO_FACE, duration_ms=10_000)])
    many = compute_integrity_score(
        [_event(EventType.NO_FACE, duration_ms=10_000, occurrences=3)]
    )
    long = compute_integrity_score([_event(EventType.NO_FACE, duration_ms=120_000)])
    assert many < one
    assert long < one


def test_one_event_cannot_sink_the_score_alone() -> None:
    """A camera pointed at a wall for four hours is one capped observation, not a
    zero — the timeline says the rest."""
    wall = [_event(EventType.NO_FACE, duration_ms=4 * 3600 * 1000)]
    assert compute_integrity_score(wall) == 75  # 100 - _PER_EVENT_CAP


def test_the_score_clamps_at_zero() -> None:
    events = [
        _event(EventType.CAMERA_DENIED),
        _event(EventType.MULTIPLE_FACES, duration_ms=300_000, occurrences=5),
        _event(EventType.HEARTBEAT_GAP, duration_ms=600_000, occurrences=4),
        _event(EventType.PASTE, occurrences=30),
        _event(EventType.PHONE_VISIBLE, duration_ms=300_000, occurrences=6),
    ]
    assert compute_integrity_score(events) == 0


def test_dismissed_events_are_excluded_when_asked() -> None:
    """The author's judgement is what a released score reflects: a dismissed
    observation leaves the number as well as the report."""
    events = [
        _event(EventType.TAB_BLUR, occurrences=2),
        _event(EventType.MULTIPLE_FACES, duration_ms=60_000, verdict=AuthorVerdict.DISMISSED),
    ]
    with_all = compute_integrity_score(events)
    released = compute_integrity_score(events, include_dismissed=False)
    assert released > with_all
    assert released == compute_integrity_score([events[0]])


# ------------------------------------------------------------------ language


def test_summaries_are_observations_not_accusations() -> None:
    """Invariant 4, held against the strings this module actually produces."""
    events = [
        _event(EventType.NO_FACE, duration_ms=42_000, occurrences=3),
        _event(EventType.TAB_BLUR, duration_ms=134_000, occurrences=6),
    ]
    lines = observation_summary(events)
    assert lines[0]["text"] == "no face for 42s (3 occurrences)"
    joined = " ".join(line["text"] for line in lines).lower()
    for accusation in ("cheat", "dishonest", "guilty", "violation", "suspicious"):
        assert accusation not in joined


def test_no_accusatory_vocabulary_in_the_proctoring_code() -> None:
    """The words that must not appear, checked in the modules that could emit
    them. Comments and docstrings are stripped; naming a rule is not breaking it."""
    import inspect

    from app.schemas import proctor as schemas_proctor
    from app.services import proctoring as services_proctoring
    from app.workers.tasks import maintenance
    from app.workers.tasks import proctoring as tasks_proctoring

    for module in (services_proctoring, schemas_proctor, tasks_proctoring, maintenance):
        source = inspect.getsource(module)
        # Strip docstrings and comments: the rule governs what the system *emits*.
        source = re.sub(r'"""[\s\S]*?"""', "", source)
        source = "\n".join(line.split("#", 1)[0] for line in source.splitlines())
        for word in ("cheat", "dishonest", "guilty", "offender"):
            assert word not in source.lower(), f"{module.__name__} contains '{word}'"
