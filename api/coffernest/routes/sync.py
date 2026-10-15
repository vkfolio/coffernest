"""/api/v1/sync — a device with its own copy of the books swaps changes with the server.

See ``services/sync.py`` for how rows, ids, conflicts and the cursor work.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from coffernest.deps import Ctx, Session, current_ctx
from coffernest.services import live
from coffernest.services.sync import SyncError, sync

router = APIRouter(tags=["sync"])


class DeleteIn(BaseModel):
    table: str = Field(max_length=40)
    id: str = Field(max_length=64)
    deleted_at: str = Field(max_length=40)


class SyncIn(BaseModel):
    # Which device is syncing, so its own changes aren't sent back to it.
    client_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    # From the last answer; leave it out for everything.
    cursor: str | None = Field(default=None, pattern=r"^\d{1,20}$")
    changes: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    deletes: list[DeleteIn] = Field(default_factory=list)
    household: dict[str, Any] | None = None
    # False while sending a big first sync in parts; true (the default) on the last request.
    pull: bool = True


@router.post("/sync")
async def sync_books(
    body: SyncIn, ctx: Annotated[Ctx, Depends(current_ctx)], session: Session
) -> JSONResponse:
    try:
        result = await sync(
            session,
            household_id=ctx.household_id,
            member_id=ctx.member_id,
            user_id=ctx.user_id,
            role=ctx.role,
            client=body.client_id,
            cursor=body.cursor,
            changes=body.changes,
            deletes=[d.model_dump() for d in body.deletes],
            household=body.household,
            pull=body.pull,
        )
    except SyncError as exc:
        raise HTTPException(exc.code, str(exc)) from exc
    if result.applied:
        live.publish(ctx.household_id)
    return JSONResponse(result.body())
