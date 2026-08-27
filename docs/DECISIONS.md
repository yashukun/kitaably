# Decisions

Each entry: what was chosen, why, what it costs, and how expensive it is to reverse.
Entries marked **assumed** were made without a stated preference — say the word and they
change.

---

## Superseded

The first draft of this project targeted a different stack. Recorded here so the change
is visible rather than mysterious.

| Was | Now | Reason |
|---|---|---|
| Self-hosted Postgres | Supabase Postgres | one managed platform for DB + auth + storage |
| Qdrant | `pgvector` in the same database | see D2 |
| MinIO | Supabase Storage | see D1 |
| Own JWT + Google OAuth | Supabase Auth, verified via JWKS | see D1 |
| Postgres job queue (`SKIP LOCKED`) | Redis + Celery | see D6 |
| Groq | OpenAI-compatible client (Ollama / OpenAI) | see D5 |
| Alembic | SQL migrations via the Supabase CLI | see D7 |
| `fastembed` in-process | standalone embeddings service | see D4 |
| PDF only | PDF, DOCX, PPTX, TXT, MD | stated requirement |
| Teacher / student roles | one `user` account | see D16 |
| Classroom-scoped canon | one shared library | see D16 |

---

## D1 — Supabase is the platform for database, auth, and storage

**Choice.** Supabase Postgres, Supabase Auth (GoTrue), and Supabase Storage. FastAPI
verifies access tokens against the project's JWKS endpoint and treats `profiles.role` as
the authority on what a user may do.

**Why.** Three infrastructure problems — a database, a correct OAuth + session
implementation, and private object storage with signed URLs — collapse into one managed
dependency with one set of credentials. Writing session refresh, password reset, and
OAuth callback handling by hand is a fortnight that teaches less than the deployment work
this project is actually for.

**Cost.** A hosted dependency in the critical path, free-tier projects that pause when
idle, and a vendor-shaped auth model. Storage APIs are Supabase's, not S3's, so a move to
S3 later touches every upload and signed-URL call site.

**Reversal.** Moderate. Postgres is portable (it is just Postgres). Auth is the sticky
part: moving off GoTrue means re-issuing every session and re-linking every OAuth
identity. Keep auth access behind `app/core/security.py` and storage behind
`app/clients/storage.py` so the blast radius stays two files.

## D2 — Vectors live in Postgres via pgvector, not a dedicated vector database

**Choice.** `chunks.embedding vector(384)` with an HNSW cosine index, in the same
database and the same row as `chunks.text`.

**Why.** The dominant failure mode of a two-store design is disagreement between the
stores — most dangerously, a vector that survives the deletion of the material it came
from, which is a privacy leak in a product whose central promise is that personal books
stay personal. One row means deletion is atomic and provenance cannot drift. At this
corpus size (a shelf of books, not a web crawl) pgvector's recall and latency are not
the binding constraint.

**Cost.** No dedicated vector-database features: no payload-index tuning, no quantisation,
weaker horizontal scaling for the search itself. Index build time competes with OLTP work
on the same instance. Beyond roughly a million chunks this decision should be revisited.

**Reversal.** Moderate. `build_retrieval_filter()` and one search function are the only
places that know how search works — but a move to an external store re-introduces exactly
the deletion-consistency problem this decision buys off, so reverse deliberately.

## D3 — Local development runs the Supabase CLI stack

**Choice.** `supabase start` runs Postgres + pgvector, Auth, Storage, and Studio in their
own containers. `docker-compose.yml` runs only the services this repo owns: backend,
worker, beat, embeddings, redis, ollama, frontend.

**Why.** Dev then exercises real JWTs, real RLS, and the real Storage API. Auth bugs
surface on a laptop instead of in a cluster, and the same SQL migrations run locally and
in the hosted project. It also works offline, which matters for a free-tier project.

**Cost.** Two commands to bring the world up instead of one, a second set of containers
competing for RAM, and a Supabase CLI version to keep in step with the hosted project.

**Reversal.** Trivial in one direction (point `.env` at a hosted project instead), and
that is the documented escape hatch when the laptop is full.

## D4 — Embeddings are a separate service, not a library import  *(assumed)*

**Choice.** A small FastAPI container wrapping `bge-small-en-v1.5` (ONNX, CPU), exposing
`POST /embed`. The Celery worker calls it over HTTP.

**Why.** Goal 2. It is a second deployable with its own image, probes, resource limits,
and autoscaling behaviour — the cheapest realistic way to learn multi-service operations
without inventing a fake service. It also keeps a ~150 MB ONNX runtime out of the API
image and lets embedding capacity scale separately from request capacity.

**Cost.** One more container, one more network hop per batch, one more thing that can be
down (hence `/ready` reporting it), and a model that must be loaded before the pod is
ready — which is precisely the liveness-vs-readiness lesson Phase 9 wants.

**Reversal.** Easy. `app/clients/embeddings.py` is one interface; collapsing it to an
in-process call is a single-file change plus a Dockerfile edit.

## D5 — One OpenAI-compatible client, provider chosen by environment  *(assumed)*

**Choice.** The `openai` SDK against `OPENAI_BASE_URL`. Local: Ollama at
`http://ollama:11434/v1`, free and CPU-only. Deployed: `https://api.openai.com/v1`.

**Why.** One code path, one retry policy, one place that counts tokens. Provider choice
becomes an environment concern rather than a code concern, which is exactly where it
belongs.

**Cost.** The lowest common denominator of both APIs — no provider-specific features.
CPU Ollama is slow: a 20-question paper is minutes, not seconds. Output quality differs
enough between a local 8B model and a hosted frontier model that prompts tuned on one
need re-checking on the other.

**Reversal.** Trivial. Any OpenAI-compatible endpoint is an env change.

## D6 — Redis + Celery for background work

**Choice.** Celery with a Redis broker and result backend, four named queues
(`ingest`, `llm`, `proctor`, `maintenance`), plus Celery beat for periodic work.

**Why.** Ingest and generation are minutes-long and CPU- or API-bound; running them in
the request process would block it. Named queues stop a 600-page ingest from starving
exam-time aggregation. Beat gives evidence purge a home. And operating a broker — queue
depth, worker scaling, dead letters — is squarely on the learning path.

**Cost.** Two more moving parts (broker and worker), at-least-once delivery to design
around, and Redis persistence semantics to understand before trusting it with anything.

**Reversal.** Moderate. Tasks stay thin wrappers over service functions, so the service
layer survives a swap to arq/RQ/Dramatiq; the deployment topology is what changes.

## D7 — The schema lives in SQL migrations, applied by the Supabase CLI

**Choice.** `supabase/migrations/*.sql`, forward-only, applied by `supabase db push` (or
CI). SQLAlchemy models mirror the schema for typed queries and never create it. No
Alembic.

**Why.** RLS policies, triggers, pgvector indexes, and storage policies are SQL objects
that autogenerate does not model. Two migration systems against one database eventually
disagree, and the loser is production. One system, in the language the features are
written in.

**Cost.** Hand-written DDL, no autogenerate diffing, and a real risk of models drifting
from the schema — mitigated by a CI check that boots a fresh database from migrations and
asserts the models match.

