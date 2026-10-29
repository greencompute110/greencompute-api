"""Account events: what a customer's code may want to act on without polling
every deployment (external API user, request #14, Oct 2026).

A deployment's change of state is recorded in the same transaction that saves
it, so no transition is missed and none is recorded that didn't happen. Only
the states a client acts on make an event; ready after suspended is a resume
(`previous_state` says so).

Readers page by event id (`after=`), so ids must become visible in order: a
transaction that took id 100 must never commit after one that took 101 was
read, or a cursor would pass 100 for good. add_event therefore holds a
per-customer lock from just before the event's id is taken until the
transaction commits: one customer's events commit in id order."""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from greencompute_persistence.orm import AccountEventORM

#: Every event type, and what it means: the table in the docs.
EVENT_TYPES: dict[str, str] = {
    "deployment.ready": "A rental or private model is running (also after a resume: see `previous_state`).",
    "deployment.failed": "It could not start, or stopped with an error; `error` says why. Nothing is billed for a start that failed.",
    "deployment.suspended": "It was paused: `suspended_reason` is `balance` (credit ran out) or `idle` (a private model nobody called).",
    "deployment.terminated": "It was stopped for good: deleted, or `max_runtime_minutes` reached.",
    "balance.low": "Your balance lasts under 24 hours at the current rate (sent again under 2 hours).",
}
_DEPLOYMENT_STATES = {"ready", "failed", "suspended", "terminated"}
_ERROR_CHARS = 1000


def utcnow() -> datetime:
    return datetime.now(UTC)


def deployment_event(row: Any, previous_state: str | None) -> AccountEventORM | None:
    """The event for a deployment row whose state is now `row.state`, or None
    when nothing a client acts on happened (no owner, same state, or a state
    in between such as pulling)."""
    state = row.state
    if not row.owner_user_id or state == previous_state or state not in _DEPLOYMENT_STATES:
        return None
    error = row.last_error if state in ("failed", "suspended", "terminated") else None
    return AccountEventORM(
        user_id=row.owner_user_id,
        type=f"deployment.{state}",
        deployment_id=row.deployment_id,
        data={
            "deployment_id": row.deployment_id,
            "name": row.name,
            "workload_id": row.workload_id,
            "state": state,
            "previous_state": previous_state,
            "error": error[:_ERROR_CHARS] if error else None,
            "suspended_reason": getattr(row, "suspended_reason", None) if state == "suspended" else None,
        },
        created_at=utcnow(),
    )


def _lock_key(user_id: str) -> int:
    return int.from_bytes(hashlib.sha256(f"account-events:{user_id}".encode()).digest()[:8], "big", signed=True)


def add_event(session, event: AccountEventORM | None) -> None:
    """Add `event` to the session's transaction, holding its customer's event
    lock until commit (Postgres; elsewhere one writer at a time anyway).
    The transaction's pending row changes are written first, so every writer
    takes its row locks before this lock, never the other way round (which
    could deadlock against one that did)."""
    if event is None:
        return
    if session.get_bind().dialect.name == "postgresql":
        session.flush()
        session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _lock_key(event.user_id)})
    session.add(event)


def event_out(row: AccountEventORM) -> dict[str, Any]:
    """An event as the API and webhooks send it."""
    return {
        "id": row.event_id,
        "type": row.type,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "data": row.data or {},
    }
