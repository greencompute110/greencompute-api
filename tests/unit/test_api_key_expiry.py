"""Expiring API keys (external API user, request #23: "scoped keys: read-only,
rent-only, spend-limited and expiring"). A key may be made to stop working
after N days or at a set time; a key made with an expiring key never outlives
it; an expired key gets 401 api_key_expired, which doesn't count towards the
per-IP auth-failure throttle (every client behind the host's NAT shares an IP)."""
import asyncio
import importlib.util
import json
import pathlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from pydantic import ValidationError

from greencompute_gateway.infrastructure.repository import GatewayRepository
from greencompute_gateway.main import app
from greencompute_gateway.transport import routes, schemas, security
from greencompute_gateway.transport.errors import CODES, RETRYABLE, describe_error
from greencompute_persistence import CredentialStore, StoredAPIKey, key_expired, session_scope
from greencompute_persistence.orm import APIKeyORM

PAST = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


@pytest.fixture
def db(tmp_path, monkeypatch):
    repo = GatewayRepository(database_url=f"sqlite:///{tmp_path / 'gc.sqlite'}", bootstrap=True)
    monkeypatch.setattr(routes.service, "repository", repo)
    monkeypatch.setattr(security, "credential_store",
                        CredentialStore(engine=repo.engine, session_factory=repo.session_factory))
    return repo


def _key(repo, secret, *, expires_at=None, scopes=(), user_id="u1", admin=False):
    with session_scope(repo.session_factory) as s:
        s.add(APIKeyORM(key_id=f"k-{secret}", user_id=user_id, name=secret, admin=admin, scopes=list(scopes),
                        secret=secret, created_at=datetime.now(UTC), expires_at=expires_at))
    return f"k-{secret}"


def call(method: str, path: str, key: str, body: dict | None = None, ip: str = "198.51.100.7") -> tuple[int, dict]:
    """One request through the real ASGI app (middleware included)."""
    raw = json.dumps(body).encode() if body is not None else b""
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
        "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
        "headers": [(b"authorization", f"Bearer {key}".encode()), (b"content-type", b"application/json"),
                    (b"host", b"api.test")],
        "client": (ip, 1234), "server": ("api.test", 80),
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


# --- refusing an expired key --------------------------------------------------------


def test_an_expired_key_gets_401_api_key_expired_with_when_and_what_to_do(db, monkeypatch):
    _key(db, "old", expires_at=PAST)
    monkeypatch.setattr(routes.service, "list_deployments", lambda **kw: [])
    status, body = call("GET", "/platform/deployments", "old")
    assert status == 401
    assert body["error"]["code"] == "api_key_expired" and body["error"]["retryable"] is False
    assert "expired at 2026-01-02T03:04:05Z" in body["error"]["message"]
    assert "https://www.green-compute.com/settings" in body["error"]["message"]
    assert body["detail"].startswith("expired api key: it expired at 2026-01-02T03:04:05Z")
    assert "api_key_expired" in CODES and CODES["api_key_expired"][0] == 401
    assert "api_key_expired" not in RETRYABLE


def test_expired_key_requests_never_trip_the_per_ip_throttle(db):
    """All clients behind the host's NAT share one IP: one agent with a stale
    key must not get everyone else's bad-key attempts throttled sooner."""
    _key(db, "old", expires_at=PAST)
    ip = "198.51.100.42"
    for _ in range(security.AUTH_FAILURE_LIMIT + 5):
        with pytest.raises(HTTPException) as exc:
            security.require_api_key("Bearer old", None, client_ip=ip)
        assert exc.value.status_code == 401 and exc.value.error_code == "api_key_expired"
    with pytest.raises(HTTPException) as exc:  # a wrong key from that address: 401, not 429
        security.require_api_key("Bearer wrong", None, client_ip=ip)
    assert exc.value.status_code == 401


def test_a_key_works_until_its_expiry(db):
    _key(db, "soon", expires_at=datetime.now(UTC) + timedelta(minutes=5))
    assert security.require_api_key("Bearer soon", None).key_id == "k-soon"
    assert key_expired(SimpleNamespace(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    assert not key_expired(SimpleNamespace(expires_at=None)) and not key_expired(SimpleNamespace())


def test_every_way_in_refuses_an_expired_key(db, monkeypatch):
    """The key-minting route (tries the admin tiers first), MCP and inference."""
    _key(db, "old", expires_at=PAST)
    status, body = call("POST", "/platform/api-keys", "old", {"name": "new"})
    assert (status, body["error"]["code"]) == (401, "api_key_expired")
    status, body = call("POST", "/v1/chat/completions", "old", {"model": "m", "messages": []})
    assert (status, body["error"]["code"]) == (401, "api_key_expired")
    status, body = call("POST", "/mcp", "old", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert status == 401 and "expired at 2026-01-02T03:04:05Z" in body["error"]["message"]


def test_an_expired_admin_key_is_refused_by_the_control_plane_and_validator(monkeypatch):
    from greencompute_control_plane.transport import security as cp_security
    from greencompute_validator.transport import security as val_security

    old = StoredAPIKey(key_id="a", name="admin", admin=True, user_id="u1", secret="s", expires_at=PAST)
    for module in (cp_security, val_security):
        monkeypatch.setattr(module.credential_store, "get_api_key_by_secret", lambda secret: old)
        with pytest.raises(HTTPException) as exc:
            module.require_admin_api_key("Bearer s", None)
        assert exc.value.status_code == 401 and "expired" in exc.value.detail
    assert cp_security.caller_is_admin("Bearer s", None) is False
    assert val_security.caller_user_id("Bearer s", None) is None


def test_a_terminal_ticket_is_no_good_once_its_key_expired(db):
    from greencompute_gateway.application import terminal

    _key(db, "live")
    _key(db, "old", expires_at=PAST)
    assert terminal.key_still_allows(db.session_factory, "k-live", "u1") is True
    assert terminal.key_still_allows(db.session_factory, "k-old", "u1") is False


def test_a_fleet_whose_key_expired_pauses_instead_of_starting_more(db):
    from greencompute_gateway.application import fleets

    _key(db, "old", expires_at=PAST)
    _key(db, "live")
    svc = SimpleNamespace(repository=db)
    assert fleets._key_gone(svc, SimpleNamespace(created_by_key_id="k-old")) == "expired"
    assert fleets._key_gone(svc, SimpleNamespace(created_by_key_id="k-live")) is None
    assert fleets._key_gone(svc, SimpleNamespace(created_by_key_id="k-none")) == "deleted"


# --- creating keys that expire -------------------------------------------------------


def test_expiry_is_days_or_a_time_never_both_and_within_a_year():
    soon = schemas.APIKeyCreate(name="a", expires_in_days=7).requested_expiry()
    assert abs(soon - (datetime.now(UTC) + timedelta(days=7))) < timedelta(seconds=5)
    naive = (datetime.now(UTC) + timedelta(days=2)).replace(tzinfo=None, microsecond=0)
    assert schemas.APIKeyCreate(name="a", expires_at=naive.isoformat()).expires_at == naive.replace(tzinfo=UTC)
    assert schemas.APIKeyCreate(name="a").requested_expiry() is None
    for bad in ({"expires_in_days": 0}, {"expires_in_days": 366},
                {"expires_in_days": 3, "expires_at": naive.isoformat()},
                {"expires_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat()},
                {"expires_at": (datetime.now(UTC) + timedelta(days=370)).isoformat()}):
        with pytest.raises(ValidationError):
            schemas.APIKeyCreate(name="a", **bad)


def test_create_list_and_get_show_the_expiry(db):
    _key(db, "full")
    made = routes.create_api_key(schemas.APIKeyCreate(name="agent", expires_in_days=30), "Bearer full", None)
    assert made["secret"] and made["expired"] is False
    expires = datetime.fromisoformat(made["expires_at"])
    assert timedelta(days=29, hours=23) < expires - datetime.now(UTC) <= timedelta(days=30)
    _key(db, "old", expires_at=PAST)
    listed = {k["name"]: k for k in routes.list_api_keys("Bearer full", None)}
    assert listed["agent"]["expires_at"] == made["expires_at"] and listed["agent"]["expired"] is False
    assert listed["old"]["expired"] is True and listed["old"]["expires_at"].startswith("2026-01-02T03:04:05")
    assert listed["full"]["expires_at"] is None and listed["full"]["expired"] is False
    got = routes.get_api_key(made["key_id"], "Bearer full", None)
    assert got["expires_at"] == made["expires_at"] and "secret" not in got


def test_a_key_made_by_an_expiring_key_never_outlives_it(db):
    parent_expiry = datetime.now(UTC) + timedelta(days=2)
    _key(db, "parent", expires_at=parent_expiry)

    def mint(**kw):
        made = routes.create_api_key(schemas.APIKeyCreate(name="child", **kw), "Bearer parent", None)
        return datetime.fromisoformat(made["expires_at"])

    assert abs(mint() - parent_expiry) < timedelta(seconds=1)  # asked for none: the parent's
    assert abs(mint(expires_in_days=30) - parent_expiry) < timedelta(seconds=1)  # asked for longer: capped
    one_day = mint(expires_in_days=1)
    assert one_day < parent_expiry and one_day - datetime.now(UTC) > timedelta(hours=23)  # shorter: kept


def test_re_saving_a_key_from_a_plain_record_keeps_its_expiry(db):
    """save_api_key with a protocol APIKeyRecord (no expires_at field) must not
    turn an expiring key into one that never expires."""
    from greencompute_protocol import APIKeyRecord

    _key(db, "agent", expires_at=PAST)
    plain = APIKeyRecord(**{k: v for k, v in db.get_api_key("k-agent").model_dump().items() if k != "expires_at"})
    db.save_api_key(plain)
    assert db.get_api_key("k-agent").expires_at is not None


def test_a_wallet_sign_in_key_can_expire(db):
    from greencompute_gateway.application import wallet_auth

    when = datetime.now(UTC) + timedelta(days=3)
    _secret, key, _revoked = wallet_auth._issue_key(
        db.session_factory, "u1", "evm:0xabc", name="agent", scopes=[], max_active_rentals=None,
        daily_spend_limit_usd=None, keep_existing_keys=True, expires_at=when)
    assert abs(datetime.fromisoformat(key["expires_at"]) - when) < timedelta(seconds=1)
    assert abs(db.get_api_key(key["key_id"]).expires_at - when) < timedelta(seconds=1)
    req = schemas.WalletVerifyRequest(nonce="n", signature="0x00", expires_in_days=3)
    assert req.requested_expiry() is not None


# --- migration ------------------------------------------------------------------------


def _migration():
    path = next(pathlib.Path("alembic/versions").glob("*_0079_*.py"))
    spec = importlib.util.spec_from_file_location("m0079", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_0079_adds_and_removes_the_column_on_sqlite(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE api_keys (key_id VARCHAR(64) PRIMARY KEY, user_id VARCHAR(64), "
                             "secret VARCHAR(255))"))
        conn.execute(sa.text("INSERT INTO api_keys VALUES ('k1', 'u1', 's1')"))
    m = _migration()
    assert (m.revision, m.down_revision) == ("20261117_0079", "20261116_0078")

    def run(fn):
        with engine.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                fn()

    run(m.upgrade)
    assert "expires_at" in {c["name"] for c in sa.inspect(engine).get_columns("api_keys")}
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT expires_at FROM api_keys")).scalar() is None  # old keys: never
    run(m.downgrade)
    assert "expires_at" not in {c["name"] for c in sa.inspect(engine).get_columns("api_keys")}
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT secret FROM api_keys")).scalar() == "s1"


def test_rentals_a_key_started_keep_running_after_it_expires(tmp_path):
    """They belong to the account: expiry ends the key, not what it started."""
    from greencompute_control_plane.application.services import ControlPlaneService
    from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository
    from greencompute_persistence.orm import DeploymentORM
    from greencompute_protocol import DeploymentState

    cp = ControlPlaneRepository(database_url=f"sqlite:///{tmp_path / 'cp.sqlite'}", bootstrap=True)
    with session_scope(cp.session_factory) as s:
        s.add(APIKeyORM(key_id="k-old", user_id="u1", name="old", admin=False, scopes=[], secret="old",
                        created_at=PAST - timedelta(days=1), expires_at=PAST))
        s.add(DeploymentORM(deployment_id="d1", workload_id="w", owner_user_id="u1", state="ready",
                            requested_instances=1, created_by_key_id="k-old"))
    svc = ControlPlaneService(repository=cp)
    assert svc.stop_rentals_over_key_limits() == [] and svc.stop_rentals_over_account_limits() == []
    assert cp.get_deployment("d1").state == DeploymentState.READY
