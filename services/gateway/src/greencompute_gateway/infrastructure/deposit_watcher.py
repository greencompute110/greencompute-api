"""On-chain deposit watcher — auto-confirms crypto top-ups.

Polls supported chains on a fixed interval and matches incoming transfers
to pending `crypto_invoices`. For each match it calls the existing
`BillingRepository.confirm_and_credit_invoice` (which is idempotent and
atomic), so a successful match crediting the user is safe even on
overlapping ticks or restarts.

## Supported chains

- **TAO** (Bittensor mainnet) — Substrate `Balances.Transfer` events.
- **USDT-ETH, USDC-ETH** — ERC-20 `Transfer` logs via JSON-RPC `eth_getLogs`.
- **USDT-BASE, USDC-BASE** — same, against Base mainnet RPC.

## How it disambiguates deposits

Deposit addresses are shared across users (we don't run an HD wallet),
so we can't tell who sent a given deposit from the tx alone. We match
each transfer to a pending invoice by AMOUNT:

  invoice.amount_crypto <= deposit_amount <= invoice.amount_crypto * 1.05

The 5% upper bound lets users overpay slightly (e.g. send 0.06 TAO for
a 0.057799 TAO invoice) without manual review. If a single tx could
match multiple pending invoices, the *earliest-created* invoice wins —
first in, first out.

## State

We persist `last_scanned_block` per chain in `key_value_state` so each
tick only scans new blocks. After a long downtime the watcher catches
up gradually (bounded to MAX_BLOCKS_PER_TICK per chain per tick to keep
RPC bills sane).

## Env vars

- `GREENCOMPUTE_DEPOSIT_WATCHER_ENABLED` (default "true") — kill switch
- `GREENCOMPUTE_DEPOSIT_WATCHER_INTERVAL_SECONDS` (default 60)
- `ETH_RPC_URL` (default https://eth.llamarpc.com — public, can be flaky)
- `BASE_RPC_URL` (default https://mainnet.base.org)
- `GREENCOMPUTE_SUBTENSOR_URL` (already used by price feed)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
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
# so 200 blocks ≈ 40min of catch-up per tick. Base ≈ 2s/block so 200 ≈ 7min.
# TAO ≈ 12s/block, same as ETH.
MAX_BLOCKS_PER_TICK = 200

# Reorg-safety: only credit transfers buried at least this many blocks deep,
# so a transfer that later gets reorged out never credits funds we don't
# actually hold. Keyed by chain_id.
CONFIRMATIONS = {
    "eth": 12,
    "base": 20,   # Base is fast but reorgs deeper in block count terms
}

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


def _chain_rpc_url(chain_id: str) -> str:
    # publicnode is keyless and reliable; override via env with an Alchemy /
    # Infura URL for production-grade rate limits.
    if chain_id == "eth":
        return (os.environ.get("ETH_RPC_URL", "").strip()
                or "https://ethereum-rpc.publicnode.com")
    if chain_id == "base":
        return (os.environ.get("BASE_RPC_URL", "").strip()
                or "https://base-rpc.publicnode.com")
    raise ValueError(f"unknown EVM chain_id: {chain_id}")


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


def _pending_invoices_for(
    repo: BillingRepository, currency: str, address: str
) -> list[CryptoInvoiceORM]:
    """Pending invoices for this currency+address, oldest first."""
    addr_lower = (address or "").strip().lower()
    if not addr_lower:
        return []
    with session_scope(repo.session_factory) as session:
        rows = list(
            session.scalars(
                select(CryptoInvoiceORM)
                .where(
                    CryptoInvoiceORM.currency == currency,
                    CryptoInvoiceORM.status == "pending",
                )
                .order_by(CryptoInvoiceORM.created_at.asc())
            ).all()
        )
    # Filter to the right deposit address (might've rotated over time).
    return [
        r for r in rows
        if (r.deposit_address or "").strip().lower() == addr_lower
    ]


def _amount_matches(deposit_amount: float, invoice_amount: float) -> bool:
    """Accept the invoice amount exactly OR up to 5% overpayment.
    Tighter than 'send anything' so an unrelated transfer to the same
    address doesn't accidentally credit someone."""
    if invoice_amount <= 0:
        return False
    return deposit_amount >= invoice_amount and deposit_amount <= invoice_amount * 1.05


