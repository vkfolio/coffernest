"""Changes the assistant (in-app chat or an MCP client) may make when the household asks.

Each action resolves names the way people say them ("HDFC Salary", "Groceries"), writes through
the same code as the app's own screens, and returns an ``undo`` description the chat turns into
an Undo button.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coffernest.deps import Ctx
from coffernest.enums import Direction, TxnStatus
from coffernest.models import Extraction, ExtractionItem, Household, RecurringRule, Transaction
from coffernest.money import format_money
from coffernest.services import card_loans, categorize, inbox, live


class ActionError(ValueError):
    """Something the household (or the model) needs to fix; the message says what."""


def _money(value: Any) -> Decimal:
    try:
        amount = Decimal(str(value)).copy_abs().quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError) as exc:
        raise ActionError("The amount isn't a number") from exc
    if amount <= 0:
        raise ActionError("The amount must be more than zero")
    return amount


def _day(value: str) -> date:
    if not value:
        return date.today()
    try:
        return date.fromisoformat(value[:10])
    except ValueError as exc:
        raise ActionError("Dates are YYYY-MM-DD") from exc


async def add_transaction(
    session: AsyncSession,
    ctx: Ctx,
    *,
    account: str,
    amount: Any,
    direction: str,
    day: str = "",
    payee: str = "",
    category: str = "",
    note: str = "",
    scheduled: bool = False,
    key: str = "",
    tag: str = "assistant",
    person: str = "",
) -> dict[str, Any]:
    from coffernest.routes.transactions import TxnIn, write_transaction

    if not ctx.can_write:
        raise ActionError("Viewers can't add entries")
    if direction not in ("debit", "credit"):
        raise ActionError("direction is debit (money out) or credit (money in)")
    d = await inbox.directory(session, ctx.household_id)
    acc = d.by_name(account)
    if acc is None:
        names = ", ".join(a.name for a in d.accounts)
        raise ActionError(f"No account called {account!r}. Accounts: {names}")
    cat = d.category(category) if category else None
    book = await categorize.load_book(session, ctx.household_id)
    person_id = find_person(book, person) if person else None
    if person and person_id is None:
        raise ActionError(
            f"No one called {person!r}. People: "
            + (", ".join(n.title() for n in book.people) or "(none yet: add them with add_person)")
        )
    rows = await write_transaction(
        session,
        ctx,
        TxnIn(
            id=uuid.uuid5(uuid.NAMESPACE_URL, f"vkf:{ctx.household_id}:{key}") if key else None,
            account_id=acc.id,
            date=_day(day),
            amount=_money(amount),
            direction=Direction(direction),
            category_id=cat,
            person_id=person_id,
            payee=payee[:120],
            note=note[:2000],
            status=TxnStatus.scheduled if scheduled else TxnStatus.cleared,
            tags=[tag],
        ),
    )
    matched = any(r.matched for r in rows)
    who = {pid: n.title() for n, pid in book.people.items()}
    live.publish(ctx.household_id)
    return {
        "recorded": True,
        "account": acc.name,
        "date": rows[0].date.isoformat(),
        "amount": str(rows[0].amount),
        "direction": direction,
        "payee": rows[0].payee,
        "category_found": cat is not None,
        "category": book.labels.get(rows[0].category_id, "") if rows[0].category_id else "",
        "sorted_automatically": rows[0].auto_sorted,
        "for": who.get(rows[0].person_id, "Family") if rows[0].person_id else "Family",
        "matched_a_scheduled_entry": matched,
        "status": rows[0].status.value,
        "undo": None if matched else {"kind": "transactions", "ids": [str(r.id) for r in rows]},
    }


def find_person(book: categorize.Book, name: str) -> uuid.UUID | None:
    want = name.strip().lower()
    if not want:
        return None
    return book.people.get(want) or next(
        (pid for n, pid in book.people.items() if n.startswith(want) or want.startswith(n)), None
    )


async def add_person(
    session: AsyncSession, ctx: Ctx, *, name: str, relation: str = "child"
) -> dict[str, Any]:
    """Someone entries can be for: a child, the spouse, a parent. No login is made."""
    from coffernest.enums import Relation
    from coffernest.models import Person

    if not ctx.can_write:
        raise ActionError("Viewers can't add people")
    clean = " ".join(name.split())[:40]
    if not clean:
        raise ActionError("Say the person's name")
    if relation not in {r.value for r in Relation}:
        raise ActionError("relation is child, spouse, parent, self or other")
    book = await categorize.load_book(session, ctx.household_id)
    if clean.lower() in book.people:
        raise ActionError(f"{clean} is already there")
    person = Person(household_id=ctx.household_id, name=clean, relation=Relation(relation))
    session.add(person)
    await session.commit()
    live.publish(ctx.household_id)
    return {
        "added": True,
        "name": clean,
        "relation": relation,
        "undo": {"kind": "person", "ids": [str(person.id)]},
    }


async def add_recurring(
    session: AsyncSession,
    ctx: Ctx,
    *,
    name: str,
    amount: Any,
    account: str,
    frequency: str = "monthly",
    day_of_month: int = 1,
    category: str = "",
    start: str = "",
    direction: str = "debit",
    estimate: bool = True,
) -> dict[str, Any]:
    from coffernest.routes.recurring import RuleIn, create_rule

    if not ctx.can_write:
        raise ActionError("Viewers can't add repeating entries")
    if frequency not in ("monthly", "quarterly", "yearly"):
        raise ActionError("frequency is monthly, quarterly or yearly")
    d = await inbox.directory(session, ctx.household_id)
    acc = d.by_name(account)
    if acc is None:
        names = ", ".join(a.name for a in d.accounts)
        raise ActionError(f"No account called {account!r}. Accounts: {names}")
    first = _day(start)
    rule = await create_rule(
        RuleIn(
            name=name[:80] or "Bill",
            amount=_money(amount),
            amount_is_estimate=estimate,
            direction=Direction(direction),
            account_id=acc.id,
            category_id=d.category(category) if category else None,
            payee=name[:120],
            rrule=inbox._rrule(frequency, day_of_month, first),
            start_date=first,
        ),
        ctx,
        session,
    )
    live.publish(ctx.household_id)
    return {
        "added": True,
        "name": rule.name,
        "amount": str(rule.amount),
        "frequency": frequency,
        "account": acc.name,
        "undo": {"kind": "rule", "ids": [str(rule.id)]},
    }


async def review_inbox(
    session: AsyncSession, ctx: Ctx, *, file_id: str, approve: bool, item_ids: list[str] | None
) -> dict[str, Any]:
    """Approve (or skip) pending items of one inbox file; all pending ones if no ids given."""
    if not ctx.can_write:
        raise ActionError("Viewers can't change the inbox")
    try:
        ext = await session.get(Extraction, uuid.UUID(file_id))
    except ValueError as exc:
        raise ActionError("Unknown file id") from exc
    if ext is None or ext.household_id != ctx.household_id:
        raise ActionError("Unknown file id")
    rows = (
        await session.scalars(
            select(ExtractionItem)
            .where(ExtractionItem.extraction_id == ext.id, ExtractionItem.state == "pending")
            .order_by(ExtractionItem.position)
        )
    ).all()
    wanted = set(item_ids or [])
    household = await session.get(Household, ctx.household_id)
    currency = household.currency if household else "INR"
    done: list[str] = []
    labels: list[str] = []
    failed: list[str] = []
    for item in rows:
        if wanted and str(item.id) not in wanted:
            continue
        if approve:
            try:
                await inbox.apply_item(session, ctx, item)
            except ValueError as exc:
                failed.append(f"{item.payload.get('payee') or item.payload.get('name')}: {exc}")
                continue
            item.state = "approved"
            item.reason = ""
        else:
            item.state = "skipped"
        await session.commit()
        done.append(str(item.id))
        labels.append(_item_label(item, currency))
    await inbox.refresh_status(session, ext)
    return {
        "approved" if approve else "skipped": len(done),
        "failed": failed,
        "items": labels,
        "undo": {"kind": "inbox_items", "ids": done} if approve and done else None,
    }


def _item_label(item: ExtractionItem, currency: str = "INR") -> str:
    p = item.payload
    name = p.get("payee") or p.get("name") or p.get("card_name") or p.get("lender") or item.kind
    amount = p.get("amount") or p.get("total_due") or p.get("outstanding") or ""
    try:
        shown = f" {format_money(Decimal(str(amount)), currency)}" if amount else ""
    except (InvalidOperation, ValueError):
        shown = ""
    kind = {
        "recurring": "Repeating: ",
        "bill": "Upcoming bill: ",
        "loan": "Loan: ",
        "card_statement": "Card statement: ",
    }.get(item.kind, "")
    return f"{kind}{name}{shown}"


async def undo(session: AsyncSession, ctx: Ctx, spec: dict[str, Any]) -> int:
    """Reverse an action's ``undo`` description. Returns how many things were taken back."""
    kind = spec.get("kind")
    ids = [uuid.UUID(str(i)) for i in spec.get("ids") or []]
    now = datetime.now(UTC)
    count = 0
    if kind == "transactions":
        for txn in (
            await session.scalars(select(Transaction).where(Transaction.id.in_(ids)))
        ).all():
            if txn.household_id == ctx.household_id and txn.deleted_at is None:
                txn.deleted_at = now
                count += 1
    elif kind == "rule":
        from coffernest.services.recurring import drop_future

        for rule in (
            await session.scalars(select(RecurringRule).where(RecurringRule.id.in_(ids)))
        ).all():
            if rule.household_id == ctx.household_id and rule.active:
                rule.active = False  # same as deleting it in Repeating
                await drop_future(session, rule.id, date.today())
                count += 1
    elif kind == "inbox_items":
        for item in (
            await session.scalars(select(ExtractionItem).where(ExtractionItem.id.in_(ids)))
        ).all():
            if item.household_id == ctx.household_id and item.state in ("approved", "auto_added"):
                await inbox.undo_item(session, item)
                count += 1
    elif kind in ("account", "card", "loan"):
        from coffernest.models import Account, CreditCard, Loan

        for ident in ids:
            account_id: uuid.UUID | None = None
            if kind == "account":
                account_id = ident
            elif kind == "card":
                card = await session.get(CreditCard, ident)
                account_id = (
                    card.account_id if card and card.household_id == ctx.household_id else None
                )
            else:
                loan = await session.get(Loan, ident)
                if loan is not None and loan.household_id == ctx.household_id:
                    loan.closed = True
                    account_id = loan.account_id
                    from coffernest.services.recurring import drop_future

                    for rule in (
                        await session.scalars(
                            select(RecurringRule).where(RecurringRule.loan_id == loan.id)
                        )
                    ).all():
                        rule.active = False
                        await drop_future(session, rule.id, date.today())
            account = await session.get(Account, account_id) if account_id else None
            if account is not None and account.household_id == ctx.household_id:
                account.is_active = False  # archived: history is kept, like Settings does
                count += 1
    elif kind == "details":
        count = await _restore_details(session, ctx, spec)
    elif kind == "person":
        from coffernest.models import Person

        for p in (await session.scalars(select(Person).where(Person.id.in_(ids)))).all():
            if p.household_id == ctx.household_id and not p.archived:
                p.archived = True
                count += 1
    elif kind == "loan_to_card":
        count = await card_loans.undo_move(session, ctx.household_id, spec)
    elif kind == "rule_stop":
        count = await card_loans.undo_stop(session, ctx.household_id, spec)
    else:
        raise ActionError("Nothing to undo")
    await session.commit()
    live.publish(ctx.household_id)
    return count


