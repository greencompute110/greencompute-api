"""ledger_entries: index (user_id, created_at), replacing (user_id)

Spending summaries and monthly statements read one account's rows for a time
range; a busy account writes ~4,700 per-minute rows a day. The composite index
also serves every lookup by user_id alone, so the old single-column index goes
(one less B-tree to maintain on the busiest billing table).

Built CONCURRENTLY so ledger writes (metering, top-ups) never wait on it.

Revision ID: 20261029_0071
Revises: 20261029_0070
Create Date: 2026-10-29
"""
from alembic import op

revision = "20261029_0071"
down_revision = "20261029_0070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_ledger_entries_user_created", "ledger_entries", ["user_id", "created_at"],
            postgresql_concurrently=True, if_not_exists=True,
        )
        op.drop_index("ix_ledger_entries_user_id", table_name="ledger_entries",
                      postgresql_concurrently=True, if_exists=True)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index("ix_ledger_entries_user_id", "ledger_entries", ["user_id"],
                        postgresql_concurrently=True, if_not_exists=True)
        op.drop_index("ix_ledger_entries_user_created", table_name="ledger_entries",
                      postgresql_concurrently=True, if_exists=True)
