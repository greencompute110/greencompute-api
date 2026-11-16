"""Sanctions screening of wallets: at wallet sign-in, when a wallet is linked
to an account, before a deposit is credited, and before an account rents.

Sources (infrastructure/sanctions_lists.py): the OFAC SDN list and the UK
Sanctions List, downloaded daily into `sanctioned_addresses`, and for EVM
addresses the Chainalysis oracle on Ethereum, asked live.

When screening data is missing:
- A refresh that fails (download error, or a file that no longer parses to
  a plausible number of addresses) keeps the last good copy of that list:
  a list going quiet is far likelier to be an outage than a mass delisting.
  Staff are alerted once a day while a list is more than STALE_AFTER old.
- If the OFAC list has never loaded (a fresh database), wallet sign-ups that
  would create a NEW account are refused with screening_unavailable: an
  account no one has vouched for is the case screening exists for, and the
  refusal costs a retry minutes later. Everything an existing customer
  already did before this check existed (signing in, linking a wallet,
  deposits, renting) goes ahead on whatever is available, the oracle for EVM:
  turning a list download problem into an outage for paying customers would
  be worse than a few minutes without one list.
- The oracle not answering: the lists alone decide (it is a second opinion
  on the same OFAC designations, not a separate source).

GREENCOMPUTE_SANCTIONS_ENABLED=false turns all of it off;
GREENCOMPUTE_SANCTIONS_ORACLE_ENABLED=false only the oracle;
GREENCOMPUTE_SANCTIONS_REFRESH_HOURS (default 24) sets how often lists refresh.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import threading
import time
from collections import deque
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, exists, or_, select, text

from greencompute_persistence import session_scope
from greencompute_persistence.orm import KeyValueStateORM, SanctionedAddressORM, VerifiedWalletORM
from greencompute_gateway.infrastructure import sanctions_lists

log = logging.getLogger(__name__)

#: The neutral words a customer sees: never which list, never "sanctioned".
BLOCKED_MESSAGE = "This wallet can't be used with Green Compute. Contact support@green-compute.com."

OFAC = "ofac_sdn"
UK = "uk_sanctions_list"
ORACLE = "chainalysis_oracle"
SOURCES = (OFAC, UK)
# Fewer addresses than this means the file changed format, not that the
# list emptied: keep the last copy. (Nov 2026: OFAC 127 EVM, UK 6.)
MIN_ADDRESSES = {OFAC: 20, UK: 1}
STALE_AFTER = timedelta(days=3)
ORACLE_CACHE_SECONDS = 6 * 3600
# After the oracle fails to answer, don't ask again for this long: a dead RPC
# must not add its timeout to every deposit and sign-in meanwhile.
ORACLE_BACKOFF_SECONDS = 60

_REFRESH_LOCK_KEY = int.from_bytes(hashlib.sha256(b"sanctions-refresh").digest()[:8], "big", signed=True)
# Another process started a refresh this recently: don't download it again.
_REFRESH_CLAIM = timedelta(minutes=30)

_oracle_cache: dict[str, tuple[float, bool]] = {}

# Live oracle calls in the last minute (monotonic times), capped below.
_oracle_calls: deque[float] = deque()
ORACLE_MAX_PER_MINUTE = int(os.environ.get("GREENCOMPUTE_SANCTIONS_ORACLE_PER_MINUTE", "60") or 60)
_oracle_failed_at: list[float] = []
_oracle_lock = threading.Lock()
_stale_alerted: dict[str, float] = {}


class ScreeningUnavailable(Exception):
    """A new account can't be screened: the lists have never loaded."""


def _flag(name: str, default: bool = True) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    return default


def enabled() -> bool:
    return _flag("GREENCOMPUTE_SANCTIONS_ENABLED")


def oracle_enabled() -> bool:
    return enabled() and _flag("GREENCOMPUTE_SANCTIONS_ORACLE_ENABLED")


def _now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


# --- the lists in the database ------------------------------------------------


def _kv_set(session, key: str, value: str) -> None:
    row = session.get(KeyValueStateORM, key) or KeyValueStateORM(key=key)
    row.value, row.updated_at = value, _now()
    session.add(row)


def _kv(session, key: str) -> str | None:
    row = session.get(KeyValueStateORM, key)
    return row.value if row else None


def store_list(session_factory, source: str, entries: dict[tuple[str, str], str]) -> int:
    """Replace `source`'s addresses with `entries` in one transaction (a
    reader sees the old copy or the new one, never half)."""
    now = _now()
    with session_scope(session_factory) as session:
        if session.get_bind().dialect.name == "postgresql":
            # Two gateway processes refreshing at once take turns here.
            session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _REFRESH_LOCK_KEY})
        session.execute(delete(SanctionedAddressORM).where(SanctionedAddressORM.source == source))
        session.add_all(
            SanctionedAddressORM(source=source, chain=chain, address=address, label=label or None, loaded_at=now)
            for (chain, address), label in entries.items()
        )
        _kv_set(session, f"sanctions:{source}:loaded_at", now.isoformat())
        _kv_set(session, f"sanctions:{source}:count", str(len(entries)))
        _kv_set(session, f"sanctions:{source}:last_error", "")
    return len(entries)


