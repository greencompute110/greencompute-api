"""Regression tests for the crypto-deposit double-credit exploit.

A single on-chain transfer must credit at most ONE invoice, even when an
attacker has created many same-amount pending invoices to the same deposit
address. The idempotency key is the per-transfer `deposit_ref`, enforced
both by an in-transaction SELECT and a UNIQUE column constraint.

See the security audit (2026-05-22) for the full vector list.
"""
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from greencompute_persistence import session_scope
from greencompute_persistence.orm import LedgerEntryORM, UserORM
from greencompute_protocol import CryptoInvoice
from greencompute_gateway.infrastructure.billing_repository import BillingRepository


def _repo() -> BillingRepository:
    return BillingRepository(database_url="sqlite:///:memory:", bootstrap=True)


def _seed_user(repo: BillingRepository, user_id: str = "u1") -> str:
    with session_scope(repo.session_factory) as s:
        s.add(UserORM(user_id=user_id, username="u", email="u@x.io", balance_credits=0))
    return user_id


def _invoice(repo: BillingRepository, user_id: str, amount: float = 0.057799) -> CryptoInvoice:
    inv = CryptoInvoice(
        user_id=user_id, currency="tao", amount_crypto=amount, amount_usd=20.0,
        bonus_pct=0.10, total_credits=2200, deposit_address="5Addr",
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    repo.create_crypto_invoice(inv)
    return inv


def test_single_transfer_credits_only_one_of_many_same_amount_invoices():
    repo = _repo()
    user = _seed_user(repo)
    invoices = [_invoice(repo, user) for _ in range(3)]

    ref = "tao:5123456:3"  # one real transfer
    results = [
        repo.confirm_and_credit_invoice(
            invoice_id=inv.invoice_id, tx_hash="0xabc",
            description="d", deposit_ref=ref,
        )
        for inv in invoices
    ]

    assert sum(1 for r in results if r and r.get("credited")) == 1
    assert sum(1 for r in results if r and r.get("duplicate_deposit")) == 2
    with session_scope(repo.session_factory) as s:
        assert s.get(UserORM, user).balance_credits == 2200
        assert (
            s.query(LedgerEntryORM).filter(LedgerEntryORM.kind == "topup").count() == 1
        )


def test_distinct_transfers_each_credit():
    repo = _repo()
    user = _seed_user(repo)
    a, b = _invoice(repo, user), _invoice(repo, user)
    repo.confirm_and_credit_invoice(invoice_id=a.invoice_id, tx_hash="0x1", description="d", deposit_ref="tao:1:0")
    repo.confirm_and_credit_invoice(invoice_id=b.invoice_id, tx_hash="0x2", description="d", deposit_ref="tao:2:0")
    with session_scope(repo.session_factory) as s:
        assert s.get(UserORM, user).balance_credits == 4400


def test_replaying_a_consumed_transfer_does_not_credit():
    repo = _repo()
    user = _seed_user(repo)
    a, b = _invoice(repo, user), _invoice(repo, user)
    repo.confirm_and_credit_invoice(invoice_id=a.invoice_id, tx_hash="0x1", description="d", deposit_ref="tao:9:0")
    dup = repo.confirm_and_credit_invoice(invoice_id=b.invoice_id, tx_hash="0x1", description="d", deposit_ref="tao:9:0")
    assert dup and dup.get("duplicate_deposit")
    with session_scope(repo.session_factory) as s:
        assert s.get(UserORM, user).balance_credits == 2200


def test_unique_constraint_enforced_at_db_layer():
    """Backstop for the cross-replica race: even a direct insert bypassing
    the SELECT dedup must be rejected by the UNIQUE(deposit_ref)."""
    repo = _repo()

    def row(eid: str) -> LedgerEntryORM:
        return LedgerEntryORM(
            entry_id=eid, user_id="u", amount_cents=1, balance_after=1,
            kind="topup", reference_id=f"inv-{eid}", deposit_ref="SAME",
            created_at=datetime.now(UTC),
        )

    with session_scope(repo.session_factory) as s:
        s.add(row("e1"))
    with pytest.raises(IntegrityError):
        with session_scope(repo.session_factory) as s:
            s.add(row("e2"))


def test_expiry_sweep_only_expires_stale_pending_past_grace():
    """Auto-expiry: only PENDING invoices whose expires_at is older than the
    grace window become `expired`. A recently-expired pending invoice (inside
    grace) is kept so a late-but-real payment can still credit; confirmed and
    rejected invoices are never touched."""
    from greencompute_persistence.orm import CryptoInvoiceORM

    repo = _repo()
    now = datetime.now(UTC)

    def mk(status: str, expires_delta_min: int) -> str:
        inv = CryptoInvoice(
            user_id="u", currency="tao", amount_crypto=1.0, amount_usd=1.0,
            bonus_pct=0.0, total_credits=100, deposit_address="a",
            expires_at=now + timedelta(minutes=expires_delta_min),
        )
        repo.create_crypto_invoice(inv)
        with session_scope(repo.session_factory) as s:
            s.get(CryptoInvoiceORM, inv.invoice_id).status = status
        return inv.invoice_id

    stale = mk("pending", -180)       # expired 3h ago -> expire
    recent = mk("pending", -20)       # expired 20m ago, within 1h grace -> keep
    future = mk("pending", 30)        # not yet expired -> keep
    confirmed = mk("confirmed", -300) # terminal -> never touch
    rejected = mk("rejected", -300)   # terminal -> never touch

    n = repo.expire_stale_pending_invoices(grace_seconds=3600)
    assert n == 1
    with session_scope(repo.session_factory) as s:
        get = lambda x: s.get(CryptoInvoiceORM, x).status
        assert get(stale) == "expired"
        assert get(recent) == "pending"
        assert get(future) == "pending"
        assert get(confirmed) == "confirmed"
        assert get(rejected) == "rejected"


def test_null_deposit_refs_are_not_unique_constrained():
    """Usage / stripe / manual ledger rows leave deposit_ref NULL and must
    not collide with each other."""
    repo = _repo()
    with session_scope(repo.session_factory) as s:
        for i in range(3):
            s.add(LedgerEntryORM(
                entry_id=f"n{i}", user_id="u", amount_cents=1, balance_after=1,
                kind="usage", deposit_ref=None, created_at=datetime.now(UTC),
            ))
    with session_scope(repo.session_factory) as s:
        assert s.query(LedgerEntryORM).count() == 3
