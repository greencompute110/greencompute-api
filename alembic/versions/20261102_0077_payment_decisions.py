"""crypto_invoices: why staff closed a review or rejected an invoice

decision_reason is a code from gateway domain/payment_decisions.REASONS,
decision_note the staff member's own words, decided_at when. The customer is
told all three (email, Billing page, API) instead of a bare "closed".

Revision ID: 20261102_0077
Revises: 20261029_0076
Create Date: 2026-11-02
"""
import sqlalchemy as sa
from alembic import op

revision = "20261102_0077"
down_revision = "20261029_0076"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("crypto_invoices", sa.Column("decision_reason", sa.String(length=32), nullable=True))
    op.add_column("crypto_invoices", sa.Column("decision_note", sa.Text(), nullable=True))
    op.add_column("crypto_invoices", sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("crypto_invoices", "decided_at")
    op.drop_column("crypto_invoices", "decision_note")
    op.drop_column("crypto_invoices", "decision_reason")
