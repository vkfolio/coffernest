"""Brute-force lockout for login and 2FA, persisted in Postgres (adapted from VClaw's throttle.py).

Each key gets ``free`` failures; after that it is locked for ``base * 2**n`` seconds (capped).
A success clears the key. Keys: ``ip:<addr>`` and ``acct:<email>``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import Request
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from coffernest.config import get_settings
from coffernest.models import LoginThrottle


@dataclass(frozen=True)
class Policy:
    free: int
    base_s: float
    cap_s: float


PER_IP = Policy(free=8, base_s=30, cap_s=3600)
PER_ACCOUNT = Policy(free=5, base_s=60, cap_s=900)


def client_ip(request: Request) -> str:
    """Visitor address: the trusted proxy's header when one is configured, else the socket."""
    header = get_settings().trusted_proxy_header
    if header:
        forwarded = request.headers.get(header, "").split(",")[0].strip()
        if forwarded:
            return forwarded[:64]
    return request.client.host if request.client else "unknown"


def _now() -> datetime:
    return datetime.now(UTC)


async def retry_after(session: AsyncSession, keys: list[str]) -> float:
    rows = (await session.scalars(select(LoginThrottle).where(LoginThrottle.key.in_(keys)))).all()
    now = _now()
    return max([(r.locked_until - now).total_seconds() for r in rows] + [0.0])


async def record_failure(session: AsyncSession, key: str, policy: Policy) -> None:
    row = await session.get(LoginThrottle, key)
    now = _now()
    if row is None or row.last_failure < now - timedelta(days=1):
        if row is None:
            row = LoginThrottle(key=key, fails=0, locked_until=now, last_failure=now)
            session.add(row)
        row.fails = 0
    row.fails += 1
    row.last_failure = now
    if row.fails > policy.free:
        lock = min(policy.cap_s, policy.base_s * 2 ** min(row.fails - policy.free - 1, 30))
        row.locked_until = now + timedelta(seconds=lock)


async def record_success(session: AsyncSession, keys: list[str]) -> None:
    await session.execute(delete(LoginThrottle).where(LoginThrottle.key.in_(keys)))
