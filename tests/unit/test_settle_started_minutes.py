"""A rental pays for every started minute it was READY (owner decision,
2026-11-17), settled when it leaves READY: stopped, failed, deleted, paused
(balance, spend limit, idle) or moved. The minute meter keeps charging as it
runs; the settlement charges only the rest, once. Before this, the seconds
since the meter's last tick and the sub-cent remainder were never charged: a
reviewer's 11 rentals, READY 3-137 s each, were charged 1¢ in all.

Rates here are $3.00/GPU-hour (5¢ a minute, exact), so minutes read as
cents: 1 minute = 5¢."""
from datetime import UTC, datetime, timedelta

import pytest
from greencompute_control_plane.application.services import ControlPlaneService
from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository
from greencompute_persistence import session_scope
from greencompute_persistence.orm import DeploymentORM, LedgerEntryORM, UserORM
from greencompute_protocol import (
    DeploymentRecord,
    DeploymentState,
    DeploymentStatusUpdate,
    Heartbeat,
    LeaseAssignment,
    WorkloadKind,
    WorkloadSpec,
)
from sqlalchemy import select

RATE = 300  # cents per GPU-hour: 5¢ a minute


@pytest.fixture
def cp(tmp_path, monkeypatch):
    db_url = f"sqlite+pysqlite:///{tmp_path / 'settle.db'}"
    # The meter and the settlement build BillingRepository() from the environment.
    monkeypatch.setenv("GREENCOMPUTE_DATABASE_URL", db_url)
    repo = ControlPlaneRepository(database_url=db_url, bootstrap=True)
    with session_scope(repo.session_factory) as s:
        s.add(UserORM(user_id="u1", username="u1", email="u1@x.io", balance_credits=10_000))
    cp = ControlPlaneService(repository=repo)
    _heartbeat(cp, datetime.now(UTC))  # its machine is reporting
    return cp


def _workload(cp, kind=WorkloadKind.POD, gpus=1, **fields):
    return cp.repository.upsert_workload(WorkloadSpec(
        name=f"{kind.value}-w", image="img", kind=kind, owner_user_id="u1", requirements={"gpu_count": gpus},
        **fields,
    ))


def _ready(cp, workload=None, *, rate=RATE, ready_for=None, hotkey="hk1"):
    """A rental the way it really starts: placed, then reported READY.
    `ready_for` seconds: as if it became READY that long ago."""
    workload = workload or _workload(cp)
    dep = cp.repository.create_deployment(DeploymentRecord(
        workload_id=workload.workload_id, owner_user_id="u1", state=DeploymentState.STARTING,
        hotkey=hotkey, node_id="n1", hourly_rate_cents=rate,
    ))
    cp.repository.save_assignment(LeaseAssignment(
        deployment_id=dep.deployment_id, workload_id=workload.workload_id, hotkey=hotkey, node_id="n1",
        status="activating",
    ))
    _report(cp, dep.deployment_id, DeploymentState.READY)
    if ready_for is not None:
        cp.repository.set_billable_since(dep.deployment_id, datetime.now(UTC) - timedelta(seconds=ready_for))
    return dep.deployment_id


def _report(cp, dep_id, state, **fields):
    return cp.update_deployment_status(DeploymentStatusUpdate(
        deployment_id=dep_id, state=state, ready_instances=1 if state == DeploymentState.READY else 0, **fields,
    ))


def _charges(cp):
    with session_scope(cp.repository.session_factory) as s:
        rows = s.scalars(select(LedgerEntryORM).where(LedgerEntryORM.user_id == "u1")
                         .order_by(LedgerEntryORM.created_at)).all()
        return [(-e.amount_cents, e.description) for e in rows]


def _charged(cp):
    return sum(cents for cents, _ in _charges(cp))


def _tick(cp, at, elapsed):
    """A meter tick at `at`, its machine reporting then."""
    _heartbeat(cp, at)
    return cp.meter_usage(elapsed_seconds=elapsed, now=at)


def _row(cp, dep_id):
    with session_scope(cp.repository.session_factory) as s:
        row = s.get(DeploymentORM, dep_id)
        return {"metered_until": row.metered_until, "remainder": row.metering_remainder_mcents,
                "seconds": row.spell_billed_seconds}


# --- the rule ---------------------------------------------------------------------


def test_a_10_second_rental_pays_one_minute(cp):
    dep_id = _ready(cp, ready_for=10)
    _report(cp, dep_id, DeploymentState.TERMINATED)
    assert _charges(cp) == [(5, "GPU usage 1× GPU @ $3.00/hr (final minute)")]
    assert _row(cp, dep_id)["metered_until"] is None  # spell closed


