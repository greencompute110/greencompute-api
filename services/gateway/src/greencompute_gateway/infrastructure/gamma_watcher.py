"""The gamma watcher: credits what hotkeys spend at our service hotkeys.

A customer's spend_gamma sends us nothing: the chain adds it to
GammaSpent(netuid, service, hotkey). Each tick (default every 12 s, one
block) reads every total at our service hotkeys at the FINALIZED head, as
the standard says, and hands each one that grew to application/gamma.py,
which credits the growth exactly once.

Dissolving the subnet clears the chain's totals and bumps nothing until the
netuid is registered again, which raises RegisteredSubnetCounter; a total is
stored with the counter it was read under and counts from zero under a newer
one. A known total that vanishes or shrinks under the same counter (the
subnet was dissolved) is never debited; the team is alerted, since spends in
the dissolving block itself would only be in that block's events.

The connection is kept between ticks and replaced, trying each of
GREENCOMPUTE_SUBTENSOR_URL's endpoints in turn, when a call fails.

Env:
- GREENCOMPUTE_GAMMA_WATCHER_ENABLED (default true)
- GREENCOMPUTE_GAMMA_WATCHER_INTERVAL_SECONDS (default 12)
- the settings in domain/gamma.py (service and treasury hotkeys, netuid)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime

from sqlalchemy import select

from greencompute_persistence import session_scope
from greencompute_persistence.orm import GammaSpentTotalORM, KeyValueStateORM
from greencompute_gateway.application import gamma as gamma_app
from greencompute_gateway.domain import gamma
from greencompute_gateway.infrastructure.wallet_signatures import InvalidAddress, normalize_address

log = logging.getLogger(__name__)

# How often the chain's own status (gamma on for the subnet, our service
# listing) is read again, in ticks.
STATUS_EVERY_TICKS = 25
# A spend's own block and transaction are looked up in the events of the
# blocks since the last read, when there are at most this many (12 minutes
# of blocks); after a longer gap the growth is credited from totals alone.
MAX_EVENT_BLOCKS = 60

_conn = {"substrate": None, "url": None}
_ticks = {"n": 0}


def _kv_set(session_factory, key: str, value: str) -> None:
    with session_scope(session_factory) as session:
        row = session.get(KeyValueStateORM, key) or KeyValueStateORM(key=key)
        row.value = value
        row.updated_at = datetime.now(UTC)
        session.add(row)


def _close() -> None:
    substrate = _conn["substrate"]
    _conn["substrate"], _conn["url"] = None, None
    if substrate is not None:
        try:
            substrate.close()
        except Exception:
            pass


def _connect():
    """The kept connection, or a new one to the first endpoint that answers."""
    if _conn["substrate"] is not None:
        return _conn["substrate"]
    from substrateinterface import SubstrateInterface

    from greencompute_gateway.infrastructure.price_feed import subtensor_urls

    failures = []
    for url in subtensor_urls():
        try:
            _conn["substrate"], _conn["url"] = SubstrateInterface(url=url, ss58_format=42), url
            return _conn["substrate"]
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{url.split('//')[-1].split('/')[0]}: {type(exc).__name__}: {exc}")
    raise ConnectionError("no subtensor endpoint answered (" + "; ".join(failures) + ")")


def _value(obj):
    return obj.value if hasattr(obj, "value") else obj


def read_chain(substrate, netuid: int, services: list[str]) -> dict:
    """Everything a tick needs, read at one finalized block."""
    head = substrate.get_chain_finalised_head()
    block = substrate.get_block_number(head)
    counter = int(_value(substrate.query("SubtensorModule", "RegisteredSubnetCounter", [netuid], block_hash=head)) or 0)
    totals: dict[str, dict[str, int]] = {}
    for service in services:
        totals[service] = {
            str(_value(key)): int(_value(value) or 0)
            for key, value in substrate.query_map(
                "SubtensorModule", "GammaSpent", [netuid, service], block_hash=head, page_size=1000)
        }
    return {"head": head, "block": block, "counter": counter, "totals": totals}


def deposit_coldkey(substrate, head, netuid: int, hotkey: str) -> str | None:
    """The coldkey that owns the hotkey's deposit: it says whether the spend
    is our own buyback, and it is screened too. None when the deposit has
    been closed. A failed read raises: whose gamma it is can't be decided
    without it, so the caller retries next tick rather than credit a
    buyback to an account."""
    deposit = _value(substrate.query("SubtensorModule", "GammaDeposits", [netuid, hotkey], block_hash=head))
    return str(deposit.get("coldkey")) if isinstance(deposit, dict) and deposit.get("coldkey") else None


def spend_events(substrate, netuid: int, services: set[str], first: int, last: int) -> dict | None:
    """The GammaSpent events at our services in blocks first..last, oldest
    first, per (service, hotkey): [(block, extrinsic hash, amount)]. They
    give each spend its own block and transaction; totals stay the source
    of what is owed. None when the range is too long or a block can't be
    read: the growth is then credited as one spend, with no transaction."""
    from greencompute_gateway.infrastructure.deposit_watcher import _extrinsic_hashes

    if first > last or last - first + 1 > MAX_EVENT_BLOCKS:
        return None
    found: dict[tuple[str, str], list[tuple[int, str | None, int]]] = {}
    try:
        for number in range(first, last + 1):
            block_hash = substrate.get_block_hash(number)
            if not block_hash:
                return None
            hashes = None
            for ev in substrate.get_events(block_hash) or []:
                value = _value(ev) or {}
                event = value.get("event") or {}
                if event.get("module_id") != "SubtensorModule" or event.get("event_id") != "GammaSpent":
                    continue
                attrs = event.get("attributes") or {}
                if not isinstance(attrs, dict) or int(attrs.get("netuid", -1)) != netuid:
                    continue
                service, hotkey = _canonical(str(attrs.get("service"))), _canonical(str(attrs.get("hotkey")))
                if service not in services or hotkey is None:
                    continue
                if hashes is None:
                    hashes = _extrinsic_hashes(substrate, block_hash)
                found.setdefault((service, hotkey), []).append(
                    (number, hashes.get(value.get("extrinsic_idx")), int(attrs.get("amount") or 0)))
    except Exception as exc:  # noqa: BLE001
        log.warning("gamma watcher: reading spend events %d..%d failed (%s); crediting from totals", first, last, exc)
        return None
    return found


def read_status(substrate, head, netuid: int, service: str) -> dict:
    enabled = bool(_value(substrate.query("SubtensorModule", "SubnetGammaEnabled", [netuid], block_hash=head)))
    try:
        listing = _value(substrate.query("SubtensorModule", "Services", [netuid, service], block_hash=head))
    except Exception:  # noqa: BLE001
        listing = None
    url = listing.get("url") if isinstance(listing, dict) else None
    if isinstance(url, (bytes, bytearray)):
        url = url.decode("utf-8", "replace")
    out = {"gamma_enabled": enabled, "service_hotkey": service, "service_url": url or None}
    try:
        out["owner_coldkey"] = str(_value(substrate.query("SubtensorModule", "SubnetOwner", [netuid], block_hash=head)))
        out["owner_hotkey"] = str(_value(substrate.query("SubtensorModule", "SubnetOwnerHotkey", [netuid],
                                                         block_hash=head)))
    except Exception:  # noqa: BLE001
        pass
    return out


def _check_owner(status: dict, netuid: int) -> None:
    """Alert once if the subnet's owner keys no longer match what we treat
    as the treasury coldkey and the service hotkey: after an owner or
    hotkey swap, buybacks would be credited as payments, or customers would
    be spending at a hotkey nobody reads."""
    coldkey, hotkey = status.get("owner_coldkey"), status.get("owner_hotkey")
    if coldkey and _canonical(coldkey) not in {_canonical(c) for c in gamma.treasury_coldkeys()}:
        gamma_app._alert_once(f"owner-coldkey:{coldkey}", (
            f"Gamma: subnet {netuid}'s owner coldkey is now {coldkey}, which is not a treasury coldkey here "
            f"({', '.join(sorted(gamma.treasury_coldkeys())) or 'none'}). Its buybacks would be credited as "
            "customer payments: set GREENCOMPUTE_GAMMA_TREASURY_COLDKEYS on the gateway."
        ))
    if hotkey and not status.get("service_url") and _canonical(hotkey) not in {
            _canonical(h) for h in gamma.service_hotkeys()}:
        gamma_app._alert_once(f"owner-hotkey:{hotkey}", (
            f"Gamma: subnet {netuid}'s owner hotkey is now {hotkey}, and our service hotkey "
            f"{gamma.service_hotkey()} has no listing on chain. If the service moved to the new hotkey, add it "
            "to GREENCOMPUTE_GAMMA_SERVICE_HOTKEYS (keep the old one listed too)."
        ))


def _canonical(hotkey: str) -> str | None:
    try:
        return normalize_address("bittensor", hotkey)
    except InvalidAddress:
        return None


def scan_gamma(session_factory, billing=None, substrate=None) -> int:
    """One tick. Returns how many spends were credited."""
    netuid = gamma.netuid()
    services = [s for s in (_canonical(h) for h in gamma.service_hotkeys()) if s]
    if not services:
        log.warning("gamma watcher: no valid service hotkey configured")
        return 0
    own = substrate is None
    try:
        substrate = substrate or _connect()
        chain = read_chain(substrate, netuid, services)
    except Exception as exc:  # noqa: BLE001
        log.warning("gamma watcher: reading the chain failed (%s: %s); retrying next tick", type(exc).__name__, exc)
        if own:
            _close()
        return 0

    block, counter = chain["block"], chain["counter"]
    previous = gamma_app.last_block(session_factory)
    if previous is not None and block < previous:
        # An endpoint behind the one read last: its totals are older, not
        # smaller. Read it again later rather than compare them.
        log.warning("gamma watcher: finalized block %d is behind the last read (%d); skipping", block, previous)
        if own:
            _close()
        return 0
    with session_scope(session_factory) as session:
        known = {(r.service_hotkey, r.hotkey): (r.subnet_counter, r.total_raw) for r in session.scalars(
            select(GammaSpentTotalORM).where(GammaSpentTotalORM.netuid == netuid)).all()}

    credited, stuck = 0, False
    events: dict | None | bool = False  # False: not read yet (only read when a total grew)
    for service, totals in chain["totals"].items():
        for hotkey, total in totals.items():
            seen = known.get((service, hotkey))
            if seen is not None and seen[0] == counter and total <= seen[1]:
                continue
            if events is False:
                events = (spend_events(substrate, netuid, set(services), previous + 1, block)
                          if previous is not None else None)
            before = seen[1] if seen is not None and seen[0] == counter else 0
            mine = (events or {}).get((service, hotkey)) or []
            if mine and sum(amount for _, _, amount in mine) == total - before:
                # Each spend with its own block and transaction.
                steps, running = [], before
                for number, tx_hash, amount in mine:
                    running += amount
                    steps.append((running, number, tx_hash))
            else:
                steps = [(total, block, None)]
            try:
                coldkey = deposit_coldkey(substrate, chain["head"], netuid, hotkey)
                for step_total, step_block, tx_hash in steps:
                    outcome = gamma_app.credit_growth(
                        session_factory, netuid=netuid, counter=counter, service=service, hotkey=hotkey,
                        total_raw=step_total, block=step_block, coldkey=coldkey, billing=billing, tx_hash=tx_hash,
                    )
                    if outcome == "credited":
                        credited += 1
            except gamma_app.RetryLater as exc:
                log.warning("gamma watcher: %s waits (%s)", hotkey, exc)
                stuck = True
            except Exception:
                log.exception("gamma watcher: crediting %s at block %d failed; retrying next tick", hotkey, block)
                stuck = True
        for (svc, hotkey), (seen_counter, seen_total) in known.items():
            if svc != service or seen_counter != counter or seen_total <= 0:
                continue
            now_total = totals.get(hotkey, 0)
            if now_total < seen_total:
                gamma_app._alert_once(f"shrunk:{service}:{hotkey}:{counter}", (
                    f"Gamma watcher: what hotkey {hotkey} spent at our service {service} on netuid {netuid} fell "
                    f"from {gamma.gamma_text(seen_total)} to {gamma.gamma_text(now_total)} gamma at block {block}. "
                    "Totals only fall when the subnet is dissolved; nothing was debited. Check the subnet, and "
                    "the GammaSpent events of the block that removed it."
                ))

    if own:
        _ticks["n"] += 1
        if _ticks["n"] % STATUS_EVERY_TICKS == 1:
            try:
                status = read_status(substrate, chain["head"], netuid, services[0])
                _check_owner(status, netuid)
                status.update({"netuid": netuid, "checked_block": block,
                               "checked_at": datetime.now(UTC).isoformat(timespec="seconds")})
                _kv_set(session_factory, gamma_app.CHAIN_STATUS_KEY, json.dumps(status))
            except Exception as exc:  # noqa: BLE001
                log.warning("gamma watcher: reading gamma's status failed: %s", exc)
    if not stuck:
        # Only a tick that handled every total moves the cursor, so the
        # billing monitor sees a watcher stuck on one as stalled.
        _kv_set(session_factory, gamma_app.STATE_KEY, str(block))
    return credited


def _enabled() -> bool:
    return (os.environ.get("GREENCOMPUTE_GAMMA_WATCHER_ENABLED") or "true").strip().lower() not in (
        "0", "false", "no", "off")


def _interval() -> int:
    try:
        return max(6, int(os.environ.get("GREENCOMPUTE_GAMMA_WATCHER_INTERVAL_SECONDS") or 12))
    except ValueError:
        return 12


async def gamma_watcher_loop() -> None:
    """Background task (gateway lifespan)."""
    if not _enabled():
        log.info("gamma watcher disabled")
        return
    from greencompute_gateway.application.billing_service import get_billing_service

    interval = _interval()
    log.info("gamma watcher starting: netuid %d, service %s, every %d s", gamma.netuid(), gamma.service_hotkey(),
             interval)
    while True:
        try:
            billing = get_billing_service()
            n = await asyncio.to_thread(scan_gamma, billing.repo.session_factory, billing)
            if n:
                log.info("gamma watcher: credited %d spend(s)", n)
        except Exception:
            log.exception("gamma watcher tick failed")
        await asyncio.sleep(interval)
