"""Background worker (separate process, same image): materialise recurring rules, auto-post
auto-debits, and send due-date reminders. A plain asyncio loop — no Redis, tiny RAM footprint."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from coffernest import db
from coffernest.config import get_settings
from coffernest.enums import Direction
from coffernest.models import Household, Notification, RecurringRule, Transaction
from coffernest.money import format_money
from coffernest.services import insights, push, recurring

log = logging.getLogger("coffernest.worker")
TICK_S = 15 * 60
REMIND_HOUR = 8  # local time; reminders go out from 08:00


def _fmt(amount: object, currency: str) -> str:
    return format_money(amount, currency)


async def materialize_all(today: date) -> int:
    settings = get_settings()
    async with db.sessionmaker()() as session:
        created = await recurring.materialize(
            session, today + timedelta(days=settings.materialize_days_ahead)
        )
        posted = await recurring.auto_post_due(session, today)
        await session.commit()
    if created or posted:
        log.info("materialised %s, auto-posted %s", created, posted)
    return created


async def remind(today: date) -> int:
    """One notification per due item per lead time (dedupe key), pushed to every member."""
    settings = get_settings()
    sent = 0
    async with db.sessionmaker()() as session:
        households = (await session.scalars(select(Household))).all()
        for household in households:
            rules = {
                r.id: r
                for r in (
                    await session.scalars(
                        select(RecurringRule).where(RecurringRule.household_id == household.id)
                    )
                ).all()
            }
            for item in await insights.dues(session, household.id, today, 7):
                if item["direction"] is not Direction.debit:
                    continue
                due = item["date"]
                assert isinstance(due, date)
                lead = (due - today).days
                rule_id = None
                if item["transaction_id"] is not None:
                    txn = await session.get(Transaction, item["transaction_id"])
                    rule_id = txn.recurring_rule_id if txn else None
                window = rules[rule_id].remind_days_before if rule_id in rules else 3
                if item["overdue"]:
                    key_part, title = "overdue", f"Overdue: {item['title']}"
                elif lead == 0:
                    key_part, title = "today", f"Due today: {item['title']}"
                elif lead <= window:
                    key_part, title = f"d{lead}", f"{item['title']} due {due:%d %b}"
                else:
                    continue
                ref = item["transaction_id"] or item["statement_id"]
                key = f"due:{ref}:{due.isoformat()}:{key_part}"
                body = _fmt(item["amount"], household.currency)
                if item.get("min_due"):
                    body += f" · minimum {_fmt(item['min_due'], household.currency)}"
                result = await session.execute(
                    insert(Notification)
                    .values(
                        household_id=household.id,
                        kind="due",
                        title=title,
                        body=body,
                        url="/" if item["statement_id"] is None else "/debts",
                        dedupe_key=key,
                    )
                    .on_conflict_do_nothing(index_elements=[Notification.dedupe_key])
                    .returning(Notification.id)
                )
                if result.scalar() is None:
                    continue  # already reminded
                sent += await push.send_to_household(
                    session, settings, household.id, title, body, tag=key
                )
        await session.commit()
    return sent


async def tick() -> None:
    settings = get_settings()
    now = datetime.now(ZoneInfo(settings.timezone))
    today = now.date()
    await materialize_all(today)
    if now.hour >= REMIND_HOUR:
        await remind(today)
    from coffernest.demo import ensure_demo

    if (done := await ensure_demo()) in ("created", "refreshed"):
        log.info("demo account %s", done)


async def run_forever(configure_logging: bool = True) -> None:
    if configure_logging:
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
        )
    log.info("worker started")
    while True:
        started = datetime.now(UTC)
        try:
            await tick()
        except Exception:
            log.exception("worker tick failed")
        elapsed = (datetime.now(UTC) - started).total_seconds()
        await asyncio.sleep(max(30.0, TICK_S - elapsed))
