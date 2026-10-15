"""People an entry can be for (kids too: they have no login), and learned sorting rules."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Integer, SmallInteger, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from coffernest.enums import Relation, RuleSource
from coffernest.models.base import Base, HouseholdMixin, SyncedMixin, str_enum


class Person(SyncedMixin, HouseholdMixin, Base):
    __tablename__ = "person"
    __table_args__ = (UniqueConstraint("household_id", "name", name="uq_person_household_id"),)

    name: Mapped[str] = mapped_column(String(40))
    relation: Mapped[Relation] = mapped_column(str_enum(Relation), default=Relation.other)
    # The household member this person signs in as, if any (the owner, a spouse).
    member_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("member.id", ondelete="SET NULL")
    )
    color_slot: Mapped[int] = mapped_column(SmallInteger, default=8)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)


class CategoryRule(HouseholdMixin, Base):
    """'Entries from this payee go in this category (and are for this person).'"""

    __tablename__ = "category_rule"
    __table_args__ = (
        UniqueConstraint("household_id", "match", name="uq_category_rule_household_id"),
    )

    # A normalised payee key (see services.categorize.payee_key), e.g. "swiggy".
    match: Mapped[str] = mapped_column(String(60))
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("category.id", ondelete="CASCADE")
    )
    person_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("person.id", ondelete="SET NULL")
    )
    source: Mapped[RuleSource] = mapped_column(str_enum(RuleSource), default=RuleSource.learned)
    hits: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
