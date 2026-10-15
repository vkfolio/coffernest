"""Account balances from the ledger.

Sign convention: ``debit`` = money leaves you through this account, ``credit`` = money arrives.
- Asset accounts (bank, cash, wallet, investment): balance = opening + credits − debits.
- Credit cards: owed = opening + debits (spends, fees) − credits (payments, refunds).
- Loans: owed comes from the amortisation schedule, not the ledger (EMIs mix interest and
  principal), see ``services.debts``.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coffernest.enums import AccountKind, Direction, TxnStatus
from coffernest.models import Account, Transaction

ZERO = Decimal("0")


async def ledger_net(
    session: AsyncSession,
    household_id: uuid.UUID,
    as_of: date | None = None,
    include_scheduled: bool = False,
) -> dict[uuid.UUID, Decimal]:
    """credits − debits per account (cleared/reconciled only unless ``include_scheduled``)."""
    signed = case(
        (Transaction.direction == Direction.credit, Transaction.amount),
        else_=-Transaction.amount,
    )
    query = (
        select(Transaction.account_id, func.coalesce(func.sum(signed), 0))
        .where(Transaction.household_id == household_id, Transaction.deleted_at.is_(None))
        .group_by(Transaction.account_id)
    )
    if not include_scheduled:
        query = query.where(Transaction.status != TxnStatus.scheduled)
    if as_of is not None:
        query = query.where(Transaction.date <= as_of)
    return {acc: Decimal(total) for acc, total in (await session.execute(query)).all()}


def balance_of(account: Account, net: Decimal) -> Decimal:
    """Asset accounts: money held. Card accounts: amount owed (positive = you owe)."""
    if account.kind is AccountKind.credit_card:
        return account.opening_balance - net
    return account.opening_balance + net


async def balances(
    session: AsyncSession,
    household_id: uuid.UUID,
    as_of: date | None = None,
    include_scheduled: bool = False,
) -> dict[uuid.UUID, Decimal]:
    nets = await ledger_net(session, household_id, as_of, include_scheduled)
    accounts = (
        await session.scalars(select(Account).where(Account.household_id == household_id))
    ).all()
    return {
        a.id: balance_of(a, nets.get(a.id, ZERO))
        for a in accounts
        if a.kind is not AccountKind.loan
    }
