"""limit <= 0 or a negative offset used to be answered with a default page (or
a database error); now it is a 422 `validation_error` naming the parameter
(external review, Nov 2026). Through the real ASGI app."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from greencompute_gateway.main import app
from greencompute_gateway.transport import routes, security
from greencompute_protocol import APIKeyRecord

KEY = APIKeyRecord(key_id="k1", name="k", user_id="u1", scopes=[], secret="k")
ADMIN = APIKeyRecord(key_id="k-adm", name="adm", user_id="u-adm", admin=True, scopes=[], secret="adm")


@pytest.fixture
def client(monkeypatch):
    keys = {"k": KEY, "adm": ADMIN}
    monkeypatch.setattr(security.credential_store, "get_api_key_by_secret", lambda secret: keys.get(secret))
    return TestClient(app)


@pytest.mark.parametrize("path, param", [
    ("/platform/billing/ledger?limit=0", "limit"),
    ("/platform/billing/ledger?limit=-5", "limit"),
    ("/platform/billing/ledger?offset=-1", "offset"),
    ("/platform/events?limit=0", "limit"),
    ("/platform/events?after=-1", "after"),
    ("/platform/deployments/d1/logs?lines=0", "lines"),
    ("/platform/deployments?limit=0", "limit"),
    ("/platform/billing/crypto/deposits?limit=0", "limit"),
])
def test_a_bad_page_is_refused_by_name(client, path, param):
    r = client.get(path, headers={"Authorization": "Bearer k"})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "validation_error"
    assert r.json()["detail"][0]["loc"] == ["query", param]


@pytest.mark.parametrize("path", [
    "/platform/v1/invocations?limit=0",
    "/platform/v1/invocations/exports/recent?limit=-1",
    "/platform/v1/debug/routing-decisions?limit=0",
    "/platform/v1/debug/invocation-failures?limit=0",
    "/platform/billing/admin/crypto/invoices?limit=0",
    "/platform/billing/admin/crypto/deposits?limit=0",
    "/platform/billing/admin/miner-revenue?window_hours=0",
    "/platform/admin/analytics/gpu-usage?days=0",
])
def test_admin_lists_too(client, path):
    assert client.get(path, headers={"Authorization": "Bearer adm"}).status_code == 422


def test_a_good_page_still_works(client, monkeypatch):
    seen = {}

    class Billing:
        def list_ledger(self, user_id, limit, offset):
            seen.update(limit=limit, offset=offset)
            return []

    monkeypatch.setattr(routes, "_get_billing", lambda: Billing())
    assert client.get("/platform/billing/ledger?limit=20&offset=40", headers={"Authorization": "Bearer k"}).json() == []
    assert seen == {"limit": 20, "offset": 40}
    client.get("/platform/billing/ledger?limit=100000", headers={"Authorization": "Bearer k"})
    assert seen["limit"] == routes.LEDGER_PAGE_MAX  # a huge page is capped, not refused


def test_in_process_callers_still_get_the_defaults(monkeypatch):
    """Handlers are also called directly (MCP tools, tests): the defaults are
    plain ints, not Query objects."""
    import inspect

    for fn in (routes.billing_ledger, routes.list_account_events, routes.get_deployment_logs):
        for p in inspect.signature(fn).parameters.values():
            if p.name in ("limit", "offset", "after", "lines"):
                assert isinstance(p.default, int), (fn.__name__, p.name)


def _service_client(module):
    other = FastAPI()
    other.include_router(module.router)
    return TestClient(other)


def test_the_other_services_lists_too(monkeypatch):
    from greencompute_control_plane.transport import routes as cp_routes
    from greencompute_validator.transport import routes as v_routes

    v = _service_client(v_routes)
    assert v.get("/validator/v1/audit/reports?offset=-1").status_code == 422
    assert v.get("/validator/v1/audit/reports?limit=0").status_code == 422
    cp = _service_client(cp_routes)
    for path in ("/platform/v1/debug/capacity-history?limit=0", "/platform/v1/debug/placements?limit=-1",
                 "/platform/v1/debug/lease-history?limit=0"):
        assert cp.get(path).status_code == 422, path
