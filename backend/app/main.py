"""FastAPI entrypoint.

Wiring only: logging, middleware, CORS, the one exception handler, and the routers.
No business logic passes through here.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import health
from app.api.router import api_router
from app.core.config import settings
from app.core.errors import DomainError, domain_error_handler, unhandled_error_handler
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging(settings.log_level)
    logger.info(
        "starting",
        extra={"app": settings.app_name, "environment": settings.environment},
    )
    yield
    from app.clients import embeddings
    from app.db.session import engine

    await embeddings.aclose()
    await engine.dispose()
    logger.info("stopped")


app = FastAPI(
    title=f"{settings.app_name} API",
    version="0.1.0",
    lifespan=lifespan,
    # Interactive docs are a development affordance, not a production surface.
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None,
    openapi_url=None if settings.is_production else "/openapi.json",
)

app.add_middleware(RequestContextMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)

app.add_exception_handler(DomainError, domain_error_handler)
app.add_exception_handler(Exception, unhandled_error_handler)

# Unversioned operational endpoints; probes and scrapers are not API consumers.
app.include_router(health.router)
# Everything else. The browser reaches these through the Next.js rewrite
# /api/backend/:path* -> /api/v1/:path*, so it stays same-origin.
app.include_router(api_router, prefix="/api/v1")
