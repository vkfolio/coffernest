"""v3: net worth, money in vs out, loan history before entry, card history, CSV and backups."""

from __future__ import annotations

import csv
import io
import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from dateutil.relativedelta import relativedelta

from coffernest.main import app
from coffernest.services import amortization as am
from tests.conftest import FakeStorage, bank, register

API = "/api/v1"
PASS = "correct horse battery"


def _forward() -> am.Schedule:
    return am.build_schedule(
        am.LoanTerms(
            principal=Decimal("800000"),
            rate_pct=Decimal("9"),
            tenure_months=60,
            first_emi_date=date(2024, 1, 5),
        )
    )


def test_history_rebuilds_the_forward_schedule() -> None:
    full = _forward()
    entered = full.rows[12]
    terms = am.LoanTerms(
        principal=entered.opening,
        rate_pct=Decimal("9"),
        tenure_months=48,
        first_emi_date=entered.due_date,
        emi_override=full.rows[0].emi,
    )
    history = am.History(
        start_date=date(2024, 1, 5),
        original_principal=Decimal("800000"),
        original_tenure_months=60,
    )
    past = am.history_rows(terms, history)
    assert len(past) == 12
    for rebuilt, real in zip(past, full.rows[:12], strict=True):
        assert rebuilt.due_date == real.due_date
        assert rebuilt.estimated
        assert abs(rebuilt.opening - real.opening) <= 1
        assert abs(rebuilt.interest - real.interest) <= 1
        assert rebuilt.principal + rebuilt.interest == rebuilt.emi
    gap = am.reconstruction_gap(past, history)
    assert gap is not None and abs(gap) <= 1
    merged = am.with_history(am.build_schedule(terms), past)
    assert [r.period for r in merged.rows[:3]] == [1, 2, 3]
    assert merged.outstanding_on(date(2024, 6, 30)) == past[5].closing
    # Simulations still start from today's balance: history doesn't change them.
    assert am.build_schedule(terms).rows[0].opening == entered.opening


async def _loan(client: httpx.AsyncClient, funding: str, **extra: Any) -> dict[str, Any]:
    first = date.today().replace(day=5) - relativedelta(months=2)
    res = await client.post(
        f"{API}/loans",
        json={
            "name": "Home loan",
            "kind": "home",
            "lender": "HDFC Bank",
            "principal": "2000000",
            "original_principal": "2500000",
            "rate_pct": "8.5",
            "tenure_months": 180,
            "first_emi_date": first.isoformat(),
            "funding_account_id": funding,
            "tax_24b": True,
            "tax_80c": True,
            **extra,
        },
    )
    assert res.status_code == 201, res.text
    return res.json()  # type: ignore[no-any-return]


async def test_loan_history_paid_estimated_and_due(client: httpx.AsyncClient) -> None:
    acct = await bank(client, opening="500000.00")
    first = date.today().replace(day=5) - relativedelta(months=2)
    start = first - relativedelta(months=36)
    loan = await _loan(client, acct["id"], start_date=start.isoformat(), original_tenure_months=216)
    assert loan["start_date"] == start.isoformat()

    # The first EMI after entry was paid from the bank; the second one isn't marked.
    paid = await client.post(
        f"{API}/transactions",
        json={
            "account_id": acct["id"],
            "date": (first + timedelta(days=1)).isoformat(),
            "amount": "19695.00",
            "direction": "debit",
            "kind": "emi",
            "loan_id": loan["id"],
            "match_scheduled": False,
        },
    )
    assert paid.status_code == 201, paid.text

    hist = (await client.get(f"{API}/loans/{loan['id']}/history")).json()
    phases = [r["phase"] for r in hist["rows"]]
    assert phases[:36] == ["estimated"] * 36
    assert phases[36] == "paid" and hist["rows"][36]["transaction_id"]
    assert phases[37] == "due"
    assert set(phases[39:]) == {"future"}
    due_so_far = sum(1 for k in range(3) if first + relativedelta(months=k) <= date.today())
    assert hist["paid_to_date"]["emis"] == 36 + due_so_far
    assert Decimal(hist["paid_to_date"]["interest"]) > 0
    assert Decimal(hist["outstanding"]) == Decimal(loan["outstanding"])
    assert hist["reconstruction_gap"] is not None
    assert hist["series"][0]["estimated"] and not hist["series"][-1]["estimated"]

    # This year's tax view now includes the estimated EMIs from before entry.
    tax = (await client.get(f"{API}/insights/tax")).json()
    assert tax["loans"][0]["estimated"] is True

    # The same fields can be set later through PATCH.
    patched = await client.patch(
        f"{API}/loans/{loan['id']}", json={"original_emi": "21000", "original_principal": "2600000"}
    )
    assert patched.status_code == 200
    assert Decimal(patched.json()["original_emi"]) == Decimal("21000")


