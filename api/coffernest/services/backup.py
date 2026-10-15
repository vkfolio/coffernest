"""Household backup: one passphrase-locked file with every finance row and uploaded file.

File (``.cnz``, a zip; files from VKFinance used ``.vkfz``):
- ``manifest.json``: plaintext summary (who, when, counts, KDF salt) so a restore can preview it.
- ``payload.bin``: the rows as JSON, AES-256-GCM with a key from scrypt(passphrase).
- ``files/<attachment id>``: each uploaded file, encrypted the same way.

Sealed columns (full account numbers, saved PDF passwords) are opened on export and sealed again
on import: the server key behind ``security.box`` differs between installs, the passphrase
doesn't. Logins, members, sessions, tokens and the audit log are never in a backup.

Restore replaces the household's finance data in one transaction. Every id is new (so a backup
can go into any household, even the one it came from), members are matched by email, and a
safety backup of what was there is saved first.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import enum
import hashlib
import io
import json
import os
import re
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from sqlalchemy import (
    ARRAY,
    Column,
    Date,
    DateTime,
    Float,
    Numeric,
    Table,
    delete,
    insert,
    select,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncSession

from coffernest.models import (
    Account,
    AiSuggestion,
    Attachment,
    CardEmi,
    CardStatement,
    Category,
    CategoryRule,
    ChatMessage,
    ChatThread,
    CreditCard,
    DocPassword,
    Extraction,
    ExtractionItem,
    Household,
    Loan,
    LoanPrepayment,
    LoanRateChange,
    Member,
    Notification,
    Person,
    RecurringRule,
    SavedPlan,
    SkillSetting,
    Transaction,
    User,
)
from coffernest.security.box import seal, unseal
from coffernest.storage import get_storage

# Wire-format ids kept from VKFinance (Coffernest's original name) so its backups restore.
FORMAT = "vkfinance-backup"
VERSION = 1
SCHEMA_REV = "0005"
MAX_BACKUP_BYTES = 200 * 1024 * 1024
MAX_PAYLOAD_BYTES = 50 * 1024 * 1024
MAX_FILE_BYTES = 30 * 1024 * 1024
MAX_ROWS = 500_000
MIN_PASSPHRASE = 10
SCRYPT = {"n": 2**15, "r": 8, "p": 1}
PAYLOAD_AAD = b"vkfinance-backup-v1"
HOUSEHOLD_FIELDS = ("name", "currency", "timezone", "fy_start_month", "ai_enabled", "spend_buffer")
SEALED = {("account", "number_sealed"), ("doc_password", "ciphertext")}
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class BackupError(Exception):
    """A backup that can't be read or restored; the message is shown to the owner."""

    def __init__(self, message: str, code: int = 400) -> None:
        super().__init__(message)
        self.code = code


# Insert order (parents first). ``via`` = the parent column for tables without household_id.
@dataclass(frozen=True)
class _Spec:
    model: Any
    via: tuple[str, Any] | None = None

    @property
    def table(self) -> Table:
        return self.model.__table__  # type: ignore[no-any-return]


SPECS: tuple[_Spec, ...] = (
    _Spec(Category),
    _Spec(Person),
    _Spec(CategoryRule),
    _Spec(Account),
    _Spec(Loan),
    _Spec(LoanRateChange, ("loan_id", Loan)),
    _Spec(CreditCard),
    _Spec(CardStatement, ("card_id", CreditCard)),
    _Spec(CardEmi, ("card_id", CreditCard)),
    _Spec(RecurringRule),
    _Spec(Transaction),
    _Spec(LoanPrepayment, ("loan_id", Loan)),
    _Spec(Attachment),
    _Spec(Extraction),
    _Spec(ExtractionItem),
    _Spec(DocPassword),
    _Spec(ChatThread),
    _Spec(ChatMessage, ("thread_id", ChatThread)),
    _Spec(SavedPlan),
    _Spec(SkillSetting),
    _Spec(AiSuggestion),
)

# Cleared before a restore, children first. Rows of the other tables go with their parents.
WIPE = (
    ExtractionItem,
    Extraction,
    Attachment,
    DocPassword,
    ChatThread,
    SavedPlan,
    SkillSetting,
    AiSuggestion,
    Notification,
    Transaction,
    RecurringRule,
    CreditCard,
    Loan,
    Account,
    CategoryRule,
    Person,
    Category,
)


# ---------- storage ----------


def save_object(key: str, data: bytes) -> None:
    get_storage().put(key, data, "application/octet-stream")


