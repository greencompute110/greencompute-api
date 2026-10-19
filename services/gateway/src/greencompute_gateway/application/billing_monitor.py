"""Watches the money, so a billing bug or misuse shows up within minutes
rather than at the end of the month. Every few minutes it checks:

  - every account's balance still equals the sum of its ledger (a mismatch
    means something changed a balance without recording it);
  - every running rental is being charged;
  - no rental keeps running on an empty balance past the reaper's grace;
  - no charge other than saved-pod storage took a balance below zero
    (storage is billed into debt by design, until the disk is deleted);
  - credits worth a look: manual ones, large ones, and new accounts topping
    up repeatedly; plus refunds and admin debits;
  - spend spikes: an account spending far above its usual rate;

and once a day it posts a summary of top-ups, spend and balances. Findings go
to the team's alert channel (see infrastructure/alerts.py) and the log;
admins can see the current state at GET /platform/billing/admin/monitor.

- GREENCOMPUTE_BILLING_MONITOR_ENABLED (default on)
- GREENCOMPUTE_BILLING_MONITOR_INTERVAL_SECONDS (default 300)
- GREENCOMPUTE_MONITOR_LARGE_CREDIT_USD (default 100): credits from this up are reported
- GREENCOMPUTE_MONITOR_SPIKE_FLOOR_USD (default 10): an hour's spend below this is never a spike
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, time, timedelta

from greencompute_persistence import session_scope
from greencompute_persistence.orm import (
    CryptoDepositORM,
    CryptoInvoiceORM,
    DeploymentORM,
    KeyValueStateORM,
    LedgerEntryORM,
    UserORM,
    WorkloadORM,
)
from sqlalchemy import case, delete, func, select

log = logging.getLogger(__name__)

UNBILLED_AFTER = timedelta(minutes=15)  # metering charges every minute or so
EMPTY_GRACE = timedelta(minutes=30)  # the exhaustion reaper acts within 15
NEW_ACCOUNT = timedelta(days=7)
REPEAT_TOPUPS = 3  # top-ups within 24h by a new account
SPIKE_FACTOR = 3  # times the account's usual hourly spend
REALERT_AFTER = timedelta(hours=6)  # a problem still there is reported again
FIRST_RUN_LOOKBACK = timedelta(hours=1)
CURSOR_OVERLAP = timedelta(minutes=5)  # ledger rows commit slightly out of order
SENT_RETENTION = timedelta(days=7)

# Charges allowed to take a balance below zero: saved-pod storage and admin debits.
MAY_GO_NEGATIVE = ("storage", "adjustment", "refund", "chargeback")
# Money taken out of an account other than by spending; always reported, and
# never counted as spend. account_closed: credit forfeited when its owner
# deleted the account.
MANUAL_DEBITS = ("adjustment", "refund", "chargeback", "account_closed")
SPEND_KINDS_LABEL = {"usage": "GPU rentals", "storage": "saved-pod storage", "inference": "inference"}

PROBLEM = "problem"
NOTICE = "notice"


def _usd_env(name: str, default: float) -> int:
    """A dollar threshold from the environment, in cents."""
    try:
        return int(round(float(os.environ.get(name) or default) * 100))
    except ValueError:
        return int(round(default * 100))


def _aware(dt: datetime | None) -> datetime | None:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=UTC)


def _usd(cents: int) -> str:
    return f"{'-' if cents < 0 else ''}${abs(cents) / 100:,.2f}"


@dataclass
class Finding:
    check: str
    key: str  # stable id, for not repeating an alert
    severity: str  # PROBLEM or NOTICE
    text: str


# --- checks ---------------------------------------------------------------------


def _names(session, user_ids) -> dict[str, str]:
    ids = {u for u in user_ids if u}
    if not ids:
        return {}
    rows = session.execute(select(UserORM.user_id, UserORM.username).where(UserORM.user_id.in_(ids))).all()
    return {uid: name or uid[:8] for uid, name in rows}


def ledger_drift(session) -> list[Finding]:
    sums = (
        select(LedgerEntryORM.user_id, func.sum(LedgerEntryORM.amount_cents).label("total"))
        .group_by(LedgerEntryORM.user_id)
        .subquery()
    )
    ledger_total = func.coalesce(sums.c.total, 0)
    rows = session.execute(
        select(UserORM.user_id, UserORM.username, UserORM.balance_credits, ledger_total)
        .outerjoin(sums, sums.c.user_id == UserORM.user_id)
        .where(UserORM.balance_credits != ledger_total)
    ).all()
    return [
        Finding(
            "ledger_drift", f"drift:{uid}:{balance - total}", PROBLEM,
            f"{name or uid[:8]}: balance {_usd(balance)} but the ledger adds up to {_usd(int(total))} "
            f"(off by {_usd(balance - int(total))})",
        )
        for uid, name, balance, total in rows
    ]


def _running_rentals(session):
    """READY pod/VM deployments (inference is charged per token, not by the hour)."""
    return session.execute(
        select(DeploymentORM, WorkloadORM.requirements, UserORM.balance_credits, UserORM.username)
        .join(WorkloadORM, WorkloadORM.workload_id == DeploymentORM.workload_id)
        .outerjoin(UserORM, UserORM.user_id == DeploymentORM.owner_user_id)
        .where(DeploymentORM.state == "ready", WorkloadORM.kind != "inference")
    ).all()


def unbilled_rentals(session, now: datetime) -> list[Finding]:
    rentals = [
        r for r in _running_rentals(session)
        if (_aware(r[0].ready_at or r[0].updated_at) or now) <= now - UNBILLED_AFTER
    ]
    if not rentals:
        return []
    ids = [r[0].deployment_id for r in rentals]
    last_charge = dict(session.execute(
        select(LedgerEntryORM.reference_id, func.max(LedgerEntryORM.created_at))
        .where(LedgerEntryORM.reference_id.in_(ids), LedgerEntryORM.amount_cents < 0)
        .group_by(LedgerEntryORM.reference_id)
    ).all())
    out = []
    for dep, _req, balance, name in rentals:
        who = name or (dep.owner_user_id or "")[:8]
        if not dep.owner_user_id:
            out.append(Finding("unbilled_rental", f"unbilled:{dep.deployment_id}", PROBLEM,
                               f"rental {dep.deployment_id[:8]} is running with no owner to charge"))
            continue
        if (balance or 0) <= 0:
            continue  # nothing to charge; running_on_empty covers it
        last = _aware(last_charge.get(dep.deployment_id))
        if last is None or last < now - UNBILLED_AFTER:
            since = "never charged" if last is None else f"last charged {int((now - last).total_seconds() // 60)} min ago"
            out.append(Finding("unbilled_rental", f"unbilled:{dep.deployment_id}", PROBLEM,
                               f"{who}: rental {dep.deployment_id[:8]} is running but {since}"))
    return out


def rentals_on_empty(session) -> list[tuple[str, str, int]]:
    """(deployment_id, owner name, balance) of running rentals whose owner has no balance."""
    return [
        (dep.deployment_id, name or (dep.owner_user_id or "")[:8], balance or 0)
        for dep, _req, balance, name in _running_rentals(session)
        if dep.owner_user_id and (balance or 0) <= 0
    ]


def _empty_finding(dep_id: str, name: str, balance: int, minutes: int | None) -> Finding:
    how_long = f" for {minutes} min" if minutes is not None else ""
    return Finding("running_on_empty", f"empty:{dep_id}", PROBLEM,
                   f"{name}: rental {dep_id[:8]} still running on a {_usd(balance)} balance{how_long}")


def negative_charges(session, since: datetime) -> list[Finding]:
    rows = session.execute(
        select(LedgerEntryORM).where(
            LedgerEntryORM.created_at > since,
            LedgerEntryORM.balance_after < 0,
            LedgerEntryORM.kind.not_in(MAY_GO_NEGATIVE),
        )
    ).scalars().all()
    names = _names(session, [r.user_id for r in rows])
    return [
        Finding("negative_balance", f"neg:{r.entry_id}", PROBLEM,
                f"{names.get(r.user_id, r.user_id[:8])}: a {r.kind} charge of {_usd(r.amount_cents)} "
                f"took the balance to {_usd(r.balance_after)}")
        for r in rows
    ]


def _is_manual(entry) -> bool:
    ref = entry.deposit_ref or ""
    return not ref or ref.startswith("manual:")


def _credit_source(entry) -> str:
    ref = entry.deposit_ref or ""
    desc = entry.description or ""
    if ref.startswith("stripe") or desc.startswith("Stripe"):
        return "card"
    if ref.startswith("manual:"):
        return "manual"
    if ref or desc.startswith("Crypto deposit"):
        return "crypto"
    return "manual"


def notable_credits(session, since: datetime, now: datetime) -> list[Finding]:
    large = _usd_env("GREENCOMPUTE_MONITOR_LARGE_CREDIT_USD", 100)
    rows = session.execute(
        select(LedgerEntryORM).where(
            LedgerEntryORM.created_at > since,
            (LedgerEntryORM.amount_cents > 0) | LedgerEntryORM.kind.in_(MANUAL_DEBITS),
        )
    ).scalars().all()
    names = _names(session, [r.user_id for r in rows])
    out = []
    for r in rows:
        who = names.get(r.user_id, r.user_id[:8])
        note = f" ({r.description})" if r.description else ""
        if r.kind in MANUAL_DEBITS:
            out.append(Finding("manual_debit", f"credit:{r.entry_id}", NOTICE, f"{who}: {r.kind} {_usd(r.amount_cents)}{note}"))
        elif r.amount_cents > 0 and _is_manual(r):
            out.append(Finding("manual_credit", f"credit:{r.entry_id}", NOTICE, f"{who}: manual credit {_usd(r.amount_cents)}{note}"))
        elif r.amount_cents >= large:
            out.append(Finding("large_credit", f"credit:{r.entry_id}", NOTICE,
                               f"{who}: {_credit_source(r)} top-up {_usd(r.amount_cents)}{note}"))

    # New accounts topping up again and again: worth a human look.
    recent = session.execute(
        select(LedgerEntryORM.user_id, func.count(), func.sum(LedgerEntryORM.amount_cents), UserORM.created_at, UserORM.username)
        .join(UserORM, UserORM.user_id == LedgerEntryORM.user_id)
        .where(LedgerEntryORM.amount_cents > 0, LedgerEntryORM.created_at >= now - timedelta(hours=24),
               UserORM.created_at >= now - NEW_ACCOUNT)
        .group_by(LedgerEntryORM.user_id, UserORM.created_at, UserORM.username)
        .having(func.count() >= REPEAT_TOPUPS)
    ).all()
    for uid, n, total, created, name in recent:
        age = (now - _aware(created)).days
        out.append(Finding("repeat_topups", f"repeat:{uid}:{now.date()}", NOTICE,
                           f"{name or uid[:8]} (account {age} day{'s' if age != 1 else ''} old): "
                           f"{n} top-ups totalling {_usd(int(total))} in 24h"))
    return out


def spend_spikes(session, now: datetime) -> list[Finding]:
    floor = _usd_env("GREENCOMPUTE_MONITOR_SPIKE_FLOOR_USD", 10)
    hour_ago, week_ago = now - timedelta(hours=1), now - timedelta(days=7, hours=1)
    spend = -LedgerEntryORM.amount_cents
    rows = session.execute(
        select(
            LedgerEntryORM.user_id,
            func.sum(case((LedgerEntryORM.created_at >= hour_ago, spend), else_=0)),
            func.sum(case((LedgerEntryORM.created_at < hour_ago, spend), else_=0)),
        )
        .where(LedgerEntryORM.amount_cents < 0, LedgerEntryORM.kind.not_in(MANUAL_DEBITS),
               LedgerEntryORM.created_at >= week_ago)
        .group_by(LedgerEntryORM.user_id)
    ).all()
    flagged = []
    for uid, last_hour, before in rows:
        usual = int(before or 0) / (7 * 24)
        if int(last_hour or 0) >= max(floor, SPIKE_FACTOR * usual):
            flagged.append((uid, int(last_hour), usual))
    names = _names(session, [f[0] for f in flagged])
    return [
        Finding("spend_spike", f"spike:{uid}", NOTICE,
                f"{names.get(uid, uid[:8])} spent {_usd(last_hour)} in the last hour (usually {_usd(round(usual))}/hr)")
        for uid, last_hour, usual in flagged
    ]


# --- daily summary --------------------------------------------------------------


def summary(session, start: datetime, end: datetime) -> dict:
    window = (LedgerEntryORM.created_at >= start, LedgerEntryORM.created_at < end)
    topups = {"card": [0, 0], "crypto": [0, 0], "manual": [0, 0]}
    for entry in session.execute(select(LedgerEntryORM).where(LedgerEntryORM.amount_cents > 0, *window)).scalars():
        bucket = topups[_credit_source(entry)]
        bucket[0] += 1
        bucket[1] += entry.amount_cents
    spend = {
        kind: -int(total)
        for kind, total in session.execute(
            select(LedgerEntryORM.kind, func.sum(LedgerEntryORM.amount_cents))
            .where(LedgerEntryORM.amount_cents < 0, LedgerEntryORM.kind.not_in(MANUAL_DEBITS), *window)
            .group_by(LedgerEntryORM.kind)
        ).all()
    }
    new_accounts = session.scalar(
        select(func.count()).select_from(UserORM).where(UserORM.created_at >= start, UserORM.created_at < end)
    ) or 0
    running = _running_rentals(session)
    gpus = sum(int((req or {}).get("gpu_count") or 1) * (dep.requested_instances or 1) for dep, req, _b, _n in running)
    per_hour = sum(
        (dep.hourly_rate_cents or 0) * int((req or {}).get("gpu_count") or 1) * (dep.requested_instances or 1)
        for dep, req, _b, _n in running
    )
    positive, debt, debtors = session.execute(
        select(
            func.coalesce(func.sum(case((UserORM.balance_credits > 0, UserORM.balance_credits), else_=0)), 0),
            func.coalesce(func.sum(case((UserORM.balance_credits < 0, UserORM.balance_credits), else_=0)), 0),
            func.count(case((UserORM.balance_credits < 0, 1))),
        )
    ).one()
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "topups": {k: {"count": v[0], "cents": v[1]} for k, v in topups.items()},
        "spend_cents": spend,
        "new_accounts": new_accounts,
        "running_rentals": len(running),
        "running_gpus": gpus,
        "running_cents_per_hour": per_hour,
        "customer_balances_cents": int(positive),
        "storage_debt_cents": -int(debt),
        "accounts_in_debt": int(debtors),
        "unmatched_deposits": session.scalar(
            select(func.count()).select_from(CryptoDepositORM).where(CryptoDepositORM.status == "unmatched")
        ) or 0,
        "open_reviews": session.scalar(
            select(func.count()).select_from(CryptoInvoiceORM).where(CryptoInvoiceORM.review_status == "open")
        ) or 0,
    }


def summary_text(s: dict, day: str) -> str:
    t = s["topups"]
    n_topups = sum(v["count"] for v in t.values())
    topup_cents = sum(v["cents"] for v in t.values())
    spend = s["spend_cents"]
    spend_parts = ", ".join(
        f"{SPEND_KINDS_LABEL.get(k, k)} {_usd(v)}" for k, v in sorted(spend.items(), key=lambda kv: -kv[1])
    )
    lines = [
        f"Green Compute daily money report, {day} (UTC)",
        f"Top-ups: {n_topups}, {_usd(topup_cents)} (card {_usd(t['card']['cents'])}, crypto {_usd(t['crypto']['cents'])}, "
        f"manual {_usd(t['manual']['cents'])})",
        f"Spend: {_usd(sum(spend.values()))}" + (f" ({spend_parts})" if spend_parts else ""),
        f"New accounts: {s['new_accounts']}",
        f"Running now: {s['running_rentals']} rental{'s' if s['running_rentals'] != 1 else ''}, "
        f"{s['running_gpus']} GPUs, {_usd(s['running_cents_per_hour'])}/hr",
        f"Customer balances: {_usd(s['customer_balances_cents'])}; storage debt {_usd(s['storage_debt_cents'])} "
        f"across {s['accounts_in_debt']} account{'s' if s['accounts_in_debt'] != 1 else ''}",
        f"Payments needing a look: {s['unmatched_deposits']} unmatched, {s['open_reviews']} review request"
        f"{'s' if s['open_reviews'] != 1 else ''}",
    ]
    return "\n".join(lines)


# --- running it -----------------------------------------------------------------


class _State:
    """The monitor's bookkeeping in key_value_state, under "monitor:"."""

    def __init__(self, session):
        self.session = session

    def get(self, key: str) -> KeyValueStateORM | None:
        return self.session.get(KeyValueStateORM, f"monitor:{key}")

    def put(self, key: str, value: str, now: datetime) -> None:
        row = self.get(key)
        if row is None:
            self.session.add(KeyValueStateORM(key=f"monitor:{key}", value=value, updated_at=now))
        else:
            row.value, row.updated_at = value, now

    def drop(self, key: str) -> None:
        row = self.get(key)
        if row is not None:
            self.session.delete(row)

    def due(self, finding: Finding, now: datetime) -> bool:
        """Report a finding once; a recurring problem again after REALERT_AFTER."""
        sent = self.get(f"sent:{finding.key}")
        once = finding.key.startswith(("credit:", "neg:", "repeat:"))
        if sent is not None and (once or _aware(sent.updated_at) > now - REALERT_AFTER):
            return False
        self.put(f"sent:{finding.key}", finding.check, now)
        return True


