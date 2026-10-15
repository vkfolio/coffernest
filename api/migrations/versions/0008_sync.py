"""two-way sync: change tracking on the finance tables, and a log of deleted rows

Every synced row records the transaction that last changed it (``sync_xid``), the device that
sent the change through ``/sync`` (``sync_client``, so its own changes aren't echoed back) and
when it was last edited (``updated_at``, which decides conflicts: newest edit wins). A trigger
keeps all three up to date, so the existing routes, the worker and restores need no changes.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-14 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# table -> (parent table, column) for tables without household_id; columns whose changes alone
# don't count as an edit (the server keeps them itself).
TABLES: dict[str, tuple[tuple[str, str] | None, tuple[str, ...]]] = {
    "household": (None, ()),
    "category": (None, ()),
    "person": (None, ()),
    "category_rule": (None, ("hits",)),
    "account": (None, ()),
    "loan": (None, ()),
    "loan_rate_change": (("loan", "loan_id"), ()),
    "credit_card": (None, ()),
    "card_statement": (("credit_card", "card_id"), ()),
    "card_emi": (("credit_card", "card_id"), ()),
    "recurring_rule": (None, ("materialized_until",)),
    "txn": (None, ()),
    "loan_prepayment": (("loan", "loan_id"), ()),
}

TOUCH = """
CREATE FUNCTION cn_touch() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    quiet text[] := ARRAY['updated_at', 'sync_xid', 'sync_client'] || TG_ARGV;
BEGIN
    IF TG_OP = 'UPDATE' AND (to_jsonb(NEW) - quiet) = (to_jsonb(OLD) - quiet) THEN
        -- Nothing a person would call an edit (or the worker moving its own bookkeeping).
        NEW.updated_at := OLD.updated_at;
        NEW.sync_xid := OLD.sync_xid;
        NEW.sync_client := OLD.sync_client;
        RETURN NEW;
    END IF;
    NEW.sync_xid := pg_current_xact_id();
    NEW.sync_client := nullif(current_setting('cn.sync_client', true), '');
    IF coalesce(current_setting('cn.sync_apply', true), '') <> 'on' OR NEW.updated_at IS NULL THEN
        NEW.updated_at := now();
    END IF;
    RETURN NEW;
END $$;
"""

TOMBSTONE = """
CREATE FUNCTION cn_tombstone() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    hid uuid;
BEGIN
    IF TG_NARGS = 0 THEN
        hid := (to_jsonb(OLD) ->> 'household_id')::uuid;
    ELSE
        EXECUTE format('SELECT household_id FROM %I WHERE id = $1', TG_ARGV[0])
            INTO hid USING (to_jsonb(OLD) ->> TG_ARGV[1])::uuid;
    END IF;
    -- Rows going with their household (or a parent already gone) need no record.
    IF hid IS NOT NULL AND EXISTS (SELECT 1 FROM household WHERE id = hid) THEN
        INSERT INTO sync_tombstone (household_id, table_name, row_id, sync_xid, sync_client)
        VALUES (hid, TG_TABLE_NAME, OLD.id, pg_current_xact_id(),
                nullif(current_setting('cn.sync_client', true), ''))
        ON CONFLICT (table_name, row_id) DO UPDATE
            SET sync_xid = EXCLUDED.sync_xid, sync_client = EXCLUDED.sync_client,
                deleted_at = now();
    END IF;
    RETURN OLD;
END $$;
"""

REVIVE = """
CREATE FUNCTION cn_revive() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM sync_tombstone WHERE table_name = TG_TABLE_NAME AND row_id = NEW.id;
    RETURN NEW;
END $$;
"""


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE sync_tombstone (
            table_name varchar(40) NOT NULL,
            row_id uuid NOT NULL,
            household_id uuid NOT NULL REFERENCES household (id) ON DELETE CASCADE,
            sync_xid xid8 NOT NULL,
            sync_client varchar(64),
            deleted_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_sync_tombstone PRIMARY KEY (table_name, row_id)
        )
        """
    )
    op.create_index("ix_sync_tombstone_household", "sync_tombstone", ["household_id", "sync_xid"])
    op.execute(TOUCH)
    op.execute(TOMBSTONE)
    op.execute(REVIVE)
    for table, (parent, quiet) in TABLES.items():
        has_updated = table in ("txn", "category_rule")
        if not has_updated:
            op.add_column(table, sa.Column("updated_at", sa.DateTime(timezone=True)))
        op.execute(f"UPDATE {table} SET updated_at = coalesce(updated_at, created_at)")
        op.execute(f"ALTER TABLE {table} ADD COLUMN sync_xid xid8")
        op.add_column(table, sa.Column("sync_client", sa.String(64)))
        args = ", ".join(f"'{q}'" for q in quiet)
        op.execute(
            f"CREATE TRIGGER cn_touch BEFORE INSERT OR UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION cn_touch({args})"
        )
        if table == "household":
            continue
        op.create_index(f"ix_{table}_sync", table, ["sync_xid"])
        targs = f"'{parent[0]}', '{parent[1]}'" if parent else ""
        op.execute(
            f"CREATE TRIGGER cn_tombstone AFTER DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION cn_tombstone({targs})"
        )
        op.execute(
            f"CREATE TRIGGER cn_revive AFTER INSERT ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION cn_revive()"
        )


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS cn_touch ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS cn_tombstone ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS cn_revive ON {table}")
        if table != "household":
            op.drop_index(f"ix_{table}_sync", table_name=table)
        op.drop_column(table, "sync_client")
        op.drop_column(table, "sync_xid")
        if table not in ("txn", "category_rule"):
            op.drop_column(table, "updated_at")
    op.execute("DROP FUNCTION cn_touch()")
    op.execute("DROP FUNCTION cn_tombstone()")
    op.execute("DROP FUNCTION cn_revive()")
    op.drop_table("sync_tombstone")
