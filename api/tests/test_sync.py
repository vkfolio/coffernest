"""Two-way sync (/api/v1/sync): pull, push, id mapping, conflicts, deletes and household scope."""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from tests.conftest import bank, categories, register

API = "/api/v1"
PHONE = "phone-0001"
TABLET = "tablet-0001"


async def _sync(client: httpx.AsyncClient, device: str = PHONE, **body: Any) -> dict[str, Any]:
    res = await client.post(f"{API}/sync", json={"client_id": device, **body})
    assert res.status_code == 200, res.text
    out: dict[str, Any] = res.json()
    return out


def _ids(body: dict[str, Any], table: str) -> set[str]:
    return {r["id"] for r in body["changes"].get(table, [])}


def _txn(account: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "account_id": account,
        "date": "2026-10-01",
        "amount": "250.00",
        "direction": "debit",
        "kind": "expense",
        "status": "cleared",
        "source": "manual",
        "payee": "Chai",
        "note": "",
        "tags": [],
        "updated_at": "2026-10-01T10:00:00+00:00",
        **extra,
    }


def _account(**extra: Any) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "kind": "cash",
        "name": "Wallet",
        "opening_balance": "500.00",
        "opening_date": "2026-09-01",
        "updated_at": "2026-10-01T09:00:00+00:00",
        **extra,
    }


async def test_full_pull_then_nothing_new(client: httpx.AsyncClient) -> None:
    acct = await bank(client)
    first = await _sync(client)
    assert first["cursor"]
    assert acct["id"] in _ids(first, "account")
    assert len(first["changes"]["category"]) > 20
    assert "household_id" not in first["changes"]["account"][0]
    assert first["household"]["name"]
    again = await _sync(client, cursor=first["cursor"])
    assert again["changes"] == {}
    assert again["deletes"] == []


async def test_push_appears_in_api_and_not_echoed(client: httpx.AsyncClient) -> None:
    start = await _sync(client)
    wallet = _account()
    spend = _txn(wallet["id"])
    pushed = await _sync(
        client, cursor=start["cursor"], changes={"account": [wallet], "txn": [spend]}
    )
    assert pushed["rejected"] == []
    assert pushed["applied"] == 2
    assert pushed["changes"] == {}  # the phone's own rows don't come back

    res = await client.get(f"{API}/transactions", params={"account_id": wallet["id"]})
    assert [t["id"] for t in res.json()["items"]] == [spend["id"]]
    accounts = {a["id"]: a for a in (await client.get(f"{API}/accounts")).json()}
    assert accounts[wallet["id"]]["balance"] == "250.00"

    other = await _sync(client, device=TABLET, cursor=start["cursor"])
    assert _ids(other, "account") == {wallet["id"]}
    assert _ids(other, "txn") == {spend["id"]}


async def test_default_categories_map_to_the_servers(client: httpx.AsyncClient) -> None:
    cats = await categories(client)
    housing, rent = str(uuid.uuid4()), str(uuid.uuid4())
    edited = "2020-01-01T00:00:00+00:00"
    common = {"kind": "expense", "is_fixed": True, "color_slot": 1, "archived": False}
    out = await _sync(
        client,
        pull=False,
        changes={
            "category": [
                {"id": rent, "parent_id": housing, "name": "Rent", "updated_at": edited, **common},
                {
                    "id": housing,
                    "parent_id": None,
                    "name": "Housing",
                    "updated_at": edited,
                    **common,
                },
            ]
        },
    )
    assert out["id_map"] == {housing: cats["Housing"], rent: cats["Rent"]}
    assert out["applied"] == 0  # the household's copies stay
    assert {r["id"] for r in out["changes"]["category"]} == {cats["Housing"], cats["Rent"]}
    assert len(await categories(client)) == len(cats)

    # Even when the device's copy is newer: a new phone can't undo the household's own changes.
    newer = "2099-01-01T00:00:00+00:00"
    out = await _sync(
        client,
        pull=False,
        changes={
            "category": [
                {
                    **common,
                    "id": str(uuid.uuid4()),
                    "parent_id": None,
                    "name": "Housing",
                    "color_slot": 5,
                    "updated_at": newer,
                }
            ]
        },
    )
    assert out["applied"] == 0
    assert out["changes"]["category"][0]["color_slot"] == 1


async def test_newest_edit_wins(client: httpx.AsyncClient) -> None:
    acct = await bank(client)
    res = await client.post(
        f"{API}/transactions",
        json={
            "account_id": acct["id"],
            "date": "2026-10-02",
            "amount": "100.00",
            "direction": "debit",
            "payee": "Shop",
        },
    )
    txn = res.json()[0]
    start = await _sync(client)
    row = next(r for r in start["changes"]["txn"] if r["id"] == txn["id"])

    stale = {**row, "payee": "Old edit", "updated_at": "2020-01-01T00:00:00+00:00"}
    out = await _sync(client, cursor=start["cursor"], changes={"txn": [stale]})
    assert out["applied"] == 0
    assert out["changes"]["txn"][0]["payee"] == "Shop"  # the phone is told to take the server's

    fresh = {**row, "payee": "New edit", "updated_at": "2099-01-01T00:00:00+00:00"}
    out = await _sync(client, cursor=start["cursor"], changes={"txn": [fresh]})
    assert out["applied"] == 1
    got = (await client.get(f"{API}/transactions/{txn['id']}")).json()
    assert got["payee"] == "New edit"


