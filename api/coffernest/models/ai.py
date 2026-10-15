"""AI suggestions, stored so the household sees the same advice until it is refreshed."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from coffernest.models.base import Base, HouseholdMixin


class AiSuggestion(HouseholdMixin, Base):
    __tablename__ = "ai_suggestion"

    batch_id: Mapped[str] = mapped_column(String(36), index=True)
    title: Mapped[str] = mapped_column(String(160))
    body: Mapped[str] = mapped_column(Text, default="")
    action: Mapped[str] = mapped_column(String(300), default="")
    severity: Mapped[str] = mapped_column(String(10), default="info")
    verified: Mapped[bool] = mapped_column(Boolean, default=True)
    # Which tools were called and the ₹ amounts that couldn't be traced to their output.
    evidence: Mapped[dict[str, Any]] = mapped_column(default=dict)
    provider: Mapped[str] = mapped_column(String(40), default="")
    dismissed_at: Mapped[datetime | None]
    helpful: Mapped[bool | None]
