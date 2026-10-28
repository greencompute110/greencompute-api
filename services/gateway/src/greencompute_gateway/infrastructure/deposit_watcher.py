"""On-chain deposit watcher — credits crypto top-ups automatically.

Polls supported chains on a fixed interval, logs every transfer that arrives
at one of our deposit addresses (`crypto_deposits`) and hands it to
`BillingService.resolve_deposit`, which decides whose it is and credits them.
Crediting is idempotent per transfer (the ledger's UNIQUE deposit_ref), so
overlapping ticks, restarts and re-scans are safe.

## Supported chains

- **TAO** (Bittensor mainnet) — Substrate `Balances.Transfer` events.
- **Alpha** (Bittensor subnet token) — `SubtensorModule.StakeTransferred`
  events to our deposit coldkey on the configured netuid (customers pay by
  `transfer_stake`-ing alpha to the same address as TAO), valued by the
  alpha amount in the `StakeAdded` event of the same extrinsic.
- **USDT-ETH, USDC-ETH** — ERC-20 `Transfer` logs via JSON-RPC `eth_getLogs`.
- **USDT-BASE, USDC-BASE** — same, against Base mainnet RPC.

## Whose payment is it?

Deposit addresses are shared across users (we don't run an HD wallet), so
the transfer itself doesn't say who it's for. In order:

1. **Exact amount.** Every open invoice asks for a unique amount (a few extra
   millionths, worth under a cent), so a transfer of exactly that amount is
   that invoice's.
2. **Verified wallet.** A sender whose owner proved control by signing a
   message gets the value of whatever they sent, invoice or not.
3. **Small overpayment.** Up to 5% over exactly one open invoice's amount.
4. Otherwise the deposit stays **unmatched**: the payer can claim it by
   verifying the sending wallet, or an admin assigns it.

## State

We persist `last_scanned_block` per chain in `key_value_state` so each
tick only scans new blocks. After a long downtime the watcher catches
up gradually (bounded to MAX_BLOCKS_PER_TICK per chain per tick to keep
RPC bills sane).

## Env vars

- `GREENCOMPUTE_DEPOSIT_WATCHER_ENABLED` (default "true") — kill switch
- `GREENCOMPUTE_DEPOSIT_WATCHER_INTERVAL_SECONDS` (default 60)
- `ETH_RPC_URL` (default https://ethereum-rpc.publicnode.com — public, can be flaky)
- `BASE_RPC_URL` (default https://base-rpc.publicnode.com)
- `ETH_FALLBACK_RPC_URL`, `BASE_FALLBACK_RPC_URL` (optional) — a keyed RPC that
  serves old blocks (e.g. Alchemy), read when the main one can't serve a range
- `GREENCOMPUTE_WATCHER_FALLBACK_LOG_RANGE` (default 10, Alchemy's free plan)
- `GREENCOMPUTE_SUBTENSOR_URL` (already used by price feed)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from greencompute_persistence import session_scope
from greencompute_persistence.orm import (
    CryptoInvoiceORM,
    KeyValueStateORM,
)
from greencompute_gateway.application.billing_service import get_billing_service
from greencompute_gateway.infrastructure.billing_repository import BillingRepository

log = logging.getLogger(__name__)


# How many blocks to scan in a single tick, per chain. ETH ≈ 12s/block,
# so 200 blocks ≈ 40min of catch-up per tick. Base ≈ 2s/block, so it gets
# 1000 (≈33min): at 200 a few hours' outage took longer to catch up than the
# default RPC keeps logs for. TAO ≈ 12s/block, same as ETH.
MAX_BLOCKS_PER_TICK = 200
_MAX_BLOCKS_PER_TICK = {"base": 1000}

# The default public RPCs only serve eth_getLogs for recent blocks
# (base-rpc.publicnode.com: the last ~10,000, about 5.5 hours; older ranges
# get HTTP 403). A watcher left further behind than this, e.g. by an outage,
# can never read its next range again: it was stuck for five days after the
# 2026-10-14 outage. Past this lag a failed range may be skipped, but only
# when the deposit addresses provably received nothing in it (see
# _nothing_received).
STRANDED_AFTER_BLOCKS = {"eth": 1500, "base": 9000}
# Where a skip lands: far enough inside the RPC's window to scan normally.
RESUME_BLOCKS_BEHIND = {"eth": 600, "base": 3000}
# Endpoints that answer balance/nonce queries at old blocks (archive state),
# tried after the chain's own RPC and its fallback. mainnet.base.org refuses
# our eth_getLogs (429) but serves historical eth_call.
_ARCHIVE_STATE_URLS = {"base": ["https://mainnet.base.org"]}

# An optional keyed backup RPC per chain (e.g. Alchemy) that keeps all history
# but caps each eth_getLogs at a few blocks: Alchemy's free plan allows 10.
# Read when the main RPC can't serve a range (it's too old, or it's failing),
# FALLBACK_BLOCKS_PER_TICK at a time with every token and address in one call
# per FALLBACK_LOG_RANGE blocks: 30 calls a minute while it's in use.
_FALLBACK_RPC_ENV = {"eth": "ETH_FALLBACK_RPC_URL", "base": "BASE_FALLBACK_RPC_URL"}
FALLBACK_LOG_RANGE = int(os.environ.get("GREENCOMPUTE_WATCHER_FALLBACK_LOG_RANGE", "").strip() or 10)
FALLBACK_BLOCKS_PER_TICK = 300
# After the main RPC fails, stay on the backup this long before trying it again.
FALLBACK_HOLD_SECONDS = 600
_primary_failed_at: dict[str, float] = {}

# Reorg-safety: only credit transfers buried at least this many blocks deep,
# so a transfer that later gets reorged out never credits funds we don't
# actually hold. Keyed by chain_id. These are DEFAULTS — ops can raise the
# buffer toward finality without a code change via
# GREENCOMPUTE_WATCHER_CONFIRMATIONS_<CHAIN> (see _confirmations).
CONFIRMATIONS = {
    "eth": 12,
    "base": 20,   # Base is fast but reorgs deeper in block count terms
}


def _confirmations(chain_id: str) -> int:
    """Effective reorg-confirmation depth for a chain. Env-overridable so ops
    can push the buffer toward finality (e.g. 64+ on ETH) without redeploying.
    Falls back to the hardcoded CONFIRMATIONS default, then 12."""
    env = os.environ.get(f"GREENCOMPUTE_WATCHER_CONFIRMATIONS_{chain_id.upper()}")
    if env and env.strip().isdigit():
        return int(env.strip())
    return CONFIRMATIONS.get(chain_id, 12)

# Transfer(address,address,uint256) event topic (keccak256). Stable
# across all ERC-20 tokens.
ERC20_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


@dataclass(frozen=True)
class TokenConfig:
    """Per-currency on-chain config."""

    currency: str  # matches CryptoInvoice.currency (e.g. "usdt-eth")
    chain_id: str  # "eth" or "base"
    contract: str  # ERC-20 token contract address (lowercased)
    decimals: int  # ERC-20 token decimals (USDT/USDC = 6)
    state_key: str  # key in key_value_state for last_scanned_block


# Hardcoded contract addresses — these don't change.
TOKENS: list[TokenConfig] = [
    TokenConfig(
        currency="usdt-eth",
        chain_id="eth",
        contract="0xdac17f958d2ee523a2206206994597c13d831ec7",
        decimals=6,
        state_key="watcher:eth:last_block",
    ),
    TokenConfig(
        currency="usdc-eth",
        chain_id="eth",
        contract="0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
        decimals=6,
        state_key="watcher:eth:last_block",
    ),
    TokenConfig(
        currency="usdt-base",
        chain_id="base",
        contract="0xfde4c96c8593536e31f229ea8f37b2ada2699bb2",
        decimals=6,
        state_key="watcher:base:last_block",
    ),
    TokenConfig(
        currency="usdc-base",
        chain_id="base",
        contract="0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        decimals=6,
        state_key="watcher:base:last_block",
    ),
]


_DEFAULT_RPC_URLS = {
    "eth": "https://ethereum-rpc.publicnode.com",
    "base": "https://base-rpc.publicnode.com",
}

# Env var that overrides the default public RPC per chain.
_RPC_URL_ENV = {"eth": "ETH_RPC_URL", "base": "BASE_RPC_URL"}


def _chain_rpc_url(chain_id: str) -> str:
    # publicnode is keyless but can be flaky/rate-limited; override via env with
    # an Alchemy / Infura URL for production-grade rate limits.
    env_name = _RPC_URL_ENV.get(chain_id)
    if env_name is None:
        raise ValueError(f"unknown EVM chain_id: {chain_id}")
    return os.environ.get(env_name, "").strip() or _DEFAULT_RPC_URLS[chain_id]


def _using_default_rpc(chain_id: str) -> bool:
    env_name = _RPC_URL_ENV.get(chain_id)
    return not (env_name and (os.environ.get(env_name, "").strip()))


def _kv_get(repo: BillingRepository, key: str) -> str | None:
    with session_scope(repo.session_factory) as session:
        row = session.get(KeyValueStateORM, key)
        return row.value if row else None


def _kv_set(repo: BillingRepository, key: str, value: str) -> None:
    with session_scope(repo.session_factory) as session:
        row = session.get(KeyValueStateORM, key) or KeyValueStateORM(key=key)
        row.value = value
        row.updated_at = datetime.now(UTC)
        session.add(row)


# Outcomes of BillingService.resolve_deposit that put money on an account.
CREDITED = ("invoice", "invoice_overpay", "invoice_underpay", "invoice_late", "wallet")


def _deposit_addresses(repo: BillingRepository, currencies: list[str]) -> dict[str, set[str]]:
    """Addresses to watch per currency: the configured deposit address, plus
    any address a still-open invoice was issued for (in case it changed).

    Watched whether or not an invoice is open: a verified wallet can pay
    without one, and an unmatched payment must be logged to be claimable."""
    from greencompute_gateway.application.billing_service import _deposit_address_for

    watched: dict[str, set[str]] = {c: set() for c in currencies}
    for currency in currencies:
        configured = (_deposit_address_for(currency) or "").strip()
        if configured:
            watched[currency].add(configured)
    with session_scope(repo.session_factory) as session:
        rows = session.execute(
            select(CryptoInvoiceORM.currency, CryptoInvoiceORM.deposit_address).where(
                CryptoInvoiceORM.currency.in_(currencies),
                CryptoInvoiceORM.status == "pending",
            )
        ).all()
    for currency, address in rows:
        if address and address.strip():
            watched[currency].add(address.strip())
    return watched


def _service(repo: BillingRepository):
    from greencompute_gateway.application.billing_service import BillingService

    return BillingService(repo)


def _jsonrpc(url: str, method: str, params: list, timeout: float = 15.0):
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            # Many public RPC gateways (llamarpc, base.org, …) sit behind a
            # WAF that 403s the default "Python-urllib/x.y" User-Agent. Send a
            # browser-like UA so keyless public endpoints don't reject us.
            "User-Agent": "Mozilla/5.0 (compatible; GreenCompute-DepositWatcher/1.0)",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    if "error" in data:
        raise RuntimeError(f"jsonrpc error: {data['error']}")
    return data.get("result")


def _hex_to_int(h: str) -> int:
    return int(h, 16) if isinstance(h, str) and h.startswith("0x") else int(h)


def _topic_from_addr(addr: str) -> str:
    """Encode a 20-byte address as a 32-byte topic value (left-padded)."""
    raw = addr.lower().removeprefix("0x")
    if len(raw) != 40:
        raise ValueError(f"bad address: {addr}")
    return "0x" + "0" * 24 + raw


def _state_at(urls: list[str], block: int, tokens: list[TokenConfig], addresses: set[str]):
    """Each watched address's token balances and nonce at `block`, from the
    first endpoint that answers; None if none does."""
    tag = hex(block)
    for url in urls:
        try:
            out = {}
            for addr in sorted(addresses):
                data = "0x70a08231" + addr.lower().removeprefix("0x").rjust(64, "0")
                balances = tuple(
                    _hex_to_int(_jsonrpc(url, "eth_call", [{"to": t.contract, "data": data}, tag]))
                    for t in tokens
                )
                out[addr] = (balances, _hex_to_int(_jsonrpc(url, "eth_getTransactionCount", [addr, tag])))
            return out
        except Exception as exc:
            log.info("watcher: state at block %d from %s failed: %s", block, url, exc)
    return None


def _fallback_rpc_url(chain_id: str) -> str:
    env_name = _FALLBACK_RPC_ENV.get(chain_id)
    return os.environ.get(env_name, "").strip() if env_name else ""


def _fallback_logs(url: str, tokens, addresses: set[str], first: int, last: int) -> dict[tuple[str, str], list]:
    """Transfers of any watched token to any watched address in first..last,
    from the backup RPC in FALLBACK_LOG_RANGE-block calls, keyed by
    (token contract, recipient). Raises if any call fails."""
    contracts = [t.contract for t in tokens]
    to_topics = [_topic_from_addr(a) for a in sorted(addresses)]
    found: dict[tuple[str, str], list] = {}
    start = first
    while start <= last:
        end = min(last, start + FALLBACK_LOG_RANGE - 1)
        logs = _jsonrpc(
            url,
            "eth_getLogs",
            [{"fromBlock": hex(start), "toBlock": hex(end), "address": contracts,
              "topics": [ERC20_TRANSFER_TOPIC, None, to_topics]}],
            timeout=20.0,
        ) or []
        for entry in logs:
            topics = entry.get("topics") or []
            if len(topics) >= 3:
                key = ((entry.get("address") or "").lower(), "0x" + topics[2][-40:].lower())
                found.setdefault(key, []).append(entry)
        start = end + 1
    return found


def _nothing_received(chain_id: str, rpc_url: str, tokens, addresses, first: int, last: int) -> bool:
    """True when the deposit addresses provably received no watched token in
    blocks first..last: every balance and every nonce is the same before
    `first` and after `last`. A deposit raises a balance; tokens only leave a
    receive-only address in a transaction it sends (which moves its nonce) or
    through an allowance it granted earlier, which these addresses never do."""
    if not addresses:
        return True
    urls = [u for u in (rpc_url, _fallback_rpc_url(chain_id)) if u] + _ARCHIVE_STATE_URLS.get(chain_id, [])
    before = _state_at(urls, first - 1, tokens, addresses)
    after = _state_at(urls, last, tokens, addresses) if before is not None else None
    return before is not None and before == after


def scan_evm_chain(repo: BillingRepository, chain_id: str) -> int:
    """Scan one EVM chain for ERC-20 transfers to our deposit addresses, log
    each one and credit whoever it belongs to. Returns deposits credited."""
    tokens = [t for t in TOKENS if t.chain_id == chain_id]
    if not tokens:
        return 0
    state_key = f"watcher:{chain_id}:last_block"
    rpc_url = _chain_rpc_url(chain_id)
    fallback_url = _fallback_rpc_url(chain_id)

    credited = 0
    head = None
    for url in (rpc_url, fallback_url):
        if not url:
            continue
        try:
            head = _hex_to_int(_jsonrpc(url, "eth_blockNumber", []))
            break
        except Exception as exc:
            log.warning("watcher %s: failed to fetch head block: %s", chain_id, exc)
    if head is None:
        return 0

    # Only scan blocks that are deep enough to be reorg-safe. Crediting on a
    # transfer that later gets reorged out would credit funds we never
    # received. ETH finalizes slower than Base, so use a bigger buffer.
    # Env-overridable (GREENCOMPUTE_WATCHER_CONFIRMATIONS_<CHAIN>) so ops can
    # raise it toward finality without a redeploy.
    confirmations = _confirmations(chain_id)
    safe_head = head - confirmations
    if safe_head <= 0:
        return 0

    last_seen_str = _kv_get(repo, state_key)
    # If first run, start from head - 1 day's worth of blocks (~7200 for ETH,
    # ~43200 for Base). Keeps the first scan from going to genesis. We store
    # last_scanned as "next block to scan", so on subsequent ticks we start
    # exactly there — NO overlap with the previously-scanned range (an
    # inclusive eth_getLogs re-scan of the boundary block was a double-credit
    # trigger before the deposit_ref unique key existed; we close it here too).
    if last_seen_str:
        from_block = int(last_seen_str)
    else:
        from_block = max(0, safe_head - (7200 if chain_id == "eth" else 43200))
    to_block = min(safe_head, from_block + _MAX_BLOCKS_PER_TICK.get(chain_id, MAX_BLOCKS_PER_TICK))
    # The backup RPC reads ranges the main one can't: older than it keeps
    # logs for, or anything while it's been failing lately.
    use_fallback = bool(fallback_url) and (
        head - from_block > STRANDED_AFTER_BLOCKS.get(chain_id, 10**9)
        or time.monotonic() - _primary_failed_at.get(chain_id, float("-inf")) < FALLBACK_HOLD_SECONDS
    )
    if use_fallback:
        to_block = min(to_block, from_block + FALLBACK_BLOCKS_PER_TICK - 1)
    if to_block < from_block:
        return 0

    # Only advance the persisted cursor if EVERY eth_getLogs in this range
    # succeeded and every deposit was handled. A transient RPC failure
    # (timeout/429/5xx — the default public RPC is explicitly flaky) would
    # otherwise let us skip past blocks we never read, permanently losing real
    # deposits. Re-scanning is safe: crediting is idempotent via deposit_ref.
    scan_ok = True
    service = _service(repo)
    watched = _deposit_addresses(repo, [t.currency for t in tokens])
    prefetched = None
    if use_fallback:
        valid = set()
        for a in {a.lower() for t in tokens for a in watched[t.currency]}:
            try:
                _topic_from_addr(a)
                valid.add(a)
            except ValueError:
                pass
        try:
            prefetched = _fallback_logs(fallback_url, tokens, valid, from_block, to_block) if valid else {}
        except Exception as exc:
            log.warning("watcher %s: backup RPC eth_getLogs failed: %s", chain_id, exc)
            scan_ok = False
    for token in tokens if scan_ok else ():
        for addr in sorted({a.lower() for a in watched[token.currency]}):
            try:
                to_topic = _topic_from_addr(addr)
            except ValueError:
                log.warning(
                    "watcher %s: skipping invalid deposit address %r", chain_id, addr
                )
                continue
            if prefetched is not None:
                logs = prefetched.get((token.contract, addr), [])
            else:
                try:
                    logs = _jsonrpc(
                        rpc_url,
                        "eth_getLogs",
                        [
                            {
                                "fromBlock": hex(from_block),
                                "toBlock": hex(to_block),
                                "address": token.contract,
                                "topics": [ERC20_TRANSFER_TOPIC, None, to_topic],
                            }
                        ],
                        timeout=20.0,
                    ) or []
                except Exception as exc:
                    log.warning(
                        "watcher %s/%s: eth_getLogs failed: %s",
                        chain_id, token.currency, exc,
                    )
                    _primary_failed_at[chain_id] = time.monotonic()
                    scan_ok = False
                    continue

            for entry in logs:
                tx_hash = entry.get("transactionHash", "")
                topics = entry.get("topics") or []
                if not tx_hash or len(topics) < 3:
                    continue
                # Globally-unique transfer key. One ERC-20 tx can carry
                # several Transfer logs, so log_index is required for
                # uniqueness within a tx. currency already encodes the chain
                # (usdt-eth vs usdt-base), so this never collides cross-chain.
                log_index = _hex_to_int(entry.get("logIndex", "0x0"))
                deposit = {
                    "deposit_ref": f"{token.currency}:{tx_hash}:{log_index}",
                    "currency": token.currency,
                    "tx_hash": tx_hash,
                    # topics[1] is the sender, left-padded to 32 bytes.
                    "sender": "0x" + topics[1][-40:].lower(),
                    "deposit_address": addr,
                    "amount_raw": _hex_to_int(entry.get("data", "0x0")),
                    "decimals": token.decimals,
                    "block_number": _hex_to_int(entry.get("blockNumber", "0x0")),
                    "usd_price": 1.0,
                }
                try:
                    outcome = service.resolve_deposit(deposit)
                except Exception:
                    log.exception("watcher %s: handling %s failed", chain_id, deposit["deposit_ref"])
                    scan_ok = False
                    continue
                if outcome != "seen":
                    log.info(
                        "watcher %s: %s %s from %s -> %s",
                        chain_id, deposit["amount_raw"] / 10 ** token.decimals,
                        token.currency, deposit["sender"], outcome,
                    )
                if outcome in CREDITED:
                    credited += 1

    # Persist the NEXT block to scan (to_block + 1) — non-overlapping with the
    # range we just covered — ONLY if every eth_getLogs succeeded. On a
    # transient RPC failure we leave the cursor put so this range is retried
    # next tick rather than silently skipped.
    if scan_ok:
        _kv_set(repo, state_key, str(to_block + 1))
        return credited
    resume = safe_head - RESUME_BLOCKS_BEHIND.get(chain_id, 0)
    if (
        head - from_block > STRANDED_AFTER_BLOCKS.get(chain_id, 10**9)
        and resume > from_block
        and _nothing_received(
            chain_id, rpc_url, tokens, {a.lower() for t in tokens for a in watched[t.currency]},
            from_block, resume - 1,
        )
    ):
        log.warning(
            "watcher %s: blocks %d..%d are older than the RPC serves logs for; "
            "the deposit addresses' balances and nonces didn't change across "
            "them, so nothing was received — skipping to %d",
            chain_id, from_block, resume - 1, resume,
        )
        _kv_set(repo, state_key, str(resume))
        return credited
    log.warning(
        "watcher %s: error(s) this tick — NOT advancing cursor past "
        "blocks %d..%d; will retry next tick", chain_id, from_block, to_block,
    )
    return credited


# ---------------------------------------------------------------------------
# Bittensor scanner: TAO transfers and alpha (subnet token) stake transfers
# ---------------------------------------------------------------------------

BITTENSOR_STATE_KEY = "watcher:bittensor:last_block"


def _event_values(ev_obj: dict) -> list | None:
    """An event's attributes as a positional list (they decode as a list for
    subtensor's unnamed tuple events; a dict keeps the same order)."""
    attrs = ev_obj.get("attributes")
    if isinstance(attrs, (list, tuple)):
        return list(attrs)
    if isinstance(attrs, dict):
        return list(attrs.values())
    return None


