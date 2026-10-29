"""What the public API shows: no internal routes in the OpenAPI spec, and no
platform-generated SSH key for clients that brought their own."""
import ast
import inspect
from types import SimpleNamespace

from fastapi import FastAPI

from greencompute_gateway.transport import routes


def _route_functions():
    src = inspect.getsource(routes)
    tree = ast.parse(src)
    for fn in tree.body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decorators = [
            d for d in fn.decorator_list
            if isinstance(d, ast.Call) and getattr(getattr(d.func, "value", None), "id", "") == "router"
        ]
        if decorators:
            yield fn, ast.get_source_segment(src, fn), decorators


def test_every_admin_or_provisioning_route_is_hidden_from_the_spec():
    """The spec is what agents read to decide what to call; admin routes in it
    are noise for them and a map for anyone probing the API."""
    exposed = []
    for fn, body, decorators in _route_functions():
        internal = (
            "admin_required=True" in body
            or "require_provision_or_admin" in body
            or fn.name == "billing_stripe_webhook"
        )
        hidden = all(
            any(k.arg == "include_in_schema" and getattr(k.value, "value", True) is False for k in d.keywords)
            for d in decorators
        )
        if internal and not hidden:
            exposed.append(fn.name)
    assert exposed == [], f"add include_in_schema=False to: {exposed}"


def test_generated_spec_has_no_internal_paths():
    app = FastAPI()
    app.include_router(routes.router)
    paths = app.openapi()["paths"]
    leaked = [p for p in paths if "/admin" in p or "/debug" in p or "webhook" in p]
    assert leaked == []
    for public in ("/platform/deployments", "/platform/billing/balance", "/platform/pricing", "/v1/chat/completions"):
        assert public in paths, public


def _ssh(monkeypatch, own_keys, host_keys=()):
    deployment = SimpleNamespace(
        workload_id="w1", endpoint="ssh://root@203.0.113.7:40123", ssh_private_key="-----BEGIN OPENSSH PRIVATE KEY-----x",
        ssh_host_keys=list(host_keys),
    )
    workload = SimpleNamespace(metadata={"ssh_public_keys": own_keys})
    monkeypatch.setattr(routes, "require_api_key", lambda *a, **k: SimpleNamespace(user_id="u1", admin=False, key_id="k1"))
    monkeypatch.setattr(routes.service, "get_deployment", lambda *a, **k: deployment)
    monkeypatch.setattr(routes.service, "get_workload", lambda *a, **k: workload)
    return routes.get_deployment_ssh("d1", authorization="Bearer k", x_api_key=None)


def test_ssh_does_not_return_the_platform_key_to_clients_with_their_own(monkeypatch):
    out = _ssh(monkeypatch, ["ssh-ed25519 AAAA agent@box"])
    assert out["private_key"] is None
    assert out["ssh_command"] == "ssh root@203.0.113.7 -p 40123"


def test_ssh_returns_the_platform_key_when_no_key_was_supplied(monkeypatch):
    assert _ssh(monkeypatch, [])["private_key"].startswith("-----BEGIN")
    assert _ssh(monkeypatch, ["  "])["private_key"].startswith("-----BEGIN")


def test_main_agent_endpoints_declare_their_response_shapes():
    """Agents generate clients from /openapi.json; "any object" told them nothing."""
    app = FastAPI()
    app.include_router(routes.router)
    spec = app.openapi()
    paths, schemas = spec["paths"], spec["components"]["schemas"]

    def body(path, method):
        return paths[path][method]["responses"]["200"]["content"]["application/json"]["schema"]

    deployment = body("/platform/deployments/{deployment_id}", "get")
    assert deployment == {"$ref": "#/components/schemas/Deployment"}
    fields = schemas["Deployment"]["properties"]
    assert {"deployment_id", "workload_id", "state", "ready_at", "hourly_rate_cents"} <= set(fields)
    assert "ssh_private_key" not in fields
    assert body("/platform/deployments", "get")["items"] == {"$ref": "#/components/schemas/Deployment"}
    assert body("/platform/deployments/{deployment_id}/ssh", "get") == {"$ref": "#/components/schemas/SSHAccess"}
    assert body("/platform/billing/balance", "get") == {"$ref": "#/components/schemas/Balance"}
    assert body("/platform/pricing", "get") == {"$ref": "#/components/schemas/Pricing"}
    assert "amount_to_send" in schemas["CryptoInvoiceCreated"]["properties"]
