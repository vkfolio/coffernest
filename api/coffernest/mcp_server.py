"""Remote MCP server at /mcp: ask about your money from Claude Desktop, claude.ai or Codex.

Tools call the same deterministic engine the in-app assistant uses — no LLM runs here, so a
desktop question costs the server almost nothing. Skills are offered as MCP prompts.

Auth: a bearer token that is either a personal access token (``cn_pat_…``, made in Settings)
or an OAuth access token from our own authorization server (routes/oauth.py). Web-app access
tokens are never accepted here. Write tools need the ``finance.write`` scope and a member who
may change data.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import select
from starlette.applications import Starlette

from coffernest import db
from coffernest.ai.tools import BY_NAME, to_json
from coffernest.config import Settings, get_settings
from coffernest.deps import Ctx
from coffernest.enums import AttachmentKind, Direction, Role, UploadStatus
from coffernest.models import AccessToken as PatRow
from coffernest.models import Attachment, Extraction, Member, Transaction, User
from coffernest.security.tokens import (
    PAT_PREFIXES,
    SCOPE_READ,
    SCOPE_WRITE,
    decode_app_token,
    sha256,
)
from coffernest.services import live, skills

INSTRUCTIONS = """Coffernest holds one household's money: bank accounts, salary, loans
(home/personal/car), credit cards and card EMIs, repeating bills, school fees. Amounts are in
the household's own currency (get_overview returns its code, e.g. INR or USD).
Every number comes from the household's own ledger via these tools; call them rather than
estimating. For planning questions, load a playbook with get_skill (see list_skills) and follow
it. To set the books up, use add_account, add_loan and add_card (names and the last 4 digits
are enough), then add_recurring_bill for salary and bills. update_details corrects an existing
one; a full account number is saved there only when the owner asks, and is stored encrypted.
stop_recurring stops or removes a repeating entry (a bill, income or SIP). move_loan_to_card
turns a loan that is billed on a credit card into an EMI on that card, so it isn't also an
automatic debit from the bank.
Make changes only when the user asks you to record something."""


def resource_url(settings: Settings) -> str:
    return settings.public_url.rstrip("/") + "/mcp"


class Verifier:
    """Accepts personal access tokens and our OAuth access tokens."""

    async def verify_token(self, token: str) -> AccessToken | None:
        settings = get_settings()
        async with db.sessionmaker()() as session:
            if token.startswith(PAT_PREFIXES):
                row = await session.scalar(select(PatRow).where(PatRow.token_hash == sha256(token)))
                if row is None or row.revoked_at is not None:
                    return None
                user = await session.get(User, row.user_id)
                member = await session.get(Member, row.member_id)
                if user is None or not user.is_active or member is None:
                    return None
                now = datetime.now(UTC)
                if row.last_used_at is None or now - row.last_used_at > timedelta(minutes=5):
                    row.last_used_at = now
                    await session.commit()
                return AccessToken(
                    token=token,
                    client_id=f"pat:{row.id}",
                    scopes=list(row.scopes),
                    expires_at=int(time.time()) + 3600,
                    resource=resource_url(settings),
                    subject=str(user.id),
                    claims={
                        "hid": str(row.household_id),
                        "mid": str(member.id),
                        "role": member.role.value,
                    },
                )
            claims = decode_app_token(settings, token, resource_url(settings))
            if claims is None:
                return None
            user = await session.get(User, claims.user_id)
            if user is None or not user.is_active or user.token_version != claims.token_version:
                return None
            return AccessToken(
                token=token,
                client_id=claims.client_id,
                scopes=list(claims.scopes),
                expires_at=claims.expires_at,
                resource=claims.resource,
                subject=str(claims.user_id),
                claims={
                    "hid": str(claims.household_id),
                    "mid": str(claims.member_id),
                    "role": claims.role.value,
                },
            )


class Denied(Exception):
    pass


def _who(write: bool = False) -> tuple[uuid.UUID, uuid.UUID, Role]:
    tok = get_access_token()
    if tok is None or not tok.claims:
        raise Denied("Not signed in")
    if write and SCOPE_WRITE not in tok.scopes:
        raise Denied("This connection is read-only. Make a token with 'read and add' in Settings.")
    role = Role(tok.claims["role"])
    if write and role is Role.viewer:
        raise Denied("Viewers can't add entries")
    return uuid.UUID(tok.claims["hid"]), uuid.UUID(tok.claims["mid"]), role


async def _tool(name: str, args: dict[str, Any]) -> str:
    hid, _, _ = _who()
    async with db.sessionmaker()() as session:
        return to_json(await BY_NAME[name].handler(session, hid, args))


async def _ctx(session: Any, hid: uuid.UUID, mid: uuid.UUID, role: Role) -> Ctx:
    member = await session.get(Member, mid)
    if member is None:
        raise Denied("Member not found")
    return Ctx(member.user_id, member.id, hid, role)


def build(settings: Settings) -> MCPServer[Any]:
    server: MCPServer[Any] = MCPServer(
        name="coffernest",
        title="Coffernest",
        instructions=INSTRUCTIONS,
        version="2.0.0",
        token_verifier=Verifier(),
        auth=AuthSettings(
            issuer_url=settings.public_url,
            resource_server_url=resource_url(settings),
            required_scopes=[SCOPE_READ],
            validate_token_resource=True,
        ),
    )

    @server.tool(description=BY_NAME["get_overview"].description)
    async def get_overview() -> str:
        return await _tool("get_overview", {})

    @server.tool(description=BY_NAME["get_dues"].description)
    async def get_dues(days: int = 30) -> str:
        return await _tool("get_dues", {"days": days})

    @server.tool(description=BY_NAME["get_category_spend"].description)
    async def get_category_spend(month: str = "") -> str:
        """month: any YYYY-MM-DD inside the month; empty for this month."""
        return await _tool("get_category_spend", {"month": month} if month else {})

    @server.tool(description=BY_NAME["get_spend"].description)
    async def get_spend(
        start: str = "",
        end: str = "",
        category: str = "",
        person: str = "",
        text: str = "",
        money_in: bool = False,
    ) -> str:
        args: dict[str, Any] = {
            k: v
            for k, v in {
                "start": start,
                "end": end,
                "category": category,
                "person": person,
                "text": text,
                "money_in": money_in,
            }.items()
            if v
        }
        return await _tool("get_spend", args)

    @server.tool(description=BY_NAME["get_cashflow"].description)
    async def get_cashflow(months: int = 6) -> str:
        return await _tool("get_cashflow", {"months": months})

    @server.tool(description=BY_NAME["list_accounts"].description)
    async def list_accounts() -> str:
        return await _tool("list_accounts", {})

    @server.tool(description=BY_NAME["simulate_prepayment"].description)
    async def simulate_prepayment(
        loan_name: str, lump_sum: float = 0, extra_monthly: float = 0, mode: str = "reduce_tenure"
    ) -> str:
        return await _tool(
            "simulate_prepayment",
            {
                "loan_name": loan_name,
                "lump_sum": lump_sum,
                "extra_monthly": extra_monthly,
                "mode": mode,
            },
        )

    @server.tool(description=BY_NAME["plan_payoff"].description)
    async def plan_payoff(extra_monthly: float = 0) -> str:
        return await _tool("plan_payoff", {"extra_monthly": extra_monthly})

    @server.tool(description=BY_NAME["compare_paths"].description)
    async def compare_paths(
        monthly_amount: float,
        lump_sum: float = 0,
        years: int = 10,
        paths: list[str] | None = None,
        sip_return_pct: float = 12,
    ) -> str:
        return await _tool(
            "compare_paths",
            {
                "monthly_amount": monthly_amount,
                "lump_sum": lump_sum,
                "years": years,
                "paths": paths or [],
                "sip_return_pct": sip_return_pct,
            },
        )

    @server.tool(description=BY_NAME["project_goal"].description)
    async def project_goal(
        target_today: float,
        years: int,
        inflation_pct: float = 6,
        return_pct: float = 12,
        step_up_pct: float = 10,
        current_saved: float = 0,
    ) -> str:
        return await _tool(
            "project_goal",
            {
                "target_today": target_today,
                "years": years,
                "inflation_pct": inflation_pct,
                "return_pct": return_pct,
                "step_up_pct": step_up_pct,
                "current_saved": current_saved,
            },
        )

    @server.tool(description=BY_NAME["get_anomalies"].description)
    async def get_anomalies() -> str:
        return await _tool("get_anomalies", {})

    @server.tool(description=BY_NAME["get_tax_summary"].description)
    async def get_tax_summary() -> str:
        return await _tool("get_tax_summary", {})

    @server.tool()
    async def search_transactions(
        text: str = "", start: str = "", end: str = "", limit: int = 50
    ) -> str:
        """Ledger entries (newest first), optionally matching payee/note text and a YYYY-MM-DD
        date range. Scheduled (not yet paid) entries are marked status=scheduled."""
        hid, _, _ = _who()
        async with db.sessionmaker()() as session:
            query = select(Transaction).where(
                Transaction.household_id == hid, Transaction.deleted_at.is_(None)
            )
            if text:
                like = f"%{text}%"
                query = query.where(Transaction.payee.ilike(like) | Transaction.note.ilike(like))
            if start:
                query = query.where(Transaction.date >= date.fromisoformat(start))
            if end:
                query = query.where(Transaction.date <= date.fromisoformat(end))
            rows = (
                await session.scalars(
                    query.order_by(Transaction.date.desc()).limit(max(1, min(limit, 200)))
                )
            ).all()
            from coffernest.services.inbox import directory

            d = await directory(session, hid)
            names = {a.id: a.name for a in d.accounts}
            cats = {v: k for k, v in d.categories.items()}
            return to_json(
                [
                    {
                        "date": t.date,
                        "amount": t.amount,
                        "direction": t.direction,
                        "account": names.get(t.account_id, ""),
                        "payee": t.payee,
                        "category": cats.get(t.category_id, "") if t.category_id else "",
                        "status": t.status,
                        "kind": t.kind,
                        "note": t.note[:200],
                    }
                    for t in rows
                ]
            )

    @server.tool()
    async def list_skills() -> str:
        """The household's financial-planning playbooks (name and when to use each)."""
        hid, _, _ = _who()
        async with db.sessionmaker()() as session:
            items = await skills.enabled_skills(session, hid)
        return to_json([{"name": s.name, "title": s.title, "when": s.description} for s in items])

    @server.tool()
    async def get_skill(name: str) -> str:
        """Load one playbook's full instructions (and its reference list). Follow it."""
        return await _tool("load_skill", {"name": name})

    @server.tool()
    async def add_transaction(
        account: str,
        amount: float,
        direction: str,
        day: str = "",
        payee: str = "",
        category: str = "",
        note: str = "",
        idempotency_key: str = "",
        person: str = "",
    ) -> str:
        """Record money spent (direction=debit) or received (credit) on a household account,
        named as in list_accounts. day is YYYY-MM-DD (default today). category is a category
        name, e.g. 'Groceries'; leave it empty to sort it from the payee and note. person is
        who it was for ('Aarav'; empty = the whole family): "1200 on aarav uniform" is
        person Aarav, payee Uniform. Pass a unique idempotency_key so a retry never doubles
        it."""
        hid, mid, role = _who(write=True)
        from coffernest.routes.transactions import TxnIn, write_transaction
        from coffernest.services import categorize
        from coffernest.services.actions import find_person
        from coffernest.services.inbox import directory

        async with db.sessionmaker()() as session:
            d = await directory(session, hid)
            acc = d.by_name(account)
            if acc is None:
                return to_json({"error": f"No account called {account!r}; see list_accounts."})
            cat = d.category(category) if category else None
            book = await categorize.load_book(session, hid)
            person_id = find_person(book, person) if person else None
            if person and person_id is None:
                names = ", ".join(n.title() for n in book.people) or "none yet"
                return to_json(
                    {"error": f"No one called {person!r} (people: {names}); add with add_person."}
                )
            key = (
                uuid.uuid5(uuid.NAMESPACE_URL, f"vkf-mcp:{hid}:{idempotency_key}")
                if (idempotency_key)
                else None
            )
            rows = await write_transaction(
                session,
                await _ctx(session, hid, mid, role),
                TxnIn(
                    id=key,
                    account_id=acc.id,
                    date=date.fromisoformat(day) if day else date.today(),
                    amount=Decimal(str(amount)).quantize(Decimal("0.01")),
                    direction=Direction(direction),
                    category_id=cat,
                    person_id=person_id,
                    payee=payee[:120],
                    note=note[:2000],
                    tags=["mcp"],
                ),
            )
            first = rows[0].category_id
            return to_json(
                {
                    "recorded": True,
                    "matched_a_scheduled_entry": any(r.matched for r in rows),
                    "account": acc.name,
                    "category_found": cat is not None,
                    "category": book.labels.get(first, "") if first else "",
                    "for": person.title() if person_id else "Family",
                }
            )

    @server.tool()
    async def add_person(name: str, relation: str = "child") -> str:
        """Add someone entries can be for: a child (relation child), the spouse, a parent.
        No login is created. Then add_transaction and get_spend can use their name."""
        return await _setup("add_person", name=name, relation=relation)

    @server.tool()
    async def add_recurring_bill(
        name: str,
        amount: float,
        account: str,
        frequency: str = "monthly",
        day_of_month: int = 1,
        category: str = "",
        start: str = "",
        direction: str = "debit",
    ) -> str:
        """Add a repeating bill or income (monthly, quarterly or yearly on a day of the month),
        paid from/into a household account named as in list_accounts."""
        hid, mid, role = _who(write=True)
        from coffernest.routes.recurring import RuleIn, create_rule
        from coffernest.services.inbox import _rrule, directory

        async with db.sessionmaker()() as session:
            d = await directory(session, hid)
            acc = d.by_name(account)
            if acc is None:
                return to_json({"error": f"No account called {account!r}; see list_accounts."})
            first = date.fromisoformat(start) if start else date.today()
            rule = await create_rule(
                RuleIn(
                    name=name[:80],
                    amount=Decimal(str(amount)).quantize(Decimal("0.01")),
                    direction=Direction(direction),
                    account_id=acc.id,
                    category_id=d.category(category) if category else None,
                    payee=name[:120],
                    rrule=_rrule(frequency, day_of_month, first),
                    start_date=first,
                ),
                await _ctx(session, hid, mid, role),
                session,
            )
            live.publish(hid)
            live.publish(hid)
            return to_json({"added": True, "name": rule.name, "next": rule.next_date})

    async def _setup(fn_name: str, **kwargs: Any) -> str:
        hid, mid, role = _who(write=True)
        from coffernest.services import actions

        async with db.sessionmaker()() as session:
            try:
                out = await getattr(actions, fn_name)(
                    session, await _ctx(session, hid, mid, role), **kwargs
                )
            except actions.ActionError as exc:
                await session.rollback()
                return to_json({"error": str(exc)})
            return to_json({k: v for k, v in out.items() if k != "undo"})

    @server.tool()
    async def add_account(
        name: str,
        kind: str = "bank",
        institution: str = "",
        account_type: str = "",
        last4: str = "",
        balance: float = 0,
        ifsc: str = "",
        branch: str = "",
    ) -> str:
        """Add a bank, cash, wallet or investment account, e.g. name 'Axis Salary', institution
        'Axis Bank', account_type savings|salary|current|nre|nro|fd|ppf, last4 = last 4 digits
        only, balance = today's balance. IFSC and branch are optional. Full account numbers
        are never needed here (the owner can add one in the app, where it is stored
        encrypted). Loans and cards have their own tools."""
        return await _setup(
            "add_account",
            name=name,
            kind=kind,
            institution=institution,
            account_type=account_type,
            last4=last4,
            balance=balance,
            ifsc=ifsc,
            branch=branch,
        )

    @server.tool()
    async def add_loan(
        name: str,
        kind: str,
        outstanding: float,
        rate_pct: float,
        emis_left: int,
        next_emi_date: str,
        lender: str = "",
        emi: float = 0,
        rate_type: str = "fixed",
        paid_from: str = "",
    ) -> str:
        """Add a loan as it stands today: kind home|personal|car|education|gold|other,
        outstanding principal, annual rate %, EMIs left, next EMI date (YYYY-MM-DD), the EMI
        if known, rate_type fixed|floating, and paid_from = the account the EMI is debited from
        (name as in list_accounts) so it becomes an automatic monthly entry."""
        return await _setup(
            "add_loan",
            name=name,
            kind=kind,
            lender=lender,
            outstanding=outstanding,
            rate_pct=rate_pct,
            emis_left=emis_left,
            next_emi_date=next_emi_date,
            emi=emi or None,
            rate_type=rate_type,
            paid_from=paid_from,
        )

    @server.tool()
    async def add_card(
        name: str,
        issuer: str,
        credit_limit: float,
        statement_day: int,
        owed_now: float = 0,
        due_days: int = 20,
        last4: str = "",
        paid_from: str = "",
    ) -> str:
        """Add a credit card: limit, the day of the month its statement is made, what is owed
        on it today, days from statement to due date, last 4 digits, and the account the bill
        is usually paid from."""
        return await _setup(
            "add_card",
            name=name,
            issuer=issuer,
            credit_limit=credit_limit,
            statement_day=statement_day,
            owed_now=owed_now,
            due_days=due_days,
            last4=last4,
            paid_from=paid_from,
        )

    @server.tool()
    async def update_details(
        name: str,
        new_name: str = "",
        institution: str = "",
        account_number: str = "",
        last4: str = "",
        ifsc: str = "",
        branch: str = "",
        account_type: str = "",
        notes: str = "",
        credit_limit: float = 0,
        annual_interest_pct: float = 0,
        statement_day: int = 0,
        due_days: int = 0,
        sanctioned_amount: float = 0,
        rate_type: str = "",
        loan_start_date: str = "",
        original_tenure_months: int = 0,
        original_emi: float = 0,
    ) -> str:
        """Correct details of an existing account, card or loan, found by name as in
        list_accounts. Only fields given are changed; balances are not touched here.
        institution = bank or lender. account_number is the full number or reference when the
        owner asks to save it: it is stored encrypted, never returned, and only its last 4 show.
        For a card, last4 stays the card's own unless given. Cards: credit_limit,
        annual_interest_pct (e.g. 46.2), statement_day, due_days. Loans: sanctioned_amount
        (the original loan), rate_type fixed|floating, loan_start_date (YYYY-MM-DD, the
        original loan's first EMI, e.g. from a credit report's "date opened"),
        original_tenure_months and original_emi: with these the app rebuilds EMIs paid before
        the loan was entered, so interest and principal paid so far show."""
        return await _setup(
            "update_details",
            name=name,
            new_name=new_name,
            institution=institution,
            account_number=account_number,
            last4=last4,
            ifsc=ifsc,
            branch=branch,
            account_type=account_type,
            notes=notes,
            credit_limit=credit_limit or None,
            annual_interest_pct=annual_interest_pct or None,
            statement_day=statement_day or None,
            due_days=due_days or None,
            sanctioned_amount=sanctioned_amount or None,
            rate_type=rate_type,
            loan_start_date=loan_start_date,
            original_tenure_months=original_tenure_months or None,
            original_emi=original_emi or None,
        )

    @server.tool()
    async def stop_recurring(name: str, from_date: str = "") -> str:
        """Stop or remove a repeating bill, income or SIP (name as in the app's Repeating
        list) from from_date (YYYY-MM-DD, default today). Planned entries from that date are
        removed; entries already confirmed stay. A loan's EMI isn't stopped here."""
        return await _setup("stop_recurring", name=name, from_date=from_date)

    @server.tool()
    async def move_loan_to_card(loan: str, card: str, card_owed_includes_it: bool) -> str:
        """A loan that is billed on a credit card (loan on card, or a purchase converted to
        EMI) but was entered as an ordinary loan, so its EMI also shows as an auto-debit from a
        bank account. This makes it an EMI on that card with the same EMIs left and stops the
        bank entries. card_owed_includes_it: true when the card's owed figure already includes
        the loan's remaining principal (as a statement's total outstanding usually does), false
        to add it to the card's balance. Ask the user if unsure."""
        return await _setup(
            "move_loan_to_card",
            loan=loan,
            card=card,
            card_owed_includes_it=card_owed_includes_it,
        )

    @server.tool()
    async def get_inbox() -> str:
        """Recent files dropped in the inbox: what was found, what was added to the books, and
        items still waiting for approval (with file_id and item_id)."""
        hid, _, _ = _who()
        from coffernest.services import actions

        async with db.sessionmaker()() as session:
            return to_json(await actions.inbox_overview(session, hid))

    @server.tool()
    async def review_inbox_items(
        file_id: str, decision: str = "approve", item_ids: list[str] | None = None
    ) -> str:
        """Approve (add to the books) or skip items waiting in the inbox for one file. Leave
        item_ids empty for every pending item of that file."""
        hid, mid, role = _who(write=True)
        from coffernest.services import actions

        async with db.sessionmaker()() as session:
            try:
                out = await actions.review_inbox(
                    session,
                    await _ctx(session, hid, mid, role),
                    file_id=file_id,
                    approve=decision != "skip",
                    item_ids=item_ids,
                )
            except actions.ActionError as exc:
                return to_json({"error": str(exc)})
            return to_json({k: v for k, v in out.items() if k != "undo"})

    @server.tool()
    async def upload_document(filename: str, content_base64: str) -> str:
        """Drop a statement, bill or loan screenshot (base64, up to 10 MB) into the household's
        inbox. It's read in the background; check the app's Inbox for results."""
        hid, mid, _ = _who(write=True)
        try:
            data = base64.b64decode(content_base64, validate=True)
        except (binascii.Error, ValueError):
            return to_json({"error": "content_base64 isn't valid base64"})
        if len(data) > 10 * 1024 * 1024:
            return to_json({"error": "Files over 10 MB: drop them in the app instead"})
        from coffernest.ingest.prepare import sniff
        from coffernest.routes.attachments import _SAFE
        from coffernest.services import inbox
        from coffernest.storage import get_storage

        if sniff(filename, "", data) == "unknown":
            return to_json({"error": "Send a PDF, image, CSV or Excel file"})
        async with db.sessionmaker()() as session:
            member = await session.get(Member, mid)
            safe = _SAFE.sub("-", filename)[-80:] or "file"
            att = Attachment(
                household_id=hid,
                object_key=f"{hid}/{datetime.now(UTC):%Y/%m}/{uuid.uuid4().hex}-{safe}",
                filename=filename[:200],
                mime="application/octet-stream",
                size=len(data),
                kind=AttachmentKind.statement,
                status=UploadStatus.stored,
                uploaded_by=member.user_id if member else None,
                sha256=hashlib.sha256(data).hexdigest(),
                source="mcp",
            )
            get_storage().put(att.object_key, data, att.mime)
            session.add(att)
            await session.flush()
            ext = Extraction(household_id=hid, attachment_id=att.id, uploaded_by=mid)
            session.add(ext)
            await session.commit()
            inbox.enqueue(ext.id)
            live.publish(hid, "inbox")
            return to_json({"queued": True, "file": filename})

    for skill in skills.builtin_skills().values():
        if skill.shared:
            continue
        _register_prompt(server, skill)
    return server


def _register_prompt(server: MCPServer[Any], skill: skills.Skill) -> None:
    body = skill.body

    async def prompt() -> str:
        return (
            f"Follow this Coffernest playbook ({skill.title}). Use the Coffernest tools for every "
            f"number.\n\n{body}"
        )

    server.prompt(name=skill.name, title=skill.title, description=skill.description)(prompt)


_server: MCPServer[Any] | None = None


def get_server() -> MCPServer[Any]:
    global _server
    if _server is None:
        _server = build(get_settings())
    return _server


def asgi_app() -> Starlette:
    settings = get_settings()
    host = settings.public_url.split("://", 1)[-1].split("/")[0]
    return get_server().streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[
                host,
                "localhost:*",
                "127.0.0.1:*",
                *(["test"] if settings.env == "test" else []),
            ],
            allowed_origins=[settings.public_url, "https://claude.ai", "http://localhost:*"],
        ),
    )