def load_object(key: str) -> bytes:
    return get_storage().get(key, MAX_BACKUP_BYTES)


# ---------- crypto ----------


def _key(passphrase: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, **SCRYPT).derive(passphrase.encode())


def _seal(key: bytes, data: bytes, aad: bytes) -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, data, aad)


def _open(key: bytes, blob: bytes, aad: bytes) -> bytes:
    try:
        return AESGCM(key).decrypt(blob[:12], blob[12:], aad)
    except InvalidTag as exc:
        raise BackupError("That passphrase doesn't open this backup") from exc


# ---------- values ----------


def _dump(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int, float, dict)):
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, (list, tuple)):
        return [_dump(v) for v in value]
    raise TypeError(f"Can't back up a {type(value).__name__}")


def _load(column: Column[Any], value: Any) -> Any:
    if value is None:
        return None
    kind = column.type
    if isinstance(kind, UUID):
        return uuid.UUID(value)
    if isinstance(kind, Float):
        return float(value)
    if isinstance(kind, Numeric):
        return Decimal(value)
    if isinstance(kind, DateTime):
        return datetime.fromisoformat(value)
    if isinstance(kind, Date):
        return date.fromisoformat(value)
    if isinstance(kind, SAEnum) and kind.enum_class is not None:
        return kind.enum_class(value)
    if isinstance(kind, ARRAY) and isinstance(kind.item_type, Date):
        return [date.fromisoformat(v) for v in value]
    return value


def _fk_target(column: Column[Any]) -> str | None:
    for fk in column.foreign_keys:
        return fk.column.table.name
    return None


# ---------- export ----------


@dataclass
class Exported:
    data: bytes
    filename: str
    counts: dict[str, int]
    files: int
    warnings: list[str] = field(default_factory=list)


async def _rows(
    session: AsyncSession, spec: _Spec, household_id: uuid.UUID
) -> list[dict[str, Any]]:
    table = spec.table
    query = select(table)
    if spec.via is None:
        query = query.where(table.c.household_id == household_id)
    else:
        column, parent = spec.via
        query = query.where(
            table.c[column].in_(select(parent.id).where(parent.household_id == household_id))
        )
    return [dict(r) for r in (await session.execute(query)).mappings().all()]


