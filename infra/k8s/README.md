# Kubernetes — Phase 9

No product change. The same app, running on a cluster.

```
base/                     plain YAML, no templating (DECISIONS.md D14)
  namespace.yaml
  configmap.yaml          non-secret config, mirrors .env.example keys
  secret.example.yaml     shape only; the real Secret never lives in git
  backend/                deployment, service, hpa
  worker/                 one deployment per queue, no service
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

`kubectl apply -k infra/k8s/overlays/dev`.

An overlay may patch replicas, resources, images, env, and hostnames. If an overlay
needs to patch *behaviour*, the difference belongs in config, not in a manifest.

## Probes

| Service | liveness | readiness |
|---|---|---|
| backend | `GET /health` | `GET /ready` (Postgres, Redis, embeddings) |
| embeddings | `GET /health` | `GET /ready` (model loaded) |
| frontend | `GET /` | `GET /` |
| worker | `celery inspect ping` | none — workers take no traffic |

The distinction matters most for `embeddings`: the model takes seconds to load. A
readiness probe keeps traffic away until it is; a liveness probe firing in that
window would restart the pod forever. Set `initialDelaySeconds` and
`failureThreshold` accordingly.
