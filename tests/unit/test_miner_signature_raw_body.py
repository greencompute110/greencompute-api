"""A miner's signature covers the bytes it sent, not a re-serialisation of them.

2026-10-29: adding an optional field to DeploymentStatusUpdate made the control
plane re-serialise every status report with `"suspended_reason": null`, so the
bytes it verified no longer matched what nodes (still on the previous protocol)
had signed: every pulling/starting/ready report got 401 while heartbeats kept
passing. Every signed miner POST, on the control plane and the validator, now
verifies the raw request body (the RawBody dependency), so the signature is
independent of which protocol version each side runs.
"""
import inspect
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from greencompute_control_plane.transport import routes as cp_routes
from greencompute_validator.transport import routes as v_routes

HEADERS = {"X-Miner-Hotkey": "m", "X-Miner-Signature": "sig", "X-Miner-Nonce": "n", "X-Miner-Timestamp": "1",
           "Content-Type": "application/json"}
OK = SimpleNamespace(model_dump=lambda mode=None: {})

# (module, url, raw body with odd spacing and a field this side doesn't know, service stubs)
CASES = [
    (cp_routes, "/miner/v1/register",
     b'{"hotkey": "m",  "payout_address": "5F", "auth_secret": "secret-8chars", "api_base_url": "http://m",'
     b' "validator_url": "http://v", "from_a_newer_miner": 1}',
     {"register_miner": lambda p: p, "repository": SimpleNamespace(get_miner=lambda hotkey: None)}),
    (cp_routes, "/miner/v1/heartbeat",
     b'{"hotkey": "m",   "healthy": true, "from_a_newer_miner": 7}',
     {"record_heartbeat": lambda p: p}),
    (cp_routes, "/miner/v1/capacity",
     b'{"hotkey": "m", "nodes": [],  "from_a_newer_miner": 7}',
     {"update_capacity": lambda p: p}),
    (cp_routes, "/miner/v1/deployments/d1/status",
     b'{"deployment_id":"d1","state":"ready",  "from_a_newer_miner":1}',
     {"repository": SimpleNamespace(get_deployment=lambda i: SimpleNamespace(deployment_id="d1", hotkey="m")),
      "update_deployment_status": lambda p: SimpleNamespace(model_dump=lambda mode=None: {"state": "ready"})}),
    (v_routes, "/validator/v1/capabilities",
     b'{"hotkey": "m", "node_id": "n", "gpu_model": "rtx4090", "gpu_count": 1, "available_gpus": 1,'
     b' "vram_gb_per_gpu": 24, "cpu_cores": 8, "memory_gb": 32,  "from_a_newer_miner": 1}',
     {"register_capability": lambda p: p}),
    (v_routes, "/validator/v1/probes/results",
     b'{"challenge_id": "c", "hotkey": "m", "node_id": "n", "latency_ms": 1.0, "throughput": 1.0,'
     b' "benchmark_signature": "s",  "from_a_newer_miner": 1}',
     {"submit_probe_result": lambda p: OK}),
]


@pytest.mark.parametrize("module, url, raw, stubs", CASES, ids=[c[1] for c in CASES])
def test_the_signature_is_checked_over_the_bytes_the_miner_sent(monkeypatch, module, url, raw, stubs):
    seen = {}

    def capture(expected_hotkey, payload_bytes, *args, **kwargs):
        seen["bytes"] = payload_bytes

    monkeypatch.setattr(module, "require_miner_request", capture)
    for name, value in stubs.items():
        monkeypatch.setattr(module.service, name, value)
    app = FastAPI()
    app.include_router(module.router)
    res = TestClient(app).post(url, content=raw, headers=HEADERS)
    assert res.status_code == 200, res.text
    assert seen["bytes"] == raw


def test_signed_routes_stay_plain_functions_so_they_run_in_the_threadpool():
    for module, url, *_ in CASES:
        route = next(r for r in module.router.routes if getattr(r, "path", "") == url.replace("d1", "{deployment_id}"))
        assert not inspect.iscoroutinefunction(route.endpoint), url
        assert "raw_body" in inspect.signature(route.endpoint).parameters, url