**Reversal.** Painful once data exists. Choosing Alembic instead would mean re-expressing
every policy in `op.execute()` blocks anyway.

## D8 — RLS everywhere, and the chokepoint anyway

**Choice.** Every `public` table has RLS enabled and denies by default. The request path
runs as the authenticated user, so policies apply. The Celery worker runs as the service
role, so they do not.

**Why.** Defence in depth for the invariant that matters most. A missed `WHERE` in
application code is caught by the database; a policy bug is caught by the chokepoint.
Neither is trusted alone.

**Cost.** Every schema change now carries policy design; policies are easy to get subtly
wrong and hard to unit test; and the worker path is deliberately exempt, which is the one
place a scope bug is unguarded. Hence: worker-path scope tests are mandatory.

**Reversal.** Do not. Removing RLS from a table is a one-line migration and a permanent
downgrade of the strongest guarantee here.

## D9 — Retrieval scope is built at a single chokepoint

**Choice.** `build_retrieval_filter()` in `backend/app/rag/retrieve.py` is the only
function that constructs a predicate over `chunks`, and it derives scope from the
authenticated principal.

**Why.** The personal/canon boundary is the product's central privacy promise. A boundary
enforced in fifteen call sites is a boundary that will be breached in the sixteenth.

**Cost.** Slightly awkward for exotic queries; they must extend the chokepoint rather
than bypass it.

**Reversal.** Do not.

## D10 — Proctoring detection runs in the browser  *(assumed)*

**Choice.** MediaPipe Tasks Vision in the sitter's browser at ~5 fps (face landmarks
plus a light object detector for `phone_visible`). The server receives structured events
and downscaled JPEG stills of flagged moments. No video is streamed or stored.

**Why.** Server-side CV on a live class means a GPU budget, a frame queue, and storage
write volume orders of magnitude higher — for a signal a human reviews by hand anyway.
Stills are enough to justify or dismiss an event. The GPU budget here is zero.

**Cost.** The detector runs on hardware the sitter controls, so it is defeatable. This
is mitigated, not solved, by treating **absence as evidence**. `phone_visible` in
particular is the weakest detector in the set and should carry low weight and generous
thresholds.

**Reversal.** Moderate. The event schema and review gate are unchanged; a hybrid upgrade
adds a server re-verification pass over flagged windows. Design events so a
`server_confidence` field can appear later.

## D11 — Generation and grading produce drafts, never final states

**Choice.** Generated questions land as `draft`. LLM subjective grades land with
`grader='llm'` and the assessment's author may override them. Proctoring reports are
invisible to the person who sat the paper until its author releases them.

**Why.** This is the product thesis. A human's authority over assessment and over
what evidence becomes an accusation is the difference between a platform and an
automated cheating-accusation machine.

**Cost.** Human review time is on the critical path for every exam. Accepted deliberately.

**Reversal.** Do not. "Auto-release when the score is clean" is the obvious pressure and
should be refused.

## D12 — Assessment chunks are selected by coverage, not similarity

**Choice.** Question generation stratifies chunk sampling across the selected chapters
rather than taking a top-k similarity search.

**Why.** Similarity search against a topic query clusters on the densest passage, and the
resulting paper examines one section five times. A question paper needs spread.

**Cost.** More chunks read per paper; slightly higher generation cost.

**Reversal.** Trivial — it is one sampling function.

## D13 — Grounded refusal over graceful fallback

**Choice.** When retrieval returns nothing above threshold, the tutor states the material
does not cover it and stops. No world-knowledge fallback.

**Why.** "Grounded in *your* book, not the open internet" is the reason anyone trusts
this over a general chatbot. One confident ungrounded answer destroys that.

**Cost.** Students hit refusals on reasonable questions when chunking or thresholds are
poorly tuned. Treat frequent refusals as a retrieval bug to fix, not a reason to relax
the rule.

**Reversal.** Do not. Tune the threshold, keep the contract.

## D14 — Kustomize base + overlays, not Helm  *(assumed)*

**Choice.** `infra/k8s/base/` holds plain Kubernetes objects; `overlays/dev` and
`overlays/prod` patch replicas, resources, images, and env.

**Why.** Goal 2 again: reading and writing real Deployments and Services teaches
Kubernetes. Helm's templating is a packaging skill layered on top of an object model you
have to know first, and `{{ }}` in YAML obscures the object while you are still learning
what it is. Kustomize also ships inside `kubectl`.

**Cost.** No chart to publish, no `values.yaml` for consumers, more duplication than a
well-written chart. Most production shops use Helm, so this is a detour from convention.

**Reversal.** Easy, and expected. `infra/k8s/charts/` is reserved with a note for when
the objects are familiar and packaging becomes the point.

## D15 — Scope columns are denormalised onto `chunks`

**Choice.** `chunks` carries `owner_id` and `scope`, copied from `books` by trigger.

**Why.** The retrieval query is the hottest and most security-critical query in the
system. Denormalising removes a join from it, keeps the RLS policy expressible on the
table being queried, and makes the predicate readable at a glance — which matters when
the predicate is the privacy boundary.

**Cost.** Duplicated truth. A book that is shared or unshared must propagate, which is
what `propagate_book_scope()` is for; application code must never write these columns.
That trigger is `SECURITY DEFINER` because `authenticated` may read `chunks` and not
write it — see the migration for why granting the write instead would fail silently.

**Reversal.** Trivial, at the cost of a join.

---

## D16 — One kind of account, one shared library

**Choice.** There are no roles and no classrooms. Every signed-in user can do everything:
upload material, share it, generate an assessment, sit one. A book is `personal` (private
to its owner) or `canon` (shared with every signed-in user), and sharing is an act its
owner performs on their own row.

Supersedes the original model, in which a teacher owned a classroom, students joined it
with a code, `canon` meant "shared with that room", and a set of actions were reserved to
teachers. `classrooms`, `enrollments`, the membership helpers, the roster view, and the
`user_role` enum are all dropped.

**Why.** Stated product direction. The classroom was the unit the schema was built
around, but it was not the unit the product needed: what it actually needs is somebody's
private material, somebody's shared material, and a link that grants access to a paper.
Roles turned out to be describing *intent* rather than *authority* — every real
authorization check in the system was already about the caller's relationship to a
specific row, and not one of them consulted `profiles.role`.

**What this does not weaken.** The privacy boundary is unchanged and still enforced the
same way. A personal book is visible to its owner and to nobody else; scope is derived
server-side from the authenticated principal; `build_retrieval_filter()` is still the one
place a predicate over `chunks` is constructed; RLS is still the second line. Every
caller's retrieval filter now has the *same shape*, differing only in the owner id —
there is no account whose reach is wider, so there is no account to escalate into.
`tests/test_scoping.py` asserts exactly that.

**What this genuinely weakens.** `canon` used to mean "shared with one classroom, by the
teacher who owns it". It now means "shared with everyone signed in, by anyone". Two
consequences worth naming rather than discovering later:

- There is no longer a check that the person publishing material is entitled to. Anyone
  can put a book in the pool assessments are generated from.
- The blast radius of a mistaken share is every user, not one room.

Both are accepted deliberately. Reintroducing a distinction is D16's reversal, below.

