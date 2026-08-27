# Architecture

## System shape

```
                      Browser  (one kind of account)
                        │
      Supabase JS ──────┤ sign-in, OAuth, session refresh  ──►  Supabase Auth
      (cookie session)  │
                        │ fetch /api/backend/*  (Bearer access token)
                        ▼
            ┌──────────────────────────────────────┐
            │ Next.js  :3000                       │
            │  App Router, RSC + client UI         │
            │  rewrite /api/backend/:path*         │
            └──────────────────┬───────────────────┘
                               │  →  /api/v1/:path*
                               ▼
            ┌──────────────────────────────────────┐
            │ FastAPI  :8000                       │
            │  api/v1 → services → db              │
            │  JWT verified against Supabase JWKS  │
            │  /health  /ready  /metrics           │
            └───┬──────────┬───────────┬───────────┘
                │ asyncpg  │ redis     │ httpx
                ▼          ▼           ▼
   ┌────────────────────┐ ┌──────────┐ ┌──────────────────┐
   │ Supabase Postgres  │ │ Redis    │ │ embeddings :8001 │
   │  rows + chunk text │ │ broker + │ │  bge-small-en    │
   │  + pgvector(384)   │ │ results  │ │  CPU, ONNX       │
   ├────────────────────┤ └────┬─────┘ └──────────────────┘
   │ Supabase Storage   │      │
   │  books/ evidence/  │      ▼
   ├────────────────────┤ ┌──────────────────────────┐     ┌────────────────────┐
   │ Supabase Auth      │ │ Celery worker + beat     │────►│ LLM (OpenAI-compat)│
   │  GoTrue, JWKS      │ │ ingest · generate ·      │     │ Ollama :11434 dev  │
   └────────────────────┘ │ grade · aggregate · purge│     │ api.openai.com prod│
                          └──────────────────────────┘     └────────────────────┘
```

### Where data lives, and what is rebuildable

| Data | Home | Rebuildable? |
|---|---|---|
| users | Supabase Postgres (`auth.users` + `public.profiles`) | no |
| chunk **text** and **embedding** | Supabase Postgres (`chunks.text`, `chunks.embedding`) | embeddings yes, from text |
| source documents, evidence stills | Supabase Storage (private buckets) | no |
| task state, rate-limit counters, cache | Redis | yes — Redis is disposable |
| model weights | embeddings service image / volume | yes |

One database holds rows, text, and vectors. There is no second store that can silently
disagree with the first — the single most common failure mode of the earlier
Postgres + separate-vector-DB design was an orphaned vector surviving a deleted book.

## Environments

| | local | dev (cluster) | prod (cluster) |
|---|---|---|---|
| Postgres / Auth / Storage | Supabase CLI stack (`supabase start`) | Supabase hosted project | Supabase hosted project |
| Backend, worker, embeddings, frontend | Docker Compose | EKS, Kustomize `overlays/dev` | EKS, Kustomize `overlays/prod` |
| Redis | compose service | in-cluster Deployment | in-cluster (or ElastiCache) |
| LLM | Ollama container | Ollama Deployment or OpenAI | OpenAI |
| Secrets | `.env` | Kubernetes Secret from SSM | Kubernetes Secret from SSM |

Same images, same manifests, different overlay. If something works only under Compose,
it is not finished.

## Trust boundaries

| Boundary | Trusted? | Consequence |
|---|---|---|
| Browser → API | **No** | Recompute integrity scores server-side. Never accept `scope`/`book_id`/`owner_id` from the client as authorization. |
| Supabase JWT | Signature yes, claims almost entirely not | Signature and expiry are verified against JWKS. **Exactly one field is lifted out: `sub`.** Everything else about the caller is read from `profiles`, because a user can rewrite their own `user_metadata` with one authenticated request. |
| Exam share link | Token only | Bearer of the token gets *access to attempt*, not identity. Identity comes from the session. |
| Backend → LLM | Outbound | Book content leaves the network when a hosted provider is configured. Say so in the upload consent copy. Ollama keeps it local. |
| Worker → Storage / Postgres | Internal, privileged | The worker uses a service role that bypasses RLS. Every worker query therefore carries its scope predicate explicitly. |

