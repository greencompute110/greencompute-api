"""Fleets: "keep N running" and "start N as GPUs free up".

A fleet is a target count of rentals from one workload. The fleet loop
(`fleet_loop`, started with the gateway) brings each active fleet to its
target every 30 seconds:

- `replace=True` (keep N running, request #13): whenever a member stops or
  fails, a new one is started in its place, under the same name.
- `replace=False` (start N as they free up, #3/#31): members are started until
  `target` of them have run; members that end are not replaced, and the fleet
  is `completed` once all have.

Members are ordinary deployments created through GatewayService.create_deployment
with the fleet owner's id and the fleet's API key, so every normal check applies
(GPU online, capacity now). On top of that the fleet starts only as many as
the balance covers an hour of, counting everything the account already runs
(the per-rental check alone would let one fleet start 64 GPUs on an hour of
one). Each member is named `<prefix>-NN` (its slot): a replacement takes the
slot of the one it replaces, so a pool's worker names stay put. The loop never
replaces a member that is only paused (suspended, e.g. for an empty balance),
and waits rather than retries when GPUs, credit, or the key's limits run out.
If three members in a row fail before ever becoming ready (a broken image,
say), the fleet pauses with the reason; resuming it clears that count.

Every change to a fleet (the loop's pass, create, update, stop) runs under a
per-fleet lock (a thread lock, and a Postgres advisory lock across processes),
so a request and the loop can't both start the same members.

An external API user asked for this (Oct 2026): "I had to build a separate
watchdog that checks every 10 minutes and re-rents."
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select, text

from greencompute_persistence import session_scope
from greencompute_persistence.orm import DeploymentORM, FleetMemberORM, FleetORM
from greencompute_protocol import DeploymentState, WorkloadKind

log = logging.getLogger(__name__)

MAX_TARGET = 64
#: Members in a row that failed before ever becoming ready, before the fleet pauses.
FAILURES_BEFORE_PAUSE = 3
LOOP_SECONDS = 30
#: Finished members older than this are only counted, not loaded.
KEEP_FINISHED = timedelta(days=1)
_LOOP_LOCK = 0x47_43_46_4C  # "GCFL": one fleet-loop pass at a time, across gateway processes

_PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,43}$")
_FINISHED = {DeploymentState.TERMINATED, DeploymentState.FAILED}
_STARTING = {DeploymentState.PENDING, DeploymentState.SCHEDULED, DeploymentState.PULLING, DeploymentState.STARTING}

_thread_locks: dict[str, threading.Lock] = {}
_thread_locks_guard = threading.Lock()


class FleetError(ValueError):
    """A fleet request that can't be done as asked (shown to the caller as 400)."""


