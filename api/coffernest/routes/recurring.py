"""/api/v1/recurring-rules — salary, EMIs, rent, school-fee terms, subscriptions…

Rules materialise ``scheduled`` transactions ``materialize_days_ahead`` days into the future.
Creating a rule never back-fills the past: history is assumed to be in the opening balance.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from dateutil.rrule import rrulestr
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select

from coffernest.config import get_settings
from coffernest.deps import Reader, Session, Writer, owned
from coffernest.enums import Direction, TxnKind
from coffernest.models import Account, Category, Person, RecurringRule
from coffernest.services import recurring as svc

router = APIRouter(prefix="/recurring-rules", tags=["recurring"])


class RuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    amount_is_estimate: bool = False
    direction: Direction
    kind: TxnKind | None = None
    account_id: uuid.UUID
    to_account_id: uuid.UUID | None = None
    category_id: uuid.UUID | None = None
    member_id: uuid.UUID | None = None
    person_id: uuid.UUID | None = None
    payee: str = Field(default="", max_length=120)
    rrule: str = Field(min_length=6, max_length=255, examples=["FREQ=MONTHLY;BYMONTHDAY=5"])
    start_date: date
    end_date: date | None = None
    remind_days_before: int = Field(default=2, ge=0, le=30)
    auto_post: bool = False

    @model_validator(mode="after")
    def _check(self) -> RuleIn:
        self.rrule = svc.validate_rrule(self.rrule, self.start_date)
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("end_date is before start_date")
        return self


class RulePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    amount: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    amount_is_estimate: bool | None = None
    category_id: uuid.UUID | None = None
    person_id: uuid.UUID | None = None
    payee: str | None = Field(default=None, max_length=120)
    rrule: str | None = Field(default=None, min_length=6, max_length=255)
    end_date: date | None = None
    remind_days_before: int | None = Field(default=None, ge=0, le=30)
    auto_post: bool | None = None
    active: bool | None = None


class RuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    amount: Decimal
    amount_is_estimate: bool
    direction: Direction
    kind: TxnKind
    account_id: uuid.UUID
    to_account_id: uuid.UUID | None
    category_id: uuid.UUID | None
    member_id: uuid.UUID | None
    person_id: uuid.UUID | None = None
    payee: str
    rrule: str
    start_date: date
    end_date: date | None
    skipped_dates: list[date]
    remind_days_before: int
    auto_post: bool
    active: bool
    loan_id: uuid.UUID | None
    next_date: date | None = None


class PreviewIn(BaseModel):
    rrule: str = Field(min_length=6, max_length=255)
    start_date: date
    count: int = Field(default=6, ge=1, le=36)

    @field_validator("rrule")
    @classmethod
    def _norm(cls, v: str) -> str:
        return svc.normalize_rrule(v)


def _out(rule: RecurringRule) -> RuleOut:
    out = RuleOut.model_validate(rule)
    out.next_date = (
        svc.next_occurrence(rule, date.today() - timedelta(days=1)) if rule.active else None
    )
    return out


def _horizon() -> date:
    return date.today() + timedelta(days=get_settings().materialize_days_ahead)


def _default_kind(direction: Direction, transfer: bool) -> TxnKind:
    if transfer:
        return TxnKind.transfer
    return TxnKind.income if direction is Direction.credit else TxnKind.expense


@router.get("", response_model=list[RuleOut])
async def list_rules(
    ctx: Reader, session: Session, include_inactive: bool = False
) -> list[RuleOut]:
    query = select(RecurringRule).where(RecurringRule.household_id == ctx.household_id)
    if not include_inactive:
        query = query.where(RecurringRule.active.is_(True))
    rows = (await session.scalars(query.order_by(RecurringRule.name))).all()
    return sorted((_out(r) for r in rows), key=lambda r: (r.next_date or date.max, r.name))


@router.post("/preview", response_model=list[date])
async def preview(body: PreviewIn, _: Reader) -> list[date]:
    """Next ``count`` dates for a rule being edited (for the live preview in the form)."""
    try:
        rr = rrulestr(body.rrule, dtstart=datetime.combine(body.start_date, time()))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"Invalid rule: {exc}") from exc
    out: list[date] = []
    for occurrence in rr:
        out.append(occurrence.date())
        if len(out) >= body.count:
            break
    return out


@router.post("", response_model=RuleOut, status_code=201)
async def create_rule(body: RuleIn, ctx: Writer, session: Session) -> RuleOut:
    await owned(session, Account, body.account_id, ctx)
    if body.to_account_id is not None:
        await owned(session, Account, body.to_account_id, ctx)
    if body.category_id is not None:
        await owned(session, Category, body.category_id, ctx)
    if body.person_id is not None:
        await owned(session, Person, body.person_id, ctx)
    data = body.model_dump()
    data["kind"] = body.kind or _default_kind(body.direction, body.to_account_id is not None)
    rule = RecurringRule(household_id=ctx.household_id, **data)
    today = date.today()
    if rule.start_date < today:
        rule.materialized_until = today - timedelta(days=1)
    session.add(rule)
    await session.flush()
    await svc.materialize(session, _horizon(), rule_id=rule.id)
    await session.commit()
    return _out(rule)


@router.get("/{rule_id}", response_model=RuleOut)
async def get_rule(rule_id: uuid.UUID, ctx: Reader, session: Session) -> RuleOut:
    return _out(await owned(session, RecurringRule, rule_id, ctx))


@router.patch("/{rule_id}", response_model=RuleOut)
async def patch_rule(rule_id: uuid.UUID, body: RulePatch, ctx: Writer, session: Session) -> RuleOut:
    rule = await owned(session, RecurringRule, rule_id, ctx)
    changes = body.model_dump(exclude_unset=True)
    if changes.get("rrule"):
        changes["rrule"] = svc.validate_rrule(changes["rrule"], rule.start_date)
    if changes.get("category_id") is not None:
        await owned(session, Category, changes["category_id"], ctx)
    if changes.get("person_id") is not None:
        await owned(session, Person, changes["person_id"], ctx)
    for key, value in changes.items():
        setattr(rule, key, value)
    # Future unconfirmed occurrences follow the new rule; confirmed history is untouched.
    today = date.today()
    await svc.drop_future(session, rule.id, today)
    rule.materialized_until = today - timedelta(days=1)
    await session.flush()
    if rule.active:
        await svc.materialize(session, _horizon(), rule_id=rule.id)
    await session.commit()
    return _out(rule)


@router.delete("/{rule_id}", status_code=204)
async def delete_rule(rule_id: uuid.UUID, ctx: Writer, session: Session) -> None:
    rule = await owned(session, RecurringRule, rule_id, ctx)
    rule.active = False
    await svc.drop_future(session, rule.id, date.today())
    await session.commit()
