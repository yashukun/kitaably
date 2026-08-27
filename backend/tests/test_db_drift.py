"""The models against a database built from the migrations.

`supabase/migrations/` is the schema; SQLAlchemy only reads it. Nothing forces the
two to agree except this test: pointed at a database the migrations built, it asserts
every mapped relation exists there with every column the model declares. CI boots a
throwaway Postgres with the Supabase CLI and sets ``DRIFT_DATABASE_URL``; without
that variable the test skips, so the ordinary suite stays runnable without Docker.

The check is deliberately one-directional. A column the model declares and the
database lacks breaks the first query that touches it; a column the database has and
the model does not is legal SQL-only surface — the generated tsvector behind lexical
search is exactly that.
"""

import os

import pytest
from sqlalchemy import inspect
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.db import models  # noqa: F401  # imports every mapper into Base.metadata
from app.db.base import Base

# Read directly rather than through Settings: config supplies a default local URL,
# and a default must not make this test try to reach a database nobody started.
DRIFT_URL = os.environ.get("DRIFT_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DRIFT_URL is None,
    reason="no migrated database to compare against (DRIFT_DATABASE_URL unset)",
)


def _public_relations(conn: Connection) -> dict[str, set[str]]:
    """Every table and view in ``public``, with its column names."""
    inspector = inspect(conn)
    names = inspector.get_table_names(schema="public") + inspector.get_view_names(
        schema="public"
    )
    return {
        name: {column["name"] for column in inspector.get_columns(name, schema="public")}
        for name in names
    }


async def test_every_model_matches_the_migrated_schema() -> None:
    assert DRIFT_URL is not None  # skipif above; narrows for the type checker
    engine = create_async_engine(DRIFT_URL)
    try:
        async with engine.connect() as conn:
            actual = await conn.run_sync(_public_relations)
    finally:
        await engine.dispose()

    for table in Base.metadata.sorted_tables:
        assert table.name in actual, (
            f"model maps {table.name!r} but no migration creates it"
        )
        missing = {column.name for column in table.columns} - actual[table.name]
        assert not missing, (
            f"{table.name}: model declares columns the database lacks: {sorted(missing)}"
        )
