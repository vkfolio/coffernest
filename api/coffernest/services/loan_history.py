"""A loan's whole life: EMIs paid so far (estimated before it was entered, matched to the ledger
after), what's left, and the series behind the history charts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coffernest.enums import TxnKind, TxnStatus
from coffernest.models import Loan, Transaction
from coffernest.services import debts
from coffernest.services.amortization import ZERO, ScheduleRow, reconstruction_gap

MATCH_DAYS = 10


@dataclass
class _Payment:
    id: Any
    date: date
    amount: Decimal
    kind: TxnKind
    used: bool = False


def _phase(row: ScheduleRow, today: date, matched: bool) -> str:
    if row.due_date > today:
        return "future"
    if matched:
        return "paid"
    return "estimated" if row.estimated else "due"


async def loan_history(session: AsyncSession, loan: Loan, today: date) -> dict[str, Any]:
    live, full = await debts.loan_full_schedule(session, loan)
    history = debts.loan_history_terms(loan)
    past = [r for r in full.rows if r.estimated]
    payments = [
        _Payment(t.id, t.date, t.amount, t.kind)
        for t in (
            await session.scalars(
                select(Transaction)
                .where(
                    Transaction.loan_id == loan.id,
                    Transaction.deleted_at.is_(None),
                    Transaction.status != TxnStatus.scheduled,
                    Transaction.kind.in_((TxnKind.emi, TxnKind.prepayment)),
                )
                .order_by(Transaction.date)
            )
        ).all()
    ]
    window = timedelta(days=MATCH_DAYS)
    rows: list[dict[str, Any]] = []
    paid: dict[str, Any] = {
        "emis": 0,
        "total_paid": ZERO,
        "interest": ZERO,
        "principal": ZERO,
        "prepayments": ZERO,
    }
    to_go: dict[str, Any] = {"emis": 0, "total": ZERO, "interest": ZERO, "principal": ZERO}
    series: list[dict[str, Any]] = []
    cum_interest = cum_principal = ZERO
    for row in full.rows:
        match = next(
            (
                p
                for p in payments
                if not p.used and p.kind is TxnKind.emi and abs(p.date - row.due_date) <= window
            ),
            None,
        )
        if match is not None and row.due_date <= today + window:
            match.used = True
        else:
            match = None
        if row.due_date <= today:
            if row.emi > 0:
                paid["emis"] += 1
            paid["total_paid"] += row.emi + row.prepayment
            paid["interest"] += row.interest
            paid["principal"] += row.principal
            paid["prepayments"] += row.prepayment
        else:
            if row.emi > 0:
                to_go["emis"] += 1
            to_go["total"] += row.emi + row.prepayment
            to_go["interest"] += row.interest
            to_go["principal"] += row.principal + row.prepayment
        cum_interest += row.interest
        cum_principal += row.principal + row.prepayment
        rows.append(
            {
                "period": row.period,
                "due_date": row.due_date,
                "opening": row.opening,
                "rate_pct": row.rate_pct,
                "emi": row.emi,
                "interest": row.interest,
                "principal": row.principal,
                "prepayment": row.prepayment,
                "closing": row.closing,
                "phase": _phase(row, today, match is not None),
                "actual_amount": match.amount if match else None,
                "actual_date": match.date if match else None,
                "transaction_id": match.id if match else None,
            }
        )
        series.append(
            {
                "month": row.due_date.replace(day=1),
                "balance": row.closing,
                "cum_interest": cum_interest,
                "cum_principal": cum_principal,
                "estimated": row.estimated,
            }
        )
    return {
        "start_date": full.rows[0].due_date if full.rows else loan.first_emi_date,
        "entry_date": loan.first_emi_date,
        "today": today,
        "original_principal": loan.original_principal,
        "outstanding": live.outstanding_on(today),
        "reconstruction_gap": reconstruction_gap(past, history) if history else None,
        "paid_to_date": paid,
        "to_go": to_go,
        "rows": rows,
        "series": series,
    }
