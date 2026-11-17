"""Account secrets (external security review, Nov 2026): GET /platform/secrets
returned every value in plain text to any key, even a read-only one, and no
workload could use a secret. Now no request ever returns a value; names are
unique; a pod names secrets in metadata.secret_env (a model, its HF token in
metadata.hf_token_secret) and the values are filled in only for the miner
holding its lease, never in a workload or deployment row."""
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from greencompute_control_plane.application.services import ControlPlaneService
from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository
from greencompute_control_plane.transport import routes as cp_routes
from greencompute_gateway.application.services import GatewayService, SecretReferenceError
from greencompute_gateway.domain.scopes import required_scope
from greencompute_gateway.infrastructure.repository import GatewayRepository
from greencompute_gateway.transport import mcp as mcp_module
from greencompute_gateway.transport import routes
from greencompute_gateway.transport.errors import install_error_handlers
from greencompute_persistence import session_scope
from greencompute_persistence.orm import UserORM, UserSecretORM
from greencompute_persistence.user_secrets import is_ssh_public_key, resolve_workload_secrets
from greencompute_protocol import DeploymentCreateRequest, WorkloadCreateRequest

VALUE = "hf_live_TOPSECRET_9f8e7d"
POOL = "pool-pass-0xDEADBEEF"
PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f me@laptop"
AUTH = {"Authorization": "Bearer gk_test"}


@pytest.fixture
def world(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 's.sqlite'}"
    cp = ControlPlaneService(repository=ControlPlaneRepository(database_url=url, bootstrap=True))
    gw = GatewayService(repository=GatewayRepository(database_url=url, bootstrap=True), control_plane=cp)
    with session_scope(gw.repository.session_factory) as s:
        s.add(UserORM(user_id="ann", username="ann", email="ann@x.io", balance_credits=10_000))
        s.add(UserORM(user_id="bob", username="bob", email="bob@x.io", balance_credits=10_000))
    monkeypatch.setattr(routes, "service", gw)
    keys = {"Bearer gk_test": "ann", "Bearer gk_bob": "bob"}

    def fake_key(authorization, x_api_key, **kw):
        return SimpleNamespace(key_id=f"k-{keys[authorization]}", user_id=keys[authorization], admin=False,
                               scopes=[], max_active_rentals=None, daily_spend_limit_usd=None)

    monkeypatch.setattr(routes, "require_api_key", fake_key)
    monkeypatch.setattr(mcp_module, "require_api_key", fake_key)
    app = FastAPI()
    app.include_router(routes.router)
    app.include_router(mcp_module.router)
    install_error_handlers(app)
    return SimpleNamespace(gw=gw, cp=cp, client=TestClient(app))


def _no_values(*bodies):
    text = json.dumps(bodies, default=str)
    assert VALUE not in text and POOL not in text, text


def _secret(w, name, value, headers=AUTH):
    return w.client.post("/platform/secrets", json={"name": name, "value": value}, headers=headers)


def _pod(w, metadata, headers=AUTH):
    return w.client.post("/platform/workloads", headers=headers, json={
        "name": "job", "image": "pytorch", "kind": "pod", "metadata": metadata})


# --- the store never hands a value back ---------------------------------------------


def test_no_request_returns_a_secret_value(world):
    created = _secret(world, "HF_TOKEN", VALUE)
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["name"] == "HF_TOKEN" and "value" not in body and body["public_key"] is None
    listed = world.client.get("/platform/secrets", headers=AUTH)
    assert [s["name"] for s in listed.json()] == ["HF_TOKEN"] and "value" not in listed.json()[0]
    replaced = world.client.put(f"/platform/secrets/{body['secret_id']}", json={"value": VALUE + "-v2"}, headers=AUTH)
    assert replaced.status_code == 200 and "value" not in replaced.json()
    deleted = world.client.delete(f"/platform/secrets/{body['secret_id']}", headers=AUTH)
    assert deleted.status_code == 200 and "value" not in deleted.json()
    _no_values(created.json(), listed.json(), replaced.json(), deleted.json())
    assert world.client.get("/platform/secrets", headers=AUTH).json() == []


def test_replacing_a_value_keeps_the_name_and_reaches_new_rentals(world):
    sid = _secret(world, "HF_TOKEN", "old").json()["secret_id"]
    world.client.put(f"/platform/secrets/{sid}", json={"value": VALUE}, headers=AUTH)
    with session_scope(world.gw.repository.session_factory) as s:
        assert s.get(UserSecretORM, sid).value == VALUE


