# Roadmap

Phased so each phase ends with something demonstrable and `docker compose up` still
works. Do not start a phase before its predecessor's exit criteria hold — particularly
the scoping tests in Phase 3, which everything downstream depends on being correct.

Every phase carries two tracks:

- **Ship** — the product increment.
- **Learn** — the operations layer that increment justifies. Skipping the Learn track
  makes the last three phases enormous instead of routine.

Estimates assume ~8 h/week, solo. They are ranges because the DevOps track is where
unfamiliar work lives.

> **Re-scoped after Phase 4.** Roles and classrooms were removed (DECISIONS.md D16).
> There is one kind of account and one shared library. Phases 5–8 below still describe
> the work accurately; read "teacher" as *the assessment's author* and "student" as
> *whoever is sitting it* — both are relationships to one paper, not account types.

---

## Phase 0 — Skeleton and local platform · ~1 week

**Ship.** The repository as it stands: folder structure, stub modules, configuration,
`docker-compose.yml`, and this documentation set. Nothing implements a feature.

**Learn.** Containers and the local platform.
- `git init`; first commit
- `supabase start` bringing up Postgres + pgvector, Auth, Storage, Studio
- Compose bringing up backend, worker, beat, embeddings, redis, ollama, frontend
- `/health` and `/ready` on backend and embeddings; compose healthchecks wired to them
- GitHub Actions: lint + typecheck + test on every push (they pass trivially at first)

**Exit:** `supabase start && docker compose up` gives a rendered home page, a green
`/ready`, and a green CI run.

---

## Phase 1 — Identity · ~1–2 weeks  ·  **done**

**Ship.**
- Supabase Auth, email + password. Sign-up and sign-in are one page each, for everybody.
- `profiles` populated by a trigger on signup; it copies `name` and nothing else out of
  client-supplied metadata
- FastAPI JWT verification against JWKS, key set cached, algorithm taken from the key
  rather than the token header
- `Principal`, and the guards: `require_auth`, `require_book_owner`, `allow_anonymous`
- One signed-in shell at `(app)`, with middleware redirecting the signed-out away from it

**Learn.** Secrets and configuration.
- Every key in `.env.example`, nothing hardcoded, service-role key backend-only
- Structured JSON logging with a request id from the first request

**Exit:** met. Two accounts, a route that refuses an unauthenticated call, and a log line
traceable from browser to handler. Verified further: an account whose JWT literally
carries `user_metadata.role = "superadmin"` is reported by `/api/v1/me` as an ordinary
user, because the role is read from `profiles` and the token is only asked for `sub`.

**Not done:** OAuth (open question 9). It needs client credentials from a provider that
no amount of local configuration substitutes for, so it is deferred rather than
half-built.

---

## Phase 2 — The sharing boundary · ~1 week  ·  **done, and re-scoped**

Originally "classrooms and enrollment". The classroom was removed mid-build
(DECISIONS.md D16), and what this phase actually delivers is the boundary that survived
it: private by default, shared on purpose.

**Ship.**
- One kind of account. No role guard exists, and nothing reads `profiles.role`.
- Every upload lands `personal`, whoever made it
- `PATCH /books/{id}/scope` — the owner shares or unshares; audited in both directions
- RLS reduced to one rule on `books` and `chunks`: `owner_id = auth.uid() OR scope = 'canon'`
- A books screen with two shelves, and Share/Delete offered only to a book's owner

**Learn.** Migrations as a discipline — forward-only, reviewed, applied to a throwaway
database before merge. This phase is also where the *cost* of that discipline showed up:
sharing failed at runtime because `propagate_book_scope()` ran with invoker rights
against a table `authenticated` may only read. The fix is its own migration
(`20260824133000`) rather than an edit to the one that had already been applied.

**Exit:** met, and proven three ways rather than by clicking —
1. at the database, by adopting each user's claims and reading `books` and `chunks`
2. through the API, where a non-owner gets 404 on GET, PATCH and DELETE alike
3. through the browser, where sharing a book makes it appear for another signed-in user
   and unsharing removes it again

**Not done:** `next_cursor` is still always null, and there are no DB-backed integration
tests — the scoping suite asserts on the compiled predicate, and the RLS behaviour behind
it was verified by hand.

---

## Phase 3 — Ingestion and the scoping boundary · ~2–3 weeks

The most important phase in the build. Everything else assumes it is right.

