"""/api/v1/loans — term loans with schedules, rate changes, prepayments and what-ifs."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal, cast

from dateutil.relativedelta import relativedelta
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coffernest.config import get_settings
from coffernest.deps import Ctx, Reader, Session, Writer, not_found, owned
from coffernest.enums import (
    AccountKind,
    CategoryKind,
    Direction,
    LoanKind,
    PrepayMode,
    RateType,
    TxnKind,
    TxnSource,
)
from coffernest.models import (
    Account,
    Category,
    CreditCard,
    Loan,
    LoanPrepayment,
    LoanRateChange,
    RecurringRule,
    Transaction,
)
from coffernest.services import amortization as am
from coffernest.services import card_loans, debts, live, loan_history
from coffernest.services import recurring as rec

Day = date  # field names "date" would shadow the type inside Pydantic models

router = APIRouter(prefix="/loans", tags=["loans"])

Money = Field(gt=0, max_digits=14, decimal_places=2)
Pct = Field(ge=0, le=60, max_digits=7, decimal_places=4)

_EMI_CATEGORY = {
    LoanKind.home: "Home loan EMI",
    LoanKind.personal: "Personal loan EMI",
    LoanKind.car: "Car loan EMI",
}


class LoanIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    kind: LoanKind
    lender: str = Field(default="", max_length=80)
    last4: str = Field(default="", pattern=r"^\d{0,4}$")
    principal: Decimal = Money
    original_principal: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    rate_pct: Decimal = Pct
    rate_type: RateType = RateType.fixed
    tenure_months: int = Field(ge=1, le=600)
    first_emi_date: date
    emi_override: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    funding_account_id: uuid.UUID | None = None
    start_date: date | None = None
    original_tenure_months: int | None = Field(default=None, ge=1, le=600)
    original_emi: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    tax_80c: bool = False
    tax_24b: bool = False
    tax_80e: bool = False
    prepayment_penalty_pct: Decimal = Field(default=Decimal("0"), ge=0, le=10)
    owner_member_id: uuid.UUID | None = None
    # Also create a monthly auto-debit EMI rule from the funding account.
    create_emi_rule: bool = True


class LoanPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    lender: str | None = Field(default=None, max_length=80)
    original_principal: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    start_date: date | None = None
    original_tenure_months: int | None = Field(default=None, ge=1, le=600)
    original_emi: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    emi_override: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    funding_account_id: uuid.UUID | None = None
    tax_80c: bool | None = None
    tax_24b: bool | None = None
    tax_80e: bool | None = None
    rate_type: RateType | None = None
    closed: bool | None = None


class LoanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    account_id: uuid.UUID
    name: str = ""
    kind: LoanKind
    lender: str
    principal: Decimal
    original_principal: Decimal | None
    rate_pct: Decimal
    rate_type: RateType
    tenure_months: int
    first_emi_date: date
    emi_override: Decimal | None
    start_date: date | None = None
    original_tenure_months: int | None = None
    original_emi: Decimal | None = None
    funding_account_id: uuid.UUID | None
    tax_80c: bool
    tax_24b: bool
    tax_80e: bool
    prepayment_penalty_pct: Decimal
    closed: bool
    # derived
    outstanding: Decimal = Decimal("0")
    emi: Decimal = Decimal("0")
    current_rate_pct: Decimal = Decimal("0")
    next_due: date | None = None
    end_date: date | None = None
    emis_left: int = 0
    interest_left: Decimal = Decimal("0")
    principal_paid_pct: Decimal = Decimal("0")


class ScheduleRowOut(BaseModel):
    period: int
    due_date: date
    opening: Decimal
    rate_pct: Decimal
    emi: Decimal
    interest: Decimal
    principal: Decimal
    prepayment: Decimal
    closing: Decimal
    is_past: bool


class ScheduleOut(BaseModel):
    rows: list[ScheduleRowOut]
    total_interest: Decimal
    total_paid: Decimal
    end_date: date | None


class HistoryRowOut(BaseModel):
    period: int
    due_date: date
    opening: Decimal
    rate_pct: Decimal
    emi: Decimal
    interest: Decimal
    principal: Decimal
    prepayment: Decimal
    closing: Decimal
    phase: Literal["estimated", "paid", "due", "future"]
    actual_amount: Decimal | None
    actual_date: date | None
    transaction_id: uuid.UUID | None


class PaidToDateOut(BaseModel):
    emis: int
    total_paid: Decimal
    interest: Decimal
    principal: Decimal
    prepayments: Decimal


class ToGoOut(BaseModel):
    emis: int
    total: Decimal
    interest: Decimal
    principal: Decimal


class HistoryPointOut(BaseModel):
    month: date
    balance: Decimal
    cum_interest: Decimal
    cum_principal: Decimal
    estimated: bool


class LoanHistoryOut(BaseModel):
    start_date: date
    entry_date: date
    today: date
    original_principal: Decimal | None
    outstanding: Decimal
    reconstruction_gap: Decimal | None
    paid_to_date: PaidToDateOut
    to_go: ToGoOut
    rows: list[HistoryRowOut]
    series: list[HistoryPointOut]


class RateChangeIn(BaseModel):
    effective_date: date
    rate_pct: Decimal = Pct
    keep_emi: bool = True


class RateChangeOut(RateChangeIn):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID


class PrepaymentIn(BaseModel):
    date: Day
    amount: Decimal = Money
    mode: PrepayMode = PrepayMode.reduce_tenure
    # Record the money leaving this account in the ledger too.
    from_account_id: uuid.UUID | None = None


class PrepaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    date: Day
    amount: Decimal
    mode: PrepayMode
    transaction_id: uuid.UUID | None


class SimulateIn(BaseModel):
    lump_sum: Decimal = Field(default=Decimal("0"), ge=0, max_digits=14, decimal_places=2)
    lump_date: date | None = None
    extra_monthly: Decimal = Field(default=Decimal("0"), ge=0, max_digits=14, decimal_places=2)
    mode: PrepayMode = PrepayMode.reduce_tenure
    new_rate_pct: Decimal | None = Field(default=None, ge=0, le=60)


class SimulateOut(BaseModel):
    base_interest: Decimal
    new_interest: Decimal
    interest_saved: Decimal
    penalty: Decimal
    net_saving: Decimal
    base_end: date | None
    new_end: date | None
    months_saved: int
    current_emi: Decimal
    new_emi: Decimal | None


class PayoffIn(BaseModel):
    extra_monthly: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    include_cards: bool = True


class PayoffDebtOut(BaseModel):
    key: str
    name: str
    months: int
    interest: Decimal


Strategy = Literal["none", "avalanche", "snowball"]


class PayoffPlanOut(BaseModel):
    strategy: Strategy
    months: int
    total_interest: Decimal
    debt_free_on: date
    order: list[PayoffDebtOut]


class PayoffOut(BaseModel):
    baseline: PayoffPlanOut
    avalanche: PayoffPlanOut
    snowball: PayoffPlanOut
    recommended: Literal["avalanche", "snowball"]


# ---------- helpers ----------


async def _out(session: AsyncSession, loan: Loan, today: date | None = None) -> LoanOut:
    today = today or date.today()
    account = await session.get(Account, loan.account_id)
    schedule = await debts.loan_schedule(session, loan)
    st = debts.loan_status(loan, schedule, today)
    out = LoanOut.model_validate(loan)
    out.name = account.name if account else ""
    out.outstanding = st.outstanding
    out.emi = st.emi
    out.current_rate_pct = st.rate_pct
    out.next_due = st.next_due
    out.end_date = st.end_date
    out.emis_left = st.emis_left
    out.interest_left = st.interest_left
    out.principal_paid_pct = st.principal_paid_pct
    return out


async def _emi_category(session: AsyncSession, ctx: Ctx, kind: LoanKind) -> uuid.UUID | None:
    name = _EMI_CATEGORY.get(kind)
    query = select(Category.id).where(
        Category.household_id == ctx.household_id, Category.kind == CategoryKind.debt
    )
    if name:
        found = await session.scalar(query.where(Category.name == name))
        if found:
            return found
    return await session.scalar(query.where(Category.parent_id.is_(None)))


async def sync_emi_rule(session: AsyncSession, loan: Loan) -> None:
    """Keep the auto-debit EMI rule's amount and end date in line with the live schedule."""
    rule = await session.scalar(select(RecurringRule).where(RecurringRule.loan_id == loan.id))
    if rule is None:
        return
    st = debts.loan_status(loan, await debts.loan_schedule(session, loan), date.today())
    if st.emi > 0 and (rule.amount != st.emi or rule.end_date != st.end_date):
        rule.amount = st.emi
        rule.end_date = st.end_date
        today = date.today()
        await rec.drop_future(session, rule.id, today)
        rule.materialized_until = today - timedelta(days=1)
        await session.flush()
        await rec.materialize(
            session, today + timedelta(days=get_settings().materialize_days_ahead), rule_id=rule.id
        )
    if st.emis_left == 0 or loan.closed:
        rule.active = False
        await rec.drop_future(session, rule.id, date.today())


