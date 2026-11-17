from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from threading import Lock
from time import time


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    remaining: int
    reset_at: float


class FixedWindowRateLimiter:
    def __init__(self) -> None:
        self._windows: dict[tuple[str, str], tuple[float, int]] = defaultdict(lambda: (0.0, 0))
        self._lock = Lock()

    def check(self, namespace: str, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        now = time()
        bucket = (namespace, key)
        with self._lock:
            window_start, used = self._windows[bucket]
            if now >= window_start + window_seconds:
                window_start = now
                used = 0
            if used >= limit:
                return RateLimitResult(
                    allowed=False,
                    remaining=0,
                    reset_at=window_start + window_seconds,
                )
            used += 1
            self._windows[bucket] = (window_start, used)
            return RateLimitResult(
                allowed=True,
                remaining=max(limit - used, 0),
                reset_at=window_start + window_seconds,
            )


@dataclass(frozen=True)
class BucketResult:
    allowed: bool
    limit: int  # the bucket's size: requests that may be sent at once
    remaining: int  # requests that may be sent now
    reset_seconds: float  # until the bucket is full again
    retry_after_seconds: float  # until the next request is allowed (0 when this one was)


class TokenBucketRateLimiter:
    """`rate` requests a second on average with bursts of up to `burst`, per
    key. In memory, per process: one lock and a little arithmetic per request,
    no database. Buckets that have refilled are dropped once there are more
    than `max_keys`, so memory stays bounded."""

    def __init__(self, max_keys: int = 100_000) -> None:
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, at)
        self._lock = Lock()
        self._max_keys = max_keys

    def take(self, key: str, *, rate: float, burst: int, now: float | None = None) -> BucketResult:
        now = time() if now is None else now
        with self._lock:
            tokens, at = self._buckets.get(key, (float(burst), now))
            tokens = min(float(burst), tokens + max(0.0, now - at) * rate)
            allowed = tokens >= 1.0
            if allowed:
                tokens -= 1.0
            self._buckets[key] = (tokens, now)
            if len(self._buckets) > self._max_keys:
                self._prune(now, rate, burst)
        return BucketResult(
            allowed=allowed,
            limit=burst,
            remaining=int(tokens),
            reset_seconds=(burst - tokens) / rate,
            retry_after_seconds=0.0 if allowed else (1.0 - tokens) / rate,
        )

    def _prune(self, now: float, rate: float, burst: int) -> None:
        full = [k for k, (tokens, at) in self._buckets.items() if tokens + (now - at) * rate >= burst]
        for k in full:
            del self._buckets[k]

    def clear(self) -> None:
        with self._lock:
            self._buckets.clear()
