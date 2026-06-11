"""Regression tests for the 2026-06-10 review event-bus findings.

A handler exception used to orphan the claimed delivery in 'processing'
forever during normal operation: claim_pending only selects 'pending' rows
and stale-recovery ran exclusively at startup — so one transient error
wedged the deployment until the control-plane was restarted (hit live with
a gpu_count=10 pod). process_pending_events now marks a raising event back
to retryable-pending with backoff, parks it as terminally 'failed' once the
attempt budget is spent, and the worker loop runs stale-recovery
periodically as a backstop.
"""
from datetime import UTC, datetime, timedelta

import pytest
from greencompute_persistence import session_scope
from greencompute_persistence.bus import SubjectBus
from greencompute_persistence.orm import BusDeliveryORM


def utcnow() -> datetime:
    return datetime.now(UTC)


@pytest.fixture
def stack(tmp_path):
    from greencompute_control_plane.application.services import ControlPlaneService
    from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository

    db_url = f"sqlite+pysqlite:///{tmp_path / 'bus.db'}"
    repository = ControlPlaneRepository(database_url=db_url, bootstrap=True)
    bus = SubjectBus(engine=repository.engine, session_factory=repository.session_factory)
    service = ControlPlaneService(repository, bus=bus)
    return service, bus


def _delivery_row(bus: SubjectBus, delivery_id: int) -> dict:
    with session_scope(bus.session_factory) as s:
        row = s.get(BusDeliveryORM, delivery_id)
        return {
            "status": row.status,
            "attempts": row.attempts,
            "last_error": row.last_error,
            "available_at": row.available_at,
        }


def _make_claimable(bus: SubjectBus, delivery_id: int) -> None:
    with session_scope(bus.session_factory) as s:
        row = s.get(BusDeliveryORM, delivery_id)
        row.available_at = utcnow() - timedelta(seconds=1)


def test_raising_handler_requeues_event_instead_of_orphaning(stack):
    service, bus = stack
    service._process_deployment_request = _boom
    event = bus.publish("deployment.requested", {"deployment_id": "d1"})

    service.process_pending_events()

    deliveries = bus.list_deliveries(consumer="control-plane-worker")
    assert len(deliveries) == 1
    state = _delivery_row(bus, deliveries[0].delivery_id)
    # The pre-fix behavior left status='processing' forever.
    assert state["status"] == "pending"
    assert "boom" in state["last_error"]
    assert state["attempts"] == 1
    assert event.event_id == deliveries[0].event_id


def test_poison_event_parks_as_failed_after_attempt_budget(stack):
    service, bus = stack
    service._process_deployment_request = _boom
    bus.publish("deployment.requested", {"deployment_id": "d1"})
    delivery_id = bus.list_deliveries(consumer="control-plane-worker")[0].delivery_id

    for _ in range(service._MAX_EVENT_ATTEMPTS):
        _make_claimable(bus, delivery_id)
        service.process_pending_events()

    state = _delivery_row(bus, delivery_id)
    assert state["status"] == "failed"
    assert state["attempts"] == service._MAX_EVENT_ATTEMPTS

    # Terminally failed events are never claimed again.
    _make_claimable(bus, delivery_id)
    assert bus.claim_pending("control-plane-worker", ["deployment.requested"]) == []


def test_one_raising_event_does_not_block_the_next(stack):
    service, bus = stack
    processed = []

    def selective(event):
        if event.payload.get("deployment_id") == "poison":
            raise RuntimeError("boom")
        processed.append(event.payload["deployment_id"])
        bus.mark_completed(event.delivery_id)
        return None

    service._process_deployment_request = selective
    bus.publish("deployment.requested", {"deployment_id": "poison"})
    bus.publish("deployment.requested", {"deployment_id": "healthy"})

    service.process_pending_events()

    assert processed == ["healthy"]


def test_periodic_recovery_threshold_spares_fresh_inflight_events(stack):
    service, bus = stack
    bus.publish("deployment.requested", {"deployment_id": "d1"})
    claimed = bus.claim_pending("control-plane-worker", ["deployment.requested"])
    assert len(claimed) == 1
    delivery_id = claimed[0].delivery_id

    # Freshly claimed (simulating a slow-but-alive handler): a conservative
    # periodic threshold must NOT requeue it...
    result = service.recover_inflight_events(stale_after_seconds=900.0)
    assert result["requeued_deliveries"] == 0
    assert _delivery_row(bus, delivery_id)["status"] == "processing"

    # ...but once genuinely stale (orphaned by a crash), it is reclaimed.
    with session_scope(bus.session_factory) as s:
        s.get(BusDeliveryORM, delivery_id).updated_at = utcnow() - timedelta(seconds=901)
    result = service.recover_inflight_events(stale_after_seconds=900.0)
    assert result["requeued_deliveries"] == 1
    assert _delivery_row(bus, delivery_id)["status"] == "pending"


def _boom(event):
    raise RuntimeError("boom")
