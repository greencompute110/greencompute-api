"""model_catalog.image_override — pin a serving image per model

Adds ONE nullable column to `model_catalog`. NULL means "the miner picks the
image", which is every existing row, so this is inert for current traffic.

Why: some models load on exactly one vLLM build — Kimi K3 needs a build that
registers `KimiK3ForConditionalGeneration`, which the stable cu130 tag does not.
Without a per-model pin, serving it would mean moving the whole fleet onto a
nightly.

This is the THIRD time the same trap bit: a field was added to the pydantic
model and written into the workload runtime, but with no column to persist it
the value was silently dropped on save (see also multi_node in 0056 and
max_model_len). A catalog round-trip check is the only reliable way to catch it.

Strictly additive, reversible.

Revision ID: 20260731_0058
Revises: 20260730_0057
Create Date: 2026-07-31
"""
import sqlalchemy as sa
from alembic import op

revision = "20260731_0058"
down_revision = "20260730_0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "model_catalog",
        sa.Column("image_override", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("model_catalog", "image_override")