# ---------------------------------------------------------------- setting up the books


_IFSC = re.compile(r"^[A-Za-z]{4}0[A-Za-z0-9]{6}$")


def _last4(value: str) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[-4:]


async def _find_account(session: AsyncSession, ctx: Ctx, name: str) -> Any:
    if not name:
        return None
    d = await inbox.directory(session, ctx.household_id)
    acc = d.by_name(name)
    if acc is None:
        names = ", ".join(a.name for a in d.accounts)
        raise ActionError(f"No account called {name!r}. Accounts: {names}")
    return acc


async def add_account(
    session: AsyncSession,
    ctx: Ctx,
    *,
    name: str,
    kind: str = "bank",
    institution: str = "",
    last4: str = "",
    balance: Any = 0,
    counts_towards_spending: bool = True,
    account_type: str = "",
    ifsc: str = "",
    branch: str = "",
) -> dict[str, Any]:
    """A bank, cash, wallet or investment account. Only a name and the last 4 digits are kept."""
    from coffernest.enums import AccountKind
    from coffernest.routes.accounts import AccountIn, create_account

    if not ctx.can_write:
        raise ActionError("Viewers can't add accounts")
    if kind not in ("bank", "cash", "wallet", "investment"):
        raise ActionError(
            "kind is bank, cash, wallet or investment (loans and cards have their own)"
        )
    d = await inbox.directory(session, ctx.household_id)
    if any(a.name.lower() == name.strip().lower() for a in d.accounts):
        raise ActionError(f"There's already an account called {name!r}")
    try:
        opening = Decimal(str(balance or 0)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError) as exc:
        raise ActionError("The balance isn't a number") from exc
    out = await create_account(
        AccountIn(
            kind=AccountKind(kind),
            name=name.strip()[:80],
            institution=institution.strip()[:80],
            last4=_last4(last4),
            opening_balance=opening,
            include_in_safe_to_spend=counts_towards_spending and kind != "investment",
            account_type=account_type
            if account_type in ("savings", "salary", "current", "nre", "nro", "fd", "ppf")
            else "",
            ifsc=ifsc.strip().upper() if _IFSC.match(ifsc.strip()) else "",
            branch=branch.strip()[:160],
        ),
        ctx,
        session,
    )
    live.publish(ctx.household_id)
    return {
        "added": True,
        "name": out.name,
        "kind": kind,
        "balance": str(opening),
        "undo": {"kind": "account", "ids": [str(out.id)]},
    }


