# Data model

Supabase Postgres is the source of truth for everything except file bytes: rows, chunk
text, **and** embedding vectors all live here. Supabase Storage holds bytes only; every
object is referenced by a row. Redis holds nothing that matters after a restart.

Conventions: `id` is `uuid` (default `gen_random_uuid()`), `created_at`/`updated_at` are
`timestamptz`, enums are Postgres enum types mirroring Python enums, deletes are soft
where evidence or grades depend on the row.

The schema is defined by SQL files in `supabase/migrations/`, applied with the Supabase
CLI. SQLAlchemy models in `backend/app/db/models/` mirror that schema for typed access —
they do not own it, and they never create it.

**RLS is on for every table in `public`, with no permissive default.** Each table below
lists its policy intent; the SQL lives beside the table definition in the same migration.

---

## Identity and scoping

### profiles
Mirrors `auth.users` (managed by Supabase Auth) with the application's own facts.

| column | type | notes |
|---|---|---|
| id | uuid pk | **references `auth.users(id)` on delete cascade** |
| email | citext unique | copied on signup by trigger |
| name | text | copied from signup metadata by trigger |
| role | enum(`user`) | one value; nothing branches on it (DECISIONS.md D16) |
| avatar_url | text null | |
| created_at | timestamptz | |

The caller is read from this table on every request, never from a JWT claim — a claim is
client-visible and, in some flows, client-influenced. A Supabase user can rewrite their
own `user_metadata` with one authenticated `PUT /auth/v1/user`, so the token's idea of
who they are is worth exactly one field: `sub`.

`role` holds `'user'` for everybody and is kept as a column rather than deleted, so that
reintroducing a distinction is `alter type public.app_role add value ...` plus policies,
not a new identity model. **Nothing in the application reads it to make a decision.**

The signup trigger copies `name` out of metadata and nothing else. It used to read a
requested `role` from there and fail closed to `student`; there is no longer any
authority for client-supplied metadata to touch.

RLS: a user selects and updates their own row, and only their own row. There is no policy
admitting anyone to anyone else's — see open question 10 for what that costs.

> **Removed in D16.** `classrooms` and `enrollments`, the `is_classroom_teacher` /
> `is_active_enrollee` helpers, the `classroom_roster` view, and the `user_role` enum.
> Membership is no longer a fact this database stores. `supabase/migrations/20260824120000_single_role_shared_library.sql`
> is the removal, and it says why in full.

## Material

### books
| column | type | notes |
|---|---|---|
| id | uuid pk | |
| owner_id | uuid fk profiles | |
| scope | enum(`canon`,`personal`) | **the security-critical column** |
| title, author | text | |
| source_format | enum(`pdf`,`docx`,`pptx`,`txt`,`md`) | selects the parser |
| storage_path | text | `books/{owner_id}/{book_id}/source.{ext}` |
| byte_size | bigint | enforced against `MAX_UPLOAD_MB` before the row is written |
| page_count | int null | pages for PDF, slides for PPTX, null for flat text |
| status | enum(`uploaded`,`parsing`,`chunking`,`embedding`,`ready`,`failed`) | |
| error | text null | user-facing failure reason |
| needs_ocr | bool | set when text density is too low to trust |
| created_at, updated_at | timestamptz | |

Every book is created `personal`, whoever uploaded it. Sharing is a separate `PATCH
/books/{id}/scope` by the owner, and it writes an `audit_log` row in both directions —
it is the one action in the product that changes who can read something.

Index: `(scope, status)`, `(owner_id, scope)`.

RLS: `owner_id = auth.uid() OR scope = 'canon'`. That is the whole rule, for both `books`
and `chunks`. **No policy grants anyone access to another user's `personal` row**, and
there is no account for which the predicate is wider.

### chapters
`id`, `book_id`, `index` (int, ordering), `title`, `page_start`, `page_end`.
Unique `(book_id, index)`. RLS: inherits the parent book's visibility.

