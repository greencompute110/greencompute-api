"""A deposit watcher left behind further than its RPC serves logs for (Base's
public RPC keeps ~5.5 hours) used to retry the same range forever: the Base
watcher sat stuck for five days after the 2026-10-14 outage. It now skips the
range only when the deposit addresses provably received nothing in it, and
the billing monitor reports a watcher that has stopped moving."""
import urllib.error
from datetime import UTC, datetime, timedelta

import pytest
from greencompute_gateway.application import billing_monitor as mon
from greencompute_gateway.infrastructure import deposit_watcher as w
from greencompute_gateway.infrastructure.billing_repository import BillingRepository
from greencompute_persistence import session_scope
from greencompute_persistence.orm import KeyValueStateORM

ADDR = "0x" + "ab" * 20
HEAD = 1_000_000


@pytest.fixture(autouse=True)
def fresh_failures(monkeypatch):
    monkeypatch.setattr(w, "_primary_failed_at", {})
    monkeypatch.delenv("BASE_FALLBACK_RPC_URL", raising=False)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for name in ("BILLING_DEPOSIT_USDC_BASE", "BILLING_DEPOSIT_USDT_BASE"):
        monkeypatch.setenv(name, ADDR)
    monkeypatch.delenv("BASE_RPC_URL", raising=False)
    return BillingRepository(database_url=f"sqlite:///{tmp_path / 'w.sqlite'}", bootstrap=True)


class Chain:
    """A fake RPC: logs are refused for blocks older than `window`; balances
    and nonces come from `state(block)`."""

    def __init__(self, window=10_000, state=lambda block: (100, 1, 0)):
        self.window, self.state, self.calls = window, state, []

    def __call__(self, url, method, params, timeout=15.0):
        self.calls.append((url, method))
        if method == "eth_blockNumber":
            return hex(HEAD)
        if method == "eth_getLogs":
            if HEAD - int(params[0]["fromBlock"], 16) > self.window:
                raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
            return []
        usdc, usdt, nonce = self.state(int(params[1], 16))
        if method == "eth_getTransactionCount":
            return hex(nonce)
        return hex(usdc if params[0]["to"].lower() == w.TOKENS[3].contract else usdt)


def cursor(repo, value=None):
    if value is not None:
        w._kv_set(repo, "watcher:base:last_block", str(value))
    return int(w._kv_get(repo, "watcher:base:last_block"))


def test_a_stranded_watcher_skips_a_range_where_nothing_arrived(repo, monkeypatch):
    monkeypatch.setattr(w, "_jsonrpc", Chain())
    cursor(repo, HEAD - 200_000)
    w.scan_evm_chain(repo, "base")
    assert cursor(repo) == HEAD - w.CONFIRMATIONS["base"] - w.RESUME_BLOCKS_BEHIND["base"]
    w.scan_evm_chain(repo, "base")  # and then scans normally, inside the window
    assert cursor(repo) == HEAD - w.CONFIRMATIONS["base"] - w.RESUME_BLOCKS_BEHIND["base"] + 1001


def test_it_stays_put_when_a_balance_changed_in_the_range(repo, monkeypatch):
    monkeypatch.setattr(w, "_jsonrpc", Chain(state=lambda block: (100 if block < HEAD - 150_000 else 125, 1, 0)))
    cursor(repo, HEAD - 200_000)
    w.scan_evm_chain(repo, "base")
    assert cursor(repo) == HEAD - 200_000


def test_it_stays_put_when_the_address_sent_anything(repo, monkeypatch):
    monkeypatch.setattr(w, "_jsonrpc", Chain(state=lambda block: (100, 1, 0 if block < HEAD - 150_000 else 1)))
    cursor(repo, HEAD - 200_000)
    w.scan_evm_chain(repo, "base")
    assert cursor(repo) == HEAD - 200_000


def test_it_stays_put_when_old_state_cant_be_read(repo, monkeypatch):
    chain = Chain()
    def no_state(url, method, params, timeout=15.0):
        if method in ("eth_call", "eth_getTransactionCount"):
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)
        return chain(url, method, params, timeout)
    monkeypatch.setattr(w, "_jsonrpc", no_state)
    cursor(repo, HEAD - 200_000)
    w.scan_evm_chain(repo, "base")
    assert cursor(repo) == HEAD - 200_000


def test_a_brief_failure_is_retried_not_skipped(repo, monkeypatch):
    chain = Chain(window=0)  # every getLogs fails, but we're only a little behind
    monkeypatch.setattr(w, "_jsonrpc", chain)
    cursor(repo, HEAD - 2_000)
    w.scan_evm_chain(repo, "base")
    assert cursor(repo) == HEAD - 2_000
    assert ("https://mainnet.base.org", "eth_call") not in chain.calls


def test_base_catches_up_a_thousand_blocks_a_tick(repo, monkeypatch):
    monkeypatch.setattr(w, "_jsonrpc", Chain())
    cursor(repo, HEAD - 8_000)
    w.scan_evm_chain(repo, "base")
    assert cursor(repo) == HEAD - 8_000 + 1001


