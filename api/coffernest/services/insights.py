"""Deterministic insight engine. Every number the app (and the AI layer) shows comes from here.

Money-flow classification (avoids double counting):
- *income*: credits of kind income/refund/interest-earned (transfers excluded).
- *spend*: debits of kind expense/fee/interest on any account — a card swipe is spend on the day.
- *debt*: EMI and prepayment debits from bank accounts.
- Card bill payments and other transfers are neither: the money already counted as spend.
"""

from __future__ import annotations

import itertools
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, cast

from dateutil.relativedelta import relativedelta
from sqlalchemy import ColumnElement, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coffernest.enums import AccountKind, Direction, StatementStatus, TxnKind, TxnStatus
from coffernest.models import (
    Account,
    CardEmi,
    CardStatement,
    Category,
    CreditCard,
    Household,
    Loan,
    RecurringRule,
    Transaction,
)
from coffernest.services import debts
from coffernest.services.amortization import ZERO, q2
from coffernest.services.balances import balances
from coffernest.services.recurring import next_occurrence, occurrences

SPEND_KINDS = (TxnKind.expense, TxnKind.fee, TxnKind.interest)
DEBT_KINDS = (TxnKind.emi, TxnKind.prepayment)
INCOME_KINDS = (TxnKind.income, TxnKind.refund)
LIQUID_KINDS = (AccountKind.bank, AccountKind.cash, AccountKind.wallet)


def month_bounds(day: date) -> tuple[date, date]:
    first = day.replace(day=1)
    return first, first + relativedelta(months=1) - timedelta(days=1)


def _live(household_id: uuid.UUID) -> list[ColumnElement[bool]]:
    return [Transaction.household_id == household_id, Transaction.deleted_at.is_(None)]


# ---------- flows ----------


@dataclass
class Flow:
    income: Decimal = ZERO
    spend: Decimal = ZERO
    debt: Decimal = ZERO

    @property
    def net(self) -> Decimal:
        return self.income - self.spend - self.debt


async def flow(
    session: AsyncSession, household_id: uuid.UUID, start: date, end: date, scheduled: bool
) -> Flow:
    """Income / spend / debt between two dates. ``scheduled`` adds not-yet-confirmed items."""
    bucket = case(
        (
            (Transaction.direction == Direction.credit) & Transaction.kind.in_(INCOME_KINDS),
            "income",
        ),
        ((Transaction.direction == Direction.debit) & Transaction.kind.in_(SPEND_KINDS), "spend"),
        ((Transaction.direction == Direction.debit) & Transaction.kind.in_(DEBT_KINDS), "debt"),
        else_="other",
    )
    query = (
        select(bucket, func.sum(Transaction.amount))
        .where(*_live(household_id), Transaction.date.between(start, end))
        .group_by(bucket)
    )
    if not scheduled:
        query = query.where(Transaction.status != TxnStatus.scheduled)
    out = Flow()
    for name, total in (await session.execute(query)).all():
        if name in ("income", "spend", "debt"):
            setattr(out, name, Decimal(total or 0))
    return out


async def cashflow_months(
    session: AsyncSession, household_id: uuid.UUID, today: date, months: int
) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for back in range(months - 1, -1, -1):
        start, end = month_bounds(today - relativedelta(months=back))
        f = await flow(session, household_id, start, end, scheduled=back == 0)
        out.append(
            {"month": start, "income": f.income, "spend": f.spend, "debt": f.debt, "net": f.net}
        )
    return out


