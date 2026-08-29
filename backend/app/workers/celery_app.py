"""Celery application: four named queues and the beat schedule.

Queues are named so one workload cannot starve another (DECISIONS.md D6). A
600-page ingest must not delay the aggregation that runs the moment an exam ends.

| queue         | tasks                                | shape                          |
|---------------|--------------------------------------|--------------------------------|
| ingest        | ingest_book, delete_book             | CPU-bound, minutes, low concurrency |
| llm           | generate_assessment, grade_attempt   | IO-bound on a slow API, retry-heavy |
| proctor       | aggregate_session                    | short, bursty at exam end      |
| maintenance   | purge_evidence, sweep_stale_sessions | periodic, from beat            |
"""

from celery import Celery

from app.core.config import settings

celery_app = Celery(
    "kitaably",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[
        "app.workers.tasks.ingest",
        "app.workers.tasks.assessments",
        "app.workers.tasks.grading",
        "app.workers.tasks.proctoring",
        "app.workers.tasks.maintenance",
    ],
)

celery_app.conf.update(
    task_default_queue="ingest",
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # At-least-once delivery: a task may run twice, so every task is idempotent.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    # Longer than the worst-case ingest, or the broker will redeliver a task that
    # is still running and two workers will write the same chunks.
    broker_transport_options={"visibility_timeout": 3600},
    result_expires=3600,
    task_routes={
        "app.workers.tasks.ingest.*": {"queue": "ingest"},
        "app.workers.tasks.assessments.*": {"queue": "llm"},
        "app.workers.tasks.grading.*": {"queue": "llm"},
        "app.workers.tasks.proctoring.*": {"queue": "proctor"},
        "app.workers.tasks.maintenance.*": {"queue": "maintenance"},
    },
)

# Each entry lands in the same change that implements its task, never earlier: a
# beat entry naming a task that does not exist is a KeyError in the worker every
# interval, which trains everybody to ignore worker errors precisely until a real
# one appears. Still to come: `purge_evidence` (Phase 11).
celery_app.conf.beat_schedule = {
    # The server half of "absence is evidence": records heartbeat_gap events on
    # silent active sessions and closes abandoned ones so they still reach the
    # review queue. Every minute, matching HEARTBEAT_GAP_SECONDS — sweeping much
    # slower than the gap it detects would make the gap threshold a fiction.
    "sweep-stale-proctor-sessions": {
        "task": "app.workers.tasks.maintenance.sweep_stale_sessions",
        "schedule": 60.0,
    },
    # A worker killed mid-generation leaves its row `generating` for ever with no
    # error and no way to edit or retry — the failure handler cannot run under
    # SIGKILL. This returns such rows to draft with a reason. Five minutes: the
    # staleness threshold is fifteen, so a dead row waits at most twenty.
    "sweep-stale-generations": {
        "task": "app.workers.tasks.maintenance.sweep_stale_generations",
        "schedule": 300.0,
    },
}