def _now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def default_prefix(workload_name: str) -> str:
    """A name prefix from the workload's name: "My miner!" -> "My-miner"."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", workload_name or "").strip("-._")
    cleaned = cleaned[:40].rstrip("-._")
    return cleaned if cleaned and cleaned[0].isalnum() else "rig"


@contextmanager
def _advisory_lock(engine, key: int, *, wait: bool):
    """A Postgres session advisory lock on ONE dedicated connection, held
    from lock to unlock (a Session would hand its connection back to the pool
    on commit, and the unlock could then run on another and release nothing).
    Yields whether the lock was taken; elsewhere than Postgres, always True."""
    if engine.dialect.name != "postgresql":
        yield True
        return
    with engine.connect() as conn:
        fn = "pg_advisory_lock" if wait else "pg_try_advisory_lock"
        got = conn.execute(text(f"SELECT {fn}(:k)"), {"k": key}).scalar()
        conn.commit()  # end the transaction; the lock stays with this connection
        got = True if wait else bool(got)
        try:
            yield got
        finally:
            if got:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
                conn.commit()


@contextmanager
def _fleet_lock(service, fleet_id: str):
    """Hold the fleet: one thread in this process, one connection across
    processes."""
    with _thread_locks_guard:
        lock = _thread_locks.setdefault(fleet_id, threading.Lock())
    key = int.from_bytes(hashlib.sha256(fleet_id.encode()).digest()[:8], "big", signed=True)
    with lock, _advisory_lock(service.repository.engine, key, wait=True):
        yield


class _Pass:
    """What one loop pass reads once for every fleet: each owner's committed
    spend per hour and the GPU models online."""

    def __init__(self, service) -> None:
        self.service = service
        self._committed: dict[str, int] | None = None
        self._online: list[str] | None = None

    def committed(self, owner: str) -> int:
        if self._committed is None:
            from greencompute_gateway.application.credit_watch import _committed_by_owner, _Workloads

            cp = self.service.control_plane
            self._committed = _committed_by_owner(cp, _Workloads(cp), _now())[0]
        return self._committed.get(owner, 0)

    def add(self, owner: str, cents: int) -> None:
        self.committed(owner)
        self._committed[owner] = self._committed.get(owner, 0) + cents

    def online(self) -> list[str]:
        if self._online is None:
            self._online = self.service.control_plane.online_gpu_models()
        return self._online


# --- reading -------------------------------------------------------------------


def _member_rows(service, fleet_id: str) -> list[tuple[dict, Any]]:
    """(member, its deployment or None if the row is gone), oldest first:
    members not yet seen to finish, and those that finished in the last day."""
    cutoff = _now() - KEEP_FINISHED
    with session_scope(service.repository.session_factory) as session:
        rows = session.scalars(
            select(FleetMemberORM)
            .where(FleetMemberORM.fleet_id == fleet_id)
            .where((FleetMemberORM.finished_at.is_(None)) | (FleetMemberORM.finished_at > cutoff))
            .order_by(FleetMemberORM.created_at, FleetMemberORM.slot)
        ).all()
        members = [{"deployment_id": m.deployment_id, "slot": m.slot, "created_at": _utc(m.created_at),
                    "finished_at": _utc(m.finished_at), "ran": bool(m.ran)} for m in rows]
    deps = service.control_plane.repository.get_deployments([m["deployment_id"] for m in members])
    return [(m, deps.get(m["deployment_id"])) for m in members]


def _ran_count(service, fleet_id: str) -> int:
    with session_scope(service.repository.session_factory) as session:
        return int(session.scalar(select(func.count()).select_from(FleetMemberORM).where(
            FleetMemberORM.fleet_id == fleet_id, FleetMemberORM.ran.is_(True))) or 0)


def _ran_slots(service, fleet_id: str) -> set[int]:
    with session_scope(service.repository.session_factory) as session:
        return set(session.scalars(select(FleetMemberORM.slot).where(
            FleetMemberORM.fleet_id == fleet_id, FleetMemberORM.ran.is_(True))).all())


def _live(dep) -> bool:
    return dep is not None and dep.state not in _FINISHED


def _member_cost_cents(service, workload, online: list[str] | None = None) -> int:
    """What one member costs per hour: the rate a new deployment of this
    workload is quoted (the dearest GPU it may get), times its GPUs."""
    cp = service.control_plane
    rate = cp._quoted_hourly_rate_cents(workload, online if online is not None else cp.online_gpu_models())
    return rate * ((workload.requirements.gpu_count if workload.requirements else 1) or 1)


def view(service, fleet: FleetORM, members: list | None = None, online: list[str] | None = None) -> dict[str, Any]:
    """What GET /platform/fleets/{id} returns. `online` (the GPU models
    online) can be passed in when listing several fleets."""
    members = members if members is not None else _member_rows(service, fleet.fleet_id)
    live = [(m, d) for m, d in members if _live(d)]
    workload = service.control_plane.repository.get_workload(fleet.workload_id)
    cost = _member_cost_cents(service, workload, online) if workload is not None else None
    running = sum(1 for _, d in live if d.state == DeploymentState.READY)
    status = fleet.status_reason or ""
    # Nothing to wait for: say how things stand now, not at the last pass.
    if fleet.state == "active" and len(live) >= fleet.target:
        status = (f"All {fleet.target} running." if running >= fleet.target
                  else f"{len(live)} of {fleet.target} started; {running} running.")
    return {
        "fleet_id": fleet.fleet_id,
        "workload_id": fleet.workload_id,
        "name_prefix": fleet.name_prefix,
        "target": fleet.target,
        "replace": fleet.replace,
        "state": fleet.state,
        "status": status,
        "running": running,
        "starting": sum(1 for _, d in live if d.state in _STARTING),
        "paused": sum(1 for _, d in live if d.state == DeploymentState.SUSPENDED),
        "max_runtime_minutes": fleet.max_runtime_minutes,
        "save_on_exhaustion": fleet.save_on_exhaustion,
        "resume_on_topup": fleet.resume_on_topup,
        "usd_per_hour_at_target": round(cost * fleet.target / 100, 2) if cost else None,
        "members": [
            {
                "deployment_id": m["deployment_id"],
                "name": (d.name if d is not None else None) or f"{fleet.name_prefix}-{m['slot']:02d}",
                "slot": m["slot"],
                "state": getattr(d.state, "value", str(d.state)) if d is not None else "deleted",
                "ready_at": _utc(d.ready_at).isoformat() if d is not None and d.ready_at else None,
                "last_error": (d.last_error or None) if d is not None else None,
            }
            for m, d in reversed(members)
        ],
        "created_at": _utc(fleet.created_at).isoformat() if fleet.created_at else None,
        "updated_at": _utc(fleet.updated_at).isoformat() if fleet.updated_at else None,
    }


def get_fleet(service, fleet_id: str, user_id: str | None, *, admin: bool = False) -> FleetORM:
    with session_scope(service.repository.session_factory) as session:
        fleet = session.get(FleetORM, fleet_id)
        if fleet is None or (not admin and fleet.owner_user_id != user_id):
            raise KeyError(f"fleet not found: {fleet_id}")
        session.expunge(fleet)
        return fleet


def list_fleets(service, user_id: str) -> list[FleetORM]:
    """The account's fleets: everything not stopped, plus fleets stopped in the last week."""
    with session_scope(service.repository.session_factory) as session:
        rows = session.scalars(
            select(FleetORM).where(FleetORM.owner_user_id == user_id).order_by(FleetORM.created_at.desc())
        ).all()
        cutoff = _now() - timedelta(days=7)
        out = [f for f in rows if f.state != "stopped" or (_utc(f.updated_at) or cutoff) > cutoff]
        for f in out:
            session.expunge(f)
        return out