async def category_breakdown(
    session: AsyncSession, household_id: uuid.UUID, month: date
) -> list[dict[str, object]]:
    """Spend per top-level category this month vs last month and the 3-month average."""
    parent = func.coalesce(Category.parent_id, Category.id)
    start, end = month_bounds(month)
    prev_start = start - relativedelta(months=1)
    avg_start = start - relativedelta(months=3)

    def window(a: date, b: date) -> Any:
        return func.sum(case((Transaction.date.between(a, b), Transaction.amount), else_=0))

    query = (
        select(
            parent.label("cat"),
            window(start, end).label("this"),
            window(prev_start, start - timedelta(days=1)).label("prev"),
            window(avg_start, start - timedelta(days=1)).label("prior3"),
        )
        .select_from(Transaction)
        .outerjoin(Category, Category.id == Transaction.category_id)
        .where(
            *_live(household_id),
            Transaction.direction == Direction.debit,
            Transaction.kind.in_((*SPEND_KINDS, *DEBT_KINDS)),
            Transaction.status != TxnStatus.scheduled,
            Transaction.date.between(avg_start, end),
        )
        .group_by(parent)
    )
    names = {
        c.id: c
        for c in (
            await session.scalars(select(Category).where(Category.household_id == household_id))
        ).all()
    }
    rows = []
    for cat, this, prev, prior3 in (await session.execute(query)).all():
        category = names.get(cat) if cat else None
        rows.append(
            {
                "category_id": cat,
                "name": category.name if category else "Uncategorised",
                "color_slot": category.color_slot if category else 8,
                "is_fixed": category.is_fixed if category else False,
                "amount": Decimal(this or 0),
                "previous": Decimal(prev or 0),
                "avg_3m": q2(Decimal(prior3 or 0) / 3),
            }
        )
    rows.sort(key=lambda r: r["amount"], reverse=True)
    return [r for r in rows if r["amount"] or r["previous"]]


# ---------- recurring income / commitments ----------


def monthly_equivalent(rule: RecurringRule, today: date) -> Decimal:
    """Average monthly amount over the next 12 months (school fees termly → per month)."""
    hits = occurrences(
        rule.rrule,
        rule.start_date,
        today,
        today + relativedelta(years=1) - timedelta(days=1),
        rule.end_date,
        rule.skipped_dates,
    )
    return q2(rule.amount * len(hits) / 12)


async def monthly_income(session: AsyncSession, household_id: uuid.UUID, today: date) -> Decimal:
    rules = (
        await session.scalars(
            select(RecurringRule).where(
                RecurringRule.household_id == household_id,
                RecurringRule.active.is_(True),
                RecurringRule.direction == Direction.credit,
                RecurringRule.kind == TxnKind.income,
            )
        )
    ).all()
    from_rules = sum((monthly_equivalent(r, today) for r in rules), ZERO)
    if from_rules > 0:
        return from_rules
    # No income rules yet: fall back to the average of the last 3 full months.
    start = month_bounds(today - relativedelta(months=3))[0]
    end = month_bounds(today - relativedelta(months=1))[1]
    f = await flow(session, household_id, start, end, scheduled=False)
    return q2(f.income / 3)


async def next_payday(session: AsyncSession, household_id: uuid.UUID, today: date) -> date | None:
    rules = (
        await session.scalars(
            select(RecurringRule).where(
                RecurringRule.household_id == household_id,
                RecurringRule.active.is_(True),
                RecurringRule.direction == Direction.credit,
                RecurringRule.kind == TxnKind.income,
            )
        )
    ).all()
    if not rules:
        return None
    biggest = max(rules, key=lambda r: monthly_equivalent(r, today))
    return next_occurrence(biggest, today)


# ---------- safe to spend ----------


@dataclass
class SafeToSpend:
    amount: Decimal
    per_day: Decimal
    until: date
    days_left: int
    liquid: Decimal
    committed: Decimal
    card_dues: Decimal
    buffer: Decimal
    lines: list[dict[str, object]] = field(default_factory=list)