### chunks
| column | type | notes |
|---|---|---|
| id | uuid pk | |
| book_id, chapter_id | uuid fk | |
| index | int | order within book |
| text | text | **source of truth for chunk text** |
| embedding | `vector(384)` | `bge-small-en-v1.5`, cosine |
| page | int | for citation deep-links |
| token_count | int | |
| owner_id, scope | denormalised | copied from `books` so the retrieval predicate needs no join |

Denormalising the two scope columns onto `chunks` is deliberate: the hot path is a
vector search with a filter, and a join there costs both latency and the chance of
someone writing the filter against the wrong alias. Two triggers keep them in step with
`books` and they are never written by application code — `sync_chunk_scope()` overwrites
whatever an INSERT supplied, and `propagate_book_scope()` carries a share or unshare down
to every chunk in the same transaction.

`propagate_book_scope()` is `SECURITY DEFINER`. It has to be: `authenticated` may read
`chunks` and not write it, so as an invoker-rights function the trigger was refused and
took the whole share transaction with it. Granting the write instead would have been
worse — `chunks` has no UPDATE policy, so the grant would have matched zero rows and the
share would have *appeared* to succeed while the chunks kept the old scope. A stale
access grant that reports success is the failure mode worth avoiding here.

Indexes:
```sql
create index on chunks using hnsw (embedding vector_cosine_ops);
create index on chunks (book_id, index);
create index on chunks (scope) where scope = 'canon';
create index on chunks (owner_id) where scope = 'personal';
```

RLS: same rule as `books`, expressed on the denormalised columns.

## Chat

### chat_sessions
`id`, `user_id`, `title`, `created_at`. RLS: owner only.

No scope column. What a conversation may reach is recomputed from the principal on
every question — a scope frozen onto the session would go on answering from material
the owner has since lost access to.

### chat_messages
`id`, `session_id`, `role` enum(`user`,`assistant`), `content` text,
`citations` jsonb `[{chunk_id, book_id, page, scope}]`, `token_usage` jsonb null,
`created_at`. RLS: through the session's owner.

`scope` is carried in the citation so the UI can distinguish *class book* from
*your upload*.

## Assessment

### assessments
| column | type | notes |
|---|---|---|
| id | uuid pk | |
| author_id | uuid fk profiles | whoever created it; holds authority over its results |
| title | text | |
| type | enum(`mcq`,`subjective`,`mixed`) | derived from the formats, not taken from the client |
| source_selection | jsonb | `{book_ids:[], chapter_ids:[]}` |
| generation_spec | jsonb | `{formats:[], levels:[], instructions, auto}` — what was asked for. `{}` means auto |
| rigor | enum(`beginner`…`research`) | how hard, for the whole paper. Steers register only |
| question_count | int | requested; actual = count of rows |
| duration_minutes | int | server-authoritative |
| status | enum(`draft`,`generating`,`published`,`closed`) | |
| share_token | text unique null | issued on publish, revocable |

| proctoring_enabled | bool | |
| results_release | enum(`immediate`,`on_review`) | |
| opens_at, closes_at | timestamptz null | |
| max_score | numeric null | frozen at publish |

RLS: the author, full access; anyone else selects a row only once they have an attempt
on it. The share token is resolved by a SECURITY DEFINER function rather than a policy,
because a prospective sitter cannot see the row that starting would let them see.

### questions

Two columns describe what a question is, and keeping them apart is the design
(DECISIONS.md D25): `format` is the **shape** the author picked and the sitter sees
(fourteen), `type` is the **grading family** (six, one marking function each). Seven
formats mark as `mcq`, because a true/false really is a two-option multiple choice.
Postgres holds the mapping as a check constraint; a row where the two disagree is
refused.

