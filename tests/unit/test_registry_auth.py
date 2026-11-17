"""GET /registry/auth handed every account the same static Basic credential
(":greencompute-registry") for the builder's registry (external review, Nov
2026). No customer registry runs and nothing pulls with it, and one shared
password would mean cross-tenant push/pull on any registry that did; so it is
turned off (501) and out of the spec until there are per-account credentials."""
import base64
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from greencompute_gateway.transport import routes
from greencompute_gateway.transport.errors import install_error_handlers


@pytest.fixture
def client(monkeypatch):
    def fake_key(authorization, x_api_key, **kw):
        if authorization != "Bearer gk_test":
            raise HTTPException(status_code=401, detail="invalid api key")
        return SimpleNamespace(key_id="k1", user_id="u1", admin=False, scopes=[])

    monkeypatch.setattr(routes, "require_api_key", fake_key)
    app = FastAPI()
    app.include_router(routes.router)
    install_error_handlers(app)
    return TestClient(app), app


def test_no_registry_credential_is_handed_out(client):
    c, _ = client
    r = c.get("/registry/auth", headers={"Authorization": "Bearer gk_test"})
    assert r.status_code == 501 and r.json()["error"]["code"] == "not_implemented"
    static = base64.b64encode(b":greencompute-registry").decode()
    assert static not in r.text and "auth_header" not in r.text


def test_it_still_needs_a_key_and_is_not_in_the_spec(client):
    c, app = client
    assert c.get("/registry/auth").status_code == 401
    assert "/registry/auth" not in app.openapi()["paths"]
