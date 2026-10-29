"""Shared test fixtures.

The database URL is set before ``app`` is imported so the whole suite runs
against a throwaway SQLite file instead of the developer's real one.
"""
from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

import pytest

_TMP_DB = Path(tempfile.gettempdir()) / ("collabdoc-test-%s.db" % uuid.uuid4().hex)
os.environ["COLLABDOC_DATABASE_URL"] = "sqlite:///%s" % _TMP_DB.as_posix()
os.environ["COLLABDOC_SECRET_KEY"] = "test-secret-" + "0" * 32
os.environ["COLLABDOC_BROADCASTER"] = "memory"
os.environ["COLLABDOC_AUTH_RATE_LIMIT"] = "0"  # tests that need it turn it on
os.environ["COLLABDOC_AI_PROVIDER"] = "local"  # never call a paid API from the suite
os.environ["COLLABDOC_UPLOAD_DIR"] = str(Path(tempfile.gettempdir()) / ("collabdoc-uploads-%s" % uuid.uuid4().hex))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, engine  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _database():
    Base.metadata.create_all(bind=engine)
    yield
    engine.dispose()
    _TMP_DB.unlink(missing_ok=True)


@pytest.fixture()
def client():
    with TestClient(create_app()) as test_client:
        yield test_client


class Account:
    """A registered user plus the helpers to act as them."""

    def __init__(self, client: TestClient, data: dict) -> None:
        self.client = client
        self.id = data["user"]["id"]
        self.name = data["user"]["name"]
        self.email = data["user"]["email"]
        self.token = data["access_token"]
        self.color = data["user"]["presence_color"]

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer %s" % self.token}

    def get(self, url, **kw):
        return self.client.get(url, headers=self.headers, **kw)

    def post(self, url, **kw):
        return self.client.post(url, headers=self.headers, **kw)

    def patch(self, url, **kw):
        return self.client.patch(url, headers=self.headers, **kw)

    def put(self, url, **kw):
        return self.client.put(url, headers=self.headers, **kw)

    def delete(self, url, **kw):
        return self.client.delete(url, headers=self.headers, **kw)

    def new_document(self, title="Test document", content="") -> str:
        response = self.post(
            "/api/documents", json={"title": title, "content": content}
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]


@pytest.fixture()
def register(client):
    def _register(name: str = "Tester", password: str = "correct-horse-1") -> Account:
        email = "%s-%s@example.com" % (name.lower().replace(" ", "-"), uuid.uuid4().hex[:8])
        response = client.post(
            "/api/auth/register",
            json={"email": email, "name": name, "password": password},
        )
        assert response.status_code == 201, response.text
        return Account(client, response.json())

    return _register


@pytest.fixture()
def alice(register):
    return register("Alice")


@pytest.fixture()
def bob(register):
    return register("Bob")
