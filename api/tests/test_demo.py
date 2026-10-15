"""The server's own demo account: made and kept by the server, never the admin seat, and the
pages the server serves itself."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, update

from coffernest import db, demo
from coffernest.config import get_settings
from coffernest.models import User
from tests.conftest import FakeStorage, register

API = "/api/v1"
DEMO = "demo@example.com"
PASSWORD = "demo-password-123"


@pytest.fixture
def demo_on(monkeypatch: pytest.MonkeyPatch, storage: FakeStorage) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "allow_registration", False)
    monkeypatch.setattr(settings, "seed_demo", True)
    monkeypatch.setattr(settings, "demo_email", DEMO)
    monkeypatch.setattr(settings, "demo_password", PASSWORD)
    monkeypatch.setattr(demo, "MONTHS", 1)  # a month of books is plenty here


async def _login(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post(f"{API}/auth/login", json={"email": DEMO, "password": PASSWORD})


async def _demo_user() -> User | None:
    async with db.sessionmaker()() as session:
        user: User | None = await session.scalar(select(User).where(User.email == DEMO))
        return user


async def test_off_unless_configured() -> None:
    assert await demo.ensure_demo() == "off"
    assert await _demo_user() is None


@pytest.mark.usefixtures("demo_on")
async def test_demo_never_takes_the_admin_seat(anon: httpx.AsyncClient) -> None:
    assert await demo.ensure_demo() == "created"
    assert await demo.ensure_demo() == "kept"
    assert (await _login(anon)).status_code == 200
    assert (await anon.get(f"{API}/meta")).json()["first_user"] is True

    # The first real sign-up (from the iPhone) is the admin of its own household.
    admin = await register(anon, "admin@example.com")
    assert admin["role"] == "owner"
    assert (await anon.get(f"{API}/meta")).json()["first_user"] is False
    anon.headers.pop("Authorization")
    stranger = await anon.post(
        f"{API}/auth/register",
        json={"email": "x@example.com", "password": "correct-horse-battery", "display_name": "X"},
    )
    assert stranger.status_code == 403


@pytest.mark.usefixtures("demo_on")
async def test_nobody_can_sign_up_as_the_demo(anon: httpx.AsyncClient) -> None:
    taken = await anon.post(
        f"{API}/auth/register",
        json={"email": DEMO.upper(), "password": "someone-elses-pass", "display_name": "Me"},
    )
    assert taken.status_code == 403


@pytest.mark.usefixtures("demo_on")
async def test_comes_back_after_deletion_and_starts_afresh(anon: httpx.AsyncClient) -> None:
    await demo.ensure_demo()
    session = (await _login(anon)).json()
    anon.headers["Authorization"] = f"Bearer {session['access_token']}"
    gone = await anon.request("DELETE", f"{API}/auth/me", json={"password": PASSWORD})
    assert gone.status_code == 204
    assert await _demo_user() is None

    assert await demo.ensure_demo() == "created"
    first = await _demo_user()
    assert first is not None
    async with db.sessionmaker()() as s:
        await s.execute(
            update(User)
            .where(User.id == first.id)
            .values(created_at=datetime.now(UTC) - timedelta(days=31))
        )
        await s.commit()
    assert await demo.ensure_demo() == "refreshed"
    fresh = await _demo_user()
    assert fresh is not None and fresh.id != first.id
    anon.headers.pop("Authorization")
    assert (await _login(anon)).status_code == 200


async def test_policy_pages_without_a_web_app(anon: httpx.AsyncClient) -> None:
    privacy = await anon.get("/privacy")
    assert privacy.status_code == 200
    assert privacy.headers["content-type"].startswith("text/html")
    assert "<h1>Privacy policy</h1>" in privacy.text
    assert '<a href="https://github.com/vkfolio/coffernest/issues">' in privacy.text
    assert '<a href="mailto:me@vkfolio.com">me@vkfolio.com</a>' in privacy.text
    assert privacy.headers["x-content-type-options"] == "nosniff"
    assert privacy.headers["x-frame-options"] == "DENY"
    terms = await anon.get("/terms")
    assert "<ol><li><strong>What Coffernest is.</strong>" in terms.text
    assert "Coffernest server" in (await anon.get("/")).text
    support = await anon.get("/support")
    assert support.status_code == 200 and "<h1>Coffernest support</h1>" in support.text


async def test_retires_a_saved_browser_app(anon: httpx.AsyncClient) -> None:
    sw = await anon.get("/sw.js")
    assert sw.status_code == 200
    assert sw.headers["content-type"].startswith("text/javascript")
    assert sw.headers["cache-control"] == "no-cache"
    assert "registration.unregister()" in sw.text
