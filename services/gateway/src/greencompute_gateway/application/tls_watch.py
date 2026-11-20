"""Tell the team before a TLS certificate on our own hosts expires.

On 2026-11-20 the api/control/validator certificate expired. certbot's
renewals had failed twice a day for 30 days (a duplicate nginx directive) and
nothing said so, until every HTTPS call to the API failed. This checks the
certificates nginx actually serves every few hours and alerts the team
(infrastructure/alerts: the support inbox, plus Slack/Telegram when
configured) when one is invalid, can't be checked, or has less than
GREENCOMPUTE_TLS_WARN_DAYS (default 14) left. Let's Encrypt renews at 30 days
left, so 14 means renewal has been failing for over two weeks.

Hosts: api/control/validator.green-compute.com (GREENCOMPUTE_TLS_WATCH_HOSTS,
comma separated, replaces them), plus every model address in our zone
(<label>.inference.green-compute.com, the only names gc-model-certs issues)
with a deployment ready for over an hour.

When a host isn't OK it's checked again after RECHECK (15 minutes), not the
full interval, so "could not check" (UNREACHABLE_STREAK checks in a row) and a
model address's "not valid" (two in a row: its certificate may still be on its
way) alert within the hour. A host alerts again only after REPEAT, unless its
problem changes (expiring → expired alerts at once).

Inside docker on the validator host, the host's public IP has no route back
to it. nginx is then reached through the container's default gateway instead,
still with the host name for SNI and verification, so the check sees exactly
what clients see.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import socket
import ssl
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from greencompute_persistence import session_scope
from greencompute_persistence.orm import DeploymentORM, WorkloadORM

log = logging.getLogger(__name__)

DEFAULT_HOSTS = ("api.green-compute.com", "control.green-compute.com", "validator.green-compute.com")
MODEL_HOST_MIN_READY = timedelta(hours=1)
REPEAT = timedelta(hours=24)  # the same problem on a host alerts again at most this often
RECHECK = timedelta(minutes=15)  # next pass when any host isn't OK
UNREACHABLE_STREAK = 3  # checks in a row before "could not check" is worth an alert
MODEL_INVALID_STREAK = 2  # a model address's certificate may still be being issued
MAX_LINES = 20  # alert channels cut long messages (Telegram rejects >4096 chars)
_TIMEOUT_SECONDS = 8.0

OK, EXPIRING, INVALID, UNREACHABLE = "ok", "expiring", "invalid", "unreachable"

# A connection that drops during the handshake says nothing about the
# certificate (an nginx reload, workers out of file descriptors): treated like
# an unreachable host, not an invalid certificate.
_TRANSIENT_TLS = (ssl.SSLEOFError, ssl.SSLZeroReturnError, ssl.SSLSyscallError)


@dataclass
class Result:
    host: str
    status: str
    expires_at: datetime | None = None
    detail: str = ""


@dataclass
class Watch:
    """What the previous passes saw: kept in memory, so a gateway restart at
    worst repeats an alert once."""
    streak: dict[str, tuple[str, int]] = field(default_factory=dict)  # host -> (status, passes in a row)
    last_alert: dict[str, tuple[str, datetime]] = field(default_factory=dict)  # host -> (status, when)


_WATCH = Watch()


def warn_days() -> int:
    try:
        return max(1, int(os.environ.get("GREENCOMPUTE_TLS_WARN_DAYS") or 14))
    except ValueError:
        return 14


def base_hosts() -> list[str]:
    raw = os.environ.get("GREENCOMPUTE_TLS_WATCH_HOSTS")
    if raw is None or not raw.strip():
        return list(DEFAULT_HOSTS)
    return [h.strip().lower() for h in raw.split(",") if h.strip()]


def _model_host_pattern() -> re.Pattern[str]:
    zone = (os.environ.get("GREENCOMPUTE_MODEL_INGRESS_DOMAIN") or "inference.green-compute.com").strip().lower()
    return re.compile(r"[a-z0-9-]{1,63}\." + re.escape(zone))


def default_gateway(route_file: str = "/proc/net/route") -> str | None:
    """The default route's gateway (IPv4), or None. In a container that is
    the docker host."""
    try:
        with open(route_file) as f:
            next(f, None)  # header
            for line in f:
                parts = line.split()
                if len(parts) >= 3 and parts[1] == "00000000" and parts[2] != "00000000":
                    raw = bytes.fromhex(parts[2])[::-1]  # little-endian
                    return socket.inet_ntoa(raw)
    except (OSError, ValueError):
        return None
    return None


def fetch_expiry(host: str, connect: str | None = None, timeout: float = _TIMEOUT_SECONDS) -> datetime:
    """The expiry of the certificate served for `host`, fully verified.
    Raises ssl.SSLError when it isn't valid, OSError when unreachable."""
    ctx = ssl.create_default_context()
    with socket.create_connection((connect or host, 443), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            cert = tls.getpeercert() or {}
    not_after = cert.get("notAfter")
    if not not_after:
        raise ssl.SSLError(f"no expiry date in the certificate for {host}")
    return datetime.fromtimestamp(ssl.cert_time_to_seconds(not_after), UTC)


def check_host(host: str, now: datetime | None = None, fetch: Callable[..., datetime] = fetch_expiry,
               gateway: Callable[[], str | None] = default_gateway) -> Result:
    now = now or datetime.now(UTC)
    targets: list[str | None] = [None]
    unreachable = ""
    while targets:
        connect = targets.pop(0)
        try:
            expires = fetch(host, connect)
        except ssl.SSLCertVerificationError as exc:  # before SSLError/OSError: both are its parents
            return Result(host, INVALID, detail=exc.verify_message or str(exc))
        except _TRANSIENT_TLS as exc:  # before SSLError: they are its children
            unreachable = f"{type(exc).__name__}: {exc}"
        except ssl.SSLError as exc:
            return Result(host, INVALID, detail=str(exc))
        except OSError as exc:
            unreachable = f"{type(exc).__name__}: {exc}"
        else:
            if expires - now < timedelta(days=warn_days()):
                return Result(host, EXPIRING, expires_at=expires)
            return Result(host, OK, expires_at=expires)
        if connect is None:
            gw = gateway()
            if gw:
                targets.append(gw)
    return Result(host, UNREACHABLE, detail=unreachable)


def model_hosts(session_factory, now: datetime) -> list[str]:
    """Our model addresses with a deployment ready for over an hour."""
    pattern = _model_host_pattern()
    with session_scope(session_factory) as session:
        rows = session.execute(
            select(WorkloadORM.ingress_host)
            .join(DeploymentORM, DeploymentORM.workload_id == WorkloadORM.workload_id)
            .where(
                WorkloadORM.ingress_host.is_not(None),
                DeploymentORM.state == "ready",
                DeploymentORM.ready_at.is_not(None),
                DeploymentORM.ready_at < now - MODEL_HOST_MIN_READY,
            )
            .distinct()
        ).scalars().all()
    return sorted({h.strip().lower() for h in rows if h and pattern.fullmatch(h.strip().lower())})


def watched_hosts(session_factory, now: datetime) -> list[str]:
    hosts = base_hosts()
    return hosts + [h for h in model_hosts(session_factory, now) if h not in hosts]


def _days_left(expires: datetime, now: datetime) -> int:
    return max(0, (expires - now) // timedelta(days=1))


def _line(r: Result, now: datetime, streak: int) -> str:
    if r.status == EXPIRING:
        assert r.expires_at is not None
        return (f"{r.host}: certificate expires in {_days_left(r.expires_at, now)} days "
                f"({r.expires_at:%Y-%m-%d %H:%M} UTC) and has not been renewed")
    if r.status == INVALID:
        return f"{r.host}: certificate is not valid ({r.detail})"
    return f"{r.host}: could not check the certificate for {streak} checks in a row ({r.detail})"


def _needed(r: Result, is_model_host: bool) -> int:
    if r.status == UNREACHABLE:
        return UNREACHABLE_STREAK
    if r.status == INVALID and is_model_host:
        return MODEL_INVALID_STREAK
    return 1


def run_once(session_factory, now: datetime | None = None, check: Callable[..., Result] | None = None,
             send: Callable[[str], None] | None = None, watch: Watch | None = None) -> dict:
    """Check every watched host; alert on what needs attention. Returns what
    was seen and sent, and whether to check again soon, for the loop, logs
    and tests."""
    now = now or datetime.now(UTC)
    check = check or (lambda host: check_host(host, now))
    watch = watch if watch is not None else _WATCH
    if send is None:
        from greencompute_gateway.infrastructure.alerts import send_payment_alert as send

    hosts = base_hosts()
    try:
        models = [h for h in model_hosts(session_factory, now) if h not in hosts]
    except Exception:  # the base hosts don't need the database
        log.exception("tls watch: could not list model addresses; checking the base hosts only")
        models = []

    results: list[Result] = []
    due: list[tuple[Result, int]] = []
    for host in hosts + models:
        try:
            r = check(host)
        except Exception as exc:  # one bad host must never stop the others' alerts
            r = Result(host, UNREACHABLE, detail=f"{type(exc).__name__}: {exc}")
        results.append(r)
        if r.status == OK:
            watch.streak.pop(host, None)
            watch.last_alert.pop(host, None)  # healthy again: a new problem alerts at once
            continue
        prev_status, n = watch.streak.get(host, ("", 0))
        n = n + 1 if prev_status == r.status else 1
        watch.streak[host] = (r.status, n)
        if n < _needed(r, host in models):
            continue
        last = watch.last_alert.get(host)
        if last is not None and last[0] == r.status and now - last[1] < REPEAT:
            continue
        due.append((r, n))

    lines = [_line(r, now, n) for r, n in due]
    if lines:
        shown = lines[:MAX_LINES]
        more = len(lines) - len(shown)
        send(
            "TLS certificates need attention:\n"
            + "\n".join(f"! {line}" for line in shown)
            + (f"\n…and {more} more" if more else "")
            + "\nOn the validator host: certbot renew --dry-run, and read /var/log/letsencrypt/letsencrypt.log"
        )
        for r, _ in due:  # only once sent: a failed send is retried next pass
            watch.last_alert[r.host] = (r.status, now)
    return {"results": results, "alerted": lines, "recheck_soon": any(r.status != OK for r in results)}


def _summary(results: list[Result], now: datetime) -> str:
    parts = []
    for r in results:
        if r.expires_at is not None:
            parts.append(f"{r.host}={_days_left(r.expires_at, now)}d")
        else:
            parts.append(f"{r.host}={r.status}")
    return " ".join(parts)


def _interval() -> int:
    try:
        return max(300, int(os.environ.get("GREENCOMPUTE_TLS_WATCH_INTERVAL_SECONDS") or 21600))
    except ValueError:
        return 21600


async def tls_watch_loop() -> None:
    """Background coroutine started with the gateway. Logs at WARNING: under
    uvicorn the root logger drops INFO, and these lines are the evidence the
    watch is alive (four a day when all is well)."""
    if os.environ.get("GREENCOMPUTE_TLS_WATCH_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
        log.warning("tls watch disabled")
        return
    interval = _interval()
    log.warning("tls watch starting (interval=%ds, warn at %d days)", interval, warn_days())
    await asyncio.sleep(120)  # let startup settle
    from greencompute_gateway.application.billing_service import get_billing_service

    while True:
        sleep = interval
        try:
            out = await asyncio.to_thread(run_once, get_billing_service().repo.session_factory)
            log.warning("tls watch: %s", _summary(out["results"], datetime.now(UTC)))
            if out["alerted"]:
                log.warning("tls watch: alerted on %d host(s)", len(out["alerted"]))
            if out["recheck_soon"]:
                sleep = min(interval, int(RECHECK.total_seconds()))
        except Exception:
            log.exception("tls watch run failed")
            sleep = min(interval, int(RECHECK.total_seconds()))
        await asyncio.sleep(sleep)