# --- changing -----------------------------------------------------------------


def create_fleet(service, *, user_id: str, key_id: str | None, admin: bool, body: dict) -> FleetORM:
    """Create a fleet and start its first members. A problem starting them
    is left to the loop (and shown in its status), so the fleet the caller
    asked for always comes back once, and a retry with the same
    Idempotency-Key replays it rather than making a second fleet."""
    from greencompute_gateway.application.services import RESUME_NEEDS_SAVE

    workload_id = body.get("workload_id")
    workload = service.control_plane.repository.get_workload(workload_id) if workload_id else None
    if workload is None:
        raise KeyError(f"workload not found: {workload_id}")
    if not admin and not service._user_can_access_workload(workload, user_id):
        raise PermissionError(f"workload access denied: {workload_id}")
    if workload.kind not in (WorkloadKind.POD, WorkloadKind.VM):
        raise FleetError("fleets are for GPU rentals (pod workloads); a model scales by itself")
    target = body.get("target")
    if not isinstance(target, int) or isinstance(target, bool) or not 1 <= target <= MAX_TARGET:
        raise FleetError(f"target must be a whole number from 1 to {MAX_TARGET}")
    fleet_id = str(uuid4())
    prefix = body.get("name_prefix")
    if prefix is None:
        # From the workload's name, made unique with the start of the fleet id,
        # so two fleets of one workload never hand out the same names.
        prefix = f"{default_prefix(workload.display_name or workload.name)[:39]}-{fleet_id[:4]}"
    if not isinstance(prefix, str) or not _PREFIX_RE.match(prefix):
        raise FleetError(
            "name_prefix: letters, digits, '.', '_' and '-', up to 44, starting with a letter or digit "
            "(members are named <prefix>-01, <prefix>-02, …)"
        )
    with session_scope(service.repository.session_factory) as session:
        taken = session.scalar(select(func.count()).select_from(FleetORM).where(
            FleetORM.owner_user_id == user_id, FleetORM.name_prefix == prefix, FleetORM.state != "stopped"))
    if taken:
        raise FleetError(f"another of your fleets already uses the name_prefix {prefix!r}; choose another")
    save = bool(body.get("save_on_exhaustion", False))
    resume = bool(body.get("resume_on_topup", False))
    if resume and not save:
        raise FleetError(RESUME_NEEDS_SAVE)
    fleet = FleetORM(
        fleet_id=fleet_id, owner_user_id=user_id, created_by_key_id=key_id, workload_id=workload.workload_id,
        name_prefix=prefix, target=target, replace=bool(body.get("replace", True)), state="active",
        status_reason="Starting.", max_runtime_minutes=body.get("max_runtime_minutes"), save_on_exhaustion=save,
        resume_on_topup=resume, created_at=_now(), updated_at=_now(),
    )
    with session_scope(service.repository.session_factory) as session:
        session.add(fleet)
    try:
        reconcile(service, fleet.fleet_id)
    except Exception:  # noqa: BLE001 - the loop retries; the fleet exists either way
        log.exception("fleet %s: first fill failed", fleet.fleet_id)
    return get_fleet(service, fleet.fleet_id, user_id, admin=admin)