There is no row for "a more privileged browser", because there is no more privileged
account (DECISIONS.md D16). Authorization is entirely about the caller's relationship to
the row being touched.

## Authentication and identity

```
1. Browser signs up or signs in through Supabase Auth (email + password).
2. A trigger on auth.users creates the profiles row, copying `name` and nothing else
   out of client-supplied signup metadata.
3. @supabase/ssr keeps the session in httpOnly cookies and refreshes it.
4. Server Components read the session server-side; client calls attach the access token.
5. FastAPI verifies the JWT: signature via cached JWKS, `exp`, `aud`, `iss`.
   The algorithm comes from the JWKS key, never from the token's own header.
6. `sub` → profiles row → Principal. A missing row is Unauthenticated, not a default.
7. Guards (require_auth, require_book_owner) run against that Principal.
```

No token in `localStorage`. A JWT proves *who*; the database decides *what they may do*.

Step 6 is a query rather than a decode on purpose: a deleted account must stop working
immediately, not when its last issued token happens to expire.

## The scoping model

This is the load-bearing idea. Everything else is plumbing.

```
Book.scope ∈ { canon, personal }

personal  → where every upload starts, whoever made it
             readable by: its owner ONLY
             usable for: that person's chat, and papers that person authors (D29)
             never: any other user, or any other author's paper, at all

canon     → the owner deliberately shared it (PATCH /books/{id}/scope, audited)
             readable by: every signed-in user
             usable for: chat, and assessment generation
```

Resolved to a predicate at exactly one chokepoint:

```python
# backend/app/rag/retrieve.py
def build_retrieval_filter(principal):
    """The ONLY place a retrieval predicate over `chunks` is constructed.
    Derived from the authenticated principal — never from request-supplied hints."""
    return or_(canon_clause(),                               # scope = 'canon'
               personal_clause(owner_id=principal.id))
```

Assessment generation runs under this same filter, built for the paper's **author**
(D29): an author may examine on their own uploads, shared or not, and can never reach
anyone else's.

Note what the signature does not take: a scoping argument. Canon is one platform-wide
pool, so there is no parameter for a caller to get wrong and no way to express "someone
else's canon", because there is no such thing. The personal clause is bound to
`principal.id` at construction, and no argument widens it — the worst a caller bug can
do is show callers their own material.

Every caller gets the same predicate shape, differing only in the owner id.
`tests/test_scoping.py` asserts that directly, by rendering two principals' filters and
requiring them to be identical once the ids are masked. That is the property that means
there is no account to escalate into.

### Defence in depth: RLS

Every table has row level security enabled and denies by default. Policies express the
same rule the chokepoint expresses, in SQL, against `auth.uid()`:

```sql
create policy "books_select_own_or_shared" on public.books for select
using (
  owner_id = (select auth.uid())
  or scope = 'canon'
);
-- chunks carries the identical policy on its denormalised columns.
```

The request path runs as the authenticated user, so RLS applies. The Celery worker runs
as the service role, so RLS does **not** apply — which is exactly why worker queries
must carry their scope predicates explicitly.

**GRANT and RLS are separate layers and both are required.** The share path proved it:
`propagate_book_scope()` carries a scope change from `books` down to `chunks`, and as an
invoker-rights function it ran as `authenticated`, which holds SELECT on `chunks` and no
more. Every share failed with `permission denied`. Granting the write would have been
the worse fix — `chunks` has no UPDATE policy, so the grant matches zero rows and the
share *appears* to succeed while the chunks keep the old scope. The trigger is
`SECURITY DEFINER` instead; authorization already happened in the policy that admitted
the UPDATE on `books`.

## Request flows

### A. Document ingestion (async)

