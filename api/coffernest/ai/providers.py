"""LLM providers behind one interface.

- ``claude_agent``: the Claude Agent SDK (bundled Claude Code CLI). Our tools are an in-process
  MCP server; every built-in tool (files, shell, web) is switched off. Auth comes from
  ``CLAUDE_CODE_OAUTH_TOKEN`` (``claude setup-token``) or ``ANTHROPIC_API_KEY`` in the env.
- ``openai_compat``: any OpenAI-compatible chat-completions endpoint (OpenAI, Kimi, Ollama…)
  with a plain tool-calling loop.

Three jobs:
- ``run``: one prompt, tools allowed, final text (suggestions, quick questions).
- ``stream``: a chat turn with tools, yielding text as it is written (the assistant).
- ``extract``: read a document (text and/or page images) into a JSON-schema shape, no tools.

Every tool result is recorded, so callers can check the model's numbers against them.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import tempfile
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coffernest.ai.tools import BY_NAME, TOOLS, Tool, to_json
from coffernest.config import Settings

log = logging.getLogger("coffernest.ai")
MAX_TURNS = 10
CHAT_TURNS = 16
# One agent session at a time: each spawns a CLI process (~200-300 MB each).
_AGENT_LOCK = asyncio.Lock()


def agent_busy() -> bool:
    return _AGENT_LOCK.locked()


class AIUnavailable(RuntimeError):
    """The configured provider is off, missing credentials, or failed."""


@dataclass
class Run:
    text: str
    tool_calls: list[str] = field(default_factory=list)
    tool_outputs: list[str] = field(default_factory=list)


@dataclass
class Event:
    """One step of a streamed chat turn."""

    type: str  # token | status | done
    text: str = ""
    tool: str = ""
    run: Run | None = None


class LLM(Protocol):
    async def run(self, system: str, prompt: str) -> Run: ...

    def stream(
        self,
        system: str,
        prompt: str,
        extra_tools: Sequence[Tool] = (),
        images: Sequence[bytes] = (),
    ) -> AsyncIterator[Event]: ...

    async def extract(
        self, system: str, text: str, images: Sequence[bytes], schema: dict[str, Any]
    ) -> dict[str, Any]: ...


async def _call_tool(
    sessions: async_sessionmaker[AsyncSession],
    household_id: uuid.UUID,
    name: str,
    args: dict[str, Any],
    extra: dict[str, Tool] | None = None,
) -> str:
    tool = (extra or {}).get(name) or BY_NAME.get(name)
    if tool is None:
        return json.dumps({"error": f"unknown tool {name}"})
    try:
        async with sessions() as session:
            return to_json(await tool.handler(session, household_id, args))
    except Exception as exc:  # a tool failing must not kill the conversation
        log.exception("tool %s failed", name)
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"})


def _image_blocks(images: Sequence[bytes]) -> list[dict[str, Any]]:
    return [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64.b64encode(img).decode(),
            },
        }
        for img in images
    ]


def _user_turn(text: str, images: Sequence[bytes]) -> dict[str, Any]:
    return {
        "type": "user",
        "parent_tool_use_id": None,
        "session_id": "",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": text}, *_image_blocks(images)],
        },
    }


class ClaudeAgent:
    def __init__(
        self,
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession],
        household_id: uuid.UUID,
    ) -> None:
        self.settings = settings
        self.sessions = sessions
        self.household_id = household_id

    @staticmethod
    def _sdk() -> Any:
        try:
            import claude_agent_sdk
        except ImportError as exc:
            raise AIUnavailable("claude-agent-sdk is not installed (pip extra 'ai')") from exc
        return claude_agent_sdk

    def _options(
        self, sdk: Any, system: str, workdir: str, tools: Sequence[Tool], out: Run, **extra: Any
    ) -> Any:
        by_name = {t.name: t for t in tools}

        def wrap(t: Tool) -> Any:
            @sdk.tool(t.name, t.description, t.schema)  # type: ignore[untyped-decorator]
            async def handler(args: dict[str, Any]) -> dict[str, Any]:
                result = await _call_tool(
                    self.sessions, self.household_id, t.name, args or {}, by_name
                )
                out.tool_outputs.append(result)
                return {"content": [{"type": "text", "text": result}]}

            return handler

        servers: dict[str, Any] = {}
        allowed: list[str] = []
        if tools:
            servers["money"] = sdk.create_sdk_mcp_server(
                name="coffernest", version="2.0.0", tools=[wrap(t) for t in tools]
            )
            allowed = [f"mcp__money__{t.name}" for t in tools]
        return sdk.ClaudeAgentOptions(
            model=self.settings.claude_model,
            system_prompt=system,
            tools=[],  # no built-in tools at all: no files, shell or web
            mcp_servers=servers,
            allowed_tools=allowed,
            permission_mode="dontAsk",  # anything not allowed is refused, never prompted
            setting_sources=[],  # ignore any CLAUDE.md / settings on the host
            cwd=workdir,
            env={"CLAUDE_AGENT_SDK_CLIENT_APP": "coffernest/0.2"},
            **extra,
        )

    async def run(self, system: str, prompt: str) -> Run:
        out = Run(text="")
        async for event in self.stream(system, prompt, (), (), _turns=MAX_TURNS, _partial=False):
            if event.type == "done" and event.run is not None:
                out = event.run
        return out

    async def stream(
        self,
        system: str,
        prompt: str,
        extra_tools: Sequence[Tool] = (),
        images: Sequence[bytes] = (),
        *,
        _turns: int = CHAT_TURNS,
        _partial: bool = True,
    ) -> AsyncIterator[Event]:
        sdk = self._sdk()
        out = Run(text="")
        payload = _user_turn(prompt, images) if images else None

        async def as_messages() -> AsyncIterator[dict[str, Any]]:
            assert payload is not None
            yield payload  # built up front: the SDK hangs if this generator raises

        query_prompt: Any = as_messages() if payload is not None else prompt
        tools = list({t.name: t for t in [*TOOLS, *extra_tools]}.values())
        texts: list[str] = []
        with tempfile.TemporaryDirectory(prefix="cn-ai-") as workdir:
            options = self._options(
                sdk,
                system,
                workdir,
                tools,
                out,
                max_turns=_turns,
                include_partial_messages=_partial,
            )
            async with _AGENT_LOCK:
                try:
                    async for message in sdk.query(prompt=query_prompt, options=options):
                        if isinstance(message, sdk.StreamEvent):
                            ev = message.event
                            if ev.get("type") == "content_block_delta":
                                delta = ev.get("delta") or {}
                                if delta.get("type") == "text_delta" and delta.get("text"):
                                    yield Event("token", text=delta["text"])
                            elif ev.get("type") == "content_block_start":
                                block = ev.get("content_block") or {}
                                if block.get("type") == "tool_use":
                                    name = str(block.get("name", "")).removeprefix("mcp__money__")
                                    yield Event("status", tool=name)
                        elif isinstance(message, sdk.AssistantMessage):
                            for block in message.content:
                                if isinstance(block, sdk.TextBlock):
                                    texts.append(block.text)
                                elif isinstance(block, sdk.ToolUseBlock):
                                    out.tool_calls.append(block.name.removeprefix("mcp__money__"))
                        elif isinstance(message, sdk.ResultMessage):
                            if message.is_error:
                                raise AIUnavailable(f"Claude run failed: {message.subtype}")
                            if not _partial and message.result:
                                texts = [message.result]
                except AIUnavailable:
                    raise
                except Exception as exc:
                    raise AIUnavailable(f"Claude agent error: {exc}") from exc
        out.text = "\n\n".join(t.strip() for t in texts if t.strip())
        yield Event("done", run=out)

    async def extract(
        self, system: str, text: str, images: Sequence[bytes], schema: dict[str, Any]
    ) -> dict[str, Any]:
        sdk = self._sdk()
        payload = _user_turn(text, images)

        async def messages() -> AsyncIterator[dict[str, Any]]:
            yield payload  # built up front: the SDK hangs if this generator raises

        result: Any = None
        with tempfile.TemporaryDirectory(prefix="cn-read-") as workdir:
            options = self._options(
                sdk,
                system,
                workdir,
                (),
                Run(text=""),
                max_turns=4,
                output_format={"type": "json_schema", "schema": schema},
            )
            async with _AGENT_LOCK:
                try:
                    async for message in sdk.query(prompt=messages(), options=options):
                        if isinstance(message, sdk.ResultMessage):
                            if message.is_error:
                                raise AIUnavailable(f"Reading failed: {message.subtype}")
                            result = message.structured_output
                except AIUnavailable:
                    raise
                except Exception as exc:
                    raise AIUnavailable(f"Claude agent error: {exc}") from exc
        if not isinstance(result, dict):
            raise AIUnavailable("The reader didn't return the expected shape")
        return result


class OpenAICompat:
    def __init__(
        self,
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession],
        household_id: uuid.UUID,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not settings.llm_api_key and "localhost" not in settings.llm_base_url:
            raise AIUnavailable("LLM_API_KEY is not set")
        self.settings = settings
        self.sessions = sessions
        self.household_id = household_id
        self.transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.settings.llm_base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {self.settings.llm_api_key}"},
            timeout=180,
            transport=self.transport,
        )

    async def run(self, system: str, prompt: str) -> Run:
        out = Run(text="")
        async for event in self.stream(system, prompt):
            if event.type == "done" and event.run is not None:
                out = event.run
        return out

    async def stream(
        self,
        system: str,
        prompt: str,
        extra_tools: Sequence[Tool] = (),
        images: Sequence[bytes] = (),
    ) -> AsyncIterator[Event]:
        """Tool loop; the final answer is sent in chunks once known (tool calls can't stream)."""
        all_tools = list({t.name: t for t in [*TOOLS, *extra_tools]}.values())
        extra = {t.name: t for t in extra_tools}
        tools = [
            {
                "type": "function",
                "function": {"name": t.name, "description": t.description, "parameters": t.schema},
            }
            for t in all_tools
        ]
        user: Any = prompt
        if images:
            user = [
                {"type": "text", "text": prompt},
                *(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/jpeg;base64," + base64.b64encode(i).decode()
                        },
                    }
                    for i in images
                ),
            ]
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        out = Run(text="")
        async with self._client() as client:
            for _ in range(CHAT_TURNS):
                res = await client.post(
                    "/chat/completions",
                    json={"model": self.settings.llm_model, "messages": messages, "tools": tools},
                )
                if res.status_code >= 400:
                    raise AIUnavailable(f"LLM error {res.status_code}: {res.text[:200]}")
                choice = res.json()["choices"][0]["message"]
                messages.append(choice)
                calls = choice.get("tool_calls") or []
                if not calls:
                    out.text = (choice.get("content") or "").strip()
                    for i in range(0, len(out.text), 24):
                        yield Event("token", text=out.text[i : i + 24])
                    yield Event("done", run=out)
                    return
                for call in calls:
                    name = call["function"]["name"]
                    yield Event("status", tool=name)
                    try:
                        args = json.loads(call["function"].get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    result = await _call_tool(self.sessions, self.household_id, name, args, extra)
                    out.tool_calls.append(name)
                    out.tool_outputs.append(result)
                    messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
        raise AIUnavailable("The model kept calling tools without answering")

    async def extract(
        self, system: str, text: str, images: Sequence[bytes], schema: dict[str, Any]
    ) -> dict[str, Any]:
        parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for img in images:
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/jpeg;base64," + base64.b64encode(img).decode()
                    },
                }
            )
        async with self._client() as client:
            res = await client.post(
                "/chat/completions",
                json={
                    "model": self.settings.llm_model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": parts},
                    ],
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {"name": "extraction", "schema": schema},
                    },
                },
            )
        if res.status_code >= 400:
            raise AIUnavailable(f"LLM error {res.status_code}: {res.text[:200]}")
        content = res.json()["choices"][0]["message"].get("content") or ""
        try:
            data = json.loads(content[content.find("{") : content.rfind("}") + 1])
        except json.JSONDecodeError as exc:
            raise AIUnavailable("The reader's answer wasn't valid JSON") from exc
        if not isinstance(data, dict):
            raise AIUnavailable("The reader didn't return the expected shape")
        return data


Factory = Callable[[Settings, async_sessionmaker[AsyncSession], uuid.UUID], LLM]
_override: Factory | None = None  # tests inject a fake


def set_provider_override(factory: Factory | None) -> None:
    global _override
    _override = factory


def get_llm(
    settings: Settings, sessions: async_sessionmaker[AsyncSession], household_id: uuid.UUID
) -> LLM:
    if _override is not None:
        return _override(settings, sessions, household_id)
    if settings.ai_provider == "claude_agent":
        return ClaudeAgent(settings, sessions, household_id)
    if settings.ai_provider == "openai_compat":
        return OpenAICompat(settings, sessions, household_id)
    raise AIUnavailable("AI is switched off on the server (AI_PROVIDER=none)")