def _record_failure(session_factory, source: str, error: str) -> None:
    try:
        with session_scope(session_factory) as session:
            _kv_set(session, f"sanctions:{source}:last_error", error[:500])
            _kv_set(session, f"sanctions:{source}:failed_at", _now().isoformat())
    except Exception:  # noqa: BLE001 - recording a failure must not raise one
        log.exception("sanctions: could not record the %s failure", source)


def _fetch(source: str, download) -> dict[tuple[str, str], str]:
    if source == OFAC:
        sdn_url, comments_url = sanctions_lists.ofac_urls()
        sdn = download(sdn_url)
        try:
            comments = download(comments_url)
        except Exception:  # noqa: BLE001 - without it a few long entries lose their tail
            log.warning("sanctions: SDN_COMMENTS.CSV unavailable; reading SDN.CSV alone")
            comments = None
        return sanctions_lists.parse_ofac_sdn(sdn, comments)
    if source == UK:
        return sanctions_lists.parse_uk_list(download(sanctions_lists.uk_url()))
    raise ValueError(source)


def refresh(session_factory, *, download=None, sources: tuple[str, ...] = SOURCES) -> dict[str, dict]:
    """Download each list and replace its copy; a source that fails keeps
    the copy it had. Returns {source: {"ok", "count" | "error"}}."""
    download = download or sanctions_lists.download
    out: dict[str, dict] = {}
    for source in sources:
        try:
            entries = _fetch(source, download)
            if len(entries) < MIN_ADDRESSES[source]:
                raise ValueError(f"only {len(entries)} addresses found; the file's format may have changed")
            out[source] = {"ok": True, "count": store_list(session_factory, source, entries)}
            log.info("sanctions: %s refreshed, %d addresses", source, out[source]["count"])
        except Exception as exc:  # noqa: BLE001 - keep the last good copy, say why
            error = f"{type(exc).__name__}: {exc}"[:500]
            log.warning("sanctions: %s refresh failed (%s); keeping the last copy", source, error)
            _record_failure(session_factory, source, error)
            out[source] = {"ok": False, "error": error}
    return out


def list_state(session_factory) -> dict[str, dict]:
    """Per source: when it last loaded, how many addresses, the last error."""
    with session_scope(session_factory) as session:
        state = {}
        for source in SOURCES:
            loaded = _kv(session, f"sanctions:{source}:loaded_at")
            state[source] = {
                "loaded_at": loaded or None,
                "addresses": int(_kv(session, f"sanctions:{source}:count") or 0),
                "last_error": _kv(session, f"sanctions:{source}:last_error") or None,
                "failed_at": _kv(session, f"sanctions:{source}:failed_at") or None,
            }
    return state


def lists_loaded(session_factory) -> bool:
    """Has the OFAC list (where nearly all the addresses are) ever loaded?"""
    with session_scope(session_factory) as session:
        return bool(_kv(session, f"sanctions:{OFAC}:loaded_at"))


# --- screening ----------------------------------------------------------------


def listed(session_factory, chain: str, address: str) -> str | None:
    """The list naming this wallet, or None."""
    with session_scope(session_factory) as session:
        return session.scalar(
            select(SanctionedAddressORM.source).where(
                SanctionedAddressORM.chain == chain, SanctionedAddressORM.address == address
            ).order_by(SanctionedAddressORM.source).limit(1)
        )


def oracle_says(address: str) -> bool | None:
    """The Chainalysis oracle's answer for an EVM address (cached for a few
    hours), or None when it can't be asked."""
    now = time.monotonic()
    with _oracle_lock:
        cached = _oracle_cache.get(address)
        if cached and now - cached[0] < ORACLE_CACHE_SECONDS:
            return cached[1]
        if _oracle_failed_at and now - _oracle_failed_at[-1] < ORACLE_BACKOFF_SECONDS:
            return None
        # A cap on live calls: they share ETH_RPC_URL with the deposit watcher,
        # and fresh addresses cost nothing to make, so a flood of sign-ins
        # mustn't get our RPC rate-limited. Over it, the lists decide.
        while _oracle_calls and now - _oracle_calls[0] > 60:
            _oracle_calls.popleft()
        if len(_oracle_calls) >= ORACLE_MAX_PER_MINUTE:
            return None
        _oracle_calls.append(now)
    try:
        answer = sanctions_lists.oracle_is_sanctioned(address)
    except Exception as exc:  # noqa: BLE001 - unknown, not cached: ask again after the backoff
        log.warning("sanctions: oracle unavailable (%s); screening on the lists alone", type(exc).__name__)
        with _oracle_lock:
            _oracle_failed_at[:] = [now]
        return None
    with _oracle_lock:
        if len(_oracle_cache) > 50_000:
            _oracle_cache.clear()
        _oracle_cache[address] = (now, answer)
    return answer


