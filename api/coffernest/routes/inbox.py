"""/api/v1/inbox — drop any file; it gets read and the books are filled in.

Sure rows are added straight away and stamped; the rest wait here for one tap.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from coffernest.deps import Reader, Session, Writer, owned
from coffernest.enums import AttachmentKind, UploadStatus
from coffernest.ingest.prepare import sniff
from coffernest.models import Attachment, DocPassword, Extraction, ExtractionItem
from coffernest.routes.attachments import _key
from coffernest.security.box import seal
from coffernest.services import inbox as svc
from coffernest.storage import get_storage

router = APIRouter(prefix="/inbox", tags=["inbox"])
MAX_FILES = 20


class ItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    position: int
    kind: str
    payload: dict[str, Any]
    confidence: float
    state: str
    reason: str
    created_ref: dict[str, Any]


class FileOut(BaseModel):
    id: uuid.UUID  # extraction id
    attachment_id: uuid.UUID
    filename: str
    mime: str
    size: int
    status: str
    doc_type: str
    summary: str
    error: str
    created_at: datetime
    counts: dict[str, int]
    line: str


class FileDetail(FileOut):
    items: list[ItemOut]


class PasswordIn(BaseModel):
    password: str = Field(min_length=1, max_length=128)
    remember: bool = True
    label: str = Field(default="", max_length=120)


class ApproveIn(BaseModel):
    payload: dict[str, Any] | None = None  # edited fields from the review screen


class InboxSummary(BaseModel):
    reading: int
    to_check: int
    needs_password: int
    added_this_week: int


def _out(ext: Extraction, att: Attachment, items: list[ExtractionItem]) -> FileOut:
    counts: dict[str, int] = {}
    for item in items:
        counts[item.state] = counts.get(item.state, 0) + 1
    return FileOut(
        id=ext.id,
        attachment_id=att.id,
        filename=att.filename,
        mime=att.mime,
        size=att.size,
        status=ext.status,
        doc_type=ext.doc_type,
        summary=ext.summary,
        error=ext.error,
        created_at=ext.created_at,
        counts=counts,
        line=svc.summary_line(ext, items),
    )


async def _items(session: Session, ext_id: uuid.UUID) -> list[ExtractionItem]:
    rows = await session.scalars(
        select(ExtractionItem)
        .where(ExtractionItem.extraction_id == ext_id)
        .order_by(ExtractionItem.position)
    )
    return list(rows.all())


@router.get("", response_model=list[FileOut])
async def list_files(ctx: Reader, session: Session, limit: int = 60) -> list[FileOut]:
    rows = (
        await session.execute(
            select(Extraction, Attachment)
            .join(Attachment, Attachment.id == Extraction.attachment_id)
            .where(Extraction.household_id == ctx.household_id)
            .order_by(Extraction.created_at.desc())
            .limit(min(limit, 200))
        )
    ).all()
    ids = [ext.id for ext, _ in rows]
    items: dict[uuid.UUID, list[ExtractionItem]] = {i: [] for i in ids}
    if ids:
        for item in (
            await session.scalars(
                select(ExtractionItem).where(ExtractionItem.extraction_id.in_(ids))
            )
        ).all():
            items[item.extraction_id].append(item)
    return [_out(ext, att, items[ext.id]) for ext, att in rows]


@router.get("/summary", response_model=InboxSummary)
async def summary(ctx: Reader, session: Session) -> InboxSummary:
    by_status = dict(
        (
            await session.execute(
                select(Extraction.status, func.count())
                .where(Extraction.household_id == ctx.household_id)
                .group_by(Extraction.status)
            )
        ).all()
    )
    pending = await session.scalar(
        select(func.count())
        .select_from(ExtractionItem)
        .where(ExtractionItem.household_id == ctx.household_id, ExtractionItem.state == "pending")
    )
    week = datetime.now(UTC) - timedelta(days=7)
    added = await session.scalar(
        select(func.count())
        .select_from(ExtractionItem)
        .where(
            ExtractionItem.household_id == ctx.household_id,
            ExtractionItem.state.in_(("auto_added", "approved")),
            ExtractionItem.created_at >= week,
        )
    )
    return InboxSummary(
        reading=int(by_status.get("queued", 0)) + int(by_status.get("reading", 0)),
        to_check=int(pending or 0),
        needs_password=int(by_status.get("needs_password", 0)),
        added_this_week=int(added or 0),
    )


@router.post("/upload", response_model=list[FileOut], status_code=201)
async def upload(ctx: Writer, session: Session, files: list[UploadFile]) -> list[FileOut]:
    if not files:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Add at least one file")
    if len(files) > MAX_FILES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Up to {MAX_FILES} files at a time")
    out: list[FileOut] = []
    for file in files:
        data = await file.read(svc.MAX_BYTES + 1)
        filename = (file.filename or "file")[:200]
        if len(data) > svc.MAX_BYTES:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"{filename} is over 25 MB"
            )
        mime = file.content_type or "application/octet-stream"
        kind = sniff(filename, mime, data)
        if kind == "unknown":
            raise HTTPException(
                status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                f"{filename}: drop a PDF, photo, screenshot, CSV or Excel file",
            )
        digest = hashlib.sha256(data).hexdigest()
        same = await session.scalar(
            select(Extraction)
            .join(Attachment, Attachment.id == Extraction.attachment_id)
            .where(Attachment.household_id == ctx.household_id, Attachment.sha256 == digest)
            .limit(1)
        )
        if same is not None:  # the same file again: show the earlier result, don't re-read
            att = await session.get(Attachment, same.attachment_id)
            assert att is not None
            out.append(_out(same, att, await _items(session, same.id)))
            continue
        att = Attachment(
            household_id=ctx.household_id,
            object_key=_key(ctx, filename),
            filename=filename,
            mime=mime,
            size=len(data),
            kind=AttachmentKind.statement
            if kind in ("pdf", "sheet", "xlsx", "text")
            else AttachmentKind.bill,
            status=UploadStatus.stored,
            uploaded_by=ctx.user_id,
            sha256=digest,
            source="inbox",
        )
        await asyncio.to_thread(get_storage().put, att.object_key, data, mime)
        session.add(att)
        await session.flush()
        ext = Extraction(
            household_id=ctx.household_id, attachment_id=att.id, uploaded_by=ctx.member_id
        )
        session.add(ext)
        await session.commit()
        svc.enqueue(ext.id)
        out.append(_out(ext, att, []))
    return out


async def _load(session: Session, ext_id: uuid.UUID, ctx: Any) -> tuple[Extraction, Attachment]:
    ext = await owned(session, Extraction, ext_id, ctx)
    att = await session.get(Attachment, ext.attachment_id)
    if att is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    return ext, att


@router.get("/{ext_id}", response_model=FileDetail)
async def get_file(ext_id: uuid.UUID, ctx: Reader, session: Session) -> FileDetail:
    ext, att = await _load(session, ext_id, ctx)
    items = await _items(session, ext.id)
    return FileDetail(
        **_out(ext, att, items).model_dump(), items=[ItemOut.model_validate(i) for i in items]
    )


@router.post("/{ext_id}/password", response_model=FileOut)
async def unlock(ext_id: uuid.UUID, body: PasswordIn, ctx: Writer, session: Session) -> FileOut:
    ext, att = await _load(session, ext_id, ctx)
    if ext.status != "needs_password":
        raise HTTPException(status.HTTP_409_CONFLICT, "This file isn't locked")
    if body.remember:
        session.add(
            DocPassword(
                household_id=ctx.household_id,
                label=body.label or att.filename[:120],
                ciphertext=seal(body.password),
            )
        )
    ext.status = "queued"
    await session.commit()
    svc.enqueue(ext.id, body.password)
    return _out(ext, att, [])


@router.post("/{ext_id}/retry", response_model=FileOut)
async def retry(ext_id: uuid.UUID, ctx: Writer, session: Session) -> FileOut:
    ext, att = await _load(session, ext_id, ctx)
    if ext.status not in ("failed", "needs_password"):
        raise HTTPException(status.HTTP_409_CONFLICT, "This file is already read")
    ext.status = "queued"
    await session.commit()
    svc.enqueue(ext.id)
    return _out(ext, att, [])


async def _item(session: Session, ext: Extraction, item_id: uuid.UUID) -> ExtractionItem:
    item = await session.get(ExtractionItem, item_id)
    if item is None or item.extraction_id != ext.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")
    return item


@router.post("/{ext_id}/items/{item_id}/approve", response_model=ItemOut)
async def approve(
    ext_id: uuid.UUID, item_id: uuid.UUID, body: ApproveIn, ctx: Writer, session: Session
) -> ExtractionItem:
    ext, _ = await _load(session, ext_id, ctx)
    item = await _item(session, ext, item_id)
    if item.state in ("auto_added", "approved"):
        return item
    if body.payload:
        item.payload = {**item.payload, **body.payload}
    try:
        await svc.apply_item(session, ctx, item)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    item.state = "approved"
    item.reason = ""
    await session.commit()
    await svc.refresh_status(session, ext)
    return item


@router.post("/{ext_id}/items/{item_id}/skip", response_model=ItemOut)
async def skip(
    ext_id: uuid.UUID, item_id: uuid.UUID, ctx: Writer, session: Session
) -> ExtractionItem:
    ext, _ = await _load(session, ext_id, ctx)
    item = await _item(session, ext, item_id)
    if item.state in ("auto_added", "approved"):
        await svc.undo_item(session, item)
    else:
        item.state = "skipped"
    await session.commit()
    await svc.refresh_status(session, ext)
    return item


class BulkOut(BaseModel):
    approved: int
    failed: list[str]


@router.post("/{ext_id}/approve-all", response_model=BulkOut)
async def approve_all(ext_id: uuid.UUID, ctx: Writer, session: Session) -> BulkOut:
    ext, _ = await _load(session, ext_id, ctx)
    done, failed = 0, []
    for item in await _items(session, ext.id):
        if item.state != "pending":
            continue
        try:
            await svc.apply_item(session, ctx, item)
        except ValueError as exc:
            item.reason = str(exc)
            failed.append(
                f"{item.payload.get('payee') or item.payload.get('name') or item.kind}: {exc}"
            )
            await session.commit()
            continue
        item.state = "approved"
        item.reason = ""
        await session.commit()
        done += 1
    await svc.refresh_status(session, ext)
    return BulkOut(approved=done, failed=failed)


@router.post("/{ext_id}/undo", response_model=FileOut)
async def undo(ext_id: uuid.UUID, ctx: Writer, session: Session) -> FileOut:
    """Take back everything this file added."""
    ext, att = await _load(session, ext_id, ctx)
    items = await _items(session, ext.id)
    for item in items:
        if item.state in ("auto_added", "approved"):
            await svc.undo_item(session, item)
    await session.commit()
    await svc.refresh_status(session, ext)
    return _out(ext, att, items)


@router.delete("/{ext_id}", status_code=204)
async def remove(ext_id: uuid.UUID, ctx: Writer, session: Session) -> None:
    """Forget the file (what it added stays in the books)."""
    _ext, att = await _load(session, ext_id, ctx)
    await asyncio.to_thread(get_storage().delete, att.object_key)
    await session.delete(att)
    await session.commit()