**Ship.**
- Upload to Supabase Storage with size and page caps, format sniffing
- Parsers: PDF (PyMuPDF), DOCX, PPTX, TXT/MD, behind one registry keyed by format
- Celery `ingest` queue: parse → chapters → chunk → embed → store
- The embeddings service called over HTTP, batched
- `books`, `chapters`, `chunks` with `vector(384)` and an HNSW index
- `build_retrieval_filter()` as the sole predicate constructor
- Status surfaced in the UI **including failures**, with a retry
- Delete path removes rows and the storage object in one task

**Learn.** Asynchronous work and its failure modes.
- Celery queues, retries with backoff, idempotent task design
- Worker concurrency vs. CPU on your machine; watch a 600-page PDF actually ingest
- Task metrics: duration by queue, failure counter

**Exit:** a shared book and a private book both indexed, plus a passing test suite
proving: nobody retrieves another user's personal chunks; assessment generation sees no
personal chunk at all; unsharing revokes access on the next request; and the same holds
on the worker path, where RLS does not apply.

---

## Phase 4 — Grounded chat · ~2 weeks

**Ship.** Chat sessions and messages; retrieve → prompt → stream over SSE; citations with
page deep-links and a canon/personal label; grounded refusal below threshold; per-user
rate limiting in Redis.

**Learn.** Streaming through a proxy — SSE through the Next.js rewrite, buffering
surprises, and why `/metrics` should count refusals separately from errors.

**Exit:** someone asks a chapter question and gets a cited answer; asks something
outside the material and gets a refusal rather than a guess.

---

## Phase 5 — Assessment generation · ~2 weeks  ·  **done**

**Ship.**
- `assessments` and `questions`, with the answer key reachable only through a view
- Coverage-stratified chunk sampling from **canon books only**, never similarity
- Strict JSON generation in batches, validated question by question, then deduped by
  stem embedding
- An author review screen showing every question with its answer key, rubric,
  provenance and origin, plus delete and publish
- Publishing freezes `max_score` and mints a share token

**Learn.** Slow, expensive, unreliable dependencies. A batch that will not parse or
will not validate is dropped and the remaining batches still produce a paper — one bad
batch must not fail the whole thing. Rejections are counted separately from errors,
because a rejected question is the validator working.

**Exit:** met. A book becomes a mixed paper in about a minute on CPU-only Ollama;
questions that refer to "the passage", duplicate an option, or cite a chunk that was
not in their batch are refused before they reach the database.

**Worth knowing:** generation returns *fewer* questions than asked for when the source
is thin, deliberately. Four were requested from a three-paragraph book and two were
kept. Inventing the other two is the failure mode this trades against.

### Phase 5b — the format taxonomy  ·  **done**

**Ship.**
- Fourteen question formats over six grading families, plus Bloom's six cognitive
  levels and nine levels of rigor (`docs/DECISIONS.md` D25)
- A picker on the create form where **every part is skippable** — choosing nothing
  means auto, and the server picks a mix suited to the material
- Deterministic partial credit for select-all, match-the-following and ordering
- Renderers for all fourteen in the exam runner, the author's review, and the released
  result

**Learn.** Where a taxonomy stops. The catalogue this came from ran to about two hundred
items; sorted, it was three axes wearing one coat, and about a hundred and ninety of the
two hundred were the same shape with a different instruction. The interesting design work
was deciding what *not* to model — and then deciding that the shape and the marking
should be two columns rather than one, so fourteen presentations can share six testable
marking paths.

**Exit:** met for everything except an end-to-end run against a live database — the
suites cover the registry, both copies of the format→family mapping, the shape builder
and every partial-credit rule, but the migration itself has not been applied yet.

**Worth knowing:** batching moved from one LLM call per chunk-batch to one per *format*
per chunk-batch. Three JSON shapes in one reply is more than a 3B model on a laptop
reliably manages, and it also means a batch that will not parse costs one format's
worth of questions rather than the paper's.

---

## Phase 6 — Taking an assessment, unproctored · ~2 weeks  ·  **done**

**Ship.**
- Publish → share URL. **The link is the whole access grant** — no roster, no
  invitation. Sitting requires an account only so a result has somebody to belong to,
  and a signed-out visitor is returned to the exam after signing in.
- Attempt lifecycle with a server-authoritative deadline, fixed once at start
- Autosave per question, debounced, flushed on submit
- MCQ graded deterministically; written answers graded by LLM against the stored
  rubric, with the total recomputed from its parts and clamped