async def test_deletes_both_ways(client: httpx.AsyncClient) -> None:
    start = await _sync(client)
    wallet, spare = _account(), _account(name="Spare")
    spend = _txn(wallet["id"])
    await _sync(client, changes={"account": [wallet, spare], "txn": [spend]})
    tablet = await _sync(client, device=TABLET)

    out = await _sync(
        client,
        cursor=start["cursor"],
        deletes=[{"table": "txn", "id": spend["id"], "deleted_at": "2099-01-01T00:00:00+00:00"}],
    )
    assert out["applied"] == 1
    res = await client.get(f"{API}/transactions", params={"account_id": wallet["id"]})
    assert res.json()["items"] == []

    # The app only archives accounts; a device's delete removes the row, and others hear of it.
    gone_at = "2099-01-01T00:00:00+00:00"
    await _sync(client, deletes=[{"table": "account", "id": spare["id"], "deleted_at": gone_at}])
    seen = await _sync(client, device=TABLET, cursor=tablet["cursor"])
    gone = {(d["table"], d["id"]) for d in seen["deletes"]}
    assert ("txn", spend["id"]) in gone
    assert ("account", spare["id"]) in gone


async def test_household_scope(anon: httpx.AsyncClient, client: httpx.AsyncClient) -> None:
    theirs = await bank(client, name="Theirs")
    pulled = await _sync(client)
    their_row = next(r for r in pulled["changes"]["account"] if r["id"] == theirs["id"])

    # Someone else, in their own household.
    async with httpx.AsyncClient(
        transport=anon._transport, base_url="http://test", headers={"X-Client": "native"}
    ) as other:
        await register(other, "intruder@example.com")
        out = await _sync(
            other,
            device="intruder-1",
            changes={
                "account": [
                    {**their_row, "name": "Mine now", "updated_at": "2099-01-01T00:00:00Z"}
                ],
                "txn": [_txn(theirs["id"])],
            },
        )
        # The borrowed id becomes a new account of theirs, and their entry follows it.
        assert out["id_map"][theirs["id"]] != theirs["id"]
        assert out["rejected"] == []
        # An entry pointing straight at our account is refused.
        out = await _sync(other, device="intruder-1", changes={"txn": [_txn(theirs["id"])]})
        assert [r["table"] for r in out["rejected"]] == ["txn"]
        mine = await _sync(other, device="intruder-1")
        assert theirs["id"] not in _ids(mine, "account")

    accounts = {a["id"]: a for a in (await client.get(f"{API}/accounts")).json()}
    assert accounts[theirs["id"]]["name"] == "Theirs"


async def test_bill_occurrences_match_by_rule_and_date(client: httpx.AsyncClient) -> None:
    acct = await bank(client)
    cats = await categories(client)
    res = await client.post(
        f"{API}/recurring-rules",
        json={
            "name": "Broadband",
            "amount": "999.00",
            "direction": "debit",
            "kind": "expense",
            "account_id": acct["id"],
            "category_id": cats["Broadband"],
            "rrule": "FREQ=MONTHLY;BYMONTHDAY=8",
            "start_date": "2026-01-01",
        },
    )
    assert res.status_code == 201, res.text
    rule = res.json()
    full = await _sync(client)
    assert all(
        not t.get("recurring_rule_id") or t["status"] != "scheduled"
        for t in full["changes"].get("txn", [])
    )
    assert rule["id"] in _ids(full, "recurring_rule")

    # The phone made its own copy of next month's bill and marked it paid.
    upcoming = (await client.get(f"{API}/recurring-rules/{rule['id']}")).json()
    day = upcoming["next_date"]
    mine = _txn(
        acct["id"],
        date=day,
        amount="999.00",
        status="cleared",
        source="recurring",
        recurring_rule_id=rule["id"],
        occurrence_date=day,
        updated_at="2099-01-01T00:00:00+00:00",
    )
    out = await _sync(client, cursor=full["cursor"], changes={"txn": [mine]})
    server_id = out["id_map"][mine["id"]]
    got = (await client.get(f"{API}/transactions/{server_id}")).json()
    assert got["status"] == "cleared"


async def test_worker_bookkeeping_is_not_an_edit(client: httpx.AsyncClient) -> None:
    from sqlalchemy import text

    from coffernest import db

    acct = await bank(client)
    res = await client.post(
        f"{API}/recurring-rules",
        json={
            "name": "Rent",
            "amount": "20000.00",
            "direction": "debit",
            "kind": "expense",
            "account_id": acct["id"],
            "rrule": "FREQ=MONTHLY;BYMONTHDAY=1",
            "start_date": "2026-01-01",
        },
    )
    rule = res.json()
    start = await _sync(client)
    async with db.engine().begin() as conn:
        await conn.execute(
            text("UPDATE recurring_rule SET materialized_until = '2099-01-01' WHERE id = :i"),
            {"i": rule["id"]},
        )
    again = await _sync(client, cursor=start["cursor"])
    assert rule["id"] not in _ids(again, "recurring_rule")


async def test_too_many_rows_at_once(client: httpx.AsyncClient) -> None:
    rows = [_account() for _ in range(5001)]
    res = await client.post(f"{API}/sync", json={"client_id": PHONE, "changes": {"account": rows}})
    assert res.status_code == 413


async def test_sub_category_waits_for_its_parent(client: httpx.AsyncClient) -> None:
    row = {
        "id": str(uuid.uuid4()),
        "parent_id": str(uuid.uuid4()),  # not sent yet
        "name": "Orphan",
        "kind": "expense",
        "is_fixed": False,
        "color_slot": 2,
        "archived": False,
        "updated_at": "2026-10-01T00:00:00+00:00",
    }
    out = await _sync(client, pull=False, changes={"category": [row]})
    assert [r["id"] for r in out["rejected"]] == [row["id"]]
    assert "Orphan" not in await categories(client)
