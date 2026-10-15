"""/api/v1/categorize — sort entries automatically, review the unsure ones, and undo."""

from __future__ import annotations

import logging
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from coffernest import db
from coffernest.ai.providers import AIUnavailable, get_llm
from coffernest.config import get_settings
from coffernest.deps import Ctx, Reader, Session, Writer, owned
from coffernest.enums import RuleSource
from coffernest.models import AuditLog, Category, CategoryRule, Household, Person, Transaction
from coffernest.security import throttle
from coffernest.services import categorize, live

log = logging.getLogger("coffernest.categorize")

router = APIRouter(prefix="/categorize", tags=["categorize"])

ACTION = "categorize_run"


class RunOut(BaseModel):
    sorted: int
    by_rules: int
    by_ai: int
    suggested: int
    left: int
    ai_used: bool
    run_id: uuid.UUID | None


class ReviewGroup(BaseModel):
    key: str
    label: str
    count: int
    total: Decimal
    last_date: date
    suggested_category_id: uuid.UUID | None
    suggested_person_id: uuid.UUID | None


class AcceptIn(BaseModel):
    key: str
    category_id: uuid.UUID
    person_id: uuid.UUID | None = None


class SimilarIn(BaseModel):
    transaction_id: uuid.UUID


class ChangedOut(BaseModel):
    updated: int
    run_id: uuid.UUID | None


class UndoIn(BaseModel):
    run_id: uuid.UUID


class RuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    match: str
    category_id: uuid.UUID | None
    person_id: uuid.UUID | None
    source: RuleSource
    hits: int


async def _record(
    session: Session, request: Request, ctx: Ctx, undo: dict[str, Any], what: str
) -> uuid.UUID | None:
    if not undo:
        return None
    entry = AuditLog(
        household_id=ctx.household_id,
        user_id=ctx.user_id,
        action=ACTION,
        ip=throttle.client_ip(request),
        meta={"what": what, "count": len(undo), "undo": undo},
    )
    session.add(entry)
    await session.flush()
    return entry.id


@router.post("/run", response_model=RunOut)
async def run(request: Request, ctx: Writer, session: Session) -> RunOut:
    """Sort every uncategorised entry: rules first, then the assistant for unknown payees."""
    household = await session.get(Household, ctx.household_id)
    llm = None
    if household is not None and household.ai_enabled:
        try:
            llm = get_llm(get_settings(), db.sessionmaker(), ctx.household_id)
        except AIUnavailable:
            llm = None
    try:
        result = await categorize.run(session, ctx.household_id, llm)
    except AIUnavailable:
        await session.rollback()
        result = await categorize.run(session, ctx.household_id, None)
    except Exception:
        # The assistant failing must not lose what the rules sorted: run again without it.
        log.exception("sorting with the assistant failed")
        await session.rollback()
        result = await categorize.run(session, ctx.household_id, None)
    run_id = await _record(session, request, ctx, result.undo, "run")
    await session.commit()
    live.publish(ctx.household_id)
    return RunOut(
        sorted=result.by_rules + result.by_ai,
        by_rules=result.by_rules,
        by_ai=result.by_ai,
        suggested=result.suggested,
        left=result.left,
        ai_used=result.ai_used,
        run_id=run_id,
    )


@router.get("/review", response_model=list[ReviewGroup])
async def review(ctx: Reader, session: Session) -> list[dict[str, Any]]:
    """Payees whose entries still have no category, biggest first, with a suggestion."""
    return await categorize.review(session, ctx.household_id)


@router.post("/accept", response_model=ChangedOut)
async def accept(body: AcceptIn, request: Request, ctx: Writer, session: Session) -> ChangedOut:
    """'Swiggy goes in Food delivery': remember it and sort every waiting entry from it."""
    await owned(session, Category, body.category_id, ctx)
    if body.person_id is not None:
        await owned(session, Person, body.person_id, ctx)
    key = categorize.payee_key(body.key) or body.key.strip().lower()
    await categorize.learn(session, ctx.household_id, key, body.category_id, body.person_id)
    waiting = [
        t
        for t in await categorize.similar(session, ctx.household_id, key, body.category_id)
        if t.category_id is None or await _is_other(session, t.category_id)
    ]
    undo = await categorize.apply_to(waiting, body.category_id, body.person_id)
    run_id = await _record(session, request, ctx, undo, f"accept {key}")
    await session.commit()
    live.publish(ctx.household_id)
    return ChangedOut(updated=len(undo), run_id=run_id)


async def _is_other(session: Session, category_id: uuid.UUID) -> bool:
    cat = await session.get(Category, category_id)
    return cat is not None and cat.parent_id is None and cat.name == "Other"


@router.post("/similar", response_model=ChangedOut)
async def apply_similar(
    body: SimilarIn, request: Request, ctx: Writer, session: Session
) -> ChangedOut:
    """Give every other entry from this entry's payee the same category (and person)."""
    txn = await owned(session, Transaction, body.transaction_id, ctx)
    if txn.category_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Give this entry a category first")
    key = categorize.payee_key(txn.payee)
    others = [
        t
        for t in await categorize.similar(session, ctx.household_id, key, txn.category_id)
        if t.id != txn.id
    ]
    undo = await categorize.apply_to(others, txn.category_id, txn.person_id)
    run_id = await _record(session, request, ctx, undo, f"similar {key}")
    await session.commit()
    live.publish(ctx.household_id)
    return ChangedOut(updated=len(undo), run_id=run_id)


@router.post("/undo", response_model=ChangedOut)
async def undo(body: UndoIn, ctx: Writer, session: Session) -> ChangedOut:
    entry = await session.get(AuditLog, body.run_id)
    if entry is None or entry.household_id != ctx.household_id or entry.action != ACTION:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Nothing to undo")
    if entry.meta.get("undone"):
        return ChangedOut(updated=0, run_id=None)
    count = await categorize.restore(session, ctx.household_id, entry.meta.get("undo") or {})
    entry.meta = {**entry.meta, "undone": True}
    await session.commit()
    live.publish(ctx.household_id)
    return ChangedOut(updated=count, run_id=None)


@router.get("/rules", response_model=list[RuleOut])
async def list_rules(ctx: Reader, session: Session) -> list[CategoryRule]:
    rows = await session.scalars(
        select(CategoryRule)
        .where(
            CategoryRule.household_id == ctx.household_id,
            CategoryRule.source != RuleSource.suggested,
        )
        .order_by(CategoryRule.match)
    )
    return list(rows.all())


@router.delete("/rules/{rule_id}", status_code=204)
async def delete_rule(rule_id: uuid.UUID, ctx: Writer, session: Session) -> None:
    rule = await owned(session, CategoryRule, rule_id, ctx)
    await session.delete(rule)
    await session.commit()