def _is_event(ev_obj: object, module: str, event: str) -> bool:
    return (
        isinstance(ev_obj, dict)
        and (ev_obj.get("module_id") or "").lower() == module
        and (ev_obj.get("event_id") or "").lower() == event
    )


def _extract_alpha_transfer(
    ev_obj: object, dest_addrs: set[str], want_netuid: int
) -> tuple[str, str] | None:
    """Decode a SubtensorModule.StakeTransferred event into
    (destination_coldkey, origin_coldkey) when it moves stake to one of our
    deposit addresses on the expected subnet — else None.

    Customers top up with alpha by `transfer_stake`-ing it to our deposit
    coldkey, which emits:

        StakeTransferred(origin_coldkey, destination_coldkey, hotkey,
                         origin_netuid, destination_netuid, tao_moved)

    The last field is the TAO value of the stake, NOT the alpha amount; the
    alpha we received is in the StakeAdded event of the same extrinsic (see
    _alpha_deposits). We REQUIRE destination_netuid == the subnet we priced
    the invoice against — alpha of another subnet has a different TAO value,
    so crediting it at our price would be wrong."""
    if not _is_event(ev_obj, "subtensormodule", "staketransferred"):
        return None
    vals = _event_values(ev_obj)
    if not vals or len(vals) < 6:
        return None
    try:
        origin, dest, dest_netuid = str(vals[0]), str(vals[1]), int(vals[4])
    except (TypeError, ValueError):
        return None
    if dest not in dest_addrs:
        return None
    # Wrong-subnet alpha would be mis-valued at our subnet's price — skip it
    # (handled as a support case, never auto-credited at the wrong rate).
    if want_netuid >= 0 and dest_netuid != want_netuid:
        return None
    return dest, origin