async def safe_to_spend(session: AsyncSession, household_id: uuid.UUID, today: date) -> SafeToSpend:
    household = await session.get(Household, household_id)
    buffer = Decimal(household.spend_buffer if household else 0)
    payday = await next_payday(session, household_id, today) or month_bounds(today)[1]
    accounts = (
        await session.scalars(select(Account).where(Account.household_id == household_id))
    ).all()
    liquid_ids = [
        a.id
        for a in accounts
        if a.kind in LIQUID_KINDS and a.is_active and a.include_in_safe_to_spend
    ]
    bals = await balances(session, household_id)
    liquid = sum((bals.get(i, ZERO) for i in liquid_ids), ZERO)

    lines: list[dict[str, object]] = []
    committed = ZERO
    sched = (
        await session.scalars(
            select(Transaction)
            .where(
                *_live(household_id),
                Transaction.status == TxnStatus.scheduled,
                Transaction.account_id.in_(liquid_ids),
                Transaction.direction == Direction.debit,
                Transaction.date <= payday,
            )
            .order_by(Transaction.date)
        )
    ).all()
    paid_cards: set[uuid.UUID] = set()
    for txn in sched:  # includes overdue unconfirmed items: they still have to be paid
        committed += txn.amount
        lines.append({"date": txn.date, "label": txn.payee, "amount": txn.amount})
        if txn.transfer_group_id is not None:
            paid_cards.update(
                (
                    await session.scalars(
                        select(Transaction.account_id).where(
                            Transaction.transfer_group_id == txn.transfer_group_id,
                            Transaction.id != txn.id,
                        )
                    )
                ).all()
            )

    card_dues = ZERO
    cards = await debts.cards_by_account(session, household_id)
    for account_id, card in cards.items():
        if account_id in paid_cards:
            continue
        stmt = await session.scalar(
            select(CardStatement)
            .where(
                CardStatement.card_id == card.id,
                CardStatement.status != StatementStatus.paid,
                CardStatement.due_date <= payday,
            )
            .order_by(CardStatement.statement_date.desc())
            .limit(1)
        )
        if stmt is not None:
            left = max(ZERO, stmt.total_due - stmt.paid_amount)
            if left > 0:
                card_dues += left
                name = next((a.name for a in accounts if a.id == account_id), card.issuer)
                lines.append({"date": stmt.due_date, "label": f"{name} bill", "amount": left})

    amount = liquid - committed - card_dues - buffer
    days_left = max(1, (payday - today).days)
    return SafeToSpend(
        amount=amount,
        per_day=q2(amount / days_left) if amount > 0 else ZERO,
        until=payday,
        days_left=days_left,
        liquid=liquid,
        committed=committed,
        card_dues=card_dues,
        buffer=buffer,
        lines=sorted(lines, key=lambda line: cast(date, line["date"])),
    )


# ---------- upcoming dues ----------


async def dues(
    session: AsyncSession, household_id: uuid.UUID, today: date, days: int
) -> list[dict[str, object]]:
    """Everything expected in the next ``days`` (plus anything overdue), oldest first."""
    horizon = today + timedelta(days=days)
    accounts = {
        a.id: a
        for a in (
            await session.scalars(select(Account).where(Account.household_id == household_id))
        ).all()
    }
    rule_names = dict(
        (
            await session.execute(
                select(RecurringRule.id, RecurringRule.name).where(
                    RecurringRule.household_id == household_id
                )
            )
        ).all()
    )
    items: list[dict[str, object]] = []
    sched = (
        await session.scalars(
            select(Transaction)
            .where(
                *_live(household_id),
                Transaction.status == TxnStatus.scheduled,
                Transaction.date <= horizon,
            )
            .order_by(Transaction.date)
        )
    ).all()
    seen_groups: set[uuid.UUID] = set()
    for txn in sched:
        if txn.transfer_group_id is not None:
            # Show a transfer once, from the paying side.
            if txn.transfer_group_id in seen_groups or txn.direction is Direction.credit:
                continue
            seen_groups.add(txn.transfer_group_id)
        account = accounts.get(txn.account_id)
        items.append(
            {
                "date": txn.date,
                "title": rule_names.get(txn.recurring_rule_id, txn.payee)
                if txn.recurring_rule_id
                else txn.payee,
                "amount": txn.amount,
                "direction": txn.direction,
                "kind": txn.kind,
                "source": "recurring",
                "transaction_id": txn.id,
                "statement_id": None,
                "card_id": None,
                "account_name": account.name if account else "",
                "overdue": txn.date < today,
            }
        )
    cards = await debts.cards_by_account(session, household_id)
    for account_id, card in cards.items():
        stmts = (
            await session.scalars(
                select(CardStatement).where(
                    CardStatement.card_id == card.id,
                    CardStatement.status != StatementStatus.paid,
                    CardStatement.due_date <= horizon,
                )
            )
        ).all()
        for stmt in stmts:
            left = max(ZERO, stmt.total_due - stmt.paid_amount)
            if left <= 0:
                continue
            account = accounts.get(account_id)
            items.append(
                {
                    "date": stmt.due_date,
                    "title": f"{account.name if account else card.issuer} bill",
                    "amount": left,
                    "min_due": max(ZERO, stmt.min_due - stmt.paid_amount),
                    "direction": Direction.debit,
                    "kind": TxnKind.card_payment,
                    "source": "card_statement",
                    "transaction_id": None,
                    "statement_id": stmt.id,
                    "card_id": card.id,
                    "account_name": account.name if account else "",
                    "overdue": stmt.due_date < today,
                }
            )
    items.sort(key=lambda i: (i["date"], i["title"]))
    return items