async def add_loan(
    session: AsyncSession,
    ctx: Ctx,
    *,
    name: str,
    kind: str,
    lender: str = "",
    outstanding: Any,
    rate_pct: Any,
    emis_left: int,
    next_emi_date: str,
    emi: Any = None,
    rate_type: str = "fixed",
    paid_from: str = "",
    last4: str = "",
) -> dict[str, Any]:
    """A loan as it stands today; its monthly EMI becomes an automatic entry."""
    from coffernest.enums import LoanKind, RateType
    from coffernest.routes.loans import LoanIn, create_loan

    if not ctx.can_write:
        raise ActionError("Viewers can't add loans")
    if kind not in {k.value for k in LoanKind}:
        raise ActionError("kind is home, personal, car, education, gold or other")
    funding = await _find_account(session, ctx, paid_from)
    try:
        rate = Decimal(str(rate_pct))
        left = int(emis_left)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ActionError("rate_pct and emis_left must be numbers") from exc
    if left < 1:
        raise ActionError("emis_left must be at least 1")
    out = await create_loan(
        LoanIn(
            name=name.strip()[:80],
            kind=LoanKind(kind),
            lender=lender.strip()[:80],
            last4=_last4(last4),
            principal=_money(outstanding),
            rate_pct=rate,
            rate_type=RateType(rate_type) if rate_type in ("fixed", "floating") else RateType.fixed,
            tenure_months=left,
            first_emi_date=_day(next_emi_date),
            emi_override=_money(emi) if emi else None,
            funding_account_id=funding.id if funding else None,
            tax_24b=kind == "home",
            tax_80c=kind == "home",
        ),
        ctx,
        session,
    )
    live.publish(ctx.household_id)
    return {
        "added": True,
        "name": out.name,
        "emi": str(out.emi),
        "ends": str(out.end_date) if getattr(out, "end_date", None) else "",
        "emi_auto_entry_from": funding.name if funding else "(none: say which account pays it)",
        "undo": {"kind": "loan", "ids": [str(out.id)]},
    }


