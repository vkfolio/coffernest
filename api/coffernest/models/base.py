"""Declarative base, shared column types and mixins."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import Date, DateTime, ForeignKey, MetaData, Numeric, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

# Stable constraint names so Alembic migrations stay deterministic.
NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

Money = Numeric(14, 2)
Rate = Numeric(7, 4)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)
    type_annotation_map = {  # noqa: RUF012
        Decimal: Money,
        datetime: DateTime(timezone=True),
        date: Date,
        uuid.UUID: UUID(as_uuid=True),
        dict[str, Any]: JSONB,
    }


def str_enum(enum: type[StrEnum]) -> SAEnum:
    """Store enums as VARCHAR + CHECK (no native PG enum): adding a value is a cheap migration."""
    return SAEnum(
        enum,
        native_enum=False,
        length=24,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )


class IdMixin:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class HouseholdMixin(IdMixin):
    @declared_attr
    def household_id(cls) -> Mapped[uuid.UUID]:
        return mapped_column(ForeignKey("household.id", ondelete="CASCADE"), index=True)


class SyncedMixin:
    """Last edit time, kept by the ``cn_touch`` trigger (migration 0008); ``/sync`` compares it."""

    updated_at: Mapped[datetime | None]
