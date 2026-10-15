"""/api/v1/accounts"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from coffernest.deps import Reader, Session, Writer, owned
from coffernest.enums import LIABILITY_KINDS, AccountKind
from coffernest.models import Account, AuditLog, Loan
from coffernest.security import throttle
from coffernest.security.box import seal, unseal
from coffernest.services import debts
from coffernest.services.balances import balances

router = APIRouter(prefix="/accounts", tags=["accounts"])


AccountType = Literal["", "savings", "salary", "current", "nre", "nro", "fd", "ppf", "other"]
IFSC = Field(default="", pattern=r"^$|^[A-Za-z]{4}0[A-Za-z0-9]{6}$")
NUMBER = Field(default=None, pattern=r"^[0-9A-Za-z -]{4,34}$")


def _digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isalnum())


class AccountIn(BaseModel):
    kind: AccountKind
    name: str = Field(min_length=1, max_length=80)
    institution: str = Field(default="", max_length=80)
    last4: str = Field(default="", pattern=r"^\d{0,4}$")
    account_type: AccountType = ""
    ifsc: str = IFSC
    branch: str = Field(default="", max_length=160)
    account_number: str | None = NUMBER  # write-only: sealed, never returned in full
    notes: str = Field(default="", max_length=2000)
    owner_member_id: uuid.UUID | None = None
    opening_balance: Decimal = Field(default=Decimal("0"), max_digits=14, decimal_places=2)
    opening_date: date = Field(default_factory=date.today)
    include_in_safe_to_spend: bool = True
    sort: int = 0


class AccountPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    institution: str | None = Field(default=None, max_length=80)
    last4: str | None = Field(default=None, pattern=r"^\d{0,4}$")
    account_type: AccountType | None = None
    ifsc: str | None = Field(default=None, pattern=r"^$|^[A-Za-z]{4}0[A-Za-z0-9]{6}$")
    branch: str | None = Field(default=None, max_length=160)
    account_number: str | None = Field(default=None, pattern=r"^$|^[0-9A-Za-z -]{4,34}$")
    notes: str | None = Field(default=None, max_length=2000)
    owner_member_id: uuid.UUID | None = None
    opening_balance: Decimal | None = Field(default=None, max_digits=14, decimal_places=2)
    opening_date: date | None = None
    include_in_safe_to_spend: bool | None = None
    is_active: bool | None = None
    sort: int | None = None


class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: AccountKind
    name: str
    institution: str
    last4: str
    owner_member_id: uuid.UUID | None
    opening_balance: Decimal
    opening_date: date
    include_in_safe_to_spend: bool
    is_active: bool
    sort: int
    is_liability: bool = False
    # Assets: money held. Cards/loans: amount owed.
    balance: Decimal = Decimal("0")
    account_type: str = ""
    ifsc: str = ""
    branch: str = ""
    notes: str = ""
    has_account_number: bool = False
    account_number_masked: str = ""


class NumberOut(BaseModel):
    account_number: str


async def _out(session: Session, account: Account, bal: Decimal | None = None) -> AccountOut:
    out = AccountOut.model_validate(account)
    out.is_liability = account.kind in LIABILITY_KINDS
    out.has_account_number = bool(account.number_sealed)
    if account.number_sealed:
        out.account_number_masked = "XXXXXX" + (account.last4 or "")
    if account.kind is AccountKind.loan:
        loan = await session.scalar(select(Loan).where(Loan.account_id == account.id))
        if loan is not None:
            schedule = await debts.loan_schedule(session, loan)
            out.balance = schedule.outstanding_on(date.today())
    elif bal is not None:
        out.balance = bal
    return out


@router.get("", response_model=list[AccountOut])
async def list_accounts(
    ctx: Reader, session: Session, include_inactive: bool = False
) -> list[AccountOut]:
    query = select(Account).where(Account.household_id == ctx.household_id)
    if not include_inactive:
        query = query.where(Account.is_active.is_(True))
    rows = (await session.scalars(query.order_by(Account.sort, Account.created_at))).all()
    bals = await balances(session, ctx.household_id)
    return [await _out(session, a, bals.get(a.id)) for a in rows]


@router.post("", response_model=AccountOut, status_code=201)
async def create_account(body: AccountIn, ctx: Writer, session: Session) -> AccountOut:
    if body.kind in (AccountKind.loan, AccountKind.credit_card):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Create loans via /loans and cards via /cards"
        )
    data = body.model_dump(exclude={"account_number"})
    data["ifsc"] = data["ifsc"].upper()
    account = Account(household_id=ctx.household_id, **data)
    if body.account_number:
        number = _digits(body.account_number)
        account.number_sealed = seal(number)
        account.last4 = number[-4:]
    session.add(account)
    await session.commit()
    return await _out(session, account, account.opening_balance)


@router.get("/{account_id}", response_model=AccountOut)
async def get_account(account_id: uuid.UUID, ctx: Reader, session: Session) -> AccountOut:
    account = await owned(session, Account, account_id, ctx)
    bals = await balances(session, ctx.household_id)
    return await _out(session, account, bals.get(account.id))


@router.patch("/{account_id}", response_model=AccountOut)
async def patch_account(
    account_id: uuid.UUID, body: AccountPatch, ctx: Writer, session: Session
) -> AccountOut:
    account = await owned(session, Account, account_id, ctx)
    changes = body.model_dump(exclude_unset=True)
    if "account_number" in changes:
        number = _digits(changes.pop("account_number") or "")
        account.number_sealed = seal(number) if number else None
        if number:
            account.last4 = number[-4:]
    if changes.get("ifsc"):
        changes["ifsc"] = changes["ifsc"].upper()
    for key, value in changes.items():
        setattr(account, key, value)
    await session.commit()
    bals = await balances(session, ctx.household_id)
    return await _out(session, account, bals.get(account.id))


@router.get("/{account_id}/number", response_model=NumberOut)
async def reveal_number(
    account_id: uuid.UUID, request: Request, ctx: Writer, session: Session
) -> NumberOut:
    """The full account number, for the member who asks. Every reveal is audited."""
    account = await owned(session, Account, account_id, ctx)
    plain = unseal(account.number_sealed) if account.number_sealed else None
    if not plain:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No account number saved")
    session.add(
        AuditLog(
            household_id=ctx.household_id,
            user_id=ctx.user_id,
            action="account_number_revealed",
            ip=throttle.client_ip(request),
            meta={"account": account.name},
        )
    )
    await session.commit()
    return NumberOut(account_number=plain)


@router.delete("/{account_id}", status_code=204)
async def delete_account(account_id: uuid.UUID, ctx: Writer, session: Session) -> None:
    """Archive (keeps history). Hard delete is intentionally not offered."""
    account = await owned(session, Account, account_id, ctx)
    account.is_active = False
    await session.commit()
