"""Demo household with a year of history, created through the real API (so it doubles as an
end-to-end smoke test).

uv run coffernest seed-demo --email you@example.com --password '…'
uv run coffernest seed-demo --base-url https://coffernest.example.com --email … --password …

With ``--base-url`` it signs up on a running server over HTTP (registration must be open);
otherwise it writes straight into this install's database.

A server with ``SEED_DEMO``, ``DEMO_EMAIL`` and ``DEMO_PASSWORD`` set keeps its own demo account
(``ensure_demo``): created at start-up, brought back if someone deletes it, and started afresh
every ``DEMO_REFRESH_DAYS``. It never counts as the server's first account.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from dateutil.relativedelta import relativedelta
from sqlalchemy import select

from coffernest import db
from coffernest.config import Settings, get_settings
from coffernest.models import User
from coffernest.routes.auth import SEEDING_DEMO, remove_files
from coffernest.routes.meta import PASSWORD_MIN_LENGTH
from coffernest.services.erase import EraseError, erase_account

MONTHS = 12
log = logging.getLogger("coffernest.demo")
_lock = asyncio.Lock()


async def seed(email: str, password: str, base_url: str | None = None) -> None:
    headers = {"X-Client": "native", "User-Agent": "coffernest-seed-demo"}
    if base_url:
        async with httpx.AsyncClient(
            base_url=base_url.rstrip("/"), headers=headers, timeout=60
        ) as client:
            await build(client, email, password)
    else:
        from coffernest.main import app

        # Run on the server itself, so it may sign up even once sign-up is closed.
        token = SEEDING_DEMO.set(True)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://demo", headers=headers
            ) as client:
                await build(client, email, password)
        finally:
            SEEDING_DEMO.reset(token)
    print(f"Demo household ready. Log in as {email}")


def demo_enabled(settings: Settings) -> bool:
    return (
        settings.seed_demo
        and bool(settings.demo_email)
        and len(settings.demo_password) >= PASSWORD_MIN_LENGTH
    )


async def _erase(email: str) -> bool:
    """Remove the demo account and its household. False when it can't go (someone joined it)."""
    async with db.sessionmaker()() as session:
        user = await session.scalar(select(User).where(User.email == email))
        if user is None:
            return True
        try:
            households = await erase_account(session, user.id)
        except EraseError:
            return False
        await session.commit()
    await remove_files(households)
    return True


async def ensure_demo() -> str:
    """Keep the server's demo account: create it when it's missing, and start its books afresh
    once they're older than ``DEMO_REFRESH_DAYS``. Returns "off", "kept", "created" or
    "refreshed"."""
    settings = get_settings()
    if not demo_enabled(settings):
        return "off"
    email = settings.demo_email.lower()
    async with _lock:
        async with db.sessionmaker()() as session:
            created_at = await session.scalar(select(User.created_at).where(User.email == email))
        if created_at is not None:
            if datetime.now(UTC) - created_at < timedelta(days=settings.demo_refresh_days):
                return "kept"
            if not await _erase(email):
                log.warning("demo account %s has other members; not refreshing it", email)
                return "kept"
        try:
            await seed(email, settings.demo_password)
        except Exception:
            # A half-built demo would otherwise be kept until the next refresh.
            await _erase(email)
            raise
        return "created" if created_at is None else "refreshed"