# ---------- debt load ----------


@dataclass
class DebtLoad:
    total_outstanding: Decimal
    loans_outstanding: Decimal
    cards_outstanding: Decimal
    monthly_emi: Decimal
    monthly_income: Decimal
    foir_pct: Decimal | None
    foir_band: str
    dti_ratio: Decimal | None
    card_limit: Decimal
    utilization_pct: Decimal
    loans: list[dict[str, object]]
    cards: list[dict[str, object]]


def foir_band(pct: Decimal | None) -> str:
    if pct is None:
        return "unknown"
    if pct < 40:
        return "healthy"
    if pct < 50:
        return "stretched"
    if pct < 60:
        return "high"
    return "critical"


async def debt_load(session: AsyncSession, household_id: uuid.UUID, today: date) -> DebtLoad:
    accounts = {
        a.id: a
        for a in (
            await session.scalars(select(Account).where(Account.household_id == household_id))
        ).all()
    }
    loans_out: list[dict[str, object]] = []
    loans_total = monthly_emi = ZERO
    for loan in (
        await session.scalars(
            select(Loan).where(Loan.household_id == household_id, Loan.closed.is_(False))
        )
    ).all():
        st = debts.loan_status(loan, await debts.loan_schedule(session, loan), today)
        loans_total += st.outstanding
        monthly_emi += st.emi
        account = accounts.get(loan.account_id)
        loans_out.append(
            {
                "id": loan.id,
                "name": account.name if account else loan.lender,
                "kind": loan.kind,
                "outstanding": st.outstanding,
                "emi": st.emi,
                "rate_pct": st.rate_pct,
                "end_date": st.end_date,
                "principal_paid_pct": st.principal_paid_pct,
            }
        )
    bals = await balances(session, household_id)
    cards_out: list[dict[str, object]] = []
    cards_total = limit_total = ZERO
    for card in (
        await session.scalars(select(CreditCard).where(CreditCard.household_id == household_id))
    ).all():
        account = accounts.get(card.account_id)
        if account is None or not account.is_active:
            continue
        owed = max(ZERO, bals.get(card.account_id, ZERO))
        emis = (
            await session.scalars(
                select(CardEmi).where(CardEmi.card_id == card.id, CardEmi.closed.is_(False))
            )
        ).all()
        card_emi = sum(
            (
                s.emi + s.gst_on_interest
                for s in (debts.card_emi_status(e, today) for e in emis)
                if s.emis_left
            ),
            ZERO,
        )
        monthly_emi += card_emi
        cards_total += owed
        limit_total += card.credit_limit
        cards_out.append(
            {
                "id": card.id,
                "name": account.name,
                "owed": owed,
                "limit": card.credit_limit,
                "utilization_pct": debts.utilization_pct(owed, card.credit_limit),
                "emi_monthly": card_emi,
            }
        )
    income = await monthly_income(session, household_id, today)
    foir = q2(monthly_emi / income * 100) if income > 0 else None
    total = loans_total + cards_total
    return DebtLoad(
        total_outstanding=total,
        loans_outstanding=loans_total,
        cards_outstanding=cards_total,
        monthly_emi=monthly_emi,
        monthly_income=income,
        foir_pct=foir,
        foir_band=foir_band(foir),
        dti_ratio=q2(total / (income * 12)) if income > 0 else None,
        card_limit=limit_total,
        utilization_pct=debts.utilization_pct(cards_total, limit_total),
        loans=loans_out,
        cards=cards_out,
    )


