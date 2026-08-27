# Migrations

The schema lives here (DECISIONS.md D7). SQLAlchemy models in
`backend/app/db/models/` mirror it for typed access; they do not own it and they
never create it.

## Rules

- **Forward-only.** A new timestamped file per change:
  `supabase migration new <name>`. Never edit a migration that has been applied
  anywhere else — including on someone else's laptop.
- **RLS ships with the table.** A migration that creates a table also contains
  `alter table … enable row level security` and every policy, in the same file. A
  table with RLS off is a review failure, and a table with RLS on but no policy
  denies by default, which is the correct starting point.
- **Additive.** On a release, migrations apply before images, so both versions of
  the application are live for a few seconds. Add a column, backfill, switch reads,
  drop later. Never rename in place.
- **Enums are constrained here.** Adding a value to a Python enum means a migration;
  the Python side alone is not the source of truth.

## Order

`docs/DATA-MODEL.md` is the specification. Phases fill this directory:

| Phase | Tables |
|---|---|
| 1 | `profiles` (+ the signup trigger that populates it) |
| 2 | *(originally `classrooms`, `enrollments` — both since dropped, see below)* |
| 3 | `books`, `chapters`, `chunks` (`vector(384)`, HNSW index, scope trigger) |
| 4 | `chat_sessions`, `chat_messages` |
| 5 | `assessments`, `questions` |
| 6 | `attempts`, `answers`, `audit_log` |
| 7 | `proctor_sessions`, `proctor_events` |
| 8 | the released-report view (upheld events only, `released_at IS NOT NULL`) |

## The two revision migrations

`20260824120000_single_role_shared_library.sql` removed roles and classrooms
(DECISIONS.md D16): it drops `classrooms`, `enrollments`, the membership helpers, the
roster view and both `classroom_id` columns, replaces `user_role` with a single-valued
`app_role`, and reduces the `books`/`chunks` policies to
`owner_id = auth.uid() or scope = 'canon'`.

`20260824133000_propagate_scope_as_definer.sql` fixes what that exposed. Sharing a book
is the first time an ordinary user causes an UPDATE on `books`, which fires
`propagate_book_scope()`, which writes `chunks` — a table `authenticated` may only read.
As an invoker-rights function it was refused and took the transaction with it. It is
`SECURITY DEFINER` now.

That second file is a separate migration rather than an edit to the first, even though
the first was minutes old, because it had already been applied. **That is the rule doing
its job**, not an inconvenience: an applied migration is a fact about a database
somewhere, and the ledger records a checksum.

Phases 5 and 6 added three more, and two of them are the same story again:

| File | What |
|---|---|
| `20260824150000_assessments` | `assessments`, `questions`, `attempts`, `answers`, their RLS, and the `question_sit` / `question_key` views |
| `20260824160000_attempt_sitter` | the view that lets an author see who sat their paper |
| `20260824170000_question_key_renders` | `question_key` gains `index` and `stem` so a marked paper renders from one source |
| `20260824180000_drop_access_mode` | removes password-protected links, which were modelled and never enforced (DECISIONS.md D17) |
| `20260825140000_question_format_types` | the format taxonomy's enums: four grading families, three cognitive levels, `question_format`, `assessment_rigor` |
| `20260825141000_question_formats` | `questions.format` / `prompt_items` / `answer_key`, `assessments.generation_spec` / `rigor`, the shape constraints, and both views rebuilt (DECISIONS.md D25) |
| `20260825142000_question_shape_null_guard` | the four shape constraints again, this time able to fail |
| `20260825150000_generation_note` | `assessments.generation_note` — a paper that came back short has to say so |
| `20260825160000_generation_trace` | `assessments.generation_trace` — the pipeline trace, content-free because a sitter can read the row and RLS cannot hide a column |

Those two are one change in two files, and the split is not stylistic. `alter type ...
add value` may run inside a transaction on Postgres 12+, but **the value it adds cannot
be used in the same transaction** — and the CLI wraps each file in one. A single file
that added `'match'` and then wrote `check (type <> 'match' or ...)` fails with
`unsafe use of new value "match"`. Types in the first file, everything that uses them in
the second.

The second also has to *backfill before it constrains*: `format` defaults to `'mcq'`,
which lands on every existing subjective question, whose family is `subjective` — so the
family check would validate rows the column default had just made invalid and roll the
whole ALTER TABLE back. `tests/test_formats.py` pins the ordering.

## A CHECK constraint passes on NULL

`20260825142000` exists because the four shape constraints shipped unable to fail. They
were written as

    check (type <> 'match' or (... and jsonb_exists(answer_key, 'pairs')))

and `jsonb_exists` is **strict**: a NULL input gives a NULL output, not false. So for a
match question with no answer key the expression was `false OR NULL` = NULL — and a CHECK
constraint accepts NULL. The row it existed to refuse went straight in. Demonstrated by
inserting one.

The neighbouring `questions_mcq_shape` from `20260824150000` was never affected, and the
difference is the whole rule: it is written entirely in `is not null` tests, which are
themselves never NULL. Three-valued logic only bites once a strict function joins the
chain. The fix is an explicit `answer_key is not null` in front of every `jsonb_exists`,
because `false AND NULL` is a definite false.

Nothing in the application could produce such a row — `build_question_fields` refuses
every one of them with a reason. That is exactly the argument these constraints exist so
nobody has to make: they are the second line, for when the application is wrong, and a
second line that evaluates to NULL is decorative.
`tests/test_formats.py :: test_no_check_constraint_leaves_a_strict_function_unguarded`
reads the surviving constraint bodies back out of the migrations and enforces the rule.

The last was a bug: `result_view` walked `question_sit` — the *sitting* projection,
scoped by "does the caller have an attempt" — to list a marked paper's answers. Correct
for the sitter, and correctly empty for the author, who wrote the paper and never sat it.
The score came back right and the breakdown was blank.

## Row security cannot hide a column

Worth stating once, because it shapes three objects in `20260824150000`.

A `questions` row carries `correct_option`, `model_answer` and `rubric`. RLS decides
which *rows* a caller sees, never which columns, so "let the sitter read the question but
not the answer" is not expressible as a policy. Column-level `GRANT` can express it, but
grants are per-role and every user here is `authenticated`, so revoking the answer columns
would take them from the author too.

So a sitter has **no policy on `questions` at all**, and reads `public.question_sit`,
which does not select those columns. The absence is the enforcement: a `select *` in a
future refactor cannot leak what the view never selected, and a Pydantic schema that
forgot to drop a field has nothing to drop.

The earlier `20260823*` files still describe classrooms. Leave them. They are an accurate
record of the schema at the moment they ran, and rewriting history to match the present
is what forward-only exists to prevent.