def _extract_stake_added(ev_obj: object) -> tuple[str, int, int] | None:
    """Decode SubtensorModule.StakeAdded(coldkey, hotkey, tao_amount,
    alpha_amount, netuid, fee) into (coldkey, netuid, alpha_rao). Alpha has 9
    decimals, like TAO."""
    if not _is_event(ev_obj, "subtensormodule", "stakeadded"):
        return None
    vals = _event_values(ev_obj)
    if not vals or len(vals) < 5:
        return None
    try:
        return str(vals[0]), int(vals[4]), int(vals[3])
    except (TypeError, ValueError):
        return None


def _alpha_deposits(
    events: list, dest_addrs: set[str], want_netuid: int
) -> list[tuple[int, str, int, str, object]]:
    """Alpha transfers to our deposit addresses in one block, as
    (event_idx, our_address, alpha_rao_received, sender, extrinsic_idx).

    The amount comes from the StakeAdded event that the same extrinsic emits
    for our coldkey on our subnet. A transfer with no such companion event
    can't be valued, so it is logged and left for manual review."""
    by_extrinsic: dict[object, list[tuple[str, int, int]]] = {}
    transfers: list[tuple[int, object, str, str]] = []
    for idx, ev in enumerate(events):
        ev_obj = getattr(ev, "value", ev)
        if not isinstance(ev_obj, dict):
            continue
        xidx = ev_obj.get("extrinsic_idx")
        added = _extract_stake_added(ev_obj)
        if added is not None:
            by_extrinsic.setdefault(xidx, []).append(added)
            continue
        moved = _extract_alpha_transfer(ev_obj, dest_addrs, want_netuid)
        if moved is not None:
            transfers.append((idx, xidx, moved[0], moved[1]))

    deposits = []
    for idx, xidx, dest, origin in transfers:
        alpha = next(
            (a for coldkey, netuid, a in by_extrinsic.get(xidx, [])
             if coldkey == dest and netuid == want_netuid),
            None,
        )
        if alpha is None:
            log.warning(
                "watcher alpha: stake transfer to %s (event %d) has no StakeAdded "
                "for our coldkey — can't value it, needs manual review", dest, idx,
            )
            continue
        deposits.append((idx, dest, alpha, origin, xidx))
    return deposits