def test_another_accounts_secret_is_not_found(world):
    sid = _secret(world, "HF_TOKEN", VALUE).json()["secret_id"]
    bob = {"Authorization": "Bearer gk_bob"}
    assert world.client.put(f"/platform/secrets/{sid}", json={"value": "x"}, headers=bob).status_code == 404
    assert world.client.delete(f"/platform/secrets/{sid}", headers=bob).status_code == 404
    assert world.client.get("/platform/secrets", headers=bob).json() == []


def test_a_name_already_taken_is_a_conflict(world):
    first = _secret(world, "HF_TOKEN", VALUE).json()
    again = _secret(world, "  HF_TOKEN ", "other")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "conflict"
    assert again.json()["detail"]["secret_id"] == first["secret_id"]
    assert "PUT /platform/secrets/" in again.json()["detail"]["hint"]
    _no_values(again.json())
    # Another account may use the same name.
    assert _secret(world, "HF_TOKEN", "bobs", headers={"Authorization": "Bearer gk_bob"}).status_code == 200


def test_duplicates_from_before_stay_readable_and_deletable_and_the_newest_is_used(world):
    from datetime import UTC, datetime, timedelta

    old = datetime(2026, 1, 1, tzinfo=UTC)
    with session_scope(world.gw.repository.session_factory) as s:
        s.add(UserSecretORM(secret_id="s-old", user_id="ann", name="TOKEN", value="stale", created_at=old, updated_at=old))
        s.add(UserSecretORM(secret_id="s-new", user_id="ann", name="TOKEN", value=VALUE,
                            created_at=old + timedelta(days=1), updated_at=old + timedelta(days=1)))
    names = [s["secret_id"] for s in world.client.get("/platform/secrets", headers=AUTH).json()]
    assert sorted(names) == ["s-new", "s-old"]
    wl = world.gw.create_workload(WorkloadCreateRequest(
        name="job", image="pytorch", kind="pod", metadata={"secret_env": {"T": "TOKEN"}}), "ann")
    assert resolve_workload_secrets(wl, world.gw.repository.session_factory).metadata["env"] == {"T": VALUE}
    assert world.client.delete("/platform/secrets/s-old", headers=AUTH).status_code == 200
    assert _secret(world, "TOKEN", "x").status_code == 409  # one is still there


def test_names_are_checked(world):
    bad = _secret(world, "bad\nname", VALUE)
    assert bad.status_code == 422 and bad.json()["detail"][0]["loc"] == ["body", "name"]


# --- the website's SSH keys ----------------------------------------------------------


def test_an_ssh_key_entry_returns_its_public_key_and_must_be_one(world):
    saved = _secret(world, "ssh-key:laptop", PUBKEY)
    assert saved.status_code == 200 and saved.json()["public_key"] == PUBKEY
    assert world.client.get("/platform/secrets", headers=AUTH).json()[0]["public_key"] == PUBKEY
    refused = _secret(world, "ssh-key:oops", "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk=\n")
    assert refused.status_code == 422 and refused.json()["detail"][0]["loc"] == ["body", "value"]


def test_only_real_public_keys_count():
    assert is_ssh_public_key(PUBKEY)
    assert not is_ssh_public_key("ssh-ed25519 notbase64!!")
    assert not is_ssh_public_key("ssh-rsa AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f")  # wrong type
    assert not is_ssh_public_key(VALUE)


def test_an_old_ssh_key_entry_saved_with_a_stray_line_break_still_shows(world):
    with session_scope(world.gw.repository.session_factory) as s:
        s.add(UserSecretORM(secret_id="s1", user_id="ann", name="ssh-key:old", value=PUBKEY + "\r\n"))
        s.add(UserSecretORM(secret_id="s2", user_id="ann", name="ssh-key:priv",
                            value="-----BEGIN OPENSSH PRIVATE KEY-----\nssh-ed25519 AAAA\n"))
    keys = {s["secret_id"]: s["public_key"] for s in world.client.get("/platform/secrets", headers=AUTH).json()}
    assert keys == {"s1": PUBKEY, "s2": None}


def test_an_old_ssh_key_entry_holding_a_secret_is_not_returned(world):
    with session_scope(world.gw.repository.session_factory) as s:
        s.add(UserSecretORM(secret_id="s1", user_id="ann", name="ssh-key:weird", value=VALUE))
    listed = world.client.get("/platform/secrets", headers=AUTH).json()
    assert listed[0]["public_key"] is None
    _no_values(listed)


