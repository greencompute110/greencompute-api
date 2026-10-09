"""TAO / alpha deposit scanners must reach a new payment while its invoice is
still open, however long the scanner sat idle before it.

The Bittensor scanners only run while an invoice is pending, so their stored
cursor went months stale on prod. Replaying from it at 200 blocks a tick took
days; the invoice expired first and the payment was never credited.
"""
import sys
import types
from datetime import UTC, datetime, timedelta

from greencompute_persistence import session_scope
from greencompute_persistence.orm import CryptoInvoiceORM, UserORM
from greencompute_protocol import CryptoInvoice
from greencompute_gateway.infrastructure import deposit_watcher as dw
from greencompute_gateway.infrastructure.billing_repository import BillingRepository

ADDR = "5DepositColdkeyAaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
HEAD = 9_246_028
STALE_CURSOR = HEAD - 800_000  # ~110 days behind, as found on prod


class FakeSubstrate:
    """Just enough of substrateinterface.SubstrateInterface for the scanners."""

    events: dict[int, list] = {}
    requested: list[int] = []

    def __init__(self, **_kwargs):
        pass

    def get_chain_head(self):
        return "head"

    def get_block_number(self, _hash):
        return HEAD

    def get_block_hash(self, n):
        FakeSubstrate.requested.append(n)
        return n

    def get_events(self, block_hash):
        return FakeSubstrate.events.get(block_hash, [])

    def close(self):
        pass


def _setup(monkeypatch, currency: str, amount: float, events: dict[int, list]):
    monkeypatch.setitem(
        sys.modules, "substrateinterface",
        types.SimpleNamespace(SubstrateInterface=FakeSubstrate),
    )
    monkeypatch.setenv("GREENCOMPUTE_BITTENSOR_NETUID", "110")
    FakeSubstrate.events = events
    FakeSubstrate.requested = []

    repo = BillingRepository(database_url="sqlite:///:memory:", bootstrap=True)
    with session_scope(repo.session_factory) as s:
        s.add(UserORM(user_id="u1", username="u", email="u@x.io", balance_credits=0))
    inv = CryptoInvoice(
        user_id="u1", currency=currency, amount_crypto=amount, amount_usd=5.0,
        bonus_pct=0.0, total_credits=500, deposit_address=ADDR,
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    repo.create_crypto_invoice(inv)
    # The invoice was opened two minutes ago.
    with session_scope(repo.session_factory) as s:
        s.get(CryptoInvoiceORM, inv.invoice_id).created_at = datetime.now(UTC) - timedelta(minutes=2)
    dw._kv_set(repo, f"watcher:{currency}:last_block", str(STALE_CURSOR))
    return repo, inv.invoice_id


def _status(repo, invoice_id):
    with session_scope(repo.session_factory) as s:
        return s.get(CryptoInvoiceORM, invoice_id).status


def test_tao_payment_credits_despite_stale_cursor(monkeypatch):
    paid_at = HEAD - 15  # a few blocks after the invoice, buried past the reorg buffer
    transfer = {
        "module_id": "Balances", "event_id": "Transfer",
        "attributes": {"from": "5Payer", "to": ADDR, "amount": 16_485_000},
        "extrinsic_hash": "0x26ec",
    }
    repo, invoice_id = _setup(monkeypatch, "tao", 0.016485, {paid_at: [transfer]})

    credited = sum(dw.scan_tao(repo) for _ in range(3))

    assert credited == 1
    assert _status(repo, invoice_id) == "confirmed"
    # It never replayed the idle months.
    assert min(FakeSubstrate.requested) > HEAD - 2_000


def test_alpha_payment_credits_despite_stale_cursor(monkeypatch):
    paid_at = HEAD - 12
    # StakeTransferred(origin_coldkey, destination_coldkey, hotkey,
    #                  origin_netuid, destination_netuid, amount_rao)
    transfer = {
        "module_id": "SubtensorModule", "event_id": "StakeTransferred",
        "attributes": ["5Payer", ADDR, "5Hotkey", 110, 110, 985_006_000],
    }
    repo, invoice_id = _setup(monkeypatch, "alpha", 0.985006, {paid_at: [transfer]})

    credited = sum(dw.scan_alpha(repo) for _ in range(3))

    assert credited == 1
    assert _status(repo, invoice_id) == "confirmed"
    assert min(FakeSubstrate.requested) > HEAD - 2_000


def test_scan_start_keeps_a_current_cursor():
    now = datetime.now(UTC)
    # Normal operation: the cursor is past the invoice's block, resume exactly there.
    assert dw._bittensor_scan_start(HEAD - 50, HEAD, now - timedelta(hours=2), now) == HEAD - 50


def test_scan_start_skips_to_just_before_the_oldest_invoice():
    now = datetime.now(UTC)
    opened = now - timedelta(minutes=10)  # 50 blocks ago
    start = dw._bittensor_scan_start(STALE_CURSOR, HEAD, opened, now)
    assert start == HEAD - 50 - dw.INVOICE_LOOKBACK_BLOCKS


def test_scan_start_first_run_and_idle():
    now = datetime.now(UTC)
    # No cursor yet: the old ~6h default still applies when it is later.
    assert dw._bittensor_scan_start(None, HEAD, now - timedelta(days=2), now) == HEAD - 1800
    # Nothing pending: the cursor is left alone.
    assert dw._bittensor_scan_start(STALE_CURSOR, HEAD, None, now) == STALE_CURSOR


def test_pending_lookup_handles_naive_timestamps():
    repo = BillingRepository(database_url="sqlite:///:memory:", bootstrap=True)
    for minutes_ago in (5, 40):
        inv = CryptoInvoice(
            user_id="u", currency="tao", amount_crypto=1.0, amount_usd=1.0,
            bonus_pct=0.0, total_credits=100, deposit_address=ADDR,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
        repo.create_crypto_invoice(inv)
        with session_scope(repo.session_factory) as s:
            s.get(CryptoInvoiceORM, inv.invoice_id).created_at = datetime.now(UTC) - timedelta(minutes=minutes_ago)

    addrs, oldest = dw._pending_bittensor_invoices(repo, "tao")

    assert addrs == {ADDR}
    assert oldest.tzinfo is not None
    assert timedelta(minutes=39) < datetime.now(UTC) - oldest < timedelta(minutes=41)
