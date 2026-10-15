"""Loans and credit cards: schedules, cycles, minimum due and utilisation."""

from __future__ import annotations

import calendar
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coffernest.models import Account, CardEmi, CreditCard, Loan, LoanPrepayment, LoanRateChange
from coffernest.services.amortization import (
    CENT,
    ZERO,
    Debt,
    History,
    LoanTerms,
    PrepayEvent,
    RateEvent,
    Schedule,
    build_schedule,
    emi,
    history_rows,
    q2,
    with_history,
)
from coffernest.services.balances import balances

# ---------- loans ----------


async def loan_terms(session: AsyncSession, loan: Loan) -> LoanTerms:
    rates = (
        await session.scalars(select(LoanRateChange).where(LoanRateChange.loan_id == loan.id))
    ).all()
    prepays = (
        await session.scalars(select(LoanPrepayment).where(LoanPrepayment.loan_id == loan.id))
    ).all()
    return LoanTerms(
        principal=loan.principal,
        rate_pct=loan.rate_pct,
        tenure_months=loan.tenure_months,
        first_emi_date=loan.first_emi_date,
        emi_override=loan.emi_override,
        rate_events=[RateEvent(r.effective_date, r.rate_pct, r.keep_emi) for r in rates],
        prepayments=[PrepayEvent(p.date, p.amount, p.mode) for p in prepays],
    )


async def loan_schedule(session: AsyncSession, loan: Loan) -> Schedule:
    return build_schedule(await loan_terms(session, loan))


def loan_history_terms(loan: Loan) -> History | None:
    if loan.start_date is None or loan.start_date >= loan.first_emi_date:
        return None
    return History(
        start_date=loan.start_date,
        original_principal=loan.original_principal,
        original_tenure_months=loan.original_tenure_months,
        original_emi=loan.original_emi,
    )


async def loan_full_schedule(session: AsyncSession, loan: Loan) -> tuple[Schedule, Schedule]:
    """(live schedule, schedule with the estimated history before entry prepended)."""
    terms = await loan_terms(session, loan)
    live = build_schedule(terms)
    history = loan_history_terms(loan)
    if history is None:
        return live, live
    return live, with_history(live, history_rows(terms, history))


@dataclass(frozen=True)
class LoanStatus:
    outstanding: Decimal
    emi: Decimal
    rate_pct: Decimal
    next_due: date | None
    end_date: date | None
    emis_left: int
    interest_left: Decimal
    principal_paid_pct: Decimal


def loan_status(loan: Loan, schedule: Schedule, today: date) -> LoanStatus:
    upcoming = [r for r in schedule.rows if r.due_date > today and r.emi > 0]
    nxt = upcoming[0] if upcoming else None
    outstanding = schedule.outstanding_on(today)
    original = loan.original_principal or loan.principal
    paid_pct = q2((original - outstanding) / original * 100) if original > 0 else Decimal("100")
    return LoanStatus(
        outstanding=outstanding,
        emi=nxt.emi if nxt else ZERO,
        rate_pct=nxt.rate_pct if nxt else loan.rate_pct,
        next_due=nxt.due_date if nxt else None,
        end_date=schedule.end_date,
        emis_left=len(upcoming),
        interest_left=sum((r.interest for r in upcoming), ZERO),
        principal_paid_pct=max(ZERO, min(Decimal("100"), paid_pct)),
    )


# ---------- card EMIs (EMI conversions, loan on card) ----------


def card_emi_terms(item: CardEmi) -> LoanTerms:
    return LoanTerms(
        principal=item.principal,
        rate_pct=item.rate_pct,
        tenure_months=item.tenure_months,
        first_emi_date=item.first_emi_date,
        # Card EMIs are computed to the paisa, not rounded to the rupee.
        emi_override=emi(item.principal, item.rate_pct, item.tenure_months, round_to=CENT),
    )


@dataclass(frozen=True)
class CardEmiStatus:
    emi: Decimal
    gst_on_interest: Decimal  # next instalment's GST (billed on the card on top of the EMI)
    outstanding: Decimal
    emis_left: int
    next_due: date | None
    end_date: date | None
    total_cost: Decimal  # interest + GST + processing fee (+ its GST) over the whole life