def update_fleet(service, fleet_id: str, *, user_id: str | None, admin: bool, body: dict) -> FleetORM:
    get_fleet(service, fleet_id, user_id, admin=admin)  # ownership
    target = body.get("target")
    if target is not None and (not isinstance(target, int) or isinstance(target, bool) or not 1 <= target <= MAX_TARGET):
        raise FleetError(f"target must be a whole number from 1 to {MAX_TARGET}")
    paused = body.get("paused")
    if paused is not None and not isinstance(paused, bool):
        raise FleetError("paused must be true or false")
    with _fleet_lock(service, fleet_id):
        with session_scope(service.repository.session_factory) as session:
            fleet = session.get(FleetORM, fleet_id)
            if fleet.state == "stopped":
                raise FleetError("this fleet is stopped; create a new one")
            if target is not None:
                fleet.target = target
                if fleet.state == "completed" and not fleet.replace:
                    fleet.state = "active"  # more to start
            if paused is True:
                fleet.state = "paused"
                fleet.status_reason = "Paused by you. Members keep running; none are started or replaced."
            elif paused is False and fleet.state == "paused":
                fleet.state = "active"
                fleet.failures_reset_at = _now()
                fleet.status_reason = "Resuming."
            fleet.updated_at = _now()
        _reconcile_locked(service, fleet_id)
    return get_fleet(service, fleet_id, user_id, admin=admin)


def stop_fleet(service, fleet_id: str, *, user_id: str | None, admin: bool, keep_running: bool) -> FleetORM:
    """Stop the fleet: it starts and replaces nothing more (marked stopped
    first, under the lock, so no pass can start another). Its members are
    stopped too unless `keep_running`."""
    fleet = get_fleet(service, fleet_id, user_id, admin=admin)
    with _fleet_lock(service, fleet_id):
        with session_scope(service.repository.session_factory) as session:
            row = session.get(FleetORM, fleet_id)
            row.state = "stopped"
            row.status_reason = ("Stopped. Its rentals keep running as ordinary rentals." if keep_running
                                 else "Stopped, with its rentals.")
            row.updated_at = _now()
        if not keep_running:
            for m, dep in _member_rows(service, fleet_id):
                if _live(dep):
                    try:
                        service.terminate_deployment(m["deployment_id"], actor_user_id=fleet.owner_user_id, admin=True)
                    except Exception:  # noqa: BLE001 - stop the rest regardless
                        log.exception("fleet %s: could not stop %s", fleet_id, m["deployment_id"])
    with _thread_locks_guard:
        _thread_locks.pop(fleet_id, None)  # a stopped fleet is never locked again
    return get_fleet(service, fleet_id, user_id, admin=admin)


