"""/api/v1/connections — AI apps connected over MCP: personal access tokens and OAuth grants."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update

from coffernest.deps import Reader, Session, SettingsDep, Writer
from coffernest.enums import Role
from coffernest.mcp_server import resource_url
from coffernest.models import AccessToken, OAuthClient, OAuthGrant
from coffernest.security.tokens import PAT_PREFIX, SCOPE_READ, SCOPE_WRITE, new_opaque, sha256

router = APIRouter(prefix="/connections", tags=["connections"])


class TokenOut(BaseModel):
    id: uuid.UUID
    name: str
    prefix: str
    scopes: list[str]
    created_at: datetime
    last_used_at: datetime | None


class TokenCreated(TokenOut):
    token: str  # shown once


class TokenIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    access: Literal["read", "write"] = "read"


class GrantOut(BaseModel):
    family_id: uuid.UUID
    client_name: str
    scopes: list[str]
    connected_at: datetime
    last_used_at: datetime | None


class ConnectionsOut(BaseModel):
    mcp_url: str
    tokens: list[TokenOut]
    apps: list[GrantOut]


def _token_out(row: AccessToken) -> TokenOut:
    return TokenOut(
        id=row.id,
        name=row.name,
        prefix=row.prefix,
        scopes=list(row.scopes),
        created_at=row.created_at,
        last_used_at=row.last_used_at,
    )


@router.get("", response_model=ConnectionsOut)
async def list_connections(ctx: Reader, session: Session, settings: SettingsDep) -> ConnectionsOut:
    tokens = (
        await session.scalars(
            select(AccessToken)
            .where(AccessToken.user_id == ctx.user_id, AccessToken.revoked_at.is_(None))
            .order_by(AccessToken.created_at.desc())
        )
    ).all()
    rows = (
        await session.execute(
            select(
                OAuthGrant.family_id,
                OAuthClient.client_name,
                func.min(OAuthGrant.created_at),
                func.max(OAuthGrant.last_used_at),
                func.max(OAuthGrant.scopes),
            )
            .join(OAuthClient, OAuthClient.client_id == OAuthGrant.client_id)
            .where(
                OAuthGrant.user_id == ctx.user_id,
                OAuthGrant.revoked_at.is_(None),
                OAuthGrant.rotated_at.is_(None),
                OAuthGrant.expires_at > datetime.now(UTC),
            )
            .group_by(OAuthGrant.family_id, OAuthClient.client_name)
        )
    ).all()
    return ConnectionsOut(
        mcp_url=resource_url(settings),
        tokens=[_token_out(t) for t in tokens],
        apps=[
            GrantOut(
                family_id=f, client_name=n, scopes=list(s or []), connected_at=c, last_used_at=u
            )
            for f, n, c, u, s in rows
        ],
    )


@router.post("/tokens", response_model=TokenCreated, status_code=201)
async def create_token(body: TokenIn, ctx: Writer, session: Session) -> TokenCreated:
    if body.access == "write" and ctx.role is Role.viewer:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Viewers can only make read-only tokens")
    count = await session.scalar(
        select(func.count())
        .select_from(AccessToken)
        .where(AccessToken.user_id == ctx.user_id, AccessToken.revoked_at.is_(None))
    )
    if (count or 0) >= 20:
        raise HTTPException(status.HTTP_409_CONFLICT, "Revoke an old token first (20 at most)")
    secret = PAT_PREFIX + new_opaque()
    row = AccessToken(
        household_id=ctx.household_id,
        user_id=ctx.user_id,
        member_id=ctx.member_id,
        name=body.name,
        token_hash=sha256(secret),
        prefix=secret[: len(PAT_PREFIX) + 4],
        scopes=[SCOPE_READ, SCOPE_WRITE] if body.access == "write" else [SCOPE_READ],
    )
    session.add(row)
    await session.commit()
    return TokenCreated(**_token_out(row).model_dump(), token=secret)


@router.delete("/tokens/{token_id}", status_code=204)
async def revoke_token(token_id: uuid.UUID, ctx: Reader, session: Session) -> None:
    row = await session.get(AccessToken, token_id)
    if row is None or row.user_id != ctx.user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Token not found")
    row.revoked_at = datetime.now(UTC)
    await session.commit()


@router.delete("/apps/{family_id}", status_code=204)
async def disconnect_app(family_id: uuid.UUID, ctx: Reader, session: Session) -> None:
    await session.execute(
        update(OAuthGrant)
        .where(
            OAuthGrant.family_id == family_id,
            OAuthGrant.user_id == ctx.user_id,
            OAuthGrant.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(UTC))
    )
    await session.commit()