- Author override → `grader='human'`, `llm_rationale` preserved, score re-derived,
  `audit_log` row
- `results_release` respected: `immediate` releases at grading, `on_review` waits for
  the author

**Learn.** Time and correctness — and one lesson that was not on the list. The RLS
context is transaction-local, so a route that commits and then reads again queries as
*nobody*: no error, zero rows, and an exam that opens with no questions on it. Read
before committing; commit early only when a task must see the row.

**Exit:** met, and driven in a browser end to end — a cold visitor on a share link
signs in, sits the paper, hands it in, and sees marks and feedback once released;
the author sees the gradebook and can overrule any mark.

**Not done:** retakes (one attempt per person, open question 2), and question editing
is delete-and-rewrite rather than in-place. Password-protected links were dropped
rather than left half-built (DECISIONS.md D17).

---

## Phase 7 — Proctoring capture · ~2–3 weeks

**Ship.** Consent screen; camera permission and baseline still; MediaPipe loop with
debounced heuristics; event batching and heartbeats; signed-URL still upload for
high-severity events; server-side severity mapping and integrity scoring; graceful
degradation on denial, camera stop, and network loss.

**Learn.** Untrusted clients and object storage: signed upload URLs, bucket policies,
retention TTLs, and why absence of data is itself data.

**Exit:** a completed attempt produces a coherent event timeline, and deliberately
looking away, a second face entering frame, or switching tabs each show up correctly.

---

## Phase 8 — The review gate · ~1–2 weeks

The phase that makes the product defensible.

**Ship.** An author's review queue ordered by weighted severity; timeline UI with
evidence stills and per-event dismiss/uphold; reviewer note; `release` / `clear` /
`void`; the sitter's report visible only after release, upheld events only; copy audit
for accusatory language.

**Learn.** Audit trails: append-only tables, actor attribution, and retention that
differs from application logs.

**Exit:** a flagged attempt where the author dismisses two events and upholds one, and
the person who sat it sees exactly the upheld one plus the reviewer's note — and saw
nothing at all before that.

---

## Phase 9 — Kubernetes · ~2–3 weeks

**Ship.** No product change. The same app, running on a cluster.

**Learn.**
- Multi-stage, non-root, pinned-base images for backend, worker, embeddings, frontend
- Kustomize `base/` with Deployments, Services, ConfigMap, Secret, Ingress, HPA
- `overlays/dev` and `overlays/prod` differing only in replicas, resources, and env
- Liveness vs. readiness probes wired to `/health` and `/ready` — and understanding why
  the difference matters when the embeddings service is slow to load its model
- Local cluster first (`kind` or Docker Desktop), so a broken manifest costs nothing

**Exit:** `kubectl apply -k infra/k8s/overlays/dev` on a local cluster serves the app,
and killing a pod does not lose a request.

---

## Phase 10 — Terraform and continuous delivery · ~3–4 weeks

**Ship.** Nothing user-visible. A deployed environment.

**Learn.**
- Terraform: VPC, EKS, ECR, IAM roles for service accounts, remote state with locking
- `envs/dev` and `envs/prod` as separate state, sharing modules
- GitHub Actions: build and push images on merge, `terraform plan` on PR with the plan
  posted as a comment, `apply` gated on approval, then `kubectl apply -k`
- Supabase hosted project per environment; migrations applied by the pipeline, not by you

**Exit:** a commit to `main` reaches a running EKS cluster with no manual step, and
`terraform destroy` on dev leaves nothing behind but the state bucket.

---

## Phase 11 — Observability and hardening · ~2–3 weeks

**Ship.** Nothing new; everything sturdier.

**Learn.**
- Prometheus scraping `/metrics`; Grafana dashboards for latency, queue depth, LLM cost
- Alerts that mean something: ingest failure rate, review queue age, error budget
- Evidence purge job verified against `evidence_purge_after`
- Deletion cascades verified across Postgres and Storage
- Rate limits and cost ceilings on every LLM path
- Backup and restore drill on the Supabase project; re-embed from `chunks.text`
- Load check: 600-page book ingest, 30 concurrent exam sessions

**Exit:** delete an account and prove nothing of it remains in Postgres or Storage; then
restore last night's backup into a scratch project and prove chat still answers.

---

## Explicitly out of scope for v1

Live sessions, video recording (stills only), audio monitoring, mobile apps,
plagiarism detection across submissions, LMS/SIS integration, multi-tenant billing,
non-English material, OCR for scanned documents, and screen recording.
