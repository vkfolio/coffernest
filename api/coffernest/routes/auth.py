"""/api/v1/auth — registration, login with optional TOTP, rotating refresh tokens, sessions.

Web clients get the refresh token as an HttpOnly cookie scoped to this router; native clients
send ``X-Client: native`` and receive it in the body instead.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Cookie, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from coffernest.config import Settings
from coffernest.deps import Reader, Session, SettingsDep
from coffernest.enums import Relation, Role
from coffernest.models import AuditLog, Household, Invite, Member, RefreshToken, User
from coffernest.money import check_currency, check_timezone
from coffernest.routes.people import person_for_member
from coffernest.security import throttle, totp
from coffernest.security.passwords import hash_password, needs_rehash, verify_password
from coffernest.security.tokens import AccessClaims, issue_access, new_opaque, sha256
from coffernest.services.erase import EraseError, erase_account, storage_prefix
from coffernest.services.seed import seed_categories
from coffernest.storage import get_storage

router = APIRouter(prefix="/auth", tags=["auth"])
log = logging.getLogger("coffernest.auth")

COOKIE = "cn_refresh"
COOKIE_PATH = "/api/v1/auth"
REUSE_GRACE_S = 15
ClientHeader = Annotated[Literal["web", "native"], Header(alias="X-Client")]
# Set only while the server builds its own demo account (coffernest.demo), which is the one
# way that email can sign up.
SEEDING_DEMO: ContextVar[bool] = ContextVar("seeding_demo", default=False)


# ---------- schemas ----------


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=200)
    display_name: str = Field(min_length=1, max_length=80)
    household_name: str = Field(default="Home", max_length=80)
    invite_token: str | None = None
    # Only used when this sign-up starts a new household; joining keeps the family's.
    currency: str | None = None
    timezone: str | None = None
    fy_start_month: int | None = Field(default=None, ge=1, le=12)

    @field_validator("currency")
    @classmethod
    def _currency(cls, v: str | None) -> str | None:
        return None if v is None else check_currency(v)

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, v: str | None) -> str | None:
        return None if v is None else check_timezone(v)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(max_length=200)
    totp_code: str | None = Field(default=None, max_length=20)
    device: str = Field(default="", max_length=120)


class RefreshIn(BaseModel):
    refresh_token: str | None = None


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    totp_enabled: bool


class SessionOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    refresh_token: str | None = None
    user: UserOut
    household_id: uuid.UUID
    member_id: uuid.UUID
    role: Role


class DeviceSession(BaseModel):
    family_id: uuid.UUID
    device: str
    ip: str
    last_used_at: datetime | None
    created_at: datetime
    current: bool


class PasswordChangeIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=10, max_length=200)


class DeleteAccountIn(BaseModel):
    password: str = Field(max_length=200)


class TotpSetupOut(BaseModel):
    secret: str
    uri: str


class CodeIn(BaseModel):
    code: str = Field(max_length=20)


class TotpDisableIn(BaseModel):
    password: str
    code: str = Field(max_length=20)


class RecoveryCodesOut(BaseModel):
    recovery_codes: list[str]


# ---------- helpers ----------


def _now() -> datetime:
    return datetime.now(UTC)


def _user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        totp_enabled=bool(user.totp_secret),
    )


def _secure(request: Request, settings: Settings) -> bool:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return proto == "https" or settings.env == "prod"


async def _issue(
    session: AsyncSession,
    settings: Settings,
    request: Request,
    response: Response,
    client: str,
    user: User,
    member: Member,
    device: str,
    family_id: uuid.UUID | None = None,
) -> SessionOut:
    opaque = new_opaque()
    session.add(
        RefreshToken(
            user_id=user.id,
            member_id=member.id,
            family_id=family_id or uuid.uuid4(),
            token_hash=sha256(opaque),
            device=device[:120] or request.headers.get("user-agent", "")[:120],
            ip=throttle.client_ip(request),
            expires_at=_now() + timedelta(seconds=settings.refresh_ttl_s),
            last_used_at=_now(),
        )
    )
    access = issue_access(
        settings,
        AccessClaims(user.id, member.id, member.household_id, member.role, user.token_version),
    )
    out = SessionOut(
        access_token=access,
        expires_in=settings.access_ttl_s,
        user=_user_out(user),
        household_id=member.household_id,
        member_id=member.id,
        role=member.role,
    )
    if client == "native":
        out.refresh_token = opaque
    else:
        response.set_cookie(
            COOKIE,
            opaque,
            max_age=settings.refresh_ttl_s,
            httponly=True,
            secure=_secure(request, settings),
            samesite="strict",
            path=COOKIE_PATH,
            domain=settings.cookie_domain,
        )
    return out


async def _primary_member(session: AsyncSession, user_id: uuid.UUID) -> Member:
    member = await session.scalar(
        select(Member).where(Member.user_id == user_id).order_by(Member.created_at).limit(1)
    )
    if member is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "No household for this account")
    return member


def _audit(
    session: AsyncSession,
    request: Request,
    action: str,
    user: User | None,
    household_id: uuid.UUID | None = None,
    **meta: str,
) -> None:
    session.add(
        AuditLog(
            household_id=household_id,
            user_id=user.id if user else None,
            action=action,
            ip=throttle.client_ip(request),
            meta=meta,
        )
    )


async def pending_invite(session: AsyncSession, email: str) -> Invite | None:
    """The oldest unexpired, unaccepted invite for this email, on any household."""
    invite: Invite | None = await session.scalar(
        select(Invite)
        .where(
            func.lower(Invite.email) == email.lower(),
            Invite.accepted_at.is_(None),
            Invite.expires_at > _now(),
        )
        .order_by(Invite.created_at)
        .limit(1)
    )
    return invite


async def has_real_users(session: AsyncSession, settings: Settings) -> bool:
    """Someone other than the server's own demo account has signed up."""
    query = select(User.id)
    if settings.demo_email:
        query = query.where(User.email != settings.demo_email.lower())
    return await session.scalar(query.limit(1)) is not None