def test_a_61_second_rental_pays_two_minutes(cp):
    dep_id = _ready(cp, ready_for=61)
    cp.cleanup_deployment(dep_id, reason="user terminated")  # DELETE /platform/deployments/{id}
    assert _charged(cp) == 10


def test_the_meters_minutes_are_not_charged_again(cp):
    dep_id = _ready(cp, ready_for=150)
    since = cp.repository.get_billable_since(dep_id)
    # The meter ticked once, 120 s in: 2 minutes, 10¢.
    assert _tick(cp, since + timedelta(seconds=120), 120) == {dep_id: 10}
    # Stopped 30 s after that tick, 150 s in: the third minute, once.
    _report(cp, dep_id, DeploymentState.TERMINATED)
    assert [c for c, _ in _charges(cp)] == [10, 5]


def test_a_whole_number_of_metered_minutes_adds_nothing(cp):
    dep_id = _ready(cp, ready_for=1)
    since = cp.repository.get_billable_since(dep_id)
    _tick(cp, since + timedelta(seconds=600), 600)
    # The meter is already past now: no tail, exactly 10 minutes.
    _report(cp, dep_id, DeploymentState.TERMINATED)
    assert _charged(cp) == 50


def test_the_carried_sub_cent_remainder_is_charged_rounded_up(cp):
    # $0.40/hr: 0.67¢ a minute. Three metered minutes = 2.0¢ exactly; four = 2.67¢.
    dep_id = _ready(cp, rate=40, ready_for=1)
    since = cp.repository.get_billable_since(dep_id)
    _tick(cp, since + timedelta(seconds=240), 240)
    assert _charged(cp) == 2
    _report(cp, dep_id, DeploymentState.TERMINATED)
    assert _charges(cp)[-1] == (1, "GPU usage 1× GPU @ $0.40/hr (final minute)")
    assert _row(cp, dep_id)["remainder"] == 0


def test_the_rate_counts_every_gpu(cp):
    dep_id = _ready(cp, _workload(cp, gpus=4), rate=40, ready_for=30)
    _report(cp, dep_id, DeploymentState.TERMINATED)
    # 4 × $0.40/hr for one minute = 2.67¢ → 3¢.
    assert _charges(cp) == [(3, "GPU usage 4× GPU @ $0.40/hr (final minute)")]


def test_a_rental_that_never_became_ready_costs_nothing(cp):
    workload = _workload(cp)
    dep = cp.repository.create_deployment(DeploymentRecord(
        workload_id=workload.workload_id, owner_user_id="u1", state=DeploymentState.STARTING,
        hotkey="hk1", node_id="n1", hourly_rate_cents=RATE,
    ))
    _report(cp, dep.deployment_id, DeploymentState.FAILED, error="image pull failed")
    cp.cleanup_deployment(dep.deployment_id)
    assert _charges(cp) == []


# --- exactly once ------------------------------------------------------------------


def test_stopping_it_again_settles_once(cp):
    dep_id = _ready(cp, ready_for=20)
    before = cp.repository.get_deployment(dep_id)
    _report(cp, dep_id, DeploymentState.TERMINATED)
    # The node re-reports, the user deletes it again, the reaper had it too.
    _report(cp, dep_id, DeploymentState.TERMINATED)
    cp.cleanup_deployment(dep_id, reason="user terminated")
    assert cp.settle_ready_spell(before) == 0
    cp.meter_usage(elapsed_seconds=60)
    assert _charged(cp) == 5


def test_a_meter_tick_after_the_stop_accrues_nothing(cp):
    """The meter listed it READY, then it was stopped (and settled) before
    the meter reached it."""
    dep_id = _ready(cp, ready_for=20)
    _report(cp, dep_id, DeploymentState.TERMINATED)
    assert cp.repository.accrue_metering(dep_id, add_mcents=50_000, billed_seconds=600) == (0, 0)
    assert _charged(cp) == 5


def test_several_ready_spells_each_pay_their_started_minutes(cp):
    """Paused (spend limit), resumed, paused again (idle), resumed, stopped."""
    dep_id = _ready(cp, ready_for=10)
    _report(cp, dep_id, DeploymentState.SUSPENDED, error="paused: spend limit", suspended_reason="spend_limit")
    assert _charged(cp) == 5  # 10 s: 1 minute
    # Paused time is never charged: resumed much later.
    _report(cp, dep_id, DeploymentState.READY)
    cp.repository.set_billable_since(dep_id, datetime.now(UTC) - timedelta(seconds=70))
    _report(cp, dep_id, DeploymentState.SUSPENDED, error="idle", suspended_reason="idle")
    assert _charged(cp) == 15  # + 70 s: 2 minutes
    _report(cp, dep_id, DeploymentState.READY)
    since = datetime.now(UTC) - timedelta(seconds=200)
    cp.repository.set_billable_since(dep_id, since)
    _tick(cp, since + timedelta(seconds=180), 180)  # 3 minutes, 15¢
    cp.cleanup_deployment(dep_id, reason="user terminated")
    assert _charged(cp) == 35  # + 200 s: 4 minutes
    assert sum(1 for _, text in _charges(cp) if text.endswith("(final minute)")) == 3