**Cost.** A destructive migration, and a vocabulary change across the codebase. Anything
that wanted "this cohort sees this material" has to wait for that distinction to come
back — and until then, "share" means "share with the whole instance", which the UI says
in those words rather than hiding behind "publish".

**Reversal.** Moderate, and deliberately left cheap. `profiles.role` still exists as
`public.app_role`, holding the single value `'user'`; nothing branches on it. Bringing
roles back is `alter type public.app_role add value ...` plus the policies that read it,
not a new identity model. Bringing classrooms back is genuinely expensive — the tables
are dropped, so it means new migrations and a scope column on `books` again.

---

## D17 — Removed rather than left half-built

**Choice.** Password-protected share links (`assessments.access_mode`,
`access_password_hash`) are dropped, along with the shadcn install kit the frontend
never used, four unused npm dependencies, and the `graphql_public` schema exposure.

**Why.** A column with two values where only one does anything is worse than no column:
its existence is what makes the feature look supported. A paper set to `link_password`
was protected by a link and a belief. The same reasoning applies to
`graphql_public` — a second query surface nobody tests, on a stack where every read goes
through FastAPI.

**Cost.** Password-gated links have to be rebuilt from scratch if wanted, rather than
half-existing. That is the intended trade.

**Reversal.** A migration plus the code that enforces it, in the same change. The rule
this sets: **nothing lands in the schema before the code that enforces it.**

**Deliberately kept** despite being unreferenced today: the proctoring scaffolding
(Phases 7–8, which is the design doc for the next phase), the `infra/` READMEs, the
Celery `beat` service and its empty schedule, and `errors.Forbidden` — see its docstring
for why its being unused is the point rather than an oversight.

---

## D18 — Password recovery is an emailed one-shot session, not a reset token we hold

**Choice.** "Forgot password" sends a GoTrue recovery link. The link lands on
`/auth/callback`, a Route Handler that spends the token **server-side**, trades it for
an ordinary httpOnly session cookie, and forwards to `/reset-password` — which is then
a plain authenticated `updateUser({ password })` with no token in sight. Finishing it
calls `signOut({ scope: "others" })`.

**Why.** The obvious alternative — our own `password_resets` table with a token column —
means minting, storing, expiring and comparing a credential, and getting all four right.
GoTrue already does it, and the flow above never lets the token reach client JS or
browser history: it arrives at a server route, is consumed once, and what survives is
the same session cookie every other signed-in page uses. There is no new secret in our
schema, so there is no new secret to leak.

`scope: "others"` is the part that is easy to skip. Somebody resetting a password is
often doing it *because* a session they did not open is still alive somewhere; leaving
those running makes the reset cosmetic. Keeping the current one means they are not
signed out of the tab they are standing in.

**The response is deliberately uninformative.** Requesting a link says "if that address
has an account" and says it for addresses that do not exist. A distinguishable answer
turns an unauthenticated form into a membership oracle — feed it a list, learn who is
registered here. GoTrue returns 200 either way for that reason and the copy has to
match, or the leak reappears at the UI layer.

**Cost.** Recovery mail is GoTrue's, so the template and its expiry live in
`supabase/config.toml` rather than in application code, and a hosted project needs SMTP
configured before any of this works in front of real people. `otp_expiry` is pulled down
to an hour from the 24h default: a link one click from a password change should not sit
valid in an inbox all day.

**Reversal.** Cheap, and it is the same shape as adding OAuth (open question 9) — both
are "an emailed or redirected credential arrives at `/auth/callback`". That route now
exists and handles both the PKCE `?code=` and the older `?token_hash=&type=` templates,
which is most of the work of question 9 already done.

**Note on the redirect allowlist.** `additional_redirect_urls` is what stops a crafted
`redirectTo` mailing a live recovery token to somebody else's origin; GoTrue silently
falls back to `site_url` for anything unlisted. It is an allowlist, not a hint, and the
deploy overlay adds its origin rather than loosening the pattern.

---

## Open questions

Not yet decided; each needs an answer before the phase that depends on it.

1. **Identity verification.** Is the baseline still matched against the face during the
   exam (`face_mismatch`), or captured for the reviewer's eyes only? Matching adds a real
   false-positive risk against people who share a device or sit in poor light.
2. **Retakes.** Can someone attempt an assessment more than once? Currently modelled as
   one attempt per person per assessment.
3. **OCR.** Scanned PDFs are flagged `needs_ocr` and not indexed. Adding OCR means
   Tesseract in the image and a much slower ingest.
4. **Who may publish.** D16 lets anyone share a book into the pool assessments draw from,
   and anyone author an assessment. If that turns out to be too open, the fix is a
   distinction in `app_role` plus a policy — not a UI check. Decide before this is used
   by people who did not all set it up.
5. **Evidence retention default.** 60 days is a placeholder; an institution's policy
   would override it.
6. **Language.** Chunking, embedding, and prompts currently assume English material.
7. **LLM in production.** Ollama in-cluster (free, needs nodes with real CPU and slow) or
   OpenAI (fast, costs per call, book content leaves the cluster). Decide before Phase 10
   sizes the node group.
8. **Supabase tier.** The free tier pauses idle projects and caps storage — fine for dev,
   a real constraint for a prod environment that must answer at exam time.
9. **OAuth provider.** Sign-up and sign-in are email/password. Adding one provider needs
   client credentials from Google (or similar) that no amount of local configuration
   substitutes for, so it is deferred rather than half-built. `[auth.external.*]` in
   `supabase/config.toml` is now the whole change: the `/auth/callback` route handler
   this used to also require was built for password recovery (D18) and already exchanges
   a PKCE `?code=` for a session.
10. **Attribution on shared books.** The shared library shows a book's title but not who
   shared it, because the `profiles` policy admits a user to their own row only and
   widening it would hand every user every other user's row. A narrow view exposing a
   display name for people who have shared something is the obvious fix; it is a
   deliberate omission rather than an oversight.

## D19 — Chat classifies intent before it retrieves

**Choice.** Every chat message is labelled — `question`, `follow_up`, `greeting`,
`chitchat`, `meta`, `unclear` — before anything is embedded. Only the first two reach the
vector search. The rest are answered from fixed copy, or from the reader's own library,
with no embedding call and no LLM content call.

Rules decide first (`app/rag/intent.py`), and the model is consulted only for what the
rules decline to label. When the classifier is unreachable or incoherent the answer is
`question`, always.

**Why.** Before this, "hi" was embedded, cleared no distance threshold, and came back as
*"Your books don't cover that."* Every piece of that was working exactly as designed, and
the product felt broken — a greeting is not a failed question. The refusal is the most
important sentence this system says (D13), and spending it on "thanks" devalues it for
the moment it actually matters.

Rules before the model because a local 8B model costs roughly a second and a half to
agree with a lexicon lookup, and this runs on the path every single message takes.

**The failure direction is chosen, not accidental.** A greeting misfiled as a question
costs one wasted search and produces the old behaviour. A question misfiled as a greeting
means somebody's actual work went unanswered and they were handed a cheerful hello. So
every ambiguous case, every parse failure, and every upstream outage resolves to
`question`. `tests/test_chat_intent.py` pins that direction case by case.