# --- the loop -------------------------------------------------------------------


def _set_status(service, fleet_id: str, *, reason: str, state: str | None = None) -> None:
    with session_scope(service.repository.session_factory) as session:
        row = session.get(FleetORM, fleet_id)
        if row is None:
            return
        if state:
            row.state = state
        if row.status_reason != reason or state:
            row.status_reason = reason
            row.updated_at = _now()


def _key_deleted(service, fleet: FleetORM) -> bool:
    return bool(fleet.created_by_key_id) and service.repository.get_api_key(fleet.created_by_key_id) is None


def _record_progress(service, members: list[tuple[dict, Any]]) -> None:
    """Note, once each, that a member has run (seen ready) and that it has
    finished (ended, or its row is gone). `ran` is recorded when it is seen
    ready, so a member whose row is deleted later still counts as having run."""
    ran = [m["deployment_id"] for m, d in members if not m["ran"] and d is not None and d.ready_at is not None]
    done = [m["deployment_id"] for m, d in members if m["finished_at"] is None and not _live(d)]
    if not ran and not done:
        return
    with session_scope(service.repository.session_factory) as session:
        for dep_id in ran:
            row = session.get(FleetMemberORM, dep_id)
            if row is not None:
                row.ran = True
        for dep_id in done:
            row = session.get(FleetMemberORM, dep_id)
            if row is not None:
                row.finished_at = _now()
    for m, _d in members:
        if m["deployment_id"] in ran:
            m["ran"] = True


def _adopt_orphans(service, fleet: FleetORM, members: list[tuple[dict, Any]]) -> None:
    """A member started but not recorded (the gateway stopped in between):
    the owner's live deployment of this workload, named <prefix>-NN, created
    since the fleet was, by its key, that no fleet member row points at."""
    known = {m["deployment_id"] for m, _d in members}
    pattern = re.compile(rf"^{re.escape(fleet.name_prefix)}-(\d{{2,}})$")
    key_match = (DeploymentORM.created_by_key_id.is_(None) if fleet.created_by_key_id is None
                 else DeploymentORM.created_by_key_id == fleet.created_by_key_id)
    with session_scope(service.repository.session_factory) as session:
        rows = session.execute(
            select(DeploymentORM.deployment_id, DeploymentORM.name, DeploymentORM.created_at).where(
                DeploymentORM.workload_id == fleet.workload_id,
                DeploymentORM.owner_user_id == fleet.owner_user_id,
                DeploymentORM.created_at >= fleet.created_at,
                DeploymentORM.state.not_in(("terminated", "failed")),
                key_match,
            )
        ).all()
        ids = [r[0] for r in rows]
        claimed = set(session.scalars(select(FleetMemberORM.deployment_id).where(
            FleetMemberORM.deployment_id.in_(ids))).all()) if ids else set()
        for dep_id, name, created_at in rows:
            m = pattern.match(name or "")
            if m and dep_id not in known and dep_id not in claimed:
                session.add(FleetMemberORM(deployment_id=dep_id, fleet_id=fleet.fleet_id, slot=int(m.group(1)),
                                           created_at=_utc(created_at) or _now()))
                log.info("fleet %s: adopted %s (%s)", fleet.fleet_id, dep_id, name)


def reconcile(service, fleet_id: str, run: _Pass | None = None) -> None:
    """Bring one fleet to its target, under its lock."""
    with _fleet_lock(service, fleet_id):
        _reconcile_locked(service, fleet_id, run)


def _stop_extras(service, fleet: FleetORM, live: list) -> None:
    """More live members than the target: stop the extras, not-yet-ready
    ones first, then the highest slots, so <prefix>-01.. stay."""
    extras = sorted(live, key=lambda md: (md[1].state == DeploymentState.READY, -md[0]["slot"]))
    for m, _d in extras[: len(live) - fleet.target]:
        try:
            service.terminate_deployment(m["deployment_id"], actor_user_id=fleet.owner_user_id, admin=True)
        except Exception:  # noqa: BLE001 - try the others; the next pass retries
            log.exception("fleet %s: could not stop %s", fleet.fleet_id, m["deployment_id"])


