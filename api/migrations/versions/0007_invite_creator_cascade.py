"""invites go with the account that created them, so accounts can be deleted

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-14 12:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FK = "fk_invite_created_by_user_account"


def upgrade() -> None:
    op.drop_constraint(FK, "invite", type_="foreignkey")
    op.create_foreign_key(FK, "invite", "user_account", ["created_by"], ["id"], ondelete="CASCADE")


def downgrade() -> None:
    op.drop_constraint(FK, "invite", type_="foreignkey")
    op.create_foreign_key(FK, "invite", "user_account", ["created_by"], ["id"])
