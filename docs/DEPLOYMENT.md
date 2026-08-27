# Deployment

The path from a laptop to a cluster, in the order it should be walked. Nothing here is
built before Phase 9 — the directories exist now so the shape is visible, and so each
piece has an obvious home when its phase arrives.

## The four artefacts

| Artefact | Built from | Runs as |
|---|---|---|
| `kitaably-backend` | `backend/Dockerfile` | Deployment `backend`, serves `:8000` |
| `kitaably-worker` | `backend/Dockerfile` (same image, different command) | Deployments `worker-ingest`, `worker-llm`, `beat` |
| `kitaably-embeddings` | `embeddings/Dockerfile` | Deployment `embeddings`, serves `:8001` |
| `kitaably-frontend` | `frontend/Dockerfile` | Deployment `frontend`, serves `:3000` |

The worker shares the backend image deliberately: same code, same models, same
migrations, one build. Only the entrypoint and the resource profile differ. Splitting the
image is a later optimisation, and a reversible one.

Supabase (Postgres, Auth, Storage) is **not** deployed by this repo. It is a hosted
project per environment, referenced by URL and keys.

## Image rules

- Multi-stage builds; the final stage carries no compiler and no package manager cache.
- Pinned base images by digest in `prod`, by tag in `dev`.
- Non-root user, read-only root filesystem where possible, no shell in the final layer if
  it can be avoided.
- One process per container. No supervisors, no `&`.
- Tag with the git SHA. `latest` is not a deployment target.
- Config from environment, secrets from a Secret — never baked into a layer. A key in an
  image layer is a key in every registry copy of that image, forever.

## Kubernetes layout

```
infra/k8s/
  base/                     the objects themselves — plain YAML, no templating
    namespace.yaml
    configmap.yaml          non-secret config, mirrors .env.example keys
    secret.example.yaml     shape only; the real Secret never lives in git
    backend/                deployment, service, hpa
    worker/                 deployment per queue, no service
    embeddings/             deployment, service, hpa
    frontend/               deployment, service
    redis/                  deployment, service, pvc
    ingress.yaml
    kustomization.yaml
  overlays/
    dev/                    1 replica, small requests, dev image tag, dev host
    prod/                   HA replicas, real limits, pinned digests, prod host
  charts/                   reserved — see DECISIONS.md D14
```

Apply with `kubectl apply -k infra/k8s/overlays/dev`. An overlay may patch replicas,
resources, images, env, and hostnames. If an overlay needs to patch *behaviour*, the
difference belongs in config, not in a manifest.

### Probes

| Service | liveness | readiness |
|---|---|---|
| backend | `GET /health` | `GET /ready` (Postgres, Redis, embeddings) |
| embeddings | `GET /health` | `GET /ready` (model loaded) |
| frontend | `GET /` | `GET /` |
| worker | `celery inspect ping` | none — workers take no traffic |

The distinction matters most for `embeddings`: the model takes seconds to load. A
readiness probe keeps traffic away until it is loaded; a liveness probe that fired during
that window would restart the pod forever. Set `initialDelaySeconds` and
`failureThreshold` with that in mind.

### Scaling

- `backend`: HPA on CPU and request concurrency.
- `worker-ingest`: HPA on queue depth (KEDA later, or a fixed count first). Ingest is
  CPU-bound and bursty — a class uploading at the start of term.
- `worker-llm`: low concurrency, high timeout. Scaling it does not make a slow provider
  faster; it only avoids head-of-line blocking.
- `embeddings`: HPA on CPU, generous memory requests, slow start.

## Terraform layout

```
infra/terraform/
  modules/
    network/        VPC, subnets, NAT, security groups
    eks/            cluster, node groups, addons, OIDC provider
    ecr/            one repository per image, lifecycle policy
    irsa/           IAM roles for service accounts (least privilege per workload)
    observability/  prometheus/grafana or managed equivalents
  envs/
    dev/            backend.tf (remote state), main.tf, terraform.tfvars.example
    prod/           same modules, separate state, different sizing
```

Rules:

- Remote state in S3 with DynamoDB locking, created once by hand (the bootstrap
  chicken-and-egg), then never destroyed by a plan.
- `dev` and `prod` are separate state files that share modules. Never one workspace with
  a `count` on the environment.
- No secret values in `.tf` or `.tfvars` that are committed. Secrets live in SSM Parameter
  Store / Secrets Manager and are read by the cluster.
- `terraform plan` output is a review artefact: it goes on the pull request.

## CI/CD

```
.github/workflows/
  ci.yml          on push + PR: lint, typecheck, test (backend, embeddings, frontend),
                  migrations applied to a throwaway Postgres, model/schema drift check
  cd.yml          on merge to main: build + push images tagged with the SHA,
                  apply Supabase migrations to the target project,
                  kubectl apply -k the matching overlay, wait for rollout
  terraform.yml   on PR touching infra/terraform: fmt, validate, plan (comment),
                  on merge: apply, gated by an environment approval
```

Order of operations on a release matters: **migrations first, then images**. Every
migration must be backward compatible with the currently running version, because for a
few seconds both versions are live. That constraint is why migrations are forward-only
and additive: add a column, backfill, switch reads, drop later — never rename in place.

## Secrets

| Where | What |
|---|---|
| `.env` (gitignored) | local only |
| GitHub Actions secrets | registry credentials, AWS role to assume, Supabase access token |
| AWS SSM / Secrets Manager | Supabase service-role key, OpenAI key, app secrets |
| Kubernetes Secret | projected from SSM (External Secrets Operator or a sync step) |

The service-role key is the crown jewel: it bypasses RLS. It belongs to the backend and
worker only, never to the frontend, never to a `NEXT_PUBLIC_*` variable, and never in a
browser bundle. Rotate it if it is ever printed in a log.

## Monitoring

- **Metrics** — Prometheus scrapes `/metrics` on backend and embeddings. Dashboards:
  request latency (p50/p95/p99), error rate, Celery queue depth and task duration by
  queue, LLM calls and estimated cost, retrieval hit rate vs. refusal rate, ingest
  success rate.
- **Logs** — JSON to stdout, collected by the cluster's agent. Every line carries
  `request_id`; task logs carry the `request_id` that enqueued them.
- **Alerts** worth waking up for: ingest failure rate above threshold, review queue with
  attempts older than N days (a stalled review gate is a person waiting on a result),
  `/ready` failing across replicas, LLM spend above a daily ceiling.
- **Audit** — `audit_log` is not monitoring and must not be sampled, truncated, or
  shipped to a lossy pipeline. It is the record that a human decided.

## Runbook stubs

Write these as they become real, in `infra/monitoring/runbooks/`:

- Ingest queue backing up
- Embeddings pod restarting (model load timeout)
- Supabase project paused or over quota
- LLM provider outage during an exam window
- Restore from backup, including re-embedding from `chunks.text`