# ---------- CRUD ----------


@router.get("", response_model=list[LoanOut])
async def list_loans(ctx: Reader, session: Session, include_closed: bool = False) -> list[LoanOut]:
    query = select(Loan).where(Loan.household_id == ctx.household_id)
    if not include_closed:
        query = query.where(Loan.closed.is_(False))
    rows = (await session.scalars(query.order_by(Loan.created_at))).all()
    return [await _out(session, loan) for loan in rows]


@router.post("", response_model=LoanOut, status_code=201)
async def create_loan(body: LoanIn, ctx: Writer, session: Session) -> LoanOut:
    if body.funding_account_id is not None:
        await owned(session, Account, body.funding_account_id, ctx)
    account = Account(
        household_id=ctx.household_id,
        kind=AccountKind.loan,
        name=body.name,
        institution=body.lender,
        last4=body.last4,
        owner_member_id=body.owner_member_id,
        opening_balance=body.principal,
        opening_date=body.first_emi_date - timedelta(days=30),
        include_in_safe_to_spend=False,
    )
    session.add(account)
    await session.flush()
    data = body.model_dump(exclude={"name", "last4", "owner_member_id", "create_emi_rule"})
    loan = Loan(household_id=ctx.household_id, account_id=account.id, **data)
    session.add(loan)
    await session.flush()

    if body.create_emi_rule and body.funding_account_id is not None:
        schedule = await debts.loan_schedule(session, loan)
        st = debts.loan_status(loan, schedule, date.today() - timedelta(days=1))
        today = date.today()
        rule = RecurringRule(
            household_id=ctx.household_id,
            name=f"{body.name} EMI",
            amount=st.emi or am.emi(loan.principal, loan.rate_pct, loan.tenure_months),
            direction=Direction.debit,
            kind=TxnKind.emi,
            account_id=body.funding_account_id,
            category_id=await _emi_category(session, ctx, body.kind),
            member_id=body.owner_member_id,
            payee=body.lender or body.name,
            rrule=rec.normalize_rrule(f"FREQ=MONTHLY;BYMONTHDAY={body.first_emi_date.day}"),
            start_date=body.first_emi_date,
            end_date=schedule.end_date,
            auto_post=True,
            remind_days_before=3,
            loan_id=loan.id,
            materialized_until=today - timedelta(days=1) if body.first_emi_date < today else None,
        )
        session.add(rule)
        await session.flush()
        await rec.materialize(
            session, today + timedelta(days=get_settings().materialize_days_ahead), rule_id=rule.id
        )
    await session.commit()
    return await _out(session, loan)