async def export_household(
    session: AsyncSession,
    household_id: uuid.UUID,
    passphrase: str,
    include_files: bool = True,
) -> Exported:
    if len(passphrase) < MIN_PASSPHRASE:
        raise BackupError(f"Use a passphrase of at least {MIN_PASSPHRASE} characters")
    household = await session.get(Household, household_id)
    if household is None:
        raise BackupError("Household not found", 404)
    warnings: list[str] = []
    tables: dict[str, list[dict[str, Any]]] = {}
    for spec in SPECS:
        rows = []
        for row in await _rows(session, spec, household_id):
            for name in list(row):
                if (spec.table.name, name) in SEALED and row[name]:
                    plain = unseal(row[name])
                    if plain is None:
                        warnings.append(f"A sealed value in {spec.table.name} couldn't be opened")
                    row[name] = plain
            rows.append({k: _dump(v) for k, v in row.items()})
        tables[spec.table.name] = rows
    members = [
        {"id": str(m.id), "email": u.email, "display_name": u.display_name, "role": m.role.value}
        for m, u in (
            await session.execute(
                select(Member, User)
                .join(User, User.id == Member.user_id)
                .where(Member.household_id == household_id)
            )
        ).all()
    ]
    body = {
        "household": {f: _dump(getattr(household, f)) for f in HOUSEHOLD_FIELDS},
        "members": members,
        "tables": tables,
    }
    salt = os.urandom(16)
    key = await asyncio.to_thread(_key, passphrase, salt)
    payload = _seal(key, json.dumps(body, separators=(",", ":")).encode(), PAYLOAD_AAD)

    files: dict[str, bytes] = {}
    if include_files:
        storage = get_storage()
        for att in tables["attachment"]:
            try:
                raw = await asyncio.to_thread(storage.get, att["object_key"], MAX_FILE_BYTES)
            except Exception:
                warnings.append(f"{att['filename']} is missing from storage and was left out")
                continue
            files[att["id"]] = _seal(key, raw, b"file:" + att["id"].encode())

    now = datetime.now(UTC)
    counts = {name: len(rows) for name, rows in tables.items()}
    manifest = {
        "format": FORMAT,
        "version": VERSION,
        "schema_rev": SCHEMA_REV,
        "exported_at": now.isoformat(),
        "household_name": household.name,
        "counts": counts,
        "attachments_included": include_files,
        "attachments_count": len(files),
        "kdf": {"name": "scrypt", "salt": base64.b64encode(salt).decode(), **SCRYPT},
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:  # ciphertext doesn't compress
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.writestr("payload.bin", payload)
        for att_id, blob in files.items():
            zf.writestr(f"files/{att_id}", blob)
    slug = re.sub(r"[^a-z0-9]+", "-", household.name.lower()).strip("-") or "household"
    return Exported(
        data=buf.getvalue(),
        filename=f"coffernest-backup-{slug}-{now:%Y%m%d-%H%M}.cnz",
        counts=counts,
        files=len(files),
        warnings=warnings,
    )


# ---------- read ----------


def read_manifest(data: bytes) -> dict[str, Any]:
    if len(data) > MAX_BACKUP_BYTES:
        raise BackupError("That backup is too big (over 200 MB)", 413)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            manifest: dict[str, Any] = json.loads(zf.read("manifest.json"))
    except (zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise BackupError("That isn't a Coffernest backup file") from exc
    if manifest.get("format") != FORMAT:
        raise BackupError("That isn't a Coffernest backup file")
    if int(manifest.get("version", 0)) > VERSION:
        raise BackupError("This backup was made by a newer version of Coffernest")
    return manifest


Opened = tuple[dict[str, Any], dict[str, bytes], bytes]


def open_backup(data: bytes, passphrase: str) -> Opened:
    """(rows, encrypted files, key). Raises BackupError on a wrong passphrase or bad file."""
    manifest = read_manifest(data)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        payload = zf.read("payload.bin")
        if len(payload) > MAX_PAYLOAD_BYTES + 64:
            raise BackupError("That backup's data is too big", 413)
        if hashlib.sha256(payload).hexdigest() != manifest.get("payload_sha256"):
            raise BackupError("This backup file is damaged")
        files = {n.split("/", 1)[1]: zf.read(n) for n in zf.namelist() if n.startswith("files/")}
    kdf = manifest["kdf"]
    salt = base64.b64decode(kdf["salt"])
    key = Scrypt(salt=salt, length=32, n=int(kdf["n"]), r=int(kdf["r"]), p=int(kdf["p"])).derive(
        passphrase.encode()
    )
    body: dict[str, Any] = json.loads(_open(key, payload, PAYLOAD_AAD))
    return body, files, key


# ---------- import ----------


@dataclass
class Restored:
    counts: dict[str, int]
    warnings: list[str]
    files: int


def _remap_text(text: str, ids: dict[str, str]) -> str:
    return _UUID_RE.sub(lambda m: ids.get(m.group(0), m.group(0)), text)


def _remap_json(value: Any, ids: dict[str, str]) -> Any:
    if isinstance(value, str):
        return _remap_text(value, ids)
    if isinstance(value, list):
        return [_remap_json(v, ids) for v in value]
    if isinstance(value, dict):
        return {_remap_text(k, ids): _remap_json(v, ids) for k, v in value.items()}
    return value


async def restore_household(
    session: AsyncSession,
    household_id: uuid.UUID,
    user_id: uuid.UUID,
    member_id: uuid.UUID,
    opened: Opened,
) -> Restored:
    """Replace this household's finance data with an opened backup. Commits on success."""
    body, files, key = opened
    tables: dict[str, list[dict[str, Any]]] = body.get("tables", {})
    if sum(len(v) for v in tables.values()) > MAX_ROWS:
        raise BackupError("That backup has too many rows to restore", 413)
    warnings: list[str] = []
    hid = str(household_id)

    # Members: same email in this household keeps its rows; anyone else's become the owner's.
    here = {
        u.email.lower(): m.id
        for m, u in (
            await session.execute(
                select(Member, User)
                .join(User, User.id == Member.user_id)
                .where(Member.household_id == household_id)
            )
        ).all()
    }
    members: dict[str, str] = {}
    unmatched = []
    for m in body.get("members", []):
        found = here.get(str(m.get("email", "")).lower())
        members[m["id"]] = str(found or member_id)
        if found is None:
            unmatched.append(m.get("display_name") or m.get("email"))
    if unmatched:
        warnings.append(
            "Entries by " + ", ".join(unmatched) + " are now yours: they aren't in this household"
        )

    # New ids for every row (and for bare group ids such as transfer_group_id).
    ids: dict[str, str] = {}
    for spec in SPECS:
        for row in tables.get(spec.table.name, []):
            ids[row["id"]] = str(uuid.uuid4())

    def fix(spec: _Spec, row: dict[str, Any]) -> dict[str, Any] | None:
        out: dict[str, Any] = {}
        for column in spec.table.columns:
            if column.name not in row:
                continue
            value = row[column.name]
            if column.name == "household_id":
                value = hid
            elif value is not None and isinstance(column.type, UUID):
                target = _fk_target(column)
                if target == "member":
                    value = members.get(value, str(member_id))
                elif target == "user_account":
                    value = str(user_id)
                elif target is not None:
                    if value not in ids:
                        if not column.nullable:
                            return None  # dangling parent: drop the row
                        value = None
                    else:
                        value = ids[value]
                else:
                    value = ids.setdefault(value, str(uuid.uuid4()))
            elif value is not None and isinstance(column.type, (JSONB, ARRAY)):
                value = _remap_json(value, ids)
            if (spec.table.name, column.name) in SEALED and value:
                value = seal(value)
            out[column.name] = _load(column, value)
        return out

    storage = get_storage()
    uploaded: list[str] = []
    missing_files = 0
    try:
        for model in WIPE:
            await session.execute(delete(model).where(model.household_id == household_id))
        counts: dict[str, int] = {}
        for spec in SPECS:
            raw = tables.get(spec.table.name, [])
            if spec.model is Category:
                raw = _by_depth(raw)
            rows = [r for r in (fix(spec, row) for row in raw) if r is not None]
            if spec.model is Attachment:
                missing_files = await _put_files(rows, raw, ids, files, key, hid, uploaded)
            if len(rows) < len(raw) and spec.model is not Attachment:
                warnings.append(f"{len(raw) - len(rows)} {spec.table.name} rows were skipped")
            counts[spec.table.name] = len(rows)
            if spec.model is Category:
                for level in _levels(rows):
                    await session.execute(insert(spec.table), level)
            elif rows:
                for chunk in range(0, len(rows), 1000):
                    await session.execute(insert(spec.table), rows[chunk : chunk + 1000])
        household = await session.get(Household, household_id)
        assert household is not None
        for name, value in (body.get("household") or {}).items():
            if name in HOUSEHOLD_FIELDS and value is not None:
                setattr(household, name, value)
        await session.commit()
    except Exception:
        await session.rollback()
        for object_key in uploaded:
            await asyncio.to_thread(_quiet, storage.delete, object_key)
        raise
    if missing_files:
        warnings.append(f"{missing_files} uploaded files weren't in the backup")
    return Restored(counts=counts, warnings=warnings, files=len(uploaded))


def _quiet(fn: Callable[[str], None], arg: str) -> None:
    with contextlib.suppress(Exception):  # best-effort cleanup
        fn(arg)


async def _put_files(
    rows: list[dict[str, Any]],
    raw: list[dict[str, Any]],
    ids: dict[str, str],
    files: dict[str, bytes],
    key: bytes,
    hid: str,
    uploaded: list[str],
) -> int:
    """Upload each attachment's file under a fresh key in this household. A file the backup
    doesn't carry keeps its old key (still there when restoring into the same household)."""
    storage = get_storage()
    by_new = {str(r["id"]): r for r in rows}
    missing = 0
    for old in raw:
        row = by_new.get(ids.get(old["id"], ""))
        if row is None:
            continue
        blob = files.get(old["id"])
        if blob is None:
            missing += 1
            continue
        plain = _open(key, blob, b"file:" + old["id"].encode())
        base = str(old["object_key"]).rsplit("/", 1)[-1]
        object_key = f"{hid}/restored/{uuid.UUID(str(row['id'])).hex}-{base}"[:255]
        await asyncio.to_thread(storage.put, object_key, plain, row["mime"])
        uploaded.append(object_key)
        row["object_key"] = object_key
    return missing


def _by_depth(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {r["id"]: r for r in rows}

    def depth(row: dict[str, Any]) -> int:
        d, seen, cur = 0, set(), row
        while cur.get("parent_id") and cur["parent_id"] in by_id and cur["id"] not in seen:
            seen.add(cur["id"])
            cur = by_id[cur["parent_id"]]
            d += 1
        return d

    return sorted(rows, key=depth)


def _levels(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Categories grouped so each parent is inserted before its children."""
    done: set[uuid.UUID] = set()
    left = list(rows)
    out: list[list[dict[str, Any]]] = []
    while left:
        level = [r for r in left if r.get("parent_id") is None or r["parent_id"] in done]
        if not level:  # a cycle: break it rather than loop
            level = [{**r, "parent_id": None} for r in left]
        out.append(level)
        done |= {r["id"] for r in level}
        left = [r for r in left if r["id"] not in done]
    return out
