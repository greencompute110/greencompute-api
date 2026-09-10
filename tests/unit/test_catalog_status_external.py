"""External models must show as available on the public catalog.

They have no flux assignment and no deployment rows, so the existing counters
report 0 and every UI paints them cold while they are serving fine. The status
endpoint therefore probes their health directly.

The helpers are exec'd from source rather than imported: the validator package
pulls in substrateinterface (chain client), which these pure functions do not
need and which is not present in every environment.
"""
import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[2] / (
    "services/validator/src/greencompute_validator/transport/routes.py")


def _load(*names):
    tree = ast.parse(SRC.read_text())
    wanted = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert len(wanted) == len(names), f"missing {set(names) - {n.name for n in wanted}}"
    ns = {}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), "<helpers>", "exec"),
         {"os": __import__("os"), "urlrequest": __import__("urllib.request", fromlist=["request"])}, ns)
    return ns


def _source_of(name):
    tree = ast.parse(SRC.read_text())
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return ast.get_source_segment(SRC.read_text(), n)
    raise AssertionError(f"{name} not found")


def _with(fn, raw):
    import os
    os.environ["GREENCOMPUTE_EXTERNAL_MODEL_UPSTREAMS"] = raw
    try:
        return fn()
    finally:
        os.environ.pop("GREENCOMPUTE_EXTERNAL_MODEL_UPSTREAMS", None)


def test_parses_the_same_format_as_the_gateway():
    """Both services read one env var; a format drift would make the public
    catalog disagree with what actually routes."""
    f = _load("_external_upstreams")["_external_upstreams"]
    assert _with(f, "k3-coder=http://h:1") == {"k3-coder": "http://h:1"}
    assert _with(f, "K3-Coder=http://h:1/") == {"k3-coder": "http://h:1"}
    assert _with(f, "") == {}
    assert _with(f, "garbage") == {}


def test_health_probe_never_raises_and_is_bounded():
    """catalog-status is public and unauthenticated: a wedged upstream must not
    hang it or 500 it. 8s, not 3s -- the upstream's /health takes ~1.1s idle and
    queues behind in-flight generation, and 3s turned busy-but-healthy into
    'cold'."""
    src = _source_of("_external_is_healthy")
    assert "timeout=8" in src
    assert "except Exception" in src


def test_health_probe_is_cached_to_stop_badge_flapping():
    """catalog-status is polled by every open browser tab every 10s and the
    upstream serves ONE request at a time. Probing per call queued health checks
    behind real inference and the badge flapped hot/cold/hot."""
    src = _source_of("_external_is_healthy")
    assert "_external_health_cache" in src
    assert "_EXTERNAL_HEALTH_TTL_S" in src


def test_status_counts_external_models_and_flags_them():
    src = _source_of("catalog_status")
    assert "_external_upstreams" in src
    assert "ext_running" in src
    assert '"externally_hosted"' in src