def _tao_transfers(events: list, dest_addrs: set[str]) -> list[tuple[int, str, int, str, object]]:
    """Balances.Transfer events paying one of our addresses, as
    (event_idx, our_address, amount_rao, sender, extrinsic_idx)."""
    found = []
    for idx, ev in enumerate(events):
        ev_obj = getattr(ev, "value", ev)
        if not _is_event(ev_obj, "balances", "transfer"):
            continue
        attrs = ev_obj.get("attributes") or {}
        # `attributes` can be a dict {from,to,amount} or a list.
        try:
            if isinstance(attrs, dict):
                sender = str(attrs.get("from") or "")
                dest = str(attrs.get("to") or attrs.get("dest") or "")
                amount = int(attrs.get("amount") or 0)
            else:
                sender, dest, amount = str(attrs[0]), str(attrs[1]), int(attrs[2])
        except (IndexError, TypeError, ValueError):
            continue
        if dest in dest_addrs:
            found.append((idx, dest, amount, sender, ev_obj.get("extrinsic_idx")))
    return found


def _extrinsic_hashes(substrate, block_hash) -> dict:
    """extrinsic index -> 0x-hash for one block; empty if it can't be read."""
    try:
        block = substrate.get_block(block_hash=block_hash)
        return {
            i: "0x" + bytes(ex.extrinsic_hash).hex()
            for i, ex in enumerate(block["extrinsics"])
            if getattr(ex, "extrinsic_hash", None)
        }
    except Exception:
        return {}


