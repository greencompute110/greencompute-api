"""model_catalog.extra_engine_args / extra_env — per-model engine tuning

Adds TWO nullable JSON columns to `model_catalog`. NULL means "no tuning",
which is every existing row, so this is inert for current traffic.

Why: Kimi K3 on RTX 5090 (sm_120) needs `--moe-backend marlin` — vLLM's auto
oracle picks DeepGEMM, whose `layout.hpp` has no sm_120 branch and hard-asserts
with "Unknown SF transformation". It also needs raised distributed timeouts and
`--enforce-eager`. Modelling every engine knob as a first-class column would
never keep up, so these two carry the long tail.

ADMIN-ONLY by construction (the catalog is admin-managed). The values become
container argv and env, and the multi-node launcher builds a `sh -c` script, so
the protocol layer rejects shell metacharacters and the launcher shlex-quotes.

This is the FOURTH time the same trap has bitten: a field added to the pydantic
model and written into the workload runtime is silently dropped on save when
there is no column to persist it (see multi_node in 0056, max_model_len, and
image_override in 0058). A catalog round-trip test is the only reliable guard.

Strictly additive, reversible.

Revision ID: 20260801_0059
Revises: 20260731_0058
Create Date: 2026-08-01
"""
import sqlalchemy as sa
from alembic import op

revision = "20260801_0059"
down_revision = "20260731_0058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("model_catalog", sa.Column("extra_engine_args", sa.JSON(), nullable=True))
    op.add_column("model_catalog", sa.Column("extra_env", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("model_catalog", "extra_env")
    op.drop_column("model_catalog", "extra_engine_args")
