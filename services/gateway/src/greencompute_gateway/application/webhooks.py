"""Account events and webhooks (external API user, request #14, Oct 2026:
"polling is the only way to see changes").

Events are written by whatever changes a deployment's state, in the same
transaction (greencompute_persistence.account_events), and by the low-credit
sweep. Customers read them two ways:
- GET /platform/events?after=<id>: one call for every change since the last
  one seen, for clients that can't receive webhooks (an agent on a laptop);
- webhooks: each event POSTed to the customer's HTTPS endpoint, signed, in
  order, at least once (retried with backoff; `id` tells repeats apart).

Ids appear in order: each customer's events are written under a lock held
until commit (account_events.add_event), so a cursor never passes an event
that was still about to appear.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, exists, func, select, text

from greencompute_persistence import session_scope
from greencompute_persistence.account_events import EVENT_TYPES, event_out
from greencompute_persistence.orm import AccountEventORM, WebhookORM
from greencompute_gateway.infrastructure import webhook_http

log = logging.getLogger(__name__)

MAX_WEBHOOKS = 5
EVENT_RETENTION = timedelta(days=30)
DISABLE_AFTER = timedelta(days=3)
BATCH = 50
#: One webhook's turn in a pass: a slow endpoint continues next pass.
TURN_SECONDS = 60
SIGNATURE_HEADER = "Green-Compute-Signature"
_PASS_LOCK_KEY = int.from_bytes(hashlib.sha256(b"webhook-delivery").digest()[:8], "big", signed=True)


class WebhookError(ValueError):
    """A request about webhooks that can't be done; the message says why."""