# ---------- routes ----------


@router.post("/register", response_model=SessionOut, status_code=201)
async def register(
    body: RegisterIn,
    request: Request,
    response: Response,
    session: Session,
    settings: SettingsDep,
    client: ClientHeader = "web",
) -> SessionOut:
    email = body.email.lower()
    if settings.demo_email and email == settings.demo_email.lower() and not SEEDING_DEMO.get():
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This email is kept for the demo")
    invite: Invite | None = None
    if body.invite_token:
        invite = await session.scalar(
            select(Invite).where(Invite.token_hash == sha256(body.invite_token))
        )
        if (
            invite is None
            or invite.accepted_at is not None
            or invite.expires_at < _now()
            or invite.email.lower() != email
        ):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invite is invalid or expired")
    if await session.scalar(select(User.id).where(User.email == email)):
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this e-mail exists")
    if invite is None:
        # Added to a family by its admin: signing up with that email joins it.
        invite = await pending_invite(session, email)
    if (
        invite is None
        and not settings.allow_registration
        and not SEEDING_DEMO.get()
        and await has_real_users(session, settings)
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Ask your family's admin to add this email first"
        )

    user = User(
        email=email, display_name=body.display_name, password_hash=hash_password(body.password)
    )
    session.add(user)
    await session.flush()

    if invite is not None:
        member = Member(household_id=invite.household_id, user_id=user.id, role=invite.role)
        invite.accepted_at = _now()
    else:
        household = Household(name=body.household_name)
        if body.currency:
            household.currency = body.currency
        if body.timezone:
            household.timezone = body.timezone
        if body.fy_start_month:
            household.fy_start_month = body.fy_start_month
        session.add(household)
        await session.flush()
        await seed_categories(session, household.id)
        member = Member(household_id=household.id, user_id=user.id, role=Role.owner)
    session.add(member)
    await session.flush()
    await person_for_member(
        session, member, user.display_name, Relation.self_ if invite is None else Relation.other
    )
    _audit(session, request, "register", user, member.household_id)
    out = await _issue(session, settings, request, response, client, user, member, "")
    await session.commit()
    return out


