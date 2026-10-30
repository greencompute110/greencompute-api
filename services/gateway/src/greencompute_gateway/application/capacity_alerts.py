""""Tell me when this GPU is free" (external API user, request #30: the site
showed one 4090 left, it couldn't be rented, and they had to keep checking).

An alert waits for room for `count` rentals of `gpu_count` GPUs of one model,
each rental on one machine (what placement needs), checked every minute
against the scheduler's own count of free GPUs per machine. It fires once:
a `capacity.available` account event (events feed, webhooks) and an email.
It expires after 7 days. To rent the moment GPUs free up instead of being
told, use a fleet with `replace: false`.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from sqlalchemy import select

from greencompute_persistence import session_scope
from greencompute_persistence.account_events import add_event
from greencompute_persistence.orm import AccountEventORM, CapacityAlertORM, UserORM

log = logging.getLogger(__name__)

MAX_WAITING = 5
LIFETIME = timedelta(days=7)
MAX_GPU_COUNT = 8
MAX_COUNT = 64


class CapacityAlertError(ValueError):
    pass


class CapacityAlertNotFound(KeyError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def normalize_model(raw: object) -> str:
    """The canonical name (`rtx4090` from "RTX 4090", "rtx-4090"…) of a GPU
    we rent out: the price list's own list, so an alert can always match."""
    from greencompute_gateway.domain.gpu_catalog import normalize_gpu_model, public_gpu_models

    model = normalize_gpu_model(str(raw or ""))
    public = [normalize_gpu_model(m) for m in public_gpu_models()]
    if model not in public:
        raise CapacityAlertError("gpu_model must be a GPU we rent out: " + ", ".join(public))
    return model


