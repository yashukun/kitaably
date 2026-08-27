# infra

Nothing here is built before Phase 9. The directories exist now so the shape is
visible and each piece has an obvious home when its phase arrives
(`docs/DEPLOYMENT.md`).

| Directory | Phase | Contents |
|---|---|---|
| `k8s/` | 9 | Kustomize `base/` + `overlays/{dev,prod}` |
| `terraform/` | 10 | `modules/` + `envs/{dev,prod}`, separate state per env |
| `monitoring/` | 11 | Prometheus rules, Grafana dashboards, runbooks |

Supabase is not deployed by this repo. It is a hosted project per environment,
referenced by URL and keys.
