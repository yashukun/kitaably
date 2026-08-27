"""A model's enum type name must be one the migrations actually left behind.

This exists because `Profile.role` spent a phase naming `public.user_role`, a type
migration 20260824120000 had dropped and replaced with `public.app_role`.

What made it survive so long is worth stating, because it is the reason a test is
needed rather than a careful reading: **a stale enum name does not fail on SELECT.**
Reading a row returns the value as text and coerces it Python-side, so `/me`, every
guard and every login worked perfectly. It fails only when the column is *bound as a
parameter*, because the asyncpg dialect renders the cast itself::

    WHERE profiles.role = $1::public.user_role
    -> UndefinedObjectError: type "public.user_role" does not exist

So the first filter or write on that column is where it goes off — a route that
worked in review, failing in production on a code path nothing had exercised yet.

Parsed from the migration files rather than a live database, to match the rest of
this suite: the schema is `supabase/migrations/`, and it is readable without one.
"""

import re

import pytest
from sqlalchemy import Enum as SAEnum
from sqlalchemy import inspect

from app.db.base import Base
from app.db.models import *  # noqa: F401,F403  (import registers every mapper)
from tests.migrations import migration_sql

# `create type public.foo as enum (...)` / `drop type public.foo`. The schema
# qualifier is optional because a migration may or may not spell it out.
_CREATE = re.compile(
    r"create\s+type\s+(?:public\.)?(\w+)\s+as\s+enum", re.IGNORECASE
)
_DROP = re.compile(
    r"drop\s+type\s+(?:if\s+exists\s+)?(?:public\.)?(\w+)", re.IGNORECASE
)


def _enum_types_after_every_migration() -> set[str]:
    """The enum types a fresh `supabase db reset` leaves in `public`.

    Applied in filename order, which is the order the CLI applies them, so a type
    that is created and later dropped correctly ends up absent.
    """
    live: set[str] = set()
    for sql in migration_sql():
        # Statement order within one file matters as much as file order: 20260824120000
        # creates `app_role` and drops `user_role` in the same file.
        events = [(m.start(), m.group(1).lower(), True) for m in _CREATE.finditer(sql)]
        events += [(m.start(), m.group(1).lower(), False) for m in _DROP.finditer(sql)]
        for _, name, created in sorted(events):
            live.add(name) if created else live.discard(name)
    return live


def _named_enum_columns() -> list[tuple[str, str, str]]:
    """(model, column, enum type name) for every mapped column with a native enum."""
    found = []
    for mapper in Base.registry.mappers:
        for column in inspect(mapper.class_).columns:
            type_ = column.type
            if isinstance(type_, SAEnum) and type_.name:
                found.append((mapper.class_.__name__, column.key, type_.name))
    return found


def test_the_enum_sweep_is_not_empty() -> None:
    """The test below is parametrised over this list. An import that quietly stopped
    registering mappers would turn it into zero passing cases, which reads as green."""
    assert len(_named_enum_columns()) >= 4


def test_the_migrations_declare_enum_types() -> None:
    """Likewise for the other half: a parser that matched nothing would pass."""
    assert len(_enum_types_after_every_migration()) >= 10


@pytest.mark.parametrize(
    "model,column,type_name",
    _named_enum_columns(),
    ids=lambda value: str(value),
)
def test_model_enum_names_exist_in_the_migrations(
    model: str, column: str, type_name: str
) -> None:
    live = _enum_types_after_every_migration()
    assert type_name.lower() in live, (
        f"{model}.{column} is typed `public.{type_name}`, which no migration leaves "
        f"behind. Reads will still work; the first query that binds {column} as a "
        f"parameter will fail with `type \"public.{type_name}\" does not exist`. "
        f"Types the migrations do create: {', '.join(sorted(live))}."
    )
