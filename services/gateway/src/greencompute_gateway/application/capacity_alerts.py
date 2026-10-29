""""Tell me when this GPU is free" (external API user, request #30: the site
showed one 4090 left, it couldn't be rented, and they had to keep checking).

An alert waits for room for `count` rentals of `gpu_count` GPUs of one model
(the most free on one machine is what a multi-GPU rental needs), checked
every minute against the same numbers the price list shows. It fires once:
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
from greencompute_persistence.orm import AccountEventORM, CapacityAlertORM

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


def _families() -> list[str]:
    raw = os.getenv("GREENCOMPUTE_PUBLIC_RENTAL_GPU_FAMILIES", "4090,5090")
    return [f.strip().lower() for f in raw.split(",") if f.strip()]


def normalize_model(raw: object) -> str:
    """`rtx4090` from "RTX 4090", "rtx-4090"…; only GPUs we rent out."""
    model = "".join(ch for ch in str(raw or "").lower() if ch.isalnum())
    families = _families()
    if not model or not any(f in model for f in families):
        choices = ", ".join(f"rtx{f}" for f in families)
        raise CapacityAlertError(f"gpu_model must be a GPU we rent out: {choices}")
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
        waiting = session.scalars(select(CapacityAlertORM).where(
            CapacityAlertORM.user_id == user_id, CapacityAlertORM.state == "waiting")).all()
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


def list_alerts(session_factory, user_id: str, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Waiting alerts, and those that fired or expired in the last 7 days."""
    since = (now or _now()) - LIFETIME
    with session_scope(session_factory) as session:
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


def _room(capacity: dict[str, dict[str, int]], model: str, gpu_count: int, count: int) -> dict[str, int] | None:
    entry = capacity.get(model)
    if not entry:
        return None
    if entry.get("max_gpus_per_deployment", 0) >= gpu_count and entry.get("available_gpus", 0) >= gpu_count * count:
        return entry
    return None


def check_once(session_factory, capacity: dict[str, dict[str, int]], *, notify: Callable | None = None,
               now: datetime | None = None) -> list[str]:
    """Fire every waiting alert there is room for now, expire the old ones.
    Returns the ids fired. The event is written with the change of state, so
    an alert fires exactly once; the email follows."""
    now = now or _now()
    fired: list[tuple[str, dict[str, Any], str]] = []
    with session_scope(session_factory) as session:
        rows = session.scalars(
            select(CapacityAlertORM).where(CapacityAlertORM.state == "waiting").with_for_update(skip_locked=True)
        ).all()
        for row in sorted(rows, key=lambda r: (r.user_id, r.created_at or now)):  # customers' event locks in one order
            if _utc(row.expires_at) <= now:
                row.state = "expired"
                continue
            entry = _room(capacity, row.gpu_model, row.gpu_count, row.count)
            if entry is None:
                continue
            row.state = "fired"
            row.fired_at = now
            data = {
                "alert_id": row.alert_id, "gpu_model": row.gpu_model, "gpu_count": row.gpu_count, "count": row.count,
                "available_gpus": entry.get("available_gpus", 0),
                "max_gpus_per_deployment": entry.get("max_gpus_per_deployment", 0),
            }
            add_event(session, AccountEventORM(user_id=row.user_id, type="capacity.available", data=data, created_at=now))
            fired.append((row.user_id, data, row.alert_id))
    for user_id, data, alert_id in fired:
        if notify is not None:
            notify(session_factory, user_id, "gpu_available", data, event_id=f"gpu_available:{alert_id}")
    return [alert_id for _, _, alert_id in fired]


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
            capacity = await asyncio.to_thread(service.control_plane.capacity_by_model)
            fired = await asyncio.to_thread(check_once, sf, capacity, notify=notify_user)
            if fired:
                log.info("capacity alerts: %d fired", len(fired))
        except Exception:  # noqa: BLE001
            log.exception("capacity alerts: check failed")
        await asyncio.sleep(every)
