"""profiles - application facts about an auth.users row. Phase 1.

id (pk, references auth.users on delete cascade), email citext unique, name,
role enum(user), avatar_url, created_at.

role is read from this table on every request. Never from a JWT claim.

RLS: a user selects and updates their own row, and only their own row. There is no
policy that lets one user read another's profile -- a name is learned from an
attempt or a shared book, not from a directory.

Mirrors supabase/migrations/. SQLAlchemy does not own this schema and never
creates it; CI boots a database from the migrations and asserts the model matches.
"""

import uuid
from datetime import datetime

from sqlalchemy import Enum as SAEnum
from sqlalchemy import String
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps
from app.db.models.enums import Role


class Profile(Base, Timestamps):
    __tablename__ = "profiles"

    # No server_default here: the id is auth.users(id), assigned by GoTrue and
    # copied across by the signup trigger. Nothing in this application mints one.
    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)

    email: Mapped[str] = mapped_column(CITEXT, nullable=False, unique=True)
    name: Mapped[str | None] = mapped_column(String, nullable=True)

    role: Mapped[Role] = mapped_column(
        SAEnum(
            Role,
            # `app_role`, not `user_role`: migration 20260824120000 replaced the
            # teacher/student type with a single-member one and dropped the old
            # name. A stale name here does not fail on SELECT -- it fails the moment
            # `role` is *bound as a parameter*, because asyncpg renders the cast
            # `$1::public.user_role`. So it survived every read and would have gone
            # off on the first write. tests/test_enum_names.py now checks the names
            # against the migrations so the next rename cannot hide the same way.
            name="app_role",
            schema="public",
            # The type is created by the migration. SQLAlchemy must never emit
            # CREATE TYPE — two owners of one schema is how they drift apart.
            create_type=False,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
    )

    avatar_url: Mapped[str | None] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