**Cost.** A lexicon is a maintenance surface, and it is English-only — which the whole
pipeline already is (open question 6). The `chat_intents_total` metric is labelled by how
the label was decided, so drift shows up as the `model` share climbing rather than as
silent misclassification.

**Reversal.** Trivial. Delete the classify step in `prepare_turn` and every message goes
back to retrieval.

## D20 — Book kind sets the tutor's register; it never filters retrieval

**Choice.** `books.kind` is a four-value enum — `fiction`, `nonfiction`, `academic`,
`reference` — written by ingest, best effort, nullable. `books.genre` is free text
("Organic chemistry", "Historical fiction") and `books.summary` is one sentence. All
three are shown to the reader, and `kind` additionally selects the tutor's register.

**None of them narrows a search.** Which book answers a question is decided from the
retrieved chunks themselves, by a weighted vote in `app/rag/rank.py`.

**Why not a category filter.** The obvious design is to classify a book into a library
taxonomy and narrow retrieval to the matching category. It was considered and rejected.

A reader's library is tens of books, not millions. The embedding already separates a
chunk about enthalpy from a chunk about a wedding, so the filter adds no precision worth
having — while adding a new way to fail. Misclassify one book and its material becomes
unreachable, with no error, no empty result and nothing in the answer to notice: just a
tutor that has quietly stopped answering questions it used to answer. That is the most
expensive failure mode this system has, because it is indistinguishable from correct
grounded-refusal behaviour (invariant 5, D13).

The routing question is also answerable without a taxonomy. "Which of my books covers
this" is already settled by the retrieval result — five hits from one book and one from
another *is* the answer — so it costs no model call, no label written months earlier, and
it cannot go stale.

**Why four values.** `kind` earns its place by changing something real: you do not explain
a novel the way you explain a thermodynamics textbook. Four categories a small local model
can classify reliably; sixty leaf categories it cannot. The fine-grained label survives as
`genre`, where being wrong is cosmetic — which is exactly why it is allowed to be free
text rather than an enum.

**Cost.** Register is coarser than it could be, and `genre` is unvalidated, so two books
on the same subject may be labelled differently. Neither affects what can be found.
`llama3.1:8b` also under-uses `academic`, calling textbooks `nonfiction`; the prompt names
that as the commonest mistake, and a stronger model in deploy does better. A book whose
classification failed has all three columns null and is answered exactly as well.

**Reversal.** Trivial in the safe direction — drop the columns, and the tutor loses only
its register. Reversing into a *filter* is the change this decision exists to argue
against, and should not be made without a corpus large enough that recall becomes the
binding constraint.

---

## D21 — Chunks are sized to the embedder; the tutor's prompt is excerpted

**The bug this starts from.** `bge-small-en-v1.5` reads 512 tokens and **truncates
silently** past them. It does not error and it does not degrade: a longer passage
returns a vector byte-identical to the one its own first 512 tokens produce. Measured
directly — embed a passage, embed its head alone, compare — the cosine similarity is
`1.000000`.

`CHUNK_TOKENS` was 800. **78.7% of the library was over the line**, so the tail of
four chunks in five was in the database, in the table, in `select count(*)`, and
absent from the index. The symptom is a grounded refusal about a page the reader can
see. Nothing logs it, because from the application's side nothing went wrong.

So chunk size is not a tuning knob. It is a property of the embedding model, and
`chunk_token_budget()` — not the setting — is the ceiling, at 80% of the model's
limit because the `chars // 4` token estimate under-counts dense prose. A setting
cannot raise it past the model; `tests/test_indexing_budget.py` asserts that, because
the failure it prevents is invisible.

**Then the latency.** The tutor took ~35s per answer on a CPU box and the LLM was
essentially all of it. Retrieval measures 0.03–0.3s end to end against this library;
there was never a database problem to solve. The measured rates that matter:

| | llama3.1:8b | llama3.2:3b |
|---|---|---|
| prompt evaluation | 55.7 tok/s | 113.9 tok/s |
| generation | 8.2 tok/s | 16.5 tok/s |

Both halves are paid per token, so the levers are *how much prompt* and *how much
answer* — and the model, which scales both.

**Choice.** Six changes, each aimed at one of those terms:

1. **Chunks 800 → 320 tokens** (overlap 100 → 64). Fixes the truncation, and a
   passage that is a third the size is a third the prompt.
2. **`top_k` 8 → 5.** Every passage is prompt the model must read before it writes a
   word.
3. **Excerpting** (`app/rag/trim.py`). A chunk is a unit of *indexing*, sized to what
   the embedder swallows; it is not a unit of *evidence*. Only the copy handed to the
   model is cut to the sentences bearing on the question — contiguous, in order,
   marked with an ellipsis. **The chunk stays whole in the database, in the ranking,
   and in the citation the reader opens.** Nothing about what was retrieved or what
   can be checked changes.
4. **History 6 turns → 4**, assistant turns 400 → 240 chars. Unlike the system
   prompt, history is not prefix-cached, so every turn of it is re-read every time.
5. **A length target** (~150 words) and a hard `max_tokens`. Generation is the slower
   half; an answer that runs on is time spent producing prose the reader stopped
   reading. Structured callers — assessment generation, grading — are deliberately
   **not** capped, because a truncated JSON body is a broken feature rather than a
   terse one.
6. **`llama3.1:8b` → `llama3.2:3b`**, and three Ollama settings that were quietly
   costing more than any of the above:
   - `OLLAMA_NUM_PARALLEL=1`. The context window is divided *between* parallel slots,
     so the default parallelism served a 4096 window as 2048 per request and the
     prompt was **silently truncated to fit**. Sources were being dropped before the
     model ever saw them — the same class of invisible failure as the chunking bug,
     one layer up.
   - `OLLAMA_CONTEXT_LENGTH=4096`, sized to the prompt this app actually builds.
   - `OLLAMA_KEEP_ALIVE=24h`. The default unloads after 5 minutes; the next question
     then pays a cold load, measured at 8.2s.

**Measured, end to end through the API, same three questions, same library:**

| | before | after |
|---|---|---|
| time to first token | 12.20s | **2.31s** |
| total answer | 34.71s | **9.22s** |
| retrieval alone | 0.10s | 0.32s |

**73% faster overall, 81% to the first token.** The "before" column is conservative:
it ran against the *new* 320-token chunks, so it read 2560 tokens of sources where
the real prior system read 6400 (truncated to 2048 by the parallelism bug). The
genuine prior latency was worse than the number above.

**What this costs.** A 3B model is a weaker writer than an 8B one, and that is a real
trade rather than a free win. It is one environment variable — `LLM_MODEL=llama3.1:8b`
restores the old quality at roughly twice the wait, and the deployed configuration
points at a hosted model where the whole calculation is different. Excerpting can
degrade an answer where the question shares no vocabulary with the passage that
answers it; the fallback is the head of the passage, and `RETRIEVAL_SOURCE_TOKENS=0`
turns it off entirely.

**A second, unrelated fix landed here** because it was found by the same measurement.
`embed_query` was embedding questions bare. `bge` is **asymmetric**: passages go in
plain, questions go in behind a retrieval instruction. Without it every distance came
back slightly too high, and the ones near `RETRIEVAL_MAX_DISTANCE` fell past it into a
refusal — "your books don't cover this" about material sitting in the index. That is a
recall bug, not a speed one, and it was making the tutor look less capable than it was.

