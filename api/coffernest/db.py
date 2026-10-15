"""Async engine and session plumbing."""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from coffernest.config import get_settings

_engine: AsyncEngine | None = None
_sessions: async_sessionmaker[AsyncSession] | None = None


def engine() -> AsyncEngine:
    global _engine, _sessions
    if _engine is None:
        # Small pool: a small Postgres is capped at 30 connections shared by api + worker.
        _engine = create_async_engine(
            get_settings().async_database_url, pool_size=5, max_overflow=5, pool_pre_ping=True
        )
        _sessions = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def sessionmaker() -> async_sessionmaker[AsyncSession]:
    engine()
    assert _sessions is not None
    return _sessions


async def dispose() -> None:
    global _engine, _sessions
    if _engine is not None:
        await _engine.dispose()
    _engine = _sessions = None


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request, committed by the route when it writes."""
    async with sessionmaker()() as session:
        yield session
