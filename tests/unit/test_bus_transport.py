from __future__ import annotations

from datetime import UTC, datetime

from greenference_persistence import NatsJetStreamBus, SubjectBus, WorkflowEventRepository, create_subject_bus
from greenference_persistence.bus import BusMessage


def test_create_subject_bus_uses_durable_for_auto_without_nats() -> None:
    shared_db = "sqlite+pysqlite:///:memory:"
    workflow_repository = WorkflowEventRepository(database_url=shared_db, bootstrap=True)

    bus = create_subject_bus(
        database_url=shared_db,
        bootstrap=True,
        workflow_repository=workflow_repository,
        transport="auto",
    )

    assert isinstance(bus, SubjectBus)


def test_create_subject_bus_returns_nats_wrapper_when_requested() -> None:
    shared_db = "sqlite+pysqlite:///:memory:"
    workflow_repository = WorkflowEventRepository(database_url=shared_db, bootstrap=True)

    bus = create_subject_bus(
        database_url=shared_db,
        bootstrap=True,
        workflow_repository=workflow_repository,
        transport="nats",
        nats_url="nats://nats:4222",
    )

    assert isinstance(bus, NatsJetStreamBus)
    assert bus.active_transport in {"nats", "durable"}


def test_nats_publish_uses_transport_when_available(monkeypatch) -> None:
    shared_db = "sqlite+pysqlite:///:memory:"
    workflow_repository = WorkflowEventRepository(database_url=shared_db, bootstrap=True)
    durable_bus = SubjectBus(
        database_url=shared_db,
        bootstrap=True,
        workflow_repository=workflow_repository,
    )
    bus = NatsJetStreamBus(durable_bus=durable_bus, nats_url="nats://nats:4222", enabled=True)
    bus.client_available = True
    published: list[tuple[str, str]] = []

    def fake_publish(subject: str, event) -> None:
        published.append((subject, event.event_id))

    monkeypatch.setattr(bus, "_publish_to_nats", fake_publish)

    event = bus.publish("build.accepted", {"build_id": "build-1"})

    assert published == [("build.accepted", event.event_id)]


def test_nats_publish_falls_back_to_durable_when_transport_publish_fails(monkeypatch) -> None:
    shared_db = "sqlite+pysqlite:///:memory:"
    workflow_repository = WorkflowEventRepository(database_url=shared_db, bootstrap=True)
    durable_bus = SubjectBus(
        database_url=shared_db,
        bootstrap=True,
        workflow_repository=workflow_repository,
    )
    bus = NatsJetStreamBus(durable_bus=durable_bus, nats_url="nats://nats:4222", enabled=True)
    bus.client_available = True

    def fail_publish(subject: str, event) -> None:
        raise RuntimeError("nats unavailable")

    monkeypatch.setattr(bus, "_publish_to_nats", fail_publish)

    event = bus.publish("build.accepted", {"build_id": "build-1"})
    persisted = workflow_repository.list_events(subjects=["build.accepted"], statuses=["pending"])

    assert event.event_id == persisted[0].event_id


def test_nats_claim_pending_uses_fetched_messages(monkeypatch) -> None:
    shared_db = "sqlite+pysqlite:///:memory:"
    workflow_repository = WorkflowEventRepository(database_url=shared_db, bootstrap=True)
    durable_bus = SubjectBus(
        database_url=shared_db,
        bootstrap=True,
        workflow_repository=workflow_repository,
    )
    bus = NatsJetStreamBus(durable_bus=durable_bus, nats_url="nats://nats:4222", enabled=True)
    bus.client_available = True
    event = durable_bus.publish("build.accepted", {"build_id": "build-1"})
    message = BusMessage(
        delivery_id=1,
        event_id=event.event_id,
        consumer="builder-worker",
        subject="build.accepted",
        payload={"build_id": "build-1"},
        attempts=1,
        available_at=datetime.now(UTC),
        last_error=None,
    )

    def fake_claim(consumer: str, subjects: list[str], limit: int = 10) -> list[BusMessage]:
        assert consumer == "builder-worker"
        assert subjects == ["build.accepted"]
        assert limit == 10
        return [message]

    monkeypatch.setattr(bus, "_claim_pending_from_nats", fake_claim)

    claimed = bus.claim_pending("builder-worker", ["build.accepted"])

    assert claimed == [message]


def test_nats_mark_completed_acks_pending_message(monkeypatch) -> None:
    shared_db = "sqlite+pysqlite:///:memory:"
    workflow_repository = WorkflowEventRepository(database_url=shared_db, bootstrap=True)
    durable_bus = SubjectBus(
        database_url=shared_db,
        bootstrap=True,
        workflow_repository=workflow_repository,
    )
    bus = NatsJetStreamBus(durable_bus=durable_bus, nats_url="nats://nats:4222", enabled=True)

    event = durable_bus.publish("build.accepted", {"build_id": "build-1"})
    claimed = durable_bus.claim_pending("builder-worker", ["build.accepted"])
    assert len(claimed) == 1
    delivery_id = claimed[0].delivery_id
    raw_message = object()
    client = object()
    bus._pending_messages[delivery_id] = (client, raw_message)
    acked: list[object] = []
    closed: list[object] = []

    def fake_ack(message: object) -> None:
        acked.append(message)

    def fake_close(message_client: object) -> None:
        closed.append(message_client)

    monkeypatch.setattr(bus, "_ack_message", fake_ack)
    monkeypatch.setattr(bus, "_close_client_if_unused", fake_close)

    result = bus.mark_completed(delivery_id)

    assert result is not None
    assert result.event_id == event.event_id
    assert acked == [raw_message]
    assert closed == [client]
