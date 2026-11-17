"""/openapi.json is what agents generate clients from (external review, Nov
2026): it declared no auth scheme (every route looked public), documented
only 200/201/422, left out POST /platform/api-keys and listed internal routes
and ones that answer 501. Built from the real app."""
import inspect
import re

import pytest

from greencompute_gateway.main import app
from greencompute_gateway.transport.openapi import OPTIONAL_KEY

# A key check, or handing the caller's key on to a handler that checks it
# (/v1/completions runs as /v1/chat/completions).
AUTH_CALLS = re.compile(
    r"\b(require_api_key|_user_key|_require_user|require_provision_or_admin)\(|authorization=authorization")


@pytest.fixture(scope="module")
def spec():
    app.openapi_schema = None
    return app.openapi()


def _ops(spec):
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            yield method, path, op


def _routes_in_spec():
    for route in app.routes:
        if getattr(route, "include_in_schema", False) and hasattr(route, "endpoint"):
            for method in route.methods or ():
                yield method.lower(), getattr(route, "path_format", route.path), route.endpoint


def test_it_declares_bearer_and_x_api_key(spec):
    schemes = spec["components"]["securitySchemes"]
    assert schemes["bearerAuth"] == {**schemes["bearerAuth"], "type": "http", "scheme": "bearer"}
    assert schemes["apiKeyHeader"] == {**schemes["apiKeyHeader"], "type": "apiKey", "in": "header", "name": "X-API-Key"}


def test_routes_that_take_a_key_require_one_and_public_ones_do_not(spec):
    ops = {(m, p): op for m, p, op in _ops(spec)}
    for method, path, endpoint in _routes_in_spec():
        source = inspect.getsource(endpoint)
        security = ops[(method, path)]["security"]
        if AUTH_CALLS.search(source) and (method, path) not in OPTIONAL_KEY:
            assert security == [{"bearerAuth": []}, {"apiKeyHeader": []}], (method, path)
        else:
            assert security == [], (method, path)
    for public in (("get", "/platform/pricing"), ("get", "/platform/nodes/supported"), ("get", "/healthz"),
                   ("post", "/platform/auth/wallet/challenge"), ("post", "/platform/auth/wallet/verify")):
        assert ops[public]["security"] == [], public
    assert ops[("get", "/platform/deployments")]["security"]
    # The key headers are the schemes' job now, not per-route parameters.
    for _m, _p, op in _ops(spec):
        names = {p["name"].lower() for p in op.get("parameters", []) if p["in"] == "header"}
        assert not names & {"authorization", "x-api-key"}


def test_post_api_keys_is_documented_for_customers(spec):
    op = spec["paths"]["/platform/api-keys"]["post"]
    body = op["requestBody"]["content"]["application/json"]["schema"]
    assert body == {"$ref": "#/components/schemas/APIKeyCreatePublic"}
    fields = spec["components"]["schemas"]["APIKeyCreatePublic"]["properties"]
    assert {"name", "scopes", "expires_in_days", "daily_spend_limit_usd"} <= set(fields)
    assert not {"admin", "user_id"} & set(fields)
    created = op["responses"]["200"]["content"]["application/json"]["schema"]
    assert created == {"$ref": "#/components/schemas/APIKeyCreated"}
    assert "secret" in spec["components"]["schemas"]["APIKeyCreated"]["properties"]
    assert "master" not in op.get("description", "").lower() and "provision" not in op.get("description", "").lower()


@pytest.mark.parametrize("path", [
    "/misc/hf_repo_info", "/misc/proxy", "/bounties", "/idp/scopes", "/idp/authorize", "/idp/token",
    "/e2e/invoke", "/e2e/instances/{workload_id}", "/_metrics", "/logos", "/logos/{logo_id}.{ext}",
    "/registry/auth", "/mcp",
])
def test_internal_and_turned_off_routes_are_left_out(spec, path):
    assert path not in spec["paths"]


def test_nothing_in_the_spec_is_a_turned_off_stub(spec):
    """Stubs that always answer 501 are left out (a live route that a flag can
    turn off, like the terminal, stays)."""
    for method, path, endpoint in _routes_in_spec():
        assert "_feature_disabled(" not in inspect.getsource(endpoint), (method, path)


def test_the_real_error_responses_are_documented_with_the_coded_body(spec):
    paths = spec["paths"]
    coded = {"$ref": "#/components/schemas/ErrorResponse"}

    def schema(path, method, status):
        return paths[path][method]["responses"][status]["content"]["application/json"]["schema"]

    create = paths["/platform/deployments"]["post"]["responses"]
    assert {"400", "401", "402", "403", "409", "422", "429", "503"} <= set(create)
    for status in ("400", "401", "402", "403", "409", "429", "503"):
        assert schema("/platform/deployments", "post", status) == coded
    assert "`no_capacity`" in create["503"]["description"] and "Retry-After" in create["503"]["headers"]
    assert {"Retry-After", "X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset"} <= set(
        create["429"]["headers"])
    assert schema("/platform/deployments", "post", "422") == {"$ref": "#/components/schemas/ValidationErrorResponse"}
    assert "X-RateLimit-Remaining" in create["200"]["headers"]
    assert {"401", "403", "404", "429"} <= set(paths["/platform/deployments/{deployment_id}"]["get"]["responses"])
    assert {"401", "403", "429"} <= set(paths["/platform/billing/balance"]["get"]["responses"])
    error = spec["components"]["schemas"]["ApiError"]
    assert {"code", "message", "retryable", "retry_after_seconds"} <= set(error["properties"])
    assert "rate_limited" in error["properties"]["code"]["enum"]
    # Public routes don't claim auth errors.
    assert "401" not in paths["/platform/pricing"]["get"]["responses"]
