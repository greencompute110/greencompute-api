"""Scoped API keys: an agent can be given a key that only reads, or only calls
models, so a leaked or prompt-injected key can't rent machines, fetch SSH
access or top up the account. Keys without scopes keep full access.

Requests go through the real ASGI app (middleware included) so the check is
exercised the way production runs it."""
import asyncio
import json

import pytest

from greencompute_gateway.domain.scopes import normalize_scopes, required_scope
from greencompute_gateway.main import app
from greencompute_gateway.transport import routes, security
from greencompute_protocol import APIKeyRecord

KEYS = {
    "full": APIKeyRecord(key_id="k-full", name="full", user_id="u1", scopes=[], secret="full"),
    "read": APIKeyRecord(key_id="k-read", name="read", user_id="u1", scopes=["read"], secret="read"),
    "infer": APIKeyRecord(key_id="k-inf", name="infer", user_id="u1", scopes=["inference"], secret="infer"),
    "keys": APIKeyRecord(key_id="k-keys", name="keys", user_id="u1", scopes=["keys", "read"], secret="keys"),
}


def call(method: str, path: str, key: str, body: dict | None = None) -> tuple[int, dict]:
    """One request through the ASGI app, without an HTTP client library."""
    raw = json.dumps(body).encode() if body is not None else b""
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
        "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
        "headers": [(b"authorization", f"Bearer {key}".encode()), (b"content-type", b"application/json"),
                    (b"host", b"api.test")],
        "client": ("203.0.113.9", 1234), "server": ("api.test", 80),
    }
    sent = {"status": None, "body": b""}
    chunks = [{"type": "http.request", "body": raw, "more_body": False}]

    async def receive():
        return chunks.pop(0) if chunks else {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            sent["status"] = message["status"]
        elif message["type"] == "http.response.body":
            sent["body"] += message.get("body", b"")

    asyncio.run(app(scope, receive, send))
    return sent["status"], json.loads(sent["body"] or b"null")


@pytest.fixture(autouse=True)
def fake_keys(monkeypatch):
    monkeypatch.setattr(security.credential_store, "get_api_key_by_secret", lambda secret: KEYS.get(secret))
    monkeypatch.setattr(routes.service, "list_deployments", lambda **kw: [])
    saved = []
    monkeypatch.setattr(routes.service.repository, "save_api_key", lambda rec: saved.append(rec) or rec)
    return saved


def _scope_error(status, body):
    return status == 403 and isinstance(body.get("detail"), dict) and "required_scope" in body["detail"]


def test_read_only_key_reads_but_cannot_rent_or_get_ssh():
    assert call("GET", "/platform/deployments", "read")[0] == 200
    status, body = call("POST", "/platform/deployments", "read", {"workload_id": "w"})
    assert _scope_error(status, body) and body["detail"]["required_scope"] == "rentals"
    assert _scope_error(*call("GET", "/platform/deployments/d1/ssh", "read"))
    assert _scope_error(*call("POST", "/platform/billing/topup/crypto", "read", {"currency": "tao", "amount_usd": 5}))


def test_inference_only_key_cannot_read_the_account():
    status, body = call("GET", "/platform/deployments", "infer")
    assert _scope_error(status, body) and body["detail"]["required_scope"] == "read"


def test_full_access_key_is_unrestricted(monkeypatch):
    def missing(*a, **k):
        raise KeyError("workload not found: missing")

    monkeypatch.setattr(routes.service, "create_deployment", missing)
    assert call("GET", "/platform/deployments", "full")[0] == 200
    status, body = call("POST", "/platform/deployments", "full", {"workload_id": "missing"})
    assert status == 404 and not _scope_error(status, body)  # got past the scope check


def test_scoped_keys_mint_only_narrower_keys(fake_keys):
    # A full key can mint a read-only key.
    status, body = call("POST", "/platform/api-keys", "full", {"name": "agent", "scopes": ["read", "inference"]})
    assert status == 200 and body["scopes"] == ["inference", "read"]
    # A keys+read key can mint a read-only key...
    assert call("POST", "/platform/api-keys", "keys", {"name": "r", "scopes": ["read"]})[0] == 200
    # ...but not a broader one, and never a full-access one.
    assert call("POST", "/platform/api-keys", "keys", {"name": "x", "scopes": ["rentals"]})[0] == 403
    assert call("POST", "/platform/api-keys", "keys", {"name": "x", "scopes": []})[0] == 403
    # A key without the keys scope can't mint at all.
    assert _scope_error(*call("POST", "/platform/api-keys", "read", {"name": "x", "scopes": ["read"]}))
    # Unknown scope names are refused.
    assert call("POST", "/platform/api-keys", "full", {"name": "x", "scopes": ["root"]})[0] == 400


@pytest.mark.parametrize("method, path, scope", [
    ("POST", "/v1/chat/completions", "inference"),
    ("GET", "/v1/models", "inference"),
    ("GET", "/platform/deployments", "read"),
    ("POST", "/platform/deployments", "rentals"),
    ("DELETE", "/platform/deployments/d1", "rentals"),
    ("POST", "/platform/workloads", "rentals"),
    ("GET", "/platform/deployments/d1/ssh", "rentals"),
    ("GET", "/platform/billing/balance", "read"),
    ("POST", "/platform/billing/topup/crypto", "billing"),
    ("POST", "/platform/billing/wallets", "billing"),
    ("GET", "/platform/api-keys", "keys"),
    ("DELETE", "/platform/api-keys/k1", "keys"),
    ("PATCH", "/platform/me/profile", None),
])
def test_scope_map(method, path, scope):
    assert required_scope(method, path) == scope


def test_scope_names_are_validated():
    assert normalize_scopes([" Read", "read", "inference"]) == ["inference", "read"]
    with pytest.raises(ValueError):
        normalize_scopes(["admin"])