def scan_bittensor(repo: BillingRepository) -> int:
    """Scan Bittensor mainnet for TAO transfers and alpha stake transfers to
    our deposit coldkeys, log each one and credit whoever it belongs to.
    Returns deposits credited.

    Runs every tick, invoice or not, reading each block once for both
    currencies. (It used to run only while an invoice was open, so its cursor
    sat still between invoices and fell months behind.)"""
    try:
        from substrateinterface import SubstrateInterface
    except ImportError:
        log.warning("watcher bittensor: substrate-interface not installed — skipping")
        return 0

    watched = _deposit_addresses(repo, ["tao", "alpha"])
    if not (watched["tao"] or watched["alpha"]):
        return 0
    rpc_url = (
        os.environ.get("GREENCOMPUTE_SUBTENSOR_URL")
        or os.environ.get("SUBTENSOR_URL")
        or "wss://entrypoint-finney.opentensor.ai:443/"
    )
    # Same subnet the invoice was priced against (price_feed). Single source of
    # truth so the credit netuid can never drift from the pricing netuid.
    from greencompute_gateway.infrastructure.price_feed import _subnet_netuid, get_price
    want_netuid = _subnet_netuid()
    service = _service(repo)

    prices: dict[str, float | None] = {}

    def price(currency: str) -> float | None:
        if currency not in prices:
            try:
                prices[currency] = get_price(currency) or None
            except Exception:
                prices[currency] = None
        return prices[currency]

    last_seen_str = _kv_get(repo, BITTENSOR_STATE_KEY)
    substrate: SubstrateInterface | None = None
    credited = 0
    try:
        substrate = SubstrateInterface(
            url=rpc_url, ss58_format=42,
            type_registry_preset="substrate-node-template",
        )
        head_num = substrate.get_block_number(substrate.get_chain_head())
        safe_head = head_num - 10  # reorg-safety buffer (Bittensor finalizes fast)
        if safe_head <= 0:
            return 0
        from_block = int(last_seen_str) if last_seen_str else max(0, safe_head - 1800)  # ~6h
        to_block = min(safe_head, from_block + MAX_BLOCKS_PER_TICK)
        if to_block <= from_block:
            return 0

        # Advance the cursor only across blocks fully handled. On a failed
        # block we STOP (not skip) so the next tick resumes from it — skipping
        # would silently lose any deposit in that block.
        scanned_through = from_block
        for block_num in range(from_block + 1, to_block + 1):
            try:
                block_hash = substrate.get_block_hash(block_num)
                events = substrate.get_events(block_hash) or []
            except Exception as exc:
                log.warning(
                    "watcher bittensor: get_events block %d failed: %s — stopping; "
                    "will resume here next tick", block_num, exc
                )
                break

            found = [("tao", *t) for t in _tao_transfers(events, watched["tao"])]
            found += [("alpha", *a) for a in _alpha_deposits(events, watched["alpha"], want_netuid)]
            if found:
                hashes = _extrinsic_hashes(substrate, block_hash)
                try:
                    for currency, idx, dest, amount, sender, xidx in found:
                        deposit = {
                            "deposit_ref": f"{currency}:{block_num}:{idx}",
                            "currency": currency,
                            "tx_hash": hashes.get(xidx),
                            "sender": sender,
                            "deposit_address": dest,
                            "amount_raw": amount,
                            "decimals": 9,
                            "block_number": block_num,
                            "usd_price": price(currency),
                        }
                        outcome = service.resolve_deposit(deposit)
                        log.info(
                            "watcher bittensor: %s %s from %s (ref=%s) -> %s",
                            amount / 1e9, currency, sender, deposit["deposit_ref"], outcome,
                        )
                        if outcome in CREDITED:
                            credited += 1
                except Exception:
                    log.exception("watcher bittensor: handling block %d failed — will retry", block_num)
                    break

            # Block fully handled — safe to advance the cursor through it.
            scanned_through = block_num

        _kv_set(repo, BITTENSOR_STATE_KEY, str(scanned_through))
    finally:
        if substrate is not None:
            try:
                substrate.close()
            except Exception:
                pass
    return credited


