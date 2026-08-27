# Monitoring — Phase 11

Nothing new; everything sturdier.

```
prometheus/     scrape config and alerting rules
grafana/        dashboards as JSON
runbooks/       one file per alert that can fire
```

Dashboards worth having: request latency p50/p95/p99, error rate, Celery queue depth
and task duration by queue, LLM calls and estimated cost, retrieval hit rate vs.
refusal rate, ingest success rate.

Refusals are counted separately from errors. A grounded refusal is correct behaviour
— folding the two together makes a working tutor read as a broken one.

Alerts worth waking up for: ingest failure rate above threshold, a review queue with
attempts older than N days (a stalled review gate is a student waiting on a result),
`/ready` failing across replicas, LLM spend above a daily ceiling.

`audit_log` is not monitoring. It must not be sampled, truncated, or shipped to a
lossy pipeline — it is the record that a human decided.

## Runbooks to write as they become real

- Ingest queue backing up
- Embeddings pod restarting (model load timeout)
- Supabase project paused or over quota
- LLM provider outage during an exam window
- Restore from backup, including re-embedding from `chunks.text`