def screen(session_factory, chain: str, address: str, *, new_account: bool = False) -> str | None:
    """Which source names this wallet (canonical address), or None if none
    does. Raises ScreeningUnavailable for a wallet about to become a new
    account when the lists have never loaded and nothing named it."""
    if not enabled():
        return None
    hit = listed(session_factory, chain, address)
    if hit:
        return hit
    if chain == "evm" and oracle_enabled() and oracle_says(address):
        return ORACLE
    if new_account and not lists_loaded(session_factory):
        raise ScreeningUnavailable()
    return None


def flag_wallet(session_factory, chain: str, address: str, source: str) -> None:
    """Record a live (oracle) hit on a verified wallet, so its account can't
    rent. List hits need no flag: they are looked up each time."""
    if source in SOURCES:
        return
    with session_scope(session_factory) as session:
        row = session.scalar(select(VerifiedWalletORM).where(
            VerifiedWalletORM.chain == chain, VerifiedWalletORM.address == address))
        if row is not None:
            row.sanctions_hit, row.sanctions_checked_at = source, _now()


def account_blocked(session_factory, user_id: str | None) -> str | None:
    """The source naming one of this account's verified wallets, or None."""
    if not user_id or not enabled():
        return None
    with session_scope(session_factory) as session:
        listed_wallet = exists().where(
            SanctionedAddressORM.chain == VerifiedWalletORM.chain,
            SanctionedAddressORM.address == VerifiedWalletORM.address,
        )
        row = session.execute(
            select(VerifiedWalletORM.sanctions_hit, VerifiedWalletORM.address).where(
                VerifiedWalletORM.user_id == user_id,
                or_(VerifiedWalletORM.sanctions_hit.is_not(None), listed_wallet),
            ).limit(1)
        ).first()
    if row is None:
        return None
    return row[0] or "list"


# --- the daily refresh ----------------------------------------------------------


def _refresh_hours() -> float:
    try:
        return max(1.0, float(os.environ.get("GREENCOMPUTE_SANCTIONS_REFRESH_HOURS") or 24))
    except ValueError:
        return 24.0


def _due(session_factory, every: timedelta) -> tuple[str, ...]:
    """The lists whose copy is older than `every` (or missing), unless
    another process started a refresh in the last _REFRESH_CLAIM. Claims the
    refresh when there are some (two processes can both claim in a race;
    store_list then serialises them, and the second rewrites the same rows)."""
    now = _now()
    with session_scope(session_factory) as session:
        started = _kv(session, "sanctions:refresh_started_at")
        if started and now - _utc(datetime.fromisoformat(started)) < _REFRESH_CLAIM:
            return ()
        due = []
        for source in SOURCES:
            loaded = _kv(session, f"sanctions:{source}:loaded_at")
            if not loaded or now - _utc(datetime.fromisoformat(loaded)) >= every:
                due.append(source)
        if due:
            _kv_set(session, "sanctions:refresh_started_at", now.isoformat())
    return tuple(due)


def _alert_if_stale(session_factory) -> None:
    """Tell staff, at most once a day per list, that a list hasn't loaded
    for STALE_AFTER (or ever)."""
    from greencompute_gateway.infrastructure.alerts import send_payment_alert

    now = _now()
    for source, state in list_state(session_factory).items():
        loaded = _utc(datetime.fromisoformat(state["loaded_at"])) if state["loaded_at"] else None
        if loaded and now - loaded < STALE_AFTER:
            continue
        if time.monotonic() - _stale_alerted.get(source, -1e9) < 86400:
            continue
        _stale_alerted[source] = time.monotonic()
        if loaded:
            since, using = f"since {loaded:%Y-%m-%d %H:%M} UTC", "its last good copy"
        elif source == OFAC:
            since, using = "ever", "the other sources only, and new wallet sign-ups are refused until it loads"
        else:
            since, using = "ever", "the other sources only"
        send_payment_alert(
            f"Sanctions screening: the {source} list hasn't downloaded {since} "
            f"(last error: {state['last_error'] or 'none recorded'}). Wallets are screened against {using}."
        )


async def sanctions_refresh_loop() -> None:
    """Background coroutine started with the gateway: refresh the lists when
    the copy is older than GREENCOMPUTE_SANCTIONS_REFRESH_HOURS, checking
    every 10 minutes (a failed download is tried again once the claim lapses,
    after 30 minutes)."""
    from greencompute_gateway.application.services import service

    if not enabled():
        log.info("sanctions screening disabled")
        return
    sf = service.repository.session_factory
    every = timedelta(hours=_refresh_hours())
    await asyncio.sleep(15)  # let startup settle
    while True:
        try:
            due = await asyncio.to_thread(_due, sf, every)
            if due:
                await asyncio.to_thread(refresh, sf, sources=due)
            await asyncio.to_thread(_alert_if_stale, sf)
        except Exception:  # noqa: BLE001 - logged; tried again next check
            log.exception("sanctions refresh pass failed")
        await asyncio.sleep(600)