# ---------- emergency fund ----------


async def emergency_months(
    session: AsyncSession, household_id: uuid.UUID, today: date, liquid: Decimal
) -> Decimal | None:
    start = month_bounds(today - relativedelta(months=3))[0]
    end = month_bounds(today - relativedelta(months=1))[1]
    f = await flow(session, household_id, start, end, scheduled=False)
    monthly_out = (f.spend + f.debt) / 3
    if monthly_out <= 0:
        return None
    return q2(liquid / monthly_out)


# ---------- tax (India, old regime deductions) ----------

CAP_80C = Decimal("150000")
CAP_24B = Decimal("200000")


def fy_bounds(today: date, fy_start_month: int = 4) -> tuple[date, date]:
    year = today.year if today.month >= fy_start_month else today.year - 1
    start = date(year, fy_start_month, 1)
    return start, start + relativedelta(years=1) - timedelta(days=1)


async def tax_summary(
    session: AsyncSession, household_id: uuid.UUID, today: date
) -> dict[str, object]:
    household = await session.get(Household, household_id)
    start, end = fy_bounds(today, household.fy_start_month if household else 4)
    principal_80c = interest_24b = interest_80e = ZERO
    per_loan = []
    for loan in (
        await session.scalars(select(Loan).where(Loan.household_id == household_id))
    ).all():
        _, sched = await debts.loan_full_schedule(session, loan)
        principal = sched.principal_between(start, end)
        interest = sched.interest_between(start, end)
        estimated = any(r.estimated and start <= r.due_date <= end for r in sched.rows)
        if loan.tax_80c:
            principal_80c += principal
        if loan.tax_24b:
            interest_24b += interest
        if loan.tax_80e:
            interest_80e += interest
        if loan.tax_80c or loan.tax_24b or loan.tax_80e:
            account = await session.get(Account, loan.account_id)
            per_loan.append(
                {
                    "loan_id": loan.id,
                    "name": account.name if account else loan.lender,
                    "principal": principal,
                    "interest": interest,
                    "estimated": estimated,
                }
            )
    return {
        "fy_start": start,
        "fy_end": end,
        "principal_80c": principal_80c,
        "principal_80c_claimable": min(principal_80c, CAP_80C),
        "interest_24b": interest_24b,
        "interest_24b_claimable": min(interest_24b, CAP_24B),
        "interest_80e": interest_80e,
        "loans": per_loan,
        "note": "Home-loan principal shares the ₹1.5 L 80C cap with PF, PPF, ELSS and insurance.",
    }


# ---------- anomalies & notes ----------


