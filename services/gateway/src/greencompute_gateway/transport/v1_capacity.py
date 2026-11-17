"""Room for /v1 inference in the gateway, which is one process.

A non-streamed completion holds a thread for as long as the model
generates (two minutes, or half an hour for the largest reasoning models).
Every sync route used to share AnyIO's one pool of 40 threads, so one
account sending ~40 long requests (a few dollars of balance) pinned them all
and every customer's /v1 and /platform calls queued behind it (review H3).
Now:

- /v1 completions run on their own threads (`run_v1`, `v1_stream`), so they
  can never take the threads /platform needs; main.py also raises the shared
  pool's size;
- each account has at most V1_MAX_IN_FLIGHT_PER_ACCOUNT of them in progress
  (`claim_slot`); one more is answered at once with 429 `rate_limited`.
"""
from __future__ import annotations

import functools
import os
import threading
import time
import uuid
import weakref
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import anyio
import anyio.to_thread
from starlette.responses import StreamingResponse

from greencompute_gateway.transport.errors import coded

#: Threads for /v1 completions, apart from the pool every other route uses.
V1_THREADS = int(os.getenv("GREENCOMPUTE_V1_THREADS", "120"))
#: /v1 completions one account may have in progress at once.
V1_MAX_IN_FLIGHT_PER_ACCOUNT = int(os.getenv("GREENCOMPUTE_V1_MAX_IN_FLIGHT_PER_ACCOUNT", "8"))
#: A slot never released (a stream nobody read, a crash) stops counting after
#: this long, so an account can't be locked out by a leak.
SLOT_MAX_AGE_SECONDS = 2 * 3600 + 600
#: The shared pool's size for every other sync route (AnyIO's default is 40).
DEFAULT_THREADS = int(os.getenv("GREENCOMPUTE_THREADPOOL_THREADS", "100"))

_slots: dict[str, dict[str, float]] = {}
_slots_lock = threading.Lock()
_limiters: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


class Slot:
    """One /v1 request in progress for an account; release() is idempotent."""

    def __init__(self, account: str) -> None:
        self.account = account
        self.slot_id = uuid.uuid4().hex

    def release(self) -> None:
        with _slots_lock:
            held = _slots.get(self.account)
            if held is not None:
                held.pop(self.slot_id, None)
                if not held:
                    _slots.pop(self.account, None)


class _NoSlot(Slot):
    """For a request that isn't limited (the caller's own private model)."""

    def __init__(self) -> None:
        self.account = ""
        self.slot_id = ""

    def release(self) -> None:
        pass


NO_SLOT = _NoSlot()


def claim_slot(api_key: Any) -> Slot:
    """Count one more /v1 request in progress for the key's account, or raise
    429 `rate_limited` when it already has V1_MAX_IN_FLIGHT_PER_ACCOUNT.
    Only requests to shared models count; a caller's own private model
    isn't limited (routes._complete doesn't claim one for it)."""
    account = getattr(api_key, "user_id", None) or f"key:{getattr(api_key, 'key_id', None)}"
    now = time.monotonic()
    with _slots_lock:
        held = _slots.setdefault(account, {})
        for slot_id, started in list(held.items()):
            if now - started > SLOT_MAX_AGE_SECONDS:
                held.pop(slot_id, None)
        if len(held) >= V1_MAX_IN_FLIGHT_PER_ACCOUNT:
            raise coded(429, "rate_limited", {
                "message": f"this account already has {len(held)} requests in progress to shared models, the most "
                           f"at once; send more as those finish (stream long answers to get them sooner). Your own "
                           f"private models aren't limited this way",
                "limit": V1_MAX_IN_FLIGHT_PER_ACCOUNT,
                "in_flight": len(held),
                "retry_after_seconds": 2,
            }, {"Retry-After": "2"})
        slot = Slot(account)
        held[slot.slot_id] = now
    return slot


def in_flight(account: str) -> int:
    with _slots_lock:
        return len(_slots.get(account, {}))


def _limiter() -> anyio.CapacityLimiter:
    """The /v1 threads' limiter, one per event loop (AnyIO's primitives
    belong to the loop that made them)."""
    import asyncio

    loop = asyncio.get_running_loop()
    limiter = _limiters.get(loop)
    if limiter is None:
        limiter = _limiters[loop] = anyio.CapacityLimiter(V1_THREADS)
    return limiter


async def run_v1(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a /v1 handler's blocking work on the /v1 threads."""
    return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs), limiter=_limiter())


_END = object()


def _close(body: Iterator[str]) -> None:
    close = getattr(body, "close", None)
    if close is not None:
        close()


def v1_stream(body: Iterator[str], slot: Slot | None) -> AsyncIterator[str]:
    """A streamed answer for StreamingResponse, each line read on a /v1
    thread (Starlette would read it on the shared pool). Closing it closes
    `body` (which bills what was served) and frees the slot; so does
    garbage collection, should it never be read at all."""

    async def relay() -> AsyncIterator[str]:
        try:
            while True:
                line = await anyio.to_thread.run_sync(next, body, _END, limiter=_limiter())
                if line is _END:
                    break
                yield line
        finally:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(_close, body, limiter=_limiter())
            if slot is not None:
                slot.release()

    stream = relay()
    if slot is not None:
        weakref.finalize(stream, slot.release)
    return stream


class ClosingStreamingResponse(StreamingResponse):
    """A StreamingResponse that always closes its body when it is done,
    including when the caller hung up. Starlette leaves an unfinished body
    iterator to the garbage collector, and one caught in a reference cycle
    (an exception's traceback holds it) kept its in-flight slot, its node
    connection and its billing waiting for minutes to hours (review H1)."""

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            aclose = getattr(self.body_iterator, "aclose", None)
            if aclose is not None:
                with anyio.CancelScope(shield=True):
                    await aclose()


def raise_default_pool() -> None:
    """Called at startup: the shared pool's size for every other sync route."""
    anyio.to_thread.current_default_thread_limiter().total_tokens = max(DEFAULT_THREADS, 40)