def test_old_state_falls_back_to_an_archive_endpoint(repo, monkeypatch):
    chain = Chain()
    def publicnode_has_no_history(url, method, params, timeout=15.0):
        if method in ("eth_call", "eth_getTransactionCount") and "publicnode" in url:
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
        return chain(url, method, params, timeout)
    monkeypatch.setattr(w, "_jsonrpc", publicnode_has_no_history)
    cursor(repo, HEAD - 200_000)
    w.scan_evm_chain(repo, "base")
    assert cursor(repo) > HEAD - 200_000
    assert ("https://mainnet.base.org", "eth_getTransactionCount") in chain.calls


# --- the billing monitor --------------------------------------------------------

NOW = datetime(2026, 10, 19, 15, 0, tzinfo=UTC)


def _cursor_moved(repo, chain, ago):
    with session_scope(repo.session_factory) as s:
        s.add(KeyValueStateORM(key=f"watcher:{chain}:last_block", value="52609788", updated_at=NOW - ago))


def test_the_monitor_reports_a_watcher_that_stopped(repo, monkeypatch):
    monkeypatch.delenv("GREENCOMPUTE_DEPOSIT_WATCHER_ENABLED", raising=False)
    _cursor_moved(repo, "base", timedelta(days=5))
    _cursor_moved(repo, "eth", timedelta(minutes=2))
    with session_scope(repo.session_factory) as s:
        found = mon.stalled_watchers(s, NOW)
    assert [f.check for f in found] == ["stalled_watcher"]
    assert "Base" in found[0].text and "120.0 h" in found[0].text


def test_no_report_when_the_watcher_is_switched_off(repo, monkeypatch):
    monkeypatch.setenv("GREENCOMPUTE_DEPOSIT_WATCHER_ENABLED", "false")
    _cursor_moved(repo, "base", timedelta(days=5))
    with session_scope(repo.session_factory) as s:
        assert mon.stalled_watchers(s, NOW) == []


# --- a keyed backup RPC (Alchemy) that keeps history but serves 10 blocks a call ---

BACKUP = "https://base-mainnet.example/v2/key"
PAID_AT = HEAD - 199_950  # a payment inside the stranded range


class Backup(Chain):
    """The main RPC (publicnode) refuses old logs; the backup serves any
    block, at most 10 per eth_getLogs, with both tokens in one call."""

    def __init__(self, primary_window=10_000, **kw):
        super().__init__(window=primary_window, **kw)
        self.backup_calls = []

    def __call__(self, url, method, params, timeout=15.0):
        if url != BACKUP or method != "eth_getLogs":
            return super().__call__(url, method, params, timeout)
        first, last = int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16)
        assert last - first + 1 <= 10, "backup allows 10 blocks per call"
        assert isinstance(params[0]["address"], list) and len(params[0]["address"]) == 2
        self.backup_calls.append((first, last))
        if first <= PAID_AT <= last:
            return [{"transactionHash": "0xpaid", "logIndex": "0x0", "blockNumber": hex(PAID_AT), "data": hex(25_000_000),
                     "address": w.TOKENS[3].contract, "topics": [w.ERC20_TRANSFER_TOPIC, "0x" + "0" * 24 + "cd" * 20, params[0]["topics"][2][0]]}]
        return []


@pytest.fixture
def backup(repo, monkeypatch):
    monkeypatch.setenv("BASE_FALLBACK_RPC_URL", BACKUP)
    seen = []
    monkeypatch.setattr(w, "_service", lambda repo: type("S", (), {"resolve_deposit": staticmethod(lambda d: seen.append(d) or "unmatched")})())
    return seen


def test_a_stranded_range_is_read_from_the_backup_instead_of_skipped(repo, backup, monkeypatch):
    chain = Backup()
    monkeypatch.setattr(w, "_jsonrpc", chain)
    cursor(repo, HEAD - 200_000)
    w.scan_evm_chain(repo, "base")
    assert cursor(repo) == HEAD - 200_000 + w.FALLBACK_BLOCKS_PER_TICK
    assert len(chain.backup_calls) == w.FALLBACK_BLOCKS_PER_TICK // 10
    assert [(d["currency"], d["amount_raw"], d["block_number"]) for d in backup] == [("usdc-base", 25_000_000, PAID_AT)]


def test_after_the_main_rpc_fails_the_backup_takes_over(repo, backup, monkeypatch):
    chain = Backup(primary_window=0)  # the main RPC refuses even recent logs
    monkeypatch.setattr(w, "_jsonrpc", chain)
    cursor(repo, HEAD - 2_000)
    w.scan_evm_chain(repo, "base")  # fails on the main RPC, no skip this close to the head
    assert cursor(repo) == HEAD - 2_000 and not chain.backup_calls
    w.scan_evm_chain(repo, "base")  # the next tick reads from the backup
    assert cursor(repo) == HEAD - 2_000 + w.FALLBACK_BLOCKS_PER_TICK


def test_when_the_backup_fails_too_the_provable_skip_still_applies(repo, backup, monkeypatch):
    chain = Backup()
    def backup_down(url, method, params, timeout=15.0):
        if url == BACKUP and method == "eth_getLogs":
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)
        return chain(url, method, params, timeout)
    monkeypatch.setattr(w, "_jsonrpc", backup_down)
    cursor(repo, HEAD - 200_000)
    w.scan_evm_chain(repo, "base")
    assert cursor(repo) == HEAD - w.CONFIRMATIONS["base"] - w.RESUME_BLOCKS_BEHIND["base"]
