"""loan history: start date, original tenure and EMI

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-13 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("loan", sa.Column("start_date", sa.Date(), nullable=True))
    op.add_column("loan", sa.Column("original_tenure_months", sa.SmallInteger(), nullable=True))
    op.add_column(
        "loan", sa.Column("original_emi", sa.Numeric(precision=14, scale=2), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("loan", "original_emi")
    op.drop_column("loan", "original_tenure_months")
    op.drop_column("loan", "start_date")