async def anomalies(
    session: AsyncSession, household_id: uuid.UUID, today: date
) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    year_ago = today - relativedelta(years=1)
    recent_from = today - timedelta(days=30)
    rows = (
        await session.execute(
            select(
                Transaction.category_id,
                Transaction.amount,
                Transaction.id,
                Transaction.date,
                Transaction.payee,
            ).where(
                *_live(household_id),
                Transaction.direction == Direction.debit,
                Transaction.kind.in_(SPEND_KINDS),
                Transaction.status != TxnStatus.scheduled,
                Transaction.date >= year_ago,
            )
        )
    ).all()
    by_cat: dict[uuid.UUID | None, list[Decimal]] = defaultdict(list)
    for cat, amount, *_ in rows:
        by_cat[cat].append(amount)
    for cat, amount, txn_id, day, payee in rows:
        values = sorted(by_cat[cat])
        if day < recent_from or len(values) < 8:
            continue
        p95 = values[int(len(values) * 0.95) - 1]
        median = values[len(values) // 2]
        if amount > p95 and amount > median * 3:
            out.append(
                {
                    "kind": "unusual_amount",
                    "transaction_id": txn_id,
                    "date": day,
                    "title": f"{payee or 'Spend'} is unusually large",
                    "amount": amount,
                    "typical": median,
                }
            )
    seen: dict[tuple[Decimal, str], tuple[uuid.UUID, date]] = {}
    for cat, amount, txn_id, day, payee in sorted(rows, key=lambda r: r[3]):
        del cat
        if day < recent_from or not payee:
            continue
        key = (amount, payee.lower())
        if key in seen and (day - seen[key][1]).days <= 3:
            out.append(
                {
                    "kind": "possible_duplicate",
                    "transaction_id": txn_id,
                    "date": day,
                    "title": f"Possible duplicate: {payee}",
                    "amount": amount,
                    "other_transaction_id": seen[key][0],
                }
            )
        seen[key] = (txn_id, day)
    overdue = (
        await session.scalars(
            select(Transaction).where(
                *_live(household_id),
                Transaction.status == TxnStatus.scheduled,
                Transaction.date < today,
                Transaction.direction == Direction.debit,
            )
        )
    ).all()
    for txn in overdue:
        if txn.transfer_group_id is None or txn.direction is Direction.debit:
            out.append(
                {
                    "kind": "unconfirmed",
                    "transaction_id": txn.id,
                    "date": txn.date,
                    "title": f"{txn.payee}: was it paid?",
                    "amount": txn.amount,
                }
            )
    return out


async def notes(
    session: AsyncSession, household_id: uuid.UUID, today: date
) -> list[dict[str, object]]:
    """Rule-based observations with evidence. The AI layer rewrites/extends these; it never
    invents the numbers."""
    out: list[dict[str, object]] = []
    load = await debt_load(session, household_id, today)
    for card in load.cards:
        util = card["utilization_pct"]
        assert isinstance(util, Decimal)
        if util >= 30:
            severity = "high" if util >= 70 else "medium"
            out.append(
                {
                    "kind": "card_utilization",
                    "severity": severity,
                    "title": f"{card['name']} is {util:.0f}% used",
                    "body": "Keeping each card under 30% of its limit at statement time helps your "
                    "credit score. Paying part of it before the statement date lowers the "
                    "reported figure.",
                    "evidence": {
                        "card_id": str(card["id"]),
                        "owed": str(card["owed"]),
                        "limit": str(card["limit"]),
                    },
                }
            )
    if load.foir_pct is not None and load.foir_pct >= 40:
        out.append(
            {
                "kind": "foir",
                "severity": "high" if load.foir_pct >= 50 else "medium",
                "title": f"EMIs take {load.foir_pct:.0f}% of take-home pay",
                "body": "Lenders get cautious above 40–50%. Clearing the smallest high-rate loan "
                "first frees cash flow fastest.",
                "evidence": {
                    "monthly_emi": str(load.monthly_emi),
                    "monthly_income": str(load.monthly_income),
                },
            }
        )
    if load.loans:
        costliest = max(load.loans, key=lambda r: cast(Decimal, r["rate_pct"]))
        out.append(
            {
                "kind": "prepay_target",
                "severity": "info",
                "title": f"Extra money? {costliest['name']} costs the most "
                f"at {costliest['rate_pct']:.2f}%",
                "body": "Prepaying the highest-rate loan saves the most interest per rupee. "
                "Try the simulator on that loan.",
                "evidence": {"loan_id": str(costliest["id"])},
            }
        )
    # Saving: last full month's left over as a share of what came in.
    last_start, last_end = month_bounds(today.replace(day=1) - timedelta(days=1))
    last = await flow(session, household_id, last_start, last_end, scheduled=False)
    if last.income > 0:
        rate = q2(last.net / last.income * 100)
        if rate < 20:
            out.append(
                {
                    "kind": "savings_rate_low",
                    "severity": "high" if rate < 0 else "medium",
                    "title": f"{last_start:%B} kept {max(rate, ZERO):.0f}% of what came in",
                    "body": "A CA's rule of thumb is to keep at least 20% after spending and "
                    "EMIs. The biggest categories on Insights show where it went.",
                    "evidence": {
                        "savings_rate_pct": str(rate),
                        "income": str(last.income),
                        "left_over": str(last.net),
                    },
                }
            )
    # Net worth falling three month-ends in a row.
    from coffernest.services import networth

    worth = await networth.net_worth(session, household_id, today, 4)
    nets = [cast(Decimal, p["net"]) for p in worth["series"]]
    if len(nets) == 4 and all(b < a for a, b in itertools.pairwise(nets)):
        out.append(
            {
                "kind": "networth_down",
                "severity": "medium",
                "title": "Net worth has fallen three months running",
                "body": "What you own is shrinking against what you owe. Check whether spending "
                "or new debt is behind it before it becomes a habit.",
                "evidence": {"change_1m": str(worth["change_1m"]), "net": str(worth["net"])},
            }
        )
    # Loans whose EMI is still mostly interest.
    for loan in (
        await session.scalars(
            select(Loan).where(Loan.household_id == household_id, Loan.closed.is_(False))
        )
    ).all():
        row = (await debts.loan_schedule(session, loan)).next_row(today)
        if row is None or row.emi <= 0:
            continue
        share = q2(row.interest / row.emi * 100)
        if share > 60:
            name = next((r["name"] for r in load.loans if r["id"] == loan.id), loan.lender)
            out.append(
                {
                    "kind": "interest_heavy_loan",
                    "severity": "info",
                    "title": f"{share:.0f}% of the {name} EMI is still interest",
                    "body": "Early in a loan most of the EMI is interest. Even a small prepayment "
                    "now cuts interest for every year that's left.",
                    "evidence": {
                        "loan_id": str(loan.id),
                        "interest_share_pct": str(share),
                        "interest": str(row.interest),
                        "emi": str(row.emi),
                    },
                }
            )
    # Sinking funds: lumpy bills (school fees, insurance) in the next 6 months.
    rules = (
        await session.scalars(
            select(RecurringRule).where(
                RecurringRule.household_id == household_id,
                RecurringRule.active.is_(True),
                RecurringRule.direction == Direction.debit,
            )
        )
    ).all()
    for rule in rules:
        if "FREQ=MONTHLY;" in rule.rrule + ";" and "INTERVAL" not in rule.rrule:
            continue
        if "FREQ=WEEKLY" in rule.rrule or "FREQ=DAILY" in rule.rrule:
            continue
        nxt = next_occurrence(rule, today - timedelta(days=1))
        if nxt is None or nxt > today + relativedelta(months=6) or nxt <= today + timedelta(days=7):
            continue
        months = max(1, (nxt.year - today.year) * 12 + nxt.month - today.month)
        out.append(
            {
                "kind": "sinking_fund",
                "severity": "info",
                "title": f"{rule.name}: ₹{rule.amount:,.0f} due {nxt:%d %b}",
                "body": f"Setting aside ₹{q2(rule.amount / months):,.0f} a month covers it without "
                "a squeeze.",
                "evidence": {
                    "rule_id": str(rule.id),
                    "due": nxt.isoformat(),
                    "per_month": str(q2(rule.amount / months)),
                },
            }
        )
    return out


# ---------- money in vs out ----------

TRANSFER_KINDS = (TxnKind.transfer, TxnKind.card_payment)


async def inout(session: AsyncSession, household_id: uuid.UUID, month: date) -> dict[str, object]:
    """One month's money in and out, per account and per kind (cleared entries only).

    Moves between your own accounts (transfers, card bill payments) are listed separately so a
    card payment isn't counted as money going out twice."""
    start, end = month_bounds(month)
    f = await flow(session, household_id, start, end, scheduled=False)
    live = [*_live(household_id), Transaction.status != TxnStatus.scheduled]
    live.append(Transaction.date.between(start, end))
    accounts = {
        a.id: a
        for a in (
            await session.scalars(select(Account).where(Account.household_id == household_id))
        ).all()
    }
    per_account: dict[uuid.UUID, dict[str, Decimal]] = defaultdict(
        lambda: {"in": ZERO, "out": ZERO}
    )
    rows = await session.execute(
        select(Transaction.account_id, Transaction.direction, func.sum(Transaction.amount))
        .where(*live)
        .group_by(Transaction.account_id, Transaction.direction)
    )
    for account_id, direction, total in rows.all():
        per_account[account_id]["in" if direction is Direction.credit else "out"] += Decimal(total)
    by_kind = [
        {"kind": kind, "direction": direction, "amount": Decimal(total)}
        for kind, direction, total in (
            await session.execute(
                select(Transaction.kind, Transaction.direction, func.sum(Transaction.amount))
                .where(*live)
                .group_by(Transaction.kind, Transaction.direction)
                .order_by(func.sum(Transaction.amount).desc())
            )
        ).all()
    ]
    transfers = await session.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            *live, Transaction.kind.in_(TRANSFER_KINDS), Transaction.direction == Direction.debit
        )
    )
    by_account = [
        {
            "account_id": aid,
            "name": accounts[aid].name if aid in accounts else "Account",
            "kind": accounts[aid].kind if aid in accounts else AccountKind.bank,
            "in": v["in"],
            "out": v["out"],
        }
        for aid, v in sorted(per_account.items(), key=lambda kv: -(kv[1]["in"] + kv[1]["out"]))
    ]
    return {
        "month": start,
        "income": f.income,
        "spend": f.spend,
        "debt": f.debt,
        "transfers": Decimal(transfers or 0),
        "net": f.net,
        "by_account": by_account,
        "by_kind": by_kind,
    }


