"""workloads: backfill public=false for user-submitted (non-catalog) models

Fixes the visibility leak: the deploy templates defaulted public=True, so every
user-submitted "private" model was listed to all authenticated users (and its
metadata.hf_token leaked alongside it). New models now default private (the
template builders are fixed); this backfills existing rows.

Scope is deliberately narrow: only user-OWNED, non-Flux workloads. Catalog /
Flux-managed workloads have owner_user_id IS NULL (and/or metadata.managed_by =
'flux') and MUST stay public, so they are excluded.

Data-only migration — no schema change.

Revision ID: 20260604_0053
Revises: 20260602_0052
Create Date: 2026-06-04
"""
from alembic import op


revision = "20260604_0053"
down_revision = "20260602_0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE workloads
           SET public = false
         WHERE public = true
           AND owner_user_id IS NOT NULL
           AND (metadata->>'managed_by') IS DISTINCT FROM 'flux'
        """
    )


def downgrade() -> None:
    # Intentionally a no-op: we can't reliably identify exactly which rows were
    # flipped, and re-publishing private user models would re-introduce the
    # leak this migration closes.
    pass