def test_every_signed_route_with_a_body_takes_the_raw_body():
    """Any route that checks a miner signature and parses a JSON body must
    verify the raw bytes (RawBody), so a new route can't quietly go back to
    re-serialising the model."""
    from pydantic import BaseModel

    checked = 0
    for module in (cp_routes, v_routes):
        for route in module.router.routes:
            params = inspect.signature(getattr(route, "endpoint", lambda: None)).parameters
            if "x_miner_signature" not in params:
                continue
            has_body = any(
                isinstance(p.annotation, type) and issubclass(p.annotation, BaseModel) for p in params.values()
            )
            if has_body:
                assert "raw_body" in params, f"{route.path} verifies a signature but not over the raw body"
                checked += 1
    assert checked >= 6


def test_a_status_report_cannot_say_why_a_deployment_is_suspended(monkeypatch):
    got = {}
    monkeypatch.setattr(cp_routes, "require_miner_request", lambda *a, **k: None)
    monkeypatch.setattr(cp_routes.service, "repository",
                        SimpleNamespace(get_deployment=lambda i: SimpleNamespace(deployment_id="d1", hotkey="m")))

    def update(payload):
        got["payload"] = payload
        return SimpleNamespace(model_dump=lambda mode=None: {})

    monkeypatch.setattr(cp_routes.service, "update_deployment_status", update)
    app = FastAPI()
    app.include_router(cp_routes.router)
    raw = b'{"deployment_id":"d1","state":"suspended","suspended_reason":"balance"}'
    assert TestClient(app).post("/miner/v1/deployments/d1/status", content=raw, headers=HEADERS).status_code == 200
    assert got["payload"].suspended_reason is None


# --- registration proves it is the miner already on file -----------------------

from greencompute_control_plane.config import settings as cp_settings
from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository
from greencompute_protocol import DeploymentRecord, MinerRegistration, sign_payload


def _registration(secret, payout="5Fowner", url="http://owner"):
    return MinerRegistration(hotkey="victim-hotkey", payout_address=payout, auth_secret=secret,
                             api_base_url=url, validator_url="http://v")


def _post_register(tc, reg, signing_secret):
    body = reg.model_dump_json().encode()
    signed = sign_payload(secret=signing_secret, actor_id=reg.hotkey, body=body)
    return tc.post("/miner/v1/register", content=body, headers={
        "X-Miner-Hotkey": reg.hotkey, "X-Miner-Signature": signed.signature, "X-Miner-Nonce": signed.nonce,
        "X-Miner-Timestamp": str(signed.timestamp), "Content-Type": "application/json"})


@pytest.fixture
def registry(monkeypatch):
    repo = ControlPlaneRepository(database_url="sqlite+pysqlite:///:memory:", bootstrap=True)
    monkeypatch.setattr(cp_routes.service, "repository", repo)
    monkeypatch.setattr(cp_settings, "whitelist_enabled", False)
    app = FastAPI()
    app.include_router(cp_routes.router)
    return TestClient(app), repo


def test_nobody_can_take_over_a_registered_hotkey_with_their_own_secret(registry):
    tc, repo = registry
    assert _post_register(tc, _registration("owner-secret-1"), "owner-secret-1").status_code == 200
    attack = _registration("attacker-secret", payout="5Fattacker", url="http://attacker")
    assert _post_register(tc, attack, "attacker-secret").status_code == 401
    kept = repo.get_miner("victim-hotkey")
    assert (kept.payout_address, kept.api_base_url, kept.auth_secret) == ("5Fowner", "http://owner", "owner-secret-1")


def test_the_real_miner_can_still_re_register_and_rotate_its_secret(registry):
    tc, repo = registry
    assert _post_register(tc, _registration("owner-secret-1"), "owner-secret-1").status_code == 200
    rotated = _registration("owner-secret-2", url="http://owner-new")
    assert _post_register(tc, rotated, "owner-secret-1").status_code == 200  # signed with the secret on file
    assert repo.get_miner("victim-hotkey").api_base_url == "http://owner-new"