def _try_credit(
    repo: BillingRepository,
    invoices: list[CryptoInvoiceORM],
    deposit_amount: float,
    tx_hash: str,
    deposit_ref: str,
) -> tuple[CryptoInvoiceORM, dict] | None:
    """Find the first pending invoice that fits this deposit amount and
    confirm-credit it. `deposit_ref` is the globally-unique on-chain
    transfer key — passed to confirm_and_credit_invoice so a single
    transfer can credit at most one invoice EVER (the column is UNIQUE).

    Returns the matched (invoice, result) pair, or None if no invoice
    matched the amount. If a match was found but the transfer was already
    consumed (duplicate_deposit), we stop — the transfer is spent."""
    for inv in invoices:
        if _amount_matches(deposit_amount, float(inv.amount_crypto)):
            description = (
                f"Crypto deposit {inv.currency.upper()} "
                f"${inv.amount_usd:.2f} (+{int((inv.bonus_pct or 0) * 100)}% bonus)"
            )
            result = repo.confirm_and_credit_invoice(
                invoice_id=inv.invoice_id,
                tx_hash=tx_hash,
                description=description,
                deposit_ref=deposit_ref,
            )
            if result is None:
                continue
            # The transfer was already consumed by another invoice — it's
            # spent, don't keep trying to match it to further invoices.
            if result.get("duplicate_deposit"):
                return inv, result
            invoices.remove(inv)
            return inv, result
    return None


# ---------------------------------------------------------------------------
# EVM scanner (USDT/USDC on Ethereum + Base)
# ---------------------------------------------------------------------------


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


def scan_evm_chain(repo: BillingRepository, chain_id: str) -> int:
    """Scan one EVM chain for new ERC-20 deposits. Returns the number of
    invoices credited this pass."""
    tokens = [t for t in TOKENS if t.chain_id == chain_id]
    if not tokens:
        return 0
    state_key = f"watcher:{chain_id}:last_block"
    rpc_url = _chain_rpc_url(chain_id)

    # Group pending invoices by deposit address (across all tokens on
    # this chain). For each token: pull recent logs filtered by `to`.
    credited = 0
    try:
        head = _hex_to_int(_jsonrpc(rpc_url, "eth_blockNumber", []))
    except Exception as exc:
        log.warning("watcher %s: failed to fetch head block: %s", chain_id, exc)
        return 0

    # Only scan blocks that are deep enough to be reorg-safe. Crediting on a
    # transfer that later gets reorged out would credit funds we never
    # received. ETH finalizes slower than Base, so use a bigger buffer.
    confirmations = CONFIRMATIONS.get(chain_id, 12)
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
    to_block = min(safe_head, from_block + MAX_BLOCKS_PER_TICK)
    if to_block < from_block:
        return 0

    for token in tokens:
        # Collect deposit-address topics. The address is the SAME for all
        # pending invoices of this currency (we only ever set one global
        # BILLING_DEPOSIT_<X> at a time), so pull addresses from the
        # pending invoices themselves and dedupe.
        with session_scope(repo.session_factory) as session:
            addrs = {
                (r.deposit_address or "").lower()
                for r in session.scalars(
                    select(CryptoInvoiceORM).where(
                        CryptoInvoiceORM.currency == token.currency,
                        CryptoInvoiceORM.status == "pending",
                    )
                ).all()
                if r.deposit_address
            }
        if not addrs:
            continue

        for addr in addrs:
            try:
                to_topic = _topic_from_addr(addr)
            except ValueError:
                log.warning(
                    "watcher %s: skipping invalid deposit address %r", chain_id, addr
                )
                continue
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
                continue

            invoices = _pending_invoices_for(repo, token.currency, addr)
            if not invoices:
                continue

            for entry in logs:
                amount_raw = _hex_to_int(entry.get("data", "0x0"))
                deposit_amount = amount_raw / (10 ** token.decimals)
                tx_hash = entry.get("transactionHash", "")
                if not tx_hash:
                    continue
                # Globally-unique transfer key. One ERC-20 tx can carry
                # several Transfer logs, so log_index is required for
                # uniqueness within a tx. currency already encodes the chain
                # (usdt-eth vs usdt-base), so this never collides cross-chain.
                log_index = entry.get("logIndex", "0x0")
                deposit_ref = f"{token.currency}:{tx_hash}:{_hex_to_int(log_index)}"
                matched = _try_credit(
                    repo, invoices, deposit_amount, tx_hash, deposit_ref
                )
                if matched is None:
                    continue
                inv, result = matched
                log.info(
                    "watcher %s/%s: invoice %s ref=%s amount=%s -> %s",
                    chain_id, token.currency,
                    inv.invoice_id[:8], deposit_ref[:40], deposit_amount,
                    "credited" if result.get("credited")
                    else "duplicate" if result.get("duplicate_deposit")
                    else "already_confirmed",
                )
                if result.get("credited"):
                    credited += 1

    # Persist the NEXT block to scan (to_block + 1) — non-overlapping with
    # the range we just covered.
    _kv_set(repo, state_key, str(to_block + 1))
    return credited


# ---------------------------------------------------------------------------
# TAO scanner (Substrate / Bittensor mainnet)
# ---------------------------------------------------------------------------