# --- scopes -------------------------------------------------------------------------


def test_a_read_key_lists_names_and_a_rentals_key_manages_them():
    assert required_scope("GET", "/platform/secrets") == "read"
    for method in ("POST", "PUT", "DELETE"):
        assert required_scope(method, "/platform/secrets/s1" if method != "POST" else "/platform/secrets") == "rentals"


def test_a_read_only_key_cannot_create_one():
    from fastapi import HTTPException

    from greencompute_gateway.transport.security import _check_scopes, current_request_route

    token = current_request_route.set(("POST", "/platform/secrets"))
    try:
        with pytest.raises(HTTPException) as exc:
            _check_scopes(SimpleNamespace(admin=False, scopes=["read"]))
        assert exc.value.status_code == 403
        _check_scopes(SimpleNamespace(admin=False, scopes=["rentals"]))  # no raise
    finally:
        current_request_route.reset(token)


# --- workloads that use secrets -----------------------------------------------------


def test_a_pod_names_secrets_and_no_read_path_shows_a_value(world):
    _secret(world, "hf-read", VALUE)
    _secret(world, "pool", POOL)
    created = _pod(world, {"env": {"SEED": "7"}, "secret_env": {"HF_TOKEN": "hf-read", "POOL_PASS": "pool"}})
    assert created.status_code == 200, created.text
    wl = created.json()
    assert wl["metadata"]["secret_env"] == {"HF_TOKEN": "hf-read", "POOL_PASS": "pool"}  # names, as given
    wid = wl["workload_id"]
    dep = world.cp.create_deployment(DeploymentCreateRequest(workload_id=wid).model_dump() | {"owner_user_id": "ann"})
    responses = [
        world.client.get(f"/platform/workloads/{wid}", headers=AUTH),
        world.client.get("/platform/workloads?mine=true", headers=AUTH),
        world.client.patch(f"/platform/workloads/{wid}", json={"display_name": "Job"}, headers=AUTH),
        world.client.get(f"/platform/deployments/{dep.deployment_id}", headers=AUTH),
        world.client.get("/platform/deployments", headers=AUTH),
        world.client.get("/platform/events", headers=AUTH),
        world.client.post("/mcp", headers=AUTH, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "get_rental", "arguments": {"deployment_id": dep.deployment_id}}}),
    ]
    assert [r.status_code for r in responses] == [200] * len(responses), [r.text for r in responses]
    assert responses[4].json()[0]["deployment_id"] == dep.deployment_id
    assert responses[6].json()["result"]["isError"] is False, responses[6].text
    _no_values(created.json(), [r.json() for r in responses])
    # Nor is it stored with the workload or the deployment.
    _no_values(world.cp.repository.get_workload(wid).model_dump(mode="json"),
               world.cp.repository.get_deployment(dep.deployment_id).model_dump(mode="json"))


def test_an_unknown_secret_is_refused_at_create(world):
    r = _pod(world, {"secret_env": {"HF_TOKEN": "nope"}})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"
    assert r.json()["detail"][0]["loc"] == ["body", "metadata", "secret_env", "HF_TOKEN"]
    assert "no secret named 'nope'" in r.json()["detail"][0]["msg"]
    # Another account's secret is just as unknown.
    _secret(world, "bobs", VALUE, headers={"Authorization": "Bearer gk_bob"})
    assert _pod(world, {"secret_env": {"T": "bobs"}}).status_code == 422


@pytest.mark.parametrize("metadata, says", [
    ({"secret_env": ["HF_TOKEN"]}, "must be an object"),
    ({"secret_env": {"1BAD": "hf-read"}}, "not a valid variable name"),
    ({"secret_env": {"AUTHORIZED_KEYS": "hf-read"}}, "set by the platform"),
    ({"env": {"HF_TOKEN": "x"}, "secret_env": {"HF_TOKEN": "hf-read"}}, "also in metadata.env"),
    ({"secret_env": {"HF_TOKEN": ""}}, "name of one of your secrets"),
    ({"hf_token_secret": "hf-read"}, "for models"),
])
def test_bad_references_are_refused_with_the_reason(world, metadata, says):
    _secret(world, "hf-read", VALUE)
    r = _pod(world, metadata)
    assert r.status_code == 422, r.text
    assert says in r.json()["detail"][0]["msg"]


