"""The ``/api/v1`` aggregator.

Resource nouns are plural and nested under their parent: ``/books/{id}/scope``.
Routers are registered here as their phase lands; a router that exists but has no
routes yet is still registered, so the shape of the API is visible from one file.
"""

from fastapi import APIRouter

from app.api.v1 import (
    assessments,
    attempts,
    books,
    chat,
    proctoring,
    profiles,
)

api_router = APIRouter()

api_router.include_router(profiles.router)      # Phase 1
api_router.include_router(books.router)         # Phase 3
api_router.include_router(chat.router)          # Phase 4
api_router.include_router(assessments.router)   # Phase 5
api_router.include_router(attempts.router)      # Phase 6
api_router.include_router(proctoring.router)    # Phase 7-8
