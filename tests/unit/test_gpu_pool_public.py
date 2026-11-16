"""The public GPU pool says whether each GPU class can be deployed now, as a
state, never how many: much of the capacity is rented by direct contract, so
"14 of 16" would undersell it, and the owner keeps free counts private too
("only 3 left"). Admins still see the counts."""
from types import SimpleNamespace

import pytest

from greencompute_control_plane.transport import routes, security

COUNTS = {"available_gpus", "max_gpus_per_deployment", "total_gpus", "node_count", "schedulable_nodes"}


@pytest.fixture
def pool(monkeypatch):
    nodes = [
        SimpleNamespace(node_id="a", gpu_model="rtx4090", vram_gb_per_gpu=24, gpu_count=8, available_gpus=6),
        SimpleNamespace(node_id="b", gpu_model="rtx4090", vram_gb_per_gpu=24, gpu_count=8, available_gpus=0),
        SimpleNamespace(node_id="c", gpu_model="a4000", vram_gb_per_gpu=16, gpu_count=10, available_gpus=10),
    ]
    monkeypatch.setattr(routes.service.repository, "list_nodes", lambda: nodes)
    monkeypatch.setattr(routes.service, "_is_node_stale", lambda node: False)
    # What the scheduler could place on each node (net of reservations); node
    # b is drained, so it isn't there at all.
    monkeypatch.setattr(routes.service, "schedulable_capacity", lambda: [(nodes[0], 6), (nodes[2], 10)])
    monkeypatch.setenv("GREENCOMPUTE_PUBLIC_RENTAL_GPU_FAMILIES", "4090,5090")
    monkeypatch.delenv("GREENCOMPUTE_ALMOST_FULLY_RENTED_PERCENT", raising=False)
    return nodes


@pytest.fixture
def admin(monkeypatch):
    monkeypatch.setattr(security.credential_store, "get_api_key_by_secret",
                        lambda secret: SimpleNamespace(admin=True) if secret == "admin-key" else None)


def _public():
    [row] = routes.gpu_pool(authorization=None, x_api_key=None)
    return row


def _admin():
    [row] = routes.gpu_pool(authorization=None, x_api_key="admin-key")
    return row


def test_public_sees_a_state_not_a_count(pool):
    assert routes.gpu_pool(authorization=None, x_api_key=None) == [
        {"gpu_model": "rtx4090", "vram_gb_per_gpu": 24, "rentable_now": True, "availability": "available"}
    ]


def test_an_ordinary_key_sees_the_public_view(pool, monkeypatch):
    monkeypatch.setattr(security.credential_store, "get_api_key_by_secret",
                        lambda secret: SimpleNamespace(admin=False))
    [row] = routes.gpu_pool(authorization="Bearer user-key", x_api_key=None)
    assert not COUNTS & set(row)


def test_admin_still_sees_the_counts(pool, admin):
    row = _admin()
    assert (row["total_gpus"], row["available_gpus"], row["node_count"]) == (16, 6, 2)
    assert row["availability"] == "available"


def test_the_pool_counts_what_the_scheduler_can_place_not_the_raw_heartbeat(pool, monkeypatch, admin):
    """Node a's heartbeat says 6 free, but 5 are already reserved for
    deployments being placed, and node b is drained: the scheduler could hand
    out 1 of the 8 it would use. Advertising 6 made 3 of 3 creates fail
    (2026-10-22)."""
    monkeypatch.setattr(routes.service, "schedulable_capacity", lambda: [(pool[0], 1)])
    assert _admin()["available_gpus"] == 1
    row = _public()
    assert (row["availability"], row["rentable_now"]) == ("almost_fully_rented", True)


def test_every_gpu_rented_is_fully_rented_not_on_request(pool, monkeypatch):
    monkeypatch.setattr(routes.service, "schedulable_capacity", lambda: [(pool[0], 0)])
    row = _public()
    assert (row["availability"], row["rentable_now"]) == ("fully_rented", True)


def test_a_multi_gpu_rental_needs_its_gpus_on_one_machine(pool, monkeypatch, admin):
    monkeypatch.setattr(routes.service, "schedulable_capacity", lambda: [(pool[0], 4), (pool[1], 4)])
    row = _admin()
    assert (row["available_gpus"], row["max_gpus_per_deployment"]) == (8, 4)


def test_drained_or_unhealthy_nodes_are_not_rentable(pool, monkeypatch):
    # Every 4090 miner drained for maintenance: nothing to rent, and nothing
    # to call rented out either, so it is on request.
    monkeypatch.setattr(routes.service, "schedulable_capacity", lambda: [])
    row = _public()
    assert (row["availability"], row["rentable_now"]) == ("on_request", False)


def test_an_override_never_changes_the_public_state(pool, monkeypatch, admin):
    """Sales can set the admin view's numbers for contracted hardware, but a
    public "available" from numbers no machine backs would put a Rent button
    on GPUs nobody can rent."""
    from contextlib import contextmanager

    rows = [SimpleNamespace(gpu_model="rtx4090", total_gpus=64, available_gpus=40, note="contract"),
            SimpleNamespace(gpu_model="rtx5090", total_gpus=32, available_gpus=32, note="")]

    class Session:
        def execute(self, *a, **k):
            return SimpleNamespace(all=lambda: rows)

    @contextmanager
    def session_factory():
        yield Session()

    monkeypatch.setattr(routes.service.repository, "session_factory", session_factory)
    monkeypatch.setattr(routes.service, "schedulable_capacity", lambda: [(pool[0], 0)])
    public = {r["gpu_model"]: r for r in routes.gpu_pool(authorization=None, x_api_key=None)}
    assert public["rtx4090"]["availability"] == "fully_rented"
    assert (public["rtx5090"]["availability"], public["rtx5090"]["rentable_now"]) == ("on_request", False)
    assert all(not COUNTS & set(r) for r in public.values())
    assert {r["gpu_model"]: r["available_gpus"] for r in routes.gpu_pool(authorization=None, x_api_key="admin-key")} \
        == {"rtx4090": 40, "rtx5090": 32}