def _empty_with_grace(session, state: _State, now: datetime) -> list[Finding]:
    """Running-on-empty rentals seen for longer than EMPTY_GRACE."""
    current = rentals_on_empty(session)
    seen = {
        row.key.removeprefix("monitor:empty:"): row
        for row in session.execute(
            select(KeyValueStateORM).where(KeyValueStateORM.key.like("monitor:empty:%"))
        ).scalars()
    }
    out = []
    for dep_id, name, balance in current:
        row = seen.pop(dep_id, None)
        if row is None:
            state.put(f"empty:{dep_id}", "", now)
            continue
        first = _aware(row.updated_at)
        if first <= now - EMPTY_GRACE:
            out.append(_empty_finding(dep_id, name, balance, int((now - first).total_seconds() // 60)))
    for row in seen.values():  # no longer running on empty
        session.delete(row)
    return out


def _alert_text(findings: list[Finding]) -> str:
    problems = [f for f in findings if f.severity == PROBLEM]
    notices = [f for f in findings if f.severity == NOTICE]
    head = ", ".join(
        f"{len(group)} {label}{'s' if len(group) != 1 else ''}"
        for group, label in ((problems, "problem"), (notices, "notice"))
        if group
    )
    lines = [f"Billing monitor: {head}"]
    for f in problems[:15] + notices[:15]:
        lines.append(f"{'!' if f.severity == PROBLEM else '-'} {f.text}")
    hidden = max(0, len(problems) - 15) + max(0, len(notices) - 15)
    if hidden:
        lines.append(f"…and {hidden} more: GET /platform/billing/admin/monitor")
    return "\n".join(lines)


def run_once(session_factory, now: datetime | None = None, send=None) -> dict:
    """One monitoring pass: alert on what's new, and post yesterday's summary
    once per day. Returns what was sent, for logs and tests."""
    if send is None:
        from greencompute_gateway.infrastructure.alerts import send_payment_alert as send
    now = now or datetime.now(UTC)
    with session_scope(session_factory) as session:
        state = _State(session)
        cursor_row = state.get("cursor:ledger")
        cursor = _aware(datetime.fromisoformat(cursor_row.value)) if cursor_row else now - FIRST_RUN_LOOKBACK
        since = cursor - CURSOR_OVERLAP
        findings = (
            ledger_drift(session)
            + unbilled_rentals(session, now)
            + _empty_with_grace(session, state, now)
            + negative_charges(session, since)
            + notable_credits(session, since, now)
            + spend_spikes(session, now)
        )
        new = [f for f in findings if state.due(f, now)]
        state.put("cursor:ledger", now.isoformat(), now)

        daily = None
        today = now.date()
        last_daily = state.get("daily:last")
        if last_daily is None or last_daily.value != today.isoformat():
            start = datetime.combine(today - timedelta(days=1), time(), tzinfo=UTC)
            daily = summary_text(summary(session, start, start + timedelta(days=1)), start.date().isoformat())
            state.put("daily:last", today.isoformat(), now)

        session.execute(
            delete(KeyValueStateORM).where(
                KeyValueStateORM.key.like("monitor:sent:%"),
                KeyValueStateORM.updated_at < now - SENT_RETENTION,
            )
        )
    if new:
        send(_alert_text(new))
    if daily:
        send(daily)
    return {"alerted": [asdict(f) for f in new], "daily": daily}


def snapshot(session_factory, now: datetime | None = None) -> dict:
    """Everything the checks see right now, without alerting or remembering
    anything: for the admin page."""
    now = now or datetime.now(UTC)
    with session_scope(session_factory) as session:
        day_ago = now - timedelta(hours=24)
        findings = (
            ledger_drift(session)
            + unbilled_rentals(session, now)
            + [_empty_finding(d, n, b, None) for d, n, b in rentals_on_empty(session)]
            + negative_charges(session, day_ago)
            + notable_credits(session, day_ago, now)
            + spend_spikes(session, now)
        )
        today = summary(session, datetime.combine(now.date(), time(), tzinfo=UTC), now)
    return {
        "checked_at": now.isoformat(),
        "problems": [asdict(f) for f in findings if f.severity == PROBLEM],
        "notices": [asdict(f) for f in findings if f.severity == NOTICE],
        "today": today,
    }


async def billing_monitor_loop() -> None:
    """Background coroutine started with the gateway."""
    if os.environ.get("GREENCOMPUTE_BILLING_MONITOR_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
        log.info("billing monitor disabled")
        return
    interval = max(60, int(os.environ.get("GREENCOMPUTE_BILLING_MONITOR_INTERVAL_SECONDS", "300")))
    log.info("billing monitor starting (interval=%ds)", interval)
    await asyncio.sleep(30)  # let startup settle
    from greencompute_gateway.application.billing_service import get_billing_service

    while True:
        try:
            result = await asyncio.to_thread(run_once, get_billing_service().repo.session_factory)
            if result["alerted"]:
                log.info("billing monitor: %d new finding(s)", len(result["alerted"]))
        except Exception:
            log.exception("billing monitor run failed")
        await asyncio.sleep(interval)