def test_the_lease_holder_gets_the_values_and_no_other_miner_does(world, monkeypatch):
    _secret(world, "hf-read", VALUE)
    wid = _pod(world, {"env": {"SEED": "7"}, "secret_env": {"HF_TOKEN": "hf-read"}}).json()["workload_id"]
    dep = world.cp.create_deployment(DeploymentCreateRequest(workload_id=wid).model_dump() | {"owner_user_id": "ann"})
    monkeypatch.setattr(cp_routes, "service", world.cp)
    monkeypatch.setattr(cp_routes, "require_miner_request", lambda *a, **k: None)
    leases = {"hk-runs-it": [SimpleNamespace(workload_id=wid, deployment_id=dep.deployment_id)],
              "hk-other": [SimpleNamespace(workload_id="w-else", deployment_id="d-else")]}
    monkeypatch.setattr(world.cp, "list_leases", lambda hotkey: leases.get(hotkey, []))
    app = FastAPI()
    app.include_router(cp_routes.router)
    client = TestClient(app)

    mine = client.get(f"/miner/v1/workloads/{wid}", headers={"X-Miner-Hotkey": "hk-runs-it"}).json()
    assert mine["metadata"]["env"] == {"SEED": "7", "HF_TOKEN": VALUE}
    other = client.get(f"/miner/v1/workloads/{wid}", headers={"X-Miner-Hotkey": "hk-other"}).json()
    assert other["metadata"]["env"] == {"SEED": "7"} and other["metadata"]["secret_env"] == {"HF_TOKEN": "hf-read"}
    _no_values(other)
    # Handing it to the machine changed nothing stored.
    _no_values(world.cp.repository.get_workload(wid).model_dump(mode="json"))


def test_only_the_owner_can_start_a_workload_that_uses_secrets(world, monkeypatch):
    """A shared or public workload naming secrets would put its owner's values
    in a pod someone else can SSH into."""
    _secret(world, "hf-read", VALUE)
    wid = _pod(world, {"secret_env": {"HF_TOKEN": "hf-read"}}).json()["workload_id"]
    monkeypatch.setattr(world.gw, "_user_can_access_workload", lambda w, u: True)  # as if shared with bob
    with pytest.raises(PermissionError, match="owner's secrets"):
        world.gw.create_deployment({"workload_id": wid}, user_id="bob")
    # And should one exist anyway, its machine gets the names alone.
    bobs = world.cp.create_deployment(DeploymentCreateRequest(workload_id=wid).model_dump() | {"owner_user_id": "bob"})
    monkeypatch.setattr(cp_routes, "service", world.cp)
    monkeypatch.setattr(cp_routes, "require_miner_request", lambda *a, **k: None)
    monkeypatch.setattr(world.cp, "list_leases", lambda hotkey: [
        SimpleNamespace(workload_id=wid, deployment_id=bobs.deployment_id)])
    app = FastAPI()
    app.include_router(cp_routes.router)
    got = TestClient(app).get(f"/miner/v1/workloads/{wid}", headers={"X-Miner-Hotkey": "hk"}).json()
    _no_values(got)


def test_a_model_names_its_hf_token_secret(world):
    _secret(world, "hf-read", VALUE)
    r = world.client.post("/platform/workloads", headers=AUTH, json={
        "template": "vllm", "model": "meta-llama/Llama-3.1-8B-Instruct", "hf_token_secret": "hf-read"})
    assert r.status_code == 200, r.text
    assert r.json()["metadata"]["hf_token_secret"] == "hf-read"  # a name, not redacted
    _no_values(r.json())
    wl = world.cp.repository.get_workload(r.json()["workload_id"])
    assert resolve_workload_secrets(wl, world.gw.repository.session_factory).metadata["hf_token"] == VALUE
    both = world.client.post("/platform/workloads", headers=AUTH, json={
        "template": "vllm", "model": "meta-llama/Llama-3.1-8B-Instruct", "hf_token": "hf_x",
        "hf_token_secret": "hf-read"})
    assert both.status_code == 422 and "not both" in both.json()["detail"][0]["msg"]
    pod_style = world.client.post("/platform/workloads", headers=AUTH, json={
        "name": "m", "image": "vllm", "kind": "inference", "metadata": {"secret_env": {"A": "hf-read"}}})
    assert pod_style.status_code == 422 and "hf_token_secret" in pod_style.json()["detail"][0]["msg"]


