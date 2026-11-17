"""Warm-up was a GET with a side effect (external review, Nov 2026): POST is
now the endpoint; GET still answers, deprecated (out of the spec, with a
`Deprecation` header and a Link to POST)."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from greencompute_gateway.domain.scopes import required_scope
from greencompute_gateway.transport import routes


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(routes, "require_api_key", lambda *a, **k: SimpleNamespace(
        key_id="k1", user_id="u1", admin=False, scopes=[]))
    monkeypatch.setattr(routes.service, "get_workload", lambda wid, **k: SimpleNamespace() if wid == "w1" else None)
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app), app


def test_post_warms_up(client):
    c, _ = client
    r = c.post("/platform/workloads/w1/warmup")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert '"warmup_started"' in r.text and '"warmup_complete"' in r.text
    assert "deprecation" not in r.headers
    assert c.post("/platform/workloads/nope/warmup").status_code == 404


def test_get_still_works_but_says_it_is_deprecated(client):
    c, _ = client
    r = c.get("/platform/workloads/w1/warmup")
    assert r.status_code == 200 and '"warmup_complete"' in r.text
    assert r.headers["deprecation"] == "true"
    assert r.headers["link"] == '</platform/workloads/w1/warmup>; rel="successor-version"'


def test_only_post_is_in_the_spec(client):
    _, app = client
    ops = app.openapi()["paths"]["/platform/workloads/{workload_id}/warmup"]
    assert set(ops) == {"post"}


def test_post_needs_the_rentals_scope():
    assert required_scope("POST", "/platform/workloads/w1/warmup") == "rentals"
