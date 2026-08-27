"""Authorization guards.

Every route declares one. A new route with no guard is a review failure; a genuinely
public route says ``Depends(allow_anonymous)`` so the absence is deliberate and
greppable rather than forgotten.

There is one kind of account, so there is no role guard here and nothing that reads
``principal.role``. Authorization is entirely about the caller's relationship to the
row being touched — which is what it was actually made of all along.

Guards verify against the database. An id in a URL is a claim, not an
authorization — ``require_book_owner`` is the verification of that claim.

Guards are named for the resource they check. There is no generic ``require_owner``:
ownership is a fact about a specific row, so a guard that cannot name its table
cannot verify anything.
"""

from uuid import UUID

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound, Unauthenticated
from app.core.security import (
    Principal,
    apply_rls_context,
    load_principal,
    verify_access_token,
)
from app.db.models.assessment import Assessment
from app.db.models.attempt import Attempt
from app.db.models.book import Book
from app.db.session import get_session

# auto_error=False so a missing header raises our own domain error rather than
# FastAPI's HTTPException — one error envelope, one handler. It still puts the
# Authorize button on /docs.
_bearer_scheme = HTTPBearer(auto_error=False, description="Supabase access token")


async def allow_anonymous() -> None:
    """Explicitly public: health, ready, metrics, share-token exam entry.

    Present so that "this route has no guard" is a statement rather than an omission.
    """
    return None


async def require_auth(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    session: AsyncSession = Depends(get_session),
) -> Principal:
    """Any signed-in user.

    Order matters here:

    1. verify the token's signature, expiry, audience and issuer
    2. adopt the caller's identity on the connection, so RLS policies apply for the
       rest of this transaction
    3. read ``profiles`` for the role

    Step 2 before step 3 is deliberate: even the profile lookup runs under RLS.
    """
    if credentials is None or not credentials.credentials:
        raise Unauthenticated()

    claims = await verify_access_token(credentials.credentials)
    await apply_rls_context(session, claims)
    return await load_principal(session, claims)


async def require_book_owner(
    book_id: UUID,
    principal: Principal = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> Principal:
    """Caller owns this book.

    Named for the resource rather than a generic `require_owner`: ownership is a fact
    about a specific row, and a guard that cannot name the table it is checking
    cannot check anything. Later resources get their own sibling.

    NotFound rather than Forbidden — a 403 on someone else's book confirms it exists.
    """
    owns = await session.scalar(
        select(func.count())
        .select_from(Book)
        .where(Book.id == book_id, Book.owner_id == principal.id)
    )
    if not owns:
        raise NotFound("That book does not exist.")
    return principal


async def require_assessment_author(
    assessment_id: UUID,
    principal: Principal = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> Principal:
    """Caller wrote this assessment.

    The author holds every authority over a paper: editing it, publishing it, marking
    it, and deciding when a result becomes visible. That is not a role — it is a fact
    about one row, which is why this guard names the table it checks.

    NotFound rather than Forbidden: a 403 on somebody else's paper confirms it exists.
    """
    owns = await session.scalar(
        select(func.count())
        .select_from(Assessment)
        .where(Assessment.id == assessment_id, Assessment.author_id == principal.id)
    )
    if not owns:
        raise NotFound("That assessment does not exist.")
    return principal


async def require_attempt_participant(
    attempt_id: UUID,
    principal: Principal = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> Principal:
    """Caller either sat this attempt or wrote the paper it belongs to.

    Both parties can read an attempt and they see very different things — the sitter
    gets their own marks once released, the author gets the gradebook. Splitting *what*
    each may do stays in the service; this guard answers the cheaper question of
    whether the caller has any business with this row at all.
    """
    attempt = await session.scalar(select(Attempt).where(Attempt.id == attempt_id))
    if attempt is None:
        raise NotFound("That attempt does not exist.")

    if attempt.sitter_id == principal.id:
        return principal

    authored = await session.scalar(
        select(func.count())
        .select_from(Assessment)
        .where(
            Assessment.id == attempt.assessment_id,
            Assessment.author_id == principal.id,
        )
    )
    if not authored:
        raise NotFound("That attempt does not exist.")
    return principal


async def require_attempt_author(
    attempt_id: UUID,
    principal: Principal = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> Principal:
    """Caller wrote the paper this attempt belongs to. Marking and release only.

    Deliberately separate from ``require_attempt_participant``: releasing a result and
    overriding a mark are the author's acts alone, and a guard that admitted the sitter
    to the same routes would be one service-layer omission away from letting somebody
    mark their own paper.
    """
    attempt = await session.scalar(select(Attempt).where(Attempt.id == attempt_id))
    if attempt is None:
        raise NotFound("That attempt does not exist.")

    authored = await session.scalar(
        select(func.count())
        .select_from(Assessment)
        .where(
            Assessment.id == attempt.assessment_id,
            Assessment.author_id == principal.id,
        )
    )
    if not authored:
        raise NotFound("That attempt does not exist.")
    return principal


async def require_attempt_sitter(
    attempt_id: UUID,
    principal: Principal = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> Principal:
    """Caller is the person sitting this attempt. Answering and submitting only.

    The author can read an attempt but must never write an answer into one.
    """
    sitter = await session.scalar(
        select(Attempt.sitter_id).where(Attempt.id == attempt_id)
    )
    if sitter != principal.id:
        raise NotFound("That attempt does not exist.")
    return principal
