# Workflows

| File | Phase | Trigger and job |
|---|---|---|
| `ci.yml` | 0 | push + PR: lint, typecheck, test across backend, embeddings, frontend; migrations applied to a throwaway Postgres; model/schema drift check |
| `cd.yml` | 10 | merge to main: build and push images tagged with the SHA, apply Supabase migrations to the target project, `kubectl apply -k` the matching overlay, wait for rollout |
| `terraform.yml` | 10 | PR touching `infra/terraform`: fmt, validate, plan posted as a comment; on merge: apply, gated by an environment approval |

Order of operations on a release matters: **migrations first, then images.** Every
migration must be backward compatible with the currently running version, because for
a few seconds both versions are live. That is why migrations are forward-only and
additive: add a column, backfill, switch reads, drop later — never rename in place.

The schema drift check in `ci.yml` is what stops `supabase/migrations/` and
`backend/app/db/models/` describing two different databases: it boots a database from
the migrations and asserts the SQLAlchemy models still match.
