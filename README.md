# Kitaably

A study platform built on the books its users actually work from.

You upload the books you are studying from. They stay private to you until you share
them. Once shared they become (a) a conversational tutor anyone signed in can question,
and (b) a source for auto-generated assessments, sat under camera-based proctoring, with
the assessment's author reviewing the report before it reaches the person who sat it.

There is one kind of account. Everyone can upload, share, author an assessment, and sit
one — see [`docs/DECISIONS.md`](docs/DECISIONS.md) D16 for why roles and classrooms were
removed, and what that cost.

Design and rationale live in [`docs/`](docs/); the invariants that must not be traded
away live in [`CLAUDE.md`](CLAUDE.md).

**Status: Phases 0–6 complete.** Sign-up, sign-in, upload, share, grounded cited chat,
assessment generation and sitting all work end to end against a live database. Phase 7
(proctoring) is next; see [`docs/ROADMAP.md`](docs/ROADMAP.md).

Seeded accounts after `supabase db reset`: `amina@kitaably.test` and
`ravi@kitaably.test`, password `Passw0rd!123`.

---

## Running it

Two commands, because Supabase runs its own stack (`docs/DECISIONS.md` D3).

```bash
cp .env.example .env       # fill the keys `supabase start` prints
supabase start             # Postgres+pgvector, Auth, Storage, Studio
docker compose up --build  # backend, worker, beat, embeddings, redis, ollama, frontend
```

| Service | URL |
|---|---|
| frontend | http://localhost:3000 |
| backend docs | http://localhost:8000/docs |
| backend readiness | http://localhost:8000/ready |
| embeddings | http://localhost:8001/health |
| Supabase Studio | http://localhost:54323 |
| Supabase API | http://localhost:54321 |
| Ollama | http://localhost:11434 |

`make help` lists every target. To work outside containers: `make sync`, then
`make dev-backend`, `make dev-embeddings`, `make dev-frontend`.

