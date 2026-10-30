"""Credit watch: what an account burns per hour, how long its balance lasts,
and two sweeps for customers whose credit runs out.

- `forecast(user_id)`: the hourly-billed deployments that are READY right now
  (pod and VM rentals and private models; shared catalog models are paid per
  token), what they cost per hour together, and how long the balance lasts
  at that rate. Behind GET /platform/billing/forecast and the emails below.
- `low_credit_once()`: emails a customer once when their credit will run out
  within 24 hours, and once more within 2 hours. A top-up that lifts them back
  above 24 hours (or nothing of theirs running) re-arms both levels.
- `auto_resume_once()`: resumes deployments the customer flagged
  `resume_on_topup` that the meter suspended for an empty balance
  (`suspended_reason == "balance"`), once the balance covers an hour of
  everything that would then be running. Oldest suspension first, one email
  per customer per sweep saying what came back.

What a deployment costs per hour, and whether it is billed at all, comes from
the control plane's `hourly_charge_cents`, the meter's own rule.

Both sweeps run from `credit_watch_loop`, started with the gateway: a sweep
rather than a hook on every top-up path (card, invoice, verified wallet,
review, admin), so no path can forget it. An external API user asked for both
(Oct 2026): "suspended cards sit idle until someone notices".
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from greencompute_persistence import session_scope
from greencompute_persistence.account_events import add_event
from greencompute_persistence.orm import AccountEventORM, CreditWarningORM
from greencompute_protocol import DeploymentRecord, DeploymentState, WorkloadKind, WorkloadSpec

log = logging.getLogger(__name__)

#: `suspended_reason` the meter stamps when it drains a balance (as opposed to
#: "idle", a private model nobody called, which resumes on its next request).
SUSPENDED_FOR_BALANCE = "balance"

#: Email when the balance lasts less than this many hours, each level once.
WARN_LEVELS_HOURS = (24.0, 2.0)

#: Deployments that cost money now or will the moment they are up.
_COMMITTED_STATES = (
    DeploymentState.READY,
    DeploymentState.PULLING,
    DeploymentState.STARTING,
    DeploymentState.SCHEDULED,
    DeploymentState.PENDING,
)
#: A SUSPENDED deployment whose lease is in one of these is being resumed:
#: the control plane flipped its lease back and the machine hasn't reported it
#: running yet. Committed spend, and not a candidate again.
_LIVE_LEASE_STATUSES = ["assigned", "activating", "active"]


def _service():
    from greencompute_gateway.application.services import service

    return service


def _billing_repo():
    """The gateway service's one BillingRepository (its engine and pool);
    never a new engine per sweep or request."""
    return _service().billing_repo


def _notify(session_factory, user_id: str, kind: str, fields: dict, *, event_id: str) -> None:
    from greencompute_gateway.infrastructure.notify_website import notify_user

    notify_user(session_factory, user_id, kind, fields, event_id=event_id)


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=UTC)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _int_env(name: str, default: int, minimum: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        log.warning("%s is not a number; using %d", name, default)
        value = default
    return max(minimum, value)


def _label(deployment: DeploymentRecord, workload: WorkloadSpec | None) -> str:
    """How the customer knows this deployment: "rig-07 (1× rtx4090)", or the
    workload's name when the rental has none of its own."""
    name = deployment.name or ((workload.display_name or workload.name) if workload else deployment.deployment_id[:8])
    gpus = (workload.requirements.supported_gpu_models if workload else None) or []
    count = (workload.requirements.gpu_count if workload else 1) or 1
    return f"{name} ({count}× {gpus[0]})" if gpus else f"{name} ({count} GPU{'s' if count != 1 else ''})"


class _Workloads:
    """One workload lookup per workload id within a sweep."""

    def __init__(self, control_plane) -> None:
        self.cp = control_plane
        self.seen: dict[str, WorkloadSpec | None] = {}

    def get(self, workload_id: str) -> WorkloadSpec | None:
        if workload_id not in self.seen:
            self.seen[workload_id] = self.cp.repository.get_workload(workload_id)
        return self.seen[workload_id]