```
POST /api/v1/books   (multipart)
  ├─ authorize: require_book_owner — the upload is the caller's, and starts personal
  ├─ validate: mime type in {pdf, docx, pptx, txt, md, zip}, size cap, page cap
  │            zip = one book uploaded in parts; members combined at parse time (D26)
  ├─ stream to Storage  books/{owner_id}/{book_id}/source.{ext}
  ├─ INSERT book (status=uploaded)
  ├─ celery: ingest_book.delay(book_id)      ← enqueued after commit
  └─ 202 { book_id, status }

worker (queue: ingest)
  1. parse    per-format parser → per-page/section text, page provenance kept
  2. chapter  outline/TOC → heading heuristics → whole-document fallback
  3. chunk    ~320 tokens, 64 overlap, never across a chapter boundary
              sized to the EMBEDDER: bge-small reads 512 tokens and silently
              truncates past them, so a bigger chunk is indexed on its head (D21)
  4. embed    POST embeddings:8001/embed  (batched)
  5. store    INSERT chunks (text + embedding vector(384)) in one transaction,
              one executemany rather than a statement per chunk
  6. status=ready   (or failed + error, surfaced in the UI)
```

Client polls `GET /books/{id}` for status. Failure is a visible state with a reason, not
a silent stall.

### B. Grounded chat (streaming)

```
POST /api/v1/chat/sessions/{id}/messages   { content }
  1. classify intent — a greeting is not a failed question (D19)
  2. classify SHAPE — how does this question want its material gathered? (D22)
       focused   the default and the path below
       overview  "summarize this book" → coverage sample in reading order
                 (ntile per book) + chapter outline; no vector search at all
       lookup    "find every mention of X" → full-text search (GIN) fused with
                 a vector search on the extracted topic; zero hits IS the answer
       compare   "compare these books" → per-book quotas, dominant-book routing
                 deliberately off
       metadata  "who wrote this book" → the `books` row, no search and no model:
                 the answer is not in any chunk (D23). A named work the library
                 does not hold falls back to the focused content search
     every shape retrieves under the same predicate from step 3
  3. build_retrieval_filter(principal)          # canon + the caller's own personal
  4. embed query via embeddings service, behind bge's retrieval instruction:
     the model is ASYMMETRIC — passages bare, questions prefixed (D21)
  5. SET LOCAL hnsw.ef_search; SELECT ... ORDER BY embedding <=> :q LIMIT 30
     WHERE <predicate> AND score ok      # the vector column itself is deferred
  6. if nothing above threshold → grounded refusal, no LLM call for content
  7. rank: dedupe overlap → vote a dominant book → spread across pages → top 5
  8. prompt: numbered sources + citation contract + refusal contract.
     Each source is EXCERPTED to the sentences bearing on the question (D21);
     the whole chunk is what was indexed, ranked and cited — only the model's
     copy is cut, because prompt evaluation is the larger half of the wait.
     Non-focused shapes add a task block saying what the sources ARE.
  9. LLM stream → SSE to client   (intent, pipeline, citations, token…, done)
     `pipeline` is the trace behind the transcript's "Advanced" disclosure —
     stages with timings, the book vote, the outcome. Ephemeral: it rides the
     stream and is never persisted.
  9. persist message + citations [{chunk_id, book_id, page, scope}]
```

Citations carry `scope`, so the UI can label a claim as coming from *the class book*
versus *your own upload* — the reader needs to know which of their sources they may be
actually examined on.

### C. Assessment generation

```
POST /api/v1/assessments  { book_ids[], chapter_ids[], type, count, difficulty_mix }
  → draft assessment + generate_assessment.delay(assessment_id)

worker (queue: llm)
  1. select chunks by COVERAGE, not similarity — stratify across chapters so the
     paper spans the syllabus instead of clustering on one dense section
  2. batch chunks → LLM with a strict JSON schema, one call per batch
  3. validate: schema, exactly one correct option, options distinct,
     stem answerable from the cited chunk alone
  4. dedupe: embed stems, drop near-duplicates above cosine threshold
  5. store questions (status=draft) with source_chunk_ids
```

The author then edits, regenerates individual questions, and publishes. **Generation
produces a draft; a human publishes.** Same principle as the review gate.

### D. Sitting a proctored assessment

