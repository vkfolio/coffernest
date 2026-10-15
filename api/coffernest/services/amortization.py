"""Pure, exact-decimal loan maths. No I/O — everything here is unit-testable.

Conventions (match Indian banks / emicalculator.net):
- Monthly reducing balance: interest = round(opening × annual% / 1200, 2).
- EMI from the standard formula, rounded to the rupee (``emi_override`` wins when the bank's
  figure differs). The final instalment absorbs the rounding.
- A rate change applies from the first EMI due on/after its effective date. ``keep_emi`` keeps the
  EMI and moves the end date (banks' default on floating loans); otherwise the EMI is recomputed
  over the remaining planned tenure.
- A prepayment dated ``d`` reduces the balance before the first EMI due *after* ``d``.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, localcontext

from dateutil.relativedelta import relativedelta

from coffernest.enums import PrepayMode

ZERO = Decimal("0")
CENT = Decimal("0.01")
RUPEE = Decimal("1")
MAX_PERIODS = 1200


def q2(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def monthly_rate(annual_pct: Decimal) -> Decimal:
    return annual_pct / Decimal(1200)


def emi(principal: Decimal, annual_pct: Decimal, months: int, round_to: Decimal = RUPEE) -> Decimal:
    if months <= 0:
        raise ValueError("months must be positive")
    if principal <= 0:
        return ZERO
    with localcontext() as ctx:
        ctx.prec = 34
        r = monthly_rate(annual_pct)
        if r == 0:
            raw = principal / months
        else:
            growth = (1 + r) ** months
            raw = principal * r * growth / (growth - 1)
    return raw.quantize(round_to, rounding=ROUND_HALF_UP)


def months_to_repay(balance: Decimal, annual_pct: Decimal, payment: Decimal) -> int | None:
    """Payments of ``payment`` needed to clear ``balance``; None if it never amortises."""
    if balance <= 0:
        return 0
    r = monthly_rate(annual_pct)
    if r == 0:
        return int((balance / payment).to_integral_value(rounding=ROUND_CEILING))
    if payment <= balance * r:
        return None
    n = -math.log(1 - float(balance * r / payment)) / math.log(1 + float(r))
    return max(1, math.ceil(n - 1e-9))


@dataclass(frozen=True)
class RateEvent:
    effective_date: date
    rate_pct: Decimal
    keep_emi: bool = True


@dataclass(frozen=True)
class PrepayEvent:
    date: date
    amount: Decimal
    mode: PrepayMode = PrepayMode.reduce_tenure


@dataclass(frozen=True)
class LoanTerms:
    principal: Decimal
    rate_pct: Decimal
    tenure_months: int
    first_emi_date: date
    emi_override: Decimal | None = None
    rate_events: Sequence[RateEvent] = ()
    prepayments: Sequence[PrepayEvent] = ()


@dataclass(frozen=True)
class ScheduleRow:
    period: int
    due_date: date
    opening: Decimal
    rate_pct: Decimal
    emi: Decimal
    interest: Decimal
    principal: Decimal
    prepayment: Decimal  # applied to the balance just before this EMI
    closing: Decimal
    estimated: bool = False  # reconstructed history from before the loan was entered


@dataclass
class Schedule:
    rows: list[ScheduleRow] = field(default_factory=list)

    @property
    def total_interest(self) -> Decimal:
        return sum((r.interest for r in self.rows), ZERO)

    @property
    def total_paid(self) -> Decimal:
        return sum((r.emi + r.prepayment for r in self.rows), ZERO)

    @property
    def end_date(self) -> date | None:
        return self.rows[-1].due_date if self.rows else None

    def outstanding_on(self, day: date) -> Decimal:
        """Principal still owed after every EMI/prepayment up to and including ``day``."""
        if not self.rows:
            return ZERO
        balance = self.rows[0].opening + self.rows[0].prepayment
        for row in self.rows:
            if row.due_date > day:
                break
            balance = row.closing
        return balance

    def paid_rows(self, day: date) -> list[ScheduleRow]:
        return [r for r in self.rows if r.due_date <= day]

    def next_row(self, day: date) -> ScheduleRow | None:
        return next((r for r in self.rows if r.due_date > day), None)

    def interest_between(self, start: date, end: date) -> Decimal:
        return sum((r.interest for r in self.rows if start <= r.due_date <= end), ZERO)

    def principal_between(self, start: date, end: date) -> Decimal:
        return sum(
            (r.principal + r.prepayment for r in self.rows if start <= r.due_date <= end), ZERO
        )


def build_schedule(terms: LoanTerms) -> Schedule:
    balance = terms.principal
    rate = terms.rate_pct
    payment = terms.emi_override or emi(balance, rate, terms.tenure_months)
    plan_n = terms.tenure_months  # planned number of EMIs in the current plan
    rates = sorted(terms.rate_events, key=lambda e: e.effective_date)
    prepays = sorted(terms.prepayments, key=lambda p: p.date)
    ri = pi = 0
    out = Schedule()
    k = 0
    while balance > 0 and k < MAX_PERIODS:
        due = terms.first_emi_date + relativedelta(months=k)

        prepaid = ZERO
        while pi < len(prepays) and prepays[pi].date < due:
            event = prepays[pi]
            pi += 1
            amount = min(event.amount, balance)
            if amount <= 0:
                continue
            balance -= amount
            prepaid += amount
            if balance <= 0:
                break
            if event.mode is PrepayMode.reduce_emi:
                payment = emi(balance, rate, max(1, plan_n - k))
            else:
                plan_n = k + (months_to_repay(balance, rate, payment) or plan_n - k)

        while ri < len(rates) and rates[ri].effective_date <= due:
            event_r = rates[ri]
            ri += 1
            rate = event_r.rate_pct
            left = months_to_repay(balance, rate, payment) if event_r.keep_emi else None
            if (
                left is None
            ):  # recompute EMI (by choice, or because the old one no longer covers interest)
                payment = emi(balance, rate, max(1, plan_n - k))
            else:
                plan_n = k + left

        if balance <= 0:
            out.rows.append(ScheduleRow(k + 1, due, prepaid, rate, ZERO, ZERO, ZERO, prepaid, ZERO))
            break

        interest = q2(balance * monthly_rate(rate))
        principal = payment - interest
        last_planned = k >= plan_n - 1 and balance - principal < payment
        if principal >= balance or last_planned or k == MAX_PERIODS - 1:
            principal = balance  # final EMI absorbs the rupee rounding
        this_emi = principal + interest
        closing = balance - principal
        out.rows.append(
            ScheduleRow(
                period=k + 1,
                due_date=due,
                opening=balance + prepaid,
                rate_pct=rate,
                emi=this_emi,
                interest=interest,
                principal=principal,
                prepayment=prepaid,
                closing=closing,
            )
        )
        balance = closing
        k += 1
    return out


@dataclass(frozen=True)
class History:
    """What's known about a loan before it was entered: the original loan's first EMI date and,
    if known, its sanctioned amount, tenure and EMI."""

    start_date: date
    original_principal: Decimal | None = None
    original_tenure_months: int | None = None
    original_emi: Decimal | None = None


def months_between(start: date, end: date) -> int:
    return (end.year - start.year) * 12 + end.month - start.month


def history_rows(terms: LoanTerms, history: History) -> list[ScheduleRow]:
    """EMIs paid before ``terms.first_emi_date``, rebuilt backwards from the balance at entry.

    The balance at entry is the figure checked against the bank, so it anchors the history:
    walking back, each month's opening = (closing + EMI) / (1 + r). The EMI is the original EMI
    if known, else the one implied by the sanctioned amount and tenure, else today's EMI. Every
    row is marked estimated; ``reconstruction_gap`` says how far the rebuilt start is from the
    sanctioned amount."""
    n_pre = months_between(history.start_date, terms.first_emi_date)
    if n_pre <= 0:
        return []
    rates = sorted(terms.rate_events, key=lambda e: e.effective_date)

    def rate_on(day: date) -> Decimal:
        rate = terms.rate_pct
        for event in rates:
            if event.effective_date <= day:
                rate = event.rate_pct
        return rate

    start_rate = rate_on(history.start_date)
    if history.original_emi:
        payment = history.original_emi
    elif history.original_principal and history.original_tenure_months:
        payment = emi(history.original_principal, start_rate, history.original_tenure_months)
    else:
        payment = terms.emi_override or emi(terms.principal, terms.rate_pct, terms.tenure_months)
    rows: list[ScheduleRow] = []
    balance = terms.principal
    for k in range(1, min(n_pre, MAX_PERIODS) + 1):
        due = terms.first_emi_date - relativedelta(months=k)
        rate = rate_on(due)
        opening = q2((balance + payment) / (1 + monthly_rate(rate)))
        principal = opening - balance
        rows.append(
            ScheduleRow(
                period=0,
                due_date=due,
                opening=opening,
                rate_pct=rate,
                emi=payment,
                interest=payment - principal,
                principal=principal,
                prepayment=ZERO,
                closing=balance,
                estimated=True,
            )
        )
        balance = opening
    rows.reverse()
    return rows


def with_history(schedule: Schedule, past: list[ScheduleRow]) -> Schedule:
    """``past`` rows followed by the live schedule, periods renumbered from 1."""
    if not past:
        return schedule
    rows = [*past, *schedule.rows]
    return Schedule(rows=[replace(r, period=i) for i, r in enumerate(rows, start=1)])


def reconstruction_gap(past: list[ScheduleRow], history: History) -> Decimal | None:
    if not past or not history.original_principal:
        return None
    return past[0].opening - history.original_principal


@dataclass(frozen=True)
class Comparison:
    base_interest: Decimal
    new_interest: Decimal
    interest_saved: Decimal
    base_end: date | None
    new_end: date | None
    months_saved: int
    new_emi: Decimal | None


def compare(base: Schedule, new: Schedule) -> Comparison:
    new_emis = [r.emi for r in new.rows if r.emi > 0]
    return Comparison(
        base_interest=base.total_interest,
        new_interest=new.total_interest,
        interest_saved=base.total_interest - new.total_interest,
        base_end=base.end_date,
        new_end=new.end_date,
        months_saved=len([r for r in base.rows if r.emi > 0])
        - len([r for r in new.rows if r.emi > 0]),
        new_emi=new_emis[-2] if len(new_emis) >= 2 else (new_emis[0] if new_emis else None),
    )


def with_extra(
    terms: LoanTerms,
    lump_sum: Decimal = ZERO,
    lump_date: date | None = None,
    extra_monthly: Decimal = ZERO,
    mode: PrepayMode = PrepayMode.reduce_tenure,
    start: date | None = None,
) -> LoanTerms:
    """Terms plus hypothetical prepayments (a lump sum and/or a fixed extra every month)."""
    extra: list[PrepayEvent] = list(terms.prepayments)
    first = start or terms.first_emi_date
    if lump_sum > 0:
        extra.append(PrepayEvent(lump_date or first, lump_sum, mode))
    if extra_monthly > 0:
        for k in range(terms.tenure_months + 240):
            extra.append(PrepayEvent(first + relativedelta(months=k), extra_monthly, mode))
    return LoanTerms(
        principal=terms.principal,
        rate_pct=terms.rate_pct,
        tenure_months=terms.tenure_months,
        first_emi_date=terms.first_emi_date,
        emi_override=terms.emi_override,
        rate_events=terms.rate_events,
        prepayments=extra,
    )


# ---------- cross-debt payoff planning (avalanche / snowball) ----------


@dataclass(frozen=True)
class Debt:
    key: str
    name: str
    balance: Decimal
    rate_pct: Decimal
    payment: Decimal  # the contractual monthly payment (EMI / minimum)


@dataclass(frozen=True)
class DebtOutcome:
    key: str
    name: str
    months: int
    interest: Decimal


@dataclass(frozen=True)
class PayoffPlan:
    strategy: str
    months: int
    total_interest: Decimal
    order: list[DebtOutcome]


@dataclass
class _Live:
    debt: Debt
    balance: Decimal
    interest: Decimal = ZERO


def plan_payoff(debts: Iterable[Debt], extra: Decimal, strategy: str) -> PayoffPlan:
    """Month-by-month simulation. Every debt gets its payment; ``extra`` (plus the payments of
    debts already cleared — the "snowball" roll-over) goes to the target debt: highest rate first
    (avalanche) or smallest balance first (snowball). ``strategy="none"`` is the baseline."""
    live = [_Live(d, d.balance) for d in debts if d.balance > 0]
    done: list[DebtOutcome] = []
    month = 0
    freed = ZERO
    while live and month < MAX_PERIODS:
        month += 1
        for item in live:
            interest = q2(item.balance * monthly_rate(item.debt.rate_pct))
            item.interest += interest
            pay = min(item.debt.payment, item.balance + interest)
            item.balance = item.balance + interest - pay
        pool = extra + freed if strategy != "none" else ZERO
        if strategy == "avalanche":
            targets = sorted(live, key=lambda i: (-i.debt.rate_pct, i.balance))
        else:
            targets = sorted(live, key=lambda i: (i.balance, -i.debt.rate_pct))
        for item in targets:
            if pool <= 0:
                break
            take = min(pool, item.balance)
            item.balance -= take
            pool -= take
        for item in [i for i in live if i.balance <= 0]:
            live.remove(item)
            freed += item.debt.payment
            done.append(DebtOutcome(item.debt.key, item.debt.name, month, item.interest))
    return PayoffPlan(
        strategy=strategy,
        months=month,
        total_interest=sum((d.interest for d in done), ZERO),
        order=done,
    )
