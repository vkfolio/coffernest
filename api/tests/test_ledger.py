import uuid
from datetime import date, timedelta

import httpx

from coffernest.services.recurring import materialize
from tests.conftest import FakeStorage, bank, categories, register

API = "/api/v1"


async def _balance(client: httpx.AsyncClient, account_id: str) -> str:
    res = await client.get(f"{API}/accounts/{account_id}")
    return str(res.json()["balance"])


async def test_spend_income_transfer_balances(client: httpx.AsyncClient) -> None:
    cats = await categories(client)
    salary = await bank(client, "50000.00", "Salary")
    savings = await bank(client, "0.00", "Savings")
    await client.post(
        f"{API}/transactions",
        json={
            "account_id": salary["id"],
            "date": "2026-02-01",
            "amount": "1250.50",
            "direction": "debit",
            "category_id": cats["Groceries"],
            "payee": "DMart",
        },
    )
    await client.post(
        f"{API}/transactions",
        json={
            "account_id": salary["id"],
            "date": "2026-02-01",
            "amount": "100000",
            "direction": "credit",
            "category_id": cats["Salary"],
        },
    )
    transfer = await client.post(
        f"{API}/transactions",
        json={
            "account_id": salary["id"],
            "to_account_id": savings["id"],
            "date": "2026-02-02",
            "amount": "20000",
            "direction": "debit",
        },
    )
    legs = transfer.json()
    assert len(legs) == 2 and legs[0]["transfer_group_id"] == legs[1]["transfer_group_id"]
    assert await _balance(client, salary["id"]) == "128749.50"
    assert await _balance(client, savings["id"]) == "20000.00"

    # Deleting one leg removes both; restore brings both back.
    await client.delete(f"{API}/transactions/{legs[1]['id']}")
    assert await _balance(client, savings["id"]) == "0.00"
    await client.post(f"{API}/transactions/{legs[1]['id']}/restore")
    assert await _balance(client, salary["id"]) == "128749.50"


async def test_client_supplied_id_is_idempotent(client: httpx.AsyncClient) -> None:
    acc = await bank(client)
    ident = str(uuid.uuid4())
    body = {
        "id": ident,
        "account_id": acc["id"],
        "date": "2026-03-01",
        "amount": "99",
        "direction": "debit",
    }
    first = await client.post(f"{API}/transactions", json=body)
    again = await client.post(f"{API}/transactions", json=body)
    assert first.json()[0]["id"] == again.json()[0]["id"] == ident
    page = (await client.get(f"{API}/transactions")).json()
    assert len(page["items"]) == 1


async def test_recurring_materialises_and_matches(client: httpx.AsyncClient) -> None:
    cats = await categories(client)
    acc = await bank(client)
    today = date.today()
    rule = await client.post(
        f"{API}/recurring-rules",
        json={
            "name": "Rent",
            "amount": "25000",
            "direction": "debit",
            "account_id": acc["id"],
            "category_id": cats["Rent"],
            "rrule": f"FREQ=MONTHLY;BYMONTHDAY={min(today.day, 28)}",
            "start_date": today.isoformat(),
        },
    )
    assert rule.status_code == 201, rule.text
    assert rule.json()["next_date"] is not None
    sched = (await client.get(f"{API}/transactions", params={"status": "scheduled"})).json()
    assert 2 <= len(sched["items"]) <= 3  # ~60 days ahead
    first = min(sched["items"], key=lambda t: t["date"])

    # Paying ₹24,800 a day later fulfils the scheduled rent instead of duplicating it.
    paid_on = date.fromisoformat(first["date"]) + timedelta(days=1)
    res = await client.post(
        f"{API}/transactions",
        json={
            "account_id": acc["id"],
            "date": paid_on.isoformat(),
            "amount": "24800",
            "direction": "debit",
            "category_id": cats["Rent"],
        },
    )
    out = res.json()
    assert out[0]["matched"] is True and out[0]["id"] == first["id"]
    assert out[0]["status"] == "cleared"

    # Skipping the next one records it on the rule.
    remaining = (await client.get(f"{API}/transactions", params={"status": "scheduled"})).json()
    nxt = min(remaining["items"], key=lambda t: t["date"])
    assert (await client.post(f"{API}/transactions/{nxt['id']}/skip")).status_code == 204
    rule_after = (await client.get(f"{API}/recurring-rules/{rule.json()['id']}")).json()
    assert nxt["occurrence_date"] in rule_after["skipped_dates"]


async def test_materialize_is_idempotent(client: httpx.AsyncClient) -> None:
    from coffernest import db

    acc = await bank(client)
    await client.post(
        f"{API}/recurring-rules",
        json={
            "name": "Milk",
            "amount": "60",
            "direction": "debit",
            "account_id": acc["id"],
            "rrule": "FREQ=DAILY",
            "start_date": date.today().isoformat(),
        },
    )
    before = len((await client.get(f"{API}/transactions", params={"limit": 200})).json()["items"])
    async with db.sessionmaker()() as session:
        created = await materialize(session, date.today() + timedelta(days=60))
        await session.commit()
    after = len((await client.get(f"{API}/transactions", params={"limit": 200})).json()["items"])
    assert created == 0 and before == after == 61


async def test_households_are_isolated(anon: httpx.AsyncClient) -> None:
    await register(anon, "a@example.com")
    acc = await bank(anon)
    txn = (
        await anon.post(
            f"{API}/transactions",
            json={
                "account_id": acc["id"],
                "date": "2026-01-05",
                "amount": "10",
                "direction": "debit",
            },
        )
    ).json()[0]
    await register(anon, "b@example.com")
    assert (await anon.get(f"{API}/accounts/{acc['id']}")).status_code == 404
    assert (await anon.get(f"{API}/transactions/{txn['id']}")).status_code == 404
    assert (await anon.get(f"{API}/transactions")).json()["items"] == []
    stolen = await anon.post(
        f"{API}/transactions",
        json={"account_id": acc["id"], "date": "2026-01-05", "amount": "10", "direction": "debit"},
    )
    assert stolen.status_code == 404


async def test_attachment_upload_and_link(client: httpx.AsyncClient, storage: FakeStorage) -> None:
    files = {"file": ("bill.pdf", b"%PDF-1.4 test", "application/pdf")}
    res = await client.post(f"{API}/attachments/upload", files=files, data={"kind": "bill"})
    assert res.status_code == 201, res.text
    item = res.json()
    assert item["size"] == 13 and item["status"] == "stored"
    link = await client.get(f"{API}/attachments/{item['id']}/url")
    assert link.json()["url"].startswith("https://s3.test/get/")

    ticket = await client.post(
        f"{API}/attachments",
        json={
            "filename": "statement.pdf",
            "mime": "application/pdf",
            "size": 1000,
            "kind": "statement",
        },
    )
    pending = ticket.json()["attachment"]
    assert (await client.post(f"{API}/attachments/{pending['id']}/complete")).status_code == 409
    key = ticket.json()["upload_url"].removeprefix("https://s3.test/put/")
    storage.objects[key] = b"x" * 1000
    done = await client.post(f"{API}/attachments/{pending['id']}/complete")
    assert done.json()["status"] == "stored"
    bad = await client.post(
        f"{API}/attachments",
        json={"filename": "x.exe", "mime": "application/x-msdownload", "size": 10},
    )
    assert bad.status_code == 415
