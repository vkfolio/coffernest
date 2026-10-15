"""/api/v1/events — a server-sent-events stream that says "your data changed"."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from coffernest.deps import Reader, Session
from coffernest.services import live

router = APIRouter(tags=["live"])
HEARTBEAT_S = 20


@router.get("/events")
async def events(request: Request, ctx: Reader, session: Session) -> StreamingResponse:
    """One event per change: ``event: changed`` with ``{"topic": "data"|"inbox"|"chat"}``.
    Comment lines every 20 s keep proxies from closing the connection."""
    hid = ctx.household_id
    # The sign-in check borrowed a pooled connection; a stream lives for hours, so give it back
    # now (otherwise every open tab pins one and the pool runs dry).
    await session.close()
    queue = live.subscribe(hid)

    async def stream() -> AsyncIterator[str]:
        try:
            yield "retry: 3000\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    topic = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_S)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                topics = {topic}
                while not queue.empty():  # coalesce a burst (e.g. an inbox file's rows)
                    topics.add(queue.get_nowait())
                yield f"event: changed\ndata: {json.dumps({'topics': sorted(topics)})}\n\n"
        finally:
            live.unsubscribe(hid, queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )
