"""Family on a server: the first account is the admin, then only emails the admin adds join."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from coffernest.config import get_settings
from tests.conftest import register

API = "/api/v1"


async def _signup(client: httpx.AsyncClient, email: str) -> httpx.Response:
    client.headers.pop("Authorization", None)
    return await client.post(
        f"{API}/auth/register",
        json={"email": email, "password": "correct-horse-battery", "display_name": email[:5]},
    )


@pytest.fixture
def closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "allow_registration", False)


@pytest.mark.usefixtures("closed")
async def test_first_account_is_admin_then_only_added_emails(anon: httpx.AsyncClient) -> None:
    assert (await anon.get(f"{API}/meta")).json()["first_user"] is True
    admin = await register(anon, "admin@example.com")
    assert admin["role"] == "owner"
    assert (await anon.get(f"{API}/meta")).json()["first_user"] is False

    stranger = await _signup(anon, "stranger@example.com")
    assert stranger.status_code == 403
    assert "admin" in stranger.json()["detail"]

    anon.headers["Authorization"] = f"Bearer {admin['access_token']}"
    added = await anon.post(f"{API}/household/invites", json={"email": "Spouse@Example.com"})
    assert added.status_code == 201, added.text
    pending = (await anon.get(f"{API}/household/invites")).json()
    assert [p["email"] for p in pending] == ["spouse@example.com"]

    joined = await _signup(anon, "spouse@example.com")
    assert joined.status_code == 201, joined.text
    spouse: dict[str, Any] = joined.json()
    assert spouse["household_id"] == admin["household_id"]
    assert spouse["role"] == "adult"

    anon.headers["Authorization"] = f"Bearer {admin['access_token']}"
    members = (await anon.get(f"{API}/household/members")).json()
    assert {m["email"]: m["role"] for m in members} == {
        "admin@example.com": "owner",
        "spouse@example.com": "adult",
    }
    assert (await anon.get(f"{API}/household/invites")).json() == []


@pytest.mark.usefixtures("closed")
async def test_adding_rules(anon: httpx.AsyncClient) -> None:
    admin = await register(anon, "admin@example.com")
    add = f"{API}/household/invites"
    assert (await anon.post(add, json={"email": "admin@example.com"})).status_code == 409
    first = await anon.post(add, json={"email": "kid@example.com"})
    assert first.status_code == 201
    assert (await anon.post(add, json={"email": "kid@example.com"})).status_code == 409

    # Removing a pending email closes the door again.
    assert (await anon.delete(f"{add}/{first.json()['id']}")).status_code == 204
    assert (await _signup(anon, "kid@example.com")).status_code == 403
    anon.headers["Authorization"] = f"Bearer {admin['access_token']}"


async def test_open_registration_still_makes_new_households(anon: httpx.AsyncClient) -> None:
    a = await register(anon, "a@example.com")
    b = await register(anon, "b@example.com")
    assert a["household_id"] != b["household_id"]