def test_a_tick_taken_before_the_spell_began_doesnt_move_its_clock_back(cp):
    """The meter took its `now`, then the pod became READY, then the meter
    reached it: that tick bills nothing and leaves the spell's clock at its
    start (it set it 5 s earlier, so a 56 s spell was charged 2 minutes)."""
    dep_id = _ready(cp, ready_for=56)
    since = cp.repository.get_billable_since(dep_id)
    _tick(cp, since - timedelta(seconds=5), 60)
    assert (_row(cp, dep_id)["seconds"], _row(cp, dep_id)["metered_until"]) == (0.0, since.replace(tzinfo=None))
    _report(cp, dep_id, DeploymentState.TERMINATED)
    assert _charged(cp) == 5  # 56 s: 1 minute


def test_becoming_ready_opens_the_spell_in_the_same_write(cp, monkeypatch):
    """No meter tick can find it READY with no spell (or the last one's)."""
    seen = []
    original = cp.repository.update_deployment

    def update(deployment, **kw):
        saved = original(deployment, **kw)
        seen.append((deployment.state, _row(cp, deployment.deployment_id)["metered_until"] is not None))
        return saved

    monkeypatch.setattr(cp.repository, "update_deployment", update)
    _ready(cp)
    assert seen == [(DeploymentState.READY, True)]


# --- billing pauses while the machine is silent --------------------------------------


def _heartbeat(cp, seen, hotkey="hk1"):
    cp.repository.upsert_heartbeat(Heartbeat(hotkey=hotkey, healthy=True, observed_at=seen))


def test_time_while_the_machine_was_silent_is_never_charged(cp):
    t0 = datetime.now(UTC) - timedelta(seconds=590)
    _heartbeat(cp, t0)
    dep_id = _ready(cp)
    cp.repository.set_billable_since(dep_id, t0)
    cp.meter_usage(elapsed_seconds=60, now=t0 + timedelta(seconds=60))  # reporting: 1 minute
    assert _charged(cp) == 5
    # The machine goes quiet (last heard at t0, the timeout is 120 s): ticks
    # charge nothing but the spell is accounted through them.
    for second in range(180, 600, 60):
        assert cp.meter_usage(elapsed_seconds=60, now=t0 + timedelta(seconds=second)) == {}
    # Stopped while still silent: no tail either. 60 s billed = 1 minute.
    _report(cp, dep_id, DeploymentState.TERMINATED)
    assert _charged(cp) == 5


def test_back_from_silence_and_stopped_before_the_next_tick_adds_no_tail(cp):
    """The machine comes back and its first report says the container is
    gone: the time since the last tick was silent, so it isn't charged."""
    t0 = datetime.now(UTC) - timedelta(seconds=659)
    _heartbeat(cp, t0)
    dep_id = _ready(cp)
    cp.repository.set_billable_since(dep_id, t0)
    cp.meter_usage(elapsed_seconds=60, now=t0 + timedelta(seconds=60))
    for second in range(180, 601, 60):
        cp.meter_usage(elapsed_seconds=60, now=t0 + timedelta(seconds=second))
    _heartbeat(cp, datetime.now(UTC))  # reporting again
    _report(cp, dep_id, DeploymentState.FAILED, error="container exited")
    assert _charged(cp) == 5  # the 60 s metered: 1 minute


def test_after_the_machine_is_back_a_real_tick_counts_again(cp):
    t0 = datetime.now(UTC) - timedelta(seconds=590)
    _heartbeat(cp, t0)
    dep_id = _ready(cp)
    cp.repository.set_billable_since(dep_id, t0)
    cp.meter_usage(elapsed_seconds=60, now=t0 + timedelta(seconds=60))  # 1 minute, 5¢
    for second in range(180, 480, 60):
        cp.meter_usage(elapsed_seconds=60, now=t0 + timedelta(seconds=second))  # silent
    _tick(cp, t0 + timedelta(seconds=540), 60)  # reporting again: another 60 s
    _report(cp, dep_id, DeploymentState.TERMINATED)
    # 120 s metered + the ~50 s since that tick = 3 minutes.
    assert _charged(cp) == 15