Requires the [Supabase CLI](https://supabase.com/docs/guides/local-development),
Docker, `uv`, and Node 20+. The backend answers `/health` without Supabase running;
`/ready` reports `degraded` until Postgres is up, which is the correct behaviour and
worth seeing once.

---

## The tree

```
.
├── backend/                    FastAPI API + Celery workers — one image, two roles
│   ├── app/
│   │   ├── main.py             app wiring: logging, middleware, CORS, handlers, routers
│   │   ├── core/
│   │   │   ├── config.py       pydantic Settings — the one place env is read
│   │   │   ├── logging.py      JSON logs + the request_id carried through the request
│   │   │   ├── errors.py       DomainError hierarchy + the single HTTP mapping
│   │   │   ├── metrics.py      Prometheus registry
│   │   │   ├── middleware.py   request id, access log, latency histogram
│   │   │   ├── security.py     JWT verification vs JWKS; the Principal
│   │   │   └── deps.py         auth guards — require_auth, require_book_owner, …
│   │   ├── db/
│   │   │   ├── base.py         DeclarativeBase + shared mixins
│   │   │   ├── session.py      async engine; request-path vs worker-path sessions
│   │   │   └── models/         SQLAlchemy models mirroring supabase/migrations/
│   │   ├── schemas/            Pydantic Create / Update / Read contracts
│   │   ├── api/
│   │   │   ├── health.py       /health, /ready, /metrics  (unversioned)
│   │   │   ├── router.py       the /api/v1 aggregator
│   │   │   └── v1/             one router per resource
│   │   ├── services/           business logic — where decisions live
│   │   ├── rag/
│   │   │   ├── parse.py        per-format parsers behind one registry
│   │   │   ├── chunk.py        chapter detection and chunking
│   │   │   ├── embed.py        batched calls to the embeddings service
│   │   │   ├── retrieve.py     ← build_retrieval_filter(): THE scoping chokepoint
│   │   │   ├── formats.py      14 question formats → 6 grading families
│   │   │   └── prompts.py      grounding + observation-not-accusation contracts
│   │   ├── clients/            outbound: embeddings, llm, storage — one file each
│   │   └── workers/
│   │       ├── celery_app.py   four named queues + the beat schedule
│   │       └── tasks/          thin wrappers over services; idempotent
│   ├── tests/                  test_scoping.py is the phase-3 exit criteria
│   ├── Dockerfile              multi-stage, non-root; API and worker share it
│   └── pyproject.toml
│
├── embeddings/                 standalone CPU embedding service (bge-small-en-v1.5)
│   ├── app/
│   │   ├── main.py             /health, /ready, /metrics, POST /embed
│   │   ├── model.py            ONNX load + encode off the event loop
│   │   ├── schemas.py
│   │   └── config.py
│   ├── Dockerfile              weights are NOT baked in — they land in a volume
│   └── pyproject.toml
│
├── frontend/                   Next.js App Router, TypeScript, Tailwind, shadcn/ui
│   ├── app/
│   │   ├── page.tsx            landing
│   │   ├── (auth)/             /login, /signup
│   │   ├── (app)/              dashboard, books, chat, assessments — one shell
│   │   └── exam/[token]/       the runner — outside the shell, by design
│   ├── components/             site-nav, book-list, chat-panel, scope-chip, glass
│   │                           question-input.tsx: one renderer per grading family
│   ├── lib/
│   │   ├── api/                one typed client — no fetch calls in components
│   │   └── supabase/           browser, server, and session-refresh clients
│   ├── proxy.ts                refreshes the session cookie on every request
│   ├── next.config.ts          /api/backend/:path* → backend /api/v1/:path*
│   └── Dockerfile              standalone output; NEXT_PUBLIC_* are build args
│
├── supabase/                   ← the schema lives here
│   ├── config.toml             the local CLI stack
│   ├── migrations/             forward-only SQL; RLS ships with its table
│   └── seed.sql                test accounts and sample material
│
├── bruno/                      every endpoint, runnable — `bru run --env local -r`
│   ├── 00-auth … 08-cleanup/   numbered because each folder feeds the next
│   └── 07-boundaries/          nine requests that pass by being refused
│
├── infra/                      Phases 9–11; READMEs describe what lands where
│   ├── k8s/                    kustomize base/ + overlays/{dev,prod}
│   ├── terraform/              modules/ + envs/{dev,prod}
│   └── monitoring/             prometheus rules, dashboards, runbooks
│
├── .github/workflows/          ci.yml, cd.yml, terraform.yml
├── docs/                       architecture, data model, roadmap, decisions, deployment
├── .claude/skills/             subsystem playbooks — read before touching that area
├── docker-compose.yml          the services this repo owns (not Supabase)
├── Makefile
└── .env.example
```

## Where the rules live

| Question | File |
|---|---|
| What must never be traded away | [`CLAUDE.md`](CLAUDE.md) |
| How a request flows, and across which trust boundaries | [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) |
| Tables, columns, enums, indexes, RLS intent | [`docs/DATA-MODEL.md`](docs/DATA-MODEL.md) |
| What to build next | [`docs/ROADMAP.md`](docs/ROADMAP.md) |
| Why it was built that way, and what reversal costs | [`docs/DECISIONS.md`](docs/DECISIONS.md) |
| Containers → Kubernetes → Terraform → CI → monitoring | [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) |
| How to add a route without inventing a new shape | [`.claude/skills/api-conventions`](.claude/skills/api-conventions/SKILL.md) |
| What every endpoint does, runnable | [`bruno/`](bruno/README.md) |

Four files carry more weight than their size suggests:

- [`backend/app/rag/retrieve.py`](backend/app/rag/retrieve.py) — the only place a
  predicate over `chunks` is built. A personal book stays personal because of this
  file.
- [`backend/app/core/deps.py`](backend/app/core/deps.py) — every route declares a
  guard from here. A route with no guard is a review failure.
- [`backend/tests/test_scoping.py`](backend/tests/test_scoping.py) — the cases that
  must pass before any scoping change merges. The one that matters most asserts that
  two users' filters are identical once their ids are masked: there is no account
  whose reach is wider, so there is no account to escalate into.
- [`backend/app/rag/formats.py`](backend/app/rag/formats.py) — the fourteen question
  formats and the six grading families they mark through. Postgres holds the same
  mapping as a check constraint, because a paper drawn as one thing and marked as
  another scores zero for everybody who sat it while looking completely normal.
