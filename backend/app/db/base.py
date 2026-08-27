"""Declarative base and the mixins every table shares.

SQLAlchemy models mirror the schema defined in ``supabase/migrations/``. They do not
own it and they never create it (DECISIONS.md D7), so nothing here emits DDL.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    #: A model mapped onto a database VIEW rather than a table. Nothing inserts into
    #: one, so the rules about id generation and server defaults do not apply — and
    #: `tests/test_model_defaults.py` skips them on the strength of this flag rather
    #: than on a list of names that would go stale.
    __read_only__: bool = False


class UUIDPrimaryKey:
    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )


class Timestamps:
    """Both columns are maintained by the database.

    No ``onupdate`` here: every table carries a ``touch_updated_at`` trigger, and two
    mechanisms writing one column disagree the moment a row is changed by anything
    other than this ORM -- which the Celery worker will do.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
