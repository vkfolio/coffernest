"""/api/v1/notifications — in-app inbox and Web Push subscriptions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert

from coffernest.deps import Reader, Session, SettingsDep
from coffernest.models import Notification, PushSubscription

router = APIRouter(prefix="/notifications", tags=["notifications"])


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    title: str
    body: str
    url: str
    created_at: datetime
    read_at: datetime | None


class SubscriptionKeys(BaseModel):
    p256dh: str = Field(max_length=200)
    auth: str = Field(max_length=100)


class SubscriptionIn(BaseModel):
    endpoint: str = Field(max_length=2000)
    keys: SubscriptionKeys
    user_agent: str = Field(default="", max_length=200)


class UnsubscribeIn(BaseModel):
    endpoint: str = Field(max_length=2000)


class PushConfigOut(BaseModel):
    enabled: bool
    public_key: str


@router.get("", response_model=list[NotificationOut])
async def list_notifications(
    ctx: Reader, session: Session, unread_only: bool = False
) -> list[Notification]:
    query = select(Notification).where(Notification.household_id == ctx.household_id)
    if unread_only:
        query = query.where(Notification.read_at.is_(None))
    rows = await session.scalars(query.order_by(Notification.created_at.desc()).limit(100))
    return list(rows.all())


@router.post("/read-all", status_code=204)
async def read_all(ctx: Reader, session: Session) -> None:
    await session.execute(
        update(Notification)
        .where(Notification.household_id == ctx.household_id, Notification.read_at.is_(None))
        .values(read_at=datetime.now(UTC))
    )
    await session.commit()


@router.get("/push", response_model=PushConfigOut)
async def push_config(_: Reader, settings: SettingsDep) -> PushConfigOut:
    return PushConfigOut(
        enabled=bool(settings.vapid_public_key), public_key=settings.vapid_public_key
    )


@router.post("/push/subscribe", status_code=204)
async def subscribe(body: SubscriptionIn, ctx: Reader, session: Session) -> None:
    stmt = insert(PushSubscription).values(
        id=uuid.uuid4(),
        user_id=ctx.user_id,
        endpoint=body.endpoint,
        p256dh=body.keys.p256dh,
        auth=body.keys.auth,
        user_agent=body.user_agent,
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[PushSubscription.endpoint],
            set_={"user_id": ctx.user_id, "p256dh": body.keys.p256dh, "auth": body.keys.auth},
        )
    )
    await session.commit()


@router.post("/push/unsubscribe", status_code=204)
async def unsubscribe(body: UnsubscribeIn, ctx: Reader, session: Session) -> None:
    await session.execute(
        delete(PushSubscription).where(
            PushSubscription.endpoint == body.endpoint, PushSubscription.user_id == ctx.user_id
        )
    )
    await session.commit()
