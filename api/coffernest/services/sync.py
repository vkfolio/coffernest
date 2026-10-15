"""Two-way sync between a device's own copy of the books and this server.

A device sends what changed since it last synced (whole rows, with the time each was edited) and
gets back everything that changed here since its cursor. Conflicts go to the newest edit, row by
row. Rows use the database's own columns (the same shapes as a backup), minus what only the
server keeps: who entered a row, members, sealed account numbers and the recurring-rule
bookkeeping.

Ids: a device makes its own UUIDs. When a row it sends is one the server already has under
another id (a default category, a person with the same name, a statement for the same card and
date, an occurrence of the same bill), the server keeps its id and answers with ``id_map`` so
the device renames its copy.

Cursor: the oldest transaction still running when the changes were read
(``pg_snapshot_xmin``). Rows changed by any transaction from that one on are sent again next
time, so a write that commits late is never missed; sending a row twice is harmless.

Scheduled occurrences of a bill that nobody has touched are left out of a pull: every device
creates its own from the bill's rule. Once one is edited, paid or deleted it travels like any
other row, and the device matches it to its own copy by rule, date and account.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    Column,
    Table,
    and_,
    bindparam,
    delete,
    insert,
    literal_column,
    not_,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from coffernest.enums import Role, TxnStatus
from coffernest.models import (
    Account,
    CardEmi,
    CardStatement,
    Category,
    CategoryRule,
    CreditCard,
    Household,
    Loan,
    LoanPrepayment,
    LoanRateChange,
    Person,
    RecurringRule,
    Transaction,
)
from coffernest.services.backup import _by_depth, _dump, _fk_target, _load

MAX_ROWS = 5000  # per request; a device sends a big first sync in several


@dataclass(frozen=True)
class _Spec:
    model: Any
    via: tuple[str, Any] | None = None  # parent column and model, for tables without household_id
    natural: tuple[str, ...] = ()  # columns that identify the same thing on both sides

    @property
    def table(self) -> Table:
        return self.model.__table__  # type: ignore[no-any-return]

    @property
    def name(self) -> str:
        return self.table.name


# Parents first.
SPECS: tuple[_Spec, ...] = (
    _Spec(Category, natural=("parent_id", "name")),
    _Spec(Person, natural=("name",)),
    _Spec(CategoryRule, natural=("match",)),
    _Spec(Account),
    _Spec(Loan, natural=("account_id",)),
    _Spec(LoanRateChange, ("loan_id", Loan)),
    _Spec(CreditCard, natural=("account_id",)),
    _Spec(CardStatement, ("card_id", CreditCard), natural=("card_id", "statement_date")),
    _Spec(CardEmi, ("card_id", CreditCard)),
    _Spec(RecurringRule),
    _Spec(Transaction, natural=("recurring_rule_id", "occurrence_date", "account_id")),
    _Spec(LoanPrepayment, ("loan_id", Loan)),
)
BY_NAME = {s.name: s for s in SPECS}
# When a device's row turns out to be one the household already has under another id (its
# starter categories, a person with the same name), the household's copy is kept. For the rest
# (a bill occurrence paid on the phone, a statement) the newest edit still wins.
SERVER_COPY_WINS = (Category, Person, CategoryRule, Loan, CreditCard)

# Never sent, never taken from a device.
HIDDEN = {"household_id", "member_id", "owner_member_id", "created_by", "number_sealed"}
# Sent, but only taken from a device when the row is new.
INSERT_ONLY = {"created_at", "materialized_until"}
HOUSEHOLD_FIELDS = ("name", "currency", "timezone", "fy_start_month", "ai_enabled", "spend_buffer")


class SyncError(Exception):
    def __init__(self, message: str, code: int = 400) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class Result:
    cursor: str | None = None
    changes: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))
    deletes: list[dict[str, str]] = field(default_factory=list)
    id_map: dict[str, str] = field(default_factory=dict)
    rejected: list[dict[str, str]] = field(default_factory=list)
    household: dict[str, Any] | None = None
    applied: int = 0

    def body(self) -> dict[str, Any]:
        return {
            "cursor": self.cursor,
            "changes": dict(self.changes),
            "deletes": self.deletes,
            "id_map": self.id_map,
            "rejected": self.rejected,
            "household": self.household,
            "applied": self.applied,
        }


def _wire_columns(spec: _Spec) -> list[Column[Any]]:
    return [c for c in spec.table.columns if c.name not in HIDDEN]


def _wire(spec: _Spec, row: Any) -> dict[str, Any]:
    return {c.name: _dump(row[c.name]) for c in _wire_columns(spec)}


def _scope(spec: _Spec, hid: uuid.UUID) -> ColumnElement[bool]:
    t = spec.table
    if spec.via is None:
        return t.c.household_id == hid
    column, parent = spec.via
    return t.c[column].in_(select(parent.id).where(parent.household_id == hid))


def _uuid(value: Any) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def _when(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


class _Session:
    """One sync request: the household, the device and what has been decided so far."""

    def __init__(
        self, session: AsyncSession, hid: uuid.UUID, member: uuid.UUID, user: uuid.UUID, client: str
    ) -> None:
        self.s = session
        self.hid = hid
        self.member = member
        self.user = user
        self.client = client
        self.out = Result()
        self.owned: dict[str, set[str]] = {}

    async def load_owned(self) -> None:
        for spec in SPECS:
            if spec.model is Transaction:
                continue  # big; looked up when a row points at one
            ids = (
                await self.s.scalars(select(spec.table.c.id).where(_scope(spec, self.hid)))
            ).all()
            self.owned[spec.name] = {str(i) for i in ids}

    async def owns(self, table: str, ids: set[str]) -> set[str]:
        if table in self.owned:
            return ids & self.owned[table]
        found = (
            await self.s.scalars(
                select(Transaction.id).where(
                    Transaction.household_id == self.hid,
                    Transaction.id.in_([uuid.UUID(i) for i in ids]),
                )
            )
        ).all()
        return {str(i) for i in found}

    def reject(self, table: str, row_id: Any, reason: str) -> None:
        self.out.rejected.append({"table": table, "id": str(row_id), "reason": reason})

    # ---------- push ----------

    async def push(self, changes: dict[str, list[dict[str, Any]]]) -> None:
        for name in changes:
            if name not in BY_NAME:
                raise SyncError(f"Unknown table: {name}")
        for spec in SPECS:
            rows = changes.get(spec.name) or []
            if spec.model is Category:
                # A level at a time, so children follow a parent that turned out to exist here.
                for level in _depth_levels(rows):
                    await self._push_table(spec, level)
            elif rows:
                await self._push_table(spec, rows)

    async def _push_table(self, spec: _Spec, raw: list[dict[str, Any]]) -> None:
        t = spec.table
        wire = {c.name: c for c in _wire_columns(spec)}
        refs = {c.name: target for c in wire.values() if (target := _fk_target(c)) in BY_NAME}
        # 1. Shape: ids, edit times, renamed parents.
        rows: list[dict[str, Any]] = []
        for row in raw:
            if not isinstance(row, dict):
                raise SyncError(f"Rows in {spec.name} must be objects")
            rid = _uuid(row.get("id"))
            edited = _when(row.get("updated_at"))
            if rid is None or edited is None:
                self.reject(spec.name, row.get("id"), "Needs an id and updated_at")
                continue
            clean = {k: v for k, v in row.items() if k in wire}
            clean["id"] = self.out.id_map.get(rid, rid)
            for col in refs:
                if clean.get(col) is not None:
                    ref = _uuid(clean[col])
                    clean[col] = self.out.id_map.get(ref, ref) if ref else "bad"
            clean["_sent_id"] = rid
            rows.append(clean)

        # 2. Parents must be this household's (or arriving in this same push).
        incoming = {r["id"] for r in rows}
        for col, target in refs.items():
            wanted = {r[col] for r in rows if r.get(col) not in (None, "bad")}
            ok = await self.owns(target, wanted) if wanted else set()
            if target == spec.name:
                ok |= wanted & incoming
            for r in list(rows):
                value = r.get(col)
                if value is None or value in ok:
                    continue
                if wire[col].nullable and target != spec.name:
                    r[col] = None
                else:
                    # A sub-category whose parent hasn't arrived stays on the device for next time
                    # rather than turning into a top-level one.
                    self.reject(spec.name, r["_sent_id"], f"Its {col} isn't in this household")
                    rows.remove(r)

        # 3. What the server already has: same id, or the same thing under another id.
        ids = [uuid.UUID(r["id"]) for r in rows]
        scope_col = t.c.household_id if spec.via is None else t.c[spec.via[0]]
        existing = {
            str(r.id): r
            for r in (
                await self.s.execute(
                    select(t.c.id, scope_col.label("scope"), t.c.updated_at).where(t.c.id.in_(ids))
                )
            ).all()
        }
        parent_owned = (
            None if spec.via is None else self.owned.get(BY_NAME_PARENT[spec.name], set())
        )
        natural = await self._natural(spec, rows) if spec.natural else {}

        inserts: list[dict[str, Any]] = []
        updates: list[dict[str, Any]] = []
        losers: list[str] = []
        tomb_check: list[dict[str, Any]] = []
        for r in rows:
            hit = existing.get(r["id"])
            if hit is not None:
                mine = (
                    hit.scope == self.hid
                    if parent_owned is None
                    else str(hit.scope) in parent_owned
                )
                if not mine:  # an id another household uses: give this row a new one
                    new = str(uuid.uuid4())
                    self.out.id_map[r["_sent_id"]] = new
                    r["id"] = new
                    hit = None
            adopted = False
            if hit is None and spec.natural:
                key = tuple(_key(r.get(c)) for c in spec.natural)
                if _natural_ok(spec, key) and key in natural:
                    sid = natural[key]
                    if sid != r["id"]:
                        self.out.id_map[r["_sent_id"]] = sid
                        r["id"] = sid
                        adopted = spec.model in SERVER_COPY_WINS
                    hit = existing.get(sid) or await self._row_meta(spec, sid)
            if hit is None:
                tomb_check.append(r)
                continue
            edited = _when(r["updated_at"])
            assert edited is not None
            if adopted:
                losers.append(r["id"])  # a new device's starter copy: the household's version stays
            elif hit.updated_at is None or edited > hit.updated_at:
                updates.append(r)
            elif edited < hit.updated_at:
                losers.append(r["id"])
            if spec.natural:
                natural[tuple(_key(r.get(c)) for c in spec.natural)] = r["id"]

        # 4. New rows the server deleted later than they were last edited stay deleted.
        if tomb_check:
            tombs: dict[str, datetime] = dict(
                (
                    await self.s.execute(
                        text(
                            "SELECT row_id::text, deleted_at FROM sync_tombstone "
                            "WHERE table_name = :t AND household_id = :h AND row_id = ANY(:ids)"
                        ),
                        {
                            "t": spec.name,
                            "h": self.hid,
                            "ids": [uuid.UUID(r["id"]) for r in tomb_check],
                        },
                    )
                ).all()
            )
            for r in tomb_check:
                gone = tombs.get(r["id"])
                if gone is not None and _when(r["updated_at"]) <= gone:  # type: ignore[operator]
                    self.out.deletes.append({"table": spec.name, "id": r["id"]})
                    continue
                inserts.append(r)
                if spec.natural:
                    natural[tuple(_key(r.get(c)) for c in spec.natural)] = r["id"]

        await self._write(spec, inserts, updates, wire)
        if spec.name in self.owned:
            self.owned[spec.name] |= {r["id"] for r in inserts}
        if losers:
            await self._send_rows(spec, losers)

    async def _row_meta(self, spec: _Spec, sid: str) -> Any:
        t = spec.table
        return (
            await self.s.execute(select(t.c.id, t.c.updated_at).where(t.c.id == uuid.UUID(sid)))
        ).first()

    async def _natural(self, spec: _Spec, rows: list[dict[str, Any]]) -> dict[tuple[Any, ...], str]:
        t = spec.table
        cols = [t.c[c] for c in spec.natural]
        query = select(t.c.id, *cols).where(_scope(spec, self.hid))
        if spec.model is Transaction:
            rules = {r["recurring_rule_id"] for r in rows if r.get("recurring_rule_id")}
            if not rules:
                return {}
            query = query.where(
                t.c.recurring_rule_id.in_([uuid.UUID(x) for x in rules]),
                t.c.deleted_at.is_(None),
            )
        out: dict[tuple[Any, ...], str] = {}
        for row in (await self.s.execute(query)).all():
            out[tuple(_key(row[i + 1]) for i in range(len(cols)))] = str(row[0])
        return out

    def _values(
        self, spec: _Spec, r: dict[str, Any], wire: dict[str, Column[Any]], new: bool
    ) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, value in r.items():
            if name.startswith("_") or name not in wire:
                continue
            if not new and name in INSERT_ONLY:
                continue
            out[name] = _load(wire[name], value)
        if new:
            if spec.via is None:
                out["household_id"] = self.hid
            if spec.model in (Transaction, RecurringRule):
                out["member_id"] = self.member
            if spec.model is Transaction:
                out["created_by"] = self.user
        return out

    async def _write(
        self,
        spec: _Spec,
        inserts: list[dict[str, Any]],
        updates: list[dict[str, Any]],
        wire: dict[str, Column[Any]],
    ) -> None:
        t = spec.table
        prepared: list[tuple[bool, dict[str, Any], dict[str, Any]]] = []
        for new, batch in ((True, inserts), (False, updates)):
            for r in batch:
                try:
                    prepared.append((new, r, self._values(spec, r, wire, new)))
                except (ValueError, TypeError, ArithmeticError) as exc:
                    self.reject(spec.name, r["_sent_id"], f"Bad value: {exc}"[:200])
        if not prepared:
            return

        async def run(group: list[tuple[bool, dict[str, Any], dict[str, Any]]]) -> None:
            by_shape: dict[tuple[bool, tuple[str, ...]], list[dict[str, Any]]] = defaultdict(list)
            for new, _, values in group:
                by_shape[(new, tuple(sorted(values)))].append(values)
            for (new, keys), batch in by_shape.items():
                if new:
                    await self.s.execute(insert(t), batch)
                else:
                    stmt = (
                        update(t)
                        .where(t.c.id == bindparam("_id"))
                        .values({k: bindparam("v_" + k) for k in keys if k != "id"})
                    )
                    params = [
                        {"_id": v["id"], **{"v_" + k: x for k, x in v.items() if k != "id"}}
                        for v in batch
                    ]
                    await self.s.execute(stmt, params)

        try:
            async with self.s.begin_nested():
                await run(prepared)
            self.out.applied += len(prepared)
            return
        except DBAPIError:
            pass
        # Something in the batch broke a rule: save the rest, report the ones that won't go in.
        for item in prepared:
            try:
                async with self.s.begin_nested():
                    await run([item])
                self.out.applied += 1
            except DBAPIError as exc:
                reason = str(exc.orig).splitlines()[0] if exc.orig else "Rejected"
                self.reject(spec.name, item[1]["_sent_id"], reason[:200])
                if item[0] and spec.name in self.owned:
                    self.owned[spec.name].discard(item[1]["id"])

    async def _send_rows(self, spec: _Spec, ids: list[str]) -> None:
        t = spec.table
        rows = (
            await self.s.execute(select(t).where(t.c.id.in_([uuid.UUID(i) for i in ids])))
        ).mappings()
        self.out.changes[spec.name].extend(_wire(spec, r) for r in rows)

    async def push_deletes(self, deletes: list[dict[str, Any]]) -> None:
        by_table: dict[str, list[tuple[str, datetime]]] = defaultdict(list)
        for d in deletes:
            name = d.get("table")
            rid = _uuid(d.get("id"))
            when = _when(d.get("deleted_at"))
            if name not in BY_NAME or rid is None or when is None:
                self.reject(str(name), d.get("id"), "Needs a table, an id and deleted_at")
                continue
            by_table[name].append((self.out.id_map.get(rid, rid), when))
        for spec in reversed(SPECS):  # children first
            items = by_table.get(spec.name)
            if not items:
                continue
            t = spec.table
            found = {
                str(r.id): r.updated_at
                for r in (
                    await self.s.execute(
                        select(t.c.id, t.c.updated_at).where(
                            _scope(spec, self.hid), t.c.id.in_([uuid.UUID(i) for i, _ in items])
                        )
                    )
                ).all()
            }
            doomed: list[tuple[str, datetime]] = []
            keep: list[str] = []
            for rid, when in items:
                if rid not in found:
                    continue
                if found[rid] is not None and found[rid] > when:
                    keep.append(rid)  # edited here after the device deleted it
                else:
                    doomed.append((rid, when))
            for rid, when in doomed:
                if spec.model is Transaction:
                    await self.s.execute(
                        update(t)
                        .where(t.c.id == uuid.UUID(rid))
                        .values(deleted_at=when, updated_at=when)
                    )
                else:
                    await self.s.execute(delete(t).where(t.c.id == uuid.UUID(rid)))
                self.out.applied += 1
            if keep:
                await self._send_rows(spec, keep)

    async def push_household(self, values: dict[str, Any], role: Role) -> None:
        if role != Role.owner:
            return  # only the owner changes household settings
        when = _when(values.get("updated_at"))
        household = await self.s.get(Household, self.hid)
        if household is None or when is None:
            return
        if household.updated_at is not None and when <= household.updated_at:
            return
        changed = False
        name = values.get("name")
        if isinstance(name, str) and 0 < len(name.strip()) <= 80:
            household.name = name.strip()
            changed = True
        buffer = values.get("spend_buffer")
        if isinstance(buffer, int) and not isinstance(buffer, bool) and 0 <= buffer <= 10_000_000:
            household.spend_buffer = buffer
            changed = True
        if changed:
            household.updated_at = when
            await self.s.flush()
            self.out.applied += 1

    # ---------- pull ----------

    async def pull(self, cursor: str | None) -> None:
        self.out.cursor = await self.s.scalar(
            text("SELECT pg_snapshot_xmin(pg_current_snapshot())::text")
        )
        others = literal_column("sync_client").is_distinct_from(self.client)
        since = (
            literal_column("sync_xid") >= literal_column(f"'{int(cursor)}'::xid8")
            if cursor
            else None
        )
        for spec in SPECS:
            t = spec.table
            query = select(t).where(_scope(spec, self.hid), others)
            if since is not None:
                query = query.where(since)
            if spec.model is Transaction:
                query = query.where(
                    not_(
                        and_(
                            t.c.recurring_rule_id.is_not(None),
                            t.c.status == TxnStatus.scheduled,
                            t.c.deleted_at.is_(None),
                            or_(t.c.updated_at.is_(None), t.c.updated_at == t.c.created_at),
                        )
                    )
                )
                if since is None:
                    query = query.where(t.c.deleted_at.is_(None))
            for row in (await self.s.execute(query)).mappings():
                if spec.model is Transaction and row["deleted_at"] is not None:
                    self.out.deletes.append({"table": spec.name, "id": str(row["id"])})
                else:
                    self.out.changes[spec.name].append(_wire(spec, row))
        if since is not None:
            gone = await self.s.execute(
                text(
                    "SELECT table_name, row_id::text FROM sync_tombstone "
                    "WHERE household_id = :h AND sync_xid >= CAST(:c AS xid8) "
                    "AND sync_client IS DISTINCT FROM :me"
                ),
                {"h": self.hid, "c": int(cursor), "me": self.client},  # type: ignore[arg-type]
            )
            self.out.deletes.extend({"table": n, "id": i} for n, i in gone.all())
        household = await self.s.get(Household, self.hid)
        if household is not None:
            self.out.household = {f: _dump(getattr(household, f)) for f in HOUSEHOLD_FIELDS}
            self.out.household["updated_at"] = _dump(household.updated_at)


BY_NAME_PARENT = {s.name: s.via[1].__table__.name for s in SPECS if s.via is not None}


def _natural_ok(spec: _Spec, key: tuple[Any, ...]) -> bool:
    if spec.model is Category:
        return key[1] is not None  # a top-level category has no parent
    return all(k is not None for k in key)


def _depth_levels(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    by_id = {str(r.get("id")): r for r in rows if isinstance(r, dict)}
    levels: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in _by_depth([r for r in rows if isinstance(r, dict)]):
        depth, cur, seen = 0, row, set()
        while cur.get("parent_id") and str(cur["parent_id"]) in by_id and id(cur) not in seen:
            seen.add(id(cur))
            cur = by_id[str(cur["parent_id"])]
            depth += 1
        levels[depth].append(row)
    bad = [r for r in rows if not isinstance(r, dict)]
    return [levels[d] for d in sorted(levels)] + ([bad] if bad else [])


def _key(value: Any) -> Any:
    """Natural-key values compared as text, so a device's strings match the server's types."""
    return None if value is None else str(_dump(value))


async def sync(
    session: AsyncSession,
    *,
    household_id: uuid.UUID,
    member_id: uuid.UUID,
    user_id: uuid.UUID,
    role: Role,
    client: str,
    cursor: str | None,
    changes: dict[str, list[dict[str, Any]]],
    deletes: list[dict[str, Any]],
    household: dict[str, Any] | None,
    pull: bool,
) -> Result:
    """Apply a device's changes, then (if asked) read what changed here. Commits."""
    sent = sum(len(v) for v in changes.values()) + len(deletes)
    if sent > MAX_ROWS:
        raise SyncError(f"Send at most {MAX_ROWS} changes at a time", 413)
    if (sent or household) and role == Role.viewer:
        raise SyncError("Viewers can't change the books", 403)
    run = _Session(session, household_id, member_id, user_id, client)
    await session.execute(text("SELECT set_config('cn.sync_client', :c, true)"), {"c": client})
    await session.execute(text("SELECT set_config('cn.sync_apply', 'on', true)"))
    try:
        if sent:
            await run.load_owned()
            await run.push(changes)
            await run.push_deletes(deletes)
        if household:
            await run.push_household(household, role)
        if pull:
            await run.pull(cursor)
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    return run.out
