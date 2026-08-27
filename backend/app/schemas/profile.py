"""Profile schemas. Phase 1.

Separate Create, Update, and Read models. Never accept an ORM model as a request
body, never return one directly. The Read schema is the contract that stops a column
leaking after a policy change.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.db.models.enums import Role


class ProfileRead(BaseModel):
    """What the caller may know about a profile.

    There is no `ProfileCreate`: rows are written by the signup trigger, never by
    this API. And no `role` on any Update schema — a user who could PATCH their own
    role would make the whole read-role-from-the-database rule pointless.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    name: str | None
    role: Role
    avatar_url: str | None
    created_at: datetime
