"""/api/v1/cards — credit cards, their statements, payments and card EMIs / loans on card."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from coffernest.deps import Reader, Session, Writer, not_found, owned
from coffernest.enums import (
    AccountKind,
    CardEmiKind,
    CategoryKind,
    Direction,
    StatementStatus,
    TxnKind,
    TxnSource,
)
from coffernest.models import Account, CardEmi, CardStatement, Category, CreditCard, Transaction
from coffernest.services import debts, insights
from coffernest.services.balances import balances

Day = date  # field names "date" would shadow the type inside Pydantic models

router = APIRouter(prefix="/cards", tags=["cards"])

Money = Field(ge=0, max_digits=14, decimal_places=2)
ZERO = Decimal("0")


class CardIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    issuer: str = Field(min_length=1, max_length=60)
    network: str = Field(default="", max_length=20)
    last4: str = Field(default="", pattern=r"^\d{0,4}$")
    credit_limit: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    statement_day: int = Field(ge=1, le=31)
    due_days: int = Field(default=20, ge=1, le=60)
    monthly_rate_pct: Decimal = Field(default=Decimal("3.75"), ge=0, le=10)
    min_due_pct: Decimal = Field(default=Decimal("5"), ge=0, le=100)
    min_due_floor: Decimal = Field(default=Decimal("200"), ge=0)
    annual_fee: Decimal = Field(default=Decimal("0"), ge=0)
    fee_waiver_spend: Decimal | None = Field(default=None, ge=0)
    pay_from_account_id: uuid.UUID | None = None
    owner_member_id: uuid.UUID | None = None
    # What you owe on the card when tracking starts (today, unless ``opening_date`` says otherwise).
    opening_owed: Decimal = Money
    opening_date: date | None = None


class CardPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    credit_limit: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    statement_day: int | None = Field(default=None, ge=1, le=31)
    due_days: int | None = Field(default=None, ge=1, le=60)
    monthly_rate_pct: Decimal | None = Field(default=None, ge=0, le=10)
    min_due_pct: Decimal | None = Field(default=None, ge=0, le=100)
    min_due_floor: Decimal | None = Field(default=None, ge=0)
    annual_fee: Decimal | None = Field(default=None, ge=0)
    fee_waiver_spend: Decimal | None = Field(default=None, ge=0)
    pay_from_account_id: uuid.UUID | None = None


class StatementIn(BaseModel):
    statement_date: date
    due_date: date | None = None
    total_due: Decimal = Money
    min_due: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    interest_charged: Decimal = Field(default=ZERO, ge=0)
    fees: Decimal = Field(default=ZERO, ge=0)


class StatementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    statement_date: date
    due_date: date
    total_due: Decimal
    min_due: Decimal
    interest_charged: Decimal
    fees: Decimal
    status: StatementStatus
    paid_amount: Decimal


class PayIn(BaseModel):
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    from_account_id: uuid.UUID | None = None
    date: Day = Field(default_factory=date.today)


class CardEmiIn(BaseModel):
    kind: CardEmiKind
    description: str = Field(min_length=1, max_length=120)
    principal: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    rate_pct: Decimal = Field(ge=0, le=60)
    tenure_months: int = Field(ge=1, le=120)
    first_emi_date: date
    processing_fee: Decimal = Field(default=ZERO, ge=0)
    gst_pct: Decimal = Field(default=Decimal("18"), ge=0, le=28)
    disbursed_to_account_id: uuid.UUID | None = None
    # Loan on card: also record the card debit and the bank credit (a transfer).
    record_disbursal: bool = True


class CardEmiOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: CardEmiKind
    description: str
    principal: Decimal
    rate_pct: Decimal
    tenure_months: int
    first_emi_date: date
    processing_fee: Decimal
    gst_pct: Decimal
    disbursed_to_account_id: uuid.UUID | None
    closed: bool
    emi: Decimal = ZERO
    gst_on_interest: Decimal = ZERO
    outstanding: Decimal = ZERO
    emis_left: int = 0
    next_due: date | None = None
    end_date: date | None = None
    total_cost: Decimal = ZERO


class CardOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    account_id: uuid.UUID
    name: str = ""
    last4: str = ""
    issuer: str
    network: str
    credit_limit: Decimal
    statement_day: int
    due_days: int
    monthly_rate_pct: Decimal
    min_due_pct: Decimal
    min_due_floor: Decimal
    annual_fee: Decimal
    fee_waiver_spend: Decimal | None
    pay_from_account_id: uuid.UUID | None
    # derived
    owed: Decimal = ZERO
    available: Decimal = ZERO
    utilization_pct: Decimal = ZERO
    last_statement_date: date | None = None
    next_statement_date: date | None = None
    latest_statement: StatementOut | None = None
    emi_monthly: Decimal = ZERO
    emi_outstanding: Decimal = ZERO


def _emi_out(item: CardEmi, today: date) -> CardEmiOut:
    st = debts.card_emi_status(item, today)
    out = CardEmiOut.model_validate(item)
    for key, value in st.__dict__.items():
        setattr(out, key, value)
    return out


async def _card_out(session: AsyncSession, card: CreditCard, owed: Decimal) -> CardOut:
    today = date.today()
    account = await session.get(Account, card.account_id)
    latest = await session.scalar(
        select(CardStatement)
        .where(CardStatement.card_id == card.id)
        .order_by(CardStatement.statement_date.desc())
        .limit(1)
    )
    emis = (
        await session.scalars(
            select(CardEmi).where(CardEmi.card_id == card.id, CardEmi.closed.is_(False))
        )
    ).all()
    emi_states = [debts.card_emi_status(e, today) for e in emis]
    out = CardOut.model_validate(card)
    out.name = account.name if account else card.issuer
    out.last4 = account.last4 if account else ""
    out.owed = owed
    out.available = card.credit_limit - owed
    out.utilization_pct = debts.utilization_pct(owed, card.credit_limit)
    out.last_statement_date = debts.last_statement_date(card, today)
    out.next_statement_date = debts.next_statement_date(card, today)
    out.latest_statement = StatementOut.model_validate(latest) if latest else None
    out.emi_monthly = sum((s.emi + s.gst_on_interest for s in emi_states if s.emis_left), ZERO)
    out.emi_outstanding = sum((s.outstanding for s in emi_states), ZERO)
    return out


async def _card_payment_category(
    session: AsyncSession, household_id: uuid.UUID
) -> uuid.UUID | None:
    return await session.scalar(
        select(Category.id).where(
            Category.household_id == household_id,
            Category.kind == CategoryKind.transfer,
            Category.name == "Card bill",
        )
    )


@router.get("", response_model=list[CardOut])
async def list_cards(ctx: Reader, session: Session) -> list[CardOut]:
    rows = (
        await session.scalars(
            select(CreditCard)
            .join(Account, Account.id == CreditCard.account_id)
            .where(CreditCard.household_id == ctx.household_id, Account.is_active.is_(True))
            .order_by(Account.sort, CreditCard.created_at)
        )
    ).all()
    owed = await balances(session, ctx.household_id)
    return [await _card_out(session, c, owed.get(c.account_id, ZERO)) for c in rows]


@router.post("", response_model=CardOut, status_code=201)
async def create_card(body: CardIn, ctx: Writer, session: Session) -> CardOut:
    if body.pay_from_account_id is not None:
        await owned(session, Account, body.pay_from_account_id, ctx)
    account = Account(
        household_id=ctx.household_id,
        kind=AccountKind.credit_card,
        name=body.name,
        institution=body.issuer,
        last4=body.last4,
        owner_member_id=body.owner_member_id,
        opening_balance=body.opening_owed,
        opening_date=min(body.opening_date or date.today(), date.today()),
        include_in_safe_to_spend=False,
    )
    session.add(account)
    await session.flush()
    data = body.model_dump(
        exclude={"name", "last4", "owner_member_id", "opening_owed", "opening_date"}
    )
    card = CreditCard(household_id=ctx.household_id, account_id=account.id, **data)
    session.add(card)
    await session.commit()
    return await _card_out(session, card, body.opening_owed)


@router.get("/{card_id}", response_model=CardOut)
async def get_card(card_id: uuid.UUID, ctx: Reader, session: Session) -> CardOut:
    card = await owned(session, CreditCard, card_id, ctx)
    owed = await balances(session, ctx.household_id)
    return await _card_out(session, card, owed.get(card.account_id, ZERO))


class CardMonthOut(BaseModel):
    month: date
    spend: Decimal
    payments: Decimal
    interest_fees: Decimal
    owed_end: Decimal


class CardHistoryOut(BaseModel):
    months: list[CardMonthOut]
    interest_fees_fy: Decimal


@router.get("/{card_id}/history", response_model=CardHistoryOut)
async def card_history(card_id: uuid.UUID, ctx: Reader, session: Session) -> CardHistoryOut:
    """The last 12 months on this card: spent, paid back, interest and fees, owed at month-end."""
    card = await owned(session, CreditCard, card_id, ctx)
    return CardHistoryOut.model_validate(
        await insights.card_history(session, ctx.household_id, card, date.today())
    )


@router.patch("/{card_id}", response_model=CardOut)
async def patch_card(card_id: uuid.UUID, body: CardPatch, ctx: Writer, session: Session) -> CardOut:
    card = await owned(session, CreditCard, card_id, ctx)
    changes = body.model_dump(exclude_unset=True)
    name = changes.pop("name", None)
    if name:
        account = await session.get(Account, card.account_id)
        if account is not None:
            account.name = name
    if changes.get("pay_from_account_id") is not None:
        await owned(session, Account, changes["pay_from_account_id"], ctx)
    for key, value in changes.items():
        setattr(card, key, value)
    await session.commit()
    owed = await balances(session, ctx.household_id)
    return await _card_out(session, card, owed.get(card.account_id, ZERO))


# ---------- statements ----------


@router.get("/{card_id}/statements", response_model=list[StatementOut])
async def list_statements(card_id: uuid.UUID, ctx: Reader, session: Session) -> list[CardStatement]:
    await owned(session, CreditCard, card_id, ctx)
    rows = await session.scalars(
        select(CardStatement)
        .where(CardStatement.card_id == card_id)
        .order_by(CardStatement.statement_date.desc())
    )
    return list(rows.all())


@router.post("/{card_id}/statements", response_model=StatementOut, status_code=201)
async def add_statement(
    card_id: uuid.UUID, body: StatementIn, ctx: Writer, session: Session
) -> CardStatement:
    card = await owned(session, CreditCard, card_id, ctx)
    emi_billed = sum(
        (
            s.emi + s.gst_on_interest
            for s in (
                debts.card_emi_status(e, body.statement_date)
                for e in (
                    await session.scalars(select(CardEmi).where(CardEmi.card_id == card.id))
                ).all()
            )
            if s.emis_left
        ),
        ZERO,
    )
    stmt = CardStatement(
        card_id=card.id,
        statement_date=body.statement_date,
        due_date=body.due_date or debts.due_date_for(card, body.statement_date),
        total_due=body.total_due,
        min_due=body.min_due
        if body.min_due is not None
        else debts.min_due(card, body.total_due, emi_billed, body.interest_charged + body.fees),
        interest_charged=body.interest_charged,
        fees=body.fees,
        status=StatementStatus.paid if body.total_due == 0 else StatementStatus.unpaid,
    )
    session.add(stmt)
    try:
        await session.commit()
    except IntegrityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "A statement for that date exists") from exc
    return stmt


@router.post("/{card_id}/statements/{statement_id}/pay", response_model=StatementOut)
async def pay_statement(
    card_id: uuid.UUID, statement_id: uuid.UUID, body: PayIn, ctx: Writer, session: Session
) -> CardStatement:
    """Record a bill payment: bank debit + card credit (a transfer) and update the statement."""
    card = await owned(session, CreditCard, card_id, ctx)
    stmt = await session.get(CardStatement, statement_id)
    if stmt is None or stmt.card_id != card.id:
        raise not_found("Statement not found")
    source_id = body.from_account_id or card.pay_from_account_id
    if source_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Which account paid the bill?")
    await owned(session, Account, source_id, ctx)
    account = await session.get(Account, card.account_id)
    group = uuid.uuid4()
    category_id = await _card_payment_category(session, ctx.household_id)
    payee = f"{account.name if account else card.issuer} bill"
    for acc, direction in ((source_id, Direction.debit), (card.account_id, Direction.credit)):
        session.add(
            Transaction(
                household_id=ctx.household_id,
                account_id=acc,
                date=body.date,
                amount=body.amount,
                direction=direction,
                kind=TxnKind.card_payment,
                source=TxnSource.manual,
                category_id=category_id,
                member_id=ctx.member_id,
                payee=payee,
                transfer_group_id=group,
                card_statement_id=stmt.id,
                created_by=ctx.user_id,
            )
        )
    stmt.paid_amount += body.amount
    if stmt.paid_amount >= stmt.total_due:
        stmt.status = StatementStatus.paid
    elif stmt.paid_amount >= stmt.min_due:
        stmt.status = StatementStatus.min_paid
    else:
        stmt.status = StatementStatus.partial
    await session.commit()
    return stmt


# ---------- card EMIs ----------


@router.get("/{card_id}/emis", response_model=list[CardEmiOut])
async def list_emis(card_id: uuid.UUID, ctx: Reader, session: Session) -> list[CardEmiOut]:
    await owned(session, CreditCard, card_id, ctx)
    rows = (
        await session.scalars(
            select(CardEmi).where(CardEmi.card_id == card_id).order_by(CardEmi.first_emi_date)
        )
    ).all()
    today = date.today()
    return [_emi_out(e, today) for e in rows]


@router.post("/{card_id}/emis", response_model=CardEmiOut, status_code=201)
async def add_emi(card_id: uuid.UUID, body: CardEmiIn, ctx: Writer, session: Session) -> CardEmiOut:
    card = await owned(session, CreditCard, card_id, ctx)
    if body.disbursed_to_account_id is not None:
        await owned(session, Account, body.disbursed_to_account_id, ctx)
    item = CardEmi(card_id=card.id, **body.model_dump(exclude={"record_disbursal"}))
    session.add(item)
    if (
        body.kind is CardEmiKind.loan_on_card
        and body.record_disbursal
        and body.disbursed_to_account_id is not None
    ):
        group = uuid.uuid4()
        start = body.first_emi_date.replace(day=1)
        for acc, direction in (
            (card.account_id, Direction.debit),
            (body.disbursed_to_account_id, Direction.credit),
        ):
            session.add(
                Transaction(
                    household_id=ctx.household_id,
                    account_id=acc,
                    date=min(start, date.today()),
                    amount=body.principal,
                    direction=direction,
                    kind=TxnKind.transfer,
                    payee=f"Loan on card · {body.description}",
                    transfer_group_id=group,
                    member_id=ctx.member_id,
                    created_by=ctx.user_id,
                )
            )
    await session.commit()
    return _emi_out(item, date.today())


@router.delete("/{card_id}/emis/{emi_id}", status_code=204)
async def close_emi(card_id: uuid.UUID, emi_id: uuid.UUID, ctx: Writer, session: Session) -> None:
    """Mark an EMI closed (foreclosed or finished)."""
    card = await owned(session, CreditCard, card_id, ctx)
    item = await session.get(CardEmi, emi_id)
    if item is None or item.card_id != card.id:
        raise not_found("EMI not found")
    item.closed = True
    await session.commit()
