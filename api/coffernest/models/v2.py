"""Inbox extraction, assistant chat, skills, and app connections (MCP tokens / OAuth)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from coffernest.models.base import Base, HouseholdMixin, IdMixin


class Extraction(HouseholdMixin, Base):
    """What the reader made of one dropped file."""

    __tablename__ = "extraction"

    attachment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("attachment.id", ondelete="CASCADE"), unique=True
    )
    # queued | reading | needs_password | review | done | failed
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    doc_type: Mapped[str] = mapped_column(String(30), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    result: Mapped[dict[str, Any]] = mapped_column(default=dict)
    provider: Mapped[str] = mapped_column(String(60), default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("member.id", ondelete="SET NULL")
    )
    updated_at: Mapped[datetime | None]


class ExtractionItem(HouseholdMixin, Base):
    """One proposed change: a transaction, a card statement, a loan, a repeating bill…"""

    __tablename__ = "extraction_item"

    extraction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("extraction.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer, default=0)
    # txn | card_statement | loan | recurring | account
    kind: Mapped[str] = mapped_column(String(20))
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    # auto_added | pending | approved | skipped | duplicate | failed
    state: Mapped[str] = mapped_column(String(20), default="pending")
    reason: Mapped[str] = mapped_column(Text, default="")
    created_ref: Mapped[dict[str, Any]] = mapped_column(default=dict)


class DocPassword(HouseholdMixin, Base):
    """A statement password the household chose to remember (Fernet-encrypted)."""

    __tablename__ = "doc_password"

    label: Mapped[str] = mapped_column(String(120), default="")
    ciphertext: Mapped[str] = mapped_column(Text)
    last_used_at: Mapped[datetime | None]


class ChatThread(HouseholdMixin, Base):
    __tablename__ = "chat_thread"

    member_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("member.id", ondelete="SET NULL")
    )
    title: Mapped[str] = mapped_column(String(160), default="New conversation")
    updated_at: Mapped[datetime | None]


class ChatMessage(IdMixin, Base):
    __tablename__ = "chat_message"

    thread_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chat_thread.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(12))  # user | assistant
    content: Mapped[str] = mapped_column(Text, default="")
    # Rich parts rendered by the client, e.g. {"type": "roadmap", ...}
    blocks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    tools_used: Mapped[list[str]] = mapped_column(ARRAY(String(60)), default=list)
    skills_used: Mapped[list[str]] = mapped_column(ARRAY(String(80)), default=list)
    verified: Mapped[bool] = mapped_column(Boolean, default=True)
    unverified_amounts: Mapped[list[str]] = mapped_column(ARRAY(String(40)), default=list)
    # Compact tool outputs, so later turns can re-check amounts the model repeats.
    evidence: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)


class SavedPlan(HouseholdMixin, Base):
    """A roadmap path the household chose to follow."""

    __tablename__ = "saved_plan"

    title: Mapped[str] = mapped_column(String(160))
    path_id: Mapped[str] = mapped_column(String(80))
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class SkillSetting(HouseholdMixin, Base):
    """Per-household switch for a built-in skill, or a custom skill the owner pasted in."""

    __tablename__ = "skill_setting"
    __table_args__ = (UniqueConstraint("household_id", "name"),)

    name: Mapped[str] = mapped_column(String(80))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    custom_body: Mapped[str | None] = mapped_column(Text)  # full SKILL.md for custom skills
    last_used_at: Mapped[datetime | None]


class AccessToken(HouseholdMixin, Base):
    """Personal access token for MCP clients (Codex, Claude Code, mcp-remote). Stored hashed."""

    __tablename__ = "access_token"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user_account.id", ondelete="CASCADE"))
    member_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("member.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(80))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    prefix: Mapped[str] = mapped_column(String(16))
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(40)), default=list)
    last_used_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]


class OAuthClient(Base):
    """A dynamically registered OAuth client (claude.ai, Claude Desktop, Codex…)."""

    __tablename__ = "oauth_client"

    client_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_name: Mapped[str] = mapped_column(String(120), default="")
    redirect_uris: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class OAuthCode(IdMixin, Base):
    __tablename__ = "oauth_code"

    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    client_id: Mapped[str] = mapped_column(ForeignKey("oauth_client.client_id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user_account.id", ondelete="CASCADE"))
    member_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("member.id", ondelete="CASCADE"))
    household_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("household.id", ondelete="CASCADE"))
    redirect_uri: Mapped[str] = mapped_column(Text)
    code_challenge: Mapped[str] = mapped_column(String(128))
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(40)), default=list)
    resource: Mapped[str] = mapped_column(Text, default="")
    expires_at: Mapped[datetime]
    used_at: Mapped[datetime | None]


class OAuthGrant(IdMixin, Base):
    """A refresh-token family issued to one OAuth client. Rotated on every use."""

    __tablename__ = "oauth_grant"

    client_id: Mapped[str] = mapped_column(ForeignKey("oauth_client.client_id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user_account.id", ondelete="CASCADE"))
    member_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("member.id", ondelete="CASCADE"))
    household_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("household.id", ondelete="CASCADE"), index=True
    )
    family_id: Mapped[uuid.UUID] = mapped_column(index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(40)), default=list)
    resource: Mapped[str] = mapped_column(Text, default="")
    expires_at: Mapped[datetime]
    last_used_at: Mapped[datetime | None]
    rotated_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]
