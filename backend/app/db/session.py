"""Async engine and session factories.

Two paths, deliberately different (DECISIONS.md D8):

* **Request path** — runs as the authenticated user, so RLS policies apply. This is
  the second line of defence behind the predicate the application builds itself.
* **Worker path** — runs as the service role and therefore *bypasses RLS*. Every
  worker query must carry its scope predicate explicitly, and the scope tests cover
  this path precisely because the database is not covering it.

One transaction per request, opened by the dependency and committed once at the end.
Never commit inside a loop.
"""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings

engine = create_async_engine(
    settings.database_url,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_pre_ping=True,
    echo=False,
)

# The worker gets its own engine, and the difference is not tuning -- it is
# correctness.
#
# Each Celery task runs `asyncio.run()`, which creates and then destroys an event
# loop. A pooled asyncpg connection is bound to the loop that opened it, so the
# second task checks out a connection belonging to the first task's dead loop and
# fails with "got Future attached to a different loop". NullPool holds nothing
# between checkouts, so every connection lives and dies inside one loop.
#
# The API never hits this because it has a single long-lived loop, which is exactly
# what made the bug look like a worker-only mystery.
worker_engine = create_async_engine(
    settings.database_url,
    poolclass=NullPool,
    echo=False,
)

# NOTE: no asyncpg vector codec is registered here, deliberately.
#
# pgvector ships two serialisers and they conflict. `pgvector.sqlalchemy.VECTOR`
# has a bind_processor that turns a list into pgvector's text form, while
# `pgvector.asyncpg.register_vector` installs a *binary* codec whose encoder expects
# a list. Register both and the codec receives an already-stringified value:
#   asyncpg.exceptions.DataError: invalid input for query argument $5:
#   '[-0.013249...' (expected list or ndarray)
# The SQLAlchemy type alone is sufficient and is the path with no code of our own.

SessionFactory = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
)

# Used by Celery tasks. Runs as the service role, so RLS does NOT apply and every
# query written against it must carry its own scope predicate.
WorkerSessionFactory = async_sessionmaker(
    worker_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Request-path session. One transaction, committed on success.

    Phase 1 extends this to adopt the caller's identity for the connection (so that
    RLS evaluates ``auth.uid()`` correctly) rather than connecting as an owner role
    that policies do not constrain.
    """
    async with SessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def ping() -> bool:
    """Cheap connectivity probe for ``/ready``. Never used on a request path."""
    from sqlalchemy import text

    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return True