def test_re_registering_does_not_undo_an_admin_drain(registry):
    tc, repo = registry
    assert _post_register(tc, _registration("owner-secret-1"), "owner-secret-1").status_code == 200
    repo.set_miner_drained("victim-hotkey", True)
    assert _post_register(tc, _registration("owner-secret-1"), "owner-secret-1").status_code == 200
    assert repo.get_miner("victim-hotkey").drained is True


# --- the status route says nothing about a deployment to a stranger ------------


def _signed_status(tc, deployment_id, hotkey, secret):
    body = ('{"deployment_id":"%s","state":"ready"}' % deployment_id).encode()
    headers = {"Content-Type": "application/json"}
    if hotkey:
        signed = sign_payload(secret=secret, actor_id=hotkey, body=body)
        headers.update({"X-Miner-Hotkey": hotkey, "X-Miner-Signature": signed.signature,
                        "X-Miner-Nonce": signed.nonce, "X-Miner-Timestamp": str(signed.timestamp)})
    return tc.post(f"/miner/v1/deployments/{deployment_id}/status", content=body, headers=headers)


def test_a_stranger_cannot_tell_which_deployment_ids_exist(registry, monkeypatch):
    tc, repo = registry
    for hotkey, secret in (("miner-a", "secret-a-123"), ("miner-b", "secret-b-123")):
        reg = MinerRegistration(hotkey=hotkey, payout_address="5F", auth_secret=secret,
                                api_base_url="http://m", validator_url="http://v")
        assert _post_register(tc, reg, secret).status_code == 200
    wl_id = "w-1"
    real = repo.create_deployment(DeploymentRecord(workload_id=wl_id, hotkey="miner-a", node_id="n1"))
    monkeypatch.setattr(cp_routes.service, "update_deployment_status",
                        lambda p: SimpleNamespace(model_dump=lambda mode=None: {"state": p.state.value}))

    # Anonymous: 401 whether or not the id exists.
    assert _signed_status(tc, real.deployment_id, None, None).status_code == 401
    assert _signed_status(tc, "no-such-id", None, None).status_code == 401
    # Another registered miner: 404 for someone else's deployment, same as for none.
    assert _signed_status(tc, real.deployment_id, "miner-b", "secret-b-123").status_code == 404
    assert _signed_status(tc, "no-such-id", "miner-b", "secret-b-123").status_code == 404
    # A forged signature for the owner's hotkey: 401.
    assert _signed_status(tc, real.deployment_id, "miner-a", "wrong-secret-1").status_code == 401
    # The owner: accepted.
    assert _signed_status(tc, real.deployment_id, "miner-a", "secret-a-123").status_code == 200



def _signed_get(tc, deployment_id, hotkey, secret):
    headers = {}
    if hotkey:
        signed = sign_payload(secret=secret, actor_id=hotkey, body=b"")
        headers = {"X-Miner-Hotkey": hotkey, "X-Miner-Signature": signed.signature,
                   "X-Miner-Nonce": signed.nonce, "X-Miner-Timestamp": str(signed.timestamp)}
    return tc.get(f"/miner/v1/deployments/{deployment_id}", headers=headers)


def test_reading_a_deployment_follows_the_same_rule(registry):
    tc, repo = registry
    for hotkey, secret in (("miner-a", "secret-a-123"), ("miner-b", "secret-b-123")):
        reg = MinerRegistration(hotkey=hotkey, payout_address="5F", auth_secret=secret,
                                api_base_url="http://m", validator_url="http://v")
        assert _post_register(tc, reg, secret).status_code == 200
    placed = repo.create_deployment(DeploymentRecord(workload_id="w", hotkey="miner-a", node_id="n1"))
    unplaced = repo.create_deployment(DeploymentRecord(workload_id="w"))  # no miner yet

    assert _signed_get(tc, placed.deployment_id, None, None).status_code == 401
    assert _signed_get(tc, "no-such-id", None, None).status_code == 401
    assert _signed_get(tc, placed.deployment_id, "miner-b", "secret-b-123").status_code == 404
    assert _signed_get(tc, unplaced.deployment_id, "miner-b", "secret-b-123").status_code == 404  # was readable
    ok = _signed_get(tc, placed.deployment_id, "miner-a", "secret-a-123")
    assert ok.status_code == 200 and ok.json()["deployment_id"] == placed.deployment_id
