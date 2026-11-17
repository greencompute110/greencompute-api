"""deployments: metered_until, spell_billed_seconds, spell_billed_mcents

A rental is charged for every started minute it was READY, settled when it
leaves READY (stop, failure, delete, pause). The minute meter keeps charging
as it goes; settlement adds what it hadn't yet: the seconds since its last
tick, the rest of the started minute, and the sub-cent remainder.

- metered_until: up to when the meter has accounted for the current READY
  spell (charged it, or skipped it while the machine was silent). Set when
  the deployment becomes READY, cleared by the settlement: non-NULL is an
  open spell, which is what makes settling it exactly once possible.
- spell_billed_seconds, spell_billed_mcents: the seconds of the current
  spell the meter charged, and what it charged for them (millicents).

Nullable columns with no default: on Postgres catalog-only changes, no table
rewrite, safe on the live database, with a 5 s lock_timeout so it can't
queue behind a long transaction. Rows already READY get a spell on their
first meter tick after the deploy.

Revision ID: 20261117_0081
Revises: 20261117_0080
Create Date: 2026-11-17
"""
import sqlalchemy as sa
from alembic import op

revision = "20261117_0081"
down_revision = "20261117_0080"
branch_labels = None
depends_on = None


def _short_lock_wait() -> None:
    """On Postgres, give up after 5 s rather than queue for the table's lock
    behind a long transaction: a queued ALTER blocks every request that needs
    the table after it. Retry the migration if it times out. SET LOCAL lasts
    until the migration transaction (alembic/env.py runs one) ends."""
    if op.get_context().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")


def upgrade() -> None:
    _short_lock_wait()
    op.add_column("deployments", sa.Column("metered_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("deployments", sa.Column("spell_billed_seconds", sa.Float(), nullable=True))
    op.add_column("deployments", sa.Column("spell_billed_mcents", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    _short_lock_wait()
    # batch_alter_table so the drops also work on SQLite (no DROP COLUMN
    # before 3.35); on Postgres it issues plain ALTER TABLE statements.
    with op.batch_alter_table("deployments") as batch:
        batch.drop_column("spell_billed_mcents")
        batch.drop_column("spell_billed_seconds")
        batch.drop_column("metered_until")
