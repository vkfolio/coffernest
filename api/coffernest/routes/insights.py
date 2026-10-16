"""/api/v1/insights — computed, read-only views over the ledger and debts."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from coffernest.deps import Reader, Session
from coffernest.enums import Direction, TxnKind
from coffernest.services import insights as svc
from coffernest.services import networth
from coffernest.services import spend as spend_svc
from coffernest.services.amortization import q2

# "2026-10" or "2026-10-01". Apps up to 1.0 build 8 send the short form.
Month = Annotated[
    date, BeforeValidator(lambda v: f"{v}-01" if isinstance(v, str) and len(v) == 7 else v)
]

Day = date  # field names "date" would shadow the type inside Pydantic models

router = APIRouter(prefix="/insights", tags=["insights"])


class FlowOut(BaseModel):
    income: Decimal
    spend: Decimal
    debt: Decimal
    net: Decimal


class MonthFlowOut(FlowOut):
    month: date


class SafeLine(BaseModel):
    date: Day
    label: str
    amount: Decimal


class SafeToSpendOut(BaseModel):
    amount: Decimal
    per_day: Decimal
    until: date
    days_left: int
    liquid: Decimal
    committed: Decimal
    card_dues: Decimal
    buffer: Decimal
    lines: list[SafeLine]


class DueOut(BaseModel):
    date: Day
    title: str
    amount: Decimal
    min_due: Decimal | None = None
    direction: Direction
    kind: TxnKind
    source: str
    transaction_id: uuid.UUID | None
    statement_id: uuid.UUID | None
    card_id: uuid.UUID | None
    account_name: str
    overdue: bool


class LoanLine(BaseModel):
    id: uuid.UUID
    name: str
    kind: str
    outstanding: Decimal
    emi: Decimal
    rate_pct: Decimal
    end_date: date | None
    principal_paid_pct: Decimal


class CardLine(BaseModel):
    id: uuid.UUID
    name: str
    owed: Decimal
    limit: Decimal
    utilization_pct: Decimal
    emi_monthly: Decimal


class DebtLoadOut(BaseModel):
    total_outstanding: Decimal
    loans_outstanding: Decimal
    cards_outstanding: Decimal
    monthly_emi: Decimal
    monthly_income: Decimal
    foir_pct: Decimal | None
    foir_band: str
    dti_ratio: Decimal | None
    card_limit: Decimal
    utilization_pct: Decimal
    loans: list[LoanLine]
    cards: list[CardLine]


class CategorySpendOut(BaseModel):
    category_id: uuid.UUID | None
    name: str
    color_slot: int
    is_fixed: bool
    amount: Decimal
    previous: Decimal
    avg_3m: Decimal


class NoteOut(BaseModel):
    kind: str
    severity: str
    title: str
    body: str
    evidence: dict[str, Any]


class AnomalyOut(BaseModel):
    kind: str
    transaction_id: uuid.UUID
    date: Day
    title: str
    amount: Decimal
    typical: Decimal | None = None
    other_transaction_id: uuid.UUID | None = None


class NetWorthBreakdown(BaseModel):
    bank: Decimal
    cash: Decimal
    wallet: Decimal
    investment: Decimal
    loans: Decimal
    cards: Decimal


class NetWorthPoint(BaseModel):
    month: date
    assets: Decimal
    liabilities: Decimal
    net: Decimal
    estimated: bool  # depends on loan history rebuilt from before the loan was entered


class NetWorthOut(BaseModel):
    assets: Decimal
    liabilities: Decimal
    net: Decimal
    change_1m: Decimal | None
    change_1m_pct: Decimal | None
    breakdown: NetWorthBreakdown
    series: list[NetWorthPoint]


class HaveOut(BaseModel):
    liquid: Decimal  # bank + cash + wallet
    invested: Decimal


class InOutAccount(BaseModel):
    account_id: uuid.UUID
    name: str
    kind: str
    in_: Decimal = Field(alias="in")
    out: Decimal

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)


class InOutKind(BaseModel):
    kind: TxnKind
    direction: Direction
    amount: Decimal


class InOutOut(BaseModel):
    month: date
    income: Decimal
    spend: Decimal
    debt: Decimal
    transfers: Decimal  # moves between your own accounts, incl. card bill payments
    net: Decimal
    by_account: list[InOutAccount]
    by_kind: list[InOutKind]


class OverviewOut(BaseModel):
    today: date
    safe_to_spend: SafeToSpendOut
    month: FlowOut  # confirmed so far
    month_projected: FlowOut  # incl. scheduled items to month-end
    dues: list[DueOut]
    debt: DebtLoadOut
    emergency_months: Decimal | None
    notes: list[NoteOut]
    net_worth: NetWorthOut
    have: HaveOut
    savings_rate_pct: Decimal | None  # left over / came in, this month so far
    wealth_rate_pct: Decimal | None  # (left over + loan principal repaid) / came in
    principal_repaid: Decimal  # loan principal repaid this month so far


class TaxLoan(BaseModel):
    loan_id: uuid.UUID
    name: str
    principal: Decimal
    interest: Decimal
    estimated: bool = False  # includes EMIs from before the loan was entered


class TaxOut(BaseModel):
    fy_start: date
    fy_end: date
    principal_80c: Decimal
    principal_80c_claimable: Decimal
    interest_24b: Decimal
    interest_24b_claimable: Decimal
    interest_80e: Decimal
    loans: list[TaxLoan]
    note: str


def _flow(f: svc.Flow) -> FlowOut:
    return FlowOut(income=f.income, spend=f.spend, debt=f.debt, net=f.net)


@router.get("/overview", response_model=OverviewOut)
async def overview(ctx: Reader, session: Session) -> OverviewOut:
    """Everything the home screen needs, in one round trip."""
    today = date.today()
    hid = ctx.household_id
    start, end = svc.month_bounds(today)
    sts = await svc.safe_to_spend(session, hid, today)
    month = await svc.flow(session, hid, start, today, scheduled=False)
    worth = await networth.net_worth(session, hid, today, 12)
    repaid = await networth.principal_repaid(session, hid, start, today)
    parts = worth["breakdown"]
    return OverviewOut(
        today=today,
        safe_to_spend=SafeToSpendOut(**{**sts.__dict__, "lines": sts.lines}),
        month=_flow(month),
        net_worth=NetWorthOut.model_validate(worth),
        have=HaveOut(
            liquid=parts["bank"] + parts["cash"] + parts["wallet"], invested=parts["investment"]
        ),
        savings_rate_pct=q2(month.net / month.income * 100) if month.income > 0 else None,
        wealth_rate_pct=(
            q2((month.net + repaid) / month.income * 100) if month.income > 0 else None
        ),
        principal_repaid=repaid,
        month_projected=_flow(await svc.flow(session, hid, start, end, scheduled=True)),
        dues=[DueOut.model_validate(d) for d in await svc.dues(session, hid, today, 30)],
        debt=DebtLoadOut(**(await svc.debt_load(session, hid, today)).__dict__),
        emergency_months=await svc.emergency_months(session, hid, today, sts.liquid),
        notes=[NoteOut.model_validate(n) for n in await svc.notes(session, hid, today)],
    )


@router.get("/networth", response_model=NetWorthOut)
async def net_worth(
    ctx: Reader, session: Session, months: int = Query(default=24, ge=2, le=60)
) -> NetWorthOut:
    """Assets minus liabilities now and at each past month-end."""
    return NetWorthOut.model_validate(
        await networth.net_worth(session, ctx.household_id, date.today(), months)
    )


@router.get("/inout", response_model=InOutOut)
async def in_out(ctx: Reader, session: Session, month: Month | None = None) -> InOutOut:
    """One month's money in vs out, per account and per kind."""
    return InOutOut.model_validate(
        await svc.inout(session, ctx.household_id, month or date.today())
    )


