"""v4: people, automatic sorting (rules, learning, the assistant), describe-an-expense and the
spending explorer."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest

from coffernest.services.categorize import payee_key
from tests.conftest import bank

API = "/api/v1"


def test_payee_key_strips_bank_noise() -> None:
    assert payee_key("UPI-SWIGGY-8834551@ybl-Food order") == "swiggy food order"
    assert payee_key("POS 4521XXXX1234 BLINKIT GURGAON") == "blinkit gurgaon"
    assert payee_key("NEFT/HDFC000123/RAMESH KUMAR") == "ramesh kumar"
    assert payee_key("") == ""


async def _cats(client: httpx.AsyncClient) -> dict[str, str]:
    rows = (await client.get(f"{API}/categories")).json()
    by_id = {c["id"]: c for c in rows}
    out = {}
    for c in rows:
        parent = by_id.get(c["parent_id"]) if c["parent_id"] else None
        out[f"{parent['name']} / {c['name']}" if parent else c["name"]] = c["id"]
    return out


async def _spend(
    client: httpx.AsyncClient, account: str, payee: str, amount: str, **extra: Any
) -> dict[str, Any]:
    res = await client.post(
        f"{API}/transactions",
        json={
            "account_id": account,
            "date": extra.pop("day", date.today()).isoformat(),
            "amount": amount,
            "direction": "debit",
            "payee": payee,
            **extra,
        },
    )
    assert res.status_code == 201, res.text
    return res.json()[0]  # type: ignore[no-any-return]


async def test_seed_has_kids_and_food_delivery(client: httpx.AsyncClient) -> None:
    cats = await _cats(client)
    assert "Lifestyle / Food delivery" in cats
    for child in ("School & tuition", "Toys & fun", "Clothes & shoes", "Classes & sports"):
        assert f"Kids / {child}" in cats
    assert sum(1 for k in cats if k == "Kids") == 1
    people = (await client.get(f"{API}/people")).json()
    assert len(people) == 1 and people[0]["relation"] == "self"


async def test_merchants_sort_and_corrections_teach(client: httpx.AsyncClient) -> None:
    acct = (await bank(client))["id"]
    cats = await _cats(client)
    swiggy = await _spend(client, acct, "UPI-SWIGGY-8834551@ybl", "420")
    assert swiggy["category_id"] == cats["Lifestyle / Food delivery"] and swiggy["auto_sorted"]
    blinkit = await _spend(client, acct, "POS 4521XXXX BLINKIT", "880")
    assert blinkit["category_id"] == cats["Household / Groceries"]

    first = await _spend(client, acct, "UPI/RAMESH KUMAR/882211", "300")
    second = await _spend(client, acct, "UPI/RAMESH KUMAR/990011", "350")
    assert first["category_id"] is None and second["category_id"] is None
    fixed = await client.patch(
        f"{API}/transactions/{first['id']}", json={"category_id": cats["Household / Groceries"]}
    )
    assert fixed.status_code == 200, fixed.text
    assert fixed.json()[0]["similar"] == 1
    applied = await client.post(f"{API}/categorize/similar", json={"transaction_id": first["id"]})
    assert applied.json()["updated"] == 1
    third = await _spend(client, acct, "UPI/RAMESH KUMAR/123456", "200")
    assert third["category_id"] == cats["Household / Groceries"]
    undo = await client.post(f"{API}/categorize/undo", json={"run_id": applied.json()["run_id"]})
    assert undo.json()["updated"] == 1
    again = (await client.get(f"{API}/transactions/{second['id']}")).json()
    assert again["category_id"] is None
    rules = (await client.get(f"{API}/categorize/rules")).json()
    assert [r["match"] for r in rules] == ["ramesh kumar"]


async def test_people_and_describe(client: httpx.AsyncClient) -> None:
    acct = (await bank(client))["id"]
    cats = await _cats(client)
    aarav = await client.post(f"{API}/people", json={"name": "Aarav", "relation": "child"})
    assert aarav.status_code == 201, aarav.text
    vid = aarav.json()["id"]
    dup = await client.post(f"{API}/people", json={"name": "Aarav"})
    assert dup.status_code == 409

    draft = (
        await client.post(
            f"{API}/transactions/describe", json={"text": "aarav uniform 1,200 yesterday"}
        )
    ).json()
    assert Decimal(draft["amount"]) == Decimal("1200")
    assert draft["person_id"] == vid
    assert draft["category_id"] == cats["Kids / Clothes & shoes"]
    assert draft["date"] == (date.today() - timedelta(days=1)).isoformat()
    toys = (
        await client.post(f"{API}/transactions/describe", json={"text": "toys for Aarav 650"})
    ).json()
    assert toys["category_id"] == cats["Kids / Toys & fun"] and Decimal(toys["amount"]) == 650
    milk = (await client.post(f"{API}/transactions/describe", json={"text": "milk 60"})).json()
    assert (
        milk["person_id"] is None and milk["category_id"] == cats["Household / Milk & daily needs"]
    )

    # Saving a described entry: the words sort it and the name marks who it was for.
    row = await _spend(client, acct, "Aarav football class", "2500")
    assert row["person_id"] == vid and row["category_id"] == cats["Kids / Classes & sports"]
    only = await client.get(f"{API}/transactions", params={"person_id": vid, "include_totals": 1})
    assert only.json()["totals"]["count"] == 1
    assert Decimal(only.json()["totals"]["out"]) == Decimal("2500")


async def test_spend_explorer(client: httpx.AsyncClient) -> None:
    acct = (await bank(client, opening="500000.00"))["id"]
    cats = await _cats(client)
    vid = (await client.post(f"{API}/people", json={"name": "Vivaan"})).json()["id"]
    today = date.today()
    start = today - timedelta(days=89)
    for i in range(6):
        await _spend(client, acct, "Zomato", "500", day=start + timedelta(days=i * 14))
    await _spend(client, acct, "Vivaan toys", "1500", day=today - timedelta(days=3))
    await _spend(client, acct, "Lego store", "999", day=today - timedelta(days=2), person_id=vid)
    # The period before: 90 days earlier.
    await _spend(client, acct, "Zomato", "700", day=start - timedelta(days=10))
    res = await client.get(
        f"{API}/insights/spend", params={"from": start.isoformat(), "to": today.isoformat()}
    )
    assert res.status_code == 200, res.text
    s = res.json()
    assert s["bucket"] == "week" and s["days"] == 90
    assert Decimal(s["total"]) == Decimal("3000") + Decimal("1500") + Decimal("999")
    assert Decimal(s["previous"]) == Decimal("700")
    life = next(c for c in s["by_category"] if c["name"] == "Lifestyle")
    assert life["children"][0]["name"] == "Food delivery"
    kids = next(c for c in s["by_category"] if c["name"] == "Kids")
    assert Decimal(kids["amount"]) == Decimal("2499")
    who = {p["name"]: Decimal(p["amount"]) for p in s["by_person"]}
    assert who["Vivaan"] == Decimal("2499")
    assert s["top_payees"][0]["label"] == "Zomato"
    assert sum(Decimal(p["amount"]) for p in s["series"]) == Decimal(s["total"])

    inside = (
        await client.get(
            f"{API}/insights/spend",
            params={"from": start.isoformat(), "category_id": cats["Kids"]},
        )
    ).json()
    assert {c["name"] for c in inside["by_category"]} == {"Toys & fun"}
    everything = (await client.get(f"{API}/insights/spend")).json()
    assert everything["from"] == (start - timedelta(days=10)).isoformat()

    page = await client.get(
        f"{API}/transactions",
        params={"limit": 1, "include_totals": 1, "category_id": cats["Lifestyle"]},
    )
    body = page.json()
    assert len(body["items"]) == 1 and body["totals"]["count"] == 7

    # The assistant answers "how much did Vivaan cost" from the same numbers.
    from coffernest.ai.tools import BY_NAME
    from coffernest.db import sessionmaker

    hid = uuid.UUID((await client.get(f"{API}/household")).json()["id"])
    async with sessionmaker()() as session:
        out = await BY_NAME["get_spend"].handler(
            session, hid, {"person": "vivaan", "start": start.isoformat()}
        )
    assert out["total"] == Decimal("2499")


class SortingLLM:
    def __init__(self, *_: Any) -> None:
        pass

    async def extract(self, system: str, text: str, images: Any, schema: Any) -> dict[str, Any]:
        return {
            "items": [
                {"key": "sri balaji stores", "category": "Household / Groceries", "person": "",
                 "confidence": 0.93},
                {"key": "mystery traders", "category": "Lifestyle / Shopping", "person": "",
                 "confidence": 0.4},
            ]
        }  # fmt: skip


async def test_sort_past_entries_with_the_assistant(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coffernest.ai import providers

    acct = (await bank(client))["id"]
    cats = await _cats(client)
    for payee in ("ZOMATO ORDER", "SRI BALAJI STORES", "SRI BALAJI STORES", "MYSTERY TRADERS"):
        await _spend(client, acct, payee, "250", auto_sort=False)

    # Without the assistant: rules only.
    plain = (await client.post(f"{API}/categorize/run")).json()
    assert plain["by_rules"] == 1 and plain["left"] == 3 and not plain["ai_used"]
    await client.post(f"{API}/categorize/undo", json={"run_id": plain["run_id"]})

    providers.set_provider_override(SortingLLM)
    try:
        await client.patch(f"{API}/household", json={"ai_enabled": True})
        run = (await client.post(f"{API}/categorize/run")).json()
    finally:
        providers.set_provider_override(None)
    assert run["ai_used"] and run["by_rules"] == 1 and run["by_ai"] == 1
    assert run["suggested"] == 1 and run["left"] == 1
    review = (await client.get(f"{API}/categorize/review")).json()
    assert [g["key"] for g in review] == ["mystery traders"]
    assert review[0]["suggested_category_id"] == cats["Lifestyle / Shopping"]
    ok = await client.post(
        f"{API}/categorize/accept",
        json={"key": "mystery traders", "category_id": cats["Lifestyle / Shopping"]},
    )
    assert ok.json()["updated"] == 1
    assert (await client.get(f"{API}/categorize/review")).json() == []
    # A new entry from a payee the assistant sorted is sorted on arrival.
    later = await _spend(client, acct, "Sri Balaji Stores", "120")
    assert later["category_id"] == cats["Household / Groceries"]

    undone = (await client.post(f"{API}/categorize/undo", json={"run_id": run["run_id"]})).json()
    assert undone["updated"] == 3


async def test_compare_with_the_same_days_last_month(client: httpx.AsyncClient) -> None:
    acct = (await bank(client))["id"]
    await client.post(f"{API}/people", json={"name": "Aarav"})
    await _spend(client, acct, "Zomato", "400", day=date(2026, 9, 5))
    await _spend(client, acct, "Zomato", "900", day=date(2026, 9, 25))
    await _spend(client, acct, "Zomato", "300", day=date(2026, 10, 4))
    await _spend(client, acct, "Aarav toys", "700", day=date(2026, 10, 6))
    s = (
        await client.get(
            f"{API}/insights/spend",
            params={"from": "2026-10-01", "to": "2026-10-13", "compare_months": 1},
        )
    ).json()
    assert (s["previous_from"], s["previous_to"]) == ("2026-09-01", "2026-09-13")
    assert Decimal(s["previous"]) == Decimal("400")
    whole = (
        await client.get(
            f"{API}/insights/spend",
            params={"from": "2026-09-01", "to": "2026-09-30", "compare_months": 1},
        )
    ).json()
    assert (whole["previous_from"], whole["previous_to"]) == ("2026-08-01", "2026-08-31")
    fam = (
        await client.get(
            f"{API}/transactions",
            params={"from": "2026-10-01", "family": 1, "include_totals": 1},
        )
    ).json()
    assert fam["totals"]["count"] == 1 and Decimal(fam["totals"]["out"]) == Decimal("300")