@router.get("/{loan_id}", response_model=LoanOut)
async def get_loan(loan_id: uuid.UUID, ctx: Reader, session: Session) -> LoanOut:
    return await _out(session, await owned(session, Loan, loan_id, ctx))


@router.patch("/{loan_id}", response_model=LoanOut)
async def patch_loan(loan_id: uuid.UUID, body: LoanPatch, ctx: Writer, session: Session) -> LoanOut:
    loan = await owned(session, Loan, loan_id, ctx)
    changes = body.model_dump(exclude_unset=True)
    name = changes.pop("name", None)
    if name:
        account = await session.get(Account, loan.account_id)
        if account is not None:
            account.name = name
    if changes.get("funding_account_id") is not None:
        await owned(session, Account, changes["funding_account_id"], ctx)
    for key, value in changes.items():
        setattr(loan, key, value)
    await session.flush()
    await sync_emi_rule(session, loan)
    await session.commit()
    return await _out(session, loan)


class MoveToCardIn(BaseModel):
    card_id: uuid.UUID
    # The card's owed figure already counts this loan's remaining principal (a statement's
    # total outstanding usually does); otherwise it is added to the card.
    card_owed_includes_it: bool = True


class MoveToCardOut(BaseModel):
    card_id: uuid.UUID
    card_emi_id: uuid.UUID
    outstanding: Decimal
    emis_left: int
    bank_entries_removed: int