def scan_tao(repo: BillingRepository) -> int:
    """Scan Bittensor mainnet for Balances.Transfer events to our TAO
    deposit address. Returns invoices credited."""
    try:
        from substrateinterface import SubstrateInterface
    except ImportError:
        log.warning("watcher tao: substrate-interface not installed — skipping")
        return 0

    rpc_url = (
        os.environ.get("GREENCOMPUTE_SUBTENSOR_URL")
        or os.environ.get("SUBTENSOR_URL")
        or "wss://entrypoint-finney.opentensor.ai:443/"
    )

    # Pull pending TAO invoices first — bail early if there's nothing to
    # do, so we don't churn the chain RPC.
    with session_scope(repo.session_factory) as session:
        addrs = {
            (r.deposit_address or "").strip()
            for r in session.scalars(
                select(CryptoInvoiceORM).where(
                    CryptoInvoiceORM.currency == "tao",
                    CryptoInvoiceORM.status == "pending",
                )
            ).all()
            if r.deposit_address
        }
    if not addrs:
        return 0

    state_key = "watcher:tao:last_block"
    last_seen_str = _kv_get(repo, state_key)
    substrate: SubstrateInterface | None = None
    credited = 0
    try:
        substrate = SubstrateInterface(
            url=rpc_url, ss58_format=42,
            type_registry_preset="substrate-node-template",
        )
        head_hash = substrate.get_chain_head()
        head_num = substrate.get_block_number(head_hash)
        # Small reorg-safety buffer (Bittensor finalizes fast).
        safe_head = head_num - 10
        if safe_head <= 0:
            return 0
        from_block = int(last_seen_str) if last_seen_str else max(0, safe_head - 1800)  # ~6h
        to_block = min(safe_head, from_block + MAX_BLOCKS_PER_TICK)
        if to_block <= from_block:
            return 0

        for block_num in range(from_block + 1, to_block + 1):
            try:
                block_hash = substrate.get_block_hash(block_num)
                events = substrate.get_events(block_hash) or []
            except Exception as exc:
                log.warning(
                    "watcher tao: get_events block %d failed: %s", block_num, exc
                )
                continue

            for event_idx, ev in enumerate(events):
                ev_obj = getattr(ev, "value", ev)
                if not isinstance(ev_obj, dict):
                    continue
                module = (ev_obj.get("module_id") or "").lower()
                event = (ev_obj.get("event_id") or "").lower()
                if module != "balances" or event != "transfer":
                    continue
                attrs = ev_obj.get("attributes") or {}
                # `attributes` can be a dict {from,to,amount} or a list.
                # Normalize.
                if isinstance(attrs, list):
                    if len(attrs) < 3:
                        continue
                    dest = str(attrs[1])
                    amount_raw = int(attrs[2])
                else:
                    dest = str(attrs.get("to") or attrs.get("dest") or "")
                    amount_raw = int(attrs.get("amount") or 0)
                if dest not in addrs:
                    continue
                deposit_amount = amount_raw / 1e9  # TAO has 9 decimals
                # Human-readable hash if available (for the invoice record);
                # the dedup key is the block+event coordinate, which is
                # globally unique and stable regardless of whether the
                # extrinsic hash decodes.
                tx_hash = str(
                    ev_obj.get("extrinsic_hash")
                    or ev_obj.get("extrinsic_idx")
                    or f"block-{block_num}"
                )
                deposit_ref = f"tao:{block_num}:{event_idx}"

                invoices = _pending_invoices_for(repo, "tao", dest)
                if not invoices:
                    continue
                matched = _try_credit(
                    repo, invoices, deposit_amount, tx_hash, deposit_ref
                )
                if matched is None:
                    continue
                inv, result = matched
                log.info(
                    "watcher tao: invoice %s ref=%s amount=%s TAO -> %s",
                    inv.invoice_id[:8], deposit_ref, deposit_amount,
                    "credited" if result.get("credited")
                    else "duplicate" if result.get("duplicate_deposit")
                    else "already_confirmed",
                )
                if result.get("credited"):
                    credited += 1

        _kv_set(repo, state_key, str(to_block))
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


async def deposit_watcher_loop() -> None:
    """Background coroutine. Spawned at gateway startup; tick every N
    seconds (default 60). Logs all credit events; errors don't kill the
    loop — they just get reported and we try again next tick."""
    if not _bool_env("GREENCOMPUTE_DEPOSIT_WATCHER_ENABLED", True):
        log.info("deposit watcher disabled via env")
        return

    interval = max(10, int(
        os.environ.get("GREENCOMPUTE_DEPOSIT_WATCHER_INTERVAL_SECONDS", "60")
    ))
    log.info("deposit watcher starting (interval=%ds)", interval)

    while True:
        try:
            repo = get_billing_service().repo
            for chain_id in ("eth", "base"):
                try:
                    n = await asyncio.to_thread(scan_evm_chain, repo, chain_id)
                    if n:
                        log.info("watcher %s: credited %d invoice(s) this tick", chain_id, n)
                except Exception:
                    log.exception("watcher %s tick failed", chain_id)
            try:
                n = await asyncio.to_thread(scan_tao, repo)
                if n:
                    log.info("watcher tao: credited %d invoice(s) this tick", n)
            except Exception:
                log.exception("watcher tao tick failed")
        except Exception:
            log.exception("deposit watcher loop iteration failed")
        await asyncio.sleep(interval)
