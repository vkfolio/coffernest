"""Path comparison and goal maths for the assistant's roadmaps.

Everything is deterministic and month-by-month, so the model only explains numbers we computed.

A *path* is a rule for where the household's spare money goes each month (plus an optional
lump sum today). Every debt always gets its contractual payment; when a debt is cleared its
payment is freed and joins the spare money ("roll-over"), and once all targeted debts are gone
the money is invested. That keeps paths comparable over the same horizon: each ends with some
debt left and some corpus built.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from dateutil.relativedelta import relativedelta

from coffernest.services.amortization import ZERO, Debt, monthly_rate, q2

PATH_IDS = ("clear_cards", "avalanche", "snowball", "invest_sip", "split", "emergency_first")
CORPUS_MARKS = (
    Decimal(500_000),
    Decimal(1_000_000),
    Decimal(2_500_000),
    Decimal(5_000_000),
    Decimal(10_000_000),
)


@dataclass
class Milestone:
    date: date
    month: int
    label: str
    kind: str  # debt_cleared | debt_free | corpus | emergency_fund


@dataclass
class PathResult:
    path_id: str
    label: str
    description: str
    debt_free_date: date | None
    months_to_debt_free: int | None
    interest_paid: Decimal
    corpus_at_end: Decimal
    debt_at_end: Decimal
    net_at_end: Decimal  # corpus + emergency cash − debt left (only what this plan moves)
    yearly: list[dict[str, object]] = field(default_factory=list)
    milestones: list[Milestone] = field(default_factory=list)


@dataclass
class _Live:
    debt: Debt
    balance: Decimal
    cleared_month: int | None = None


def _is_card(d: Debt) -> bool:
    return d.key.startswith("card")


def _label(path_id: str, debts: Sequence[Debt]) -> tuple[str, str]:
    if path_id.startswith("prepay:"):
        name = path_id.split(":", 1)[1]
        return (
            f"Prepay {name}",
            f"Every spare rupee goes to {name} until it's closed, then to a SIP.",
        )
    return {
        "clear_cards": (
            "Clear cards first",
            "Card balances and card EMIs first, then the costliest loan, then a SIP.",
        ),
        "avalanche": (
            "Costliest debt first",
            "Highest interest rate first; each cleared EMI rolls into the next.",
        ),
        "snowball": (
            "Smallest debt first",
            "Smallest balance first for quick wins; each cleared EMI rolls on.",
        ),
        "invest_sip": (
            "Invest instead",
            "Only the regular EMIs are paid; spare money goes into a SIP.",
        ),
        "split": ("Half and half", "Half to the costliest debt, half into a SIP."),
        "emergency_first": (
            "Safety fund first",
            "Build the emergency fund first, then the costliest debt, then a SIP.",
        ),
    }.get(path_id, (path_id, ""))


def _resolve_prepay(path_id: str, debts: Sequence[Debt]) -> str | None:
    wanted = path_id.split(":", 1)[1].strip().lower()
    for d in debts:
        if d.name.lower() == wanted:
            return d.key
    for d in debts:
        if wanted in d.name.lower():
            return d.key
    return None


def simulate_path(
    path_id: str,
    debts: Sequence[Debt],
    monthly_amount: Decimal,
    lump_sum: Decimal,
    years: int,
    start: date,
    sip_return_pct: Decimal,
    emergency_gap: Decimal = ZERO,
    cash_return_pct: Decimal = Decimal("3.5"),
) -> PathResult:
    label, description = _label(path_id, debts)
    months = years * 12
    live = [_Live(d, d.balance) for d in debts if d.balance > 0]
    prepay_key = _resolve_prepay(path_id, debts) if path_id.startswith("prepay:") else None
    corpus = ZERO
    cash = ZERO  # emergency-fund top-up held in a savings/liquid fund
    interest_paid = ZERO
    freed = ZERO
    sip_rate = (1 + sip_return_pct / 100) ** (Decimal(1) / 12) - 1 if sip_return_pct else ZERO
    cash_rate = monthly_rate(cash_return_pct)
    milestones: list[Milestone] = []
    yearly: list[dict[str, object]] = []
    debt_free: int | None = 0 if not live else None
    marks = list(CORPUS_MARKS)
    gap = max(emergency_gap, ZERO)

    def targets() -> list[_Live]:
        open_ = [i for i in live if i.balance > 0]
        if path_id == "snowball":
            return sorted(open_, key=lambda i: (i.balance, -i.debt.rate_pct))
        if path_id == "clear_cards":
            return sorted(open_, key=lambda i: (not _is_card(i.debt), -i.debt.rate_pct))
        if prepay_key is not None:
            return [i for i in open_ if i.debt.key == prepay_key]
        return sorted(open_, key=lambda i: (-i.debt.rate_pct, i.balance))

    def spend_on_debt(pool: Decimal) -> Decimal:
        for item in targets():
            if pool <= 0:
                break
            take = min(pool, item.balance)
            item.balance -= take
            pool -= take
        return pool

    def allocate(pool: Decimal) -> None:
        nonlocal corpus, cash, gap
        if pool <= 0:
            return
        if path_id == "emergency_first" and gap > 0:
            take = min(pool, gap)
            cash += take
            gap -= take
            pool -= take
            if gap <= 0:
                milestones.append(
                    Milestone(
                        start + relativedelta(months=month),
                        month,
                        "Emergency fund full",
                        "emergency_fund",
                    )
                )
        if path_id == "invest_sip":
            corpus += pool
            return
        if path_id == "split":
            half = q2(pool / 2)
            corpus += pool - half
            pool = half
        corpus += spend_on_debt(pool)

    month = 0
    allocate(lump_sum)
    for month in range(1, months + 1):
        for item in live:
            if item.balance <= 0:
                continue
            interest = q2(item.balance * monthly_rate(item.debt.rate_pct))
            interest_paid += interest
            pay = min(item.debt.payment, item.balance + interest)
            item.balance = item.balance + interest - pay
            if item.balance <= 0 and item.cleared_month is None:
                item.balance = ZERO
                item.cleared_month = month
                freed += item.debt.payment - pay  # this month's unused part
        corpus = q2(corpus * (1 + sip_rate))
        cash = q2(cash * (1 + cash_rate))
        # Payments of debts cleared in earlier months are freed money from now on.
        freed_now = sum(
            (
                i.debt.payment
                for i in live
                if i.cleared_month is not None and i.cleared_month < month
            ),
            ZERO,
        )
        allocate(monthly_amount + freed_now + freed)
        freed = ZERO
        for item in live:
            if item.balance <= 0 and item.cleared_month is None:
                item.balance = ZERO
                item.cleared_month = month
        for item in live:
            if item.cleared_month == month:
                milestones.append(
                    Milestone(
                        start + relativedelta(months=month),
                        month,
                        f"{item.debt.name} closed",
                        "debt_cleared",
                    )
                )
        if debt_free is None and all(i.balance <= 0 for i in live):
            debt_free = month
            milestones.append(
                Milestone(start + relativedelta(months=month), month, "Debt-free", "debt_free")
            )
        while marks and corpus >= marks[0]:
            mark = marks.pop(0)
            milestones.append(
                Milestone(
                    start + relativedelta(months=month),
                    month,
                    f"Investments reach ₹{_short(mark)}",
                    "corpus",
                )
            )
        if month % 12 == 0:
            debt_left = sum((i.balance for i in live), ZERO)
            yearly.append(
                {
                    "year": start.year + month // 12,
                    "date": start + relativedelta(months=month),
                    "debt": q2(debt_left),
                    "corpus": q2(corpus),
                    "cash": q2(cash),
                    "net": q2(corpus + cash - debt_left),
                }
            )
    debt_left = q2(sum((i.balance for i in live), ZERO))
    return PathResult(
        path_id=path_id,
        label=label,
        description=description,
        debt_free_date=start + relativedelta(months=debt_free) if debt_free is not None else None,
        months_to_debt_free=debt_free,
        interest_paid=q2(interest_paid),
        corpus_at_end=q2(corpus),
        debt_at_end=debt_left,
        net_at_end=q2(corpus + cash - debt_left),
        yearly=yearly,
        milestones=milestones,
    )


def _short(value: Decimal) -> str:
    if value >= 10_000_000:
        return (
            f"{value / 10_000_000:.0f} Cr"
            if value % 10_000_000 == 0
            else f"{value / 10_000_000:.1f} Cr"
        )
    return f"{value / 100_000:.0f} L"


def best_of(results: Sequence[PathResult]) -> dict[str, str]:
    """Which path wins each comparison row (the UI marks those cells)."""
    if not results:
        return {}
    free = [r for r in results if r.months_to_debt_free is not None]
    out = {
        "interest_paid": min(results, key=lambda r: r.interest_paid).path_id,
        "corpus_at_end": max(results, key=lambda r: r.corpus_at_end).path_id,
        "net_at_end": max(results, key=lambda r: r.net_at_end).path_id,
    }
    if free:
        out["debt_free_date"] = min(free, key=lambda r: r.months_to_debt_free or 0).path_id
    return out


@dataclass
class GoalPlan:
    target_today: Decimal
    target_future: Decimal
    years: int
    inflation_pct: Decimal
    return_pct: Decimal
    current_saved: Decimal
    current_grows_to: Decimal
    gap: Decimal
    monthly_sip_flat: Decimal
    step_up_pct: Decimal
    monthly_sip_first_year_with_step_up: Decimal


def project_goal(
    target_today: Decimal,
    years: int,
    inflation_pct: Decimal,
    return_pct: Decimal,
    step_up_pct: Decimal = ZERO,
    current_saved: Decimal = ZERO,
) -> GoalPlan:
    """Inflate the goal, grow what's saved, and solve for the monthly SIP (end-of-month)."""
    years = max(1, years)
    future = q2(target_today * (1 + inflation_pct / 100) ** years)
    grows = q2(current_saved * (1 + return_pct / 100) ** years)
    gap = max(future - grows, ZERO)
    r = (1 + return_pct / 100) ** (Decimal(1) / 12) - 1
    n = years * 12
    if r == 0:
        flat = gap / n
        stepped_factor = sum((12 * (1 + step_up_pct / 100) ** y for y in range(years)), Decimal(0))
    else:
        annuity12 = ((1 + r) ** 12 - 1) / r
        flat = gap * r / ((1 + r) ** n - 1)
        stepped_factor = sum(
            (
                (1 + step_up_pct / 100) ** y * annuity12 * (1 + r) ** (12 * (years - y - 1))
                for y in range(years)
            ),
            Decimal(0),
        )
    stepped = gap / stepped_factor if stepped_factor else ZERO
    return GoalPlan(
        target_today=q2(target_today),
        target_future=future,
        years=years,
        inflation_pct=inflation_pct,
        return_pct=return_pct,
        current_saved=q2(current_saved),
        current_grows_to=grows,
        gap=q2(gap),
        monthly_sip_flat=q2(flat).quantize(Decimal(1)),
        step_up_pct=step_up_pct,
        monthly_sip_first_year_with_step_up=q2(stepped).quantize(Decimal(1)),
    )