async def card_history(
    session: AsyncSession, household_id: uuid.UUID, card: CreditCard, today: date, months: int = 12
) -> dict[str, object]:
    """A card month by month: spent, paid back, interest and fees, owed at month-end."""
    account = await session.get(Account, card.account_id)
    out: list[dict[str, object]] = []
    bucket = case(
        (
            (Transaction.direction == Direction.debit)
            & Transaction.kind.in_((TxnKind.fee, TxnKind.interest)),
            "interest_fees",
        ),
        (Transaction.direction == Direction.debit, "spend"),
        else_="payments",
    )
    for back in range(months - 1, -1, -1):
        start, end = month_bounds(today - relativedelta(months=back))
        if account is not None and account.opening_date and end < account.opening_date:
            continue
        sums = {
            name: Decimal(total or 0)
            for name, total in (
                await session.execute(
                    select(bucket, func.sum(Transaction.amount))
                    .where(
                        *_live(household_id),
                        Transaction.account_id == card.account_id,
                        Transaction.status != TxnStatus.scheduled,
                        Transaction.date.between(start, end),
                    )
                    .group_by(bucket)
                )
            ).all()
        }
        owed = (await balances(session, household_id, as_of=min(end, today))).get(
            card.account_id, ZERO
        )
        out.append(
            {
                "month": start,
                "spend": sums.get("spend", ZERO),
                "payments": sums.get("payments", ZERO),
                "interest_fees": sums.get("interest_fees", ZERO),
                "owed_end": max(ZERO, owed),
            }
        )
    fy_start, fy_end = fy_bounds(today)
    ledger_fy = await session.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            *_live(household_id),
            Transaction.account_id == card.account_id,
            Transaction.status != TxnStatus.scheduled,
            Transaction.direction == Direction.debit,
            Transaction.kind.in_((TxnKind.fee, TxnKind.interest)),
            Transaction.date.between(fy_start, fy_end),
        )
    )
    statements_fy = await session.scalar(
        select(
            func.coalesce(func.sum(CardStatement.interest_charged + CardStatement.fees), 0)
        ).where(
            CardStatement.card_id == card.id,
            CardStatement.statement_date.between(fy_start, fy_end),
        )
    )
    # Statements and ledger entries can describe the same charges: take the larger, never both.
    return {
        "months": out,
        "interest_fees_fy": max(Decimal(ledger_fy or 0), Decimal(statements_fy or 0)),
    }
