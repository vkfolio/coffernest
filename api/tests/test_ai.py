import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coffernest.ai import providers
from coffernest.ai.providers import OpenAICompat, Run, _call_tool
from coffernest.ai.service import check_amounts
from coffernest.config import Settings, get_settings
from coffernest.demo import build

API = "/api/v1"


class ScriptedLLM:
    """Calls real tools, then answers with whatever the test scripts (incl. a made-up number)."""

    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], household_id: uuid.UUID, invent: bool
    ) -> None:
        self.sessions = sessions
        self.household_id = household_id
        self.invent = invent

    async def run(self, system: str, prompt: str) -> Run:
        overview = await _call_tool(self.sessions, self.household_id, "get_overview", {})
        data = json.loads(overview)
        emi = data["debt"]["monthly_emi"]
        items = [
            {
                "title": "EMIs are a big share of pay",
                "body": f"You pay ₹{emi:,.0f} a month in EMIs.",
                "severity": "medium",
                "action": "Clear the personal loan first.",
            },
        ]
        if self.invent:
            items.append(
                {
                    "title": "Invented",
                    "body": "You could save ₹9,87,654 easily.",
                    "severity": "info",
                    "action": "",
                }
            )
        return Run(text=json.dumps(items), tool_calls=["get_overview"], tool_outputs=[overview])

    async def stream(
        self, system: str, prompt: str, extra_tools: Any = (), images: Any = ()
    ) -> AsyncIterator[providers.Event]:
        run = await self.run(system, prompt)
        yield providers.Event("done", run=run)

    async def extract(self, system: str, text: str, images: Any, schema: Any) -> dict[str, Any]:
        return {}


@pytest.fixture
async def ai_client(
    anon: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setattr(get_settings(), "ai_provider", "claude_agent")
    providers.set_provider_override(lambda s, sessions, hid: ScriptedLLM(sessions, hid, True))
    await build(anon, "ai@example.com", "demo-password-123")
    yield anon
    providers.set_provider_override(None)


def test_check_amounts() -> None:
    outputs = [json.dumps({"emi": 83142.56, "outstanding": 5240924.0, "free": 114979.91})]
    assert check_amounts("EMIs are ₹83,143 and you owe ₹52.4 L; ₹1,14,980 is free.", outputs) == []
    assert check_amounts("Save ₹5,00,000 now", outputs) == ["₹5,00,000"]
    assert check_amounts("A ₹50 coffee", outputs) == []  # tiny amounts are ignored


async def test_suggestions_need_household_opt_in(ai_client: httpx.AsyncClient) -> None:
    res = await ai_client.post(f"{API}/ai/suggestions/refresh")
    assert res.status_code == 403
    status_ = (await ai_client.get(f"{API}/ai/status")).json()
    assert status_["available"] is True and status_["enabled_for_household"] is False


async def test_suggestions_are_verified_against_tool_output(ai_client: httpx.AsyncClient) -> None:
    await ai_client.patch(f"{API}/household", json={"ai_enabled": True})
    res = await ai_client.post(f"{API}/ai/suggestions/refresh")
    assert res.status_code == 200, res.text
    rows = {r["title"]: r for r in res.json()}
    assert rows["EMIs are a big share of pay"]["verified"] is True
    assert rows["Invented"]["verified"] is False
    assert rows["Invented"]["evidence"]["unverified"] == ["₹9,87,654"]
    assert rows["Invented"]["evidence"]["tools"] == ["get_overview"]

    again = await ai_client.post(f"{API}/ai/suggestions/refresh")
    assert again.status_code == 429  # cooldown

    listed = (await ai_client.get(f"{API}/ai/suggestions")).json()
    await ai_client.post(f"{API}/ai/suggestions/{listed[0]['id']}/dismiss")
    assert len((await ai_client.get(f"{API}/ai/suggestions")).json()) == len(listed) - 1


async def test_openai_compat_tool_loop(anon: httpx.AsyncClient) -> None:
    session = await build(anon, "oa@example.com", "demo-password-123")
    hid = uuid.UUID(session["household_id"])
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "c1",
                                        "type": "function",
                                        "function": {
                                            "name": "simulate_prepayment",
                                            "arguments": json.dumps(
                                                {"loan_name": "Personal loan", "lump_sum": 100000}
                                            ),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        tool_msg = body["messages"][-1]
        assert tool_msg["role"] == "tool"
        saved = json.loads(tool_msg["content"])["interest_saved"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": f"Prepaying saves ₹{saved:,.0f} in interest.",
                        }
                    }
                ]
            },
        )

    from coffernest import db

    settings = Settings(ai_provider="openai_compat", llm_api_key="test", llm_model="m")
    llm = OpenAICompat(settings, db.sessionmaker(), hid, transport=httpx.MockTransport(handler))
    run = await llm.run("system", "question")
    assert run.tool_calls == ["simulate_prepayment"]
    assert "Prepaying saves ₹" in run.text
    assert check_amounts(run.text, run.tool_outputs) == []
    assert calls[0]["tools"][0]["type"] == "function"
