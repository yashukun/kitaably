"""Shared test fixtures.

This is the one place ``create_all()`` is allowed. Everywhere else the schema comes
from ``supabase/migrations/`` and SQLAlchemy only reads it.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)
