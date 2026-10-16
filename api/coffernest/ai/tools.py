"""Read-only tools the AI may call. Each returns facts computed by the deterministic engine;
the model never does money arithmetic itself. Provider-neutral: plain JSON Schema + handler.

Redaction: no e-mails, account numbers, last-4 digits or user ids leave through these tools.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coffernest.enums import AccountKind, PrepayMode
from coffernest.models import Account, CreditCard, Household, Loan
from coffernest.services import amortization as am
from coffernest.services import debts, insights, planner, skills
from coffernest.services.balances import balances

Handler = Callable[[AsyncSession, uuid.UUID, dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    handler: Handler


def _plain(value: Any) -> Any:
    """JSON-safe copy: Decimals → rounded floats, dates → ISO, enums → values."""
    if isinstance(value, Decimal):
        return float(round(value, 2))
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items() if k not in {"id", "card_id"}}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    if hasattr(value, "value") and isinstance(getattr(value, "value", None), str):
        return value.value
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return _plain(dict(value.__dict__))
    return value


def to_json(value: Any) -> str:
    return json.dumps(_plain(value), ensure_ascii=False)


async def _currency(session: AsyncSession, hid: uuid.UUID) -> str:
    household = await session.get(Household, hid)
    return household.currency if household else "INR"


async def _overview(session: AsyncSession, hid: uuid.UUID, _: dict[str, Any]) -> Any:
    today = date.today()
    start, end = insights.month_bounds(today)
    sts = await insights.safe_to_spend(session, hid, today)
    load = await insights.debt_load(session, hid, today)
    return {
        "today": today,
        "currency": await _currency(session, hid),
        "safe_to_spend": {k: v for k, v in sts.__dict__.items() if k != "lines"},
        "this_month_so_far": (await insights.flow(session, hid, start, today, False)).__dict__,
        "this_month_projected": (await insights.flow(session, hid, start, end, True)).__dict__,
        "debt": {k: v for k, v in load.__dict__.items() if k not in {"loans", "cards"}},
        "loans": load.loans,
        "cards": load.cards,
        "emergency_fund_months": await insights.emergency_months(session, hid, today, sts.liquid),
    }


async def _dues(session: AsyncSession, hid: uuid.UUID, args: dict[str, Any]) -> Any:
    days = max(1, min(int(args.get("days", 30)), 120))
    rows = await insights.dues(session, hid, date.today(), days)
    keep = ("date", "title", "amount", "min_due", "direction", "kind", "overdue", "account_name")
    return [{k: r.get(k) for k in keep} for r in rows]


async def _categories(session: AsyncSession, hid: uuid.UUID, args: dict[str, Any]) -> Any:
    month = date.fromisoformat(args["month"]) if args.get("month") else date.today()
    rows = await insights.category_breakdown(session, hid, month)
    return [{k: v for k, v in r.items() if k != "category_id"} for r in rows]


async def _spend(session: AsyncSession, hid: uuid.UUID, args: dict[str, Any]) -> Any:
    from coffernest.enums import Direction
    from coffernest.services import categorize
    from coffernest.services import spend as spend_svc

    book = await categorize.load_book(session, hid)
    category_id = person_id = None
    family = False
    if args.get("category"):
        category_id = book.path(str(args["category"]))
        if category_id is None:
            return {"error": f"No category called {args['category']!r}"}
    if args.get("person"):
        name = str(args["person"]).strip().lower()
        if name in ("family", "everyone", "household"):
            family = True
        else:
            person_id = book.people.get(name) or next(
                (pid for n, pid in book.people.items() if n.startswith(name)), None
            )
            if person_id is None:
                names = ", ".join(n.title() for n in book.people) or "(none yet)"
                return {"error": f"No one called {args['person']!r}. People: {names}"}
    end = date.fromisoformat(args["end"]) if args.get("end") else date.today()
    start = date.fromisoformat(args["start"]) if args.get("start") else None
    out = await spend_svc.spend(
        session,
        hid,
        start,
        end,
        spend_svc.Filters(
            category_id=category_id,
            person_id=person_id,
            family_only=family,
            q=str(args["text"]) if args.get("text") else None,
            direction=Direction.credit if args.get("money_in") else Direction.debit,
        ),
    )
    out.pop("series", None)
    for c in out["by_category"]:
        c.pop("id", None)
        c.pop("color_slot", None)
        for ch in c["children"]:
            ch.pop("id", None)
    for p in out["by_person"]:
        p.pop("id", None)
    return out


async def _cashflow(session: AsyncSession, hid: uuid.UUID, args: dict[str, Any]) -> Any:
    months = max(1, min(int(args.get("months", 6)), 24))
    return await insights.cashflow_months(session, hid, date.today(), months)


async def _find_loan(session: AsyncSession, hid: uuid.UUID, name: str) -> Loan | None:
    rows = (
        await session.execute(
            select(Loan, Account.name)
            .join(Account, Account.id == Loan.account_id)
            .where(Loan.household_id == hid, Loan.closed.is_(False))
        )
    ).all()
    wanted = name.strip().lower()
    for loan, acc_name in rows:
        if acc_name.lower() == wanted:
            return loan
    for loan, acc_name in rows:
        if wanted in acc_name.lower() or wanted in loan.kind.value:
            return loan
    return None


async def _simulate(session: AsyncSession, hid: uuid.UUID, args: dict[str, Any]) -> Any:
    loan = await _find_loan(session, hid, str(args.get("loan_name", "")))
    if loan is None:
        return {"error": "No open loan with that name. Call get_overview to see loan names."}
    terms = await debts.loan_terms(session, loan)
    today = date.today()
    base = am.build_schedule(terms)
    nxt = base.next_row(today)
    mode = PrepayMode(args.get("mode", "reduce_tenure"))
    whatif = am.with_extra(
        terms,
        lump_sum=Decimal(str(args.get("lump_sum", 0))),
        lump_date=today,
        extra_monthly=Decimal(str(args.get("extra_monthly", 0))),
        mode=mode,
        start=nxt.due_date if nxt else today,
    )
    cmp = am.compare(base, am.build_schedule(whatif))
    penalty = am.q2(Decimal(str(args.get("lump_sum", 0))) * loan.prepayment_penalty_pct / 100)
    saving = cmp.interest_saved - penalty
    return {**cmp.__dict__, "prepayment_penalty": penalty, "net_saving": saving}


async def _payoff(session: AsyncSession, hid: uuid.UUID, args: dict[str, Any]) -> Any:
    items = await debts.household_debts(session, hid, include_cards=True)
    extra = Decimal(str(args.get("extra_monthly", 0)))
    return {
        "baseline": am.plan_payoff(items, am.ZERO, "none"),
        "avalanche": am.plan_payoff(items, extra, "avalanche"),
        "snowball": am.plan_payoff(items, extra, "snowball"),
    }


async def _anomalies(session: AsyncSession, hid: uuid.UUID, _: dict[str, Any]) -> Any:
    rows = await insights.anomalies(session, hid, date.today())
    return [{k: v for k, v in r.items() if not k.endswith("transaction_id")} for r in rows][:20]


async def _tax(session: AsyncSession, hid: uuid.UUID, _: dict[str, Any]) -> Any:
    if await _currency(session, hid) != "INR":
        return {"note": "80C and 24(b) are Indian tax rules; this household doesn't use INR."}
    out = await insights.tax_summary(session, hid, date.today())
    out["loans"] = [{k: v for k, v in r.items() if k != "loan_id"} for r in out["loans"]]  # type: ignore[attr-defined]
    return out


async def _accounts(session: AsyncSession, hid: uuid.UUID, _: dict[str, Any]) -> Any:
    owed = await balances(session, hid)
    today = date.today()
    accounts = (
        await session.scalars(
            select(Account).where(Account.household_id == hid, Account.is_active.is_(True))
        )
    ).all()
    loans = []
    for loan in (
        await session.scalars(select(Loan).where(Loan.household_id == hid, Loan.closed.is_(False)))
    ).all():
        st = debts.loan_status(loan, await debts.loan_schedule(session, loan), today)
        acc = await session.get(Account, loan.account_id)
        loans.append(
            {
                "name": acc.name if acc else loan.kind.value,
                "kind": loan.kind,
                "lender": loan.lender,
                "outstanding": st.outstanding,
                "rate_pct": st.rate_pct,
                "rate_type": loan.rate_type,
                "emi": st.emi,
                "emis_left": st.emis_left,
                "ends": st.end_date,
            }
        )
    cards = []
    for card in (
        await session.scalars(select(CreditCard).where(CreditCard.household_id == hid))
    ).all():
        acc = await session.get(Account, card.account_id)
        owed_now = owed.get(card.account_id, am.ZERO)
        cards.append(
            {
                "name": acc.name if acc else card.issuer,
                "issuer": card.issuer,
                "limit": card.credit_limit,
                "owed": owed_now,
                "utilisation_pct": debts.utilization_pct(owed_now, card.credit_limit),
                "interest_pct_per_month": card.monthly_rate_pct,
            }
        )
    return {
        "accounts": [
            {
                "name": a.name,
                "kind": a.kind,
                "type": a.account_type,
                "institution": a.institution,
                "branch": a.branch,
                "balance": owed.get(a.id, am.ZERO),
            }
            for a in accounts
            if a.kind not in (AccountKind.loan, AccountKind.credit_card)
        ],
        "loans": loans,
        "cards": cards,
    }


async def _compare(session: AsyncSession, hid: uuid.UUID, args: dict[str, Any]) -> Any:
    items = await debts.household_debts(session, hid, include_cards=True)
    today = date.today()
    monthly = Decimal(str(args.get("monthly_amount", 0)))
    lump = Decimal(str(args.get("lump_sum", 0)))
    years = max(1, min(int(args.get("years", 10)), 30))
    sip = Decimal(str(args.get("sip_return_pct", 12)))
    paths = [str(p) for p in args.get("paths") or []] or [
        "avalanche",
        "clear_cards",
        "invest_sip",
        "split",
    ]
    # Emergency fund target: 6 months of recent spend + EMIs, less what's liquid today.
    sts = await insights.safe_to_spend(session, hid, today)
    cover = await insights.emergency_months(session, hid, today, sts.liquid)
    gap = am.ZERO
    if cover is not None and 0 < cover < 6:
        gap = am.q2(sts.liquid / cover * (6 - cover))
    currency = await _currency(session, hid)
    results = [
        planner.simulate_path(pid, items, monthly, lump, years, today, sip, gap, currency=currency)
        for pid in paths[:6]
        if pid in planner.PATH_IDS or pid.startswith("prepay:")
    ]
    return {
        "assumptions": {
            "monthly_amount": monthly,
            "lump_sum": lump,
            "years": years,
            "sip_return_pct": sip,
            "emergency_fund_gap": gap,
            "note": "Investment returns are assumptions, not guaranteed. Net = investments + "
            "emergency cash − debt left, counting only what these plans move.",
        },
        "debts": [
            {"name": d.name, "balance": d.balance, "rate_pct": d.rate_pct, "payment": d.payment}
            for d in items
        ],
        "paths": results,
        "best": planner.best_of(results),
    }


async def _goal(session: AsyncSession, hid: uuid.UUID, args: dict[str, Any]) -> Any:
    return planner.project_goal(
        target_today=Decimal(str(args["target_today"])),
        years=int(args["years"]),
        inflation_pct=Decimal(str(args.get("inflation_pct", 6))),
        return_pct=Decimal(str(args.get("return_pct", 12))),
        step_up_pct=Decimal(str(args.get("step_up_pct", 10))),
        current_saved=Decimal(str(args.get("current_saved", 0))),
    )


async def _load_skill(session: AsyncSession, hid: uuid.UUID, args: dict[str, Any]) -> Any:
    name = str(args.get("name", ""))
    skill = await skills.load(session, hid, name)
    if skill is None:
        return {"error": f"No enabled skill called {name!r}."}
    ref = args.get("reference")
    if ref and ref in skill.references:
        return {"name": skill.name, "reference": ref, "body": skill.references[ref]}
    return {
        "name": skill.name,
        "title": skill.title,
        "as_of": skill.as_of,
        "body": skill.body,
        "references": sorted(skill.references),
    }


_NONE: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}

TOOLS: list[Tool] = [
    Tool(
        "get_overview",
        "Today's position: safe-to-spend until payday, this month's income/spend/EMIs, debt "
        "load (FOIR, utilisation), every loan and card, emergency-fund months.",
        _NONE,
        _overview,
    ),
    Tool(
        "get_dues",
        "Bills, EMIs, card bills and income expected in the next N days, incl. anything overdue.",
        {
            "type": "object",
            "properties": {"days": {"type": "integer", "minimum": 1, "maximum": 120}},
            "additionalProperties": False,
        },
        _dues,
    ),
    Tool(
        "get_category_spend",
        "Spend per top-level category for a month vs last month and the 3-month average.",
        {
            "type": "object",
            "properties": {"month": {"type": "string", "description": "YYYY-MM-DD in the month"}},
            "additionalProperties": False,
        },
        _categories,
    ),
    Tool(
        "get_spend",
        "How much was spent (or received, money_in=true) over any period, against the same "
        "length of time just before: total, per category with sub-categories, per person "
        "(who it was for: a child, the spouse, Family), top places, and EMIs separately. "
        "Filter by category ('Kids', 'Groceries', 'Lifestyle / Food delivery'), person "
        "('Aarav') and payee/note text. start/end YYYY-MM-DD; no start = all time. Use it "
        "for 'how much did Aarav cost this month', 'groceries last month', 'Swiggy this year'.",
        {
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "YYYY-MM-DD; empty = all time"},
                "end": {"type": "string", "description": "YYYY-MM-DD; empty = today"},
                "category": {"type": "string"},
                "person": {"type": "string"},
                "text": {"type": "string"},
                "money_in": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        _spend,
    ),
    Tool(
        "get_cashflow",
        "Income, spend and EMI payments per month for the last N months.",
        {
            "type": "object",
            "properties": {"months": {"type": "integer", "minimum": 1, "maximum": 24}},
            "additionalProperties": False,
        },
        _cashflow,
    ),
    Tool(
        "simulate_prepayment",
        "Exact effect of a lump-sum and/or extra monthly prepayment on "
        "one loan: interest saved, new end date, new EMI.",
        {
            "type": "object",
            "properties": {
                "loan_name": {"type": "string"},
                "lump_sum": {"type": "number", "minimum": 0},
                "extra_monthly": {"type": "number", "minimum": 0},
                "mode": {"type": "string", "enum": ["reduce_tenure", "reduce_emi"]},
            },
            "required": ["loan_name"],
            "additionalProperties": False,
        },
        _simulate,
    ),
    Tool(
        "plan_payoff",
        "Compare EMIs-only vs avalanche vs snowball across all loans, card EMIs "
        "and card balances for a given extra monthly amount.",
        {
            "type": "object",
            "properties": {"extra_monthly": {"type": "number", "minimum": 0}},
            "required": ["extra_monthly"],
            "additionalProperties": False,
        },
        _payoff,
    ),
    Tool(
        "get_anomalies",
        "Unusually large spends, possible duplicates, and expected items not yet confirmed.",
        _NONE,
        _anomalies,
    ),
    Tool(
        "get_tax_summary",
        "This financial year's home-loan principal (80C) and interest "
        "(24(b)) from the loan schedules.",
        _NONE,
        _tax,
    ),
]

TOOLS += [
    Tool(
        "list_accounts",
        "Every bank/cash/investment account with its balance, every open loan (outstanding, "
        "rate, EMI, EMIs left, end date) and card (limit, owed, utilisation, monthly interest).",
        _NONE,
        _accounts,
    ),
    Tool(
        "compare_paths",
        "Simulate month by month what happens over N years if the household puts a monthly "
        "amount (and an optional lump sum today) towards different paths. Path ids: "
        "clear_cards, avalanche, snowball, invest_sip, split, emergency_first, or "
        "'prepay:<loan name>'. Returns per path: debt-free date, interest paid, investments "
        "built, net position by year and milestones, plus which path is best on each measure.",
        {
            "type": "object",
            "properties": {
                "monthly_amount": {"type": "number", "minimum": 0},
                "lump_sum": {"type": "number", "minimum": 0},
                "years": {"type": "integer", "minimum": 1, "maximum": 30},
                "paths": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
                "sip_return_pct": {"type": "number", "minimum": 0, "maximum": 20},
            },
            "required": ["monthly_amount"],
            "additionalProperties": False,
        },
        _compare,
    ),
    Tool(
        "project_goal",
        "For a goal costing target_today rupees in today's money, N years away: the inflated "
        "cost, what current savings grow to, and the monthly SIP needed flat or with a yearly "
        "step-up.",
        {
            "type": "object",
            "properties": {
                "target_today": {"type": "number", "minimum": 1},
                "years": {"type": "integer", "minimum": 1, "maximum": 40},
                "inflation_pct": {"type": "number", "minimum": 0, "maximum": 20},
                "return_pct": {"type": "number", "minimum": 0, "maximum": 20},
                "step_up_pct": {"type": "number", "minimum": 0, "maximum": 30},
                "current_saved": {"type": "number", "minimum": 0},
            },
            "required": ["target_today", "years"],
            "additionalProperties": False,
        },
        _goal,
    ),
    Tool(
        "load_skill",
        "Load a financial-planning playbook by name (from the skills list in your "
        "instructions) and follow it. Optionally a named reference file inside the skill.",
        {
            "type": "object",
            "properties": {"name": {"type": "string"}, "reference": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
        _load_skill,
    ),
]

BY_NAME = {t.name: t for t in TOOLS}
