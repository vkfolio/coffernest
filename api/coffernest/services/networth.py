"""Net worth: what you have minus what you owe, now and at past month-ends.

- Assets: bank, cash, wallet and investment balances (active accounts, cleared entries).
- Liabilities: loans (amortisation schedule) plus cards owed, exactly as ``insights.debt_load``.
  Card EMIs are already inside the card's ledger balance, so they aren't added again.
- History: balances as of each month-end, skipping accounts opened later; loans from their
  schedule with the estimated pre-entry history, so a month that depends on it is flagged.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from dateutil.relativedelta import relativedelta
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coffernest.enums import AccountKind
from coffernest.models import Account, CreditCard, Loan
from coffernest.services import debts
from coffernest.services.amortization import ZERO, Schedule, q2
from coffernest.services.balances import balances

ASSET_KINDS = (AccountKind.bank, AccountKind.cash, AccountKind.wallet, AccountKind.investment)


@dataclass
class Snapshot:
    assets: Decimal = ZERO
    liabilities: Decimal = ZERO
    breakdown: dict[str, Decimal] = field(default_factory=dict)
    estimated: bool = False

    @property
    def net(self) -> Decimal:
        return self.assets - self.liabilities


@dataclass
class _Books:
    accounts: list[Account]
    card_accounts: set[uuid.UUID]
    loans: list[tuple[Loan, Schedule]]  # schedule incl. estimated history


async def _books(session: AsyncSession, household_id: uuid.UUID) -> _Books:
    accounts = list(
        (
            await session.scalars(
                select(Account).where(
                    Account.household_id == household_id, Account.is_active.is_(True)
                )
            )
        ).all()
    )
    cards = (
        await session.scalars(select(CreditCard).where(CreditCard.household_id == household_id))
    ).all()
    loans: list[tuple[Loan, Schedule]] = []
    for loan in (
        await session.scalars(
            select(Loan).where(Loan.household_id == household_id, Loan.closed.is_(False))
        )
    ).all():
        _, full = await debts.loan_full_schedule(session, loan)
        loans.append((loan, full))
    return _Books(accounts, {c.account_id for c in cards}, loans)


async def _snapshot(
    session: AsyncSession, household_id: uuid.UUID, books: _Books, day: date, today: date
) -> Snapshot:
    bals = await balances(session, household_id, as_of=None if day >= today else day)
    out = Snapshot(
        breakdown={k: ZERO for k in ("bank", "cash", "wallet", "investment", "loans", "cards")}
    )
    for account in books.accounts:
        if day < today and account.opening_date and account.opening_date > day:
            continue
        if account.kind in ASSET_KINDS:
            value = bals.get(account.id, ZERO)
            out.assets += value
            out.breakdown[account.kind.value] += value
        elif account.kind is AccountKind.credit_card and account.id in books.card_accounts:
            owed = max(ZERO, bals.get(account.id, ZERO))
            out.liabilities += owed
            out.breakdown["cards"] += owed
    for loan, schedule in books.loans:
        # With a start date the loan didn't exist before it; without one, it was already owed
        # (at its balance on entry) for as far back as the books go.
        if loan.start_date and day < loan.start_date - relativedelta(months=1):
            continue
        owed = schedule.outstanding_on(day)
        out.liabilities += owed
        out.breakdown["loans"] += owed
        if day < loan.first_emi_date:
            out.estimated = True
    return out


def _earliest(books: _Books) -> date | None:
    days = [a.opening_date for a in books.accounts if a.opening_date]
    return min(days) if days else None


async def net_worth(
    session: AsyncSession, household_id: uuid.UUID, today: date, months: int
) -> dict[str, Any]:
    books = await _books(session, household_id)
    now = await _snapshot(session, household_id, books, today, today)
    earliest = _earliest(books)
    series: list[dict[str, Any]] = []
    for back in range(months - 1, 0, -1):
        month_start = (today - relativedelta(months=back)).replace(day=1)
        month_end = month_start + relativedelta(months=1) - timedelta(days=1)
        if earliest is not None and month_end < earliest:
            continue
        snap = await _snapshot(session, household_id, books, month_end, today)
        series.append(_point(month_start, snap))
    series.append(_point(today.replace(day=1), now))
    prev = series[-2] if len(series) >= 2 else None
    change = now.net - prev["net"] if prev else None
    change_pct = (
        q2(change / abs(prev["net"]) * 100) if prev and change is not None and prev["net"] else None
    )
    return {
        "assets": now.assets,
        "liabilities": now.liabilities,
        "net": now.net,
        "change_1m": change,
        "change_1m_pct": change_pct,
        "breakdown": now.breakdown,
        "series": series,
    }


def _point(month: date, snap: Snapshot) -> dict[str, Any]:
    return {
        "month": month,
        "assets": snap.assets,
        "liabilities": snap.liabilities,
        "net": snap.net,
        "estimated": snap.estimated,
    }


async def principal_repaid(
    session: AsyncSession, household_id: uuid.UUID, start: date, end: date
) -> Decimal:
    """Loan principal (incl. prepayments) repaid between two dates: money that left the bank but
    stayed yours, as less debt."""
    total = ZERO
    for loan in (
        await session.scalars(select(Loan).where(Loan.household_id == household_id))
    ).all():
        _, full = await debts.loan_full_schedule(session, loan)
        total += full.principal_between(start, end)
    return total