def _int(value: object, name: str, top: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= top:
        raise CapacityAlertError(f"{name} must be a whole number from 1 to {top}")
    return value


def _out(row: CapacityAlertORM) -> dict[str, Any]:
    return {
        "alert_id": row.alert_id,
        "gpu_model": row.gpu_model,
        "gpu_count": row.gpu_count,
        "count": row.count,
        "state": row.state,
        "created_at": _utc(row.created_at).isoformat() if row.created_at else None,
        "expires_at": _utc(row.expires_at).isoformat() if row.expires_at else None,
        "fired_at": _utc(row.fired_at).isoformat() if row.fired_at else None,
    }


def create_alert(session_factory, user_id: str, gpu_model: object, gpu_count: object = 1, count: object = 1, *,
                 now: datetime | None = None) -> dict[str, Any]:
    """A new waiting alert, or the same one already waiting."""
    model = normalize_model(gpu_model)
    gpu_count = _int(gpu_count, "gpu_count", MAX_GPU_COUNT)
    count = _int(count, "count", MAX_COUNT)
    now = now or _now()
    with session_scope(session_factory) as session:
        # One create per customer at a time, so two at once can't both pass
        # the limit or both add the same alert.
        session.get(UserORM, user_id, with_for_update=True)
        waiting = _waiting(session, user_id, now)
        for row in waiting:
            if (row.gpu_model, row.gpu_count, row.count) == (model, gpu_count, count):
                return _out(row)
        if len(waiting) >= MAX_WAITING:
            raise CapacityAlertError(f"at most {MAX_WAITING} alerts can wait at once; cancel one first")
        row = CapacityAlertORM(
            alert_id="ca_" + secrets.token_hex(8), user_id=user_id, gpu_model=model, gpu_count=gpu_count,
            count=count, state="waiting", created_at=now, expires_at=now + LIFETIME,
        )
        session.add(row)
        session.flush()
        return _out(row)


def _waiting(session, user_id: str, now: datetime) -> list[CapacityAlertORM]:
    """The customer's waiting alerts; any past its expiry is marked expired
    here, so the limit and the list never depend on the loop running."""
    rows = session.scalars(select(CapacityAlertORM).where(
        CapacityAlertORM.user_id == user_id, CapacityAlertORM.state == "waiting")).all()
    live = []
    for row in rows:
        if _utc(row.expires_at) <= now:
            row.state = "expired"
        else:
            live.append(row)
    return live


def list_alerts(session_factory, user_id: str, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Waiting alerts, and those that fired or expired in the last 7 days."""
    now = now or _now()
    since = now - LIFETIME
    with session_scope(session_factory) as session:
        _waiting(session, user_id, now)
        rows = session.scalars(select(CapacityAlertORM).where(
            CapacityAlertORM.user_id == user_id, CapacityAlertORM.created_at >= since - LIFETIME,
        ).order_by(CapacityAlertORM.created_at.desc())).all()
        return [_out(r) for r in rows if r.state == "waiting" or _utc(r.fired_at or r.expires_at) >= since]


def cancel_alert(session_factory, user_id: str, alert_id: str) -> dict[str, Any]:
    with session_scope(session_factory) as session:
        row = session.get(CapacityAlertORM, alert_id)
        if row is None or row.user_id != user_id:
            raise CapacityAlertNotFound(alert_id)
        if row.state == "waiting":
            row.state = "cancelled"
        return _out(row)


def free_by_model(capacity: list[tuple[Any, int]]) -> dict[str, list[int]]:
    """GPUs free on each machine, per model, from the control plane's
    schedulable_capacity(): what a rental of several GPUs can really use."""
    from greencompute_gateway.domain.gpu_catalog import normalize_gpu_model

    out: dict[str, list[int]] = {}
    for node, free in capacity:
        if free > 0:
            out.setdefault(normalize_gpu_model(node.gpu_model), []).append(int(free))
    return out


def _fits(free_per_machine: list[int], gpu_count: int, count: int) -> bool:
    """`count` rentals of `gpu_count` GPUs each, each on one machine."""
    return sum(free // gpu_count for free in free_per_machine) >= count


def check_once(session_factory, free: dict[str, list[int]], *, notify: Callable | None = None,
               now: datetime | None = None) -> list[str]:
    """Fire every waiting alert there is room for now, expire the old ones.
    Returns the ids fired. The event is written with the change of state, so
    an alert fires exactly once; one email per customer follows, even when
    several of their alerts fire together."""
    now = now or _now()
    fired: list[str] = []
    emails: dict[str, dict[str, Any]] = {}
    with session_scope(session_factory) as session:
        rows = session.scalars(
            select(CapacityAlertORM).where(CapacityAlertORM.state == "waiting").with_for_update(skip_locked=True)
        ).all()
        for row in sorted(rows, key=lambda r: (r.user_id, r.created_at or now)):  # customers' event locks in one order
            if _utc(row.expires_at) <= now:
                row.state = "expired"
                continue
            machines = free.get(row.gpu_model, [])
            if not _fits(machines, row.gpu_count, row.count):
                continue
            row.state = "fired"
            row.fired_at = now
            data = {
                "alert_id": row.alert_id, "gpu_model": row.gpu_model, "gpu_count": row.gpu_count, "count": row.count,
                "available_gpus": sum(machines), "max_gpus_per_deployment": max(machines),
            }
            add_event(session, AccountEventORM(user_id=row.user_id, type="capacity.available", data=data, created_at=now))
            fired.append(row.alert_id)
            best = emails.get(row.user_id)
            if best is None or row.gpu_count * row.count > best["gpu_count"] * best["count"]:
                emails[row.user_id] = data
    if notify is not None:
        for user_id, data in emails.items():
            notify(session_factory, user_id, "gpu_available", data, event_id=f"gpu_available:{data['alert_id']}")
    return fired


async def capacity_alert_loop() -> None:
    """Background coroutine started with the gateway: one check a minute."""
    from greencompute_gateway.application.services import service
    from greencompute_gateway.infrastructure.notify_website import notify_user

    if os.environ.get("GREENCOMPUTE_CAPACITY_ALERTS_ENABLED", "1").strip().lower() in ("0", "false", "no"):
        log.info("capacity alerts disabled")
        return
    every = max(15, int(os.environ.get("GREENCOMPUTE_CAPACITY_ALERT_INTERVAL_SECONDS", "60") or 60))
    sf = service.repository.session_factory
    await asyncio.sleep(30)
    while True:
        try:
            capacity = await asyncio.to_thread(service.control_plane.schedulable_capacity)
            fired = await asyncio.to_thread(check_once, sf, free_by_model(capacity), notify=notify_user)
            if fired:
                log.info("capacity alerts: %d fired", len(fired))
        except Exception:  # noqa: BLE001
            log.exception("capacity alerts: check failed")
        await asyncio.sleep(every)
