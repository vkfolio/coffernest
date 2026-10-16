from datetime import timedelta

import httpx
import pyotp
import pytest

from coffernest.config import get_settings
from coffernest.routes import auth as auth_routes
from coffernest.security import totp
from tests.conftest import register

API = "/api/v1"


async def test_register_login_me(anon: httpx.AsyncClient) -> None:
    body = await register(anon)
    assert body["role"] == "owner"
    assert body["refresh_token"]  # native client gets it in the body
    me = await anon.get(f"{API}/auth/me")
    assert me.json()["email"] == "owner@example.com"

    anon.headers.pop("Authorization")
    res = await anon.post(
        f"{API}/auth/login",
        json={"email": "OWNER@example.com", "password": "correct-horse-battery"},
    )
    assert res.status_code == 200
    cats = await anon.get(
        f"{API}/categories", headers={"Authorization": f"Bearer {res.json()['access_token']}"}
    )
    assert any(c["name"] == "School fees" for c in cats.json())


async def test_register_sets_currency_and_household_patch(anon: httpx.AsyncClient) -> None:
    await register(anon, currency="usd", timezone="America/New_York", fy_start_month=1)
    h = (await anon.get(f"{API}/household")).json()
    assert (h["currency"], h["timezone"], h["fy_start_month"]) == ("USD", "America/New_York", 1)
    res = await anon.patch(
        f"{API}/household", json={"currency": "EUR", "timezone": "Europe/Berlin"}
    )
    assert res.status_code == 200, res.text
    assert (res.json()["currency"], res.json()["fy_start_month"]) == ("EUR", 1)
    for bad in ({"currency": "EURO"}, {"timezone": "Europe/Atlantis"}, {"fy_start_month": 13}):
        assert (await anon.patch(f"{API}/household", json=bad)).status_code == 422
    assert (await anon.patch(f"{API}/household", json={"currency": None})).status_code == 200
    assert (await anon.get(f"{API}/household")).json()["currency"] == "EUR"


async def test_register_defaults_to_inr(anon: httpx.AsyncClient) -> None:
    await register(anon)
    h = (await anon.get(f"{API}/household")).json()
    assert (h["currency"], h["timezone"], h["fy_start_month"]) == ("INR", "Asia/Kolkata", 4)
    anon.headers.pop("Authorization")
    res = await anon.post(
        f"{API}/auth/register",
        json={
            "email": "x@example.com",
            "password": "correct-horse-battery",
            "display_name": "X",
            "currency": "DOLLAR",
        },
    )
    assert res.status_code == 422


async def test_web_client_gets_httponly_cookie(anon: httpx.AsyncClient) -> None:
    await register(anon)
    res = await anon.post(
        f"{API}/auth/login",
        headers={"X-Client": "web"},
        json={"email": "owner@example.com", "password": "correct-horse-battery"},
    )
    assert res.json()["refresh_token"] is None
    cookie = res.headers["set-cookie"]
    assert "cn_refresh=" in cookie and "HttpOnly" in cookie and "Path=/api/v1/auth" in cookie
    refreshed = await anon.post(f"{API}/auth/refresh", headers={"X-Client": "web"})
    assert refreshed.status_code == 200


async def test_refresh_rotation_and_reuse_detection(
    anon: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = (await register(anon))["refresh_token"]
    second = await anon.post(f"{API}/auth/refresh", json={"refresh_token": first})
    assert second.status_code == 200
    token2 = second.json()["refresh_token"]
    assert token2 != first
    # A concurrent replay moments later (two tabs) is a race, not theft.
    race = await anon.post(f"{API}/auth/refresh", json={"refresh_token": first})
    assert race.status_code == 200
    # Replaying the spent token later revokes the whole family, including the fresh ones.
    real_now = auth_routes._now
    monkeypatch.setattr(auth_routes, "_now", lambda: real_now() + timedelta(seconds=60))
    replay = await anon.post(f"{API}/auth/refresh", json={"refresh_token": first})
    assert replay.status_code == 401
    after = await anon.post(f"{API}/auth/refresh", json={"refresh_token": token2})
    assert after.status_code == 401


async def test_logout_all_kills_access_tokens(anon: httpx.AsyncClient) -> None:
    await register(anon)
    assert (await anon.post(f"{API}/auth/logout-all")).status_code == 204
    assert (await anon.get(f"{API}/auth/me")).status_code == 401


async def test_wrong_password_lockout(anon: httpx.AsyncClient) -> None:
    await register(anon)
    anon.headers.pop("Authorization")
    for _ in range(5):
        res = await anon.post(
            f"{API}/auth/login", json={"email": "owner@example.com", "password": "nope-nope-nope"}
        )
        assert res.status_code == 401
    res = await anon.post(
        f"{API}/auth/login", json={"email": "owner@example.com", "password": "nope-nope-nope"}
    )
    assert res.status_code == 401  # 6th failure triggers the lock …
    res = await anon.post(
        f"{API}/auth/login",
        json={"email": "owner@example.com", "password": "correct-horse-battery"},
    )
    assert res.status_code == 429  # … so even the right password waits
    assert "retry-after" in res.headers


async def test_totp_flow(anon: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    await register(anon)
    setup = (await anon.post(f"{API}/auth/totp/setup")).json()
    code = pyotp.TOTP(setup["secret"]).now()
    confirm = await anon.post(f"{API}/auth/totp/confirm", json={"code": code})
    assert confirm.status_code == 200
    recovery = confirm.json()["recovery_codes"]
    assert len(recovery) == 10

    anon.headers.pop("Authorization")
    creds = {"email": "owner@example.com", "password": "correct-horse-battery"}
    need = await anon.post(f"{API}/auth/login", json=creds)
    assert need.status_code == 401 and need.json()["detail"] == "totp_required"
    # Same code can't be replayed; move the clock one step forward.
    clock = totp._clock
    monkeypatch.setattr(totp, "_clock", lambda: clock() + 30)
    fresh = pyotp.TOTP(setup["secret"]).at(int(clock()) + 30)
    ok = await anon.post(f"{API}/auth/login", json={**creds, "totp_code": fresh})
    assert ok.status_code == 200
    rec = await anon.post(f"{API}/auth/login", json={**creds, "totp_code": recovery[0]})
    assert rec.status_code == 200
    again = await anon.post(f"{API}/auth/login", json={**creds, "totp_code": recovery[0]})
    assert again.status_code == 401


async def test_invite_flow_and_roles(
    anon: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = await register(anon)
    invite = await anon.post(
        f"{API}/household/invites", json={"email": "spouse@example.com", "role": "viewer"}
    )
    token = invite.json()["token"]
    monkeypatch.setattr(get_settings(), "allow_registration", False)

    anon.headers.pop("Authorization")
    closed = await anon.post(
        f"{API}/auth/register",
        json={
            "email": "stranger@example.com",
            "password": "correct-horse-battery",
            "display_name": "X",
        },
    )
    assert closed.status_code == 403
    # Joining keeps the family's currency, whatever the new member's phone uses.
    spouse = await register(anon, "spouse@example.com", invite_token=token, currency="USD")
    assert spouse["household_id"] == owner["household_id"]
    assert (await anon.get(f"{API}/household")).json()["currency"] == "INR"
    assert spouse["role"] == "viewer"
    # Viewers can read but not write.
    assert (await anon.get(f"{API}/accounts")).status_code == 200
    denied = await anon.post(f"{API}/accounts", json={"kind": "cash", "name": "Wallet"})
    assert denied.status_code == 403
    members = (await anon.get(f"{API}/household/members")).json()
    assert {m["email"] for m in members} == {"owner@example.com", "spouse@example.com"}
