"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from coffernest import db
from coffernest.config import get_settings
from coffernest.routes import (
    accounts,
    ai,
    attachments,
    auth,
    backup,
    cards,
    categories,
    categorize,
    connections,
    export,
    household,
    inbox,
    insights,
    live,
    loans,
    meta,
    notifications,
    oauth,
    pages,
    people,
    recurring,
    skills,
    sync,
    transactions,
)
from coffernest.security.headers import SecurityHeaders
from coffernest.storage import get_storage

log = logging.getLogger("coffernest")


def _log_demo(task: asyncio.Task[str]) -> None:
    if task.cancelled():
        return
    if (exc := task.exception()) is not None:
        log.error("could not set up the demo account", exc_info=exc)
    elif task.result() != "off":
        log.info("demo account: %s", task.result())


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    if settings.env == "prod" and not settings.secret_enc_key:
        log.warning(
            "SECRET_ENC_KEY is not set: saved account numbers and PDF passwords are encrypted "
            "with a key derived from JWT_SECRET. Set SECRET_ENC_KEY before storing any."
        )
    if settings.env != "test":
        try:
            await asyncio.to_thread(get_storage().ensure_bucket)
        except Exception:  # S3 down must not stop the API; /health reports it.
            log.exception("could not ensure S3 bucket")
    demo_task: asyncio.Task[str] | None = None
    if settings.env != "test":
        from coffernest.demo import ensure_demo

        demo_task = asyncio.create_task(ensure_demo())  # minutes the first time; don't wait
        demo_task.add_done_callback(_log_demo)
    worker_task: asyncio.Task[None] | None = None
    if settings.run_worker and settings.env != "test":
        from coffernest.worker import run_forever

        worker_task = asyncio.create_task(run_forever(configure_logging=False))
    reader_task: asyncio.Task[None] | None = None
    if settings.env != "test":
        from coffernest.services.inbox import run_consumer

        reader_task = asyncio.create_task(run_consumer())
    async with AsyncExitStack() as stack:
        if settings.mcp_enabled:
            from coffernest.mcp_server import get_server

            await stack.enter_async_context(get_server().session_manager.run())
        yield
    for task in (demo_task, worker_task, reader_task):
        if task is not None:
            task.cancel()
    await db.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Coffernest API",
        version=meta.VERSION,
        lifespan=lifespan,
        docs_url="/api/v1/docs",
        openapi_url="/api/v1/openapi.json",
    )
    from coffernest.live_middleware import LiveUpdates

    app.add_middleware(LiveUpdates)
    app.add_middleware(SecurityHeaders)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    v1 = APIRouter(prefix="/api/v1")
    for module in (
        auth,
        household,
        accounts,
        categories,
        categorize,
        people,
        transactions,
        recurring,
        loans,
        cards,
        attachments,
        inbox,
        insights,
        export,
        backup,
        notifications,
        ai,
        skills,
        connections,
        live,
        meta,
        sync,
    ):
        v1.include_router(module.router)
    app.include_router(v1)
    app.include_router(oauth.router)
    app.include_router(pages.router)

    @app.get("/health", tags=["ops"])
    async def health() -> JSONResponse:
        checks: dict[str, bool] = {}
        try:
            async with db.sessionmaker()() as session:
                await session.execute(text("select 1"))
            checks["db"] = True
        except Exception:
            checks["db"] = False
        checks["s3"] = await asyncio.to_thread(get_storage().ping)
        ok = all(checks.values())
        return JSONResponse({"ok": ok, **checks}, status_code=200 if ok else 503)

    if settings.mcp_enabled:
        from coffernest.mcp_server import asgi_app

        app.mount("/", asgi_app())  # last: serves /mcp (and anything else is its 404)
    return app


app = create_app()
