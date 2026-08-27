# API collection

Every endpoint the backend serves, in the order the product actually uses them.

## Running it

```bash
supabase start && docker compose up -d      # the stack has to be up
cd bruno
npx @usebruno/cli run --env local -r        # the whole product, ~2 minutes
```

Or open this folder in the Bruno app and pick the `local` environment.

Two folders carry a deliberate wait — generation and marking are Celery tasks on a
CPU-only Ollama, so `05-assessments/02` sleeps 75s and `06-attempts/06` sleeps 45s.
On a hosted LLM provider both are seconds; shorten the pre-request scripts.

## The folders build on each other

| Folder | What it leaves behind |
|---|---|
| `00-auth` | `token` and `token2` — two identities, because a boundary needs two sides |
| `01-health` | nothing; the only requests that need no auth |
| `02-profile` | nothing |
| `03-books` | `bookId`, ingested and shared |
| `04-chat` | `chatSessionId` |
| `05-assessments` | `assessmentId`, `shareToken` — a published paper |
| `06-attempts` | `attemptId` — sat, marked, released, voided |
| `07-boundaries` | nothing. **Every request here is expected to fail.** |
| `08-cleanup` | closes the paper and deletes the book |

Tokens and ids are written with `bru.setEnvVar`, so after running `00-auth` once you can
run any folder on its own. The CLI needs `-r` for a full pass because it starts each
invocation fresh.

## `07-boundaries` is the interesting folder

It asserts **404** rather than 403 throughout. That is deliberate and it is the same
rule everywhere in this API: a 403 on somebody else's row confirms the row exists.
`errors.Forbidden` is defined and nothing raises it — that is evidence the rule is
being followed rather than an oversight.

The single most important assertion in the collection is in
`06-attempts/02-begin-the-paper`: the sitting payload must not contain
`correct_option`, `model_answer` or `rubric`. Those columns are absent because the
sitter reads a view that never selects them — row security cannot hide a column, so a
sitter has no policy on `questions` at all.

## Two requests that pass by failing

- `03-books/07-retry-ingest` asserts **409**. Retry only accepts a book in `failed`
  state; re-running one mid-ingest would put two workers on it.
- `07-boundaries/*` — all nine.

## Not covered

There are no proctoring requests, because there are no proctoring endpoints yet
(Phases 7–8). `api/v1/proctoring.py` is registered and empty, so the shape of the API
is visible from the router without any routes existing.