def test_a_secret_deleted_since_refuses_a_new_deployment(world, monkeypatch):
    sid = _secret(world, "hf-read", VALUE).json()["secret_id"]
    wid = _pod(world, {"secret_env": {"HF_TOKEN": "hf-read"}}).json()["workload_id"]
    world.client.delete(f"/platform/secrets/{sid}", headers=AUTH)
    with pytest.raises(SecretReferenceError):
        world.gw.create_deployment({"workload_id": wid}, user_id="ann")
    monkeypatch.setattr(world.gw, "create_deployment", lambda *a, **k: (_ for _ in ()).throw(
        SecretReferenceError([{"loc": ["body", "metadata", "secret_env", "HF_TOKEN"], "msg": "gone",
                               "type": "value_error"}])))
    r = world.client.post("/platform/deployments", json={"workload_id": wid}, headers=AUTH)
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"


def test_a_secret_gone_by_the_time_it_starts_is_left_out(world):
    sid = _secret(world, "hf-read", VALUE).json()["secret_id"]
    wid = _pod(world, {"env": {"SEED": "7"}, "secret_env": {"HF_TOKEN": "hf-read"}}).json()["workload_id"]
    world.client.delete(f"/platform/secrets/{sid}", headers=AUTH)
    wl = resolve_workload_secrets(world.cp.repository.get_workload(wid), world.gw.repository.session_factory)
    assert wl.metadata["env"] == {"SEED": "7"}


def test_rent_gpu_passes_secret_env_through(world, monkeypatch):
    seen = {}

    def create_workload(workload, response, **kw):
        seen["metadata"] = workload["metadata"]
        return {"workload_id": "w1"}

    monkeypatch.setattr(routes, "create_workload", create_workload)
    monkeypatch.setattr(routes, "create_deployment", lambda *a, **k: {"deployment_id": "d1", "state": "pending"})
    world.client.post("/mcp", headers=AUTH, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "rent_gpu", "arguments": {"ssh_public_keys": [PUBKEY], "secret_env": {"HF_TOKEN": "hf-read"}}}})
    assert seen["metadata"]["secret_env"] == {"HF_TOKEN": "hf-read"}


# --- a machine's error text never carries the values ------------------------------


def test_a_reported_error_quoting_docker_run_is_masked_before_it_is_stored(world, monkeypatch):
    """str(TimeoutExpired) of a pod's `docker run` is its whole argv, every
    -e VAR=value (external review, Nov 2026). The control plane masks the
    workload's values (and any -e value) before last_error, the event, the
    lease reason or the bus see it."""
    import subprocess

    from greencompute_protocol import DeploymentState, DeploymentStatusUpdate

    _secret(world, "hf-read", VALUE)
    wid = _pod(world, {"env": {"SEED": "7", "POOL_PASS": POOL, "WORKER": "rig-07"},
                       "secret_env": {"HF_TOKEN": "hf-read"}}).json()["workload_id"]
    dep = world.cp.create_deployment(DeploymentCreateRequest(workload_id=wid).model_dump() | {"owner_user_id": "ann"})
    published = []
    monkeypatch.setattr(world.cp.bus, "publish", lambda subject, payload: published.append(payload))
    argv = ["docker", "run", "-d", "-e", f"HF_TOKEN={VALUE}", "-e", f"POOL_PASS={POOL}", "-e", "SEED=7",
            "-e", "ROTATED=old-value-not-known-any-more", "-e", "AUTHORIZED_KEYS=ssh-ed25519 AAAA\nssh-rsa BBBB", "img"]
    error = f"docker not available: {subprocess.TimeoutExpired(argv, 600)}"
    for state in (DeploymentState.SCHEDULED, DeploymentState.FAILED):
        world.cp.update_deployment_status(DeploymentStatusUpdate(
            deployment_id=dep.deployment_id, state=state, error=error if state == DeploymentState.FAILED else None))
    saved = world.cp.repository.get_deployment(dep.deployment_id)
    stored = [saved.last_error, world.cp.repository.list_deployment_events(dep.deployment_id), published]
    _no_values(stored)
    text = json.dumps(stored, default=str)
    assert "old-value-not-known-any-more" not in text and "BBBB" not in text
    assert "'HF_TOKEN=***'" in saved.last_error and "'SEED=***'" in saved.last_error
    assert saved.last_error.startswith("docker not available: Command") and "timed out after 600" in saved.last_error


def test_masking_keeps_short_values_and_masks_shell_style_flags():
    from greencompute_persistence.user_secrets import mask_values

    text = "failed at step 7: docker run -e HF_TOKEN=abc123 --env=POOL=xyz img"
    assert mask_values(text, []) == "failed at step 7: docker run -e HF_TOKEN=*** --env=POOL=*** img"
    assert mask_values(None) is None