async def build(client: httpx.AsyncClient, email: str, password: str) -> dict[str, Any]:
    async def call(method: str, url: str, body: Any = None) -> Any:
        res = await client.request(method, f"/api/v1{url}", json=body)
        if res.status_code >= 400:
            raise RuntimeError(f"{method} {url} → {res.status_code}: {res.text}")
        return res.json() if res.content else None

    session = await call(
        "POST",
        "/auth/register",
        {
            "email": email,
            "password": password,
            "display_name": "Priya",
            "household_name": "Sharma home",
        },
    )
    client.headers["Authorization"] = f"Bearer {session['access_token']}"
    cats = {c["name"]: c["id"] for c in await call("GET", "/categories")}
    today = date.today()
    back3 = (today - relativedelta(months=MONTHS)).replace(day=1)  # where the books start
    salary = 210000

    salary_ac = await call(
        "POST",
        "/accounts",
        {
            "kind": "bank",
            "name": "HDFC Salary",
            "institution": "HDFC Bank",
            "last4": "4821",
            "opening_balance": "142500.00",
            "opening_date": back3.isoformat(),
        },
    )
    savings = await call(
        "POST",
        "/accounts",
        {
            "kind": "bank",
            "name": "SBI Savings",
            "institution": "SBI",
            "last4": "0937",
            "opening_balance": "310000.00",
            "opening_date": back3.isoformat(),
            "include_in_safe_to_spend": False,
        },
    )
    await call(
        "POST",
        "/accounts",
        {
            "kind": "cash",
            "name": "Cash",
            "opening_balance": "6000.00",
            "opening_date": back3.isoformat(),
        },
    )

    def monthly(day: int) -> str:
        return f"FREQ=MONTHLY;BYMONTHDAY={day}"

    first_of_month = today.replace(day=1)
    await call(
        "POST",
        "/recurring-rules",
        {
            "name": "Salary",
            "amount": f"{salary}.00",
            "direction": "credit",
            "kind": "income",
            "account_id": salary_ac["id"],
            "category_id": cats["Salary"],
            "payee": "Acme Tech",
            "rrule": "FREQ=MONTHLY;BYMONTHDAY=-1",
            "start_date": back3.isoformat(),
            "auto_post": True,
        },
    )

    # Loans: already being repaid → outstanding today + EMIs left.
    nxt5 = (first_of_month + relativedelta(months=1 if today.day >= 5 else 0)).replace(day=5)
    await call(
        "POST",
        "/loans",
        {
            "name": "Home loan",
            "kind": "home",
            "lender": "SBI",
            "principal": "4180000.00",
            "original_principal": "4800000.00",
            "rate_pct": "8.5",
            "rate_type": "floating",
            "tenure_months": 204,
            "first_emi_date": nxt5.isoformat(),
            "start_date": (nxt5 - relativedelta(months=36)).isoformat(),
            "original_tenure_months": 240,
            "funding_account_id": salary_ac["id"],
            "tax_80c": True,
            "tax_24b": True,
        },
    )
    await call(
        "POST",
        "/loans",
        {
            "name": "Personal loan",
            "kind": "personal",
            "lender": "ICICI Bank",
            "principal": "385000.00",
            "original_principal": "600000.00",
            "rate_pct": "12.25",
            "tenure_months": 26,
            "first_emi_date": (nxt5 + timedelta(days=5)).isoformat(),
            "start_date": (nxt5 + timedelta(days=5) - relativedelta(months=34)).isoformat(),
            "original_tenure_months": 60,
            "funding_account_id": salary_ac["id"],
        },
    )
    await call(
        "POST",
        "/loans",
        {
            "name": "Car loan",
            "kind": "car",
            "lender": "Axis Bank",
            "principal": "520000.00",
            "original_principal": "800000.00",
            "rate_pct": "9.1",
            "tenure_months": 38,
            "first_emi_date": (nxt5 + timedelta(days=2)).isoformat(),
            "start_date": (nxt5 + timedelta(days=2) - relativedelta(months=22)).isoformat(),
            "original_tenure_months": 60,
            "funding_account_id": salary_ac["id"],
        },
    )

    regalia = await call(
        "POST",
        "/cards",
        {
            "name": "HDFC Regalia",
            "issuer": "HDFC Bank",
            "network": "Visa",
            "last4": "7781",
            "credit_limit": "400000.00",
            "statement_day": 12,
            "due_days": 20,
            "pay_from_account_id": salary_ac["id"],
            "opening_owed": "38450.00",
            "opening_date": back3.isoformat(),
        },
    )
    simply = await call(
        "POST",
        "/cards",
        {
            "name": "SBI SimplyClick",
            "issuer": "SBI Card",
            "network": "Mastercard",
            "last4": "3304",
            "credit_limit": "150000.00",
            "statement_day": 20,
            "due_days": 20,
            "pay_from_account_id": salary_ac["id"],
            "opening_owed": "21900.00",
            "opening_date": back3.isoformat(),
        },
    )
    await call(
        "POST",
        f"/cards/{regalia['id']}/emis",
        {
            "kind": "emi_conversion",
            "description": "Phone",
            "principal": "72000.00",
            "rate_pct": "15",
            "tenure_months": 12,
            "first_emi_date": (first_of_month - relativedelta(months=7))
            .replace(day=12)
            .isoformat(),
            "processing_fee": "199",
        },
    )
    await call(
        "POST",
        f"/cards/{simply['id']}/emis",
        {
            "kind": "loan_on_card",
            "description": "Loan on card",
            "principal": "100000.00",
            "rate_pct": "14",
            "tenure_months": 24,
            "first_emi_date": (first_of_month - relativedelta(months=10))
            .replace(day=20)
            .isoformat(),
            "processing_fee": "999",
            "disbursed_to_account_id": savings["id"],
        },
    )
    last_stmt = (
        today.replace(day=12)
        if today.day >= 12
        else (today.replace(day=1) - timedelta(days=1)).replace(day=12)
    )
    await call(
        "POST",
        f"/cards/{regalia['id']}/statements",
        {"statement_date": last_stmt.isoformat(), "total_due": "46320.00"},
    )

    # School fees: four terms a year.
    await call(
        "POST",
        "/recurring-rules",
        {
            "name": "School fees",
            "amount": "48500.00",
            "direction": "debit",
            "kind": "expense",
            "account_id": salary_ac["id"],
            "category_id": cats["School fees"],
            "payee": "Sunrise School",
            "rrule": "FREQ=YEARLY;BYMONTH=1,4,7,10;BYMONTHDAY=10",
            "start_date": back3.isoformat(),
            "remind_days_before": 7,
        },
    )
    for name, amt, cat, dom, est in (
        ("Society maintenance", "4500.00", "Maintenance", 5, False),
        ("Domestic help", "7000.00", "Domestic help", 1, False),
        ("Electricity", "3200.00", "Electricity", 18, True),
        ("Broadband", "999.00", "Broadband", 8, False),
        ("Family mobile plan", "1199.00", "Mobile", 14, False),
        ("Netflix", "649.00", "DTH / OTT", 22, False),
    ):
        await call(
            "POST",
            "/recurring-rules",
            {
                "name": name,
                "amount": amt,
                "direction": "debit",
                "kind": "expense",
                "account_id": salary_ac["id"],
                "category_id": cats[cat],
                "payee": name,
                "rrule": monthly(dom),
                "start_date": back3.isoformat(),
                "amount_is_estimate": est,
            },
        )
    await call(
        "POST",
        "/recurring-rules",
        {
            "name": "Index fund SIP",
            "amount": "15000.00",
            "direction": "debit",
            "kind": "transfer",
            "account_id": salary_ac["id"],
            "to_account_id": savings["id"],
            "category_id": cats["SIP / mutual funds"],
            "payee": "SIP",
            "rrule": monthly(7),
            "start_date": back3.isoformat(),
            "auto_post": True,
        },
    )
    await call(
        "POST",
        "/recurring-rules",
        {
            "name": "Health insurance",
            "amount": "28400.00",
            "direction": "debit",
            "kind": "expense",
            "account_id": salary_ac["id"],
            "category_id": cats["Health insurance"],
            "payee": "Star Health",
            "rrule": f"FREQ=YEARLY;BYMONTH={((today.month + 2) % 12) + 1};BYMONTHDAY=15",
            "start_date": back3.isoformat(),
        },
    )

    # A year of everyday spending, mostly on cards.
    rng = random.Random(7)
    spends = [
        ("Groceries", ["BigBasket", "DMart", "Nilgiris"], (900, 4200), 9, regalia),
        ("Milk & daily needs", ["Milk basket", "Local store"], (120, 600), 14, None),
        ("Fuel", ["Indian Oil", "HP Petrol"], (1500, 3200), 4, simply),
        ("Dining out", ["Swiggy", "Zomato", "Cafe Coffee Day"], (300, 1800), 6, simply),
        ("Shopping", ["Amazon", "Myntra"], (499, 3999), 3, regalia),
        ("Medicines", ["Apollo Pharmacy"], (200, 1400), 2, None),
        ("Cab & auto", ["Uber", "Rapido"], (90, 450), 5, None),
    ]
    cards = [regalia, simply]
    loans = await call("GET", "/loans")
    # History before the rules start materialising: (day of month, name, amount, category, credit)
    history: list[tuple[int, str, float, str, bool]] = [
        (1, "Domestic help", 7000, "Domestic help", False),
        (5, "Society maintenance", 4500, "Maintenance", False),
        (14, "Family mobile plan", 1199, "Mobile", False),
        (8, "Broadband", 999, "Broadband", False),
        (22, "Netflix", 649, "DTH / OTT", False),
    ] + [
        (
            int(ln["first_emi_date"][8:10]),
            f"{ln['name']} EMI",
            float(ln["emi"]),
            {"home": "Home loan EMI", "car": "Car loan EMI"}.get(ln["kind"], "Personal loan EMI"),
            False,
        )
        for ln in loans
    ]
    # Electricity follows the seasons (₹, by month).
    power = {
        1: 2200,
        2: 2100,
        3: 2600,
        4: 3400,
        5: 4300,
        6: 4100,
        7: 3300,
        8: 3100,
        9: 3000,
        10: 2700,
        11: 2300,
        12: 2200,
    }
    one_offs = {  # (month, day): (label, amount, category, account)
        (3, 28): ("Annual bonus", 150000, "Bonus", "credit"),
        (5, 18): ("Goa trip", 38500, "Travel", "regalia"),
        (10, 24): ("Diwali gifts", 12500, "Gifts", "regalia"),
        (11, 1): ("Diwali sweets & lights", 6800, "Festivals", "salary"),
        (8, 15): ("Car service", 8900, "Car service", "simply"),
        (1, 14): ("Pongal", 4200, "Festivals", "salary"),
        (6, 12): ("School books & uniform", 9600, "Books & uniform", "salary"),
    }
    day = back3
    while day < today:
        if day.month in (1, 4, 7, 10) and day.day == 10:
            await call(
                "POST",
                "/transactions",
                {
                    "account_id": salary_ac["id"],
                    "date": day.isoformat(),
                    "amount": "48500.00",
                    "direction": "debit",
                    "category_id": cats["School fees"],
                    "payee": "Sunrise School",
                    "match_scheduled": True,
                },
            )
        if day.day == 7:
            await call(
                "POST",
                "/transactions",
                {
                    "account_id": salary_ac["id"],
                    "to_account_id": savings["id"],
                    "date": day.isoformat(),
                    "amount": "15000.00",
                    "direction": "debit",
                    "kind": "transfer",
                    "category_id": cats["SIP / mutual funds"],
                    "payee": "SIP",
                    "match_scheduled": True,
                },
            )
        if day.day == 18:
            await call(
                "POST",
                "/transactions",
                {
                    "account_id": salary_ac["id"],
                    "date": day.isoformat(),
                    "amount": f"{power[day.month] + rng.randint(-150, 150)}.00",
                    "direction": "debit",
                    "category_id": cats["Electricity"],
                    "payee": "Electricity",
                    "match_scheduled": True,
                },
            )
        if (day.month, day.day) in one_offs:
            label, amount, cat_name, where = one_offs[(day.month, day.day)]
            target = {
                "salary": salary_ac["id"],
                "regalia": regalia["account_id"],
                "simply": simply["account_id"],
            }[where if where != "credit" else "salary"]
            await call(
                "POST",
                "/transactions",
                {
                    "account_id": savings["id"] if where == "credit" else target,
                    "date": day.isoformat(),
                    "amount": f"{amount}.00",
                    "direction": "credit" if where == "credit" else "debit",
                    "category_id": cats[cat_name],
                    "payee": label,
                    "match_scheduled": False,
                },
            )
        if (day + timedelta(days=1)).month != day.month:
            await call(
                "POST",
                "/transactions",
                {
                    "account_id": salary_ac["id"],
                    "date": day.isoformat(),
                    "amount": f"{salary}.00",
                    "direction": "credit",
                    "category_id": cats["Salary"],
                    "payee": "Acme Tech",
                    "match_scheduled": False,
                },
            )
        for dom, label, amt_, cat_, credit in history:
            if day.day == dom:
                await call(
                    "POST",
                    "/transactions",
                    {
                        "account_id": salary_ac["id"],
                        "date": day.isoformat(),
                        "amount": f"{amt_:.2f}",
                        "direction": "credit" if credit else "debit",
                        "kind": "emi" if "EMI" in label else None,
                        "category_id": cats[cat_],
                        "payee": label,
                        "match_scheduled": True,
                    },
                )
        if day.day == 2 and day > back3:
            # Pay each card's bill in full (revolving balance + this month's EMIs).
            for card in cards:
                now = await call("GET", f"/cards/{card['id']}")
                owed = float(now["owed"])
                pay = min(owed, owed - float(now["emi_outstanding"]) + float(now["emi_monthly"]))
                if pay > 0:
                    await call(
                        "POST",
                        "/transactions",
                        {
                            "account_id": salary_ac["id"],
                            "to_account_id": card["account_id"],
                            "date": day.isoformat(),
                            "amount": f"{pay:.2f}",
                            "direction": "debit",
                            "kind": "card_payment",
                            "category_id": cats["Card bill"],
                            "payee": f"{card['name']} bill",
                        },
                    )
        for cat, payees, (lo, hi), per_month, card in spends:
            if rng.random() < per_month / 30:
                await call(
                    "POST",
                    "/transactions",
                    {
                        "account_id": (card or salary_ac)["account_id" if card else "id"],
                        "date": day.isoformat(),
                        "amount": f"{rng.randint(lo, hi)}.00",
                        "direction": "debit",
                        "category_id": cats[cat],
                        "payee": rng.choice(payees),
                        "match_scheduled": False,
                    },
                )
        day += timedelta(days=1)
    result: dict[str, Any] = session
    return result
