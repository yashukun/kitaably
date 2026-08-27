"""Columns the database fills in must say so in the model.

This exists because the same bug landed twice. A column declared NOT NULL whose
value comes from a database DEFAULT needs `server_default` on the model too —
without it SQLAlchemy includes the column in the INSERT as NULL instead of omitting
it and letting the default fire, and the NOT NULL constraint rejects the row.

It fails at runtime, on the first insert, with a NotNullViolationError that names the
column but not the cause. Cheaper to catch here.
"""

import pytest
from sqlalchemy import inspect

from app.db.base import Base
from app.db.models import *  # noqa: F401,F403  (import registers every mapper)

# Names whose value is always supplied by the database rather than by application
# code. Anything here that is NOT NULL must carry a server_default.
DATABASE_FILLED = {"created_at", "updated_at"}


def _mapped_models() -> list[type]:
    """Every mapped table. Views are excluded — nothing inserts into one, so neither
    rule below has anything to say about them."""
    return [
        mapper.class_
        for mapper in Base.registry.mappers
        if not getattr(mapper.class_, "__read_only__", False)
    ]


def test_the_model_sweep_is_not_empty() -> None:
    """Both tests below are parametrised over this list. An import that quietly stopped
    registering mappers would turn them into zero passing cases, which reads as green."""
    assert len(_mapped_models()) >= 8


@pytest.mark.parametrize("model", _mapped_models(), ids=lambda m: m.__name__)
def test_database_filled_columns_declare_a_server_default(model: type) -> None:
    for column in inspect(model).columns:
        if column.key not in DATABASE_FILLED or column.nullable:
            continue
        assert column.server_default is not None, (
            f"{model.__name__}.{column.key} is NOT NULL and filled by the database, "
            "but has no server_default — SQLAlchemy will INSERT NULL into it."
        )


@pytest.mark.parametrize("model", _mapped_models(), ids=lambda m: m.__name__)
def test_primary_keys_are_generated_somewhere(model: type) -> None:
    """Either the database mints the id, or something explicitly assigns it."""
    for column in inspect(model).primary_key:
        generated = column.server_default is not None or column.default is not None
        # profiles.id is the exception: it is auth.users(id), copied in by the signup
        # trigger, so nothing in this application ever mints one.
        if model.__name__ == "Profile":
            continue
        assert generated, f"{model.__name__}.{column.key} has no id generation"
