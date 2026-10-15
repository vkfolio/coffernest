"""Loans that are billed on a credit card, and stopping repeating entries.

A loan on a card (or a purchase converted to EMI) is paid through the card's bill, not by its
own debit from the bank. When one was entered as an ordinary loan, its EMI shows twice: once as
a bank auto-debit and again inside the card bill. Moving it onto the card turns it into a card
EMI with the same remaining instalments and stops the bank-side entries.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coffernest.enums import CardEmiKind, Direction, TxnKind, TxnStatus
from coffernest.models import Account, CardEmi, CreditCard, Loan, RecurringRule, Transaction
from coffernest.services import debts

ZERO = Decimal("0")


class MoveError(ValueError):
    """The move can't be made; the message says why."""


async def hide_scheduled(session: AsyncSession, rule_id: uuid.UUID, from_day: date) -> list[str]:
    """Soft-delete a rule's not-yet-confirmed entries from ``from_day``; returns their ids."""
    rows = (
        await session.scalars(
            select(Transaction).where(
                Transaction.recurring_rule_id == rule_id,
                Transaction.status == TxnStatus.scheduled,
                Transaction.date >= from_day,
                Transaction.deleted_at.is_(None),
            )
        )
    ).all()
    now = datetime.now(UTC)
    for txn in rows:
        txn.deleted_at = now
    return [str(t.id) for t in rows]


async def unhide(session: AsyncSession, household_id: uuid.UUID, ids: list[str]) -> None:
    if not ids:
        return
    for txn in (
        await session.scalars(
            select(Transaction).where(Transaction.id.in_([uuid.UUID(i) for i in ids]))
        )
    ).all():
        if txn.household_id == household_id:
            txn.deleted_at = None


async def move_loan_to_card(
    session: AsyncSession,
    household_id: uuid.UUID,
    loan: Loan,
    card: CreditCard,
    *,
    card_owed_includes_it: bool,
    today: date | None = None,
) -> dict[str, Any]:
    """Turn ``loan`` into a card EMI on ``card`` with the same remaining instalments.

    The loan is closed, its account archived and its EMI entries from the bank stopped. When the
    card's owed figure doesn't yet include the loan's remaining principal, it is added to the
    card as an adjustment so the total owed stays the same. Does not commit."""
    today = today or date.today()
    if loan.household_id != household_id or card.household_id != household_id:
        raise MoveError("Not found")
    if loan.closed:
        raise MoveError("That loan is already closed")
    schedule = await debts.loan_schedule(session, loan)
    upcoming = [r for r in schedule.rows if r.due_date > today]
    if not upcoming:
        raise MoveError("That loan has no EMIs left to move")
    outstanding = upcoming[0].opening
    account = await session.get(Account, loan.account_id)
    name = account.name if account else (loan.lender or "Loan")

    item = CardEmi(
        card_id=card.id,
        kind=CardEmiKind.loan_on_card,
        description=name[:120],
        principal=outstanding,
        rate_pct=loan.rate_pct,
        tenure_months=len(upcoming),
        first_emi_date=upcoming[0].due_date,
    )
    session.add(item)

    adjustment: Transaction | None = None
    if not card_owed_includes_it:
        adjustment = Transaction(
            household_id=household_id,
            account_id=card.account_id,
            date=today,
            amount=outstanding,
            direction=Direction.debit,
            kind=TxnKind.adjustment,
            status=TxnStatus.cleared,
            payee=f"{name}: moved onto the card"[:120],
            tags=["card-emi"],
        )
        session.add(adjustment)

    hidden: list[str] = []
    rules: list[str] = []
    for rule in (
        await session.scalars(
            select(RecurringRule).where(
                RecurringRule.loan_id == loan.id, RecurringRule.active.is_(True)
            )
        )
    ).all():
        rule.active = False
        rules.append(str(rule.id))
        # Unconfirmed EMIs from the bank, past or future, never happened that way.
        hidden += await hide_scheduled(session, rule.id, date.min)
    kept = await session.scalar(
        select(Transaction.id)
        .where(
            Transaction.loan_id == loan.id,
            Transaction.status == TxnStatus.cleared,
            Transaction.deleted_at.is_(None),
        )
        .limit(1)
    )
    loan.closed = True
    if account is not None:
        account.is_active = False
    await session.flush()
    return {
        "moved": name,
        "card": card.id,
        "card_emi_id": str(item.id),
        "outstanding": outstanding,
        "emis_left": len(upcoming),
        "next_emi": upcoming[0].due_date,
        "added_to_card_balance": adjustment is not None,
        "bank_entries_removed": len(hidden),
        "has_cleared_bank_emis": kept is not None,
        "undo": {
            "kind": "loan_to_card",
            "ids": [str(loan.id)],
            "card_emi": str(item.id),
            "adjustment": str(adjustment.id) if adjustment else None,
            "rules": rules,
            "hidden": hidden,
        },
    }


async def undo_move(session: AsyncSession, household_id: uuid.UUID, spec: dict[str, Any]) -> int:
    loan = await session.get(Loan, uuid.UUID(str(spec["ids"][0])))
    if loan is None or loan.household_id != household_id:
        return 0
    loan.closed = False
    account = await session.get(Account, loan.account_id)
    if account is not None:
        account.is_active = True
    item = await session.get(CardEmi, uuid.UUID(str(spec["card_emi"])))
    if item is not None:
        await session.delete(item)
    if spec.get("adjustment"):
        txn = await session.get(Transaction, uuid.UUID(str(spec["adjustment"])))
        if txn is not None and txn.household_id == household_id:
            txn.deleted_at = datetime.now(UTC)
    for rid in spec.get("rules") or []:
        rule = await session.get(RecurringRule, uuid.UUID(rid))
        if rule is not None and rule.household_id == household_id:
            rule.active = True
    await unhide(session, household_id, spec.get("hidden") or [])
    return 1


async def stop_rule(
    session: AsyncSession, household_id: uuid.UUID, rule: RecurringRule, from_day: date
) -> dict[str, Any]:
    """Stop a repeating entry from ``from_day``: nothing is planned on or after it. Does not
    commit. Entries already confirmed stay in the ledger."""
    if rule.household_id != household_id:
        raise MoveError("Not found")
    before = {
        "active": rule.active,
        "end_date": rule.end_date.isoformat() if rule.end_date else None,
    }
    today = date.today()
    if from_day > today:
        rule.end_date = from_day - timedelta(days=1)
    else:
        rule.active = False
    hidden = await hide_scheduled(session, rule.id, from_day)
    await session.flush()
    return {
        "stopped": rule.name,
        "from": from_day,
        "planned_entries_removed": len(hidden),
        "undo": {"kind": "rule_stop", "ids": [str(rule.id)], "before": before, "hidden": hidden},
    }


async def undo_stop(session: AsyncSession, household_id: uuid.UUID, spec: dict[str, Any]) -> int:
    rule = await session.get(RecurringRule, uuid.UUID(str(spec["ids"][0])))
    if rule is None or rule.household_id != household_id:
        return 0
    before = spec.get("before") or {}
    rule.active = bool(before.get("active", True))
    rule.end_date = date.fromisoformat(before["end_date"]) if before.get("end_date") else None
    await unhide(session, household_id, spec.get("hidden") or [])
    return 1
