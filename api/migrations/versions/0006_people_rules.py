"""people an entry is for, learned sorting rules, Kids and Food delivery categories

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-14 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KIDS = (
    "School & tuition",
    "Classes & sports",
    "Toys & fun",
    "Clothes & shoes",
    "Health",
    "Pocket money",
)


def upgrade() -> None:
    op.create_table(
        "person",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "household_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("household.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(40), nullable=False),
        sa.Column("relation", sa.String(24), nullable=False),
        sa.Column(
            "member_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("member.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("color_slot", sa.SmallInteger(), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("household_id", "name", name="uq_person_household_id"),
    )
    op.create_index("ix_person_household_id", "person", ["household_id"])
    op.create_table(
        "category_rule",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "household_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("household.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("match", sa.String(60), nullable=False),
        sa.Column(
            "category_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("category.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "person_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("person.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source", sa.String(24), nullable=False),
        sa.Column("hits", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("household_id", "match", name="uq_category_rule_household_id"),
    )
    op.create_index("ix_category_rule_household_id", "category_rule", ["household_id"])
    for table in ("txn", "recurring_rule"):
        op.add_column(
            table,
            sa.Column(
                "person_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey(
                    "person.id", ondelete="SET NULL", name=f"fk_{table}_person_id_person"
                ),
                nullable=True,
            ),
        )
    op.create_index("ix_txn_person_id", "txn", ["person_id"])

    # Existing households: a person for each member, and the new categories.
    op.execute(
        """
        INSERT INTO person (id, household_id, name, relation, member_id, color_slot, archived)
        SELECT gen_random_uuid(), m.household_id, left(split_part(u.display_name, ' ', 1), 40),
               CASE WHEN m.role = 'owner' THEN 'self' ELSE 'other' END, m.id, 8, false
        FROM member m JOIN user_account u ON u.id = m.user_id
        ON CONFLICT DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO category (id, household_id, parent_id, name, kind, is_fixed, color_slot,
                              archived)
        SELECT gen_random_uuid(), h.id, NULL, 'Kids', 'expense', false, 4, false
        FROM household h
        WHERE NOT EXISTS (SELECT 1 FROM category c WHERE c.household_id = h.id
                          AND c.parent_id IS NULL AND c.name = 'Kids')
        """
    )
    for child in KIDS:
        op.execute(
            sa.text(
                """
                INSERT INTO category (id, household_id, parent_id, name, kind, is_fixed,
                                      color_slot, archived)
                SELECT gen_random_uuid(), p.household_id, p.id, :name, 'expense', false, 4, false
                FROM category p
                WHERE p.parent_id IS NULL AND p.name = 'Kids'
                ON CONFLICT DO NOTHING
                """
            ).bindparams(name=child)
        )
    op.execute(
        """
        INSERT INTO category (id, household_id, parent_id, name, kind, is_fixed, color_slot,
                              archived)
        SELECT gen_random_uuid(), p.household_id, p.id, 'Food delivery', p.kind, false,
               p.color_slot, false
        FROM category p
        WHERE p.parent_id IS NULL AND p.name = 'Lifestyle'
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_index("ix_txn_person_id", table_name="txn")
    op.drop_column("recurring_rule", "person_id")
    op.drop_column("txn", "person_id")
    op.drop_index("ix_category_rule_household_id", table_name="category_rule")
    op.drop_table("category_rule")
    op.drop_index("ix_person_household_id", table_name="person")
    op.drop_table("person")
