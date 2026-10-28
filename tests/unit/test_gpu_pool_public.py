"""The public GPU pool says what can be deployed now, never how big the fleet
is: much of the capacity is rented by direct contract, so "14 of 16" would
undersell it. Admins still see totals."""
from types import SimpleNamespace

import pytest

from greencompute_control_plane.transport import routes, security


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


def test_public_sees_only_what_is_deployable_now(pool):
    assert routes.gpu_pool(authorization=None, x_api_key=None) == [
        {"gpu_model": "rtx4090", "vram_gb_per_gpu": 24, "available_gpus": 6, "max_gpus_per_deployment": 6,
         "rentable_now": True}
    ]


def test_an_ordinary_key_sees_the_public_view(pool, monkeypatch):
    monkeypatch.setattr(security.credential_store, "get_api_key_by_secret",
                        lambda secret: SimpleNamespace(admin=False))
    [row] = routes.gpu_pool(authorization="Bearer user-key", x_api_key=None)
    assert "total_gpus" not in row and "node_count" not in row


def test_admin_still_sees_totals(pool, monkeypatch):
    monkeypatch.setattr(security.credential_store, "get_api_key_by_secret",
                        lambda secret: SimpleNamespace(admin=True) if secret == "admin-key" else None)
    [row] = routes.gpu_pool(authorization=None, x_api_key="admin-key")
    assert (row["total_gpus"], row["available_gpus"], row["node_count"]) == (16, 6, 2)


def test_the_pool_counts_what_the_scheduler_can_place_not_the_raw_heartbeat(pool, monkeypatch):
    """Node a's heartbeat says 6 free, but 4 are already reserved for
    deployments being placed, and node b is drained: the scheduler could hand
    out 2. Advertising 6 made 3 of 3 creates fail (2026-10-22)."""
    nodes = routes.service.repository.list_nodes()
    monkeypatch.setattr(routes.service, "schedulable_capacity", lambda: [(nodes[0], 2)])
    [row] = routes.gpu_pool(authorization=None, x_api_key=None)
    assert row["available_gpus"] == 2
    assert row["rentable_now"] is True  # a usable node exists, even if full


def test_a_multi_gpu_rental_needs_its_gpus_on_one_machine(pool, monkeypatch):
    nodes = routes.service.repository.list_nodes()
    monkeypatch.setattr(routes.service, "schedulable_capacity", lambda: [(nodes[0], 4), (nodes[1], 4)])
    [row] = routes.gpu_pool(authorization=None, x_api_key=None)
    assert (row["available_gpus"], row["max_gpus_per_deployment"]) == (8, 4)


def test_drained_or_unhealthy_nodes_are_not_rentable(pool, monkeypatch):
    # Every 4090 miner drained for maintenance: nothing to rent, and the page
    # must not say "On request" as if a request could be placed.
    monkeypatch.setattr(routes.service, "schedulable_capacity", lambda: [])
    [row] = routes.gpu_pool(authorization=None, x_api_key=None)
    assert (row["available_gpus"], row["rentable_now"]) == (0, False)
