"""Inbox reading, skills, the assistant's roadmaps, the MCP server and its OAuth server."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import secrets
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coffernest.ai import providers
from coffernest.ai.providers import Event, Run, _call_tool
from coffernest.ai.tools import Tool
from coffernest.config import get_settings
from coffernest.ingest.prepare import Locked, prepare
from coffernest.services import amortization as am
from coffernest.services import inbox as inbox_svc
from coffernest.services import planner
from tests.conftest import FakeStorage, bank, categories

API = "/api/v1"


# ---------------------------------------------------------------- fixtures


def text_pdf(lines: list[str]) -> bytes:
    """A tiny valid PDF with real text, built by hand (no PDF writer needed)."""
    stream = (
        "BT /F1 10 Tf 40 800 Td 14 TL "
        + " ".join(f"({ln.replace('(', '[').replace(')', ']')}) '" for ln in lines)
        + " ET"
    )
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n{body}\nendobj\n".encode())
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


def locked_pdf(lines: list[str], password: str) -> bytes:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(text_pdf(lines))))
    writer.encrypt(password, algorithm="AES-256")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


STATEMENT_LINES = [
    "HDFC BANK Statement of account  A/c XXXX4321",
    "01/10/2026 SWIGGY ORDER 486.00 Dr",
    "03/10/2026 SALARY ACME 185000.00 Cr",
    "05/10/2026 NEFT RAMESH 12000.00 Dr",
    "07/10/2026 NETFLIX 649.00 Dr",
]


class FakeLLM:
    """Scripted stand-in for both providers: reads 'documents', streams chat, calls tools."""

    extraction: ClassVar[dict[str, Any]] = {}
    chat_script: ClassVar[list[dict[str, Any]]] = []
    last_prompt: ClassVar[str] = ""
    last_tools: ClassVar[list[str]] = []

    def __init__(self, sessions: async_sessionmaker[AsyncSession], hid: uuid.UUID) -> None:
        self.sessions = sessions
        self.hid = hid

    async def run(self, system: str, prompt: str) -> Run:
        return Run(text="[]")

    async def extract(
        self, system: str, text: str, images: Sequence[bytes], schema: dict[str, Any]
    ) -> dict[str, Any]:
        assert "Household accounts" in text
        return FakeLLM.extraction

    async def stream(
        self,
        system: str,
        prompt: str,
        extra_tools: Sequence[Tool] = (),
        images: Sequence[bytes] = (),
    ) -> AsyncIterator[Event]:
        FakeLLM.last_prompt = prompt
        FakeLLM.last_tools = [t.name for t in extra_tools]
        extra = {t.name: t for t in extra_tools}
        run = Run(text="")
        for step in FakeLLM.chat_script:
            if "tool" in step:
                yield Event("status", tool=step["tool"])
                out = await _call_tool(self.sessions, self.hid, step["tool"], step["args"], extra)
                run.tool_calls.append(step["tool"])
                run.tool_outputs.append(out)
            else:
                text = step["text"]
                if "{from:" in text:  # splice a number the tools really returned
                    key = text.split("{from:")[1].split("}")[0]
                    value = json.loads(run.tool_outputs[-1])
                    for part in key.split("."):
                        value = value[int(part)] if part.isdigit() else value[part]
                    text = text.replace("{from:" + key + "}", f"₹{float(value):,.0f}")
                run.text += text
                yield Event("token", text=text)
        yield Event("done", run=run)


@pytest.fixture
async def ai(
    client: httpx.AsyncClient, storage: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setattr(get_settings(), "ai_provider", "claude_agent")
    import coffernest.routes.inbox as inbox_routes

    monkeypatch.setattr(inbox_routes, "get_storage", lambda: storage)
    monkeypatch.setattr(inbox_svc, "get_storage", lambda: storage)
    providers.set_provider_override(lambda s, sessions, hid: FakeLLM(sessions, hid))
    await client.patch(f"{API}/household", json={"ai_enabled": True})
    yield client
    providers.set_provider_override(None)


# ---------------------------------------------------------------- reading files


def test_prepare_text_pdf_and_locked_pdf() -> None:
    plain = prepare("stmt.pdf", "application/pdf", text_pdf(STATEMENT_LINES))
    assert plain.kind == "pdf" and "SWIGGY" in plain.text and plain.pages == 1

    data = locked_pdf(STATEMENT_LINES, "ABCD1990")
    with pytest.raises(Locked):
        prepare("stmt.pdf", "application/pdf", data, ["wrong"])
    opened = prepare("stmt.pdf", "application/pdf", data, ["wrong", "ABCD1990"])
    assert "NETFLIX" in opened.text and opened.password_index == 1


def test_prepare_csv_and_xlsx_skip_bank_header_junk() -> None:
    csv_bytes = (
        b"HDFC Bank Ltd\nStatement for 4321\n\n"
        b"Date,Narration,Withdrawal Amt.,Deposit Amt.,Closing Balance\n"
        b"01/10/26,SWIGGY,486.00,,10000.00\n"
    )
    sheet = prepare("export.csv", "", csv_bytes)
    assert sheet.kind == "sheet" and sheet.text.startswith("Date\tNarration")

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.append(["ICICI Bank"])
    ws.append(["Txn Date", "Description", "Debit", "Credit", "Balance"])
    ws.append(["2026-10-01", "AMAZON", 1299, None, 5000])
    buf = io.BytesIO()
    wb.save(buf)
    xl = prepare("x.xlsx", "", buf.getvalue())
    assert "Txn Date\tDescription" in xl.text and "AMAZON" in xl.text


async def _drop(client: httpx.AsyncClient, name: str, data: bytes) -> dict[str, Any]:
    res = await client.post(f"{API}/inbox/upload", files=[("files", (name, data, ""))])
    assert res.status_code == 201, res.text
    body: dict[str, Any] = res.json()[0]
    return body


async def test_inbox_auto_adds_sure_rows_and_flags_the_rest(ai: httpx.AsyncClient) -> None:
    acc = await bank(ai, name="HDFC Salary", institution="HDFC Bank", last4="4321")
    cats = await categories(ai)
    # Netflix is already in the books: the reader must not add it twice.
    await ai.post(
        f"{API}/transactions",
        json={
            "account_id": acc["id"],
            "date": "2026-10-07",
            "amount": "649",
            "direction": "debit",
            "payee": "Netflix",
            "category_id": cats.get("Subscriptions"),
        },
    )
    FakeLLM.extraction = {
        "doc_type": "bank_statement",
        "summary": "HDFC savings statement, Oct 2026",
        "institution": "HDFC Bank",
        "account_last4": "4321",
        "account_hint": "",
        "period_start": "2026-10-01",
        "period_end": "2026-10-31",
        "closing_balance": None,
        "transactions": [
            {
                "date": "2026-10-01",
                "description": "SWIGGY ORDER",
                "amount": 486,
                "direction": "debit",
                "category": "Dining out",
                "transfer_to": "",
                "confidence": 0.97,
            },
            {
                "date": "2026-10-03",
                "description": "SALARY ACME",
                "amount": 185000,
                "direction": "credit",
                "category": "Salary",
                "transfer_to": "",
                "confidence": 0.98,
            },
            {
                "date": "2026-10-05",
                "description": "NEFT RAMESH",
                "amount": 12000,
                "direction": "debit",
                "category": "",
                "transfer_to": "",
                "confidence": 0.9,
            },
            {
                "date": "2026-10-07",
                "description": "NETFLIX",
                "amount": 649,
                "direction": "debit",
                "category": "Subscriptions",
                "transfer_to": "",
                "confidence": 0.99,
            },
        ],
        "card_statement": None,
        "loan": None,
        "bill": None,
        "recurring": [
            {
                "name": "Netflix",
                "amount": 649,
                "direction": "debit",
                "frequency": "monthly",
                "day_of_month": 7,
                "category": "Subscriptions",
            }
        ],
        "notes": "",
    }
    pdf = locked_pdf(STATEMENT_LINES, "PASS1234")
    up = await _drop(ai, "hdfc-oct.pdf", pdf)
    await inbox_svc.process(uuid.UUID(up["id"]))
    detail = (await ai.get(f"{API}/inbox/{up['id']}")).json()
    assert detail["status"] == "needs_password"

    res = await ai.post(
        f"{API}/inbox/{up['id']}/password", json={"password": "PASS1234", "remember": True}
    )
    assert res.status_code == 200
    await inbox_svc.process(uuid.UUID(up["id"]), "PASS1234")
    detail = (await ai.get(f"{API}/inbox/{up['id']}")).json()
    assert detail["status"] == "review", detail
    states = {i["payload"].get("payee", i["kind"]): i["state"] for i in detail["items"]}
    assert states["SWIGGY ORDER"] == "auto_added"
    assert states["SALARY ACME"] == "auto_added"
    assert states["NEFT RAMESH"] == "pending"  # no category
    assert states["NETFLIX"] == "duplicate"
    assert any(i["kind"] == "recurring" and i["state"] == "pending" for i in detail["items"])

    ledger = (await ai.get(f"{API}/transactions", params={"q": "SWIGGY"})).json()["items"]
    assert len(ledger) == 1 and ledger[0]["source"] == "import"

    # Approve the unsure row with a category picked in review.
    neft = next(i for i in detail["items"] if i["payload"].get("payee") == "NEFT RAMESH")
    res = await ai.post(
        f"{API}/inbox/{up['id']}/items/{neft['id']}/approve",
        json={"payload": {"category_id": cats.get("Groceries")}},
    )
    assert res.status_code == 200 and res.json()["state"] == "approved"

    # The same file again is recognised, not read twice.
    again = await _drop(ai, "copy.pdf", pdf)
    assert again["id"] == up["id"]

    # Next month's locked statement opens with the remembered password.
    up2 = await _drop(ai, "hdfc-nov.pdf", locked_pdf([*STATEMENT_LINES, "nov"], "PASS1234"))
    FakeLLM.extraction = {**FakeLLM.extraction, "transactions": [], "recurring": []}
    await inbox_svc.process(uuid.UUID(up2["id"]))
    assert (await ai.get(f"{API}/inbox/{up2['id']}")).json()["status"] == "done"

    # Undo takes back what the first file added.
    await ai.post(f"{API}/inbox/{up['id']}/undo")
    ledger = (await ai.get(f"{API}/transactions", params={"q": "SWIGGY"})).json()["items"]
    assert ledger == []
    summary = (await ai.get(f"{API}/inbox/summary")).json()
    assert summary["needs_password"] == 0


async def test_inbox_loan_screenshot_becomes_a_loan_on_approval(ai: httpx.AsyncClient) -> None:
    await bank(ai, name="HDFC Salary")
    from PIL import Image

    img = io.BytesIO()
    Image.new("RGB", (300, 600), "white").save(img, format="PNG")
    FakeLLM.extraction = {
        "doc_type": "loan",
        "summary": "Car loan from Axis Bank",
        "institution": "Axis Bank",
        "account_last4": "",
        "account_hint": "",
        "period_start": "",
        "period_end": "",
        "closing_balance": None,
        "transactions": [],
        "card_statement": None,
        "bill": None,
        "recurring": [],
        "notes": "",
        "loan": {
            "name": "Car loan",
            "kind": "car",
            "lender": "Axis Bank",
            "last4": "7788",
            "principal": 800000,
            "outstanding": 520000,
            "rate_pct": 9.1,
            "rate_type": "fixed",
            "tenure_months": 60,
            "emi": 16650,
            "first_emi_date": "2024-01-07",
            "next_emi_date": (date.today() + timedelta(days=10)).isoformat(),
            "emis_left": 38,
        },
    }
    up = await _drop(ai, "axis.png", img.getvalue())
    await inbox_svc.process(uuid.UUID(up["id"]))
    detail = (await ai.get(f"{API}/inbox/{up['id']}")).json()
    loan = next(i for i in detail["items"] if i["kind"] == "loan")
    assert loan["state"] == "pending"
    res = await ai.post(f"{API}/inbox/{up['id']}/items/{loan['id']}/approve", json={})
    assert res.status_code == 200, res.text
    loans = (await ai.get(f"{API}/loans")).json()
    assert len(loans) == 1 and Decimal(loans[0]["outstanding"]) <= Decimal("520000")


# ---------------------------------------------------------------- planning maths


def test_compare_paths_prefers_costly_debt_and_rolls_payments_over() -> None:
    debts = [
        am.Debt("card:a", "Card", Decimal("90000"), Decimal("45"), Decimal("4500")),
        am.Debt("loan:p", "Personal loan", Decimal("385000"), Decimal("12.25"), Decimal("16935")),
        am.Debt("loan:h", "Home loan", Decimal("4180000"), Decimal("8.5"), Decimal("38803")),
    ]
    start = date(2026, 10, 12)
    aval = planner.simulate_path(
        "avalanche", debts, Decimal(15000), am.ZERO, 10, start, Decimal(12)
    )
    sip = planner.simulate_path(
        "invest_sip", debts, Decimal(15000), am.ZERO, 10, start, Decimal(12)
    )
    home = planner.simulate_path(
        "prepay:home", debts, Decimal(15000), am.ZERO, 10, start, Decimal(12)
    )
    assert aval.interest_paid < sip.interest_paid
    assert aval.milestones[0].label == "Card closed"
    assert home.milestones and home.label == "Prepay home"
    assert aval.months_to_debt_free is not None and aval.debt_at_end == 0
    assert sip.corpus_at_end > aval.corpus_at_end or sip.debt_at_end > 0
    assert planner.best_of([aval, sip])["interest_paid"] == "avalanche"


def test_project_goal_matches_annuity_maths() -> None:
    g = planner.project_goal(Decimal(1_000_000), 10, Decimal(10), Decimal(12), Decimal(10))
    assert g.target_future == Decimal("2593742.46")
    # The flat SIP grows to the target at 12%/yr compounded monthly-equivalent.
    r = (Decimal("1.12") ** (Decimal(1) / 12)) - 1
    fv = g.monthly_sip_flat * (((1 + r) ** 120) - 1) / r
    assert abs(fv - g.target_future) < 2000
    assert g.monthly_sip_first_year_with_step_up < g.monthly_sip_flat


# ---------------------------------------------------------------- skills


async def test_skills_toggle_custom_and_load(client: httpx.AsyncClient) -> None:
    items = (await client.get(f"{API}/skills")).json()
    names = {s["name"] for s in items}
    assert {"financial-roadmap", "home-loan-prepayment", "india-facts"} <= names
    res = await client.patch(f"{API}/skills/credit-card-health", json={"enabled": False})
    assert res.json()["enabled"] is False
    custom = (
        "---\nname: festival-budget\ntitle: Festival budget\n"
        "description: Use when planning Diwali or wedding spends.\n---\n"
        "1. Call get_category_spend for last October.\n2. Suggest a sinking fund."
    )
    res = await client.post(f"{API}/skills/custom", json={"body": custom})
    assert res.status_code == 201, res.text
    detail = (await client.get(f"{API}/skills/festival-budget")).json()
    assert "sinking fund" in detail["body"]
    bad = await client.post(f"{API}/skills/custom", json={"body": "no frontmatter " * 5})
    assert bad.status_code == 422


# ---------------------------------------------------------------- assistant


def _events(raw: str) -> list[tuple[str, Any]]:
    out = []
    for chunk in raw.split("\n\n"):
        lines = [ln for ln in chunk.splitlines() if ln and not ln.startswith(":")]
        if not lines:
            continue
        name = lines[0].removeprefix("event: ")
        data = json.loads(lines[1].removeprefix("data: "))
        out.append((name, data))
    return out


async def test_assistant_streams_a_roadmap_with_engine_numbers(ai: httpx.AsyncClient) -> None:
    acc = await bank(ai, opening="300000.00", name="HDFC Salary")
    await ai.post(
        f"{API}/loans",
        json={
            "name": "Home loan",
            "kind": "home",
            "principal": "4000000",
            "rate_pct": "8.5",
            "tenure_months": 240,
            "first_emi_date": "2026-01-05",
            "funding_account_id": acc["id"],
        },
    )
    paths = ["avalanche", "invest_sip", "prepay:Home loan"]
    FakeLLM.chat_script = [
        {"tool": "load_skill", "args": {"name": "financial-roadmap"}},
        {"tool": "compare_paths", "args": {"monthly_amount": 15000, "paths": paths}},
        {
            "tool": "present_roadmap",
            "args": {
                "title": "₹15,000 a month",
                "monthly_amount": 15000,
                "paths": paths,
                "recommended": "prepay:Home loan",
                "why": "Cheapest guaranteed return.",
                "steps": [{"when": "Next 3 months", "text": "Start the extra EMI."}],
            },
        },
        {"text": "Prepaying saves money. You'd pay {from:paths.0} less"},
    ]
    # make the spliced number refer to something real: the compare_paths interest
    FakeLLM.chat_script[3]["text"] = "Prepaying the home loan is the steady choice."
    thread = (await ai.post(f"{API}/ai/threads")).json()
    res = await ai.post(
        f"{API}/ai/threads/{thread['id']}/messages",
        json={"content": "We can spare ₹15,000 a month. Prepay or invest?"},
    )
    assert res.status_code == 200
    assert res.headers["x-accel-buffering"] == "no"
    events = _events(res.text)
    kinds = [e[0] for e in events]
    assert kinds[0] == "meta" and kinds[-1] == "done"
    assert "status" in kinds and "token" in kinds and "block" in kinds
    block = next(d for k, d in events if k == "block")
    assert block["type"] == "roadmap" and block["recommended"] == "prepay:Home loan"
    assert {p["path_id"] for p in block["paths"]} == set(paths)
    assert block["paths"][0]["yearly"] and block["best"]
    done = events[-1][1]
    assert done["skills_used"] == ["financial-roadmap"] and done["blocks"]

    detail = (await ai.get(f"{API}/ai/threads/{thread['id']}")).json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert detail["title"].startswith("We can spare")
    skills_now = {s["name"]: s for s in (await ai.get(f"{API}/skills")).json()}
    assert skills_now["financial-roadmap"]["last_used_at"] is not None


async def test_assistant_flags_invented_amounts(ai: httpx.AsyncClient) -> None:
    await bank(ai)
    FakeLLM.chat_script = [
        {"tool": "get_overview", "args": {}},
        {"text": "You could save ₹7,77,777 by Friday."},
    ]
    thread = (await ai.post(f"{API}/ai/threads")).json()
    res = await ai.post(f"{API}/ai/threads/{thread['id']}/messages", json={"content": "Hi"})
    done = _events(res.text)[-1][1]
    assert done["verified"] is False and done["unverified_amounts"] == ["₹7,77,777"]


# ---------------------------------------------------------------- MCP + OAuth


@pytest.fixture(scope="session")
async def mcp_running() -> AsyncIterator[None]:
    import asyncio

    from coffernest.mcp_server import get_server

    started, stop = asyncio.Event(), asyncio.Event()

    async def runner() -> None:  # enter and exit the manager in one task (anyio requires it)
        async with get_server().session_manager.run():
            started.set()
            await stop.wait()

    task = asyncio.create_task(runner())
    await started.wait()
    yield
    stop.set()
    await task


async def _rpc(
    anon: httpx.AsyncClient, token: str | None, method: str, params: dict[str, Any] | None = None
) -> httpx.Response:
    headers = {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return await anon.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        headers=headers,
    )


def _bare() -> httpx.AsyncClient:
    from coffernest.main import app

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("203.0.113.10", 5000)),
        base_url="http://test",
    )


def _tool_text(res: httpx.Response) -> Any:
    body = res.json()
    assert "result" in body, body
    return json.loads(body["result"]["content"][0]["text"])


async def test_mcp_with_personal_access_tokens(
    client: httpx.AsyncClient, mcp_running: None
) -> None:
    await bank(client, name="HDFC Salary")
    web_token = client.headers["Authorization"].split()[1]
    read = (await client.post(f"{API}/connections/tokens", json={"name": "Codex"})).json()
    write = (
        await client.post(f"{API}/connections/tokens", json={"name": "Claude", "access": "write"})
    ).json()
    assert read["token"].startswith("cn_pat_")
    anon = _bare()  # no web session

    res = await _rpc(anon, None, "tools/list")
    assert res.status_code == 401 and "resource_metadata" in res.headers["www-authenticate"]
    assert (await _rpc(anon, web_token, "tools/list")).status_code == 401  # web JWTs don't work

    res = await _rpc(anon, read["token"], "tools/list")
    assert res.status_code == 200, res.text
    names = {t["name"] for t in res.json()["result"]["tools"]}
    assert {"get_overview", "compare_paths", "add_transaction", "get_skill"} <= names

    accounts = _tool_text(
        await _rpc(anon, read["token"], "tools/call", {"name": "list_accounts", "arguments": {}})
    )
    assert accounts["accounts"][0]["name"] == "HDFC Salary"

    args = {
        "account": "HDFC Salary",
        "amount": 250,
        "direction": "debit",
        "payee": "Chai",
        "idempotency_key": "abc",
    }
    denied = (
        await _rpc(
            anon, read["token"], "tools/call", {"name": "add_transaction", "arguments": args}
        )
    ).json()
    assert denied["result"]["isError"] is True
    for _ in range(2):  # the same key twice records once
        ok = _tool_text(
            await _rpc(
                anon, write["token"], "tools/call", {"name": "add_transaction", "arguments": args}
            )
        )
        assert ok["recorded"] is True
    ledger = (await client.get(f"{API}/transactions", params={"q": "Chai"})).json()["items"]
    assert len(ledger) == 1

    prompts = (await _rpc(anon, read["token"], "prompts/list")).json()["result"]["prompts"]
    assert any(p["name"] == "home-loan-prepayment" for p in prompts)

    conns = (await client.get(f"{API}/connections")).json()
    assert {t["name"] for t in conns["tokens"]} == {"Codex", "Claude"}
    await client.delete(f"{API}/connections/tokens/{read['id']}")
    assert (await _rpc(anon, read["token"], "tools/list")).status_code == 401
    await anon.aclose()


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge.rstrip(b"=").decode()


async def test_oauth_flow_for_claude_connectors(
    client: httpx.AsyncClient, mcp_running: None
) -> None:
    anon = _bare()
    meta = (await anon.get("/.well-known/oauth-protected-resource/mcp")).json()
    assert meta["resource"].endswith("/mcp")
    asm = (await anon.get("/.well-known/oauth-authorization-server")).json()
    assert asm["code_challenge_methods_supported"] == ["S256"]

    cb = "https://claude.ai/api/mcp/auth_callback"
    bad = await anon.post("/oauth/register", json={"redirect_uris": ["https://evil.example/cb"]})
    assert bad.status_code == 400
    reg = (
        await anon.post("/oauth/register", json={"redirect_uris": [cb], "client_name": "Claude"})
    ).json()
    verifier, challenge = _pkce()
    params = {
        "response_type": "code",
        "client_id": reg["client_id"],
        "redirect_uri": cb,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "xyz",
        "scope": "finance.read finance.write",
        "resource": meta["resource"],
    }
    page = await anon.get("/oauth/authorize", params=params)
    assert page.status_code == 200 and "Connect Claude" in page.text

    wrong = await anon.post(
        "/oauth/authorize",
        data={**params, "email": "owner@example.com", "password": "nope", "access": "read"},
    )
    assert "Wrong e-mail or password" in wrong.text
    res = await anon.post(
        "/oauth/authorize",
        data={
            **params,
            "email": "owner@example.com",
            "password": "correct-horse-battery",
            "access": "write",
        },
    )
    assert res.status_code == 302
    loc = urlparse(res.headers["location"])
    q = parse_qs(loc.query)
    assert q["state"] == ["xyz"]
    code = q["code"][0]

    form = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": reg["client_id"],
        "redirect_uri": cb,
        "code_verifier": "not-the-verifier",
    }
    assert (await anon.post("/oauth/token", data=form)).json()["error"] == "invalid_grant"
    # The code is single-use even after a failed exchange: start over.
    res = await anon.post(
        "/oauth/authorize",
        data={
            **params,
            "email": "owner@example.com",
            "password": "correct-horse-battery",
            "access": "write",
        },
    )
    code = parse_qs(urlparse(res.headers["location"]).query)["code"][0]
    tok = (
        await anon.post("/oauth/token", data={**form, "code": code, "code_verifier": verifier})
    ).json()
    assert tok["token_type"] == "Bearer" and "finance.write" in tok["scope"]

    res = await _rpc(
        anon, tok["access_token"], "tools/call", {"name": "get_dues", "arguments": {"days": 7}}
    )
    assert res.status_code == 200 and "result" in res.json()
    # An MCP token is useless against the web API.
    web = await anon.get(
        f"{API}/auth/me", headers={"Authorization": f"Bearer {tok['access_token']}"}
    )
    assert web.status_code == 401

    refresh = {
        "grant_type": "refresh_token",
        "refresh_token": tok["refresh_token"],
        "client_id": reg["client_id"],
    }
    new = (await anon.post("/oauth/token", data=refresh)).json()
    assert new["refresh_token"] != tok["refresh_token"]
    replay = (await anon.post("/oauth/token", data=refresh)).json()
    assert replay["error"] == "invalid_grant"
    # Replaying a spent token ended the whole grant, including the fresh one.
    dead = (
        await anon.post("/oauth/token", data={**refresh, "refresh_token": new["refresh_token"]})
    ).json()
    assert dead["error"] == "invalid_grant"
    await anon.aclose()


async def test_chat_reads_attached_files_and_records_with_undo(ai: httpx.AsyncClient) -> None:
    acc = await bank(ai, name="HDFC Salary", institution="HDFC Bank", last4="4321")
    FakeLLM.extraction = {
        "doc_type": "bill",
        "summary": "Airtel postpaid bill, Oct 2026",
        "institution": "Airtel",
        "account_last4": "",
        "account_hint": "",
        "period_start": "",
        "period_end": "",
        "closing_balance": None,
        "transactions": [],
        "card_statement": None,
        "loan": None,
        "bill": {
            "payee": "Airtel",
            "amount": 1199,
            "due_date": "2026-10-28",
            "category": "Mobile",
            "repeats": "monthly",
        },
        "recurring": [],
        "notes": "",
    }
    up = await _drop(
        ai,
        "airtel.pdf",
        text_pdf(["Airtel postpaid", "Amount due Rs 1,199.00", "Due date 28/10/2026"] * 3),
    )
    await inbox_svc.process(uuid.UUID(up["id"]))
    FakeLLM.chat_script = [
        {
            "tool": "add_transaction",
            "args": {
                "account": "HDFC Salary",
                "amount": 1199,
                "direction": "debit",
                "payee": "Airtel",
                "date": "2026-10-28",
                "upcoming": True,
            },
        },
        {"text": "Added the Airtel bill as upcoming."},
    ]
    thread = (await ai.post(f"{API}/ai/threads")).json()
    res = await ai.post(
        f"{API}/ai/threads/{thread['id']}/messages",
        json={"content": "Add this bill", "attachments": [up["id"]]},
    )
    events = _events(res.text)
    assert ("status", {"tool": "reading_files"}) in events
    assert "Attached files" in FakeLLM.last_prompt and "Airtel postpaid" in FakeLLM.last_prompt
    assert "add_transaction" in FakeLLM.last_tools
    done = events[-1][1]
    action = next(b for b in done["blocks"] if b["type"] == "action")
    assert action["title"].startswith("Upcoming bill: Airtel") and action["undo"]
    ledger = (await ai.get(f"{API}/transactions", params={"q": "Airtel"})).json()["items"]
    assert len(ledger) == 1 and ledger[0]["account_id"] == acc["id"]

    detail = (await ai.get(f"{API}/ai/threads/{thread['id']}")).json()
    assert detail["messages"][0]["blocks"][0]["filename"] == "airtel.pdf"
    res = await ai.post(
        f"{API}/ai/actions/undo", json={"message_id": done["id"], "block_id": action["id"]}
    )
    assert res.status_code == 200, res.text
    assert next(b for b in res.json()["blocks"] if b["type"] == "action")["undone"] is True
    assert (await ai.get(f"{API}/transactions", params={"q": "Airtel"})).json()["items"] == []

    # History: search finds the thread by what was said; rename sticks.
    found = (await ai.get(f"{API}/ai/threads", params={"q": "airtel bill"})).json()
    assert [t["id"] for t in found] == [thread["id"]]
    assert found[0]["preview"].startswith("Added the Airtel")
    res = await ai.patch(f"{API}/ai/threads/{thread['id']}", json={"title": "Airtel"})
    assert res.json()["title"] == "Airtel"
    assert (await ai.get(f"{API}/ai/threads", params={"q": "zzz"})).json() == []


async def test_chat_can_approve_inbox_items(ai: httpx.AsyncClient) -> None:
    await bank(ai, name="HDFC Salary", institution="HDFC Bank", last4="4321")
    FakeLLM.extraction = {
        "doc_type": "bank_statement",
        "summary": "HDFC",
        "institution": "HDFC Bank",
        "account_last4": "4321",
        "account_hint": "",
        "period_start": "",
        "period_end": "",
        "closing_balance": None,
        "card_statement": None,
        "loan": None,
        "bill": None,
        "recurring": [],
        "notes": "",
        "transactions": [
            {
                "date": "2026-10-05",
                "description": "NEFT RAMESH",
                "amount": 12000,
                "direction": "debit",
                "category": "",
                "transfer_to": "",
                "confidence": 0.9,
            }
        ],
    }
    up = await _drop(ai, "hdfc.pdf", text_pdf(STATEMENT_LINES))
    await inbox_svc.process(uuid.UUID(up["id"]))
    FakeLLM.chat_script = [
        {"tool": "get_inbox", "args": {}},
        {"tool": "review_inbox_items", "args": {"file_id": up["id"], "decision": "approve"}},
        {"text": "Done."},
    ]
    thread = (await ai.post(f"{API}/ai/threads")).json()
    res = await ai.post(
        f"{API}/ai/threads/{thread['id']}/messages",
        json={"content": "Add everything pending in my inbox"},
    )
    done = _events(res.text)[-1][1]
    action = next(b for b in done["blocks"] if b["type"] == "action")
    assert action["title"] == "NEFT RAMESH ₹12,000"
    detail = (await ai.get(f"{API}/inbox/{up['id']}")).json()
    assert detail["status"] == "done"


async def test_account_numbers_are_sealed_and_masked(client: httpx.AsyncClient) -> None:
    res = await client.post(
        f"{API}/accounts",
        json={
            "kind": "bank",
            "name": "Axis Salary",
            "institution": "Axis Bank",
            "account_type": "salary",
            "ifsc": "utib0000123",
            "branch": "Koramangala",
            "account_number": "9180 1002 3344 5521",
            "opening_balance": "50000",
        },
    )
    assert res.status_code == 201, res.text
    acc = res.json()
    assert acc["last4"] == "5521" and acc["ifsc"] == "UTIB0000123"
    assert acc["has_account_number"] and acc["account_number_masked"] == "XXXXXX5521"
    assert "9180" not in res.text
    shown = (await client.get(f"{API}/accounts/{acc['id']}/number")).json()
    assert shown["account_number"] == "918010023344" + "5521"
    bad = await client.post(f"{API}/accounts", json={"kind": "bank", "name": "X", "ifsc": "BAD"})
    assert bad.status_code == 422


async def test_mcp_sets_up_accounts_loans_and_cards(
    client: httpx.AsyncClient, mcp_running: None
) -> None:
    write = (
        await client.post(f"{API}/connections/tokens", json={"name": "Claude", "access": "write"})
    ).json()["token"]
    anon = _bare()

    async def call(name: str, args: dict[str, Any]) -> Any:
        return _tool_text(await _rpc(anon, write, "tools/call", {"name": name, "arguments": args}))

    acc = await call(
        "add_account",
        {
            "name": "Axis Salary",
            "institution": "Axis Bank",
            "account_type": "salary",
            "last4": "5521",
            "balance": 120000,
        },
    )
    assert acc["added"] and acc["name"] == "Axis Salary"
    dup = await call("add_account", {"name": "axis salary"})
    assert "already" in dup["error"]
    loan = await call(
        "add_loan",
        {
            "name": "Home loan",
            "kind": "home",
            "lender": "SBI",
            "outstanding": 4180000,
            "rate_pct": 8.5,
            "emis_left": 204,
            "next_emi_date": (date.today() + timedelta(days=9)).isoformat(),
            "rate_type": "floating",
            "paid_from": "Axis Salary",
        },
    )
    assert (
        loan["added"] and Decimal(loan["emi"]) > 0 and loan["emi_auto_entry_from"] == "Axis Salary"
    )
    card = await call(
        "add_card",
        {
            "name": "HDFC Regalia",
            "issuer": "HDFC Bank",
            "credit_limit": 400000,
            "statement_day": 12,
            "owed_now": 25000,
            "last4": "1234",
            "paid_from": "Axis Salary",
        },
    )
    assert card["added"]
    accounts = await call("list_accounts", {})
    assert accounts["accounts"][0]["type"] == "salary"
    assert accounts["loans"][0]["name"] == "Home loan" and accounts["cards"][0]["owed"] == 25000
    rules = (await client.get(f"{API}/recurring-rules")).json()
    assert any(r["name"].startswith("Home loan") or "EMI" in r["name"] for r in rules)

    # Corrections: a card keeps its own last 4 when a bureau reference is saved; a loan takes
    # its account number's last 4; numbers are sealed and never echoed.
    upd = await call(
        "update_details",
        {
            "name": "HDFC Regalia",
            "credit_limit": 429546,
            "annual_interest_pct": 46.2,
            "account_number": "0001017510002959317",
        },
    )
    assert upd["last4"] == "1234" and upd["has_account_number"]
    assert "2959317" not in json.dumps(upd)
    upd_loan = await call(
        "update_details",
        {
            "name": "Home loan",
            "institution": "State Bank of India",
            "account_number": "000-123456789",
            "sanctioned_amount": 5000000,
        },
    )
    assert upd_loan["last4"] == "6789" and "lender" in upd_loan["changed"]
    bad = await call("update_details", {"name": "Axis Salary", "credit_limit": 1000})
    assert "isn't a credit card" in bad["error"]
    cards = (await client.get(f"{API}/cards")).json()
    assert Decimal(cards[0]["credit_limit"]) == Decimal("429546")
    assert Decimal(cards[0]["monthly_rate_pct"]) == Decimal("3.85")
    loans = (await client.get(f"{API}/loans")).json()
    assert loans[0]["lender"] == "State Bank of India"
    accts = (await client.get(f"{API}/accounts")).json()
    regalia = next(a for a in accts if a["name"] == "HDFC Regalia")
    assert regalia["has_account_number"] and regalia["last4"] == "1234"
    shown = (await client.get(f"{API}/accounts/{regalia['id']}/number")).json()
    assert shown["account_number"] == "0001017510002959317"

    # Undo puts the old details back.
    from coffernest.db import sessionmaker
    from coffernest.deps import Ctx
    from coffernest.enums import Role
    from coffernest.services import actions

    async with sessionmaker()() as session:
        hid = uuid.UUID((await client.get(f"{API}/household")).json()["id"])
        me = (await client.get(f"{API}/household/members")).json()[0]
        ctx = Ctx(uuid.UUID(me["user_id"]), uuid.UUID(me["id"]), hid, Role.owner)
        out = await actions.update_details(session, ctx, name="HDFC Regalia", credit_limit=1)
        await actions.undo(session, ctx, out["undo"])
    cards = (await client.get(f"{API}/cards")).json()
    assert Decimal(cards[0]["credit_limit"]) == Decimal("429546")
    await anon.aclose()


async def test_writes_wake_the_households_devices(client: httpx.AsyncClient) -> None:
    from coffernest.services import live

    hid = uuid.UUID((await client.get(f"{API}/household")).json()["id"])
    queue = live.subscribe(hid)
    try:
        acc = await bank(client)
        assert queue.get_nowait() == "data"  # the web API write was announced
        await client.get(f"{API}/accounts")
        assert queue.empty()  # reads are not
        await client.patch(f"{API}/accounts/{acc['id']}", json={"name": "Renamed"})
        assert queue.get_nowait() == "data"
    finally:
        live.unsubscribe(hid, queue)
    assert live.listeners(hid) == 0