@router.post("/{loan_id}/move-to-card", response_model=MoveToCardOut)
async def move_to_card(
    loan_id: uuid.UUID, body: MoveToCardIn, ctx: Writer, session: Session
) -> MoveToCardOut:
    """This loan is billed on a credit card: make it an EMI on the card, stop its bank debits."""
    loan = await owned(session, Loan, loan_id, ctx)
    card = await owned(session, CreditCard, body.card_id, ctx)
    try:
        out = await card_loans.move_loan_to_card(
            session, ctx.household_id, loan, card, card_owed_includes_it=body.card_owed_includes_it
        )
    except card_loans.MoveError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    await session.commit()
    live.publish(ctx.household_id)
    return MoveToCardOut(
        card_id=card.id,
        card_emi_id=uuid.UUID(out["card_emi_id"]),
        outstanding=out["outstanding"],
        emis_left=out["emis_left"],
        bank_entries_removed=out["bank_entries_removed"],
    )


@router.get("/{loan_id}/schedule", response_model=ScheduleOut)
async def schedule(loan_id: uuid.UUID, ctx: Reader, session: Session) -> ScheduleOut:
    loan = await owned(session, Loan, loan_id, ctx)
    sched = await debts.loan_schedule(session, loan)
    today = date.today()
    return ScheduleOut(
        rows=[ScheduleRowOut(**r.__dict__, is_past=r.due_date <= today) for r in sched.rows],
        total_interest=sched.total_interest,
        total_paid=sched.total_paid,
        end_date=sched.end_date,
    )


@router.get("/{loan_id}/history", response_model=LoanHistoryOut)
async def history(loan_id: uuid.UUID, ctx: Reader, session: Session) -> LoanHistoryOut:
    """The loan's whole life: EMIs paid so far (estimated before it was entered here, matched to
    the ledger after), interest and principal paid to date, and what's left."""
    loan = await owned(session, Loan, loan_id, ctx)
    return LoanHistoryOut.model_validate(
        await loan_history.loan_history(session, loan, date.today())
    )


# ---------- rate changes & prepayments ----------


@router.get("/{loan_id}/rate-changes", response_model=list[RateChangeOut])
async def list_rate_changes(
    loan_id: uuid.UUID, ctx: Reader, session: Session
) -> list[LoanRateChange]:
    await owned(session, Loan, loan_id, ctx)
    rows = await session.scalars(
        select(LoanRateChange)
        .where(LoanRateChange.loan_id == loan_id)
        .order_by(LoanRateChange.effective_date)
    )
    return list(rows.all())


@router.post("/{loan_id}/rate-changes", response_model=RateChangeOut, status_code=201)
async def add_rate_change(
    loan_id: uuid.UUID, body: RateChangeIn, ctx: Writer, session: Session
) -> LoanRateChange:
    loan = await owned(session, Loan, loan_id, ctx)
    change = LoanRateChange(loan_id=loan.id, **body.model_dump())
    session.add(change)
    await session.flush()
    await sync_emi_rule(session, loan)
    await session.commit()
    return change


@router.delete("/{loan_id}/rate-changes/{change_id}", status_code=204)
async def delete_rate_change(
    loan_id: uuid.UUID, change_id: uuid.UUID, ctx: Writer, session: Session
) -> None:
    loan = await owned(session, Loan, loan_id, ctx)
    change = await session.get(LoanRateChange, change_id)
    if change is None or change.loan_id != loan.id:
        raise not_found("Rate change not found")
    await session.delete(change)
    await session.flush()
    await sync_emi_rule(session, loan)
    await session.commit()


@router.get("/{loan_id}/prepayments", response_model=list[PrepaymentOut])
async def list_prepayments(
    loan_id: uuid.UUID, ctx: Reader, session: Session
) -> list[LoanPrepayment]:
    await owned(session, Loan, loan_id, ctx)
    rows = await session.scalars(
        select(LoanPrepayment)
        .where(LoanPrepayment.loan_id == loan_id)
        .order_by(LoanPrepayment.date)
    )
    return list(rows.all())


@router.post("/{loan_id}/prepayments", response_model=PrepaymentOut, status_code=201)
async def add_prepayment(
    loan_id: uuid.UUID, body: PrepaymentIn, ctx: Writer, session: Session
) -> LoanPrepayment:
    loan = await owned(session, Loan, loan_id, ctx)
    txn_id = None
    if body.from_account_id is not None:
        await owned(session, Account, body.from_account_id, ctx)
        account = await session.get(Account, loan.account_id)
        txn = Transaction(
            household_id=ctx.household_id,
            account_id=body.from_account_id,
            date=body.date,
            amount=body.amount,
            direction=Direction.debit,
            kind=TxnKind.prepayment,
            source=TxnSource.manual,
            category_id=await _emi_category(session, ctx, loan.kind),
            member_id=ctx.member_id,
            payee=f"Prepayment · {account.name if account else 'loan'}",
            loan_id=loan.id,
            created_by=ctx.user_id,
        )
        session.add(txn)
        await session.flush()
        txn_id = txn.id
    prepay = LoanPrepayment(
        loan_id=loan.id, date=body.date, amount=body.amount, mode=body.mode, transaction_id=txn_id
    )
    session.add(prepay)
    await session.flush()
    await sync_emi_rule(session, loan)
    await session.commit()
    return prepay


