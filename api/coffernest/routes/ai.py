"""/api/v1/ai — suggestions and questions, grounded in the household's own numbers."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update

from coffernest import db
from coffernest.ai import chat, service
from coffernest.ai.providers import AIUnavailable, Event, Run, agent_busy, get_llm
from coffernest.deps import Reader, Session, SettingsDep, Writer, owned
from coffernest.models import (
    AiSuggestion,
    Attachment,
    ChatMessage,
    ChatThread,
    Extraction,
    Household,
)
from coffernest.services import actions, live, skills

router = APIRouter(prefix="/ai", tags=["ai"])
REFRESH_COOLDOWN = timedelta(minutes=10)


class StatusOut(BaseModel):
    available: bool
    enabled_for_household: bool
    provider: str
    model: str


class SuggestionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    body: str
    action: str
    severity: str
    verified: bool
    evidence: dict[str, object]
    created_at: datetime
    helpful: bool | None


class AskIn(BaseModel):
    question: str = Field(min_length=3, max_length=500)


class AnswerOut(BaseModel):
    text: str
    verified: bool
    unverified_amounts: list[str]
    tools_used: list[str]


class FeedbackIn(BaseModel):
    helpful: bool


async def _require_enabled(
    session: Session, household_id: uuid.UUID, settings: SettingsDep
) -> None:
    if settings.ai_provider == "none":
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "AI isn't set up on the server")
    household = await session.get(Household, household_id)
    if household is None or not household.ai_enabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Turn on AI suggestions in Settings first")


def _model_name(settings: SettingsDep) -> str:
    return settings.claude_model if settings.ai_provider == "claude_agent" else settings.llm_model


@router.get("/status", response_model=StatusOut)
async def ai_status(ctx: Reader, session: Session, settings: SettingsDep) -> StatusOut:
    household = await session.get(Household, ctx.household_id)
    return StatusOut(
        available=settings.ai_provider != "none",
        enabled_for_household=bool(household and household.ai_enabled),
        provider=settings.ai_provider,
        model=_model_name(settings) if settings.ai_provider != "none" else "",
    )


@router.get("/suggestions", response_model=list[SuggestionOut])
async def suggestions(ctx: Reader, session: Session) -> list[AiSuggestion]:
    latest = await session.scalar(
        select(AiSuggestion.batch_id)
        .where(AiSuggestion.household_id == ctx.household_id)
        .order_by(AiSuggestion.created_at.desc())
        .limit(1)
    )
    if latest is None:
        return []
    rows = await session.scalars(
        select(AiSuggestion)
        .where(
            AiSuggestion.household_id == ctx.household_id,
            AiSuggestion.batch_id == latest,
            AiSuggestion.dismissed_at.is_(None),
        )
        .order_by(AiSuggestion.created_at)
    )
    return list(rows.all())


@router.post("/suggestions/refresh", response_model=list[SuggestionOut])
async def refresh(ctx: Writer, session: Session, settings: SettingsDep) -> list[AiSuggestion]:
    await _require_enabled(session, ctx.household_id, settings)
    last = await session.scalar(
        select(func.max(AiSuggestion.created_at)).where(
            AiSuggestion.household_id == ctx.household_id
        )
    )
    if last is not None and datetime.now(UTC) - last < REFRESH_COOLDOWN:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "Suggestions were just refreshed. Try again soon."
        )
    try:
        llm = get_llm(settings, db.sessionmaker(), ctx.household_id)
        items, run = await service.suggest(llm)
    except AIUnavailable as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    batch = str(uuid.uuid4())
    rows = [
        AiSuggestion(
            household_id=ctx.household_id,
            batch_id=batch,
            title=s.title,
            body=s.body,
            action=s.action,
            severity=s.severity,
            verified=s.verified,
            evidence={"tools": sorted(set(run.tool_calls)), "unverified": s.unverified_amounts},
            provider=f"{settings.ai_provider}:{_model_name(settings)}",
        )
        for s in items
    ]
    session.add_all(rows)
    await session.commit()
    return rows


@router.post("/suggestions/{suggestion_id}/dismiss", status_code=204)
async def dismiss(suggestion_id: uuid.UUID, ctx: Writer, session: Session) -> None:
    row = await owned(session, AiSuggestion, suggestion_id, ctx)
    row.dismissed_at = datetime.now(UTC)
    await session.commit()


@router.post("/suggestions/{suggestion_id}/feedback", status_code=204)
async def feedback(
    suggestion_id: uuid.UUID, body: FeedbackIn, ctx: Reader, session: Session
) -> None:
    await session.execute(
        update(AiSuggestion)
        .where(AiSuggestion.id == suggestion_id, AiSuggestion.household_id == ctx.household_id)
        .values(helpful=body.helpful)
    )
    await session.commit()


@router.post("/ask", response_model=AnswerOut)
async def ask(body: AskIn, ctx: Reader, session: Session, settings: SettingsDep) -> AnswerOut:
    await _require_enabled(session, ctx.household_id, settings)
    try:
        llm = get_llm(settings, db.sessionmaker(), ctx.household_id)
        answer = await service.ask(llm, body.question)
    except AIUnavailable as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return AnswerOut(**answer.__dict__)


# ---------------------------------------------------------------- assistant threads


class ThreadOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime | None
    preview: str = ""


class ThreadPatch(BaseModel):
    title: str = Field(min_length=1, max_length=160)


class UndoIn(BaseModel):
    message_id: uuid.UUID
    block_id: str = Field(min_length=1, max_length=40)


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: str
    content: str
    blocks: list[dict[str, Any]]
    tools_used: list[str]
    skills_used: list[str]
    verified: bool
    unverified_amounts: list[str]
    created_at: datetime


class ThreadDetail(ThreadOut):
    messages: list[MessageOut]


class MessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    # Inbox file ids (from POST /inbox/upload) to read and talk about in this turn.
    attachments: list[uuid.UUID] = Field(default_factory=list, max_length=5)


@router.get("/threads", response_model=list[ThreadOut])
async def list_threads(
    ctx: Reader, session: Session, q: str | None = None, limit: int = 100
) -> list[ThreadOut]:
    """Newest first. ``q`` searches titles and what was said."""
    query = select(ChatThread).where(ChatThread.household_id == ctx.household_id)
    if q and q.strip():
        like = f"%{q.strip()[:80]}%"
        said = select(ChatMessage.thread_id).where(ChatMessage.content.ilike(like))
        query = query.where(ChatThread.title.ilike(like) | ChatThread.id.in_(said))
    rows = list(
        (
            await session.scalars(
                query.order_by(
                    func.coalesce(ChatThread.updated_at, ChatThread.created_at).desc()
                ).limit(max(1, min(limit, 300)))
            )
        ).all()
    )
    previews: dict[uuid.UUID, str] = {}
    if rows:
        last = (
            select(ChatMessage.thread_id, func.max(ChatMessage.created_at).label("at"))
            .where(ChatMessage.thread_id.in_([t.id for t in rows]), ChatMessage.role == "assistant")
            .group_by(ChatMessage.thread_id)
            .subquery()
        )
        for tid, content in (
            await session.execute(
                select(ChatMessage.thread_id, ChatMessage.content).join(
                    last,
                    (last.c.thread_id == ChatMessage.thread_id)
                    & (last.c.at == ChatMessage.created_at),
                )
            )
        ).all():
            previews[tid] = " ".join(content.replace("*", "").replace("#", "").split())[:140]
    return [
        ThreadOut(
            id=t.id,
            title=t.title,
            created_at=t.created_at,
            updated_at=t.updated_at,
            preview=previews.get(t.id, ""),
        )
        for t in rows
    ]


@router.patch("/threads/{thread_id}", response_model=ThreadOut)
async def rename_thread(
    thread_id: uuid.UUID, body: ThreadPatch, ctx: Reader, session: Session
) -> ThreadOut:
    thread = await owned(session, ChatThread, thread_id, ctx)
    thread.title = body.title.strip()
    await session.commit()
    return ThreadOut.model_validate(thread)


@router.post("/actions/undo", response_model=MessageOut)
async def undo_action(body: UndoIn, ctx: Writer, session: Session) -> ChatMessage:
    """Take back a change the assistant made (the Undo button on its action card)."""
    msg = await session.get(ChatMessage, body.message_id)
    thread = await session.get(ChatThread, msg.thread_id) if msg else None
    if msg is None or thread is None or thread.household_id != ctx.household_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Message not found")
    blocks = [dict(b) for b in msg.blocks]
    block = next((b for b in blocks if b.get("id") == body.block_id), None)
    if block is None or block.get("type") != "action" or not block.get("undo"):
        raise HTTPException(status.HTTP_409_CONFLICT, "Nothing to undo here")
    if block.get("undone"):
        return msg
    try:
        await actions.undo(session, ctx, block["undo"])
    except actions.ActionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    block["undone"] = True
    msg.blocks = blocks
    await session.commit()
    return msg


@router.post("/threads", response_model=ThreadOut, status_code=201)
async def new_thread(ctx: Reader, session: Session) -> ChatThread:
    thread = ChatThread(household_id=ctx.household_id, member_id=ctx.member_id)
    session.add(thread)
    await session.commit()
    return thread


async def _messages(session: Session, thread_id: uuid.UUID) -> list[ChatMessage]:
    rows = await session.scalars(
        select(ChatMessage)
        .where(ChatMessage.thread_id == thread_id)
        .order_by(ChatMessage.created_at, ChatMessage.id)
    )
    return list(rows.all())


@router.get("/threads/{thread_id}", response_model=ThreadDetail)
async def get_thread(thread_id: uuid.UUID, ctx: Reader, session: Session) -> ThreadDetail:
    thread = await owned(session, ChatThread, thread_id, ctx)
    msgs = await _messages(session, thread.id)
    return ThreadDetail(
        **ThreadOut.model_validate(thread).model_dump(),
        messages=[MessageOut.model_validate(m) for m in msgs],
    )


@router.delete("/threads/{thread_id}", status_code=204)
async def delete_thread(thread_id: uuid.UUID, ctx: Reader, session: Session) -> None:
    thread = await owned(session, ChatThread, thread_id, ctx)
    await session.delete(thread)
    await session.commit()


def _sse(event: str, data: object) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str, ensure_ascii=False)}\n\n"


@router.post("/threads/{thread_id}/messages")
async def send_message(
    thread_id: uuid.UUID, body: MessageIn, ctx: Reader, session: Session, settings: SettingsDep
) -> StreamingResponse:
    """Server-sent events: meta, status{tool}, token{text}, block{...}, done{message}, error."""
    await _require_enabled(session, ctx.household_id, settings)
    thread = await owned(session, ChatThread, thread_id, ctx)
    history = await _messages(session, thread.id)
    question = body.content.strip()
    files: list[tuple[Extraction, Attachment]] = []
    for ext_id in dict.fromkeys(body.attachments):
        ext = await session.get(Extraction, ext_id)
        att = await session.get(Attachment, ext.attachment_id) if ext else None
        if ext is None or att is None or ext.household_id != ctx.household_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Attached file not found")
        files.append((ext, att))
    file_blocks: list[dict[str, Any]] = [
        {
            "type": "file",
            "file_id": str(e.id),
            "attachment_id": str(a.id),
            "filename": a.filename,
            "mime": a.mime,
            "size": a.size,
        }
        for e, a in files
    ]
    session.add(ChatMessage(thread_id=thread.id, role="user", content=question, blocks=file_blocks))
    if not history:
        thread.title = question[:80] + ("…" if len(question) > 80 else "")
    thread.updated_at = datetime.now(UTC)
    await session.commit()
    system = await chat.system_prompt(session, ctx.household_id)
    ext_ids = [e.id for e, _ in files]
    prior_evidence = [e for m in history for e in m.evidence][-40:]
    hid, tid = ctx.household_id, thread.id
    try:
        llm = get_llm(settings, db.sessionmaker(), hid)
    except AIUnavailable as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    async def finish(state: chat.TurnState, run: Run, attached: str) -> dict[str, Any]:
        # Figures from tools, roadmaps, the question, attached files and earlier turns all count.
        known = [
            *run.tool_outputs,
            *chat.block_evidence(state.blocks),
            question,
            attached,
            *prior_evidence,
        ]
        bad = service.check_amounts(run.text, known)
        async with db.sessionmaker()() as s2:
            msg = ChatMessage(
                thread_id=tid,
                role="assistant",
                content=run.text,
                blocks=state.blocks,
                tools_used=sorted(set(run.tool_calls)),
                skills_used=state.skills_used,
                verified=not bad,
                unverified_amounts=bad[:20],
                evidence=[o[:20_000] for o in run.tool_outputs][-12:],
            )
            s2.add(msg)
            t2 = await s2.get(ChatThread, tid)
            if t2 is not None:
                t2.updated_at = datetime.now(UTC)
            await s2.commit()
            await skills.mark_used(s2, hid, state.skills_used)
            live.publish(hid, "chat")
            return MessageOut.model_validate(msg).model_dump(mode="json")

    async def events() -> AsyncIterator[str]:
        state = chat.TurnState(hid, ctx=ctx if ctx.can_write else None)
        queue: asyncio.Queue[Event | None] = asyncio.Queue()
        images: list[bytes] = []
        attached = ""

        async def produce() -> None:
            try:
                prompt = chat.history_prompt(history, question, date.today(), attached)
                async for ev in llm.stream(system, prompt, chat.chat_tools(state), images):
                    await queue.put(ev)
            except AIUnavailable as exc:
                await queue.put(Event("error", text=str(exc)))
            except Exception as exc:  # never leave the client hanging
                await queue.put(Event("error", text=f"The assistant failed ({type(exc).__name__})"))
            finally:
                await queue.put(None)

        yield _sse("meta", {"thread_id": str(tid), "busy": agent_busy()})
        if ext_ids:
            # Attached files go through the inbox reader first (so their entries land in the
            # books as usual); wait for it, with pings, then hand the assistant what was read.
            yield _sse("status", {"tool": "reading_files"})
            deadline = asyncio.get_running_loop().time() + 240
            while True:
                async with db.sessionmaker()() as s3:
                    states = [(await s3.get(Extraction, i)) for i in ext_ids]
                    busy = [
                        x for x in states if x is not None and x.status in ("queued", "reading")
                    ]
                if not busy or asyncio.get_running_loop().time() > deadline:
                    break
                yield ": ping\n\n"
                await asyncio.sleep(2)
            async with db.sessionmaker()() as s3:
                attached, images = await chat.files_context(s3, hid, ext_ids)
        task = asyncio.create_task(produce())
        sent = 0
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": ping\n\n"  # keeps proxies and nginx from closing the stream
                    continue
                if ev is None:
                    break
                while sent < len(state.blocks):
                    yield _sse("block", state.blocks[sent])
                    sent += 1
                if ev.type == "token":
                    yield _sse("token", {"text": ev.text})
                elif ev.type == "status":
                    yield _sse("status", {"tool": ev.tool})
                elif ev.type == "error":
                    yield _sse("error", {"detail": ev.text})
                elif ev.type == "done" and ev.run is not None:
                    yield _sse("done", await finish(state, ev.run, attached))
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )
