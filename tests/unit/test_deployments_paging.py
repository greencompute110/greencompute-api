"""Paging GET /platform/deployments (external API user: the busiest account has
~350 deployments, and every call returned all of them). Without `limit` or
`cursor` the reply is exactly as before; with `limit`, newest first, a page at
a time, the state filter and the limit applied in the database, and the next
page's cursor in X-Next-Cursor and Link rel="next"."""
import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest

from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository
from greencompute_gateway.infrastructure.repository import GatewayRepository
from greencompute_gateway.main import app
from greencompute_gateway.transport import mcp as mcp_module
from greencompute_gateway.transport import routes, security
from greencompute_gateway.transport.errors import CODES
from greencompute_persistence import CredentialStore, session_scope
from greencompute_persistence.orm import APIKeyORM, DeploymentORM, UserORM

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def world(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'p.sqlite'}"
    cp = ControlPlaneRepository(database_url=url, bootstrap=True)
    gw = GatewayRepository(database_url=url)
    monkeypatch.setattr(routes.service, "repository", gw)
    monkeypatch.setattr(routes.service.control_plane, "repository", cp)
    monkeypatch.setattr(security, "credential_store", CredentialStore(engine=gw.engine, session_factory=gw.session_factory))
    with session_scope(cp.session_factory) as s:
        for uid in ("u1", "u2"):
            s.add(UserORM(user_id=uid, username=uid, email=f"{uid}@x.io", balance_credits=0, profile_metadata={}))
        s.add(APIKeyORM(key_id="k1", user_id="u1", name="u1", admin=False, scopes=[], secret="u1-key",
                        created_at=T0))
        s.add(APIKeyORM(key_id="ka", user_id="u2", name="admin", admin=True, scopes=[], secret="admin-key",
                        created_at=T0))
        s.add(APIKeyORM(key_id="kn", user_id=None, name="nobody", admin=False, scopes=[], secret="no-user",
                        created_at=T0))
    return SimpleNamespace(cp=cp, sf=cp.session_factory)


STATES = ["ready", "failed", "terminated", "pending", "ready", "suspended"]


def _add(world, n, *, owner="u1", start=T0, tie_every=4):
    """n deployments; every `tie_every` share one created_at, to test the id tie-break."""
    ids = []
    with session_scope(world.sf) as s:
        for i in range(n):
            dep_id = str(uuid4())
            created = start + timedelta(minutes=i // tie_every * tie_every if tie_every else i)
            s.add(DeploymentORM(deployment_id=dep_id, workload_id="w", owner_user_id=owner,
                                state=STATES[i % len(STATES)], requested_instances=1, created_at=created))
            ids.append((created, dep_id, STATES[i % len(STATES)]))
    return ids


def call(path: str, key: str, query: str = "", origin: str | None = None) -> tuple[int, dict, object]:
    headers = [(b"authorization", f"Bearer {key}".encode()), (b"host", b"api.test")]
    if origin:
        headers.append((b"origin", origin.encode()))
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
        "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": query.encode(),
        "headers": headers, "client": ("198.51.100.9", 1234), "server": ("api.test", 80),
    }
    sent = {"status": None, "headers": {}, "body": b""}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            sent["status"] = message["status"]
            sent["headers"] = {k.decode().lower(): v.decode() for k, v in message.get("headers", [])}
        elif message["type"] == "http.response.body":
            sent["body"] += message.get("body", b"")

    asyncio.run(app(scope, receive, send))
    return sent["status"], sent["headers"], json.loads(sent["body"] or b"null")


def _newest_first(rows, states=None):
    return [d for c, d, st in sorted(rows, key=lambda r: (r[0], r[1]), reverse=True) if not states or st in states]


def test_without_limit_or_cursor_the_reply_is_unchanged(world):
    mine = _add(world, 12)
    _add(world, 3, owner="u2")
    status, headers, body = call("/platform/deployments", "u1-key")
    assert status == 200 and isinstance(body, list)
    assert sorted(r["deployment_id"] for r in body) == sorted(d for _, d, _ in mine)
    assert "x-next-cursor" not in headers and "link" not in headers
    status, _, body = call("/platform/deployments", "u1-key", "state=ready")
    assert {r["state"] for r in body} == {"ready"} and len(body) == sum(1 for *_, st in mine if st == "ready")
    # In-process callers (MCP, tests) that pass only the old arguments still work.
    caller = SimpleNamespace(user_id="u1", key_id="k1", admin=False)
    original = routes.require_api_key
    routes.require_api_key = lambda *a, **k: caller
    try:
        assert len(routes.list_deployments(state=None, authorization="x", x_api_key=None)) == 12
    finally:
        routes.require_api_key = original


def test_limit_and_cursor_walk_every_row_once_newest_first_with_the_state_filter(world):
    mine = _add(world, 23)
    _add(world, 5, owner="u2")  # someone else's never show up
    wanted = {"ready", "failed"}
    expected = _newest_first(mine, wanted)
    seen, pages, query = [], 0, "state=ready,failed&limit=3"
    while True:
        status, headers, body = call("/platform/deployments", "u1-key", query)
        assert status == 200 and len(body) <= 3
        assert {r["state"] for r in body} <= wanted
        seen += [r["deployment_id"] for r in body]
        pages += 1
        if "x-next-cursor" not in headers:
            assert "link" not in headers
            break
        link = headers["link"]
        assert link.endswith('>; rel="next"') and link.startswith("</platform/deployments?")
        params = parse_qs(urlsplit(link[1:link.index(">")]).query)
        assert params == {"state": ["ready,failed"], "limit": ["3"], "cursor": [headers["x-next-cursor"]]}
        query = urlsplit(link[1:link.index(">")]).query
    assert seen == expected and len(set(seen)) == len(seen)
    assert pages == -(-len(expected) // 3)


def test_rows_added_while_paging_neither_repeat_nor_hide_older_ones(world):
    mine = _add(world, 10, tie_every=1)
    status, headers, first = call("/platform/deployments", "u1-key", "limit=4")
    newer = _add(world, 3, start=T0 + timedelta(days=1), tie_every=1)  # created meanwhile
    seen = [r["deployment_id"] for r in first]
    cursor = headers["x-next-cursor"]
    while cursor:
        status, headers, body = call("/platform/deployments", "u1-key", f"limit=4&cursor={cursor}")
        seen += [r["deployment_id"] for r in body]
        cursor = headers.get("x-next-cursor")
    assert seen == _newest_first(mine)  # the older rows, each once, in order
    status, _, top = call("/platform/deployments", "u1-key", "limit=3")
    assert [r["deployment_id"] for r in top] == _newest_first(newer)  # a fresh walk starts with the new ones


def test_a_bad_cursor_is_a_400_invalid_cursor(world):
    _add(world, 3)
    for bad in ("garbage!", "bm90IGEgY3Vyc29y", "MnwyMDI2LTExLTAxfGFiYw"):  # junk, "not a cursor", version 2
        status, _, body = call("/platform/deployments", "u1-key", f"limit=2&cursor={bad}")
        assert status == 400 and body["error"]["code"] == "invalid_cursor", bad
        assert "X-Next-Cursor" in body["detail"]["hint"]
    assert CODES["invalid_cursor"][0] == 400
    assert call("/platform/deployments", "u1-key", "limit=0")[0] == 422
    assert call("/platform/deployments", "u1-key", "limit=501")[0] == 422
    status, _, body = call("/platform/deployments", "u1-key", "limit=2&state=nope")
    assert status == 400  # an unknown state, as before


def test_a_cursor_alone_pages_by_100(world):
    _add(world, 105, tie_every=1)
    _, headers, page = call("/platform/deployments", "u1-key", "limit=1")
    _, headers, rest = call("/platform/deployments", "u1-key", f"cursor={headers['x-next-cursor']}")
    assert len(rest) == 100 and "limit=100" in headers["link"]


def test_admins_page_through_everyone_s_with_owner_details(world):
    rows = _add(world, 4) + _add(world, 4, owner="u2", start=T0 + timedelta(hours=1))
    status, headers, body = call("/platform/deployments", "admin-key", "limit=8")
    assert status == 200 and [r["deployment_id"] for r in body] == _newest_first(rows)
    assert {r["owner_email"] for r in body} == {"u1@x.io", "u2@x.io"} and "x-next-cursor" not in headers
    assert call("/platform/deployments", "no-user", "limit=5")[2] == []  # a key bound to no account owns nothing


def test_browsers_can_read_the_paging_headers(world):
    _add(world, 3)
    _, headers, _ = call("/platform/deployments", "u1-key", "limit=1", origin="http://localhost:3000")
    exposed = {h.strip().lower() for h in headers.get("access-control-expose-headers", "").split(",")}
    assert {"x-next-cursor", "link"} <= exposed


def test_the_mcp_tool_pages_when_asked(world):
    _add(world, 5, tie_every=1)
    auth = ("Bearer u1-key", None)
    everything = mcp_module.t_list_rentals(auth, {"state": "ready,failed,terminated,pending,suspended"})
    assert "next_cursor" not in everything and len(everything["rentals"]) == 5
    first = mcp_module.t_list_rentals(auth, {"state": "ready,failed,terminated,pending,suspended", "limit": 3})
    assert len(first["rentals"]) == 3 and first["next_cursor"]
    second = mcp_module.t_list_rentals(auth, {"state": "ready,failed,terminated,pending,suspended", "limit": 3,
                                             "cursor": first["next_cursor"]})
    assert len(second["rentals"]) == 2 and second["next_cursor"] is None
    ids = [r["deployment_id"] for r in first["rentals"] + second["rentals"]]
    assert sorted(ids) == sorted(r["deployment_id"] for r in everything["rentals"])
    schema = next(t for t in mcp_module.TOOLS if t["name"] == "list_rentals")["inputSchema"]["properties"]
    assert schema["limit"]["maximum"] == 500 and "cursor" in schema