def _running_entry(control_plane, dep: DeploymentRecord, wl: WorkloadSpec | None, now: datetime) -> dict[str, Any] | None:
    charge = control_plane.hourly_charge_cents(dep, wl, now=now)
    if charge is None:
        return None
    gpus = (wl.requirements.supported_gpu_models if wl else None) or []
    return {
        "deployment_id": dep.deployment_id,
        "name": dep.name or ((wl.display_name or wl.name) if wl else dep.deployment_id[:8]),
        "label": _label(dep, wl),
        "kind": "model" if (wl is not None and wl.kind == WorkloadKind.INFERENCE) else "rental",
        "gpu_model": gpus[0] if gpus else None,
        "gpu_count": (wl.requirements.gpu_count if wl else 1) or 1,
        "cents_per_hour": charge,
        # Its machine isn't reporting: kept, not billed, until it is back.
        "offline": charge == 0,
        "resume_on_topup": bool(dep.resume_on_topup),
    }


def running_by_user(control_plane, *, user_id: str | None = None, now: datetime | None = None) -> dict[str, list[dict[str, Any]]]:
    """Owner -> the READY hourly-billed deployments and what each costs per
    hour. With `user_id`, only that owner's (and only their workloads are
    loaded)."""
    now = now or datetime.now(UTC)
    workloads = _Workloads(control_plane)
    out: dict[str, list[dict[str, Any]]] = {}
    for dep in control_plane.repository.list_deployments_by_state(DeploymentState.READY):
        if not dep.owner_user_id or (user_id is not None and dep.owner_user_id != user_id):
            continue
        entry = _running_entry(control_plane, dep, workloads.get(dep.workload_id), now)
        if entry is not None:
            out.setdefault(dep.owner_user_id, []).append(entry)
    return out


