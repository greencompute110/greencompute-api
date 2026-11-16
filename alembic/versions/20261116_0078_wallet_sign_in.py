"""wallet sign-in, sanctions screening

- wallet_auth_challenges: one-time sign-in messages issued to a wallet; the
  exact text is kept and each is claimed once by verify.
- sanctioned_addresses: crypto addresses from the OFAC SDN list and the UK
  Sanctions List, replaced per source on each successful download.
- users.signup_method ("wallet" for accounts a wallet signature created) and
  users.email_verified_at (set only by the website, after an emailed link).
- api_keys.issued_by_wallet: "<chain>:<address>" on keys wallet sign-in
  issued, so signing in again revokes exactly those.
- verified_wallets.sanctions_hit / sanctions_checked_at: a live check (the
  Chainalysis oracle) found the wallet sanctioned.

Plain add_column / create_table: the same on Postgres and SQLite.

Revision ID: 20261116_0078
Revises: 20261102_0077
Create Date: 2026-11-16
"""
import sqlalchemy as sa
from alembic import op

revision = "20261116_0078"
down_revision = "20261102_0077"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "wallet_auth_challenges",
        sa.Column("nonce", sa.String(length=64), primary_key=True),
        sa.Column("chain", sa.String(length=16), nullable=False),
        sa.Column("address", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False, server_default="sign_in"),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("client_ip", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_wallet_auth_challenges_expires_at", "wallet_auth_challenges", ["expires_at"])

    op.create_table(
        "sanctioned_addresses",
        sa.Column("source", sa.String(length=32), primary_key=True),
        sa.Column("chain", sa.String(length=16), primary_key=True),
        sa.Column("address", sa.String(length=128), primary_key=True),
        sa.Column("label", sa.String(length=255), nullable=True),
        sa.Column("loaded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_sanctioned_addresses_chain_address", "sanctioned_addresses", ["chain", "address"])

    op.add_column("users", sa.Column("signup_method", sa.String(length=16), nullable=True))
    op.add_column("users", sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("api_keys", sa.Column("issued_by_wallet", sa.String(length=160), nullable=True))
    op.create_index("ix_api_keys_issued_by_wallet", "api_keys", ["issued_by_wallet"])
    op.add_column("verified_wallets", sa.Column("sanctions_hit", sa.String(length=32), nullable=True))
    op.add_column("verified_wallets", sa.Column("sanctions_checked_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    # batch_alter_table so the drops also work on SQLite (no DROP COLUMN
    # before 3.35); on Postgres it issues plain ALTER TABLE statements.
    with op.batch_alter_table("verified_wallets") as batch:
        batch.drop_column("sanctions_checked_at")
        batch.drop_column("sanctions_hit")
    op.drop_index("ix_api_keys_issued_by_wallet", table_name="api_keys")
    with op.batch_alter_table("api_keys") as batch:
        batch.drop_column("issued_by_wallet")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("email_verified_at")
        batch.drop_column("signup_method")
    op.drop_index("ix_sanctioned_addresses_chain_address", table_name="sanctioned_addresses")
    op.drop_table("sanctioned_addresses")
    op.drop_index("ix_wallet_auth_challenges_expires_at", table_name="wallet_auth_challenges")
    op.drop_table("wallet_auth_challenges")