async def add_card(
    session: AsyncSession,
    ctx: Ctx,
    *,
    name: str,
    issuer: str,
    credit_limit: Any,
    statement_day: int,
    owed_now: Any = 0,
    due_days: int = 20,
    last4: str = "",
    paid_from: str = "",
    monthly_interest_pct: Any = None,
) -> dict[str, Any]:
    from coffernest.routes.cards import CardIn, create_card

    if not ctx.can_write:
        raise ActionError("Viewers can't add cards")
    funding = await _find_account(session, ctx, paid_from)
    try:
        owed = Decimal(str(owed_now or 0)).copy_abs().quantize(Decimal("0.01"))
        day = int(statement_day)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ActionError("owed_now and statement_day must be numbers") from exc
    if not 1 <= day <= 31:
        raise ActionError("statement_day is the day of the month the statement is made (1-31)")
    extra: dict[str, Any] = {}
    if monthly_interest_pct:
        extra["monthly_rate_pct"] = Decimal(str(monthly_interest_pct))
    out = await create_card(
        CardIn(
            name=name.strip()[:80],
            issuer=issuer.strip()[:60] or name.strip()[:60],
            last4=_last4(last4),
            credit_limit=_money(credit_limit),
            statement_day=day,
            due_days=max(1, min(int(due_days or 20), 60)),
            pay_from_account_id=funding.id if funding else None,
            opening_owed=owed,
            **extra,
        ),
        ctx,
        session,
    )
    live.publish(ctx.household_id)
    return {
        "added": True,
        "name": out.name,
        "limit": str(out.credit_limit),
        "owed": str(owed),
        "undo": {"kind": "card", "ids": [str(out.id)]},
    }