async def test_net_worth_and_money_in_out(client: httpx.AsyncClient) -> None:
    acct = await bank(client, opening="300000.00")
    await bank(client, opening="50000.00", name="Wallet", kind="wallet")
    card = await client.post(
        f"{API}/cards",
        json={
            "name": "HDFC Card",
            "issuer": "HDFC",
            "credit_limit": "200000",
            "statement_day": 10,
            "opening_owed": "40000",
        },
    )
    assert card.status_code == 201, card.text
    await _loan(client, acct["id"])
    today = date.today()
    for body in (
        {"amount": "120000", "direction": "credit", "kind": "income", "payee": "Salary"},
        {"amount": "18000", "direction": "debit", "kind": "expense", "payee": "Groceries"},
    ):
        res = await client.post(
            f"{API}/transactions",
            json={"account_id": acct["id"], "date": today.isoformat(), **body},
        )
        assert res.status_code == 201, res.text
    pay = await client.post(
        f"{API}/transactions",
        json={
            "account_id": acct["id"],
            "to_account_id": card.json()["account_id"],
            "date": today.isoformat(),
            "amount": "10000",
            "direction": "debit",
            "kind": "card_payment",
        },
    )
    assert pay.status_code == 201, pay.text

    ov = (await client.get(f"{API}/insights/overview")).json()
    nw = ov["net_worth"]
    assert Decimal(nw["assets"]) == Decimal("442000")  # 300k + 120k − 18k − 10k + 50k wallet
    assert Decimal(nw["liabilities"]) == Decimal(ov["debt"]["total_outstanding"])
    assert Decimal(nw["net"]) == Decimal(nw["assets"]) - Decimal(nw["liabilities"])
    assert Decimal(nw["breakdown"]["cards"]) == Decimal("30000")
    assert Decimal(ov["have"]["liquid"]) == Decimal("442000")
    assert Decimal(ov["savings_rate_pct"]) == Decimal("85.00")
    assert nw["series"][-1]["month"] == today.replace(day=1).isoformat()
    long = (await client.get(f"{API}/insights/networth?months=24")).json()
    assert long["net"] == nw["net"]
    # A loan without a start date was already owed in earlier months.
    assert all(Decimal(p["liabilities"]) > 1_000_000 for p in long["series"])

    io_ = (await client.get(f"{API}/insights/inout")).json()
    assert Decimal(io_["income"]) == Decimal("120000")
    assert Decimal(io_["spend"]) == Decimal("18000")
    assert Decimal(io_["transfers"]) == Decimal("10000")
    mine = next(a for a in io_["by_account"] if a["account_id"] == acct["id"])
    assert Decimal(mine["in"]) == Decimal("120000") and Decimal(mine["out"]) == Decimal("28000")

    outs = (await client.get(f"{API}/transactions?direction=debit")).json()["items"]
    kinds = {t["kind"] for t in outs}
    assert "expense" in kinds and "card_payment" not in kinds  # a card payment is a move
    ins = (await client.get(f"{API}/transactions?direction=credit")).json()["items"]
    assert [t["payee"] for t in ins] == ["Salary"]

    hist = (await client.get(f"{API}/cards/{card.json()['id']}/history")).json()
    assert Decimal(hist["months"][-1]["payments"]) == Decimal("10000")
    assert Decimal(hist["months"][-1]["owed_end"]) == Decimal("30000")


