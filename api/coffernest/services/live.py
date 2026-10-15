"""Live updates: tell every open device of a household that its data changed.

The API runs as one process, so an in-memory fan-out is enough: each open app holds a
server-sent-events stream; any write (from the app, an MCP client, or the inbox reader) calls
``publish`` and every stream of that household gets a small "changed" event, after which the
clients refetch what they show. No data travels in the event itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections import defaultdict

_subscribers: dict[uuid.UUID, set[asyncio.Queue[str]]] = defaultdict(set)
MAX_QUEUE = 50


def subscribe(household_id: uuid.UUID) -> asyncio.Queue[str]:
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=MAX_QUEUE)
    _subscribers[household_id].add(queue)
    return queue


def unsubscribe(household_id: uuid.UUID, queue: asyncio.Queue[str]) -> None:
    subs = _subscribers.get(household_id)
    if subs is not None:
        subs.discard(queue)
        if not subs:
            _subscribers.pop(household_id, None)


def publish(household_id: uuid.UUID | None, topic: str = "data") -> None:
    """Fire and forget. A slow device just misses a beat; it refetches on the next event."""
    if household_id is None:
        return
    for queue in list(_subscribers.get(household_id, ())):
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(topic)


def listeners(household_id: uuid.UUID) -> int:
    return len(_subscribers.get(household_id, ()))