class WebhookNotFound(KeyError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _types(values: object) -> list[str]:
    """Validated event types; [] means all."""
    if values is None:
        return []
    if isinstance(values, str):
        values = [v for v in values.split(",") if v.strip()]
    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
        raise WebhookError("events must be a list of event types")
    out = sorted({v.strip() for v in values})
    unknown = [v for v in out if v not in EVENT_TYPES]
    if unknown:
        raise WebhookError(f"unknown event type(s) {unknown}; choose from {sorted(EVENT_TYPES)}")
    return out


# --- the events feed ----------------------------------------------------------


def list_events(session_factory, user_id: str, *, after: int = 0, types: object = None, limit: int = 100) -> dict[str, Any]:
    """Events after `after`, oldest first. `next_after` is the cursor for the
    next call (the last id looked at, even if `types` filtered it out)."""
    wanted = _types(types)
    limit = max(1, min(int(limit), 500))
    after = max(0, int(after))
    with session_scope(session_factory) as session:
        rows = session.scalars(
            select(AccountEventORM)
            .where(AccountEventORM.user_id == user_id, AccountEventORM.event_id > after)
            .order_by(AccountEventORM.event_id)
            .limit(limit)
        ).all()
        events = [event_out(r) for r in rows if not wanted or r.type in wanted]
        next_after = rows[-1].event_id if rows else after
    return {"events": events, "next_after": next_after, "has_more": len(rows) == limit}


# --- webhooks -----------------------------------------------------------------


def _webhook_out(row: WebhookORM, *, secret: bool = False) -> dict[str, Any]:
    out = {
        "webhook_id": row.webhook_id,
        "url": row.url,
        "events": list(row.events or []),
        "active": bool(row.active),
        "disabled_reason": row.disabled_reason,
        "failures": row.failures or 0,
        "last_success_at": _iso(row.last_success_at),
        "last_failure_at": _iso(row.last_failure_at),
        "last_error": row.last_error,
        "created_at": _iso(row.created_at),
    }
    if secret:
        out["secret"] = row.secret
    return out


def _checked_url(url: object, resolve) -> str:
    try:
        host, port, _ = webhook_http.check_url(url)
        resolve(host, port)
    except webhook_http.WebhookURLError as exc:
        raise WebhookError(str(exc)) from exc
    return str(url).strip()


def create_webhook(session_factory, user_id: str, url: object, events: object = None, *,
                   resolve=webhook_http.resolve_public, now: datetime | None = None) -> dict[str, Any]:
    """A new webhook, with its signing secret (shown only now). It gets the
    events from now on, not the history (GET /platform/events has that)."""
    url = _checked_url(url, resolve)
    wanted = _types(events)
    now = now or _now()
    with session_scope(session_factory) as session:
        count = session.scalar(select(func.count()).select_from(WebhookORM).where(WebhookORM.user_id == user_id))
        if (count or 0) >= MAX_WEBHOOKS:
            raise WebhookError(f"at most {MAX_WEBHOOKS} webhooks per account; delete one first")
        latest = session.scalar(
            select(func.max(AccountEventORM.event_id)).where(AccountEventORM.user_id == user_id)
        ) or 0
        row = WebhookORM(
            webhook_id="wh_" + secrets.token_hex(8), user_id=user_id, url=url,
            secret="whsec_" + secrets.token_urlsafe(32), events=wanted, active=True,
            last_event_id=int(latest), failures=0, created_at=now,
        )
        session.add(row)
        session.flush()
        return _webhook_out(row, secret=True)


def list_webhooks(session_factory, user_id: str) -> list[dict[str, Any]]:
    with session_scope(session_factory) as session:
        rows = session.scalars(
            select(WebhookORM).where(WebhookORM.user_id == user_id).order_by(WebhookORM.created_at)
        ).all()
        return [_webhook_out(r) for r in rows]


def _own(session, user_id: str, webhook_id: str) -> WebhookORM:
    row = session.get(WebhookORM, webhook_id)
    if row is None or row.user_id != user_id:
        raise WebhookNotFound(webhook_id)
    return row


def update_webhook(session_factory, user_id: str, webhook_id: str, *, url: object = None, events: object = None,
                   active: bool | None = None, resolve=webhook_http.resolve_public) -> dict[str, Any]:
    """Change the URL or the event types, or turn it on or off. A new URL,
    or turning it back on, starts over the retry count; it then delivers
    what it missed, from where it stopped (events are kept 30 days)."""
    checked = _checked_url(url, resolve) if url is not None else None
    wanted = _types(events) if events is not None else None
    with session_scope(session_factory) as session:
        row = _own(session, user_id, webhook_id)
        if checked is not None and checked != row.url:
            row.url = checked
            row.failures = 0
            row.failing_since = None
            row.next_attempt_at = None
        if wanted is not None:
            row.events = wanted
        if active is not None:
            row.active = bool(active)
            if active:
                row.disabled_reason = None
                row.failures = 0
                row.failing_since = None
                row.next_attempt_at = None
            else:
                row.disabled_reason = "turned off by you"
        return _webhook_out(row)


def delete_webhook(session_factory, user_id: str, webhook_id: str) -> None:
    with session_scope(session_factory) as session:
        session.delete(_own(session, user_id, webhook_id))


def signature_headers(secret: str, body: bytes, event: dict[str, Any], now: datetime) -> dict[str, str]:
    """`Green-Compute-Signature: t=<unix time>,v1=<hex HMAC-SHA256 of "<t>.<body>">`,
    with the webhook's secret as the key."""
    t = str(int(now.timestamp()))
    mac = hmac.new(secret.encode(), t.encode() + b"." + body, hashlib.sha256).hexdigest()
    return {
        SIGNATURE_HEADER: f"t={t},v1={mac}",
        "Green-Compute-Event": str(event.get("type")),
        "Green-Compute-Event-Id": str(event.get("id")),
    }


def _body(event: dict[str, Any]) -> bytes:
    return json.dumps(event, separators=(",", ":"), sort_keys=True).encode()


def send_test(session_factory, user_id: str, webhook_id: str, *, post=webhook_http.post,
              now: datetime | None = None) -> dict[str, Any]:
    """POST a `webhook.test` event now and say how it went. Changes nothing."""
    now = now or _now()
    with session_scope(session_factory) as session:
        row = _own(session, user_id, webhook_id)
        url, secret = row.url, row.secret
    event = {"id": 0, "type": "webhook.test", "created_at": now.isoformat(),
             "data": {"webhook_id": webhook_id, "message": "Test from Green Compute: your endpoint works."}}
    body = _body(event)
    status, error = post(url, body, signature_headers(secret, body, event, _now()))
    return {"delivered": error is None, "status_code": status, "error": error}


# --- delivery -----------------------------------------------------------------


def _backoff(failures: int) -> timedelta:
    """10 s, 20 s, 40 s … at most an hour."""
    return timedelta(seconds=min(3600, 10 * 2 ** min(max(failures, 1) - 1, 12)))


def _record_failure(session_factory, webhook_id: str, cursor: int, error: str, now: datetime) -> None:
    with session_scope(session_factory) as session:
        row = session.get(WebhookORM, webhook_id)
        if row is None:
            return
        row.last_event_id = max(row.last_event_id or 0, cursor)
        row.failures = (row.failures or 0) + 1
        row.failing_since = row.failing_since or now
        row.last_failure_at = now
        row.last_error = error[:512]
        row.next_attempt_at = now + _backoff(row.failures)
        failing_since = row.failing_since if row.failing_since.tzinfo else row.failing_since.replace(tzinfo=UTC)
        if now - failing_since >= DISABLE_AFTER:
            row.active = False
            row.disabled_reason = (
                f"turned off after failing since {failing_since:%Y-%m-%d %H:%M} UTC ({error[:200]}); "
                "fix the endpoint, then PATCH it with active: true to get what it missed"
            )


def _record_progress(session_factory, webhook_id: str, cursor: int, now: datetime, delivered: bool) -> None:
    with session_scope(session_factory) as session:
        row = session.get(WebhookORM, webhook_id)
        if row is None:
            return
        row.last_event_id = max(row.last_event_id or 0, cursor)
        if delivered:
            row.failures = 0
            row.failing_since = None
            row.next_attempt_at = None
            row.last_success_at = now
            row.last_error = None


def _current(session_factory, webhook_id: str):
    """(url, secret, wanted types) as the webhook is now, or None if it was
    deleted or turned off: read before each send, so a change made during a
    turn (a new URL, a filter, a delete) holds from the next event on."""
    with session_scope(session_factory) as session:
        row = session.get(WebhookORM, webhook_id)
        if row is None or not row.active:
            return None
        return row.url, row.secret, set(row.events or [])


def deliver_webhook(session_factory, webhook_id: str, *, post=webhook_http.post, clock=_now,
                    turn_seconds: float = TURN_SECONDS) -> int:
    """Send this webhook's next events, in order, stopping at the first that
    fails or when its turn is up. Returns how many were delivered. Each is
    signed when it is sent, and times are taken when things happen."""
    ends = time.monotonic() + turn_seconds
    with session_scope(session_factory) as session:
        row = session.get(WebhookORM, webhook_id)
        if row is None or not row.active:
            return 0
        user_id, cursor = row.user_id, row.last_event_id or 0
        events = [event_out(e) for e in session.scalars(
            select(AccountEventORM)
            .where(AccountEventORM.user_id == user_id, AccountEventORM.event_id > cursor)
            .order_by(AccountEventORM.event_id)
            .limit(BATCH)
        ).all()]
    delivered = 0
    passed_over = False
    for event in events:
        if time.monotonic() >= ends:
            break
        current = _current(session_factory, webhook_id)
        if current is None:
            return delivered
        url, secret, wanted = current
        if wanted and event["type"] not in wanted:
            cursor = event["id"]
            passed_over = True
            continue
        body = _body(event)
        status, error = post(url, body, signature_headers(secret, body, event, clock()))
        if error is not None:
            _record_failure(session_factory, webhook_id, cursor, error, clock())
            return delivered
        cursor = event["id"]
        delivered += 1
        passed_over = False
        _record_progress(session_factory, webhook_id, cursor, clock(), True)
    if passed_over:
        _record_progress(session_factory, webhook_id, cursor, clock(), False)
    return delivered


def due_webhooks(session_factory, now: datetime) -> list[str]:
    """Active webhooks, not waiting out a backoff, with events they haven't had."""
    pending = exists().where(
        AccountEventORM.user_id == WebhookORM.user_id, AccountEventORM.event_id > WebhookORM.last_event_id,
    )
    with session_scope(session_factory) as session:
        return list(session.scalars(
            select(WebhookORM.webhook_id).where(
                WebhookORM.active.is_(True),
                (WebhookORM.next_attempt_at.is_(None)) | (WebhookORM.next_attempt_at <= now),
                pending,
            )
        ).all())


class Deliverer:
    """Hands each due webhook to a worker thread, at most one turn per
    webhook at a time, and never waits for them: a slow endpoint holds only
    its own worker, never the next pass or other customers' webhooks."""

    def __init__(self, session_factory, *, post=webhook_http.post, workers: int = 8) -> None:
        self.session_factory = session_factory
        self.post = post
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="webhook")
        self._busy: set[str] = set()
        self._lock = threading.Lock()

    def submit_due(self, now: datetime | None = None) -> int:
        started = 0
        for webhook_id in due_webhooks(self.session_factory, now or _now()):
            with self._lock:
                if webhook_id in self._busy:
                    continue
                self._busy.add(webhook_id)
            self.pool.submit(self._turn, webhook_id)
            started += 1
        return started

    def _turn(self, webhook_id: str) -> None:
        try:
            sent = deliver_webhook(self.session_factory, webhook_id, post=self.post)
            if sent:
                log.info("webhook %s: delivered %d event(s)", webhook_id, sent)
        except Exception:  # noqa: BLE001
            log.exception("webhook %s: delivery failed", webhook_id)
        finally:
            with self._lock:
                self._busy.discard(webhook_id)

    def idle(self) -> bool:
        with self._lock:
            return not self._busy


