"""An email when a running rental stops by itself (external API user, request
#36: "alerts when a card stops").

Reads the account events: a `deployment.failed` whose `previous_state` was
`ready` is a rental that was running and stopped without being asked to (its
container died, its machine was lost). Stopping it yourself, a start that
fails (it never ran), or a suspension for credit (the low-credit emails cover
that) send nothing here. One email per customer per pass lists every rental
that stopped, so a lost machine with ten of their cards is one email.

One cursor over everyone's events (key_value_state), taken under a row lock
so only one process sends. Events from different customers can commit a
moment out of id order, so only events older than SETTLE are read.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from sqlalchemy import func, select

from greencompute_persistence import session_scope
from greencompute_persistence.orm import AccountEventORM, KeyValueStateORM

log = logging.getLogger(__name__)

KEY = "notify:stopped:last_event_id"
SETTLE = timedelta(seconds=10)
BATCH = 500


def stopped_once(session_factory, notify: Callable | None, *, now: datetime | None = None) -> int:
    """One pass: returns how many customers were emailed."""
    now = now or datetime.now(UTC)
    by_user: dict[str, list[dict[str, Any]]] = {}
    first_id: dict[str, int] = {}
    with session_scope(session_factory) as session:
        row = session.get(KeyValueStateORM, KEY, with_for_update=True)
        if row is None:
            # First run: start from now, never email about the past.
            latest = session.scalar(select(func.max(AccountEventORM.event_id))) or 0
            session.add(KeyValueStateORM(key=KEY, value=str(latest), updated_at=now))
            return 0
        cursor = int(row.value or 0)
        events = session.scalars(
            select(AccountEventORM)
            .where(AccountEventORM.event_id > cursor, AccountEventORM.created_at <= now - SETTLE)
            .order_by(AccountEventORM.event_id)
            .limit(BATCH)
        ).all()
        if not events:
            return 0
        row.value = str(events[-1].event_id)
        row.updated_at = now
        for event in events:
            data = event.data or {}
            if event.type != "deployment.failed" or data.get("previous_state") != "ready":
                continue
            by_user.setdefault(event.user_id, []).append({
                "deployment_id": data.get("deployment_id"),
                "name": data.get("name") or (data.get("deployment_id") or "")[:8],
                "error": (data.get("error") or "")[:300] or None,
            })
            first_id.setdefault(event.user_id, event.event_id)
    if notify is not None:
        for user_id, rentals in by_user.items():
            notify(session_factory, user_id, "deployments_stopped", {"count": len(rentals), "rentals": rentals},
                   event_id=f"deployments_stopped:{user_id}:{first_id[user_id]}")
    return len(by_user)


async def stopped_alert_loop() -> None:
    """Background coroutine started with the gateway: one pass a minute."""
    from greencompute_gateway.application.services import service
    from greencompute_gateway.infrastructure.notify_website import notify_user

    if os.environ.get("GREENCOMPUTE_STOPPED_EMAILS_ENABLED", "1").strip().lower() in ("0", "false", "no"):
        log.info("stopped-rental emails disabled")
        return
    every = max(20, int(os.environ.get("GREENCOMPUTE_STOPPED_EMAIL_INTERVAL_SECONDS", "60") or 60))
    sf = service.repository.session_factory
    await asyncio.sleep(40)
    while True:
        try:
            sent = await asyncio.to_thread(stopped_once, sf, notify_user)
            if sent:
                log.info("stopped-rental emails: %d customer(s)", sent)
        except Exception:  # noqa: BLE001
            log.exception("stopped-rental emails: pass failed")
        await asyncio.sleep(every)
