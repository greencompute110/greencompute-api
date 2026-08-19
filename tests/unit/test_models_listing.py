"""GET /v1/models — OpenAI-compatible discovery.

Cursor, aider, Continue and LibreChat all call this to validate a custom base
URL. While it 404'd, those clients reported the whole endpoint as broken even
though /v1/chat/completions worked — the symptom looked like an outage rather
than an unimplemented convenience route.
"""
import inspect

from greencompute_gateway.transport import routes


def _source() -> str:
    return inspect.getsource(routes.list_models)


def test_route_is_registered_as_a_GET_on_v1_models():
    paths = {getattr(r, "path", None) for r in routes.router.routes}
    assert "/v1/models" in paths
    route = next(r for r in routes.router.routes if getattr(r, "path", None) == "/v1/models")
    assert "GET" in route.methods


def test_listing_requires_an_api_key():
    """It reveals which models a deployment serves, and it is a billable-surface
    endpoint like the rest of /v1 — it must not be open to the internet."""
    assert "require_api_key" in _source()


def test_response_uses_the_openai_envelope():
    """Clients parse `data[].id`; a bare list or a differently-named field makes
    them fail to enumerate even when the request succeeds."""
    src = _source()
    assert '"object": "list"' in src
    assert '"data"' in src
    assert '"id": name' in src
    assert '"owned_by"' in src


def test_only_inference_workloads_are_listed():
    """Pods and VMs are not chat models. Listing them would offer a client an id
    that always fails at /v1/chat/completions."""
    assert "inference" in _source()


def test_ids_are_deduplicated_and_stable():
    """Duplicate ids make some clients render the model twice; unstable order
    makes the picker jump around between refreshes."""
    src = _source()
    assert "in seen" in src
    assert "sorted(seen)" in src


def test_only_public_workloads_are_listed():
    """Without this, /v1/models published every inference workload ever created:
    21 of 25 were internal test artifacts (ssrf-poc-workload, sqli-test2,
    file-env-leak...) that would show up in every customer's Cursor model picker.
    `public` is the same flag the catalog uses for visibility."""
    assert '"public", False' in _source()