@router.post("/login", response_model=SessionOut)
async def login(
    body: LoginIn,
    request: Request,
    response: Response,
    session: Session,
    settings: SettingsDep,
    client: ClientHeader = "web",
) -> SessionOut:
    email = body.email.lower()
    keys = [f"ip:{throttle.client_ip(request)}", f"acct:{email}"]
    wait = await throttle.retry_after(session, keys)
    if wait > 0:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many attempts. Try again later.",
            headers={"Retry-After": str(int(wait) + 1)},
        )

    user = await session.scalar(select(User).where(User.email == email))
    ok = verify_password(user.password_hash if user else None, body.password)

    async def fail(detail: str) -> HTTPException:
        await throttle.record_failure(session, keys[0], throttle.PER_IP)
        await throttle.record_failure(session, keys[1], throttle.PER_ACCOUNT)
        _audit(session, request, "login_failed", user, email=email)
        await session.commit()
        return HTTPException(status.HTTP_401_UNAUTHORIZED, detail)

    if not ok or user is None or not user.is_active:
        raise await fail("Wrong e-mail or password")
    if user.totp_secret:
        if not body.totp_code:
            # Password was right; ask for the second factor without counting a failure.
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "totp_required")
        if not totp.verify(user, body.totp_code):
            raise await fail("Wrong verification code")

    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(body.password)
    await throttle.record_success(session, keys)
    member = await _primary_member(session, user.id)
    _audit(session, request, "login", user, member.household_id)
    out = await _issue(session, settings, request, response, client, user, member, body.device)
    await session.commit()
    return out


@router.post("/refresh", response_model=SessionOut)
async def refresh(
    request: Request,
    response: Response,
    session: Session,
    settings: SettingsDep,
    body: RefreshIn | None = None,
    cookie: Annotated[str | None, Cookie(alias=COOKIE)] = None,
    client: ClientHeader = "web",
) -> SessionOut:
    opaque = (body.refresh_token if body else None) or cookie
    if not opaque:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "No refresh token")
    row = await session.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == sha256(opaque)).with_for_update()
    )
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unknown session")
    # Two tabs (or a double-mounted app) refreshing at once replay the same token within
    # moments. That is a race, not theft: allow it briefly instead of killing the session.
    racing = (
        row.rotated_at is not None
        and row.revoked_at is None
        and _now() - row.rotated_at < timedelta(seconds=REUSE_GRACE_S)
    )
    if (row.rotated_at is not None and not racing) or row.revoked_at is not None:
        # A spent token came back: someone copied it. Kill the whole device session.
        await _revoke_family(session, row.family_id)
        user = await session.get(User, row.user_id)
        _audit(session, request, "refresh_reuse", user)
        await session.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session revoked")
    if row.expires_at < _now():
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired")

    user = await session.get(User, row.user_id)
    member = await session.get(Member, row.member_id)
    if user is None or member is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session ended")
    row.rotated_at = row.rotated_at or _now()
    out = await _issue(
        session, settings, request, response, client, user, member, row.device, row.family_id
    )
    await session.commit()
    return out


async def _revoke_family(session: AsyncSession, family_id: uuid.UUID) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=_now())
    )


@router.post("/logout", status_code=204)
async def logout(
    response: Response,
    session: Session,
    body: RefreshIn | None = None,
    cookie: Annotated[str | None, Cookie(alias=COOKIE)] = None,
) -> None:
    opaque = (body.refresh_token if body else None) or cookie
    if opaque:
        row = await session.scalar(
            select(RefreshToken).where(RefreshToken.token_hash == sha256(opaque))
        )
        if row is not None:
            await _revoke_family(session, row.family_id)
            await session.commit()
    response.delete_cookie(COOKIE, path=COOKIE_PATH)


@router.post("/logout-all", status_code=204)
async def logout_all(ctx: Reader, request: Request, response: Response, session: Session) -> None:
    user = await session.get(User, ctx.user_id)
    assert user is not None
    user.token_version += 1
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=_now())
    )
    _audit(session, request, "logout_all", user, ctx.household_id)
    await session.commit()
    response.delete_cookie(COOKIE, path=COOKIE_PATH)


@router.get("/me", response_model=UserOut)
async def me(ctx: Reader, session: Session) -> UserOut:
    user = await session.get(User, ctx.user_id)
    assert user is not None
    return _user_out(user)