class SpendChild(BaseModel):
    id: uuid.UUID | None
    name: str
    amount: Decimal


class SpendCategory(BaseModel):
    id: uuid.UUID | None
    name: str
    color_slot: int
    amount: Decimal
    share: Decimal
    previous: Decimal
    children: list[SpendChild]


class SpendPerson(BaseModel):
    id: uuid.UUID | None
    name: str
    relation: str
    amount: Decimal
    previous: Decimal


class SpendPayee(BaseModel):
    key: str
    label: str
    amount: Decimal
    count: int


class SpendPoint(BaseModel):
    start: date
    amount: Decimal
    previous: Decimal


class SpendOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    from_: date = Field(alias="from")
    to: date
    days: int
    direction: Direction
    total: Decimal
    count: int
    per_day: Decimal
    previous: Decimal
    previous_from: date
    previous_to: date
    change_pct: Decimal | None
    debt: Decimal
    bucket: str
    by_category: list[SpendCategory]
    by_person: list[SpendPerson]
    top_payees: list[SpendPayee]
    series: list[SpendPoint]


@router.get("/spend", response_model=SpendOut)
async def spend(
    ctx: Reader,
    session: Session,
    start: date | None = Query(default=None, alias="from"),
    end: date | None = Query(default=None, alias="to"),
    category_id: uuid.UUID | None = None,
    person_id: uuid.UUID | None = None,
    family: bool = False,
    q: str | None = Query(default=None, max_length=80),
    direction: Direction = Direction.debit,
    compare_months: int = Query(default=0, ge=0, le=12),
) -> SpendOut:
    """Where it went over any period (no ``from`` = all time), against the period before."""
    out = await spend_svc.spend(
        session,
        ctx.household_id,
        start,
        end or date.today(),
        spend_svc.Filters(
            category_id=category_id,
            person_id=person_id,
            family_only=family,
            q=q,
            direction=direction,
        ),
        compare_months,
    )
    return SpendOut.model_validate(out)


