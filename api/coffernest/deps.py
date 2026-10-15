"""Request dependencies: the authenticated member and their household scope."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from coffernest.config import Settings, get_settings
from coffernest.db import get_session
from coffernest.enums import Role
from coffernest.models import User
from coffernest.models.base import HouseholdMixin
from coffernest.security.tokens import decode_access

Session = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


@dataclass(frozen=True)
class Ctx:
    user_id: uuid.UUID
    member_id: uuid.UUID
    household_id: uuid.UUID
    role: Role

    @property
    def can_write(self) -> bool:
        return self.role in (Role.owner, Role.adult)


def _unauthorized(detail: str = "Not authenticated") -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED, detail, headers={"WWW-Authenticate": "Bearer"}
    )


async def current_ctx(request: Request, session: Session, settings: SettingsDep) -> Ctx:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise _unauthorized()
    claims = decode_access(settings, token)
    if claims is None:
        raise _unauthorized("Invalid or expired token")
    user = await session.get(User, claims.user_id)
    if user is None or not user.is_active or user.token_version != claims.token_version:
        raise _unauthorized("Session ended")
    return Ctx(claims.user_id, claims.member_id, claims.household_id, claims.role)


async def writer_ctx(ctx: Annotated[Ctx, Depends(current_ctx)]) -> Ctx:
    if not ctx.can_write:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Viewers can't change data")
    return ctx


async def owner_ctx(ctx: Annotated[Ctx, Depends(current_ctx)]) -> Ctx:
    if ctx.role != Role.owner:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the household owner can do this")
    return ctx


Reader = Annotated[Ctx, Depends(current_ctx)]
Writer = Annotated[Ctx, Depends(writer_ctx)]
Owner = Annotated[Ctx, Depends(owner_ctx)]


def not_found(what: str = "Not found") -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, what)


async def owned[T: HouseholdMixin](
    session: AsyncSession, model: type[T], id_: uuid.UUID, ctx: Ctx
) -> T:
    """Load a household-scoped row, or 404 — never leak another household's rows."""
    row = await session.get(model, id_)
    if row is None or row.household_id != ctx.household_id:
        raise not_found(f"{model.__name__} not found")
    return row
