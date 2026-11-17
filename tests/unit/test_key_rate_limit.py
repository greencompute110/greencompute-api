"""A per-key request rate on every authenticated /platform route and /mcp
(external review, Nov 2026: 80 requests in 1.7 s all got 200, no 429, no
X-RateLimit headers). A token bucket per API key, never per IP (every client
comes from the host's one NAT address), in memory (no database round-trip),
admin keys exempt. Requests go through the real ASGI app (middleware and all)."""
import pytest
from fastapi.testclient import TestClient

from greencompute_gateway.main import app
from greencompute_gateway.transport import routes, security
from greencompute_persistence import TokenBucketRateLimiter
from greencompute_protocol import APIKeyRecord

KEYS = {
    "ann": APIKeyRecord(key_id="k-ann", name="ann", user_id="u-ann", scopes=[], secret="ann"),
    "bob": APIKeyRecord(key_id="k-bob", name="bob", user_id="u-bob", scopes=[], secret="bob"),
    "ops": APIKeyRecord(key_id="k-ops", name="ops", user_id="u-ops", admin=True, scopes=[], secret="ops"),
}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(security.credential_store, "get_api_key_by_secret", lambda secret: KEYS.get(secret))
    monkeypatch.setattr(routes.service, "list_deployments", lambda **kw: [])
    monkeypatch.setenv("GREENCOMPUTE_KEY_RATE_PER_SECOND", "1")
    monkeypatch.setenv("GREENCOMPUTE_KEY_RATE_BURST", "5")
    return TestClient(app)


def _get(c, key, path="/platform/deployments", **kw):
    return c.get(path, headers={"Authorization": f"Bearer {key}", **kw.pop("headers", {})}, **kw)


def test_a_burst_then_429_with_retry_after_and_the_coded_error(client):
    seen = [_get(client, "ann") for _ in range(5)]
    assert [r.status_code for r in seen] == [200] * 5
    assert [r.headers["X-RateLimit-Remaining"] for r in seen] == ["4", "3", "2", "1", "0"]
    assert {r.headers["X-RateLimit-Limit"] for r in seen} == {"5"}
    assert int(seen[-1].headers["X-RateLimit-Reset"]) == 5  # seconds until the allowance is full again
    over = _get(client, "ann")
    assert over.status_code == 429
    body = over.json()
    assert (body["error"]["code"], body["error"]["retryable"]) == ("rate_limited", True)
    assert body["error"]["retry_after_seconds"] == int(over.headers["Retry-After"]) == 1
    assert over.headers["X-RateLimit-Remaining"] == "0"
    assert body["detail"]["burst"] == 5 and body["detail"]["limit_per_second"] == 1


def test_it_is_per_key_not_per_address(client):
    for _ in range(5):
        _get(client, "ann")
    assert _get(client, "ann").status_code == 429
    # Another key from the very same address is unaffected.
    assert _get(client, "bob").status_code == 200


def test_admin_keys_are_exempt(client, monkeypatch):
    monkeypatch.setattr(routes.service, "list_users", lambda: [])
    monkeypatch.setattr(routes.service, "list_workloads", lambda **kw: [])
    assert all(_get(client, "ops").status_code == 200 for _ in range(12))
    assert "X-RateLimit-Limit" not in _get(client, "ops").headers


def test_mcp_counts_once_per_request_and_answers_429_in_json_rpc(client):
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list_rentals", "arguments": {}}}
    first = client.post("/mcp", json=rpc, headers={"Authorization": "Bearer ann"})
    assert first.status_code == 200 and first.headers["X-RateLimit-Remaining"] == "4"  # the tool's REST call took none
    for _ in range(4):
        client.post("/mcp", json=rpc, headers={"Authorization": "Bearer ann"})
    over = client.post("/mcp", json=rpc, headers={"Authorization": "Bearer ann"})
    assert over.status_code == 429 and over.json()["error"]["code"] == -32002 and over.headers["Retry-After"]


def test_public_routes_and_bad_keys_are_not_counted(client):
    for _ in range(8):
        client.get("/platform/pricing")
        _get(client, "nope")
    assert _get(client, "ann").headers["X-RateLimit-Remaining"] == "4"


def test_the_headers_are_readable_by_a_browser(client):
    r = _get(client, "ann", headers={"Origin": "http://localhost:3000"})
    exposed = {h.strip().lower() for h in r.headers["access-control-expose-headers"].split(",")}
    assert {"x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset", "retry-after"} <= exposed


def test_it_can_be_turned_off(client, monkeypatch):
    monkeypatch.setenv("GREENCOMPUTE_KEY_RATE_PER_SECOND", "0")
    assert all(_get(client, "ann").status_code == 200 for _ in range(10))


def test_the_defaults_are_generous():
    assert security.KEY_RATE_PER_SECOND_DEFAULT == 20 and security.KEY_RATE_BURST_DEFAULT == 40


def test_the_bucket_refills_at_the_rate():
    bucket = TokenBucketRateLimiter()
    results = [bucket.take("k", rate=2, burst=3, now=100.0) for _ in range(4)]
    assert [r.allowed for r in results] == [True, True, True, False]
    assert results[-1].retry_after_seconds == pytest.approx(0.5)
    assert bucket.take("k", rate=2, burst=3, now=100.5).allowed  # one back after 1/rate s
    assert not bucket.take("k", rate=2, burst=3, now=100.5).allowed
    assert bucket.take("k", rate=2, burst=3, now=200.0).remaining == 2  # full again, minus this one


def test_memory_stays_bounded():
    bucket = TokenBucketRateLimiter(max_keys=10)
    for i in range(50):
        bucket.take(f"k{i}", rate=100, burst=1, now=float(i))  # each refilled before the next comes
    assert len(bucket._buckets) <= 11