@router.get("/safe-to-spend", response_model=SafeToSpendOut)
async def safe_to_spend(ctx: Reader, session: Session) -> SafeToSpendOut:
    sts = await svc.safe_to_spend(session, ctx.household_id, date.today())
    return SafeToSpendOut(**{**sts.__dict__, "lines": sts.lines})


@router.get("/dues", response_model=list[DueOut])
async def dues(
    ctx: Reader, session: Session, days: int = Query(default=30, ge=1, le=180)
) -> list[DueOut]:
    rows = await svc.dues(session, ctx.household_id, date.today(), days)
    return [DueOut.model_validate(d) for d in rows]


@router.get("/debt", response_model=DebtLoadOut)
async def debt(ctx: Reader, session: Session) -> DebtLoadOut:
    return DebtLoadOut(**(await svc.debt_load(session, ctx.household_id, date.today())).__dict__)


@router.get("/cashflow", response_model=list[MonthFlowOut])
async def cashflow(
    ctx: Reader, session: Session, months: int = Query(default=6, ge=1, le=24)
) -> list[MonthFlowOut]:
    rows = await svc.cashflow_months(session, ctx.household_id, date.today(), months)
    return [MonthFlowOut.model_validate(r) for r in rows]


@router.get("/categories", response_model=list[CategorySpendOut])
async def categories(
    ctx: Reader, session: Session, month: Month | None = None
) -> list[CategorySpendOut]:
    rows = await svc.category_breakdown(session, ctx.household_id, month or date.today())
    return [CategorySpendOut.model_validate(r) for r in rows]


@router.get("/anomalies", response_model=list[AnomalyOut])
async def anomalies(ctx: Reader, session: Session) -> list[AnomalyOut]:
    rows = await svc.anomalies(session, ctx.household_id, date.today())
    return [AnomalyOut.model_validate(r) for r in rows]


@router.get("/notes", response_model=list[NoteOut])
async def notes(ctx: Reader, session: Session) -> list[NoteOut]:
    return [
        NoteOut.model_validate(n) for n in await svc.notes(session, ctx.household_id, date.today())
    ]


@router.get("/tax", response_model=TaxOut)
async def tax(ctx: Reader, session: Session) -> TaxOut:
    return TaxOut.model_validate(await svc.tax_summary(session, ctx.household_id, date.today()))
