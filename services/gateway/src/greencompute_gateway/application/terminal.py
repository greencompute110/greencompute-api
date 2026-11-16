"""The website's terminal into a rental (request #34: "Setting up SSH keys is
the hardest step for anyone non-technical").

A browser can't put an API key on a WebSocket, so it first asks for a ticket
(POST /platform/deployments/{id}/terminal, with the key, like any request)
and opens the WebSocket with that. A ticket works once, for TICKET_TTL_SECONDS,
for the user, rental and key it was made for. Only its hash is stored, in
key_value_state, so any gateway process can take it and none can take it
twice.

The gateway then relays the socket to the node agent running the pod
(/pods/{id}/terminal, with the node secret), byte for byte, counting bytes
but never looking at them, and ends it after IDLE_SECONDS with no input and
no output, or MAX_SESSION_SECONDS in all. Frames: binary are terminal bytes
both ways; text are JSON control messages (see `relay`).

Open terminals are counted per process, as the exec route counts commands:
the gateway runs as one. Every await here is real async I/O; database calls
are made by the caller in the thread pool.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode

from greencompute_persistence import session_scope
from greencompute_persistence.orm import APIKeyORM, KeyValueStateORM
from sqlalchemy import delete, select

log = logging.getLogger(__name__)

TICKET_TTL_SECONDS = 60
PER_USER = 3
TOTAL = 100  # file descriptors and buffers on this process, whoever asks
IDLE_SECONDS = 15 * 60
MAX_SESSION_SECONDS = 4 * 3600
IDLE_WARNING_SECONDS = 60
SESSION_WARNING_SECONDS = 5 * 60
MAX_MESSAGE_BYTES = 1024 * 1024
RECHECK_SECONDS = 60  # a deleted key, a blocked account or a stopped rental ends an open terminal this soon
NODE_OPEN_TIMEOUT = 15.0
NODE_READY_TIMEOUT = 30.0
SHELLS = ("auto", "bash", "sh")
MIN_SIZE, MAX_COLS, MAX_ROWS = 2, 500, 200

_TICKET_PREFIX = "term:"
_TICKET_SHAPE = re.compile(r"[A-Za-z0-9_-]{43}")  # secrets.token_urlsafe(32)


# --- tickets out of the logs ----------------------------------------------------------

_TICKET_IN_TEXT = re.compile(r"(ticket=)[^&\s\"']+")


def redact_tickets(text: str) -> str:
    return _TICKET_IN_TEXT.sub(r"\1[redacted]", text) if "ticket=" in text else text


class RedactTickets(logging.Filter):
    """uvicorn logs each WebSocket handshake with its query string
    (`"WebSocket /platform/deployments/<id>/terminal?ticket=..." [accepted]`,
    or 403), and every request on its access log. A ticket is spent within
    a second, but not one in a log line someone reads first: blank it.
    Argument by argument, so uvicorn's access formatter (which unpacks the
    arguments) still works."""

    def filter(self, record: logging.LogRecord) -> bool:
        with contextlib.suppress(Exception):
            if isinstance(record.msg, str):
                record.msg = redact_tickets(record.msg)
            if isinstance(record.args, tuple):
                record.args = tuple(redact_tickets(a) if isinstance(a, str) else a for a in record.args)
            elif isinstance(record.args, dict):
                record.args = {k: redact_tickets(v) if isinstance(v, str) else v for k, v in record.args.items()}
            color = record.__dict__.get("color_message")
            if isinstance(color, str):
                record.__dict__["color_message"] = redact_tickets(color)
        return True


def install_log_redaction() -> None:
    """On uvicorn's loggers; once, however often it is called."""
    for name in ("uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactTickets) for f in logger.filters):
            logger.addFilter(RedactTickets())


def enabled() -> bool:
    """GREENCOMPUTE_TERMINAL_ENABLED=false turns the terminal off (501)
    without a code change, e.g. until nginx passes WebSockets through."""
    return (os.environ.get("GREENCOMPUTE_TERMINAL_ENABLED") or "true").strip().lower() not in ("0", "false", "no", "off")


# --- tickets ---------------------------------------------------------------------


@dataclass(frozen=True)
class Ticket:
    user_id: str
    key_id: str
    deployment_id: str
    shell: str
    expires_at: datetime


def _row_key(ticket: str) -> str:
    return _TICKET_PREFIX + hashlib.sha256(ticket.encode()).hexdigest()[:48]


def issue_ticket(session_factory, *, user_id: str, key_id: str, deployment_id: str, shell: str,
                 now: datetime | None = None) -> tuple[str, datetime]:
    """A new ticket and when it stops working. Tickets nobody used are
    dropped here, so the table holds no more than a minute's worth."""
    now = now or datetime.now(UTC)
    expires = now + timedelta(seconds=TICKET_TTL_SECONDS)
    ticket = secrets.token_urlsafe(32)
    record = {"user_id": user_id, "key_id": key_id, "deployment_id": deployment_id, "shell": shell,
              "expires_at": expires.isoformat()}
    with session_scope(session_factory) as session:
        session.execute(delete(KeyValueStateORM).where(
            KeyValueStateORM.key.like(_TICKET_PREFIX + "%"),
            KeyValueStateORM.updated_at < now - timedelta(seconds=TICKET_TTL_SECONDS)))
        session.add(KeyValueStateORM(key=_row_key(ticket), value=json.dumps(record), updated_at=now))
    return ticket, expires


def consume_ticket(session_factory, ticket: str, *, now: datetime | None = None) -> Ticket | None:
    """The ticket's record, taken so nobody can use it again; None if it is
    unknown, already used or expired. Under the row's lock: of two requests
    with the same ticket, the second finds it gone."""
    if not well_formed(ticket):
        return None
    now = now or datetime.now(UTC)
    key = _row_key(ticket)
    with session_scope(session_factory) as session:
        value = session.execute(
            select(KeyValueStateORM.value).where(KeyValueStateORM.key == key).with_for_update()).scalar()
        if value is None:
            return None
        # Whoever deletes the row has the ticket: a second request deletes
        # nothing (Postgres waits on the row lock, SQLite on the write lock).
        if session.execute(delete(KeyValueStateORM).where(KeyValueStateORM.key == key)).rowcount != 1:
            return None
    try:
        record = json.loads(value or "{}")
        expires = datetime.fromisoformat(record["expires_at"])
        out = Ticket(user_id=record["user_id"], key_id=record["key_id"], deployment_id=record["deployment_id"],
                     shell=record.get("shell") or "auto", expires_at=expires)
    except (ValueError, KeyError, TypeError):
        return None
    return out if now < out.expires_at else None


def well_formed(ticket: str) -> bool:
    """Shaped like a ticket we issue: anything else isn't looked up."""
    return bool(ticket) and _TICKET_SHAPE.fullmatch(ticket) is not None


def key_still_allows(session_factory, key_id: str, user_id: str) -> bool:
    """The key the ticket was made with still exists, is the user's, and may
    still open a terminal (full access, or the `rentals` scope)."""
    with session_scope(session_factory) as session:
        row = session.execute(select(APIKeyORM.user_id, APIKeyORM.scopes).where(APIKeyORM.key_id == key_id)).first()
    if row is None or row[0] != user_id:
        return False
    scopes = row[1] or []
    return not scopes or "rentals" in scopes


# --- who may connect, and how many ----------------------------------------------


def allowed_origins() -> list[str]:
    """The website's origins: GREENCOMPUTE_TERMINAL_ALLOWED_ORIGINS, else the
    CORS list the website is already allowed by (as main.py reads it)."""
    raw = (os.environ.get("GREENCOMPUTE_TERMINAL_ALLOWED_ORIGINS") or os.environ.get("GREENCOMPUTE_CORS_ALLOW_ORIGINS")
           or "http://localhost:3000,http://127.0.0.1:3000")
    return [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]


def origin_allowed(origin: str | None) -> bool:
    """No Origin: not a browser (an agent's own client), which has to have
    the ticket anyway. A browser page on any other site is refused, so it
    can't ride a ticket it somehow got (cross-site WebSocket hijacking)."""
    if origin is None:
        return True
    allowed = allowed_origins()
    return allowed == ["*"] or origin.strip().rstrip("/") in allowed


_lock = threading.Lock()
_open: dict[str, int] = {}
_total = 0


def open_count(user_id: str) -> int:
    with _lock:
        return _open.get(user_id, 0)


def take_slot(user_id: str) -> str | None:
    """Count one more open terminal; the reason it can't be, or None."""
    global _total
    with _lock:
        if _open.get(user_id, 0) >= PER_USER:
            return f"at most {PER_USER} terminals can be open at once on an account; close one first"
        if _total >= TOTAL:
            return "too many terminals are open right now; try again in a minute"
        _open[user_id] = _open.get(user_id, 0) + 1
        _total += 1
        return None


def release_slot(user_id: str) -> None:
    global _total
    with _lock:
        left = _open.get(user_id, 0) - 1
        if left > 0:
            _open[user_id] = left
        else:
            _open.pop(user_id, None)
        _total = max(0, _total - 1)


def clamp_size(cols: object, rows: object, default: tuple[int, int] = (80, 24)) -> tuple[int, int]:
    def num(value: object, fallback: int, top: int) -> int:
        if isinstance(value, bool):
            return fallback
        try:
            n = int(value)  # type: ignore[call-overload]
        except (TypeError, ValueError, OverflowError):  # OverflowError: JSON's Infinity
            return fallback
        return max(MIN_SIZE, min(n, top))
    return num(cols, default[0], MAX_COLS), num(rows, default[1], MAX_ROWS)


# --- the machine's side -----------------------------------------------------------


class NodeUnavailableError(Exception):
    """No terminal could be opened on the machine (unreachable, refused)."""


class NodeClosedError(Exception):
    def __init__(self, code: int, reason: str = "") -> None:
        super().__init__(f"node closed the terminal ({code})")
        self.code, self.reason = code, reason


class NodeLink:
    """The node agent's end of a terminal: send/recv frames (bytes or text),
    NodeClosedError once it is closed."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def send(self, data: bytes | str) -> None:
        from websockets.exceptions import ConnectionClosed

        try:
            await self._conn.send(data)
        except ConnectionClosed as exc:
            raise NodeClosedError(exc.rcvd.code if exc.rcvd else 1006) from exc

    async def recv(self) -> bytes | str:
        from websockets.exceptions import ConnectionClosed

        try:
            return await self._conn.recv()
        except ConnectionClosed as exc:
            raise NodeClosedError(exc.rcvd.code if exc.rcvd else 1006, exc.rcvd.reason if exc.rcvd else "") from exc

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._conn.close()


def node_terminal_url(base_url: str, deployment_id: str, *, cols: int, rows: int, shell: str) -> str:
    """ws(s)://<node>/pods/<id>/terminal?..., from the node's http(s) address."""
    base = base_url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    query = urlencode({"cols": cols, "rows": rows, "shell": shell})
    return f"{base}/pods/{quote(deployment_id, safe='')}/terminal?{query}"


async def open_node_link(url: str, secret: str | None) -> NodeLink:
    """Connect to the node agent's terminal with the node secret."""
    try:
        from websockets.asyncio.client import connect
        from websockets.exceptions import InvalidStatus, WebSocketException
    except ImportError as exc:  # pragma: no cover - a gateway without the dependency
        raise NodeUnavailableError("the gateway lacks the websockets package") from exc
    headers = {"X-Agent-Auth": secret} if secret else {}
    try:
        conn = await connect(
            url, additional_headers=headers, open_timeout=NODE_OPEN_TIMEOUT, max_size=MAX_MESSAGE_BYTES,
            ping_interval=20, ping_timeout=20, close_timeout=5, compression=None,
            proxy=None,  # nodes are reached directly, never through an environment proxy
            user_agent_header="greencompute-gateway")
    except InvalidStatus as exc:
        # The node refused the handshake: no or wrong node secret, or an old
        # node agent without the route (404).
        status = getattr(getattr(exc, "response", None), "status_code", None)
        raise NodeUnavailableError(f"the machine refused the terminal (HTTP {status})") from exc
    except (OSError, TimeoutError, WebSocketException) as exc:
        raise NodeUnavailableError(f"couldn't reach the machine ({type(exc).__name__})") from exc
    return NodeLink(conn)


#: The node's refusal (its error code) as what the customer is told:
#: (close code, error code, message).
_NODE_REFUSALS = {
    "conflict": (4409, "conflict", "the rental isn't running on its machine"),
    "not_found": (4409, "conflict", "the rental isn't running on its machine"),
    "terminal_limit": (4429, "terminal_limit",
                       "the machine running this rental has the most terminals open it allows; close one first"),
    "terminal_session_limit": (4410, "terminal_session_limit",
                               "the terminal reached its time limit; open a new one"),
    "invalid_request": (4400, "invalid_request", "the machine refused the terminal's settings"),
}
#: The node's notices passed on to the customer, in our words.
_NODE_NOTICES = {
    "input_dropped": "The program in your terminal isn't reading input, so some of what you typed or pasted was "
                     "dropped. Press Ctrl-C to stop it, or wait for it to finish.",
}
_UNAVAILABLE = (4503, "unavailable", "the machine running this rental didn't answer; try again shortly")


def node_refusal(message: Any) -> tuple[int, str, str]:
    code = message.get("code") if isinstance(message, dict) else None
    return _NODE_REFUSALS.get(code, _UNAVAILABLE)


async def wait_ready(node: NodeLink) -> tuple[int, str, str] | None:
    """Wait for the node's `ready`; why it wasn't, as (close code, error
    code, message), or None once it is."""
    try:
        async with asyncio.timeout(NODE_READY_TIMEOUT):
            while True:
                frame = await node.recv()
                if isinstance(frame, bytes):
                    continue
                with contextlib.suppress(ValueError):
                    message = json.loads(frame)
                    if isinstance(message, dict) and message.get("type") == "ready":
                        return None
                    if isinstance(message, dict) and message.get("type") == "error":
                        return node_refusal(message)
    except TimeoutError:
        return _UNAVAILABLE
    except NodeClosedError:
        return _UNAVAILABLE


# --- the session ------------------------------------------------------------------


@dataclass
class Session:
    session_id: str = field(default_factory=lambda: secrets.token_hex(6))
    started: float = field(default_factory=time.monotonic)
    last_activity: float = field(default_factory=time.monotonic)
    bytes_in: int = 0  # from the customer to the pod
    bytes_out: int = 0  # from the pod to the customer
    end_reason: str = "error"

    def touch(self) -> None:
        self.last_activity = time.monotonic()


async def relay(client: Any, node: NodeLink, session: Session,
                recheck: Callable[[], Awaitable[tuple[int, str, str] | None]] | None = None,
                ) -> tuple[int, str, str] | None:
    """Relay until one side ends it, the idle timeout or the session limit;
    returns how to close the customer's socket, as (close code, error code,
    message), or None when they are gone already. Sets session.end_reason.

    From the customer: binary (keystrokes), or JSON {"type": "input", "data"},
    {"type": "resize", "cols", "rows"}, {"type": "ping"} (answered with
    {"type": "pong"}; not activity). To them: binary (output), and JSON
    {"type": "exit", "code"} and {"type": "notice", "code", "message",
    "seconds_left"} (idle_warning, session_ending; input_dropped, from the
    node, without seconds_left). Every RECHECK_SECONDS,
    `recheck` says whether the terminal may stay open (None) or why not."""
    send_lock = asyncio.Lock()

    async def to_client(text: str) -> None:
        async with send_lock:
            await client.send_text(text)

    async def from_client() -> tuple[str, Any]:
        while True:
            message = await client.receive()
            if message["type"] == "websocket.disconnect":
                return "closed", None
            data, text = message.get("bytes"), message.get("text")
            if len(data if data is not None else (text or "").encode()) > MAX_MESSAGE_BYTES:
                return "too_big", None
            if data is not None:
                session.bytes_in += len(data)
                session.touch()
                await node.send(data)
                continue
            try:
                ctl = json.loads(text or "")
            except ValueError:
                continue
            if not isinstance(ctl, dict):
                continue
            kind = ctl.get("type")
            if kind == "ping":
                await to_client('{"type": "pong"}')
            elif kind == "resize":
                cols, rows = clamp_size(ctl.get("cols"), ctl.get("rows"))
                session.touch()
                await node.send(json.dumps({"type": "resize", "cols": cols, "rows": rows}))
            elif kind == "input" and isinstance(ctl.get("data"), str):
                raw = ctl["data"].encode("utf-8", "replace")  # a lone surrogate (a broken paste) isn't an error
                session.bytes_in += len(raw)
                session.touch()
                await node.send(raw)

    async def from_node() -> tuple[str, Any]:
        while True:
            frame = await node.recv()
            if isinstance(frame, bytes):
                session.bytes_out += len(frame)
                session.touch()
                async with send_lock:
                    await client.send_bytes(frame)
                continue
            with contextlib.suppress(ValueError):
                message = json.loads(frame)
                if isinstance(message, dict) and message.get("type") == "exit":
                    return "exit", message.get("code")
                if isinstance(message, dict) and message.get("type") == "error":
                    return "node_error", message
                if isinstance(message, dict) and message.get("type") == "notice" and message.get("code") in _NODE_NOTICES:
                    await to_client(json.dumps({"type": "notice", "code": message["code"],
                                                "message": _NODE_NOTICES[message["code"]]}))

    async def watchdog() -> tuple[str, Any]:
        ends = session.started + MAX_SESSION_SECONDS
        next_check = session.started + RECHECK_SECONDS
        warned_end = warned_idle = False
        while True:
            now = time.monotonic()
            idle_at = session.last_activity + IDLE_SECONDS
            if now >= ends:
                return "session_limit", None
            if now >= idle_at:
                return "idle", None
            if recheck is not None and now >= next_check:
                next_check = now + RECHECK_SECONDS
                problem = await recheck()
                if problem is not None:
                    return "revoked", problem
            if not warned_end and ends - now <= SESSION_WARNING_SECONDS:
                warned_end = True
                await to_client(json.dumps({
                    "type": "notice", "code": "session_ending", "seconds_left": int(ends - now),
                    "message": f"This terminal closes in {_minutes(ends - now)} (the {_hours(MAX_SESSION_SECONDS)} "
                               "limit). Open a new one to carry on."}))
            if idle_at - now > IDLE_WARNING_SECONDS:
                warned_idle = False  # there was activity since the last warning
            elif not warned_idle:
                warned_idle = True
                await to_client(json.dumps({
                    "type": "notice", "code": "idle_warning", "seconds_left": int(idle_at - now),
                    "message": f"Nothing has happened in this terminal for a while; it closes in "
                               f"{_minutes(idle_at - now)} unless you type something."}))
            # Wake for whatever is next (at least once a second: typing moves idle_at).
            upcoming = [ends, idle_at, next_check]
            upcoming += [] if warned_end else [ends - SESSION_WARNING_SECONDS]
            upcoming += [] if warned_idle else [idle_at - IDLE_WARNING_SECONDS]
            await asyncio.sleep(max(0.05, min([1.0] + [t - now for t in upcoming if t > now])))

    tasks = [asyncio.create_task(t) for t in (from_client(), from_node(), watchdog())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        await _end_tasks(tasks)
    finished = next(t for t in tasks if t in done)
    exc = finished.exception()
    if isinstance(exc, NodeClosedError):
        session.end_reason = f"node_closed_{exc.code}"
        return _UNAVAILABLE[0], _UNAVAILABLE[1], "the connection to the machine was lost; open the terminal again"
    if exc is not None:
        # The customer's socket broke while we wrote to it: nobody to tell.
        session.end_reason = "client_gone"
        if not isinstance(exc, (OSError, RuntimeError)) and type(exc).__name__ != "WebSocketDisconnect":
            # The type only: an exception's text can quote what was typed.
            log.warning("terminal %s: relay ended by %s", session.session_id, type(exc).__name__)
        return None
    outcome, detail = finished.result()
    session.end_reason = outcome
    if outcome == "closed":
        return None
    if outcome == "exit":
        with contextlib.suppress(Exception):
            await to_client(json.dumps({"type": "exit", "code": detail if isinstance(detail, int) else None}))
        return 1000, "", ""
    if outcome == "revoked":
        return detail
    if outcome == "too_big":
        return 1009, "invalid_request", f"a message was over {MAX_MESSAGE_BYTES // (1024 * 1024)} MiB; send large input in pieces"
    if outcome == "idle":
        return 4408, "terminal_idle", f"the terminal closed after {_minutes(IDLE_SECONDS)} with no input or output"
    if outcome == "session_limit":
        return 4410, "terminal_session_limit", f"the terminal reached its {_hours(MAX_SESSION_SECONDS)} limit; open a new one"
    return node_refusal(detail)


async def _end_tasks(tasks: list[asyncio.Task]) -> None:
    """Cancel them and let them finish. asyncio.wait, not gather: a gather
    cancelled with its caller (the server stopping) can escape the server's
    cancel scope as a stray CancelledError."""
    for task in tasks:
        task.cancel()
    await asyncio.wait(tasks)
    for task in tasks:
        if not task.cancelled():
            task.exception()  # retrieved, so it isn't logged as never retrieved


async def refuse(websocket: Any, close_code: int, code: str, message: str) -> None:
    """Tell the customer why, as an `error` message, then close with the code."""
    with contextlib.suppress(Exception):
        if code:
            await websocket.send_text(json.dumps({"type": "error", "code": code, "message": _sentence(message)}))
        await websocket.close(code=close_code, reason=code[:120])


def _sentence(text: str) -> str:
    text = (text or "").strip()
    if text and text[0].islower():
        text = text[0].upper() + text[1:]
    return text if not text or text.endswith((".", "!", "?")) else text + "."


def _minutes(seconds: float) -> str:
    n = max(1, round(seconds / 60))
    return "a minute" if n == 1 else f"{n} minutes"


def _hours(seconds: float) -> str:
    n = max(1, round(seconds / 3600))
    return f"{n}-hour"
