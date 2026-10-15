from datetime import date, timedelta
from decimal import Decimal as D

import httpx

from coffernest.demo import build
from tests.conftest import bank, register

API = "/api/v1"


def _next_day(day: int) -> date:
    today = date.today()
    candidate = today.replace(day=day)
    if candidate <= today:
        candidate = (candidate.replace(day=1) + timedelta(days=32)).replace(day=day)
    return candidate


async def test_loan_creates_emi_rule_and_follows_prepayments(client: httpx.AsyncClient) -> None:
    acc = await bank(client, "500000.00")
    first = _next_day(5)
    res = await client.post(
        f"{API}/loans",
        json={
            "name": "Car loan",
            "kind": "car",
            "lender": "Axis",
            "principal": "800000",
            "rate_pct": "9",
            "tenure_months": 60,
            "first_emi_date": first.isoformat(),
            "funding_account_id": acc["id"],
        },
    )
    assert res.status_code == 201, res.text
    loan = res.json()
    assert D(loan["emi"]) == D("16607")  # ₹8 L, 9 %, 5 years
    assert loan["outstanding"] == "800000.00" or D(loan["outstanding"]) == D("800000")
    rules = (await client.get(f"{API}/recurring-rules")).json()
    emi_rule = next(r for r in rules if r["loan_id"] == loan["id"])
    assert D(emi_rule["amount"]) == D("16607") and emi_rule["auto_post"] is True

    # A ₹2 L prepayment, reduce-EMI mode → the auto-debit rule follows the new EMI.
    pre = await client.post(
        f"{API}/loans/{loan['id']}/prepayments",
        json={
            "date": date.today().isoformat(),
            "amount": "200000",
            "mode": "reduce_emi",
            "from_account_id": acc["id"],
        },
    )
    assert pre.status_code == 201, pre.text
    after = (await client.get(f"{API}/loans/{loan['id']}")).json()
    assert D(after["emi"]) < D("16607")
    rule_after = (await client.get(f"{API}/recurring-rules/{emi_rule['id']}")).json()
    assert D(rule_after["amount"]) == D(after["emi"])
    assert (await client.get(f"{API}/accounts/{acc['id']}")).json()["balance"] == "300000.00"

    sim = await client.post(f"{API}/loans/{loan['id']}/simulate", json={"extra_monthly": "5000"})
    assert sim.status_code == 200
    assert D(sim.json()["interest_saved"]) > 0 and sim.json()["months_saved"] > 0

    sched = (await client.get(f"{API}/loans/{loan['id']}/schedule")).json()
    assert sched["rows"][-1]["closing"] in ("0.00", "0")


async def test_card_statement_min_due_and_payment(client: httpx.AsyncClient) -> None:
    acc = await bank(client, "100000.00")
    card = (
        await client.post(
            f"{API}/cards",
            json={
                "name": "Regalia",
                "issuer": "HDFC",
                "credit_limit": "200000",
                "statement_day": 12,
                "pay_from_account_id": acc["id"],
                "opening_owed": "30000",
            },
        )
    ).json()
    assert card["utilization_pct"] == "15.00"
    stmt = (
        await client.post(
            f"{API}/cards/{card['id']}/statements",
            json={"statement_date": "2026-09-12", "total_due": "30000", "interest_charged": "0"},
        )
    ).json()
    assert stmt["min_due"] == "1500.00" and stmt["due_date"] == "2026-10-02"
    paid = (
        await client.post(
            f"{API}/cards/{card['id']}/statements/{stmt['id']}/pay",
            json={"amount": "1500", "date": "2026-09-30"},
        )
    ).json()
    assert paid["status"] == "min_paid"
    card_after = (await client.get(f"{API}/cards/{card['id']}")).json()
    assert card_after["owed"] == "28500.00"
    assert (await client.get(f"{API}/accounts/{acc['id']}")).json()["balance"] == "98500.00"

    emi = await client.post(
        f"{API}/cards/{card['id']}/emis",
        json={
            "kind": "loan_on_card",
            "description": "Loan on card",
            "principal": "50000",
            "rate_pct": "14",
            "tenure_months": 12,
            "first_emi_date": _next_day(12).isoformat(),
            "processing_fee": "499",
            "disbursed_to_account_id": acc["id"],
        },
    )
    assert emi.status_code == 201, emi.text
    assert D(emi.json()["gst_on_interest"]) > 0
    card_after = (await client.get(f"{API}/cards/{card['id']}")).json()
    assert card_after["owed"] == "78500.00"  # loan on card is owed on the card …
    assert (await client.get(f"{API}/accounts/{acc['id']}")).json()["balance"] == "148500.00"


async def test_demo_household_overview(anon: httpx.AsyncClient) -> None:
    await build(anon, "demo@example.com", "demo-password-123")
    res = await anon.get(f"{API}/insights/overview")
    assert res.status_code == 200, res.text
    data = res.json()
    debt = data["debt"]
    assert len(debt["loans"]) == 3 and len(debt["cards"]) == 2
    assert D(debt["monthly_income"]) == D("210000.00")
    assert debt["foir_pct"] is not None and D(debt["foir_pct"]) > 30
    sts = data["safe_to_spend"]
    assert D(sts["amount"]) == (
        D(sts["liquid"]) - D(sts["committed"]) - D(sts["card_dues"]) - D(sts["buffer"])
    )
    assert data["dues"], "upcoming EMIs and bills should be listed"
    assert any(n["kind"] == "prepay_target" for n in data["notes"])

    plan = (await anon.post(f"{API}/loans/payoff-plan", json={"extra_monthly": "20000"})).json()
    assert D(plan["avalanche"]["total_interest"]) < D(plan["baseline"]["total_interest"])
    assert plan["avalanche"]["months"] <= plan["baseline"]["months"]

    for path in ("cashflow", "categories", "anomalies", "tax", "dues?days=60", "notes"):
        assert (await anon.get(f"{API}/insights/{path}")).status_code == 200, path


async def test_viewer_cannot_change_loans(anon: httpx.AsyncClient) -> None:
    await register(anon)
    invite = (
        await anon.post(
            f"{API}/household/invites", json={"email": "kid@example.com", "role": "viewer"}
        )
    ).json()
    await register(anon, "kid@example.com", invite_token=invite["token"])
    res = await anon.post(
        f"{API}/loans",
        json={
            "name": "x",
            "kind": "personal",
            "principal": "1000",
            "rate_pct": "10",
            "tenure_months": 10,
            "first_emi_date": "2027-01-01",
        },
    )
    assert res.status_code == 403
