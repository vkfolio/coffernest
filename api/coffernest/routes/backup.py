"""/api/v1/backup — passphrase-locked household backups (owner only). See services/backup.py."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from coffernest.deps import Owner, Session
from coffernest.models import AuditLog, Household
from coffernest.security import throttle
from coffernest.services import backup as svc
from coffernest.services import live

router = APIRouter(prefix="/backup", tags=["backup"])

MIME = "application/octet-stream"


class ExportIn(BaseModel):
    passphrase: str = Field(min_length=svc.MIN_PASSPHRASE, max_length=200)
    include_attachments: bool = True


class InspectOut(BaseModel):
    format: str
    version: int
    exported_at: datetime
    household_name: str
    counts: dict[str, int]
    attachments_included: bool
    attachments_count: int
    size: int


class ImportOut(BaseModel):
    counts: dict[str, int]
    warnings: list[str]
    files: int
    safety_backup: bool


def _download(data: bytes, filename: str) -> Response:
    return Response(
        content=data,
        media_type=MIME,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


async def _read(file: UploadFile) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while chunk := await file.read(1024 * 1024):
        size += len(chunk)
        if size > svc.MAX_BACKUP_BYTES:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "That backup is over 200 MB")
        chunks.append(chunk)
    return b"".join(chunks)


def _fail(exc: svc.BackupError) -> HTTPException:
    return HTTPException(exc.code, str(exc))


@router.post("/export", response_class=Response)
async def export(body: ExportIn, request: Request, ctx: Owner, session: Session) -> Response:
    """Download everything in this household as one file locked with ``passphrase``."""
    try:
        out = await svc.export_household(
            session, ctx.household_id, body.passphrase, body.include_attachments
        )
    except svc.BackupError as exc:
        raise _fail(exc) from exc
    _audit(session, request, ctx, "backup_exported", rows=sum(out.counts.values()), files=out.files)
    await session.commit()
    return _download(out.data, out.filename)


@router.post("/inspect", response_model=InspectOut)
async def inspect(ctx: Owner, file: UploadFile = File(...)) -> InspectOut:
    """What a backup holds, read from its plaintext manifest (no passphrase needed)."""
    data = await _read(file)
    try:
        manifest = svc.read_manifest(data)
    except svc.BackupError as exc:
        raise _fail(exc) from exc
    return InspectOut(
        format=manifest["format"],
        version=int(manifest["version"]),
        exported_at=manifest["exported_at"],
        household_name=manifest.get("household_name", ""),
        counts=manifest.get("counts", {}),
        attachments_included=bool(manifest.get("attachments_included")),
        attachments_count=int(manifest.get("attachments_count", 0)),
        size=len(data),
    )


@router.post("/import", response_model=ImportOut)
async def restore(
    request: Request,
    ctx: Owner,
    session: Session,
    file: UploadFile = File(...),
    passphrase: str = Form(..., min_length=1, max_length=200),
    confirm_name: str = Form(..., max_length=80),
) -> ImportOut:
    """Replace this household's finance data with a backup. A safety backup of what's here now
    is saved first, locked with the same passphrase."""
    household = await session.get(Household, ctx.household_id)
    if household is None or confirm_name.strip() != household.name.strip():
        raise HTTPException(status.HTTP_409_CONFLICT, "Type the household name exactly to confirm")
    data = await _read(file)
    try:
        opened = await asyncio.to_thread(svc.open_backup, data, passphrase)
        safety = await svc.export_household(session, ctx.household_id, passphrase, True)
    except svc.BackupError as exc:
        if "passphrase of at least" in str(exc):
            # The safety copy needs a usable passphrase; the backup's own one may be shorter.
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Backups are locked with at least 10 characters; that passphrase is too short",
            ) from exc
        raise _fail(exc) from exc
    key = f"{ctx.household_id}/backups/pre-restore-{datetime.now(UTC):%Y%m%d-%H%M%S}.cnz"
    await asyncio.to_thread(svc.save_object, key, safety.data)
    _audit(session, request, ctx, "backup_safety_saved", key=key, filename=safety.filename)
    await session.commit()
    try:
        result = await svc.restore_household(
            session, ctx.household_id, ctx.user_id, ctx.member_id, opened
        )
    except svc.BackupError as exc:
        raise _fail(exc) from exc
    _audit(
        session,
        request,
        ctx,
        "backup_imported",
        rows=sum(result.counts.values()),
        files=result.files,
        warnings=len(result.warnings),
    )
    await session.commit()
    live.publish(ctx.household_id)
    return ImportOut(
        counts=result.counts, warnings=result.warnings, files=result.files, safety_backup=True
    )


@router.get("/last-safety", response_class=Response)
async def last_safety(ctx: Owner, session: Session) -> Response:
    """The backup saved automatically before the most recent restore."""
    row = await session.scalar(
        select(AuditLog)
        .where(AuditLog.household_id == ctx.household_id, AuditLog.action == "backup_safety_saved")
        .order_by(AuditLog.created_at.desc())
        .limit(1)
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No restore has been done yet")
    try:
        data = await asyncio.to_thread(svc.load_object, str(row.meta["key"]))
    except Exception as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That safety backup is gone") from exc
    return _download(data, str(row.meta.get("filename") or "coffernest-before-restore.cnz"))


def _audit(session: Session, request: Request, ctx: Any, action: str, **meta: Any) -> None:
    session.add(
        AuditLog(
            household_id=ctx.household_id,
            user_id=ctx.user_id,
            action=action,
            ip=throttle.client_ip(request),
            meta=meta,
        )
    )