# ---------------------------------------------------------------------------
# Loop driver
# ---------------------------------------------------------------------------


def _bool_env(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def _expiry_grace_seconds() -> int:
    """Grace window past an invoice's expires_at before we auto-expire it.
    Generous enough to cover on-chain confirmation lag + watcher catch-up so
    a slightly-late-but-real payment still credits before we give up."""
    try:
        hours = float(os.environ.get("GREENCOMPUTE_INVOICE_EXPIRY_GRACE_HOURS", "1"))
    except (TypeError, ValueError):
        hours = 1.0
    return int(max(0.0, hours) * 3600)


async def deposit_watcher_loop() -> None:
    """Background coroutine. Spawned at gateway startup; tick every N
    seconds (default 60).

    Each tick does two things:
      1. (if enabled) scan chains and credit matching deposits
      2. (always) expire stale pending invoices past their grace window

    The expiry sweep runs AFTER the credit scan, so anything creditable this
    tick is credited first and won't be expired. It's pure DB (no RPC), so it
    still runs even when chain-scanning is disabled. Errors don't kill the
    loop — they're logged and retried next tick."""
    scan_enabled = _bool_env("GREENCOMPUTE_DEPOSIT_WATCHER_ENABLED", True)
    interval = max(10, int(
        os.environ.get("GREENCOMPUTE_DEPOSIT_WATCHER_INTERVAL_SECONDS", "60")
    ))
    if scan_enabled:
        log.info("deposit watcher starting (interval=%ds)", interval)
        # Surface the effective reorg buffer + RPC source so flakiness / a
        # too-shallow confirmation depth is visible at startup.
        for chain_id in ("eth", "base"):
            log.info(
                "deposit watcher %s: confirmations=%d rpc=%s",
                chain_id,
                _confirmations(chain_id),
                _chain_rpc_url(chain_id),
            )
            if _using_default_rpc(chain_id):
                log.warning(
                    "deposit watcher %s: using DEFAULT public RPC %s (can be "
                    "flaky/rate-limited) — set %s to an Alchemy/Infura URL in "
                    "production",
                    chain_id,
                    _DEFAULT_RPC_URLS[chain_id],
                    _RPC_URL_ENV[chain_id],
                )
    else:
        log.info(
            "deposit watcher chain-scanning disabled; running invoice-expiry sweep only"
        )

    while True:
        try:
            repo = get_billing_service().repo
            if scan_enabled:
                for chain_id in ("eth", "base"):
                    try:
                        n = await asyncio.to_thread(scan_evm_chain, repo, chain_id)
                        if n:
                            log.info("watcher %s: credited %d deposit(s) this tick", chain_id, n)
                    except Exception:
                        log.exception("watcher %s tick failed", chain_id)
                try:
                    n = await asyncio.to_thread(scan_bittensor, repo)
                    if n:
                        log.info("watcher bittensor: credited %d deposit(s) this tick", n)
                except Exception:
                    log.exception("watcher bittensor tick failed")
            # Expiry sweep — always runs (DB-only).
            try:
                expired = await asyncio.to_thread(
                    repo.expire_stale_pending_invoices, _expiry_grace_seconds()
                )
                if expired:
                    log.info("expired %d stale pending invoice(s)", expired)
            except Exception:
                log.exception("invoice expiry sweep failed")
        except Exception:
            log.exception("deposit watcher loop iteration failed")
        await asyncio.sleep(interval)
