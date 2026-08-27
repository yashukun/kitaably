"""Token verification and the authenticated Principal.

Everything Supabase-Auth-shaped is confined to this file (see DECISIONS.md D1): if
GoTrue is ever replaced, the blast radius is here and ``clients/storage.py``.

The rule that matters: **a JWT proves *who*, the database decides *what they may
do*.** ``sub`` is taken from the verified token; everything else about the caller is
read from ``profiles``.

That is not paranoia. A Supabase user can rewrite their own ``user_metadata`` with a
single authenticated request::

    PUT /auth/v1/user  {"data": {"role": "admin"}}   -> 200

and their very next access token carries that claim, real and correctly signed and
worthless as authorization. There is no role distinction to escalate into today, and
this file is written so there is nowhere to escalate *to* if one returns: the only
thing lifted out of a token here is ``sub``.
"""

import asyncio
import json
import time
from dataclasses import dataclass
from uuid import UUID

import httpx
import jwt
from jwt import PyJWK, PyJWKSet
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import Unauthenticated
from app.db.models.enums import Role
from app.db.models.profile import Profile

# Claims every token must carry before we look at any of them.
_REQUIRED_CLAIMS = ["exp", "iat", "sub", "aud", "iss"]

# An unknown `kid` triggers a JWKS refetch. Without a floor, anyone could force us to
# hammer the auth server by presenting tokens with invented key ids.
_MIN_JWKS_REFRESH_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller, as the application understands them.

    Constructed only after a token is verified *and* ``profiles`` has been read.
    Never built from request-supplied fields.
    """

    id: UUID
    role: Role
    email: str


class _JwksCache:
    """The project's signing keys, cached and refreshed on an unrecognised key id."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._keys: dict[str, PyJWK] = {}
        self._last_refresh = 0.0
        self._lock = asyncio.Lock()

    async def get(self, kid: str) -> PyJWK:
        key = self._keys.get(kid)
        if key is not None:
            return key

        async with self._lock:
            # Another request may have refreshed while we waited for the lock.
            key = self._keys.get(kid)
            if key is not None:
                return key

            if time.monotonic() - self._last_refresh < _MIN_JWKS_REFRESH_SECONDS:
                raise Unauthenticated("Session is invalid or has expired.")

            await self._refresh()

        key = self._keys.get(kid)
        if key is None:
            raise Unauthenticated("Session is invalid or has expired.")
        return key

    async def _refresh(self) -> None:
        async with httpx.AsyncClient(timeout=5.0) as client:
            # GoTrue's JWKS route sits behind Kong, which requires an API key even
            # for a public document.
            response = await client.get(
                self._url, headers={"apikey": settings.supabase_anon_key}
            )
            response.raise_for_status()
            key_set = PyJWKSet.from_dict(response.json())

        self._keys = {key.key_id: key for key in key_set.keys if key.key_id}
        self._last_refresh = time.monotonic()


_jwks = _JwksCache(settings.supabase_jwks_url)


async def verify_access_token(token: str) -> dict:
    """Verify a Supabase access token and return its claims.

    The algorithm comes from the **JWKS key**, never from the token's own header.
    Trusting the header is the classic JWT algorithm-confusion vulnerability: an
    attacker flips ``alg`` to HS256 and signs with the public key as the shared
    secret. Pinning to the key's algorithm makes that impossible to express.
    """
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise Unauthenticated("Session is invalid or has expired.") from exc

    kid = header.get("kid")
    if not kid:
        raise Unauthenticated("Session is invalid or has expired.")

    key = await _jwks.get(kid)

    try:
        return jwt.decode(
            token,
            key.key,
            algorithms=[key.algorithm_name],
            audience=settings.supabase_jwt_audience,
            issuer=settings.supabase_jwt_issuer,
            options={"require": _REQUIRED_CLAIMS},
        )
    except jwt.PyJWTError as exc:
        raise Unauthenticated("Session is invalid or has expired.") from exc


async def load_principal(session: AsyncSession, claims: dict) -> Principal:
    """Resolve verified claims into a Principal by reading ``profiles``.

    This SELECT is what makes the caller a database fact rather than a token's
    assertion. Note what is *not* used here: ``claims["user_metadata"]``,
    ``claims["role"]`` (which GoTrue sets to the Postgres role ``authenticated``,
    not an application role), or anything else the token carries beyond ``sub``.

    The profile row also being the authorization check is why this stays a query and
    does not become a decode: a deleted account must stop working immediately, not
    when its last issued token happens to expire.
    """
    try:
        user_id = UUID(claims["sub"])
    except (KeyError, ValueError) as exc:
        raise Unauthenticated("Session is invalid or has expired.") from exc

    profile = await session.scalar(select(Profile).where(Profile.id == user_id))
    if profile is None:
        # Verified token, no profile row: the signup trigger did not fire, or the
        # account was deleted. Either way there is no authority to act on.
        raise Unauthenticated("This account is not set up. Sign in again.")

    return Principal(id=profile.id, role=profile.role, email=str(profile.email))


async def apply_rls_context(session: AsyncSession, claims: dict) -> None:
    """Adopt the caller's identity on this connection so RLS policies apply.

    Without this the backend would query as the owner role, RLS would never
    evaluate, and the defence-in-depth half of DECISIONS.md D8 would be fiction.
    Both settings are transaction-local, so they cannot leak to the next request
    that borrows this pooled connection.

    **That transaction-locality has a consequence worth knowing before it bites you.**
    A route that calls ``session.commit()`` and then queries again is querying with
    no identity: the new transaction has no ``request.jwt.claims``, ``auth.uid()``
    returns NULL, and every RLS policy and SECURITY DEFINER view evaluates against
    nobody. It does not raise. It returns **zero rows**, which reads as "the data
    isn't there" rather than "you are no longer logged in" — and it cost an afternoon
    once already, when starting an exam returned a paper with no questions on it.

    So: read everything you need *before* committing, and let the session dependency
    commit at the end. Commit early only when a Celery task must see the row, and
    then do not query again afterwards.
    """
    await session.execute(
        text("select set_config('request.jwt.claims', :claims, true)"),
        {"claims": json.dumps(claims)},
    )
    await session.execute(text("select set_config('role', 'authenticated', true)"))