| column | type | notes |
|---|---|---|
| id | uuid pk | |
| assessment_id | uuid fk | |
| index | int | |
| type | enum(`mcq`,`multi_select`,`short_text`,`match`,`sequence`,`subjective`) | the grading family |
| format | enum — 14 values | the shape. `mcq`, `true_false`, `yes_no`, `fill_blank`, `assertion_reason`, `scenario`, `flashcard`, `multi_select`, `match`, `sequence`, `one_word`, `numeric`, `short_answer`, `long_answer` |
| stem | text | |
| options | jsonb null | `[{key:'A', text:'…'}]` — the choice list, for every format that has one |
| correct_option | text null | the `mcq` family only |
| prompt_items | jsonb null | sitter-visible: the left column of a match grid. Half the question, not the answer |
| answer_key | jsonb null | **never sitter-visible.** `{correct_options}` \| `{accepted, tolerance}` \| `{pairs}` \| `{order}` |
| model_answer | text null | subjective only |
| rubric | jsonb null | `[{criterion, points}]` |
| points | numeric | one mark per independently marked part, so partial credit divides evenly |
| difficulty | enum(`recall`,`understand`,`apply`,`analyze`,`evaluate`,`create`) | the cognitive level. The column keeps the older name |
| source_chunk_ids | jsonb | provenance — every question traces to material |
| origin | enum(`generated`,`edited`,`written`) | |

`source_chunk_ids` is not optional. A question with no provenance cannot be defended to
someone who disputes it.

`answer_key` is one column rather than four because the rule that matters is that it is
**absent** from `public.question_sit`. One column is one thing to keep out of one view;
four is four chances for the fifth format to add a fifth and forget.

RLS: the author, full access. **A sitter has no policy on this table at all** — RLS
decides which rows a caller sees, never which columns, so "read the question but not the
answer" is not expressible as a policy. They read `public.question_sit`, which does not
select `correct_option`, `answer_key`, `model_answer` or `rubric`. The absence is the
enforcement, and `tests/test_formats.py` reads the view's select list back out of the
migration to keep it that way.

### attempts
| column | type | notes |
|---|---|---|
| id | uuid pk | |
| assessment_id, student_id | uuid fk | unique together unless retakes allowed |
| status | enum(`in_progress`,`submitted`,`auto_submitted`,`voided`) | |
| started_at, submitted_at | timestamptz | |
| deadline_at | timestamptz | computed server-side at start |
| score, max_score | numeric null | |
| graded_at | timestamptz null | |
| results_released_at | timestamptz null | |

RLS: the sitter sees own; the assessment's author sees all attempts on it.

### answers
`id`, `attempt_id`, `question_id` (unique together), `response` text null,
`awarded_points` numeric null, `grader` enum(`auto`,`llm`,`human`) null,
`feedback` text null, `llm_rationale` jsonb null, `updated_at`.

**One `response` column, whatever the format.** The option key for `mcq`, prose for
`subjective`, and compact JSON for the structured families: `["A","C"]` for a select-all,
`{"1":"B"}` for a match grid, `["C","A","B"]` for an ordering. A second column beside it
would raise a question with no good answer — which one holds the answer when both are
populated — and the grader would have to guess. Null means unanswered, which grades to
zero without an LLM call; so does a response that will not parse, because a grading run
must not die on one malformed row and leave a cohort unmarked.

An override by the author sets `grader='human'` and never discards `llm_rationale` — the
original assessment stays auditable.

RLS: the sitter may insert/update own answers **only while the attempt is in progress and
before `deadline_at`**; `awarded_points`, `grader`, and `feedback` are never
sitter-writable.

## Proctoring

### proctor_sessions
| column | type | notes |
|---|---|---|
| id | uuid pk | |
| attempt_id | uuid fk unique | one session per attempt |
| status | enum(`active`,`closed`,`aborted`) | |
| started_at, ended_at | timestamptz | |
| baseline_path | text null | consented identity still in Storage |
| last_heartbeat_at | timestamptz null | drives `heartbeat_gap` detection |
| integrity_score | int null | **server-computed**, 0–100 |
| review_status | enum(`pending`,`cleared`,`flagged`,`released`) | |
| reviewed_by | uuid fk profiles null | |
| reviewed_at | timestamptz null | |
| reviewer_note | text null | shown to the sitter on release |
| released_at | timestamptz null | **null ⇒ the sitter sees nothing at all** |
| evidence_purge_after | timestamptz | retention TTL |

