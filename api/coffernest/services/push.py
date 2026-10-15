"""Web Push (VAPID) delivery, adapted from VClaw's push/webpush.py.

Keys come from ``VAPID_PUBLIC_KEY`` / ``VAPID_PRIVATE_KEY`` (generate with ``coffernest vapid``).
Delivery is best-effort: a dead subscription (404/410) is removed, other failures are logged.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from coffernest.config import Settings
from coffernest.models import Member, PushSubscription

log = logging.getLogger("coffernest.push")


def generate_vapid() -> tuple[str, str]:
    """(public applicationServerKey base64url, private key PEM)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    priv = ec.generate_private_key(ec.SECP256R1())
    pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    point = priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return base64.urlsafe_b64encode(point).rstrip(b"=").decode(), pem


def _send_one(settings: Settings, sub: PushSubscription, payload: str) -> int:
    """HTTP status from the push service (0 = local failure)."""
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        return 0
    try:
        webpush(
            subscription_info={
                "endpoint": sub.endpoint,
                "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
            },
            data=payload,
            vapid_private_key=settings.vapid_private_key.replace("\\n", "\n"),
            vapid_claims={"sub": settings.vapid_subject},
            ttl=86_400,
        )
        return 201
    except WebPushException as exc:
        code = getattr(exc.response, "status_code", 0) if exc.response is not None else 0
        log.warning("webpush failed (%s): %s", code, exc)
        return int(code or 0)
    except Exception:
        log.exception("webpush error")
        return 0


async def send_to_household(
    session: AsyncSession,
    settings: Settings,
    household_id: uuid.UUID,
    title: str,
    body: str,
    url: str = "/",
    tag: str | None = None,
) -> int:
    if not settings.vapid_private_key:
        return 0
    subs = (
        await session.scalars(
            select(PushSubscription)
            .join(Member, Member.user_id == PushSubscription.user_id)
            .where(Member.household_id == household_id)
        )
    ).all()
    payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag or url})
    delivered = 0
    for sub in subs:
        code = await asyncio.to_thread(_send_one, settings, sub, payload)
        if code in (404, 410):
            await session.execute(delete(PushSubscription).where(PushSubscription.id == sub.id))
        elif 200 <= code < 300:
            delivered += 1
    return delivered
