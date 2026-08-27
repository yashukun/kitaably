"""Prometheus metrics.

Request latency and count come free from middleware. Everything else is added by
the feature that needs it, on one rule: add a metric where a number would answer
"is this working". Refusal rate is the example worth remembering — a grounded
refusal is correct behaviour, so it must be counted separately from an error, or
the dashboard will read a working tutor as a broken one.
"""

from prometheus_client import CollectorRegistry, Counter, Histogram
from prometheus_client.core import REGISTRY

registry: CollectorRegistry = REGISTRY

http_requests_total = Counter(
    "kitaably_http_requests_total",
    "HTTP requests by method, path template, and status.",
    ["method", "path", "status"],
)

http_request_duration_seconds = Histogram(
    "kitaably_http_request_duration_seconds",
    "HTTP request latency by method and path template.",
    ["method", "path"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

# A grounded refusal is correct behaviour, not a failure. Counting it as an error
# would make a working tutor read as a broken one on every dashboard, and the first
# instinct on seeing the graph would be to loosen the threshold -- which is the one
# change that breaks the product's central promise.
retrieval_results_total = Counter(
    "kitaably_retrieval_results_total",
    "Retrievals that found material above the relevance threshold.",
)
retrieval_refusals_total = Counter(
    "kitaably_retrieval_refusals_total",
    "Retrievals that found nothing above threshold, producing a grounded refusal.",
)
llm_calls_total = Counter(
    "kitaably_llm_calls_total",
    "LLM calls by model and outcome.",
    ["model", "outcome"],
)
chat_rate_limited_total = Counter(
    "kitaably_chat_rate_limited_total",
    "Chat requests refused by the per-user rate limit.",
)

# Labelled by `source` -- "rules", "model" or "default" -- because the useful question
# is not just what readers ask but how often the cheap path was enough. If `model`
# climbs, the lexicon has drifted from how people actually type; if `default` climbs,
# the classifier is failing and nobody would otherwise notice, because failing to
# QUESTION looks exactly like working.
chat_intents_total = Counter(
    "kitaably_chat_intents_total",
    "Chat messages by classified intent and how the label was decided.",
    ["intent", "source"],
)

# A turn that reached the books and got narrowed to one of them. Counted against
# `retrieval_results_total` this gives the routing rate: if almost nothing is ever
# routed, the dominance threshold is too high to be doing anything.
chat_routed_total = Counter(
    "kitaably_chat_routed_total",
    "Answers narrowed to a dominant book by the retrieval vote.",
)

# Labelled by shape -- "focused", "overview", "lookup", "compare" (app/rag/shape.py).
# The share tells whether the non-focused paths earn their keep, and a shape that
# never fires means its rules have drifted from how readers actually ask.
chat_query_shapes_total = Counter(
    "kitaably_chat_query_shapes_total",
    "Retrieval-bound chat questions by query shape.",
    ["shape"],
)

# --- Phase 5-6 --------------------------------------------------------------
assessments_rate_limited_total = Counter(
    "kitaably_assessments_rate_limited_total",
    "Generation requests refused by the per-user rate limit.",
)
# A rejected question is the validator working, not the generator failing. Counted
# separately from an error for the same reason a grounded refusal is: a dashboard
# that conflates them invites somebody to loosen the validator.
questions_rejected_total = Counter(
    "kitaably_questions_rejected_total",
    "Generated questions refused by validation, by reason.",
    ["reason"],
)
questions_generated_total = Counter(
    "kitaably_questions_generated_total",
    "Generated questions that passed validation and dedupe.",
)
attempts_graded_total = Counter(
    "kitaably_attempts_graded_total",
    "Attempts graded, by outcome.",
    ["outcome"],
)

# --- Phase 7 ----------------------------------------------------------------
# By type, because the useful question is which detectors actually fire. A type
# that never appears means its detector or its debounce is broken; a type that
# dominates means its threshold is not generous enough to the sitter — and the
# fix for that is tuning, not a lower review standard.
proctor_events_recorded_total = Counter(
    "kitaably_proctor_events_recorded_total",
    "Proctoring observations accepted, by event type.",
    ["type"],
)
proctor_sessions_aggregated_total = Counter(
    "kitaably_proctor_sessions_aggregated_total",
    "Proctor sessions scored after close, by how the session ended.",
    ["status"],
)

# --- Phase 3+ ---------------------------------------------------------------
# task_duration_seconds       by queue and task name
# ingest_failures_total       by reason
# llm_tokens_total            by model and direction, for the cost dashboard

__all__ = [
    "registry",
    "http_requests_total",
    "http_request_duration_seconds",
    "retrieval_results_total",
    "retrieval_refusals_total",
    "llm_calls_total",
    "chat_rate_limited_total",
    "chat_intents_total",
    "chat_routed_total",
    "chat_query_shapes_total",
    "assessments_rate_limited_total",
    "questions_rejected_total",
    "questions_generated_total",
    "attempts_graded_total",
    "proctor_events_recorded_total",
    "proctor_sessions_aggregated_total",
]