def forecast(
    user_id: str,
    *,
    control_plane=None,
    balance_cents: int | None = None,
    running: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """How long the balance lasts at the current rate; `hours_left` is None
    when nothing hourly-billed is running (or being billed)."""
    cp = control_plane or _service().control_plane
    now = now or datetime.now(UTC)
    if running is None:
        running = running_by_user(cp, user_id=user_id, now=now).get(user_id, [])
    if balance_cents is None:
        balance_cents = _billing_repo().get_balance(user_id)
    burn = sum(r["cents_per_hour"] for r in running)
    hours_left = max(0.0, balance_cents / burn) if burn > 0 else None
    return {
        "balance_cents": balance_cents,
        "balance_usd": round(balance_cents / 100, 2),
        "burn_cents_per_hour": burn,
        "burn_usd_per_hour": round(burn / 100, 2),
        "hours_left": round(hours_left, 1) if hours_left is not None else None,
        "runs_out_at": (now + timedelta(hours=hours_left)).isoformat() if hours_left is not None else None,
        "running": running,
    }


def _level_for(hours: float | None) -> int | None:
    """The tightest warning level `hours` is under, or None when above all."""
    return next((int(lvl) for lvl in sorted(WARN_LEVELS_HOURS) if hours is not None and hours < lvl), None)


def _low_credit_fields(fc: dict[str, Any], level: int) -> dict[str, Any]:
    billed = [r for r in fc["running"] if not r["offline"]]
    return {
        "balance_usd": fc["balance_usd"],
        "burn_usd_per_hour": fc["burn_usd_per_hour"],
        "hours_left": fc["hours_left"],
        "runs_out_at": fc["runs_out_at"],
        "level_hours": level,
        "running": [r["label"] for r in billed],
        "running_rentals": sum(1 for r in billed if r["kind"] == "rental"),
        "running_models": sum(1 for r in billed if r["kind"] == "model"),
        "auto_resume_count": sum(1 for r in billed if r["resume_on_topup"]),
    }


def low_credit_once(*, now: datetime | None = None, control_plane=None, billing_repo=None, notify=_notify) -> list[str]:
    """Email each customer whose running deployments will exhaust their
    balance within 24 hours, then again within 2 hours. Returns who was
    emailed this sweep."""
    now = now or datetime.now(UTC)
    cp = control_plane or _service().control_plane
    repo = billing_repo or _billing_repo()
    by_user = running_by_user(cp, now=now)
    # Balances first, in their own sessions, then the warning rows.
    forecasts = {
        uid: forecast(uid, control_plane=cp, balance_cents=repo.get_balance(uid), running=running, now=now)
        for uid, running in by_user.items()
    }
    warned: list[str] = []
    to_send: list[tuple[str, dict[str, Any], int]] = []
    with session_scope(repo.session_factory) as session:
        rows = {row.user_id: row for row in session.scalars(select(CreditWarningORM)).all()}
        for uid, row in rows.items():
            if uid not in forecasts and row.level_hours is not None:
                row.level_hours = None  # nothing running: re-arm for the next run-down
                row.updated_at = now
        for uid, fc in sorted(forecasts.items()):  # sorted: customers' event locks in one order
            row = rows.get(uid)
            level = _level_for(fc["hours_left"])
            if level is None:
                if row is not None and row.level_hours is not None:
                    row.level_hours = None  # back above every level: re-arm
                    row.updated_at = now
                continue
            if row is not None and row.level_hours is not None and row.level_hours <= level:
                continue  # already told them at this level, or a tighter one
            if row is None:
                row = CreditWarningORM(user_id=uid)
                session.add(row)
                rows[uid] = row
            row.level_hours = level
            row.warned_at = now
            row.balance_cents = fc["balance_cents"]
            row.updated_at = now
            to_send.append((uid, fc, level))
            # For the customer's code too (events feed, webhooks); same
            # transaction as the warning row, so once per level like the email.
            add_event(session, _balance_low_event(
                uid, now, level, balance_cents=fc["balance_cents"], cents_per_hour=fc["burn_cents_per_hour"],
                hours_left=fc["hours_left"], running=_low_credit_fields(fc, level)["running"],
            ))
    for uid, fc, level in to_send:
        notify(
            repo.session_factory, uid, "low_credit", _low_credit_fields(fc, level),
            event_id=f"low_credit:{uid}:{level}:{now:%Y%m%d%H%M}",
        )
        warned.append(uid)
    return warned


def _balance_low_event(user_id: str, now: datetime, level: int, *, balance_cents: int, cents_per_hour: int,
                       hours_left: float | None, running: list[str] | None) -> AccountEventORM:
    """`balance.low` for the customer's code (events feed, webhooks)."""
    return AccountEventORM(user_id=user_id, type="balance.low", created_at=now, data={
        "balance_cents": balance_cents,
        "balance_usd": round(balance_cents / 100, 2),
        "usd_per_hour": round(cents_per_hour / 100, 2),
        "hours_left": round(hours_left, 1) if hours_left is not None else None,
        "runs_out_at": (now + timedelta(hours=hours_left)).isoformat() if hours_left is not None else None,
        "under_hours": level,
        "running": running,
    })


def _mark_informed(repo, user_id: str, balance_cents: int, cents_per_hour: int, now: datetime) -> None:
    """After a resume email that states the balance, don't follow it within
    minutes with a low-credit email for the level that balance is already
    under: record that level as told. A further drop still warns. The
    customer's code isn't reading that email, so it gets the balance.low
    event here instead."""
    hours_left = (balance_cents / cents_per_hour) if cents_per_hour > 0 else None
    level = _level_for(hours_left)
    with session_scope(repo.session_factory) as session:
        row = session.get(CreditWarningORM, user_id)
        if row is None:
            row = CreditWarningORM(user_id=user_id)
            session.add(row)
        told = row.level_hours
        row.level_hours = level
        row.warned_at = now if level is not None else row.warned_at
        row.updated_at = now
        if level is not None and (told is None or told > level):
            add_event(session, _balance_low_event(
                user_id, now, level, balance_cents=balance_cents, cents_per_hour=cents_per_hour,
                hours_left=hours_left, running=None))


def _committed_by_owner(control_plane, workloads: _Workloads, now: datetime) -> tuple[dict[str, int], set[str]]:
    """Cents per hour each owner is already paying or about to (running,
    starting up, waiting to be placed, or resumed and not yet reported
    running), and the ids of suspended deployments whose resume is under way.
    Loaded once per sweep."""
    live_lease = {
        a.deployment_id for a in control_plane.repository.list_assignments(statuses=_LIVE_LEASE_STATUSES)
    }
    committed: dict[str, int] = {}
    resuming: set[str] = set()
    for state in _COMMITTED_STATES + (DeploymentState.SUSPENDED,):
        for dep in control_plane.repository.list_deployments_by_state(state):
            if not dep.owner_user_id:
                continue
            if state == DeploymentState.SUSPENDED:
                if dep.deployment_id not in live_lease:
                    continue
                resuming.add(dep.deployment_id)
            charge = control_plane.hourly_charge_cents(dep, workloads.get(dep.workload_id), now=now)
            if charge:
                committed[dep.owner_user_id] = committed.get(dep.owner_user_id, 0) + charge
    return committed, resuming


def auto_resume_once(*, now: datetime | None = None, service=None, billing_repo=None, notify=_notify) -> list[str]:
    """Resume deployments flagged `resume_on_topup` that the meter suspended
    for an empty balance, oldest suspension first, while the owner's balance
    covers an hour of everything that would then be running (what runs or is
    coming up already, plus each one brought back). Resume by hand only asks
    for an hour of the one deployment; this is automatic, so it is more
    careful, or a $0.40 top-up would bring back three pods that all stop again
    within the hour. Returns the deployment ids resumed."""
    from greencompute_gateway.application.services import InsufficientBalanceForRentalError

    now = now or datetime.now(UTC)
    svc = service or _service()
    cp = svc.control_plane
    flagged = [
        d for d in cp.repository.list_deployments_by_state(DeploymentState.SUSPENDED)
        if d.owner_user_id and d.resume_on_topup and d.suspended_reason == SUSPENDED_FOR_BALANCE
    ]
    if not flagged:
        return []
    repo = billing_repo or _billing_repo()
    workloads = _Workloads(cp)
    committed, resuming = _committed_by_owner(cp, workloads, now)
    candidates = sorted(
        (d for d in flagged if d.deployment_id not in resuming),
        key=lambda d: _utc(d.suspended_at or d.updated_at),
    )
    resumed: list[str] = []
    came_back: dict[str, list[str]] = {}
    for dep in candidates:
        uid = dep.owner_user_id
        workload = workloads.get(dep.workload_id)
        # What it will cost once up (a suspended row has no instances yet).
        cost = cp.hourly_charge_cents(dep.model_copy(update={"ready_instances": 1}), workload, now=now) or 0
        if repo.get_balance(uid) < committed.get(uid, 0) + cost:
            continue  # not enough for an hour of everything yet; maybe after the next top-up
        try:
            svc.resume_deployment(dep.deployment_id, user_id=uid)
        except InsufficientBalanceForRentalError:
            continue
        except (ValueError, KeyError, PermissionError) as exc:
            # Its machine is full, its placement or workload is gone, or the
            # owner no longer matches: nothing a top-up fixes. The customer sees
            # it on My Rentals and resumes by hand.
            log.info("auto-resume %s not possible now: %s", dep.deployment_id, exc)
            continue
        except Exception:  # noqa: BLE001 - one bad row must not stop the sweep
            log.exception("auto-resume %s failed", dep.deployment_id)
            continue
        resumed.append(dep.deployment_id)
        committed[uid] = committed.get(uid, 0) + cost
        came_back.setdefault(uid, []).append(_label(dep, workload))
    for uid, labels in came_back.items():
        balance = repo.get_balance(uid)
        notify(
            repo.session_factory, uid, "deployments_resumed",
            {"count": len(labels), "names": labels, "balance_usd": round(balance / 100, 2)},
            event_id=f"deployments_resumed:{uid}:{now:%Y%m%d%H%M%S}",
        )
        # The resume email states the balance; a low-credit email for the
        # level it is already under would say the same thing minutes later.
        _mark_informed(repo, uid, balance, committed.get(uid, 0), now)
    return resumed


async def credit_watch_loop() -> None:
    """Background coroutine started with the gateway: auto-resume every minute,
    low-credit emails every five."""
    from greencompute_gateway.infrastructure.deposit_watcher import _bool_env

    if not _bool_env("GREENCOMPUTE_CREDIT_WATCH_ENABLED", True):
        log.info("credit watch disabled")
        return
    resume_every = _int_env("GREENCOMPUTE_AUTO_RESUME_INTERVAL_SECONDS", 60, 15)
    warn_every = _int_env("GREENCOMPUTE_LOW_CREDIT_INTERVAL_SECONDS", 300, 60)
    log.info("credit watch starting (resume every %ds, low-credit every %ds)", resume_every, warn_every)
    await asyncio.sleep(45)  # let startup settle
    last_warn = 0.0
    while True:
        try:
            resumed = await asyncio.to_thread(auto_resume_once)
            if resumed:
                log.info("credit watch: resumed %d deployment(s) after top-up: %s", len(resumed), resumed)
        except Exception:  # noqa: BLE001
            log.exception("credit watch: auto-resume sweep failed")
        if time.monotonic() - last_warn >= warn_every:
            last_warn = time.monotonic()
            try:
                warned = await asyncio.to_thread(low_credit_once)
                if warned:
                    log.info("credit watch: low-credit email to %d customer(s)", len(warned))
            except Exception:  # noqa: BLE001
                log.exception("credit watch: low-credit sweep failed")
        await asyncio.sleep(resume_every)