**Reversal.** Everything except the chunk size is a setting. Chunk size requires
re-reading every book, which is `make reingest` — each book is queued as an ordinary
idempotent ingest that deletes its own prior chunks inside the transaction that writes
the new ones, so a book is answerable from its old passages until the moment it is
answerable from its new ones. There is no window where it has none.

## D22 — Retrieval answers by query shape: focused, overview, lookup, compare

**The bug this starts from.** A single top-k vector search answers exactly one kind of
question: a focused one, whose subject some passage actually discusses. Three whole
families of legitimate questions fail on it, each in the same quiet way — the machinery
works perfectly and the reader gets *"your books don't cover this"* about a book they
can see:

- *"Summarize this book"*, *"what are the key lessons"* — there is no subject to embed.
  The nearest chunks to the word "summarize" are noise.
- *"Does this book mention X"*, *"find every mention of X"* — lexical, not semantic.
  Vector similarity finds passages ABOUT a topic; it does not find every passage NAMING
  one, and the literal occurrences are the answer.
- *"Compare what these books say about X"* — the exact question book routing (D-vote,
  `rank.route`) exists to defeat. Collapsing to a dominant book throws away half the
  answer, on purpose.

**Choice.** A second rules-only classifier (`app/rag/shape.py`, same architecture as
D19's intent rules, no model fallback at all) labels every retrieval-bound question
with a *shape*, and each shape gathers its material differently:

| shape | trigger | evidence |
|---|---|---|
| `focused` | default | the original path: vector search → dedupe → route → spread |
| `overview` | "summarize this book", "key lessons", "important chapters" | a **coverage sample** in reading order (`ntile` per book — D12's insight applied to chat), plus the chapter outline as non-citable orientation |
| `lookup` | "does it mention X", "every mention of X", "which chapters discuss X" | **full-text search** (new GIN index on `to_tsvector('english', text)`) fused with a vector search on the extracted topic, by reciprocal rank — lexical first |
| `compare` | "compare these books", "which book explains X better" | per-book quotas; `rank.route` deliberately not run; with no topic ("most beginner-friendly") each book contributes a coverage cross-section |

Every shape retrieves under the same `build_retrieval_filter`, and the two new queries
(`lexical_query`, `coverage_query`) live in `rag/retrieve.py` and are built pure so
`test_scoping.py` compiles the production SQL. **A shape changes how the caller's own
scope is sampled; it can never widen it.**

Graceful degradation is part of the choice: zero lexical+vector hits for a mention
question is answered as *"no mention found — here's what I searched"* (that IS the
answer, fixed copy, no model call); "this book" with several visible and none selected
asks which rather than guessing; a comparison over a one-book library says it needs two.

**Why not stored summaries.** An ingest-time LLM summary would answer overview
questions faster, but it is model output — answering from it is summarising a summary,
and its claims cite nothing the reader can open, which breaks invariant 5 rather than
bending it. A coverage sample cites real passages. (The one-sentence `books.summary`
from classification is display copy, never evidence — D20's line holds.)

**Why the intent lexicon grew a guard.** "Summarize this book" is three words opening
exactly like a follow-up; a demonstrative aimed at the *material* is now never treated
as anaphora on the transcript.

**Cost.** Two more rule surfaces to maintain (shape patterns, topic extraction), both
English-only like everything else (open question 6). The lookup path runs two searches
per question — measured retrieval is 0.03–0.3s against this library, so it is paid in
milliseconds. Overview answers rest on a sample and say so. The GIN index adds write
cost at ingest, on a table that is write-once per book.

**The failure direction is chosen, not accidental.** Every unrecognised question is
`focused` — the old behaviour exactly. A missed shape produces the old blind spot; an
over-eager rule would misroute a real question, so the compare rules demand the
question actually point at books or authors ("compare mitosis and meiosis" stays
focused). `chat_query_shapes_total` shows drift as a shape share that never fires.

**Reversal.** Delete the dispatch in `prepare_turn` and every question is focused
again. The index drops with one migration; nothing else references it.

## D23 — Record questions answer from the record; intent stays rules-first with a wider model tail

**The bug this starts from.** *"Who wrote this book?"* ended in *"your books don't
cover this."* Correct machinery, wrong experience — the same class of failure D19 fixed
for greetings, one layer up: the author's name lives in `books.author`, a column this
process is already holding, and retrieval searches chunk *text*, which literally cannot
contain it. The refusal was honest about the chunks and dishonest about the server.

**The question considered here** was whether to go further: replace the rules-based
intent classifier with a small LLM that labels every message. Rejected, for reasons
worth keeping:

1. **It taxes every message.** On the measured CPU stack (D21: ~114 tok/s prompt eval)
   a classify call is seconds, serial, before retrieval starts — paid on "hi".
2. **The rules survive anyway.** The classifier must never take chat down, so the
   deterministic path has to exist regardless; LLM-first means maintaining both and
   paying the model on the happy path.
3. **The failure directions are asymmetric.** Rules fail toward `question` — one wasted
   search. A model fails creatively: a real question labelled `chitchat` is somebody's
   work unanswered, which is the direction D19 chose against.
4. **Decisive here: it would not have fixed the bug.** A perfect classifier picks from
   the same labels, says `question`, and the turn still reaches a search that cannot
   contain the answer. **The label set is the ceiling, whoever assigns the labels.**
   The missing piece was a route, not intelligence.

**Choice.** Three moves instead of a rewrite:

1. **A `metadata` query shape** (`app/rag/shape.py`, D22 machinery). "Who wrote this
   book", "how many pages", "what genre", "what's it called" resolve their target
   through the same picker → named-title → ask-which logic as overview, then answer
   from the `books` row as fixed copy — no embedding, no model call, invariant 5 intact
   because no claim about *content* is being made. Honesty is in the copy: the author
   is reported *as recorded at upload*, and a null says so plainly. A NAMED work the
   library does not hold ("who wrote Hamlet", no such title here) **falls through to
   the focused content search** — a history book knows who wrote the Declaration, and
   the record was never the right place to ask.
2. **A wider model tail.** `classify_offline` now escalates long declaratives (no
   interrogative, no imperative, no "?", more than eight words) to the model instead of
   force-labelling them `question`. Safe by construction: with the fallback off,
   `classify` resolves the escalation to `question` — byte-identical to the old
   behaviour.
3. **The fallback stays per-environment.** `CHAT_INTENT_LLM_FALLBACK=false` where the
   model is a CPU (dev), flipped on where a classify answers in milliseconds (deploy).
   `chat_intents_total{source}` already shows what the model share actually buys.

**Cost.** More metadata patterns to maintain, English-only like the rest (open question
6). Publication date is not stored, so "when was this published" stays a content
search. `books.author` is user-typed, unverified — hence "recorded as", never asserted.

**Reversal.** Delete the METADATA branch and the record questions go back to honest
refusals; tighten the eight-word bound and escalation narrows to single words again.

## D24 — A clarifying question is a two-turn contract

**The bug this starts from.** The ask-which reply (D22's graceful answer to an
ambiguous "this book") was a dead end. Observed in one transcript, three ways: the
reader answered "Epic Shit" and the bare title was embedded as a *topic* — a focused
search for the phrase, citing both books, the original question silently dropped;
two turns later "what is the actual title of the book?" was met with "which book do
you mean?" *again*, one turn after the reader had said which; and the exasperated
"why are you asking again. Epic Shit" was embedded whole, complaint included, and
refused. The machinery could ask a question but could not hear the answer.

**Choice.** Deterministic conversational state, read from the transcript the pipeline
already loads — no model call, no schema change:

1. **Answers resume the question.** When the previous assistant turn is the ask-which
   reply (recognised by its own fixed heading, `prompts.PICK_BOOK_HEADING`, so copy
   and detection cannot drift), and the new message names a visible book — or says
   "all my books" — the ORIGINAL question is re-run, narrowed to the selection. A
   reply that is itself a full question ("what does Epic Shit say about money?") is
   deliberately left alone: `_is_bare_selection` strips the title and the filler
   around it (complaints included), and only near-empty remainders count as a pick.
2. **The conversation is memory for "this book".** Before asking which, both
   ask-capable paths (overview, record) scan the reader's own recent messages,
   newest first, for a title — one hit resolves, several settle nothing, and
   assistant turns are skipped because the ask-which reply names *every* book.
   A reader who answered once is never asked twice inside the history window.

The transcript stays truthful throughout: the persisted user message is what they
typed ("Epic Shit"), while the tutor answers the resumed question — and the Advanced
panel shows the `resolve` step, so the substitution is visible, not sleight of hand.

**Cost.** Two heuristics (bare-selection, title containment ≥ 4 chars) that can
misfire: a wrongly-absorbed pick re-answers the old question about the named book; a
missed pick reproduces the old behaviour. Both directions are visible in the trace
and recoverable by rephrasing. Memory is bounded by `CHAT_HISTORY_TURNS`, so a book
named long ago is forgotten — which is the safer direction for a stale reference.

**Reversal.** Delete the resume block in `prepare_turn` and the recall checks at the
two ask branches; the ask-which reply goes back to being a dead end.

## D25 — Question *format* and grading *family* are two columns, not one

**The ask.** Support the full range of question kinds a book can produce — multiple
choice, true/false, yes/no, fill in the blanks, flashcards, match the following,
ordering, one-word, numeric, short and long answer — across recall, comprehension,
critical thinking, application, analysis and synthesis, at difficulties from beginner
to research level. Written out as a catalogue that is about two hundred items long.

**What the catalogue actually contains.** Sorted, it is three axes wearing one coat:

- a **shape** — how the question is drawn and answered
- a **cognitive level** — what kind of thinking it asks for
- a **rigor** — how hard, for the same shape and the same level

Most of the two hundred are the same shape with a different instruction. A "definition
question", a "key term identification", an "acronym expansion" and a "synonym question"
are one typed short answer, four times. A "case study", a "situation-based MCQ" and a
"what would you do" are one scenario. Modelled literally, that is two hundred enum
values, two hundred renderers, and two hundred marking paths — of which about a hundred
and ninety are duplicates that will eventually disagree with each other.

**Choice.** Three axes, three columns, and a hard split between the shape and the
marking:

- `questions.format` — **fourteen** values. What the author picks and the sitter sees.
  Each earns its place by having its own prompt *and* its own renderer.
- `questions.type` — **six** values, the grading family. One marking function each.
  Seven formats mark as `mcq`, because a true/false really is a two-option multiple
  choice.
- `questions.difficulty` — extended from three rungs to Bloom's full six, which is what
  the catalogue's first six categories are. The column keeps its name; renaming it to
  `cognitive_level` would rewrite eleven migrations' worth of references for a word.
- `assessments.rigor` — nine values, one setting for the whole paper, steering register
  only. Never a retrieval filter, for the same reason `book_kind` is not (D20).

Everything else in the catalogue is reachable as *format + level + the author's own
free-text brief*. "Identify the logical fallacy" is a multiple choice at `evaluate`.
"Design a solution" is a long answer at `create`. Those are requests a prompt can carry
and a mark scheme already knows how to handle.

**Why the split is the load-bearing part.** Collapsing format into family is wrong in
both directions. Fourteen families means fourteen marking paths, and marking is the one
place in this product where a silent bug is a fairness incident rather than a broken
page — six paths can be tested exhaustively and fourteen cannot. Six formats means the
paper cannot express what an author wants: "true/false" and "multiple choice" mark
identically and read nothing alike, and an author who asked for flashcards and got radio
buttons has been given something else.

The mapping is therefore held twice, deliberately: in `app/rag/formats.py`, which
decides what to build, and as a check constraint in Postgres, which refuses a row where
the two disagree. `tests/test_formats.py` pins them together, because the failure mode
if they drift is that generation spends every LLM call it was going to spend and *then*
fails on the INSERT — the most expensive way possible to find out.

**One `answer_key` column, not four.** `multi_select`, `short_text`, `match` and
`sequence` each need a different correct answer. They share one jsonb column rather than
taking one each, because the rule that matters (invariant 2) is that the key is ABSENT
from `public.question_sit` — RLS cannot hide a column, so the sitter must not reach it
at all. One column is one thing to keep out of one view; four columns is four chances
for the fifth format to add a fifth and forget. `tests/test_formats.py` reads the view's
select list back out of the migration and asserts it.

**One `response` column, not two.** A structured answer travels as compact JSON in the
existing `answers.response` text column — `["A","C"]`, `{"1":"B"}`, `["C","A","B"]`.
Adding `response_data jsonb` beside it would raise a question with no good answer:
which one holds the answer when both are populated. The parsers return empty rather than
raising, so a response that will not parse is an unanswered question — a grading run
must not die on one malformed row and leave a cohort unmarked.

**Partial credit is a policy and it is written down.** Three families can be half-right.
`multi_select` awards `(right ticked − wrong ticked) / right available`, floored at zero:
without the subtraction, ticking every box scores full marks on every select-all ever
written; with a negative floor removed, a question could take marks off another question,
which is negative marking and a different policy. `match` and `sequence` award per pair
and per position — a four-pair grid is four questions printed together, and marking it
as one makes a single slip cost four marks. `sequence` is scored position-wise rather
than by inversion count because it is the rule a sitter can check against the screen in
front of them.

**One LLM call per (format, batch of passages).** Previously one call per batch asked for
a mix. Asking a 3B model on a laptop for a true/false, a match grid and a long answer in
one reply means three JSON shapes in one object, and it gets that wrong far more often
than it gets three separate calls wrong. It also bounds the blast radius: a batch that
will not parse costs one format's worth of questions rather than the paper's.

**Cost.** Two enums extended and two added, three columns on `questions`, two on
`assessments`, two views rebuilt, and a migration that must backfill `format` on existing
rows before it can constrain them — a subjective question left at the column default
`'mcq'` fails the family check, and the ALTER TABLE rolls back on data that was valid a
moment earlier. More formats also means a higher validation reject rate per format on a
small local model; the rejection reason is labelled by format in
`questions_rejected_total` so "this model cannot write match grids" is visible rather
than showing up as a short paper.

**Reversal.** Cheap for the formats, expensive for the families. Retiring a format is a
row update plus an enum value nobody selects. Retiring a *family* means rewriting the
answers already stored against it, which is somebody's result — so the six were chosen
to be the set worth keeping, and adding a seventh should be as reluctant a decision as
this one was.

**Deliberately not built.** The catalogue's study-material half — mind maps, cheat
sheets, formula sheets, revision guides, ELI10 explanations — and its gamification half
— XP, streaks, boss levels, survival mode. Neither is an assessment. The first is the
tutor's job and belongs in chat; the second is a retention mechanic, and attaching one
to a proctored exam a person's mark depends on is a product decision this codebase has
not made. Adaptive and spaced-repetition quizzing is a real omission rather than a
rejected one: both need a per-person history of what they got wrong, which is a data
model (`attempts` is one sitting per paper) rather than a question format.

## D26 — A ZIP upload is one book in parts, not N books

**The situation.** Real textbooks arrive split. NCERT — the corpus a large share of
Indian students actually study from — publishes each book as a ZIP of per-chapter PDFs,
and asking people to merge sixteen files by hand before uploading is a toll booth in
front of the thing they came to do. Before this, the upload form rejected the ZIP and
the workaround was sixteen separate books, which is worse than inconvenient: chapter
selection, coverage sampling, and "summarize this book" all treat a book as the unit,
so a book uploaded as sixteen books is sixteen things the product can no longer reason
about together.

**Choice.** A ZIP is accepted as **one book uploaded in parts**, and the parts are
combined — not fanned out into N `books` rows.

1. **The archive stays the stored source object.** No merged PDF is written back to
   Storage. The members are combined *at parse time*, which keeps ingest trivially
   idempotent (re-ingest re-reads the original upload), keeps deletion one object, and
   avoids the failure window where a merged file exists but the row still names the
   ZIP — the class of orphaned-object bug the delete task was built to prevent.
2. **Reading order is a natural sort of member paths** (`ch2.pdf` before `ch10.pdf`).
   Whoever split the book named the parts in order; digit runs compare numerically so
   the order survives past nine parts.
3. **Page numbers continue across the seams**, so a citation still points at exactly
   one place in the combined book and provenance survives the pipeline unchanged.
4. **Every seam is a chapter boundary.** The split points are ground truth — an
   NCERT-style zip of one-chapter files gets one chapter per file, named from the
   filename. A part whose own PDF outline names two or more chapters contributes those
   instead, pages made absolute. Either way no chunk ever spans two uploaded files.
5. **Members are judged by their bytes, not their names** — the same sniffing rule as
   top-level uploads. Junk (`__MACOSX/`, dotfiles, images) is skipped when documents
   exist; a member that *should* parse and does not fails the book loudly with the
   member's name, because a book silently missing chapter 7 is worse than one that
   failed. Nested archives are not recursed into: one level of "in parts" is the
   product.
6. **Decompression is capped on bytes produced, not bytes declared.** `MAX_UPLOAD_MB`
   bounds only the wire; a ZIP header's declared size is attacker-controlled, so
   `ZIP_MAX_UNCOMPRESSED_MB` and `ZIP_MAX_MEMBERS` are enforced during extraction, and
   deterministic failures (`UnparseableDocument`) go straight to `status=failed` with
   the reason instead of burning three retries.

**Rejected: fan out into N books.** It answers the wrong question — nobody studying
"Mathematics" wants sixteen library entries — and it would have made the upload route
mint rows the caller never asked for, each needing its own status, retry and delete
lifecycle.

**Rejected: merge to a single PDF in Storage.** Cleaner for a hypothetical future
page-image viewer, but it doubles storage, adds a repoint-the-row step whose partial
failure orphans an object, and buys nothing today — every consumer downstream of
`parse()` already works on extracted text.

**Cost.** Chapter detection re-walks the archive after parsing (cheap: page counts and
outlines, no second text extraction for PDFs, which are the bulk of any real book).
Mixed-format zips synthesize page numbers for non-PDF members, so "page 40" of a
zip-of-DOCX is a position, not a printable page — the same trade DOCX already makes
alone. `.env` files predating this change list `ALLOWED_SOURCE_FORMATS` without `zip`
and must be updated by hand; that the gate is a per-environment allowlist is the point.

**Reversal.** Drop `zip` from `ALLOWED_SOURCE_FORMATS` and the format is refused at
upload again; already-ingested books keep working, since their chunks and chapters are
ordinary rows and the enum value stays behind (removing a Postgres enum value is not
worth the surgery).

## D27 — The sitter writes proctoring data through SECURITY DEFINER functions

**The situation.** Phase 7's capture loop needs the sitter's browser to open a
session, batch events, heartbeat, and close — but the proctoring privacy invariant
rests on the sitter having **no RLS policy at all** on `proctor_sessions` and
`proctor_events`, so that `released_at IS NULL ⇒ they see nothing` cannot be defeated
by a forgotten WHERE. No policy means no INSERT or UPDATE either: the tables the
sitter must feed are tables the sitter must not touch.

**Choice.** All sitter writes go through four `SECURITY DEFINER` functions
(`open_proctor_session`, `record_proctor_events`, `proctor_heartbeat`,
`close_proctor_session`) — the same pattern `write_audit_log` established. Each
derives the caller from `auth.uid()` and the attempt row, and returns an `outcome`
value the service maps to domain errors.

Two consequences worth naming:

1. **The severity map lives in SQL** (`public.proctor_severity`, IMMUTABLE), not in
   Python. Anything granted EXECUTE to `authenticated` is callable over PostgREST
   rpc with a bare session token, so the functions must defend themselves rather
   than trust the API to have sanitized the payload: severity, evidence paths,
   `received_at` ordering, the clock-skew check, batch and rate caps all happen
   inside the function. The Python copy in `services/proctoring.py` exists for the
   scoring weights, and `tests/test_proctoring.py` parses the migration to hold the
   two maps equal.
2. **Retention is not a parameter.** `evidence_purge_after` and the integrity score
   are written by the worker (`aggregate_session`) from server config — a definer
   function that accepted a retention argument would let any sitter shorten the life
   of their own evidence with one rpc call.

**Rejected: granting the sitter narrow INSERT/UPDATE policies.** RLS cannot restrict
columns, so an UPDATE policy for heartbeats would also permit writes to
`review_status` and `released_at`; per-column GRANTs could patch that, but the
severity assignment would still live client-side of the boundary. **Rejected:
routing writes through the service role in the API.** It centralizes trust in
route code and leaves the rpc surface open anyway — the functions exist as callable
objects either way, so they must be safe either way.

**Cost.** The write path is plpgsql, which is harder to unit-test than Python — the
suite covers the mirrors and the arithmetic, and the functions were exercised
against the live database by hand. Schema changes to the event payload now touch a
migration, not just a Pydantic model.

**Reversal.** Cheap in interface terms: the routes and service signatures would
survive a move to policy-based writes; drop the functions, add the policies, and
re-point the service at the ORM. What does not survive is the guarantee — the
absence of a sitter policy is currently load-bearing, and any reversal has to
re-derive "the sitter can never read these tables" some other way.

## D28 — Ingest gets the same Advanced panel the tutor and generation already have

**The gap.** Ingest is the longest wait in the product and the least legible. The book
card showed a five-step progress bar, which says *which* stage is running and nothing
about what it found — and the machinery that could answer runs in a Celery worker whose
logs the owner cannot read. ZIP upload (D26) turned that from a rough edge into a real
question: a ZIP is N files becoming one book, and "did it find all eighteen chapters,
in what order, did it skip one" is not answerable from a page count.

**Choice.** A third trace, deliberately the same as the two that exist rather than a
new idea: `books.ingest_trace jsonb`, written by the worker, rendered by an Advanced
disclosure on the book card that is closed by default and in-flow. Steps share the
`{step, detail, ms}` shape of the chat pipeline panel and `assessments.generation_trace`,
so all three read the same way and the components stayed siblings instead of variants.

Three things it records that nothing else in the UI can say:

1. **The archive manifest** — every part, in the order it was combined. The reason the
   panel exists for a ZIP.
2. **Where the chapters came from** — a PDF outline, one per archive part, or
   `whole document (no outline found)`. A book with one synthetic chapter is a book
   with nothing to select from when generating a paper, and that fact was invisible.
3. **Where the wall clock went.** On the measured CPU stack a 276-page NCERT book is
   25s, of which 24s is embedding — so the panel says plainly that indexing is the
   cost and parsing is free, which is the opposite of what people guess.

**Persisted, and written on failure too.** Chat's trace rides the SSE stream and
vanishes because the transcript records the conversation, not the machinery. Ingest
finishes minutes after the upload request returned, so it has to be a column — and a
*failed* run is precisely the run whose trace somebody wants, so the failure handler
stores it and the panel says which stage the book got to before it stopped.

**Content-free, and owner-only, which are two separate rules.** A canon book's row is
readable by every signed-in user and RLS cannot hide a column, so nothing from inside
the book may enter a trace: the recorder accepts counts, durations, format names,
member filenames and fixed reason strings, and no parameter carries page or chunk text.
On top of that the serializer blanks the field for anyone but the owner
(`api/v1/books.py :: _to_read`), because how somebody else's upload was processed is
not a reader's business. The second rule is the narrow one; the first is what makes a
future refactor forgetting the second survivable.

**Cost.** A jsonb column on every book, and one more place to update when a pipeline
stage is added or renamed — a trace that stops describing the pipeline is worse than
none, because it is confidently wrong. The manifest is capped at 80 entries and reports
the true total, so a pathological archive cannot put a thousand filenames in a column.

**Reversal.** Drop the column and delete the panel; nothing reads the trace, and no
behaviour depends on it. The steps are recorded by one object threaded through
`_ingest`, so removing it is deleting calls rather than untangling logic.

## D29 — An author may examine on their own books, shared or not

**The rule this revises.** Since Phase 5, generation drew from `scope='canon'` chunks
only (`include_personal=False`), so an author had to share a book with the whole
platform before a paper could be written from it. That coupling was the bug in
practice: "share with everyone" and "write a paper from it" are different intents, and
forcing the first to get the second pushed private material into canon that its owner
never wanted world-readable — the opposite of the privacy the rule was defending.

**Choice.** Generation now runs under the same author-bound filter every reader gets:
`build_retrieval_filter(author)` — canon plus the *author's own* uploads, shared or
not. `fetch_generation_chunks` takes the author as its first argument and there is no
argument that widens the personal clause past that one id. The `include_personal`
switch is gone from the chokepoint entirely: every caller now has exactly one
predicate shape, which is easier to hold than a flag whose safe value depends on who
is asking.

**What invariant 1 still means.** Nobody — author or not — can ever draw from another
user's personal book. `tests/test_scoping.py` and `tests/test_generation.py` assert it
on the compiled SQL. What changed is only whose *own* material a paper may use.

**Cost, accepted deliberately.** A published paper drawn from a private book exposes
fragments of that book: stems, options and (on release) model answers quote or
paraphrase passages sitters cannot open in the library, and sitters are examined on
material they were never given to study. Both are now the author's judgement to make —
the same authority they already hold over results and evidence — and the creation UI
discloses it at the moment a private book is picked, rather than burying it in a rule.

**Reversal.** Re-add a canon-only fetch beside the author-bound one and point
`generate_questions` back at it; the chokepoint structure is unchanged. The papers
generated meanwhile keep their questions — provenance via `source_chunk_ids` still
resolves, because the chunks were the author's to use when the paper was written.

## D30 — Generation spends tokens only on what the paper keeps

**The measurement that forced it.** The generation trace on a real 10-question run
(2026-08-27): 628 seconds wall, of which 627.7 were the model — four calls at 116 to
214 seconds each. The container model decodes at ~7.5 tokens/second (prefill ~80), so
on CPU the wall clock IS the token count, and three of the four calls were paying for
tokens the paper never kept: a 170-second call whose reply overflowed `max_tokens`
mid-array and failed to parse; a `rationale` field demanded on every question, parsed,
and discarded; and a 214-second backfill call whose four questions were three
near-duplicates that end-of-run dedupe then dropped — after which the run, having
stopped at "ten accepted", still shipped seven.

**Choice.** Four rules, all token rules:

1. **Ask only for fields that are stored.** `rationale` is out of `FAMILY_SHAPE`;
   `GeneratedQuestion` still tolerates it, so a model that volunteers one costs only
   its own time.
2. **The ask is capped by the reply budget.** `FormatSpec.reply_tokens` estimates one
   question's reply cost, generously; `formats.batch_ask_cap` keeps `asked ×
   reply_tokens` inside `assessment_reply_max_tokens`, so a truncated — and therefore
   wholly wasted — call becomes a smaller successful one plus a backfill.
3. **Duplicates are rejected the moment they arrive.** `_StemDeduper` embeds each
   call's accepted stems inline (milliseconds against minutes), so the count the
   backfill steers by is the count the paper will keep, and accepted stems ride along
   in the next prompt as a do-not-repeat list.
4. **The ask first, the passages last — kept that way after measuring the reverse.**
   Reordering to passages-first (so the stable prefix could be reused from the
   provider's prompt cache, ~30 seconds of prefill a call) was implemented and
   reverted the same day: with the ask at the end, the 3B model returned one
   question when asked for four and produced un-parseable JSON in three calls of
   four, on a book that had been yielding full batches. A cached prefill saves
   seconds; a failed call wastes the whole call. Re-trying the reordering requires
   re-measuring both sides on the weakest supported model.

**What was rejected.** Parallel calls: on a saturated CPU two concurrent calls each
run twice as slow, which converts two working calls into two timeouts — concurrency
is a deploy-time (OpenAI) optimisation, not a local one, and it would also have to
unshare the worker session the checkpoints commit on. Salvaging truncated JSON:
rule 2 removes the cause; repairing the symptom would blur "parsed" and "patched".

**Reversal.** Each rule reverses independently: put `rationale` back in the shapes,
delete the cap from `ask()`, move the dedupe back after the loop, reorder the prompt.
Nothing in the schema or the stored rows depends on any of them.