def test_a_stale_meter_tick_adds_at_most_90_seconds(cp):
    """The control plane was down for hours: the time since its last tick
    isn't all billable, and is capped."""
    dep_id = _ready(cp)
    cp.repository.set_billable_since(dep_id, datetime.now(UTC) - timedelta(hours=5))
    _report(cp, dep_id, DeploymentState.TERMINATED)
    assert _charged(cp) == 10  # 90 s: 2 minutes


# --- what else has a spell ----------------------------------------------------------


def test_a_private_model_pays_its_started_minutes_too(cp):
    model = _workload(cp, kind=WorkloadKind.INFERENCE, runtime={"model_identifier": "meta-llama/Llama-3.1-8B"})
    dep_id = _ready(cp, model, ready_for=10)
    _report(cp, dep_id, DeploymentState.SUSPENDED, error="idle", suspended_reason="idle")
    assert _charges(cp) == [(5, "Private model Llama-3.1-8B · 1 GPU at $3.00/hr (final minute)")]


def test_a_shared_catalog_model_has_nothing_to_settle(cp):
    catalog = cp.repository.upsert_workload(WorkloadSpec(
        workload_id="catalog-qwen", name="qwen", image="vllm", kind=WorkloadKind.INFERENCE, public=True,
        requirements={"gpu_count": 1}, metadata={"managed_by": "flux"},
    ))
    dep_id = _ready(cp, catalog, ready_for=30)
    _report(cp, dep_id, DeploymentState.TERMINATED)
    assert _charges(cp) == []


def test_an_empty_balance_carries_the_settlement_as_owed(cp):
    with session_scope(cp.repository.session_factory) as s:
        s.get(UserORM, "u1").balance_credits = 2
    dep_id = _ready(cp, ready_for=10)
    _report(cp, dep_id, DeploymentState.SUSPENDED, error="balance", suspended_reason="balance")
    assert _charged(cp) == 2  # what there was; never below zero
    assert _row(cp, dep_id)["remainder"] == 3000  # the other 3¢, charged on a resume
    with session_scope(cp.repository.session_factory) as s:
        assert s.get(UserORM, "u1").balance_credits == 0


def test_a_spell_left_open_by_another_path_is_settled_by_the_meter(cp):
    dep_id = _ready(cp, ready_for=10)
    dep = cp.repository.get_deployment(dep_id)
    dep.state = DeploymentState.TERMINATED  # a direct state write (no settlement)
    cp.repository.update_deployment(dep)
    cp.meter_usage(elapsed_seconds=60)
    assert _charged(cp) == 5
    cp.meter_usage(elapsed_seconds=60)
    assert _charged(cp) == 5


def test_the_reviewers_eleven_short_rentals(cp):
    """11 rentals at $0.40/hr READY 3-137 s each (~380 s), the meter ticking
    every minute: charged 1¢ in all before (the 137 s one's second tick);
    now each pays its started minutes at 0.67¢, rounded up per rental."""
    durations = [3, 5, 8, 10, 12, 15, 20, 25, 45, 100, 137]  # 380 s
    for seconds in durations:
        dep_id = _ready(cp, rate=40, ready_for=seconds)
        since = cp.repository.get_billable_since(dep_id)
        for tick in range(60, seconds, 60):
            _tick(cp, since + timedelta(seconds=tick), 60)
        _report(cp, dep_id, DeploymentState.TERMINATED)
    # 9 rentals of one started minute (0.67¢ → 1¢), one of two (1.33¢ → 2¢),
    # one of three (2.00¢).
    assert _charged(cp) == 9 + 2 + 2


def test_a_rental_running_since_before_spells_gets_one_on_its_next_tick(cp):
    """Already READY when this shipped: no spell until the meter's first
    tick, which opens one; from then on its stop is settled too."""
    workload = _workload(cp)
    dep = cp.repository.create_deployment(DeploymentRecord(
        workload_id=workload.workload_id, owner_user_id="u1", state=DeploymentState.READY, ready_instances=1,
        hotkey="hk1", node_id="n1", hourly_rate_cents=RATE, ready_at=datetime.now(UTC) - timedelta(days=2),
    ))
    assert _row(cp, dep.deployment_id)["metered_until"] is None
    _tick(cp, datetime.now(UTC) - timedelta(seconds=20), 90)  # 90 s: 7.5¢ → 7¢ now
    assert _charged(cp) == 7
    _report(cp, dep.deployment_id, DeploymentState.TERMINATED)
    assert _charged(cp) == 10  # 90 s + 20 s = 2 minutes


