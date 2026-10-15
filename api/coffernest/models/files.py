"""Uploaded bills/statements (bytes live in S3) and notifications."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from coffernest.enums import AttachmentKind, UploadStatus
from coffernest.models.base import Base, HouseholdMixin, IdMixin, str_enum


class Attachment(HouseholdMixin, Base):
    __tablename__ = "attachment"

    object_key: Mapped[str] = mapped_column(String(255), unique=True)
    filename: Mapped[str] = mapped_column(String(200))
    mime: Mapped[str] = mapped_column(String(100))
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    kind: Mapped[AttachmentKind] = mapped_column(str_enum(AttachmentKind))
    status: Mapped[UploadStatus] = mapped_column(
        str_enum(UploadStatus), default=UploadStatus.pending
    )
    note: Mapped[str] = mapped_column(Text, default="")
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("txn.id", ondelete="SET NULL"), index=True
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("account.id", ondelete="SET NULL"), index=True
    )
    card_statement_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("card_statement.id", ondelete="SET NULL")
    )
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user_account.id", ondelete="SET NULL")
    )
    # Inbox: files dropped for reading. sha256 spots the same file dropped twice.
    sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(16), default="upload")


class PushSubscription(IdMixin, Base):
    __tablename__ = "push_subscription"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user_account.id", ondelete="CASCADE"), index=True
    )
    endpoint: Mapped[str] = mapped_column(Text, unique=True)
    p256dh: Mapped[str] = mapped_column(String(200))
    auth: Mapped[str] = mapped_column(String(100))
    user_agent: Mapped[str] = mapped_column(String(200), default="")


class Notification(HouseholdMixin, Base):
    __tablename__ = "notification"

    kind: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(160))
    body: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(String(255), default="/")
    # e.g. "due:<rule>:<date>:d2" — the unique key makes reminder jobs idempotent.
    dedupe_key: Mapped[str] = mapped_column(String(160), unique=True)
    read_at: Mapped[datetime | None]
    pushed_at: Mapped[datetime | None]