async def test_csv_export_has_no_account_numbers(client: httpx.AsyncClient) -> None:
    acct = await bank(client, account_number="50100123456789")
    await client.post(
        f"{API}/transactions",
        json={
            "account_id": acct["id"],
            "date": date.today().isoformat(),
            "amount": "999.50",
            "direction": "debit",
            "kind": "expense",
            "payee": "=HYPERLINK(evil)",
        },
    )
    res = await client.get(f"{API}/export/transactions.csv")
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/csv")
    assert "50100123456789" not in res.text
    rows = list(csv.DictReader(io.StringIO(res.text.lstrip("﻿"))))
    assert rows[0]["out"] == "999.50" and rows[0]["in"] == ""
    assert rows[0]["payee"].startswith("'=")


@pytest.fixture
def backup_storage(storage: FakeStorage, monkeypatch: pytest.MonkeyPatch) -> FakeStorage:
    import coffernest.services.backup

    monkeypatch.setattr(coffernest.services.backup, "get_storage", lambda: storage)
    return storage


async def _seed_books(client: httpx.AsyncClient, storage: FakeStorage) -> dict[str, Any]:
    acct = await bank(client, opening="250000.00", account_number="50100123456789")
    other = await bank(client, opening="1000.00", name="Savings")
    await _loan(client, acct["id"], start_date="2023-02-05")
    await client.post(
        f"{API}/transactions",
        json={
            "account_id": acct["id"],
            "to_account_id": other["id"],
            "date": date.today().isoformat(),
            "amount": "5000",
            "direction": "debit",
            "kind": "transfer",
        },
    )
    up = await client.post(
        f"{API}/attachments/upload",
        files={"file": ("statement.pdf", b"%PDF-backup", "application/pdf")},
    )
    assert up.status_code == 201, up.text
    return acct


async def test_backup_round_trip_into_another_household(
    client: httpx.AsyncClient, backup_storage: FakeStorage
) -> None:
    acct = await _seed_books(client, backup_storage)
    short = await client.post(f"{API}/backup/export", json={"passphrase": "short"})
    assert short.status_code == 422
    res = await client.post(
        f"{API}/backup/export", json={"passphrase": PASS, "include_attachments": True}
    )
    assert res.status_code == 200, res.text
    assert res.headers["content-disposition"].endswith('.cnz"')
    blob = res.content
    assert b"50100123456789" not in blob

    peek = await client.post(f"{API}/backup/inspect", files={"file": ("b.cnz", blob)})
    assert peek.status_code == 200, peek.text
    manifest = peek.json()
    assert manifest["counts"]["account"] >= 3 and manifest["counts"]["loan"] == 1

    # A second household, owned by someone else.
    transport = httpx.ASGITransport(app=app, client=("203.0.113.10", 5000))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={"X-Client": "native"}
    ) as other:
        await register(other, email="second@example.com")
        name = (await other.get(f"{API}/household")).json()["name"]
        form = {"passphrase": PASS, "confirm_name": "not it"}
        files = {"file": ("b.cnz", blob)}
        assert (await other.post(f"{API}/backup/import", data=form, files=files)).status_code == 409
        bad = {"passphrase": "wrong passphrase!", "confirm_name": name}
        res = await other.post(f"{API}/backup/import", data=bad, files=files)
        assert res.status_code == 400 and "passphrase" in res.json()["detail"]
        assert (await other.get(f"{API}/backup/last-safety")).status_code == 404

        ok = await other.post(
            f"{API}/backup/import", data={"passphrase": PASS, "confirm_name": name}, files=files
        )
        assert ok.status_code == 200, ok.text
        result = ok.json()
        assert result["safety_backup"] is True
        assert result["counts"]["account"] == manifest["counts"]["account"]
        assert result["counts"]["txn"] == manifest["counts"]["txn"]
        assert any("now yours" in w for w in result["warnings"])

        accounts = (await other.get(f"{API}/accounts")).json()
        assert acct["id"] not in {a["id"] for a in accounts}  # fresh ids
        restored = next(a for a in accounts if a["name"] == "Bank")
        shown = (await other.get(f"{API}/accounts/{restored['id']}/number")).json()
        assert shown["account_number"] == "50100123456789"  # sealed again with this server's key
        loans = (await other.get(f"{API}/loans")).json()
        assert loans[0]["start_date"] == "2023-02-05"
        mine = (await client.get(f"{API}/loans")).json()
        assert Decimal(loans[0]["outstanding"]) == Decimal(mine[0]["outstanding"])
        txns = (await other.get(f"{API}/transactions")).json()["items"]
        transfers = [t for t in txns if t["kind"] == "transfer"]
        assert (
            len(transfers) == 2
            and transfers[0]["transfer_group_id"] == transfers[1]["transfer_group_id"]
        )
        assert manifest["attachments_count"] == 1 and result["files"] == 1
        restored_keys = [k for k in backup_storage.objects if "/restored/" in k]
        assert backup_storage.objects[restored_keys[0]] == b"%PDF-backup"

        safety = await other.get(f"{API}/backup/last-safety")
        assert safety.status_code == 200 and safety.content[:2] == b"PK"

    # Restoring into the same household replaces, not duplicates.
    name = (await client.get(f"{API}/household")).json()["name"]
    again = await client.post(
        f"{API}/backup/import",
        data={"passphrase": PASS, "confirm_name": name},
        files={"file": ("b.cnz", blob)},
    )
    assert again.status_code == 200, again.text
    accounts = (await client.get(f"{API}/accounts")).json()
    assert len([a for a in accounts if a["name"] == "Bank"]) == 1
    assert not again.json()["warnings"] or all(
        "now yours" not in w for w in again.json()["warnings"]
    )


