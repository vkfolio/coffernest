"""Recurring rules: RRULE expansion, idempotent materialisation, and scheduled↔actual matching."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from dateutil.rrule import rrulestr
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coffernest.enums import Direction, TxnSource, TxnStatus
from coffernest.models import RecurringRule, Transaction

MATCH_DAYS = 3
MATCH_PCT = Decimal("0.05")
MATCH_PCT_ESTIMATE = Decimal("0.25")

_DAY = re.compile(r"BYMONTHDAY=(\d+)(?![\d,])")


def normalize_rrule(rule: str) -> str:
    """Upper-case, strip DTSTART, and clamp day 29–31 to month-end.

    RFC 5545 skips months that lack the day ("31st" never fires in April). People mean "the 31st,
    or the last day". ``BYMONTHDAY=N,-1;BYSETPOS=1`` gives exactly that.
    """
    body = rule.strip().upper().removeprefix("RRULE:")
    parts = [p for p in body.split(";") if p and not p.startswith("DTSTART")]
    body = ";".join(parts)
    m = _DAY.search(body)
    if m and int(m.group(1)) >= 29 and "BYSETPOS" not in body and "FREQ=MONTHLY" in body:
        body = _DAY.sub(f"BYMONTHDAY={m.group(1)},-1", body) + ";BYSETPOS=1"
    return body


def validate_rrule(rule: str, start: date) -> str:
    body = normalize_rrule(rule)
    try:
        rrulestr(body, dtstart=datetime.combine(start, time()))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid repeat rule: {exc}") from exc
    if not body.startswith("FREQ=") and "FREQ=" not in body:
        raise ValueError("Repeat rule needs FREQ")
    return body


def occurrences(
    rule: str,
    start: date,
    window_from: date,
    window_to: date,
    end: date | None = None,
    skipped: list[date] | None = None,
) -> list[date]:
    if end is not None:
        window_to = min(window_to, end)
    if window_to < window_from:
        return []
    rr = rrulestr(normalize_rrule(rule), dtstart=datetime.combine(start, time()))
    hits = rr.between(
        datetime.combine(window_from, time()), datetime.combine(window_to, time()), inc=True
    )
    skip = set(skipped or [])
    return [d.date() for d in hits if d.date() not in skip]


def next_occurrence(rule: RecurringRule, after: date) -> date | None:
    rr = rrulestr(normalize_rrule(rule.rrule), dtstart=datetime.combine(rule.start_date, time()))
    cursor = datetime.combine(after, time())
    skip = set(rule.skipped_dates or [])
    for _ in range(50):
        nxt = rr.after(cursor, inc=False)
        if nxt is None:
            return None
        if rule.end_date and nxt.date() > rule.end_date:
            return None
        if nxt.date() not in skip:
            return nxt.date()
        cursor = nxt
    return None


def _legs(rule: RecurringRule, day: date) -> list[dict[str, object]]:
    status = TxnStatus.cleared if rule.auto_post and day <= date.today() else TxnStatus.scheduled
    base: dict[str, object] = {
        "id": uuid.uuid4(),
        "household_id": rule.household_id,
        "account_id": rule.account_id,
        "date": day,
        "amount": rule.amount,
        "direction": rule.direction,
        "kind": rule.kind,
        "status": status,
        "source": TxnSource.recurring,
        "category_id": rule.category_id,
        "member_id": rule.member_id,
        "person_id": rule.person_id,
        "payee": rule.payee or rule.name,
        "note": "",
        "tags": [],
        "recurring_rule_id": rule.id,
        "occurrence_date": day,
        "loan_id": rule.loan_id,
    }
    if rule.to_account_id is None:
        return [base]
    group = uuid.uuid4()
    other = Direction.credit if rule.direction is Direction.debit else Direction.debit
    return [
        {**base, "transfer_group_id": group},
        {
            **base,
            "id": uuid.uuid4(),
            "account_id": rule.to_account_id,
            "direction": other,
            "transfer_group_id": group,
        },
    ]


async def materialize(
    session: AsyncSession,
    until: date,
    household_id: uuid.UUID | None = None,
    rule_id: uuid.UUID | None = None,
) -> int:
    """Create scheduled rows for every rule occurrence up to ``until``. Safe to run repeatedly."""
    query = select(RecurringRule).where(RecurringRule.active.is_(True))
    if household_id is not None:
        query = query.where(RecurringRule.household_id == household_id)
    if rule_id is not None:
        query = query.where(RecurringRule.id == rule_id)
    created = 0
    for rule in (await session.scalars(query)).all():
        start = rule.start_date
        if rule.materialized_until is not None:
            start = max(start, rule.materialized_until + timedelta(days=1))
        rows: list[dict[str, object]] = []
        for day in occurrences(
            rule.rrule, rule.start_date, start, until, rule.end_date, rule.skipped_dates
        ):
            rows.extend(_legs(rule, day))
        if rows:
            stmt = insert(Transaction).values(rows).on_conflict_do_nothing()
            result = await session.execute(stmt)
            created += max(result.rowcount or 0, 0)  # type: ignore[attr-defined]
        rule.materialized_until = max(until, rule.materialized_until or until)
    return created


async def auto_post_due(session: AsyncSession, today: date) -> int:
    """Clear scheduled auto-post occurrences whose date has arrived."""
    auto_rules = select(RecurringRule.id).where(RecurringRule.auto_post.is_(True))
    result = await session.execute(
        update(Transaction)
        .where(
            Transaction.status == TxnStatus.scheduled,
            Transaction.date <= today,
            Transaction.deleted_at.is_(None),
            Transaction.recurring_rule_id.in_(auto_rules),
        )
        .values(status=TxnStatus.cleared, updated_at=datetime.now(UTC))
    )
    return max(result.rowcount or 0, 0)  # type: ignore[attr-defined]


async def drop_future(session: AsyncSession, rule_id: uuid.UUID, from_day: date) -> None:
    """Remove not-yet-confirmed occurrences (rule edited, paused or deleted)."""
    await session.execute(
        update(Transaction)
        .where(
            Transaction.recurring_rule_id == rule_id,
            Transaction.status == TxnStatus.scheduled,
            Transaction.date >= from_day,
            Transaction.deleted_at.is_(None),
        )
        .values(deleted_at=datetime.now(UTC))
    )


async def find_match(
    session: AsyncSession,
    household_id: uuid.UUID,
    account_id: uuid.UUID,
    direction: Direction,
    amount: Decimal,
    day: date,
    category_id: uuid.UUID | None,
) -> Transaction | None:
    """The scheduled occurrence a real entry most likely fulfils (±3 days, ±5% / ±25% estimate)."""
    rows = (
        await session.execute(
            select(Transaction, RecurringRule.amount_is_estimate)
            .join(RecurringRule, RecurringRule.id == Transaction.recurring_rule_id)
            .where(
                Transaction.household_id == household_id,
                Transaction.account_id == account_id,
                Transaction.direction == direction,
                Transaction.status == TxnStatus.scheduled,
                Transaction.deleted_at.is_(None),
                Transaction.transfer_group_id.is_(None),
                Transaction.date.between(
                    day - timedelta(days=MATCH_DAYS), day + timedelta(days=MATCH_DAYS)
                ),
            )
        )
    ).all()
    best: tuple[int, Transaction] | None = None
    for txn, estimate in rows:
        tolerance = MATCH_PCT_ESTIMATE if estimate else MATCH_PCT
        if abs(txn.amount - amount) > txn.amount * tolerance:
            continue
        score = abs((txn.date - day).days) * 10 + (0 if txn.category_id == category_id else 5)
        if best is None or score < best[0]:
            best = (score, txn)
    return best[1] if best else None
