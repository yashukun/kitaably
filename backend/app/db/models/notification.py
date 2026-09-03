"""notifications — a server-delivered message to the one person who must act. Phase 6b.

Written by the worker, read by its recipient. There is no INSERT grant for
`authenticated`, so a user cannot manufacture one; the only write they get is marking
one read, and a trigger holds every other column still while they do it.

What is deliberately NOT here is a payload. A notification points at a row, it does not
copy it — a copied mark goes stale the moment the author overrides a grade, and a
notification is a much easier thing to leak than the resource it names.

Mirrors supabase/migrations/. SQLAlchemy does not own this schema and never creates it.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Text, text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKey
from app.db.models.enums import NotificationKind


class Notification(Base, UUIDPrimaryKey):
    __tablename__ = "notifications"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[NotificationKind] = mapped_column(
        SAEnum(
            NotificationKind,
            name="notification_kind",
            schema="public",
            create_type=False,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_type: Mapped[str] = mapped_column(Text, nullable=False)
    # Not a ForeignKey, matching the migration: an attempt can be deleted with its
    # assessment, and a notification about a vanished thing should degrade to an
    # un-clickable line rather than take the row down with it.
    target_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The database fills this, so the model must say so or SQLAlchemy INSERTs NULL
    # into a NOT NULL column (`tests/test_model_defaults.py` enforces this).
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
