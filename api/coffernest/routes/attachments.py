"""/api/v1/attachments — bills, statements and receipts stored in MinIO.

Two upload paths:
1. ``POST /attachments`` → presigned PUT URL; the client uploads straight to S3, then calls
   ``/complete``. Best for big statement PDFs (bytes never pass through the API container).
2. ``POST /attachments/upload`` (multipart) for small files and simple clients.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from coffernest.deps import Ctx, Reader, Session, Writer, owned
from coffernest.enums import AttachmentKind, UploadStatus
from coffernest.models import Account, Attachment, Transaction
from coffernest.storage import get_storage

router = APIRouter(prefix="/attachments", tags=["attachments"])

MAX_BYTES = 25 * 1024 * 1024
ALLOWED = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/heic",
    "image/heif",
    "text/csv",
}
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


class AttachmentIn(BaseModel):
    filename: str = Field(min_length=1, max_length=200)
    mime: str
    size: int = Field(gt=0, le=MAX_BYTES)
    kind: AttachmentKind = AttachmentKind.bill
    note: str = Field(default="", max_length=500)
    transaction_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None


class AttachmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    mime: str
    size: int
    kind: AttachmentKind
    status: UploadStatus
    note: str
    transaction_id: uuid.UUID | None
    account_id: uuid.UUID | None
    created_at: datetime


class UploadTicket(BaseModel):
    attachment: AttachmentOut
    upload_url: str
    headers: dict[str, str]


class LinkOut(BaseModel):
    url: str
    expires_in: int


def _key(ctx: Ctx, filename: str) -> str:
    now = datetime.now(UTC)
    safe = _SAFE.sub("-", filename)[-80:] or "file"
    return f"{ctx.household_id}/{now:%Y/%m}/{uuid.uuid4().hex}-{safe}"


def _check_mime(mime: str) -> None:
    if mime not in ALLOWED:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Upload a PDF, image or CSV")


async def _check_links(
    session: Session, ctx: Ctx, txn_id: uuid.UUID | None, account_id: uuid.UUID | None
) -> None:
    if txn_id is not None:
        await owned(session, Transaction, txn_id, ctx)
    if account_id is not None:
        await owned(session, Account, account_id, ctx)


@router.get("", response_model=list[AttachmentOut])
async def list_attachments(
    ctx: Reader,
    session: Session,
    transaction_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    kind: AttachmentKind | None = None,
) -> list[Attachment]:
    query = select(Attachment).where(
        Attachment.household_id == ctx.household_id, Attachment.status == UploadStatus.stored
    )
    if transaction_id:
        query = query.where(Attachment.transaction_id == transaction_id)
    if account_id:
        query = query.where(Attachment.account_id == account_id)
    if kind:
        query = query.where(Attachment.kind == kind)
    rows = await session.scalars(query.order_by(Attachment.created_at.desc()).limit(500))
    return list(rows.all())


@router.post("", response_model=UploadTicket, status_code=201)
async def start_upload(body: AttachmentIn, ctx: Writer, session: Session) -> UploadTicket:
    _check_mime(body.mime)
    await _check_links(session, ctx, body.transaction_id, body.account_id)
    item = Attachment(
        household_id=ctx.household_id,
        object_key=_key(ctx, body.filename),
        uploaded_by=ctx.user_id,
        **body.model_dump(),
    )
    session.add(item)
    await session.commit()
    url = await asyncio.to_thread(get_storage().presign_put, item.object_key, item.mime)
    return UploadTicket(
        attachment=AttachmentOut.model_validate(item),
        upload_url=url,
        headers={"Content-Type": item.mime},
    )


@router.post("/{attachment_id}/complete", response_model=AttachmentOut)
async def complete_upload(attachment_id: uuid.UUID, ctx: Writer, session: Session) -> Attachment:
    item = await owned(session, Attachment, attachment_id, ctx)
    size = await asyncio.to_thread(get_storage().head, item.object_key)
    if size is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "File hasn't arrived in storage yet")
    if size > MAX_BYTES:
        await asyncio.to_thread(get_storage().delete, item.object_key)
        await session.delete(item)
        await session.commit()
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "File is over 25 MB")
    item.size = size
    item.status = UploadStatus.stored
    await session.commit()
    return item


@router.post("/upload", response_model=AttachmentOut, status_code=201)
async def upload(
    ctx: Writer,
    session: Session,
    file: UploadFile,
    kind: AttachmentKind = Form(default=AttachmentKind.bill),
    note: str = Form(default="", max_length=500),
    transaction_id: uuid.UUID | None = Form(default=None),
    account_id: uuid.UUID | None = Form(default=None),
) -> Attachment:
    mime = file.content_type or "application/octet-stream"
    _check_mime(mime)
    await _check_links(session, ctx, transaction_id, account_id)
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "File is over 25 MB")
    filename = file.filename or "upload"
    item = Attachment(
        household_id=ctx.household_id,
        object_key=_key(ctx, filename),
        filename=filename[:200],
        mime=mime,
        size=len(data),
        kind=kind,
        note=note,
        status=UploadStatus.stored,
        transaction_id=transaction_id,
        account_id=account_id,
        uploaded_by=ctx.user_id,
    )
    await asyncio.to_thread(get_storage().put, item.object_key, data, mime)
    session.add(item)
    await session.commit()
    return item


@router.get("/{attachment_id}/url", response_model=LinkOut)
async def download_url(attachment_id: uuid.UUID, ctx: Reader, session: Session) -> LinkOut:
    item = await owned(session, Attachment, attachment_id, ctx)
    url = await asyncio.to_thread(get_storage().presign_get, item.object_key, item.filename)
    return LinkOut(url=url, expires_in=3600)


class AttachmentPatch(BaseModel):
    kind: AttachmentKind | None = None
    note: str | None = Field(default=None, max_length=500)
    transaction_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None


@router.patch("/{attachment_id}", response_model=AttachmentOut)
async def patch_attachment(
    attachment_id: uuid.UUID, body: AttachmentPatch, ctx: Writer, session: Session
) -> Attachment:
    item = await owned(session, Attachment, attachment_id, ctx)
    changes = body.model_dump(exclude_unset=True)
    await _check_links(session, ctx, changes.get("transaction_id"), changes.get("account_id"))
    for key, value in changes.items():
        setattr(item, key, value)
    await session.commit()
    return item


@router.delete("/{attachment_id}", status_code=204)
async def delete_attachment(attachment_id: uuid.UUID, ctx: Writer, session: Session) -> None:
    item = await owned(session, Attachment, attachment_id, ctx)
    await asyncio.to_thread(get_storage().delete, item.object_key)
    await session.delete(item)
    await session.commit()