RLS: the assessment's author, always. The sitter: **no policy at all** on this table.
Sitter-visible proctoring data is served through a released-report view that exists only
for `released_at IS NOT NULL`, exposing upheld events only. Absence of a policy is the
enforcement — it cannot be defeated by a forgotten `WHERE`.

### proctor_events
| column | type | notes |
|---|---|---|
| id | uuid pk | |
| proctor_session_id | uuid fk | |
| occurred_at | timestamptz | client clock, advisory |
| received_at | timestamptz | server clock, authoritative for ordering |
| type | enum — see below | |
| severity | enum(`info`,`low`,`medium`,`high`) | assigned server-side from type |
| confidence | real null | detector confidence, 0–1 |
| duration_ms | int null | |
| occurrences | int | coalesced episode count |
| evidence_path | text null | Storage still, high-severity only |
| metadata | jsonb | detector-specific detail |
| author_verdict | enum(`unreviewed`,`dismissed`,`upheld`) | default `unreviewed` |

Index: `(proctor_session_id, received_at)`, `(proctor_session_id, severity)`.

**Event types** — `session_start`, `session_end`, `heartbeat_gap`, `no_face`,
`multiple_faces`, `face_mismatch`, `gaze_away`, `head_pose_away`, `phone_visible`,
`tab_blur`, `window_blur`, `fullscreen_exit`, `copy`, `paste`, `context_menu`,
`camera_denied`, `camera_stopped`, `clock_skew`, `screen_share_denied`,
`screen_share_stopped`, `multiple_displays`.

Severity is assigned by the **server** from a fixed map, not sent by the client. A
client that invents `severity: 'info'` for everything changes nothing.

## Operations

### audit_log
The human record, separate from application logs and retained longer.

| column | type | notes |
|---|---|---|
| id | bigserial pk | |
| actor_id | uuid fk profiles null | null for system actions |
| action | text | `report.released`, `attempt.voided`, `grade.overridden`, `book.deleted`, `book.shared`, `book.unshared` |
| target_type, target_id | text, uuid | |
| metadata | jsonb | before/after where it matters |
| request_id | text null | ties the row to the request logs |
| created_at | timestamptz | |

Append-only: no update or delete policy exists for any role.

### Async task state

There is no `jobs` table. Celery keeps task state in Redis, which is disposable by
design. What a *user* needs to know lives on the domain row — `books.status`,
`books.error`, `assessments.status`, `attempts.graded_at` — and what an *auditor* needs
lives in `audit_log`. If a task's state matters after Redis is flushed, it belongs on a
row, not in the broker.

## Storage buckets

| bucket | path | who reads |
|---|---|---|
| `books` | `books/{owner_id}/{book_id}/source.{ext}` | owner; canon readable by any signed-in user |
| `evidence` | `evidence/{session_id}/{event_id}.jpg` | the assessment's author; the sitter only for upheld events after release |

Both private. Access is always a short-lived signed URL minted by the backend after the
same authorization check the API row read would get. Storage policies mirror the table
policies, so a leaked path is still not a leaked file.

## Retention

| Data | Default | Trigger |
|---|---|---|
| evidence stills | 60 days after attempt close | `purge_evidence` beat task |
| baseline still | with the session | session delete |
| raw events | kept with the attempt | assessment delete cascades |
| source documents | until owner deletes | delete removes chunks + storage object |
| `audit_log` | 2 years | manual review before any purge |

Deleting a book deletes its chunks — text and vectors together, because they are the same
row. That single property removes an entire class of leak that a separate vector store
makes possible.
