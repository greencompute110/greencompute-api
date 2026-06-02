"""backfill deposit_ref for historical Stripe top-ups (data-only)

The Stripe confirm path now stamps every credited top-up with a globally-
unique idempotency key `deposit_ref = "stripe:" || stripe_session_id`, reusing
the existing UNIQUE index `uq_ledger_deposit_ref` (migration 0046) so a re-
delivered `checkout.session.completed` webhook can never double-credit.

Historical top-up ledger rows predate that stamping and carry a NULL
`deposit_ref`, so the UNIQUE guard does NOT yet protect them — a re-delivered
webhook for an OLD paid session would slip past the guard. This migration
backfills `deposit_ref` on those rows so the idempotency key covers all
already-credited Stripe top-ups.

NO schema change: `deposit_ref` and `uq_ledger_deposit_ref` already exist
(migration 0046). This is a pure DATA backfill that:
  - writes `deposit_ref` ONLY where it is currently NULL,
  - touches NO balances and NO other column,
  - is idempotent (re-running is a no-op once the rows are stamped).

COLLISION-SAFETY (must never abort on prod): a historical *double-credit* — the
exact bug this effort closes — can leave TWO `topup` ledger rows sharing one
`reference_id` (== Stripe session_id). Naively stamping both with
`"stripe:" || session_id` would violate `uq_ledger_deposit_ref` and abort the
whole migration. So we stamp AT MOST ONE canonical row per session — the row
with the lexicographically-smallest `entry_id` among the still-NULL topup rows
for that `reference_id` — and only when that exact `deposit_ref` value is not
already present on some other row. Duplicate rows are intentionally left NULL
(they are pre-existing bug data; the going-forward UNIQUE guard prevents NEW
duplicates). This makes the backfill incapable of raising an IntegrityError
regardless of historical data.

Revision ID: 20260602_0049
Revises: 20260529_0047
Create Date: 2026-06-02
"""
from alembic import op


revision = "20260602_0049"
down_revision = "20260529_0047"
branch_labels = None
depends_on = None


# Postgres supports UPDATE ... FROM; SQLite (used in tests) does not and needs a
# correlated subquery. Both pick exactly ONE canonical NULL topup row per
# reference_id (MIN(entry_id)) and skip if the target ref is already taken.
_BACKFILL_POSTGRES = """
UPDATE ledger_entries AS le
SET deposit_ref = 'stripe:' || s.stripe_session_id
FROM stripe_sessions AS s
WHERE le.reference_id = s.session_id
  AND le.kind = 'topup'
  AND le.deposit_ref IS NULL
  AND s.stripe_session_id <> ''
  AND le.entry_id = (
        SELECT MIN(le2.entry_id) FROM ledger_entries le2
        WHERE le2.reference_id = le.reference_id
          AND le2.kind = 'topup'
          AND le2.deposit_ref IS NULL
  )
  AND NOT EXISTS (
        SELECT 1 FROM ledger_entries le3
        WHERE le3.deposit_ref = 'stripe:' || s.stripe_session_id
  )
"""

_BACKFILL_SQLITE = """
UPDATE ledger_entries
SET deposit_ref = (
    SELECT 'stripe:' || s.stripe_session_id
    FROM stripe_sessions AS s
    WHERE s.session_id = ledger_entries.reference_id
      AND s.stripe_session_id <> ''
)
WHERE kind = 'topup'
  AND deposit_ref IS NULL
  AND entry_id = (
        SELECT MIN(le2.entry_id) FROM ledger_entries le2
        WHERE le2.reference_id = ledger_entries.reference_id
          AND le2.kind = 'topup'
          AND le2.deposit_ref IS NULL
  )
  AND EXISTS (
        SELECT 1 FROM stripe_sessions AS s
        WHERE s.session_id = ledger_entries.reference_id
          AND s.stripe_session_id <> ''
  )
  AND NOT EXISTS (
        SELECT 1 FROM ledger_entries le3
        WHERE le3.deposit_ref = 'stripe:' || (
            SELECT s.stripe_session_id FROM stripe_sessions s
            WHERE s.session_id = ledger_entries.reference_id
              AND s.stripe_session_id <> ''
        )
  )
"""


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(_BACKFILL_SQLITE)
    else:
        op.execute(_BACKFILL_POSTGRES)


def downgrade() -> None:
    # Data-only reversal: clear ONLY the refs this backfill could have written.
    # Schema is unchanged; crypto/manual deposit refs (other prefixes) and any
    # row that never had a stripe ref are left untouched.
    op.execute(
        "UPDATE ledger_entries SET deposit_ref = NULL "
        "WHERE deposit_ref LIKE 'stripe:%'"
    )