@router.delete("/{loan_id}/prepayments/{prepay_id}", status_code=204)
async def delete_prepayment(
    loan_id: uuid.UUID, prepay_id: uuid.UUID, ctx: Writer, session: Session
) -> None:
    loan = await owned(session, Loan, loan_id, ctx)
    prepay = await session.get(LoanPrepayment, prepay_id)
    if prepay is None or prepay.loan_id != loan.id:
        raise not_found("Prepayment not found")
    if prepay.transaction_id is not None:
        txn = await session.get(Transaction, prepay.transaction_id)
        if txn is not None:
            await session.delete(txn)
    await session.delete(prepay)
    await session.flush()
    await sync_emi_rule(session, loan)
    await session.commit()


# ---------- what-ifs ----------


@router.post("/{loan_id}/simulate", response_model=SimulateOut)
async def simulate(
    loan_id: uuid.UUID, body: SimulateIn, ctx: Reader, session: Session
) -> SimulateOut:
    loan = await owned(session, Loan, loan_id, ctx)
    terms = await debts.loan_terms(session, loan)
    today = date.today()
    base = am.build_schedule(terms)
    nxt = base.next_row(today)
    start = nxt.due_date if nxt else today
    whatif = am.with_extra(
        terms,
        lump_sum=body.lump_sum,
        lump_date=body.lump_date or today,
        extra_monthly=body.extra_monthly,
        mode=body.mode,
        start=start,
    )
    if body.new_rate_pct is not None:
        whatif = am.LoanTerms(
            principal=whatif.principal,
            rate_pct=whatif.rate_pct,
            tenure_months=whatif.tenure_months,
            first_emi_date=whatif.first_emi_date,
            emi_override=whatif.emi_override,
            rate_events=[*whatif.rate_events, am.RateEvent(start, body.new_rate_pct, True)],
            prepayments=whatif.prepayments,
        )
    cmp = am.compare(base, am.build_schedule(whatif))
    penalty = am.q2(body.lump_sum * loan.prepayment_penalty_pct / 100)
    return SimulateOut(
        base_interest=cmp.base_interest,
        new_interest=cmp.new_interest,
        interest_saved=cmp.interest_saved,
        penalty=penalty,
        net_saving=cmp.interest_saved - penalty,
        base_end=cmp.base_end,
        new_end=cmp.new_end,
        months_saved=cmp.months_saved,
        current_emi=nxt.emi if nxt else am.ZERO,
        new_emi=cmp.new_emi,
    )


@router.post("/payoff-plan", response_model=PayoffOut)
async def payoff_plan(body: PayoffIn, ctx: Reader, session: Session) -> PayoffOut:
    """Which debt should extra money go to? Compares paying only EMIs/minimums against
    avalanche (highest rate first) and snowball (smallest balance first)."""
    items = await debts.household_debts(session, ctx.household_id, body.include_cards)
    start = date.today().replace(day=1)

    def shape(plan: am.PayoffPlan) -> PayoffPlanOut:
        return PayoffPlanOut(
            strategy=cast(Strategy, plan.strategy),
            months=plan.months,
            total_interest=plan.total_interest,
            debt_free_on=start + relativedelta(months=plan.months),
            order=[PayoffDebtOut(**o.__dict__) for o in plan.order],
        )

    baseline = am.plan_payoff(items, am.ZERO, "none")
    aval = am.plan_payoff(items, body.extra_monthly, "avalanche")
    snow = am.plan_payoff(items, body.extra_monthly, "snowball")
    # Recommend snowball only when it costs almost nothing extra (≤1% more interest).
    recommended: Literal["avalanche", "snowball"] = (
        "snowball"
        if snow.total_interest <= aval.total_interest * Decimal("1.01")
        and snow.months <= aval.months
        else "avalanche"
    )
    return PayoffOut(
        baseline=shape(baseline),
        avalanche=shape(aval),
        snowball=shape(snow),
        recommended=recommended,
    )
