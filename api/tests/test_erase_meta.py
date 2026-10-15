"""Server discovery (/meta) and permanent deletion of accounts and households."""

from typing import Any

import httpx
import pytest

from coffernest.config import get_settings
from tests.conftest import FakeStorage, register

API = "/api/v1"
PASSWORD = "correct-horse-battery"


async def _invite(client: httpx.AsyncClient, email: str, role: str = "adult") -> str:
    res = await client.post(f"{API}/household/invites", json={"email": email, "role": role})
    assert res.status_code == 201, res.text
    token: str = res.json()["token"]
    return token


async def _join(anon: httpx.AsyncClient, email: str, token: str) -> dict[str, Any]:
    anon.headers.pop("Authorization", None)
    return await register(anon, email, invite_token=token)


def _login(email: str) -> dict[str, str]:
    return {"email": email, "password": PASSWORD}


async def test_meta_is_public_and_tracks_settings(
    anon: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    res = await anon.get(f"{API}/meta")
    assert res.status_code == 200
    body = res.json()
    assert body["name"] == "Coffernest"
    assert body["registration_open"] is True
    assert body["ai_available"] is False
    assert body["password_min_length"] == 10
    assert body["account_deletion"] is True
    assert body["privacy_url"].endswith("/privacy")

    monkeypatch.setattr(get_settings(), "allow_registration", False)
    monkeypatch.setattr(get_settings(), "ai_provider", "openai_compat")
    monkeypatch.setattr(get_settings(), "privacy_url", "https://example.com/p")
    body = (await anon.get(f"{API}/meta")).json()
    assert body["registration_open"] is False
    assert body["ai_available"] is True
    assert body["privacy_url"] == "https://example.com/p"


async def test_sole_member_deletes_account_and_household(
    client: httpx.AsyncClient, storage: FakeStorage
) -> None:
    hid = (await client.get(f"{API}/household")).json()["id"]
    storage.put(f"{hid}/2026/10/bill.pdf", b"x", "application/pdf")
    storage.put("someone-else/2026/10/bill.pdf", b"y", "application/pdf")
    await client.post(f"{API}/accounts", json={"name": "Cash", "type": "cash"})

    wrong = await client.request("DELETE", f"{API}/auth/me", json={"password": "nope-nope-nope"})
    assert wrong.status_code == 400

    gone = await client.request("DELETE", f"{API}/auth/me", json={"password": PASSWORD})
    assert gone.status_code == 204
    assert list(storage.objects) == ["someone-else/2026/10/bill.pdf"]
    assert (await client.get(f"{API}/auth/me")).status_code == 401

    client.headers.pop("Authorization")
    login = await client.post(f"{API}/auth/login", json=_login("owner@example.com"))
    assert login.status_code == 401
    # The e-mail is free again.
    await register(client)
    assert (await client.get(f"{API}/accounts")).json() == []


async def test_member_leaves_household_intact(
    client: httpx.AsyncClient, storage: FakeStorage
) -> None:
    owner_token = client.headers["Authorization"]
    token = await _invite(client, "spouse@example.com", "owner")
    await _join(client, "spouse@example.com", token)

    # The spouse (a second owner) created an invite: deleting them must still work.
    await _invite(client, "kid@example.com", "viewer")
    res = await client.request("DELETE", f"{API}/auth/me", json={"password": PASSWORD})
    assert res.status_code == 204

    client.headers["Authorization"] = owner_token
    members = (await client.get(f"{API}/household/members")).json()
    assert [m["email"] for m in members] == ["owner@example.com"]


async def test_only_owner_of_shared_household_must_hand_over(client: httpx.AsyncClient) -> None:
    owner_token = client.headers["Authorization"]
    token = await _invite(client, "spouse@example.com")
    await _join(client, "spouse@example.com", token)
    client.headers["Authorization"] = owner_token

    res = await client.request("DELETE", f"{API}/auth/me", json={"password": PASSWORD})
    assert res.status_code == 409
    assert (await client.get(f"{API}/auth/me")).status_code == 200


async def test_owner_deletes_household(client: httpx.AsyncClient, storage: FakeStorage) -> None:
    owner_token = client.headers["Authorization"]
    hid = (await client.get(f"{API}/household")).json()["id"]
    storage.put(f"{hid}/backups/pre-restore.cnz", b"z", "application/zip")
    token = await _invite(client, "spouse@example.com")
    spouse = await _join(client, "spouse@example.com", token)
    spouse_token = f"Bearer {spouse['access_token']}"

    client.headers["Authorization"] = spouse_token
    not_owner = await client.request("DELETE", f"{API}/household", json={"password": PASSWORD})
    assert not_owner.status_code == 403

    client.headers["Authorization"] = owner_token
    res = await client.request("DELETE", f"{API}/household", json={"password": PASSWORD})
    assert res.status_code == 204
    assert storage.objects == {}
    for bearer in (owner_token, spouse_token):
        client.headers["Authorization"] = bearer
        assert (await client.get(f"{API}/auth/me")).status_code == 401
    client.headers.pop("Authorization")
    for email in ("owner@example.com", "spouse@example.com"):
        assert (await client.post(f"{API}/auth/login", json=_login(email))).status_code == 401
