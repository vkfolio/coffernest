"""/api/v1/transactions — the ledger.

Creating an entry first tries to *fulfil* a scheduled recurring occurrence (same account and
direction, ±3 days, ±5% amount) so one rent payment never shows up twice.
Clients may supply ``id`` (a UUID they generate) to make retries and the PWA offline queue
idempotent.
"""

from __future__ import annotations

import base64
import logging
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from coffernest import db
from coffernest.ai.providers import AIUnavailable, get_llm
from coffernest.config import get_settings
from coffernest.deps import Ctx, Reader, Session, Writer, owned
from coffernest.enums import CategoryKind, Direction, TxnKind, TxnSource, TxnStatus
from coffernest.models import Account, Category, Household, Person, RecurringRule, Transaction
from coffernest.services import categorize, describe
from coffernest.services.recurring import find_match

Day = date  # field names "date" would shadow the type inside Pydantic models

router = APIRouter(prefix="/transactions", tags=["transactions"])
log = logging.getLogger("coffernest.transactions")

Amount = Field(gt=0, max_digits=14, decimal_places=2)


class TxnIn(BaseModel):
    id: uuid.UUID | None = None
    account_id: uuid.UUID
    date: Day
    amount: Decimal = Amount
    direction: Direction
    kind: TxnKind | None = None
    category_id: uuid.UUID | None = None
    member_id: uuid.UUID | None = None
    person_id: uuid.UUID | None = None
    payee: str = Field(default="", max_length=120)
    note: str = Field(default="", max_length=2000)
    tags: list[str] = Field(default_factory=list, max_length=20)
    # Sort it (category, who it's for) from the payee when no category is given.
    auto_sort: bool = True
    # A transfer (bank → card payment, savings sweep): the other account gets the opposite leg.
    to_account_id: uuid.UUID | None = None
    loan_id: uuid.UUID | None = None
    status: TxnStatus = TxnStatus.cleared
    match_scheduled: bool = True

    @model_validator(mode="after")
    def _check(self) -> TxnIn:
        if self.to_account_id is not None and self.to_account_id == self.account_id:
            raise ValueError("Transfer needs two different accounts")
        self.tags = sorted({t.strip().lower()[:40] for t in self.tags if t.strip()})
        return self


class TxnPatch(BaseModel):
    date: Day | None = None
    amount: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    category_id: uuid.UUID | None = None
    member_id: uuid.UUID | None = None
    person_id: uuid.UUID | None = None
    payee: str | None = Field(default=None, max_length=120)
    note: str | None = Field(default=None, max_length=2000)
    tags: list[str] | None = Field(default=None, max_length=20)
    kind: TxnKind | None = None


class ConfirmIn(BaseModel):
    amount: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    date: Day | None = None


class TxnOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    account_id: uuid.UUID
    date: Day
    amount: Decimal
    direction: Direction
    kind: TxnKind
    status: TxnStatus
    source: TxnSource
    category_id: uuid.UUID | None
    member_id: uuid.UUID | None
    person_id: uuid.UUID | None = None
    payee: str
    note: str
    tags: list[str]
    transfer_group_id: uuid.UUID | None
    recurring_rule_id: uuid.UUID | None
    occurrence_date: Day | None
    loan_id: uuid.UUID | None
    created_at: datetime
    matched: bool = False
    # Set by the sorter: the category came from the payee, not from the person entering it.
    auto_sorted: bool = False
    # After a category change: other entries from the same payee still elsewhere.
    similar: int = 0


class TxnTotals(BaseModel):
    model_config = ConfigDict(serialize_by_alias=True)

    count: int
    in_: Decimal = Field(serialization_alias="in")
    out: Decimal


class TxnPage(BaseModel):
    items: list[TxnOut]
    next_cursor: str | None
    totals: TxnTotals | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _default_kind(direction: Direction, category: Category | None, transfer: bool) -> TxnKind:
    if transfer:
        return TxnKind.transfer
    if category is not None:
        if category.kind is CategoryKind.debt:
            return TxnKind.emi
        if category.kind is CategoryKind.income:
            return TxnKind.income
        if category.kind is CategoryKind.transfer:
            return TxnKind.transfer
    return TxnKind.income if direction is Direction.credit else TxnKind.expense


