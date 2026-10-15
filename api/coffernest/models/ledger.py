"""Accounts, categories, transactions and recurring rules."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from coffernest.enums import (
    AccountKind,
    CategoryKind,
    Direction,
    TxnKind,
    TxnSource,
    TxnStatus,
)
from coffernest.models.base import Base, HouseholdMixin, SyncedMixin, str_enum

Day = date  # Transaction has a column called `date`


class Account(SyncedMixin, HouseholdMixin, Base):
    __tablename__ = "account"

    kind: Mapped[AccountKind] = mapped_column(str_enum(AccountKind))
    name: Mapped[str] = mapped_column(String(80))
    institution: Mapped[str] = mapped_column(String(80), default="")
    last4: Mapped[str] = mapped_column(String(4), default="")
    owner_member_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("member.id", ondelete="SET NULL")
    )
    # Balance before the first tracked transaction. For liabilities this is the amount owed.
    opening_balance: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    opening_date: Mapped[date]
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    include_in_safe_to_spend: Mapped[bool] = mapped_column(Boolean, default=True)
    sort: Mapped[int] = mapped_column(SmallInteger, default=0)
    # Bank details. IFSC and branch are public; the full number is sealed (Fernet) and only
    # ever shown masked unless a member asks to reveal it. AI tools only see last4.
    account_type: Mapped[str] = mapped_column(String(20), default="", server_default="")
    ifsc: Mapped[str] = mapped_column(String(11), default="", server_default="")
    branch: Mapped[str] = mapped_column(String(160), default="", server_default="")
    number_sealed: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str] = mapped_column(Text, default="", server_default="")


class Category(SyncedMixin, HouseholdMixin, Base):
    __tablename__ = "category"
    __table_args__ = (UniqueConstraint("household_id", "parent_id", "name", name="name"),)

    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("category.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(60))
    kind: Mapped[CategoryKind] = mapped_column(str_enum(CategoryKind))
    # Fixed = EMIs, rent, fees; flexible = groceries, dining. Drives safe-to-spend and budgets.
    is_fixed: Mapped[bool] = mapped_column(Boolean, default=False)
    color_slot: Mapped[int] = mapped_column(SmallInteger, default=8)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)


class RecurringRule(SyncedMixin, HouseholdMixin, Base):
    __tablename__ = "recurring_rule"
    __table_args__ = (CheckConstraint("amount > 0", name="amount_positive"),)

    name: Mapped[str] = mapped_column(String(80))
    amount: Mapped[Decimal]
    amount_is_estimate: Mapped[bool] = mapped_column(Boolean, default=False)
    direction: Mapped[Direction] = mapped_column(str_enum(Direction))
    kind: Mapped[TxnKind] = mapped_column(str_enum(TxnKind))
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("account.id", ondelete="CASCADE"))
    # Set for transfers / card payments / EMIs: the account that receives the money.
    to_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("account.id", ondelete="SET NULL")
    )
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("category.id", ondelete="SET NULL")
    )
    member_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("member.id", ondelete="SET NULL")
    )
    # Who it's for (a child, the spouse); empty = the whole family.
    person_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("person.id", ondelete="SET NULL")
    )
    payee: Mapped[str] = mapped_column(String(120), default="")
    # RFC 5545 RRULE body, e.g. "FREQ=MONTHLY;BYMONTHDAY=5" (no DTSTART; that's `start_date`).
    rrule: Mapped[str] = mapped_column(String(255))
    start_date: Mapped[date]
    end_date: Mapped[date | None]
    skipped_dates: Mapped[list[date]] = mapped_column(ARRAY(Date), default=list)
    remind_days_before: Mapped[int] = mapped_column(SmallInteger, default=2)
    # Auto-debits / salary credits: occurrences clear themselves once their date passes.
    auto_post: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    materialized_until: Mapped[date | None]
    loan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("loan.id", ondelete="CASCADE"))


class Transaction(HouseholdMixin, Base):
    __tablename__ = "txn"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        Index("ix_txn_household_date", "household_id", "date"),
        Index("ix_txn_account_date", "account_id", "date"),
        Index("ix_txn_tags", "tags", postgresql_using="gin"),
        # One materialised row per rule occurrence → materialisation is idempotent.
        Index(
            "uq_txn_rule_occurrence",
            "recurring_rule_id",
            "occurrence_date",
            "account_id",
            unique=True,
            postgresql_where=text("recurring_rule_id IS NOT NULL AND deleted_at IS NULL"),
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("account.id", ondelete="CASCADE"))
    date: Mapped[Day]
    amount: Mapped[Decimal]
    direction: Mapped[Direction] = mapped_column(str_enum(Direction))
    kind: Mapped[TxnKind] = mapped_column(str_enum(TxnKind))
    status: Mapped[TxnStatus] = mapped_column(str_enum(TxnStatus), default=TxnStatus.cleared)
    source: Mapped[TxnSource] = mapped_column(str_enum(TxnSource), default=TxnSource.manual)
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("category.id", ondelete="SET NULL"), index=True
    )
    member_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("member.id", ondelete="SET NULL")
    )
    # Who it's for (a child, the spouse); empty = the whole family.
    person_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("person.id", ondelete="SET NULL"), index=True
    )
    payee: Mapped[str] = mapped_column(String(120), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(40)), default=list)
    # Both legs of a transfer share a group id.
    transfer_group_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    recurring_rule_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("recurring_rule.id", ondelete="SET NULL")
    )
    occurrence_date: Mapped[Day | None]
    loan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("loan.id", ondelete="SET NULL"))
    card_statement_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("card_statement.id", ondelete="SET NULL")
    )
    import_ref: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user_account.id", ondelete="SET NULL")
    )
    updated_at: Mapped[datetime | None]
    deleted_at: Mapped[datetime | None]