def _reconcile_locked(service, fleet_id: str, run: _Pass | None = None) -> None:
    """Start members it lacks (as many as GPUs, credit and the key allow,
    stopping at the first refusal), stop extras, record a status. A paused
    fleet still stops extras when its target is lowered, and nothing else."""
    from greencompute_gateway.application.services import (
        InsufficientBalanceForRentalError,
        KeyLimitError,
        NoCapacityError,
        UnsupportedGPUError,
    )

    run = run or _Pass(service)
    with session_scope(service.repository.session_factory) as session:
        fleet = session.get(FleetORM, fleet_id)
        if fleet is None or fleet.state not in ("active", "paused"):
            return
        session.expunge(fleet)
    if fleet.state == "active":
        _adopt_orphans(service, fleet, _member_rows(service, fleet_id))
    members = _member_rows(service, fleet_id)
    _record_progress(service, members)
    live = [(m, d) for m, d in members if _live(d)]
    if fleet.state == "paused":
        if len(live) > fleet.target:
            _stop_extras(service, fleet, live)
        return

    # A run of members that failed before ever being ready: pause.
    reset = _utc(fleet.failures_reset_at)
    streak, last_error = 0, None
    for m, d in reversed(members):
        if reset and m["created_at"] and m["created_at"] < reset:
            break
        if d is not None and d.state == DeploymentState.FAILED and d.ready_at is None:
            streak += 1
            last_error = last_error or d.last_error
        else:
            break
    if streak >= FAILURES_BEFORE_PAUSE:
        _set_status(service, fleet_id, state="paused", reason=(
            f"Paused: the last {streak} rentals failed before they were ready"
            + (f" ({(last_error or '')[:200].rstrip('.')})" if last_error else "")
            + ". Fix the workload, then resume the fleet."))
        return

    if len(live) > fleet.target:
        _stop_extras(service, fleet, live)
        _set_status(service, fleet_id, reason=f"Stopping {len(live) - fleet.target} to get down to {fleet.target}.")
        return

    if fleet.replace:
        needed = fleet.target - len(live)
    else:
        needed = fleet.target - len(live) - _ran_count(service, fleet_id)
        if needed <= 0 and not live:
            _set_status(service, fleet_id, state="completed", reason=f"Done: all {fleet.target} have run.")
            return
    if needed <= 0:
        running = sum(1 for _, d in live if d.state == DeploymentState.READY)
        _set_status(service, fleet_id, reason=(
            f"All {fleet.target} running." if running == fleet.target
            else f"{len(live)} of {fleet.target} started; {running} running."))
        return

    workload = service.control_plane.repository.get_workload(fleet.workload_id)
    if workload is None:
        _set_status(service, fleet_id, state="paused", reason="Paused: its workload was deleted.")
        return

    if _key_deleted(service, fleet):
        _set_status(service, fleet_id, state="paused", reason=(
            "Paused: the API key that created this fleet was deleted. Create the fleet again with a current key."))
        return

    # Credit: an hour of everything that would then be running.
    reasons: list[str] = []
    cost = _member_cost_cents(service, workload, run.online())
    committed = run.committed(fleet.owner_user_id)
    balance = service.billing_repo.get_balance(fleet.owner_user_id)
    affordable = max(0, (balance - committed) // cost) if cost > 0 else needed
    if affordable < needed:
        needed = affordable
        reasons.append(
            f"Waiting for credit: another rental needs ${cost / 100:.2f} on top of an hour of what's running "
            f"(${committed / 100:.2f}); your balance is ${balance / 100:.2f}. Add credit and it continues."
        )

    # Slots: a replacement takes a free slot among the live ones; in
    # start-N-once mode the slots of members that already ran stay taken.
    used = {m["slot"] for m, _d in live} | (set() if fleet.replace else _ran_slots(service, fleet_id))
    started = 0
    for _ in range(needed):
        slot = next(i for i in range(1, MAX_TARGET * 4) if i not in used)
        name = f"{fleet.name_prefix}-{slot:02d}"
        try:
            dep = service.create_deployment(
                {
                    "workload_id": fleet.workload_id,
                    "name": name,
                    "max_runtime_minutes": fleet.max_runtime_minutes,
                    "save_on_exhaustion": fleet.save_on_exhaustion,
                    "resume_on_topup": fleet.resume_on_topup,
                },
                user_id=fleet.owner_user_id,
                created_by_key_id=fleet.created_by_key_id,
            )
        except NoCapacityError as exc:
            reasons.insert(0, f"Waiting for a free GPU: {exc.message.split('; nothing was created')[0]}. "
                              f"Checking every {LOOP_SECONDS} seconds.")
            break
        except InsufficientBalanceForRentalError:
            reasons.insert(0, "Waiting for credit: your balance doesn't cover an hour of another rental. "
                              "Add credit and it continues.")
            break
        except UnsupportedGPUError:
            reasons.insert(0, "Waiting: no machine with this workload's GPU is online.")
            break
        except KeyLimitError as exc:
            reasons.insert(0, (
                f"Waiting: the API key that created this fleet may have {exc.max_active_rentals} rentals "
                "running at once." if exc.reason == "active" else
                f"Waiting: the API key that created this fleet has spent its daily limit "
                f"(${exc.daily_spend_limit_usd:.2f})."))
            break
        except (KeyError, PermissionError):
            _set_status(service, fleet_id, state="paused",
                        reason="Paused: its workload was deleted or is no longer yours.")
            return
        except Exception as exc:  # noqa: BLE001 - say so, retry next pass
            log.exception("fleet %s: could not start a member", fleet_id)
            reasons.insert(0, f"Couldn't start a rental ({str(exc)[:200]}). Trying again in {LOOP_SECONDS} seconds.")
            break
        with session_scope(service.repository.session_factory) as session:
            session.add(FleetMemberORM(deployment_id=dep.deployment_id, fleet_id=fleet_id, slot=slot,
                                       created_at=_now()))
        used.add(slot)
        started += 1
        run.add(fleet.owner_user_id, cost)
    head = f"{len(live) + started} of {fleet.target} started."
    _set_status(service, fleet_id, reason=f"{head} {reasons[0]}" if reasons else head)


def fleet_once(service=None) -> int:
    """One pass over every active or paused fleet. Returns how many were looked at."""
    if service is None:
        from greencompute_gateway.application.services import service as default_service

        service = default_service
    with _advisory_lock(service.repository.engine, _LOOP_LOCK, wait=False) as got:
        if not got:
            return 0  # another gateway process is on it
        with session_scope(service.repository.session_factory) as s:
            ids = [f.fleet_id for f in s.scalars(
                select(FleetORM).where(FleetORM.state.in_(("active", "paused")))).all()]
        run = _Pass(service)
        for fleet_id in ids:
            try:
                reconcile(service, fleet_id, run)
            except Exception:  # noqa: BLE001 - one fleet must not stop the rest
                log.exception("fleet %s: reconcile failed", fleet_id)
        return len(ids)


async def fleet_loop() -> None:
    """Background coroutine started with the gateway."""
    from greencompute_gateway.infrastructure.deposit_watcher import _bool_env

    if not _bool_env("GREENCOMPUTE_FLEETS_ENABLED", True):
        log.info("fleet loop disabled")
        return
    try:
        every = max(10, int(os.environ.get("GREENCOMPUTE_FLEET_INTERVAL_SECONDS", LOOP_SECONDS)))
    except ValueError:
        every = LOOP_SECONDS
    await asyncio.sleep(40)
    while True:
        try:
            await asyncio.to_thread(fleet_once)
        except Exception:  # noqa: BLE001
            log.exception("fleet loop pass failed")
        await asyncio.sleep(every)