def test_migration_0081_adds_and_removes_the_columns_on_sqlite(tmp_path):
    import importlib.util
    import pathlib

    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = next(pathlib.Path("alembic/versions").glob("*_deployment_metering_spell.py"))
    spec = importlib.util.spec_from_file_location("m0081", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert (m.revision, m.down_revision) == ("20261117_0081", "20261117_0080")
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE deployments (deployment_id VARCHAR(64) PRIMARY KEY, state VARCHAR(32))"))
        conn.execute(sa.text("INSERT INTO deployments VALUES ('d1', 'ready')"))

    def run(fn):
        with engine.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                fn()

    def columns():
        return {c["name"] for c in sa.inspect(engine).get_columns("deployments")}

    new = {"metered_until", "metered_silent", "spell_billed_seconds", "spell_billed_mcents"}
    run(m.upgrade)
    assert new <= columns()
    with engine.connect() as conn:
        assert conn.execute(sa.text(
            "SELECT metered_until, metered_silent, spell_billed_seconds, spell_billed_mcents FROM deployments")
        ).first() == (None, None, None, None)
    run(m.downgrade)
    assert not new & columns()
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT state FROM deployments")).scalar() == "ready"


# --- pending usage (balance and forecast) -------------------------------------------


def test_pending_usage_is_what_ran_since_the_last_tick_plus_the_remainder(cp):
    dep_id = _ready(cp, rate=40, ready_for=100)
    since = cp.repository.get_billable_since(dep_id)
    _tick(cp, since + timedelta(seconds=60), 60)  # 0.67¢ carried, nothing debited
    # ~40 s since that tick at $0.40/hr (0.44¢) + the 0.67¢ carried = 1.11¢ → 2¢.
    assert cp.pending_usage_cents("u1") == 2
    _report(cp, dep_id, DeploymentState.TERMINATED)
    assert cp.pending_usage_cents("u1") == 0  # settled: 2 minutes, 2¢
    assert _charged(cp) == 2


def test_pending_usage_leaves_out_the_time_after_a_silent_tick(cp):
    dep_id = _ready(cp, ready_for=100)
    since = cp.repository.get_billable_since(dep_id)
    _heartbeat(cp, since - timedelta(minutes=10))
    cp.meter_usage(elapsed_seconds=60, now=since + timedelta(seconds=60))  # silent: skipped
    _heartbeat(cp, datetime.now(UTC))  # back, but no real tick yet
    assert cp.pending_usage_cents("u1") == 0


def test_pending_usage_leaves_out_a_silent_machine_and_counts_owed_cents(cp):
    dep_id = _ready(cp, ready_for=30)
    _heartbeat(cp, datetime.now(UTC) - timedelta(minutes=10))
    assert cp.pending_usage_cents("u1") == 0  # paused while its machine is silent
    _heartbeat(cp, datetime.now(UTC))
    assert cp.pending_usage_cents("u1") == 3  # 30 s at 5¢/min = 2.5¢ → 3¢
    with session_scope(cp.repository.session_factory) as s:
        s.get(UserORM, "u1").balance_credits = 0
    _report(cp, dep_id, DeploymentState.SUSPENDED, error="balance", suspended_reason="balance")
    assert cp.pending_usage_cents("u1") == 5  # the minute it couldn't pay, owed on a resume


def test_balance_and_forecast_show_pending_and_available(cp, monkeypatch):
    from types import SimpleNamespace

    from greencompute_gateway.application import credit_watch
    from greencompute_gateway.application.billing_service import BillingService
    from greencompute_gateway.infrastructure.billing_repository import BillingRepository
    from greencompute_gateway.transport import routes

    billing = BillingRepository()  # this test's database (the environment)
    monkeypatch.setattr(routes, "service", SimpleNamespace(control_plane=cp))
    monkeypatch.setattr(routes, "_get_billing", lambda: BillingService(billing))
    monkeypatch.setattr(credit_watch, "_billing_repo", lambda: billing)
    monkeypatch.setattr(routes, "require_api_key", lambda *a, **k: SimpleNamespace(user_id="u1", admin=False))
    _ready(cp, ready_for=30)
    balance = routes.billing_balance(authorization="Bearer k", x_api_key=None)
    assert (balance["balance_credits"], balance["balance_usd"]) == (10_000, 100.0)
    assert (balance["pending_usage_cents"], balance["available_cents"]) == (3, 9_997)
    forecast = routes.billing_forecast(authorization="Bearer k", x_api_key=None)
    assert forecast["burn_cents_per_hour"] == RATE
    assert (forecast["pending_usage_cents"], forecast["available_cents"]) == (3, 9_997)
