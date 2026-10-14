"""The public GPU pool says what can be deployed now, never how big the fleet
is: much of the capacity is rented by direct contract, so "14 of 16" would
undersell it. Admins still see totals."""
from types import SimpleNamespace

import pytest

from greencompute_control_plane.transport import routes, security


@pytest.fixture
def pool(monkeypatch):
    nodes = [
        SimpleNamespace(gpu_model="rtx4090", vram_gb_per_gpu=24, gpu_count=8, available_gpus=6),
        SimpleNamespace(gpu_model="rtx4090", vram_gb_per_gpu=24, gpu_count=8, available_gpus=0),
        SimpleNamespace(gpu_model="a4000", vram_gb_per_gpu=16, gpu_count=10, available_gpus=10),
    ]
    monkeypatch.setattr(routes.service.repository, "list_nodes", lambda: nodes)
    monkeypatch.setattr(routes.service, "_is_node_stale", lambda node: False)
    monkeypatch.setenv("GREENCOMPUTE_PUBLIC_RENTAL_GPU_FAMILIES", "4090,5090")


def test_public_sees_only_what_is_deployable_now(pool):
    assert routes.gpu_pool(authorization=None, x_api_key=None) == [
        {"gpu_model": "rtx4090", "vram_gb_per_gpu": 24, "available_gpus": 6, "rentable_now": True}
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
