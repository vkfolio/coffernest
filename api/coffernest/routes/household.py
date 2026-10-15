"""/api/v1/household — settings, members and invites."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import func, select

from coffernest.deps import Owner, Reader, Session, not_found
from coffernest.enums import Role
from coffernest.models import Household, Invite, Member, User
from coffernest.routes.auth import pending_invite, remove_files
from coffernest.security.passwords import verify_password
from coffernest.security.tokens import new_opaque, sha256
from coffernest.services.erase import erase_household

router = APIRouter(prefix="/household", tags=["household"])


class HouseholdOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    currency: str
    timezone: str
    fy_start_month: int
    ai_enabled: bool
    spend_buffer: int


class HouseholdPatch(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    ai_enabled: bool | None = None
    spend_buffer: int | None = Field(default=None, ge=0, le=10_000_000)


class MemberOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    display_name: str
    email: str
    role: Role


class InviteIn(BaseModel):
    email: EmailStr
    role: Role = Role.adult


class InviteOut(BaseModel):
    id: uuid.UUID
    email: str
    role: Role
    expires_at: datetime
    # Only returned once, on creation. Not needed any more: signing up with the email is enough.
    token: str | None = None


class RolePatch(BaseModel):
    role: Role


class DeleteHouseholdIn(BaseModel):
    password: str = Field(max_length=200)


@router.get("", response_model=HouseholdOut)
async def get_household(ctx: Reader, session: Session) -> Household:
    household = await session.get(Household, ctx.household_id)
    if household is None:
        raise not_found()
    return household


@router.delete("", status_code=204)
async def delete_household(body: DeleteHouseholdIn, ctx: Owner, session: Session) -> None:
    """Owner only. Deletes every entry, account, file and member of this household for good.
    Members who belong to no other household lose their sign-in too."""
    user = await session.get(User, ctx.user_id)
    assert user is not None
    if not verify_password(user.password_hash, body.password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Password is wrong")
    await erase_household(session, ctx.household_id)
    await session.commit()
    await remove_files([ctx.household_id])


@router.patch("", response_model=HouseholdOut)
async def patch_household(body: HouseholdPatch, ctx: Owner, session: Session) -> Household:
    household = await session.get(Household, ctx.household_id)
    if household is None:
        raise not_found()
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(household, key, value)
    await session.commit()
    return household


@router.get("/members", response_model=list[MemberOut])
async def members(ctx: Reader, session: Session) -> list[MemberOut]:
    rows = (
        await session.scalars(
            select(Member)
            .where(Member.household_id == ctx.household_id)
            .order_by(Member.created_at)
        )
    ).all()
    return [
        MemberOut(
            id=m.id,
            user_id=m.user_id,
            display_name=m.user.display_name,
            email=m.user.email,
            role=m.role,
        )
        for m in rows
    ]


@router.patch("/members/{member_id}", response_model=MemberOut)
async def set_role(
    member_id: uuid.UUID, body: RolePatch, ctx: Owner, session: Session
) -> MemberOut:
    member = await session.get(Member, member_id)
    if member is None or member.household_id != ctx.household_id:
        raise not_found("Member not found")
    if member.role == Role.owner and body.role != Role.owner:
        owners = await session.scalar(
            select(func.count()).where(
                Member.household_id == ctx.household_id, Member.role == Role.owner
            )
        )
        if (owners or 0) <= 1:
            raise HTTPException(status.HTTP_409_CONFLICT, "A household needs at least one owner")
    member.role = body.role
    await session.commit()
    return MemberOut(
        id=member.id,
        user_id=member.user_id,
        display_name=member.user.display_name,
        email=member.user.email,
        role=member.role,
    )


@router.delete("/members/{member_id}", status_code=204)
async def remove_member(member_id: uuid.UUID, ctx: Owner, session: Session) -> None:
    member = await session.get(Member, member_id)
    if member is None or member.household_id != ctx.household_id:
        raise not_found("Member not found")
    if member.id == ctx.member_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "You can't remove yourself")
    await session.delete(member)
    await session.commit()


@router.get("/invites", response_model=list[InviteOut])
async def invites(ctx: Owner, session: Session) -> list[InviteOut]:
    rows = (
        await session.scalars(
            select(Invite).where(
                Invite.household_id == ctx.household_id,
                Invite.accepted_at.is_(None),
                Invite.expires_at > datetime.now(UTC),
            )
        )
    ).all()
    return [InviteOut(id=i.id, email=i.email, role=i.role, expires_at=i.expires_at) for i in rows]


@router.post("/invites", response_model=InviteOut, status_code=201)
async def create_invite(body: InviteIn, ctx: Owner, session: Session) -> InviteOut:
    """Add someone to the family: when they sign up with this email they join as a member.
    It stays open until they do, or the admin removes it."""
    email = body.email.lower()
    if await session.scalar(select(User.id).where(func.lower(User.email) == email)):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "That person already has a Coffernest account on this server"
        )
    if await pending_invite(session, email):
        raise HTTPException(status.HTTP_409_CONFLICT, "That email has already been added")
    token = new_opaque()
    invite = Invite(
        household_id=ctx.household_id,
        email=email,
        role=body.role,
        token_hash=sha256(token),
        expires_at=datetime.now(UTC) + timedelta(days=3650),
        created_by=ctx.user_id,
    )
    session.add(invite)
    await session.commit()
    return InviteOut(
        id=invite.id,
        email=invite.email,
        role=invite.role,
        expires_at=invite.expires_at,
        token=token,
    )


@router.delete("/invites/{invite_id}", status_code=204)
async def cancel_invite(invite_id: uuid.UUID, ctx: Owner, session: Session) -> None:
    invite = await session.get(Invite, invite_id)
    if invite is None or invite.household_id != ctx.household_id:
        raise not_found("Invite not found")
    await session.delete(invite)
    await session.commit()