@router.delete("/me", status_code=204)
async def delete_me(
    body: DeleteAccountIn, ctx: Reader, response: Response, session: Session
) -> None:
    """Delete this account for good. A household with no other members is deleted with it,
    including its files. The only owner of a shared household must hand it over first (409)."""
    user = await session.get(User, ctx.user_id)
    assert user is not None
    if not verify_password(user.password_hash, body.password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Password is wrong")
    try:
        households = await erase_account(session, user.id)
    except EraseError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, exc.message) from exc
    await session.commit()
    response.delete_cookie(COOKIE, path=COOKIE_PATH)
    await remove_files(households)


async def remove_files(households: list[uuid.UUID]) -> None:
    """Best effort once the rows are gone: a storage hiccup must not undo a deletion."""
    storage = get_storage()
    for household_id in households:
        try:
            await asyncio.to_thread(storage.delete_prefix, storage_prefix(household_id))
        except Exception:
            log.exception("could not remove files for deleted household %s", household_id)


@router.get("/sessions", response_model=list[DeviceSession])
async def sessions(
    ctx: Reader,
    session: Session,
    cookie: Annotated[str | None, Cookie(alias=COOKIE)] = None,
) -> list[DeviceSession]:
    rows = (
        await session.scalars(
            select(RefreshToken)
            .where(
                RefreshToken.user_id == ctx.user_id,
                RefreshToken.rotated_at.is_(None),
                RefreshToken.revoked_at.is_(None),
                RefreshToken.expires_at > _now(),
            )
            .order_by(RefreshToken.last_used_at.desc())
        )
    ).all()
    mine = sha256(cookie) if cookie else None
    return [
        DeviceSession(
            family_id=r.family_id,
            device=r.device,
            ip=r.ip,
            last_used_at=r.last_used_at,
            created_at=r.created_at,
            current=r.token_hash == mine,
        )
        for r in rows
    ]


@router.delete("/sessions/{family_id}", status_code=204)
async def revoke_session(family_id: uuid.UUID, ctx: Reader, session: Session) -> None:
    await session.execute(
        update(RefreshToken)
        .where(
            RefreshToken.family_id == family_id,
            RefreshToken.user_id == ctx.user_id,
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=_now())
    )
    await session.commit()


@router.post("/password", status_code=204)
async def change_password(
    body: PasswordChangeIn, ctx: Reader, request: Request, session: Session
) -> None:
    user = await session.get(User, ctx.user_id)
    assert user is not None
    if not verify_password(user.password_hash, body.current_password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is wrong")
    user.password_hash = hash_password(body.new_password)
    user.token_version += 1
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=_now())
    )
    _audit(session, request, "password_changed", user, ctx.household_id)
    await session.commit()


@router.post("/totp/setup", response_model=TotpSetupOut)
async def totp_setup(ctx: Reader, session: Session) -> TotpSetupOut:
    user = await session.get(User, ctx.user_id)
    assert user is not None
    if user.totp_secret:
        raise HTTPException(status.HTTP_409_CONFLICT, "Two-factor is already on")
    data = totp.begin_setup(user)
    await session.commit()
    return TotpSetupOut(**data)


@router.post("/totp/confirm", response_model=RecoveryCodesOut)
async def totp_confirm(
    body: CodeIn, ctx: Reader, request: Request, session: Session
) -> RecoveryCodesOut:
    user = await session.get(User, ctx.user_id)
    assert user is not None
    codes = totp.confirm_setup(user, body.code)
    if codes is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Code didn't match. Try the next one.")
    _audit(session, request, "totp_enabled", user, ctx.household_id)
    await session.commit()
    return RecoveryCodesOut(recovery_codes=codes)


@router.post("/totp/disable", status_code=204)
async def totp_disable(
    body: TotpDisableIn, ctx: Reader, request: Request, session: Session
) -> None:
    user = await session.get(User, ctx.user_id)
    assert user is not None
    if not verify_password(user.password_hash, body.password) or not totp.verify(user, body.code):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Password or code is wrong")
    totp.disable(user)
    _audit(session, request, "totp_disabled", user, ctx.household_id)
    await session.commit()
