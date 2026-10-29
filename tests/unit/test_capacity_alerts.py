""""Tell me when this GPU is free" (request #30): fires once, when there is
room, as an account event and an email; expires after 7 days."""
from datetime import UTC, datetime, timedelta

import pytest

from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository
from greencompute_gateway.application import capacity_alerts as ca
from greencompute_gateway.application import webhooks as wh
from greencompute_gateway.domain.scopes import required_scope

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


@pytest.fixture
def sf(tmp_path):
    return ControlPlaneRepository(database_url=f"sqlite+pysqlite:///{tmp_path / 'ca.db'}", bootstrap=True).session_factory


class Mail:
    def __init__(self):
        self.sent = []

    def __call__(self, session_factory, user_id, kind, fields, *, event_id):
        self.sent.append((user_id, kind, fields, event_id))


def test_an_alert_is_for_a_gpu_we_rent_and_sane_numbers(sf):
    alert = ca.create_alert(sf, "u1", "RTX 4090", 1, 2, now=NOW)
    assert (alert["gpu_model"], alert["gpu_count"], alert["count"], alert["state"]) == ("rtx4090", 1, 2, "waiting")
    assert alert["expires_at"] == (NOW + timedelta(days=7)).isoformat()
    # Asking again for the same returns it, not a second one.
    assert ca.create_alert(sf, "u1", "rtx-4090", 1, 2, now=NOW)["alert_id"] == alert["alert_id"]
    for bad, error in [(("a100", 1, 1), "rent out"), (("4090", 1, 1), "rent out"), (("rtx4090ti", 1, 1), "rent out"),
                       (("rtx4090", 0, 1), "gpu_count"), (("rtx4090", 9, 1), "gpu_count"),
                       (("rtx4090", 1, 0), "count"), (("rtx4090", True, 1), "gpu_count")]:
        with pytest.raises(ca.CapacityAlertError, match=error):
            ca.create_alert(sf, "u1", *bad, now=NOW)


def test_at_most_five_wait_at_once(sf):
    for n in range(1, 6):
        ca.create_alert(sf, "u1", "rtx4090", 1, n, now=NOW)
    with pytest.raises(ca.CapacityAlertError, match="at most 5"):
        ca.create_alert(sf, "u1", "rtx4090", 1, 6, now=NOW)


def test_it_fires_once_when_there_is_room_with_an_event_and_an_email(sf):
    alert = ca.create_alert(sf, "u1", "rtx4090", 1, 2, now=NOW)
    mail = Mail()
    assert ca.check_once(sf, {"rtx4090": [1]}, notify=mail, now=NOW) == []
    room = {"rtx4090": [2, 1]}
    assert ca.check_once(sf, room, notify=mail, now=NOW) == [alert["alert_id"]]
    assert ca.check_once(sf, room, notify=mail, now=NOW) == []  # once
    [(uid, kind, fields, event_id)] = mail.sent
    assert (uid, kind, fields["available_gpus"], event_id) == ("u1", "gpu_available", 3, f"gpu_available:{alert['alert_id']}")
    assert fields["max_gpus_per_deployment"] == 2
    [event] = wh.list_events(sf, "u1")["events"]
    assert event["type"] == "capacity.available" and event["data"]["gpu_model"] == "rtx4090"
    assert ca.list_alerts(sf, "u1", now=NOW)[0]["state"] == "fired"


def test_a_multi_gpu_rental_needs_that_many_free_on_one_machine(sf):
    ca.create_alert(sf, "u1", "rtx4090", 4, 2, now=NOW)
    assert ca.check_once(sf, {"rtx4090": [3, 3]}, notify=Mail(), now=NOW) == []  # 6 free, never 4 together
    assert ca.check_once(sf, {"rtx4090": [4, 1, 1, 1, 1]}, notify=Mail(), now=NOW) == []  # 8 free, one 4-GPU rental
    assert len(ca.check_once(sf, {"rtx4090": [4, 5]}, notify=Mail(), now=NOW)) == 1  # two fit


def test_old_alerts_expire_quietly_and_cancelled_ones_never_fire(sf):
    ca.create_alert(sf, "u1", "rtx5090", 1, 1, now=NOW)
    kept = ca.create_alert(sf, "u2", "rtx4090", 1, 1, now=NOW)
    ca.cancel_alert(sf, "u2", kept["alert_id"])
    mail = Mail()
    room = {"rtx5090": [8], "rtx4090": [8]}
    assert ca.check_once(sf, room, notify=mail, now=NOW + timedelta(days=7, seconds=1)) == []
    assert mail.sent == []
    assert ca.list_alerts(sf, "u1", now=NOW + timedelta(days=7, seconds=1))[0]["state"] == "expired"
    with pytest.raises(ca.CapacityAlertNotFound):
        ca.cancel_alert(sf, "u1", kept["alert_id"])  # not theirs


def test_scopes():
    assert required_scope("GET", "/platform/capacity-alerts") == "read"
    assert required_scope("POST", "/platform/capacity-alerts") == "rentals"
    assert required_scope("DELETE", "/platform/capacity-alerts/ca_1") == "rentals"


def test_several_alerts_firing_together_send_one_email(sf):
    for n in (1, 2, 3):
        ca.create_alert(sf, "u1", "rtx4090", 1, n, now=NOW)
    mail = Mail()
    assert len(ca.check_once(sf, {"rtx4090": [8]}, notify=mail, now=NOW)) == 3
    [(_, _, fields, _)] = mail.sent
    assert fields["count"] == 3  # the biggest ask that was met
    assert len(wh.list_events(sf, "u1")["events"]) == 3  # one event per alert


def test_expired_alerts_free_their_slot_even_if_the_check_never_ran(sf):
    for n in range(1, 6):
        ca.create_alert(sf, "u1", "rtx4090", 1, n, now=NOW)
    later = NOW + timedelta(days=7, minutes=1)
    assert ca.create_alert(sf, "u1", "rtx4090", 1, 1, now=later)["state"] == "waiting"
    assert [a["state"] for a in ca.list_alerts(sf, "u1", now=later)].count("waiting") == 1


def test_free_gpus_are_counted_per_machine_from_the_scheduler():
    from types import SimpleNamespace

    capacity = [(SimpleNamespace(gpu_model="RTX 4090"), 3), (SimpleNamespace(gpu_model="rtx-4090"), 0),
                (SimpleNamespace(gpu_model="rtx4090"), 5)]
    assert ca.free_by_model(capacity) == {"rtx4090": [3, 5]}
