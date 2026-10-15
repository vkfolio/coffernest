"""Loans, credit cards, card statements and card EMIs."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, SmallInteger, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from coffernest.enums import CardEmiKind, LoanKind, PrepayMode, RateType, StatementStatus
from coffernest.models.base import Base, HouseholdMixin, IdMixin, Rate, SyncedMixin, str_enum


class Loan(SyncedMixin, HouseholdMixin, Base):
    """A term loan. The amortisation schedule is derived, never stored.

    For a loan already being repaid, enter ``principal`` = outstanding today, ``first_emi_date`` =
    the next EMI and ``tenure_months`` = EMIs left; ``original_principal`` keeps the progress bar
    honest.
    """

    __tablename__ = "loan"
    __table_args__ = (
        CheckConstraint("principal > 0", name="principal_positive"),
        CheckConstraint("tenure_months between 1 and 600", name="tenure_range"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("account.id", ondelete="CASCADE"), unique=True
    )
    kind: Mapped[LoanKind] = mapped_column(str_enum(LoanKind))
    lender: Mapped[str] = mapped_column(String(80), default="")
    principal: Mapped[Decimal]
    original_principal: Mapped[Decimal | None]
    rate_pct: Mapped[Decimal] = mapped_column(Rate)
    rate_type: Mapped[RateType] = mapped_column(str_enum(RateType), default=RateType.fixed)
    tenure_months: Mapped[int] = mapped_column(SmallInteger)
    # If the bank's EMI differs from the formula (rounding), store theirs.
    emi_override: Mapped[Decimal | None]
    first_emi_date: Mapped[date]
    # History before the loan was entered here: first EMI of the original loan, its tenure and
    # EMI. Rows before ``first_emi_date`` are reconstructed from these and shown as estimated.
    start_date: Mapped[date | None]
    original_tenure_months: Mapped[int | None] = mapped_column(SmallInteger)
    original_emi: Mapped[Decimal | None]
    funding_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("account.id", ondelete="SET NULL")
    )
    tax_80c: Mapped[bool] = mapped_column(Boolean, default=False)
    tax_24b: Mapped[bool] = mapped_column(Boolean, default=False)
    tax_80e: Mapped[bool] = mapped_column(Boolean, default=False)
    prepayment_penalty_pct: Mapped[Decimal] = mapped_column(Rate, default=Decimal("0"))
    closed: Mapped[bool] = mapped_column(Boolean, default=False)


class LoanRateChange(SyncedMixin, IdMixin, Base):
    __tablename__ = "loan_rate_change"

    loan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("loan.id", ondelete="CASCADE"), index=True
    )
    effective_date: Mapped[date]
    rate_pct: Mapped[Decimal] = mapped_column(Rate)
    # Banks usually keep the EMI and stretch the tenure on a hike.
    keep_emi: Mapped[bool] = mapped_column(Boolean, default=True)


class LoanPrepayment(SyncedMixin, IdMixin, Base):
    __tablename__ = "loan_prepayment"
    __table_args__ = (CheckConstraint("amount > 0", name="amount_positive"),)

    loan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("loan.id", ondelete="CASCADE"), index=True
    )
    date: Mapped[date]
    amount: Mapped[Decimal]
    mode: Mapped[PrepayMode] = mapped_column(str_enum(PrepayMode))
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("txn.id", ondelete="SET NULL")
    )


class CreditCard(SyncedMixin, HouseholdMixin, Base):
    __tablename__ = "credit_card"
    __table_args__ = (
        CheckConstraint("statement_day between 1 and 31", name="statement_day_range"),
        CheckConstraint("due_days between 1 and 60", name="due_days_range"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("account.id", ondelete="CASCADE"), unique=True
    )
    issuer: Mapped[str] = mapped_column(String(60))
    network: Mapped[str] = mapped_column(String(20), default="")
    credit_limit: Mapped[Decimal]
    statement_day: Mapped[int] = mapped_column(SmallInteger)
    # Payment due this many days after the statement date (typically 18–20).
    due_days: Mapped[int] = mapped_column(SmallInteger, default=20)
    monthly_rate_pct: Mapped[Decimal] = mapped_column(Rate, default=Decimal("3.75"))
    min_due_pct: Mapped[Decimal] = mapped_column(Rate, default=Decimal("5"))
    min_due_floor: Mapped[Decimal] = mapped_column(default=Decimal("200"))
    annual_fee: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    fee_waiver_spend: Mapped[Decimal | None]
    pay_from_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("account.id", ondelete="SET NULL")
    )


class CardStatement(SyncedMixin, IdMixin, Base):
    __tablename__ = "card_statement"
    __table_args__ = (UniqueConstraint("card_id", "statement_date", name="cycle"),)

    card_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("credit_card.id", ondelete="CASCADE"), index=True
    )
    statement_date: Mapped[date]
    due_date: Mapped[date]
    total_due: Mapped[Decimal]
    min_due: Mapped[Decimal]
    interest_charged: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    fees: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    status: Mapped[StatementStatus] = mapped_column(
        str_enum(StatementStatus), default=StatementStatus.unpaid
    )
    paid_amount: Mapped[Decimal] = mapped_column(default=Decimal("0"))


class CardEmi(SyncedMixin, IdMixin, Base):
    __tablename__ = "card_emi"
    __table_args__ = (CheckConstraint("principal > 0", name="principal_positive"),)

    card_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("credit_card.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[CardEmiKind] = mapped_column(str_enum(CardEmiKind))
    description: Mapped[str] = mapped_column(String(120))
    principal: Mapped[Decimal]
    rate_pct: Mapped[Decimal] = mapped_column(Rate)
    tenure_months: Mapped[int] = mapped_column(SmallInteger)
    first_emi_date: Mapped[date]
    processing_fee: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    gst_pct: Mapped[Decimal] = mapped_column(Rate, default=Decimal("18"))
    # Where a loan-on-card was disbursed.
    disbursed_to_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("account.id", ondelete="SET NULL")
    )
    closed: Mapped[bool] = mapped_column(Boolean, default=False)