_ACCOUNT_FIELDS = ("name", "institution", "last4", "ifsc", "branch", "account_type", "notes")
_CARD_FIELDS = ("credit_limit", "monthly_rate_pct", "statement_day", "due_days")
_LOAN_FIELDS = (
    "lender",
    "original_principal",
    "rate_type",
    "start_date",
    "original_tenure_months",
    "original_emi",
)
_ACCOUNT_TYPES = ("savings", "salary", "current", "nre", "nro", "fd", "ppf", "other")


def _snapshot(obj: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in fields:
        value = getattr(obj, f)
        out[f] = None if value is None else getattr(value, "value", value)
        if isinstance(out[f], (Decimal, date)):
            out[f] = out[f].isoformat() if isinstance(out[f], date) else str(out[f])
    return out


async def update_details(
    session: AsyncSession,
    ctx: Ctx,
    *,
    name: str,
    new_name: str = "",
    institution: str = "",
    account_number: str = "",
    last4: str = "",
    ifsc: str = "",
    branch: str = "",
    account_type: str = "",
    notes: str = "",
    credit_limit: Any = None,
    annual_interest_pct: Any = None,
    statement_day: int | None = None,
    due_days: int | None = None,
    sanctioned_amount: Any = None,
    rate_type: str = "",
    loan_start_date: str = "",
    original_tenure_months: int | None = None,
    original_emi: Any = None,
) -> dict[str, Any]:
    """Correct the details of an existing account, card or loan (not its balance).

    A full account number is sealed on arrival and never returned; only its last 4 show."""
    from coffernest.enums import RateType
    from coffernest.models import Account
    from coffernest.security.box import seal

    if not ctx.can_write:
        raise ActionError("Viewers can't change accounts")
    d = await inbox.directory(session, ctx.household_id)
    found = d.by_name(name)
    if found is None:
        raise ActionError(
            f"No account, card or loan called {name!r}. Have: "
            + ", ".join(a.name for a in d.accounts)
        )
    account = await session.get(Account, found.id)
    assert account is not None
    card = d.cards.get(account.id)
    loan = d.loans.get(account.id)
    before: dict[str, Any] = {
        "account": _snapshot(account, _ACCOUNT_FIELDS) | {"number_sealed": account.number_sealed},
        "card": _snapshot(card, _CARD_FIELDS) if card else None,
        "loan": _snapshot(loan, _LOAN_FIELDS) if loan else None,
    }
    changed: list[str] = []

    if new_name.strip():
        account.name = new_name.strip()[:80]
        changed.append("name")
    if institution.strip():
        account.institution = institution.strip()[:80]
        if loan is not None:
            loan.lender = institution.strip()[:80]
        changed.append("lender" if loan is not None else "institution")
    if account_number.strip():
        number = "".join(ch for ch in account_number if ch.isalnum())
        if not 4 <= len(number) <= 34:
            raise ActionError("That account number doesn't look right")
        account.number_sealed = seal(number)
        changed.append("account number (encrypted)")
        # A card keeps its own last 4 (what statements print); a bureau or account reference
        # must not replace it.
        if card is None and not last4.strip():
            account.last4 = _last4(number) or number[-4:]
    if last4.strip():
        account.last4 = _last4(last4)
        changed.append("last 4")
    if ifsc.strip():
        if not _IFSC.match(ifsc.strip()):
            raise ActionError("IFSC is 11 characters, like HDFC0001234")
        account.ifsc = ifsc.strip().upper()
        changed.append("IFSC")
    if branch.strip():
        account.branch = branch.strip()[:160]
        changed.append("branch")
    if account_type.strip():
        if account_type not in _ACCOUNT_TYPES:
            raise ActionError("account_type is one of " + ", ".join(_ACCOUNT_TYPES))
        account.account_type = account_type
        changed.append("type")
    if notes.strip():
        account.notes = notes.strip()[:2000]
        changed.append("notes")

    card_bits = (credit_limit, annual_interest_pct, statement_day, due_days)
    if any(v not in (None, "", 0) for v in card_bits):
        if card is None:
            raise ActionError(f"{account.name} isn't a credit card")
        if credit_limit:
            card.credit_limit = _money(credit_limit)
            changed.append("limit")
        if annual_interest_pct:
            try:
                card.monthly_rate_pct = (Decimal(str(annual_interest_pct)) / 12).quantize(
                    Decimal("0.01")
                )
            except (InvalidOperation, ValueError) as exc:
                raise ActionError("annual_interest_pct must be a number") from exc
            changed.append("interest rate")
        if statement_day:
            if not 1 <= int(statement_day) <= 31:
                raise ActionError("statement_day is 1-31")
            card.statement_day = int(statement_day)
            changed.append("statement day")
        if due_days:
            card.due_days = max(1, min(int(due_days), 60))
            changed.append("due days")
    if sanctioned_amount or rate_type or loan_start_date or original_tenure_months or original_emi:
        if loan is None:
            raise ActionError(f"{account.name} isn't a loan")
        if sanctioned_amount:
            loan.original_principal = _money(sanctioned_amount)
            changed.append("sanctioned amount")
        if rate_type:
            if rate_type not in ("fixed", "floating"):
                raise ActionError("rate_type is fixed or floating")
            loan.rate_type = RateType(rate_type)
            changed.append("rate type")
        if loan_start_date:
            start = _day(loan_start_date)
            if start >= loan.first_emi_date:
                raise ActionError(
                    "loan_start_date is the original loan's first EMI, before "
                    f"{loan.first_emi_date:%d %b %Y} when it was entered here"
                )
            loan.start_date = start
            changed.append("start date")
        if original_tenure_months:
            if not 1 <= int(original_tenure_months) <= 600:
                raise ActionError("original_tenure_months is 1-600")
            loan.original_tenure_months = int(original_tenure_months)
            changed.append("original tenure")
        if original_emi:
            loan.original_emi = _money(original_emi)
            changed.append("original EMI")
    if not changed:
        raise ActionError("Nothing to change: say which details to update")
    await session.commit()
    live.publish(ctx.household_id)
    return {
        "updated": account.name,
        "changed": changed,
        "changed_text": ", ".join(changed),
        "last4": account.last4,
        "has_account_number": account.number_sealed is not None,
        "undo": {"kind": "details", "ids": [str(account.id)], "before": before},
    }


async def _restore_details(session: AsyncSession, ctx: Ctx, spec: dict[str, Any]) -> int:
    from coffernest.enums import RateType
    from coffernest.models import Account, CreditCard, Loan

    account = await session.get(Account, uuid.UUID(str(spec["ids"][0])))
    if account is None or account.household_id != ctx.household_id:
        return 0
    before = spec.get("before") or {}
    for key, value in (before.get("account") or {}).items():
        setattr(account, key, value)
    if before.get("card"):
        card = (
            await session.scalars(select(CreditCard).where(CreditCard.account_id == account.id))
        ).first()
        if card is not None:
            vals = before["card"]
            card.credit_limit = Decimal(vals["credit_limit"])
            card.monthly_rate_pct = Decimal(vals["monthly_rate_pct"])
            card.statement_day = int(vals["statement_day"])
            card.due_days = int(vals["due_days"])
    if before.get("loan"):
        loan = (await session.scalars(select(Loan).where(Loan.account_id == account.id))).first()
        if loan is not None:
            vals = before["loan"]
            loan.lender = vals["lender"]
            loan.original_principal = (
                Decimal(vals["original_principal"]) if vals["original_principal"] else None
            )
            loan.rate_type = RateType(vals["rate_type"])
            if "start_date" in vals:
                loan.start_date = (
                    date.fromisoformat(vals["start_date"]) if vals["start_date"] else None
                )
                loan.original_tenure_months = vals["original_tenure_months"]
                loan.original_emi = Decimal(vals["original_emi"]) if vals["original_emi"] else None
    return 1


async def move_loan_to_card(
    session: AsyncSession,
    ctx: Ctx,
    *,
    loan: str,
    card: str,
    card_owed_includes_it: bool,
) -> dict[str, Any]:
    """A loan that is really billed on a credit card becomes an EMI on that card."""
    if not ctx.can_write:
        raise ActionError("Viewers can't change loans")
    d = await inbox.directory(session, ctx.household_id)
    loan_acc = d.by_name(loan)
    the_loan = d.loans.get(loan_acc.id) if loan_acc else None
    if the_loan is None:
        raise ActionError(
            f"No open loan called {loan!r}. Loans: "
            + ", ".join(a.name for a in d.accounts if a.id in d.loans)
        )
    card_acc = d.by_name(card)
    the_card = d.cards.get(card_acc.id) if card_acc else None
    if the_card is None or card_acc is None:
        raise ActionError(
            f"No card called {card!r}. Cards: "
            + ", ".join(a.name for a in d.accounts if a.id in d.cards)
        )
    try:
        out = await card_loans.move_loan_to_card(
            session,
            ctx.household_id,
            the_loan,
            the_card,
            card_owed_includes_it=bool(card_owed_includes_it),
        )
    except card_loans.MoveError as exc:
        raise ActionError(str(exc)) from exc
    await session.commit()
    live.publish(ctx.household_id)
    return {
        "moved": out["moved"],
        "card": card_acc.name,
        "outstanding": str(out["outstanding"]),
        "emis_left": out["emis_left"],
        "next_emi": out["next_emi"].isoformat(),
        "added_to_card_balance": out["added_to_card_balance"],
        "bank_entries_removed": out["bank_entries_removed"],
        "note": (
            "Some EMIs were already marked paid from a bank account; they stay in the ledger."
            if out["has_cleared_bank_emis"]
            else ""
        ),
        "undo": out["undo"],
    }


async def stop_recurring(
    session: AsyncSession, ctx: Ctx, *, name: str, from_date: str = ""
) -> dict[str, Any]:
    """Stop a repeating bill, income or SIP (by name) from a date (default today)."""
    if not ctx.can_write:
        raise ActionError("Viewers can't change repeating entries")
    rules = (
        await session.scalars(
            select(RecurringRule).where(
                RecurringRule.household_id == ctx.household_id, RecurringRule.active.is_(True)
            )
        )
    ).all()
    wanted = name.strip().lower()
    hits = [r for r in rules if r.name.lower() == wanted] or [
        r for r in rules if wanted and wanted in r.name.lower()
    ]
    if not hits:
        raise ActionError(
            f"No repeating entry called {name!r}. Repeating: "
            + ", ".join(sorted(r.name for r in rules))
        )
    if len(hits) > 1:
        raise ActionError(
            "More than one matches; say which: " + ", ".join(sorted(r.name for r in hits))
        )
    rule = hits[0]
    if rule.loan_id is not None:
        raise ActionError(
            f"{rule.name} is a loan's EMI. It stops when the loan is closed; if it is really "
            "billed on a credit card, use move_loan_to_card."
        )
    out = await card_loans.stop_rule(session, ctx.household_id, rule, _day(from_date))
    await session.commit()
    live.publish(ctx.household_id)
    return {
        "stopped": out["stopped"],
        "from": out["from"].isoformat(),
        "planned_entries_removed": out["planned_entries_removed"],
        "undo": out["undo"],
    }


async def inbox_overview(session: AsyncSession, household_id: uuid.UUID) -> list[dict[str, Any]]:
    """Recent files with what's still waiting, so the assistant can act on 'approve my inbox'."""
    from coffernest.models import Attachment

    out: list[dict[str, Any]] = []
    rows = (
        await session.execute(
            select(Extraction, Attachment)
            .join(Attachment, Attachment.id == Extraction.attachment_id)
            .where(Extraction.household_id == household_id)
            .order_by(Extraction.created_at.desc())
            .limit(15)
        )
    ).all()
    for ext, att in rows:
        items = (
            await session.scalars(
                select(ExtractionItem)
                .where(ExtractionItem.extraction_id == ext.id)
                .order_by(ExtractionItem.position)
            )
        ).all()
        out.append(
            {
                "file_id": str(ext.id),
                "filename": att.filename,
                "status": ext.status,
                "summary": ext.summary,
                "pending": [
                    {
                        "item_id": str(i.id),
                        "kind": i.kind,
                        "reason": i.reason,
                        **{
                            k: v
                            for k, v in i.payload.items()
                            if k
                            in (
                                "date",
                                "payee",
                                "name",
                                "amount",
                                "direction",
                                "category_name",
                                "frequency",
                                "due_date",
                                "total_due",
                            )
                        },
                    }
                    for i in items
                    if i.state == "pending"
                ][:40],
                "added": sum(i.state in ("auto_added", "approved") for i in items),
                "duplicates": sum(i.state == "duplicate" for i in items),
            }
        )
    return out