async def test_live_stream_gives_its_db_connection_back(client: httpx.AsyncClient) -> None:
    """Each open tab holds an /events stream for hours: it must not pin a pooled connection."""
    from starlette.requests import Request

    from coffernest.db import sessionmaker
    from coffernest.deps import Ctx
    from coffernest.enums import Role
    from coffernest.models import User
    from coffernest.routes.live import events

    me = (await client.get(f"{API}/household/members")).json()[0]
    household = (await client.get(f"{API}/household")).json()
    async with sessionmaker()() as session:
        user = await session.get(User, uuid.UUID(me["user_id"]))
        assert user is not None and session.in_transaction()
        ctx = Ctx(user.id, uuid.UUID(me["id"]), uuid.UUID(household["id"]), Role.owner)
        res = await events(Request({"type": "http", "headers": []}), ctx, session)
        assert not session.in_transaction()
        await res.body_iterator.aclose()  # type: ignore[attr-defined]


async def _owner_ctx(client: httpx.AsyncClient) -> Any:
    from coffernest.deps import Ctx
    from coffernest.enums import Role

    hid = uuid.UUID((await client.get(f"{API}/household")).json()["id"])
    me = (await client.get(f"{API}/household/members")).json()[0]
    return Ctx(uuid.UUID(me["user_id"]), uuid.UUID(me["id"]), hid, Role.owner)


async def _scheduled(client: httpx.AsyncClient, payee_part: str) -> int:
    res = await client.get(f"{API}/transactions", params={"status": "scheduled", "limit": 200})
    assert res.status_code == 200, res.text
    body = res.json()
    rows = body["items"] if isinstance(body, dict) else body
    return sum(1 for t in rows if payee_part.lower() in (t.get("payee") or "").lower())


