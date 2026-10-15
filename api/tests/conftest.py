"""Test fixtures: a real Postgres database (``coffernest_test``), migrated with Alembic."""

from __future__ import annotations

import os

os.environ.setdefault(
    "TEST_DATABASE_URL", "postgresql+asyncpg://coffernest:coffernest@localhost:5433/coffernest_test"
)
os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
os.environ["ENV"] = "test"
os.environ["JWT_SECRET"] = "test-secret-test-secret-test-secret-42"
# Most tests sign up several unrelated households; test_family.py turns this off.
os.environ.setdefault("ALLOW_REGISTRATION", "true")

from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import httpx
import pytest
from sqlalchemy import text

from coffernest import db
from coffernest.main import app
from coffernest.models import Base


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def ensure_bucket(self) -> None: ...

    def presign_put(self, key: str, mime: str, expires: int = 900) -> str:
        return f"https://s3.test/put/{key}"

    def presign_get(self, key: str, filename: str, expires: int = 3600) -> str:
        return f"https://s3.test/get/{key}"

    def head(self, key: str) -> int | None:
        return len(self.objects[key]) if key in self.objects else None

    def put(self, key: str, data: bytes, mime: str) -> None:
        self.objects[key] = data

    def get(self, key: str, max_bytes: int) -> bytes:
        return self.objects[key][:max_bytes]

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    def delete_prefix(self, prefix: str) -> int:
        doomed = [key for key in self.objects if key.startswith(prefix)]
        for key in doomed:
            del self.objects[key]
        return len(doomed)

    def ping(self) -> bool:
        return True


@pytest.fixture(scope="session", autouse=True)
async def database() -> AsyncIterator[None]:
    url = os.environ["TEST_DATABASE_URL"].replace("+asyncpg", "")
    admin_url, name = url.rsplit("/", 1)
    conn = await asyncpg.connect(f"{admin_url}/postgres")
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()
    import asyncio

    from coffernest.cli import _migrate

    await asyncio.to_thread(_migrate)
    yield
    await db.dispose()


@pytest.fixture(autouse=True)
async def clean() -> AsyncIterator[None]:
    yield
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    async with db.engine().begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch) -> FakeStorage:
    fake = FakeStorage()
    import coffernest.main
    import coffernest.routes.attachments
    import coffernest.routes.auth

    monkeypatch.setattr(coffernest.routes.attachments, "get_storage", lambda: fake)
    monkeypatch.setattr(coffernest.main, "get_storage", lambda: fake)
    monkeypatch.setattr(coffernest.routes.auth, "get_storage", lambda: fake)
    return fake


@pytest.fixture
async def anon() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app, client=("203.0.113.9", 5000))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={"X-Client": "native"}
    ) as client:
        yield client


async def register(
    client: httpx.AsyncClient, email: str = "owner@example.com", **extra: Any
) -> dict[str, Any]:
    res = await client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "correct-horse-battery",
            "display_name": "Owner",
            **extra,
        },
    )
    assert res.status_code == 201, res.text
    body: dict[str, Any] = res.json()
    client.headers["Authorization"] = f"Bearer {body['access_token']}"
    return body


@pytest.fixture
async def client(anon: httpx.AsyncClient) -> httpx.AsyncClient:
    await register(anon)
    return anon


async def categories(client: httpx.AsyncClient) -> dict[str, str]:
    res = await client.get("/api/v1/categories")
    return {c["name"]: c["id"] for c in res.json()}


async def bank(
    client: httpx.AsyncClient, opening: str = "100000.00", name: str = "Bank", **extra: Any
) -> dict[str, Any]:
    res = await client.post(
        "/api/v1/accounts",
        json={
            "kind": "bank",
            "name": name,
            "opening_balance": opening,
            "opening_date": "2026-01-01",
            **extra,
        },
    )
    assert res.status_code == 201, res.text
    body: dict[str, Any] = res.json()
    return body
