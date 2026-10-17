"""model_catalog_submissions.submitted_by_user_id: who proposed a model

The catalog page showed only the self-typed wallet address (or nothing).
Submissions from the website now record the signed-in account. Additive,
reversible; older rows stay NULL.

Revision ID: 20261017_0067
Revises: 20261017_0066
Create Date: 2026-10-17
"""
import sqlalchemy as sa

from alembic import op

revision = "20261017_0067"
down_revision = "20261017_0066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("model_catalog_submissions", sa.Column("submitted_by_user_id", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("model_catalog_submissions", "submitted_by_user_id")