def prune_events(session_factory, now: datetime | None = None) -> int:
    cutoff = (now or _now()) - EVENT_RETENTION
    with session_scope(session_factory) as session:
        return session.execute(delete(AccountEventORM).where(AccountEventORM.created_at < cutoff)).rowcount or 0


class _Leader:
    """The one gateway process that delivers: it holds a Postgres advisory
    lock on its own connection, and checks before every pass that it still
    does (a dropped connection releases it, and another process may take
    over). Elsewhere than Postgres there is only ever this process."""

    def __init__(self, engine, key: int) -> None:
        self.engine, self.key, self.conn = engine, key, None

    def acquire(self) -> bool:
        if self.engine.dialect.name != "postgresql":
            return True
        conn = self.engine.connect()
        try:
            got = bool(conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": self.key}).scalar())
            conn.commit()
        except Exception:
            conn.close()
            raise
        if not got:
            conn.close()
            return False
        self.conn = conn
        return True

    def still_held(self) -> bool:
        if self.engine.dialect.name != "postgresql":
            return True
        try:
            n = self.conn.execute(text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted AND pid = pg_backend_pid()"
            )).scalar()
            self.conn.commit()
            return bool(n)
        except Exception:  # noqa: BLE001 - the connection is gone, and the lock with it
            return False

    def release(self) -> None:
        if self.conn is None:
            return
        try:
            self.conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": self.key})
            self.conn.commit()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self.conn.close()
            self.conn = None


