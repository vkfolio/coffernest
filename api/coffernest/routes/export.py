"""/api/v1/export — plain files for a CA or a spreadsheet. No account numbers, ever."""

from __future__ import annotations

import csv
import io
from datetime import date

from fastapi import APIRouter, Query
from fastapi.responses import Response
from sqlalchemy import select

from coffernest.deps import Reader, Session
from coffernest.enums import Direction
from coffernest.models import Account, Category, Loan, Transaction

router = APIRouter(prefix="/export", tags=["export"])

COLUMNS = (
    "date",
    "account",
    "in",
    "out",
    "kind",
    "category",
    "subcategory",
    "payee",
    "note",
    "tags",
    "status",
    "loan",
)


@router.get("/transactions.csv", response_class=Response)
async def transactions_csv(
    ctx: Reader,
    session: Session,
    start: date | None = Query(default=None, alias="from"),
    end: date | None = Query(default=None, alias="to"),
) -> Response:
    """Every entry in the range as CSV: money in and out in separate columns."""
    hid = ctx.household_id
    accounts = {
        a.id: a.name
        for a in (await session.scalars(select(Account).where(Account.household_id == hid))).all()
    }
    cats = {
        c.id: c
        for c in (await session.scalars(select(Category).where(Category.household_id == hid)))
    }
    loans = {
        loan.id: accounts.get(loan.account_id, loan.lender)
        for loan in (await session.scalars(select(Loan).where(Loan.household_id == hid))).all()
    }
    query = select(Transaction).where(
        Transaction.household_id == hid, Transaction.deleted_at.is_(None)
    )
    if start:
        query = query.where(Transaction.date >= start)
    if end:
        query = query.where(Transaction.date <= end)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(COLUMNS)
    for t in (await session.scalars(query.order_by(Transaction.date, Transaction.id))).all():
        cat = cats.get(t.category_id) if t.category_id else None
        parent = cats.get(cat.parent_id) if cat and cat.parent_id else None
        amount = f"{t.amount:.2f}"
        writer.writerow(
            (
                t.date.isoformat(),
                accounts.get(t.account_id, ""),
                amount if t.direction is Direction.credit else "",
                amount if t.direction is Direction.debit else "",
                t.kind.value,
                (parent or cat).name if cat else "",
                cat.name if cat and parent else "",
                _safe(t.payee),
                _safe(t.note),
                " ".join(t.tags or []),
                t.status.value,
                loans.get(t.loan_id, "") if t.loan_id else "",
            )
        )
    span = f"{start or 'start'}-to-{end or date.today()}"
    return Response(
        content="﻿" + buf.getvalue(),  # BOM so Excel reads ₹ and Indian names correctly
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="coffernest-{span}.csv"'},
    )


def _safe(text: str | None) -> str:
    """Spreadsheet formula injection guard: a cell starting with = + - @ is shown as text."""
    value = text or ""
    return "'" + value if value[:1] in ("=", "+", "-", "@") else value