async def _group(session: AsyncSession, txn: Transaction) -> list[Transaction]:
    if txn.transfer_group_id is None:
        return [txn]
    rows = await session.scalars(
        select(Transaction).where(Transaction.transfer_group_id == txn.transfer_group_id)
    )
    return list(rows.all())


def _encode(txn: Transaction) -> str:
    return base64.urlsafe_b64encode(f"{txn.date.isoformat()}|{txn.id}".encode()).decode()


def _decode(cursor: str) -> tuple[date, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        day, ident = raw.split("|")
        return date.fromisoformat(day), uuid.UUID(ident)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Bad cursor") from exc


@router.get("", response_model=TxnPage)
async def list_transactions(
    ctx: Reader,
    session: Session,
    start: date | None = Query(default=None, alias="from"),
    end: date | None = Query(default=None, alias="to"),
    account_id: uuid.UUID | None = None,
    category_id: uuid.UUID | None = None,
    status_: TxnStatus | None = Query(default=None, alias="status"),
    q: str | None = Query(default=None, max_length=80),
    tag: str | None = None,
    direction: Direction | None = None,
    person_id: uuid.UUID | None = None,
    family: bool = False,
    include_totals: bool = False,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> TxnPage:
    query = select(Transaction).where(
        Transaction.household_id == ctx.household_id, Transaction.deleted_at.is_(None)
    )
    if start:
        query = query.where(Transaction.date >= start)
    if end:
        query = query.where(Transaction.date <= end)
    if account_id:
        query = query.where(Transaction.account_id == account_id)
    if category_id:
        sub = select(Category.id).where(
            or_(Category.id == category_id, Category.parent_id == category_id)
        )
        query = query.where(Transaction.category_id.in_(sub))
    if status_:
        query = query.where(Transaction.status == status_)
    if q:
        like = f"%{q}%"
        query = query.where(or_(Transaction.payee.ilike(like), Transaction.note.ilike(like)))
    if tag:
        query = query.where(Transaction.tags.contains([tag.lower()]))
    if person_id:
        query = query.where(Transaction.person_id == person_id)
    elif family:  # for no one in particular
        query = query.where(Transaction.person_id.is_(None))
    if direction:
        query = query.where(Transaction.direction == direction)
        if not account_id:
            # Money in / out: moves between your own accounts are neither.
            query = query.where(Transaction.kind.not_in((TxnKind.transfer, TxnKind.card_payment)))
    totals = None
    if include_totals:
        # Across every page of the filter, not just this one. Moves between your own accounts
        # are neither in nor out unless one account is being looked at.
        sums = query.where(Transaction.status == TxnStatus.cleared)
        if not account_id:
            sums = sums.where(Transaction.kind.not_in((TxnKind.transfer, TxnKind.card_payment)))
        flat = sums.subquery()
        row = (
            await session.execute(
                select(
                    func.count(),
                    func.coalesce(
                        func.sum(flat.c.amount).filter(flat.c.direction == Direction.credit), 0
                    ),
                    func.coalesce(
                        func.sum(flat.c.amount).filter(flat.c.direction == Direction.debit), 0
                    ),
                ).select_from(flat)
            )
        ).one()
        totals = TxnTotals(count=row[0], in_=row[1], out=row[2])
    if cursor:
        day, ident = _decode(cursor)
        query = query.where(
            or_(Transaction.date < day, and_(Transaction.date == day, Transaction.id < ident))
        )
    rows = list(
        (
            await session.scalars(
                query.order_by(Transaction.date.desc(), Transaction.id.desc()).limit(limit + 1)
            )
        ).all()
    )
    more = len(rows) > limit
    rows = rows[:limit]
    return TxnPage(
        items=[TxnOut.model_validate(r) for r in rows],
        next_cursor=_encode(rows[-1]) if more and rows else None,
        totals=totals,
    )


async def _check_refs(session: AsyncSession, ctx: Ctx, body: TxnIn | TxnPatch) -> Category | None:
    category = None
    if body.category_id is not None:
        category = await owned(session, Category, body.category_id, ctx)
    if body.person_id is not None:
        await owned(session, Person, body.person_id, ctx)
    if isinstance(body, TxnIn):
        await owned(session, Account, body.account_id, ctx)
        if body.to_account_id is not None:
            await owned(session, Account, body.to_account_id, ctx)
    return category


class DescribeIn(BaseModel):
    text: str = Field(min_length=1, max_length=300)


class DraftOut(BaseModel):
    amount: Decimal | None
    payee: str
    note: str
    date: Day
    category_id: uuid.UUID | None
    person_id: uuid.UUID | None
    confidence: float
    source: str


@router.post("/describe", response_model=DraftOut)
async def describe_entry(body: DescribeIn, ctx: Reader, session: Session) -> DraftOut:
    """A draft entry from a plain description ("aarav uniform 1200"). Nothing is saved."""
    book = await categorize.load_book(session, ctx.household_id)
    draft = describe.parse(book, body.text)
    if draft.category_id is None and len(body.text.split()) >= 2:
        household = await session.get(Household, ctx.household_id)
        if household is not None and household.ai_enabled:
            try:
                llm = get_llm(get_settings(), db.sessionmaker(), ctx.household_id)
                draft = await describe.with_ai(book, draft, llm)
            except AIUnavailable:
                pass
            except Exception:  # the draft without the assistant is still useful
                log.exception("describe: assistant failed")
    return DraftOut(**draft.__dict__)


@router.post("", response_model=list[TxnOut], status_code=201)
async def create_transaction(body: TxnIn, ctx: Writer, session: Session) -> list[TxnOut]:
    """Returns every row written: one, or two for a transfer."""
    return await write_transaction(session, ctx, body)


async def write_transaction(
    session: AsyncSession,
    ctx: Ctx,
    body: TxnIn,
    *,
    source: TxnSource = TxnSource.manual,
    import_ref: str | None = None,
) -> list[TxnOut]:
    """Shared by the route, the inbox importer and the MCP server. Commits."""
    if body.id is not None:
        existing = await session.get(Transaction, body.id)
        if existing is not None:
            if existing.household_id != ctx.household_id:
                raise HTTPException(status.HTTP_409_CONFLICT, "Id already used")
            return [TxnOut.model_validate(t) for t in await _group(session, existing)]

    category = await _check_refs(session, ctx, body)
    transfer = body.to_account_id is not None
    auto_sorted = False
    if (
        body.auto_sort
        and not transfer
        and body.direction is Direction.debit
        and body.kind in (None, *categorize.SORTABLE_KINDS)
        and (body.category_id is None or body.person_id is None)
    ):
        g = categorize.guess(
            await categorize.load_book(session, ctx.household_id),
            body.payee,
            body.note,
            body.person_id,
        )
        if body.category_id is None and g.sure:
            body = body.model_copy(update={"category_id": g.category_id})
            category = await session.get(Category, g.category_id)
            auto_sorted = True
        if body.person_id is None and g.person_id is not None:
            body = body.model_copy(update={"person_id": g.person_id})
    kind = body.kind or _default_kind(body.direction, category, transfer)

    if body.match_scheduled and not transfer and body.status is TxnStatus.cleared:
        match = await find_match(
            session,
            ctx.household_id,
            body.account_id,
            body.direction,
            body.amount,
            body.date,
            body.category_id,
        )
        if match is not None:
            match.status = TxnStatus.cleared
            match.amount = body.amount
            match.date = body.date
            match.category_id = body.category_id or match.category_id
            match.payee = body.payee or match.payee
            match.note = body.note or match.note
            match.tags = body.tags or match.tags
            match.person_id = body.person_id or match.person_id
            match.member_id = body.member_id or ctx.member_id
            match.updated_at = _now()
            match.import_ref = import_ref or match.import_ref
            await session.commit()
            out = TxnOut.model_validate(match)
            out.matched = True
            return [out]

    group = uuid.uuid4() if transfer else None
    first = Transaction(
        id=body.id or uuid.uuid4(),
        household_id=ctx.household_id,
        account_id=body.account_id,
        date=body.date,
        amount=body.amount,
        direction=body.direction,
        kind=kind,
        status=body.status,
        source=source,
        category_id=body.category_id,
        member_id=body.member_id or ctx.member_id,
        person_id=body.person_id,
        payee=body.payee,
        note=body.note,
        tags=body.tags,
        transfer_group_id=group,
        loan_id=body.loan_id,
        import_ref=import_ref,
        created_by=ctx.user_id,
    )
    rows = [first]
    if body.to_account_id is not None:
        rows.append(
            Transaction(
                household_id=ctx.household_id,
                account_id=body.to_account_id,
                date=body.date,
                amount=body.amount,
                direction=(
                    Direction.credit if body.direction is Direction.debit else Direction.debit
                ),
                kind=kind,
                status=body.status,
                source=source,
                category_id=body.category_id,
                member_id=first.member_id,
                payee=body.payee,
                note=body.note,
                tags=body.tags,
                transfer_group_id=group,
                created_by=ctx.user_id,
            )
        )
    session.add_all(rows)
    await session.commit()
    written = [TxnOut.model_validate(r) for r in rows]
    written[0].auto_sorted = auto_sorted
    return written


@router.get("/{txn_id}", response_model=TxnOut)
async def get_transaction(txn_id: uuid.UUID, ctx: Reader, session: Session) -> Transaction:
    txn = await owned(session, Transaction, txn_id, ctx)
    if txn.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Transaction not found")
    return txn


@router.patch("/{txn_id}", response_model=list[TxnOut])
async def patch_transaction(
    txn_id: uuid.UUID, body: TxnPatch, ctx: Writer, session: Session
) -> list[TxnOut]:
    txn = await owned(session, Transaction, txn_id, ctx)
    await _check_refs(session, ctx, body)
    changes = body.model_dump(exclude_unset=True)
    if "tags" in changes and changes["tags"] is not None:
        changes["tags"] = sorted({t.strip().lower()[:40] for t in changes["tags"] if t.strip()})
    rows = await _group(session, txn)
    for row in rows:  # keep both legs of a transfer in step
        for key, value in changes.items():
            setattr(row, key, value)
        row.updated_at = _now()
    # A correction teaches the sorter: the next entry from this payee goes the same way.
    taught = (
        ("category_id" in changes or "person_id" in changes)
        and txn.transfer_group_id is None
        and txn.direction is Direction.debit
        and txn.kind in categorize.SORTABLE_KINDS
        and txn.category_id is not None
    )
    if taught:
        await categorize.learn(session, ctx.household_id, txn.payee, txn.category_id, txn.person_id)
    await session.commit()
    out = [TxnOut.model_validate(r) for r in rows]
    if taught:
        key = categorize.payee_key(txn.payee)
        others = await categorize.similar(session, ctx.household_id, key, txn.category_id)
        out[0].similar = len([t for t in others if t.id != txn.id])
    return out


@router.post("/{txn_id}/confirm", response_model=list[TxnOut])
async def confirm(
    txn_id: uuid.UUID, body: ConfirmIn, ctx: Writer, session: Session
) -> list[TxnOut]:
    """Mark a scheduled occurrence as paid/received, optionally with the actual amount/date."""
    txn = await owned(session, Transaction, txn_id, ctx)
    rows = await _group(session, txn)
    for row in rows:
        row.status = TxnStatus.cleared
        if body.amount is not None:
            row.amount = body.amount
        if body.date is not None:
            row.date = body.date
        row.updated_at = _now()
    await session.commit()
    return [TxnOut.model_validate(r) for r in rows]


@router.post("/{txn_id}/skip", status_code=204)
async def skip(txn_id: uuid.UUID, ctx: Writer, session: Session) -> None:
    """Skip one scheduled occurrence (e.g. no school fee this term) without editing the rule."""
    txn = await owned(session, Transaction, txn_id, ctx)
    if txn.status is not TxnStatus.scheduled or txn.recurring_rule_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Only scheduled occurrences can be skipped")
    rule = await session.get(RecurringRule, txn.recurring_rule_id)
    if rule is not None and txn.occurrence_date is not None:
        rule.skipped_dates = sorted({*rule.skipped_dates, txn.occurrence_date})
    for row in await _group(session, txn):
        row.deleted_at = _now()
    await session.commit()


@router.delete("/{txn_id}", status_code=204)
async def delete_transaction(txn_id: uuid.UUID, ctx: Writer, session: Session) -> None:
    txn = await owned(session, Transaction, txn_id, ctx)
    ids = [r.id for r in await _group(session, txn)]
    await session.execute(
        update(Transaction).where(Transaction.id.in_(ids)).values(deleted_at=_now())
    )
    await session.commit()


@router.post("/{txn_id}/restore", response_model=list[TxnOut])
async def restore(txn_id: uuid.UUID, ctx: Writer, session: Session) -> list[TxnOut]:
    """Undo a delete (the PWA's undo toast)."""
    txn = await owned(session, Transaction, txn_id, ctx)
    rows = await _group(session, txn)
    for row in rows:
        row.deleted_at = None
    await session.commit()
    return [TxnOut.model_validate(r) for r in rows]