async def webhook_loop() -> None:
    """Background coroutine started with the gateway. One process delivers
    (_Leader); the others check every minute whether to take over. Every few
    seconds the webhooks with something to send are handed to workers; every
    hour events older than 30 days are pruned. On losing the lock, deliveries
    under way finish before another process can start the same webhooks."""
    from greencompute_gateway.application.services import service

    if os.environ.get("GREENCOMPUTE_WEBHOOKS_ENABLED", "1").strip().lower() in ("0", "false", "no"):
        log.info("webhooks disabled")
        return
    every = max(2, int(os.environ.get("GREENCOMPUTE_WEBHOOK_INTERVAL_SECONDS", "5") or 5))
    sf, engine = service.repository.session_factory, service.repository.engine
    deliverer = Deliverer(sf)
    await asyncio.sleep(20)  # let startup settle
    while True:
        leader = _Leader(engine, _PASS_LOCK_KEY)
        stopping = False
        try:
            if not await asyncio.to_thread(leader.acquire):
                await asyncio.sleep(60)
                continue
            log.info("webhook delivery starting in this process (every %ds)", every)
            last_prune = 0.0
            while await asyncio.to_thread(leader.still_held):
                try:
                    await asyncio.to_thread(deliverer.submit_due)
                    if time.monotonic() - last_prune >= 3600:
                        last_prune = time.monotonic()
                        pruned = await asyncio.to_thread(prune_events, sf)
                        if pruned:
                            log.info("webhooks: pruned %d event(s) older than 30 days", pruned)
                except Exception:  # noqa: BLE001
                    log.exception("webhooks: delivery pass failed")
                await asyncio.sleep(every)
            log.warning("webhooks: lost the delivery lock; finishing deliveries under way")
        except asyncio.CancelledError:
            stopping = True  # shutting down: the process (and its deliveries) ends anyway
            raise
        except Exception:  # noqa: BLE001
            log.exception("webhooks: delivery loop failed; starting again")
        finally:
            if not stopping:
                for _ in range(int(TURN_SECONDS + 30)):
                    if deliverer.idle():
                        break
                    await asyncio.sleep(1)
            leader.release()
        await asyncio.sleep(every)
