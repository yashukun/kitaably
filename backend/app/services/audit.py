"""The append-only human record. Phase 2 onward.

Written for every consequential action: report.released, attempt.voided,
grade.overridden, book.deleted, book.shared, book.unshared.

If someone contests an outcome, this is what shows that a human decided, and when.
It is not monitoring: never sampled, never truncated, never shipped to a lossy
pipeline. No update or delete policy exists for any role.

Writes go through ``public.write_audit_log()`` rather than an INSERT, because the
request path connects as ``authenticated``, which deliberately holds no privilege on
the table. The function takes no actor argument — it reads ``auth.uid()`` itself — so
a caller can record an action but never attribute it to somebody else.
"""

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_request_id

_WRITE = text(
    """
    select public.write_audit_log(
        cast(:action as text),
        cast(:target_type as text),
        cast(:target_id as uuid),
        cast(:metadata as jsonb),
        cast(:request_id as text)
    )
    """
)


async def record(
    session: AsyncSession,
    *,
    action: str,
    target_type: str,
    target_id: UUID | None,
    metadata: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> None:
    """Append one row.

    Deliberately part of the caller's transaction: if the action rolls back, so does
    its audit row. An audit trail describing something that did not happen is worse
    than no audit trail, because it will be believed.
    """
    await session.execute(
        _WRITE,
        {
            "action": action,
            "target_type": target_type,
            "target_id": str(target_id) if target_id else None,
            "metadata": json.dumps(metadata or {}),
            "request_id": request_id or get_request_id(),
        },
    )


_WRITE_AS = text(
    """
    select public.write_audit_log_as(
        cast(:actor_id as uuid),
        cast(:action as text),
        cast(:target_type as text),
        cast(:target_id as uuid),
        cast(:metadata as jsonb),
        cast(:request_id as text)
    )
    """
)


async def record_as(
    session: AsyncSession,
    *,
    actor_id: UUID | None,
    action: str,
    target_type: str,
    target_id: UUID | None,
    metadata: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> None:
    """Append one row on behalf of a named actor. Worker path only.

    A Celery task has no JWT, so ``auth.uid()`` is null and :func:`record` would file
    a human's deletion as an anonymous system action. The actor is carried from the
    request that enqueued the task instead.
    """
    await session.execute(
        _WRITE_AS,
        {
            "actor_id": str(actor_id) if actor_id else None,
            "action": action,
            "target_type": target_type,
            "target_id": str(target_id) if target_id else None,
            "metadata": json.dumps(metadata or {}),
            "request_id": request_id,
        },
    )
