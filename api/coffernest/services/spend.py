"""Where the money went (or came from) over any period: totals, the period before, categories
with their sub-categories, people, places and a trend.

Spend is the same as everywhere else in the app: cleared money out of kind expense, fee or
interest. EMIs are their own line and never sit inside a category; moves between your own
accounts (transfers, card bill payments) are neither spend nor income.
"""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from dateutil.relativedelta import relativedelta
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coffernest.enums import Direction, TxnStatus
from coffernest.models import Category, Person, Transaction
from coffernest.services.categorize import payee_key
from coffernest.services.insights import DEBT_KINDS, INCOME_KINDS, SPEND_KINDS

ZERO = Decimal("0")
UNSORTED = "Uncategorised"
FAMILY = "Family"


@dataclass(frozen=True)
class Filters:
    category_id: uuid.UUID | None = None
    person_id: uuid.UUID | None = None
    family_only: bool = False  # entries for no one in particular
    q: str | None = None
    direction: Direction = Direction.debit


def bucket_of(span_days: int) -> str:
    if span_days <= 31:
        return "day"
    if span_days <= 120:
        return "week"
    return "month"


def _bucket_start(day: date, bucket: str, start: date) -> date:
    if bucket == "day":
        return day
    if bucket == "week":
        return start + timedelta(days=((day - start).days // 7) * 7)
    return day.replace(day=1)


def _buckets(start: date, end: date, bucket: str) -> list[date]:
    out: list[date] = []
    cur = _bucket_start(start, bucket, start)
    while cur <= end:
        out.append(cur)
        if bucket == "day":
            cur += timedelta(days=1)
        elif bucket == "week":
            cur += timedelta(days=7)
        else:
            cur = (cur + relativedelta(months=1)).replace(day=1)
    return out


async def first_day(session: AsyncSession, household_id: uuid.UUID) -> date | None:
    return await session.scalar(
        select(func.min(Transaction.date)).where(
            Transaction.household_id == household_id, Transaction.deleted_at.is_(None)
        )
    )


async def _rows(
    session: AsyncSession,
    household_id: uuid.UUID,
    start: date,
    end: date,
    f: Filters,
    kinds: tuple[Any, ...],
) -> list[Any]:
    query = select(
        Transaction.date,
        Transaction.amount,
        Transaction.category_id,
        Transaction.person_id,
        Transaction.payee,
    ).where(
        Transaction.household_id == household_id,
        Transaction.deleted_at.is_(None),
        Transaction.status != TxnStatus.scheduled,
        Transaction.direction == f.direction,
        Transaction.kind.in_(kinds),
        Transaction.date >= start,
        Transaction.date <= end,
    )
    if f.category_id is not None:
        sub = select(Category.id).where(
            or_(Category.id == f.category_id, Category.parent_id == f.category_id)
        )
        query = query.where(Transaction.category_id.in_(sub))
    if f.person_id is not None:
        query = query.where(Transaction.person_id == f.person_id)
    elif f.family_only:
        query = query.where(Transaction.person_id.is_(None))
    if f.q:
        like = f"%{f.q}%"
        query = query.where(or_(Transaction.payee.ilike(like), Transaction.note.ilike(like)))
    return list((await session.execute(query)).all())


def _pct(now: Decimal, before: Decimal) -> Decimal | None:
    if before <= 0:
        return None
    return ((now - before) / before * 100).quantize(Decimal("0.1"))


async def spend(
    session: AsyncSession,
    household_id: uuid.UUID,
    start: date | None,
    end: date,
    f: Filters,
    compare_months: int = 0,
) -> dict[str, Any]:
    """``compare_months`` = 0 compares with the same number of days just before; 1, 3 or 12
    compare with the same days of the previous month, quarter or year (1-13 Oct against 1-13
    Sept), which reads better for "this month" or "this quarter" so far."""
    if start is None:
        start = await first_day(session, household_id) or end
    start = min(start, end)
    days = (end - start).days + 1
    if compare_months > 0:
        prev_start = start - relativedelta(months=compare_months)
        prev_end = end - relativedelta(months=compare_months)
        if (end + timedelta(days=1)).day == 1:  # a whole month: compare whole months
            prev_end = prev_end + relativedelta(day=31)
    else:
        prev_end = start - timedelta(days=1)
        prev_start = prev_end - timedelta(days=days - 1)
    kinds = SPEND_KINDS if f.direction is Direction.debit else INCOME_KINDS

    rows = await _rows(session, household_id, start, end, f, kinds)
    before = await _rows(session, household_id, prev_start, prev_end, f, kinds)

    cats = {
        c.id: c
        for c in (
            await session.scalars(select(Category).where(Category.household_id == household_id))
        ).all()
    }
    people = {
        p.id: p
        for p in (
            await session.scalars(select(Person).where(Person.household_id == household_id))
        ).all()
    }

    def top_of(cid: uuid.UUID | None) -> uuid.UUID | None:
        if cid is None or cid not in cats:
            return None
        c = cats[cid]
        return c.parent_id or c.id

    focus = cats.get(f.category_id) if f.category_id is not None else None
    drill = focus is not None
    drill_parent = focus is not None and focus.parent_id is None

    # Categories: the top level, each with its sub-categories. When looking inside one
    # category, its sub-categories are the top level.
    def group_of(cid: uuid.UUID | None) -> uuid.UUID | None:
        return cid if drill and drill_parent else top_of(cid)

    now_by: dict[uuid.UUID | None, Decimal] = defaultdict(lambda: ZERO)
    prev_by: dict[uuid.UUID | None, Decimal] = defaultdict(lambda: ZERO)
    kids: dict[uuid.UUID | None, dict[uuid.UUID | None, Decimal]] = defaultdict(
        lambda: defaultdict(lambda: ZERO)
    )
    for r in rows:
        g = group_of(r.category_id)
        now_by[g] += r.amount
        kids[g][r.category_id] += r.amount
    for r in before:
        prev_by[group_of(r.category_id)] += r.amount

    total = sum((r.amount for r in rows), ZERO)
    prev_total = sum((r.amount for r in before), ZERO)

    def name_of(cid: uuid.UUID | None) -> str:
        if cid is None:
            return UNSORTED
        c = cats.get(cid)
        return c.name if c else UNSORTED

    by_category = []
    for g, amount in sorted(now_by.items(), key=lambda kv: -kv[1]):
        children = [
            {"id": cid, "name": name_of(cid), "amount": amt}
            for cid, amt in sorted(kids[g].items(), key=lambda kv: -kv[1])
            if cid != g
        ]
        own = kids[g].get(g, ZERO) if g is not None else ZERO
        if children and own > 0:
            children.append({"id": g, "name": f"{name_of(g)} (general)", "amount": own})
        c = cats.get(g) if g else None
        by_category.append(
            {
                "id": g,
                "name": name_of(g),
                "color_slot": c.color_slot if c else 8,
                "amount": amount,
                "share": (amount / total * 100).quantize(Decimal("0.1")) if total else ZERO,
                "previous": prev_by.get(g, ZERO),
                "children": children,
            }
        )

    person_now: dict[uuid.UUID | None, Decimal] = defaultdict(lambda: ZERO)
    person_prev: dict[uuid.UUID | None, Decimal] = defaultdict(lambda: ZERO)
    for r in rows:
        person_now[r.person_id] += r.amount
    for r in before:
        person_prev[r.person_id] += r.amount
    by_person = [
        {
            "id": pid,
            "name": people[pid].name if pid in people else FAMILY,
            "relation": people[pid].relation.value if pid in people else "family",
            "amount": amount,
            "previous": person_prev.get(pid, ZERO),
        }
        for pid, amount in sorted(person_now.items(), key=lambda kv: -kv[1])
    ]

    places: dict[str, list[Any]] = defaultdict(list)
    for r in rows:
        places[payee_key(r.payee) or (r.payee or "").strip().lower() or "(no payee)"].append(r)
    top_payees = []
    for key, items in sorted(places.items(), key=lambda kv: -sum(i.amount for i in kv[1]))[:8]:
        label = Counter(i.payee for i in items if i.payee).most_common(1)
        top_payees.append(
            {
                "key": key,
                "label": label[0][0] if label else key,
                "amount": sum((i.amount for i in items), ZERO),
                "count": len(items),
            }
        )

    bucket = bucket_of(days)
    series_now: dict[date, Decimal] = defaultdict(lambda: ZERO)
    for r in rows:
        series_now[_bucket_start(r.date, bucket, start)] += r.amount
    series_prev: dict[date, Decimal] = defaultdict(lambda: ZERO)
    for r in before:
        series_prev[_bucket_start(r.date, bucket, prev_start)] += r.amount
    now_keys = _buckets(start, end, bucket)
    prev_keys = _buckets(prev_start, prev_end, bucket)
    series = [
        {
            "start": k,
            "amount": series_now.get(k, ZERO),
            "previous": series_prev.get(prev_keys[i], ZERO) if i < len(prev_keys) else ZERO,
        }
        for i, k in enumerate(now_keys)
    ]

    debt = ZERO
    if f.direction is Direction.debit and f.category_id is None and not f.q:
        debt = sum(
            (r.amount for r in await _rows(session, household_id, start, end, f, DEBT_KINDS)),
            ZERO,
        )

    return {
        "from": start,
        "to": end,
        "days": days,
        "direction": f.direction.value,
        "total": total,
        "count": len(rows),
        "per_day": (total / days).quantize(Decimal("0.01")) if days else ZERO,
        "previous": prev_total,
        "previous_from": prev_start,
        "previous_to": prev_end,
        "change_pct": _pct(total, prev_total),
        "debt": debt,
        "bucket": bucket,
        "by_category": by_category,
        "by_person": by_person,
        "top_payees": top_payees,
        "series": series,
    }
