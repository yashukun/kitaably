"""profiles routes. Phase 1.

Planned surface (api-conventions: plural nouns, nested under their parent,
paginated from day one, 202 + {id, status} for anything queued):

    GET   /me                                    require_auth

Every route declares a guard. A route with no guard is a review failure;
a genuinely public one says Depends(allow_anonymous) so the absence is
deliberate and greppable.
"""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_auth
from app.core.security import Principal
from app.db.models.profile import Profile
from app.db.session import get_session
from app.schemas.profile import ProfileRead

router = APIRouter(tags=["profiles"])


@router.get("/me", response_model=ProfileRead)
async def read_me(
    principal: Principal = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> Profile:
    """The signed-in user, as the database understands them.

    Useful beyond the UI: this is the endpoint that shows a token's claimed role and
    the authoritative role disagreeing, which is the whole point of reading
    ``profiles``.
    """
    # require_auth already proved this row exists and is readable under RLS.
    profile = await session.scalar(select(Profile).where(Profile.id == principal.id))
    return profile  # type: ignore[return-value]
