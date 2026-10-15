"""Users, households, membership, sessions, throttling and audit."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ARRAY, Boolean, ForeignKey, Integer, SmallInteger, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from coffernest.enums import Role
from coffernest.models.base import Base, IdMixin, SyncedMixin, str_enum


class User(IdMixin, Base):
    __tablename__ = "user_account"

    email: Mapped[str] = mapped_column(String(254), unique=True)
    display_name: Mapped[str] = mapped_column(String(80))
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # TOTP second factor (secret stored base32; recovery codes as SHA-256 hashes).
    totp_secret: Mapped[str | None] = mapped_column(String(64))
    totp_pending: Mapped[str | None] = mapped_column(String(64))
    totp_last_step: Mapped[int] = mapped_column(Integer, default=-1)
    recovery_codes: Mapped[list[str]] = mapped_column(ARRAY(String(64)), default=list)
    # Bumped on password change / "log out everywhere" so old access tokens die early.
    token_version: Mapped[int] = mapped_column(Integer, default=0)

    memberships: Mapped[list[Member]] = relationship(back_populates="user")


class Household(SyncedMixin, IdMixin, Base):
    __tablename__ = "household"

    name: Mapped[str] = mapped_column(String(80))
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    timezone: Mapped[str] = mapped_column(String(40), default="Asia/Kolkata")
    fy_start_month: Mapped[int] = mapped_column(SmallInteger, default=4)
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # Cushion kept out of "safe to spend".
    spend_buffer: Mapped[int] = mapped_column(Integer, default=5000)

    members: Mapped[list[Member]] = relationship(back_populates="household")


class Member(IdMixin, Base):
    __tablename__ = "member"
    __table_args__ = (UniqueConstraint("household_id", "user_id"),)

    household_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("household.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user_account.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[Role] = mapped_column(str_enum(Role))

    user: Mapped[User] = relationship(back_populates="memberships", lazy="joined")
    household: Mapped[Household] = relationship(back_populates="members")


class Invite(IdMixin, Base):
    __tablename__ = "invite"

    household_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("household.id", ondelete="CASCADE"), index=True
    )
    email: Mapped[str] = mapped_column(String(254))
    role: Mapped[Role] = mapped_column(str_enum(Role))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime]
    accepted_at: Mapped[datetime | None]
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("user_account.id", ondelete="CASCADE"))


class RefreshToken(IdMixin, Base):
    """Opaque rotating refresh token. Reusing a rotated token revokes the whole family."""

    __tablename__ = "refresh_token"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user_account.id", ondelete="CASCADE"), index=True
    )
    member_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("member.id", ondelete="CASCADE"))
    family_id: Mapped[uuid.UUID] = mapped_column(index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    device: Mapped[str] = mapped_column(String(120), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
    expires_at: Mapped[datetime]
    last_used_at: Mapped[datetime | None]
    rotated_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]


class LoginThrottle(Base):
    __tablename__ = "login_throttle"

    key: Mapped[str] = mapped_column(String(160), primary_key=True)
    fails: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime]
    last_failure: Mapped[datetime]


class AuditLog(IdMixin, Base):
    __tablename__ = "audit_log"

    household_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("household.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user_account.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(60))
    ip: Mapped[str] = mapped_column(String(64), default="")
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict)
