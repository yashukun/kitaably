"""Read `supabase/migrations/` from a test.

The schema lives in SQL, so the tests that check the models against it have to read
that SQL. Kept here rather than in `conftest.py` because it is a plain helper and not
a fixture, and because more than one test wants it.
"""

import pathlib

# backend/tests/migrations.py -> repo root -> supabase/migrations
MIGRATIONS_DIR = (
    pathlib.Path(__file__).resolve().parents[2] / "supabase" / "migrations"
)


def migration_files() -> list[pathlib.Path]:
    """Every migration, in the order the CLI applies them.

    Timestamped filenames sort chronologically, which is the whole point of the
    naming convention.
    """
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:  # a wrong path here would silently pass every caller
        raise AssertionError(f"no migrations found under {MIGRATIONS_DIR}")
    return files


def migration_sql() -> list[str]:
    """The contents of every migration, in apply order, comments stripped.

    Comments go because these files explain themselves at length, and a caller
    grepping for `drop type` should not match the sentence describing one.
    """
    return [_strip_comments(path.read_text()) for path in migration_files()]


def _strip_comments(sql: str) -> str:
    """Remove `--` line comments, leaving the line so offsets stay roughly sane.

    Not a real parser: it does not know about `--` inside a string literal. No
    migration here has one, and the alternative is a SQL lexer for a test helper.
    """
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