async def test_card_loan_moves_onto_the_card(client: httpx.AsyncClient) -> None:
    from coffernest.db import sessionmaker
    from coffernest.services import actions

    acct = await bank(client, opening="200000.00", name="Axis Salary")
    card = await client.post(
        f"{API}/cards",
        json={
            "name": "Axis Card",
            "issuer": "Axis Bank",
            "credit_limit": "500000",
            "statement_day": 10,
            "opening_owed": "100000",
        },
    )
    assert card.status_code == 201, card.text
    res = await client.post(
        f"{API}/loans",
        json={
            "name": "Axis Card Loan",
            "kind": "personal",
            "lender": "Axis Bank",
            "principal": "70000",
            "rate_pct": "15",
            "tenure_months": 5,
            "first_emi_date": (date.today() + timedelta(days=7)).isoformat(),
            "funding_account_id": acct["id"],
        },
    )
    assert res.status_code == 201, res.text
    loan_id = res.json()["id"]
    assert await _scheduled(client, "Axis Bank") >= 1
    before = (await client.get(f"{API}/insights/networth", params={"months": 2})).json()

    async with sessionmaker()() as session:
        ctx = await _owner_ctx(client)
        with pytest.raises(actions.ActionError):
            await actions.stop_recurring(session, ctx, name="Axis Card Loan EMI")
        out = await actions.move_loan_to_card(
            session, ctx, loan="Axis Card Loan", card="Axis Card", card_owed_includes_it=False
        )
    assert out["emis_left"] == 5 and Decimal(out["outstanding"]) == Decimal("70000")
    assert out["added_to_card_balance"] and out["bank_entries_removed"] >= 1
    assert await _scheduled(client, "Axis Bank") == 0
    assert loan_id not in {ln["id"] for ln in (await client.get(f"{API}/loans")).json()}
    cards = (await client.get(f"{API}/cards")).json()
    assert Decimal(cards[0]["owed"]) == Decimal("170000")
    emis = (await client.get(f"{API}/cards/{cards[0]['id']}/emis")).json()
    assert len(emis) == 1 and emis[0]["kind"] == "loan_on_card"
    after = (await client.get(f"{API}/insights/networth", params={"months": 2})).json()
    # The same money is owed; it is just owed on the card now.
    assert Decimal(after["liabilities"]) == Decimal(before["liabilities"])

    async with sessionmaker()() as session:
        await actions.undo(session, await _owner_ctx(client), out["undo"])
    assert loan_id in {ln["id"] for ln in (await client.get(f"{API}/loans")).json()}
    assert await _scheduled(client, "Axis Bank") >= 1
    cards = (await client.get(f"{API}/cards")).json()
    assert Decimal(cards[0]["owed"]) == Decimal("100000")
    assert (await client.get(f"{API}/cards/{cards[0]['id']}/emis")).json() == []

    # From the app: the card already counts the loan, so its balance stays.
    moved = await client.post(
        f"{API}/loans/{loan_id}/move-to-card",
        json={"card_id": cards[0]["id"], "card_owed_includes_it": True},
    )
    assert moved.status_code == 200, moved.text
    cards = (await client.get(f"{API}/cards")).json()
    assert Decimal(cards[0]["owed"]) == Decimal("100000")
    again = await client.post(
        f"{API}/loans/{loan_id}/move-to-card", json={"card_id": cards[0]["id"]}
    )
    assert again.status_code in (404, 409)


async def test_stop_recurring_from_chat(client: httpx.AsyncClient) -> None:
    from coffernest.db import sessionmaker
    from coffernest.services import actions

    await bank(client, name="Axis Salary")
    async with sessionmaker()() as session:
        ctx = await _owner_ctx(client)
        await actions.add_recurring(
            session,
            ctx,
            name="Mutual Fund SIP",
            amount=15000,
            account="Axis Salary",
            day_of_month=min(28, (date.today() + timedelta(days=3)).day),
            start=date.today().isoformat(),
        )
    assert await _scheduled(client, "Mutual Fund SIP") >= 1
    async with sessionmaker()() as session:
        ctx = await _owner_ctx(client)
        with pytest.raises(actions.ActionError):
            await actions.stop_recurring(session, ctx, name="Netflix")
        out = await actions.stop_recurring(session, ctx, name="sip")
    assert out["stopped"] == "Mutual Fund SIP" and out["planned_entries_removed"] >= 1
    assert await _scheduled(client, "Mutual Fund SIP") == 0
    rules = (await client.get(f"{API}/recurring-rules")).json()
    assert not any(r["active"] for r in rules if r["name"] == "Mutual Fund SIP")
    async with sessionmaker()() as session:
        await actions.undo(session, await _owner_ctx(client), out["undo"])
    assert await _scheduled(client, "Mutual Fund SIP") >= 1