def card_emi_status(item: CardEmi, today: date) -> CardEmiStatus:
    schedule = build_schedule(card_emi_terms(item))
    gst = item.gst_pct / 100
    upcoming = [r for r in schedule.rows if r.due_date > today]
    nxt = upcoming[0] if upcoming else None
    interest_total = schedule.total_interest
    return CardEmiStatus(
        emi=nxt.emi if nxt else ZERO,
        gst_on_interest=q2(nxt.interest * gst) if nxt else ZERO,
        outstanding=schedule.outstanding_on(today),
        emis_left=len(upcoming),
        next_due=nxt.due_date if nxt else None,
        end_date=schedule.end_date,
        total_cost=q2(interest_total * (1 + gst) + item.processing_fee * (1 + gst)),
    )


# ---------- credit-card cycles ----------


def _on_day(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def last_statement_date(card: CreditCard, today: date) -> date:
    this_month = _on_day(today.year, today.month, card.statement_day)
    if this_month <= today:
        return this_month
    prev = today.replace(day=1) - timedelta(days=1)
    return _on_day(prev.year, prev.month, card.statement_day)


def next_statement_date(card: CreditCard, today: date) -> date:
    last = last_statement_date(card, today)
    nxt = last + relativedelta(months=1)
    return _on_day(nxt.year, nxt.month, card.statement_day)


def due_date_for(card: CreditCard, statement_date: date) -> date:
    return statement_date + timedelta(days=card.due_days)


def min_due(
    card: CreditCard,
    total_due: Decimal,
    emi_billed: Decimal = ZERO,
    fees_and_interest: Decimal = ZERO,
) -> Decimal:
    """Typical Indian MAD: EMIs + fees + interest in full, plus ``min_due_pct`` of the rest,
    never below the floor and never above the total."""
    if total_due <= 0:
        return ZERO
    rest = max(ZERO, total_due - emi_billed - fees_and_interest)
    mad = emi_billed + fees_and_interest + q2(rest * card.min_due_pct / 100)
    return min(total_due, max(mad, card.min_due_floor))


def utilization_pct(owed: Decimal, limit: Decimal) -> Decimal:
    if limit <= 0:
        return ZERO
    return q2(max(owed, ZERO) / limit * 100)


async def cards_by_account(
    session: AsyncSession, household_id: uuid.UUID
) -> dict[uuid.UUID, CreditCard]:
    rows = (
        await session.scalars(select(CreditCard).where(CreditCard.household_id == household_id))
    ).all()
    return {c.account_id: c for c in rows}


async def household_debts(
    session: AsyncSession, household_id: uuid.UUID, include_cards: bool
) -> list[Debt]:
    """Every open debt as a payoff-planner input: loans, card EMIs and revolving card balances."""
    today = date.today()
    out: list[Debt] = []
    for loan in (
        await session.scalars(
            select(Loan).where(Loan.household_id == household_id, Loan.closed.is_(False))
        )
    ).all():
        st = loan_status(loan, await loan_schedule(session, loan), today)
        if st.outstanding > 0 and st.emi > 0:
            account = await session.get(Account, loan.account_id)
            out.append(
                Debt(
                    f"loan:{loan.id}",
                    account.name if account else "Loan",
                    st.outstanding,
                    st.rate_pct,
                    st.emi,
                )
            )
    if not include_cards:
        return out
    cards = (
        await session.scalars(select(CreditCard).where(CreditCard.household_id == household_id))
    ).all()
    owed = await balances(session, household_id)
    for card in cards:
        account = await session.get(Account, card.account_id)
        name = account.name if account else card.issuer
        emis = (
            await session.scalars(
                select(CardEmi).where(CardEmi.card_id == card.id, CardEmi.closed.is_(False))
            )
        ).all()
        emi_outstanding = ZERO
        for item in emis:
            st_e = card_emi_status(item, today)
            if st_e.outstanding > 0:
                emi_outstanding += st_e.outstanding
                out.append(
                    Debt(
                        f"card_emi:{item.id}",
                        f"{name}: {item.description}",
                        st_e.outstanding,
                        item.rate_pct,
                        st_e.emi,
                    )
                )
        revolving = owed.get(card.account_id, ZERO) - emi_outstanding
        if revolving > 0:
            payment = min_due(card, revolving)
            out.append(
                Debt(f"card:{card.id}", name, revolving, card.monthly_rate_pct * 12, payment)
            )
    return out