```
someone opens /exam/{share_token}
  1. access check: link | link+password; window open?  (the token is the grant)
  2. consent screen — what is recorded, how long it is kept, who sees it
  3. camera permission → baseline still → proctor_session opened
  4. attempt created (status=in_progress, server-authoritative deadline)

during:
  browser  MediaPipe FaceLandmarker + ObjectDetector ~5fps → debounced heuristics
           POST /proctor/{session}/events     batched every 10s
           POST /proctor/{session}/heartbeat  every 15s
           high-severity event → signed upload URL → PUT 640px JPEG to Storage
  server   persists raw events; never trusts client scoring

submit / deadline / auto-submit:
  close session → aggregate_session.delay() recomputes integrity score from raw events
  → review_status = pending
```

### E. The review gate

```
author queue: attempts with review_status=pending, ordered by weighted severity
  per event → verdict ∈ { dismissed, upheld }
  reviewer note (free text)
  action  → release  (report becomes visible to the sitter, upheld events only)
          → clear    (no findings shown; attempt stands)
          → void     (attempt invalidated — a human act, never automatic)

the sitter sees NOTHING from a proctor session until release or clear.
Every terminal action writes an audit_log row.
```

Dismissed events are excluded from the released report and from the released score. The
reviewer's judgement, not the model's, is what the sitter receives.

### F. Deletion

Deleting a book removes, in one task: `chunks` (text **and** vectors, same rows) →
`chapters` → the Storage object → the `books` row → an `audit_log` entry. Because
vectors are columns rather than rows in a second database, there is no orphan class to
chase.

## Background work

Celery over Redis, with named queues so one slow workload cannot starve another:

| queue | tasks | shape |
|---|---|---|
| `ingest` | `ingest_book`, `delete_book` | CPU-bound, minutes, low concurrency |
| `llm` | `generate_assessment`, `grade_attempt` | IO-bound on a slow API, retry-heavy |
| `proctor` | `aggregate_session` | short, bursty at exam end |
| `maintenance` | `purge_evidence`, `sweep_stale_sessions` | Celery beat, periodic |

Rules: tasks are idempotent (delete prior partial output first), carry
`max_retries` with exponential backoff, and write a user-facing reason onto the owning
row on terminal failure. Enqueue **after** the transaction that created the row commits —
enqueue inside it and the worker can pick up an id that no longer exists on rollback.

## Failure behaviour

| Failure | Response |
|---|---|
| LLM unavailable during chat | stream an explicit service error; do not answer ungrounded |
| LLM unavailable during generation | task retries with backoff; assessment stays draft |
| Embeddings service down | ingestion task retries; `/ready` on backend reports degraded |
| Postgres unreachable | 503 from `/ready`; Kubernetes stops sending traffic |
| Redis down | enqueue fails loudly at upload time; no silent "uploaded but never ingested" |
| Camera denied mid-exam | `camera_denied` event; exam continues; the author sees the gap |
| Sitter network drop | heartbeat gap event; attempt resumable within the window |
| Ingest fails | `book.status=failed` + reason, retry button, book not silently empty |

## Observability

- **`/health`** — process liveness only, no dependency calls. Kubernetes `livenessProbe`.
- **`/ready`** — checks Postgres, Redis, embeddings, and (shallowly) Storage. Kubernetes
  `readinessProbe`. A dependency being down must remove the pod from the load balancer,
  not restart it.
- **`/metrics`** — Prometheus: request latency histograms, task duration by queue, LLM
  call count and token cost, retrieval hit rate, refusal rate.
- **Logs** — structured JSON with `request_id` propagated into task headers, so an ingest
  failure can be traced back to the upload that caused it.
- **`audit_log`** — the human record: who released, voided, overrode, or deleted, and
  when. Distinct from application logs, and retained longer.

## Security notes

- Sessions are Supabase httpOnly cookies via `@supabase/ssr`. No token in `localStorage`.
- The service role key exists only in the backend and worker environments. It never
  reaches the browser, and never appears in a `NEXT_PUBLIC_*` variable.
- Buckets are private. Everything is a short-lived signed URL, issued per request.
- Share tokens are high-entropy, revocable, and scoped to a single assessment.
- Evidence stills live under a per-session prefix with a retention TTL; purge on
  assessment deletion and on TTL expiry.
- Rate-limit chat and generation per user; both spend money or CPU per call.
- Uploaded documents are untrusted input — parse in the worker, cap file size and page
  count, and never render an uploaded file inline in the browser.
