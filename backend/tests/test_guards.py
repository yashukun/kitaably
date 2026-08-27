"""Access-control guards.

One of the four things CLAUDE.md requires tests for before merge, because a silent
bug here is a privacy incident rather than a broken page.

These are unit tests over the guard functions themselves. They deliberately do not
go through a route: the question being asked is "does this guard refuse", and that
answer must not depend on which routes happen to exist today.

There is one kind of account, so there is no role guard to test. What is tested
instead is the property that replaced it — that no route is left without a guard at
all, and that an unauthenticated call is refused rather than passed through.
"""

import inspect
from uuid import uuid4

import pytest

from app.core.deps import allow_anonymous, require_auth
from app.core.errors import Unauthenticated
from app.core.security import Principal
from app.db.models.enums import Role


def _principal() -> Principal:
    return Principal(id=uuid4(), role=Role.USER, email="someone@example.test")


async def test_allow_anonymous_is_explicit_and_returns_nothing() -> None:
    """A public route says so out loud, so the absence of a guard is greppable."""
    assert await allow_anonymous() is None


async def test_require_auth_rejects_a_missing_bearer_token() -> None:
    """No credentials is unauthenticated, not a 500 and not a pass."""
    with pytest.raises(Unauthenticated):
        await require_auth(request=None, credentials=None, session=None)


async def test_require_auth_rejects_an_empty_bearer_token() -> None:
    """An `Authorization: Bearer ` header with nothing after it is not a session."""

    class _Empty:
        credentials = ""

    with pytest.raises(Unauthenticated):
        await require_auth(request=None, credentials=_Empty(), session=None)


async def test_no_guard_is_left_unimplemented() -> None:
    """Every guard must be a real check.

    An unbuilt guard raising NotImplementedError fails closed, which is correct but
    temporary. This asserts none are left — a route wired to a stub guard is a route
    that 500s instead of authorizing.
    """
    from app.core import deps

    guards = [
        (name, fn)
        for name, fn in inspect.getmembers(deps, inspect.iscoroutinefunction)
        if name.startswith(("require_", "allow_"))
    ]
    assert guards, "no guards found — this test would pass vacuously"

    for name, guard in guards:
        assert "NotImplementedError" not in inspect.getsource(guard), f"{name} is a stub"


def test_every_route_declares_a_guard() -> None:
    """The rule CLAUDE.md states, enforced rather than reviewed.

    A route with no guard is a review failure. Catching it here makes it a test
    failure instead, which is the difference between finding it and hoping to.

    This walks the route modules rather than the assembled app: FastAPI 0.141 keeps
    included routers lazy, so `app.routes` holds wrappers rather than endpoints and
    the obvious version of this test passes without inspecting anything.
    """
    import importlib
    import pkgutil

    from fastapi.routing import APIRoute

    import app.api.v1 as v1
    from app.core import deps

    # Derived from the module rather than listed here. A hardcoded set goes stale the
    # first time a guard is added, and it goes stale by *failing on a route that is
    # actually fine* — which trains whoever hits it to widen the list without looking.
    known = {
        name
        for name, _ in inspect.getmembers(deps, inspect.iscoroutinefunction)
        if name.startswith(("require_", "allow_"))
    }
    checked = 0

    for module_info in pkgutil.iter_modules(v1.__path__):
        module = importlib.import_module(f"app.api.v1.{module_info.name}")
        for route in getattr(module.router, "routes", []):
            if not isinstance(route, APIRoute):
                continue
            checked += 1
            guards = {
                param.default.dependency.__name__
                for param in inspect.signature(route.endpoint).parameters.values()
                if hasattr(param.default, "dependency")
                and getattr(param.default.dependency, "__name__", "") in known
            }
            assert guards, f"{module_info.name}: {route.path} declares no guard"

    # Phase 5-8 routers are registered but still empty, so a low count is expected.
    # Zero is not: it would mean this walked nothing and asserted nothing.
    assert checked >= 10, f"only {checked} routes inspected — the walk is not finding them"


def test_principal_carries_no_authority_beyond_identity() -> None:
    """Nothing branches on role today, and this is what notices if something starts.

    A role helper reappearing on Principal means an authorization decision moved out
    of "what is this caller's relationship to this row" and back into "what kind of
    account is this" — which needs a migration and a policy, not an attribute.
    """
    principal = _principal()
    assert principal.role is Role.USER
    assert not [name for name in dir(principal) if name.startswith("is_")]
