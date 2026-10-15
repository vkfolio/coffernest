"""ASGI middleware: after any successful write through the web API, wake the household's devices.

Pure ASGI (not BaseHTTPMiddleware) so streaming responses pass through untouched.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from coffernest.config import get_settings
from coffernest.security.tokens import decode_access
from coffernest.services import live

_WRITES = {"POST", "PUT", "PATCH", "DELETE"}
# Chat and sync publish themselves (sync only when it changed something).
_SKIP = ("/api/v1/auth/", "/api/v1/events", "/api/v1/ai/threads/", "/api/v1/sync")


class LiveUpdates:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if (
            scope["type"] != "http"
            or scope.get("method") not in _WRITES
            or not path.startswith("/api/v1/")
            or path.startswith(_SKIP)
        ):
            await self.app(scope, receive, send)
            return
        auth = dict(scope.get("headers") or []).get(b"authorization", b"").decode()
        scheme, _, token = auth.partition(" ")
        claims = decode_access(get_settings(), token) if scheme.lower() == "bearer" else None

        async def watch(message: Message) -> None:
            await send(message)
            if (
                claims is not None
                and message["type"] == "http.response.start"
                and message.get("status", 500) < 400
            ):
                live.publish(claims.household_id)

        await self.app(scope, receive, watch)
