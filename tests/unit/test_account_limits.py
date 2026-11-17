"""Account-wide limits (external API user, request #19: "spend caps per account
and per API key ('stop everything at $X')"). The customer may cap what the
whole account, every key together, spends in any 24 hours and how many
rentals it has running at once. At the spend cap new rentals and paid model
requests are refused and running rentals stop, the same way as at a key's
own limit; only a full-access key may change the caps. For a new wallet
account the stricter of its own cap and the first-week default applies."""
import asyncio
import importlib.util
import json
import pathlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException

from greencompute_control_plane.application.services import ControlPlaneService
from greencompute_control_plane.infrastructure.repository import ControlPlaneRepository
from greencompute_gateway.application.services import AccountLimitError, GatewayService, NewAccountLimitError
from greencompute_gateway.infrastructure.repository import GatewayRepository
from greencompute_gateway.main import app
from greencompute_gateway.transport import routes, security
from greencompute_gateway.transport.errors import CODES, describe_error
from greencompute_persistence import session_scope
from greencompute_persistence.key_limits import NEW_ACCOUNT, OWN, account_limits, account_room, stricter
from greencompute_persistence.orm import APIKeyORM, DeploymentORM, LedgerEntryORM, UserORM
from greencompute_protocol import DeploymentCreateRequest, DeploymentState


@pytest.fixture
def db(tmp_path):
    url = f"sqlite:///{tmp_path / 'gc.sqlite'}"
    cp = ControlPlaneRepository(database_url=url, bootstrap=True)
    return SimpleNamespace(url=url, cp=cp, sf=cp.session_factory)


def _account(sf, uid="u1", *, spend_cents=None, max_rentals=None, wallet=False, age=timedelta(days=30)):
    with session_scope(sf) as s:
        s.add(UserORM(user_id=uid, username=uid, email=None if wallet else f"{uid}@x.io", balance_credits=10_000,
                      profile_metadata={}, created_at=datetime.now(UTC) - age,
                      signup_method="wallet" if wallet else None,
                      daily_spend_limit_cents=spend_cents, max_active_rentals=max_rentals))
    return uid


def _rental(sf, uid="u1", state="ready", key_id=None):
    dep_id = str(uuid4())
    with session_scope(sf) as s:
        s.add(DeploymentORM(deployment_id=dep_id, workload_id="w", owner_user_id=uid, state=state,
                            requested_instances=1, created_by_key_id=key_id))
    return dep_id


def _charge(sf, uid, cents, kind="usage", ref=None):
    with session_scope(sf) as s:
        s.add(LedgerEntryORM(entry_id=str(uuid4()), user_id=uid, amount_cents=-cents, balance_after=0, kind=kind,
                             reference_id=ref, created_at=datetime.now(UTC)))


# --- the rule ------------------------------------------------------------------------


def test_the_stricter_limit_wins_and_a_tie_is_the_new_account_default():
    assert stricter(None, None) == (None, None)
    assert stricter(500, None) == (500, OWN)
    assert stricter(None, 5000) == (5000, NEW_ACCOUNT)
    assert stricter(500, 5000) == (500, OWN)
    assert stricter(9000, 5000) == (5000, NEW_ACCOUNT)
    assert stricter(5000, 5000) == (5000, NEW_ACCOUNT)  # raising one's own wouldn't lift it


def test_no_limits_set_means_none_and_nothing_measured(db):
    _account(db.sf)
    assert account_limits(db.sf, "u1") is None
    assert account_room(db.sf, "u1") == (None, None, {})


def test_rentals_at_once_count_every_key_and_spend_counts_everything(db):
    _account(db.sf, max_rentals=2, spend_cents=1000)
    _rental(db.sf, key_id="k1")
    _rental(db.sf, state="terminated")
    assert account_room(db.sf, "u1")[0] is None
    _rental(db.sf, state="pending", key_id="k2")
    why, source, numbers = account_room(db.sf, "u1")
    assert (why, source, numbers["active_rentals"], numbers["max_active_rentals"]) == ("active", OWN, 2, 2)
    # Spend is checked whether or not a rental is being started.
    _charge(db.sf, "u1", 600)
    _charge(db.sf, "u1", 400, kind="inference")
    _charge(db.sf, "u1", 9000, kind="refund")  # staff clawing back a payment isn't spending
    why, source, numbers = account_room(db.sf, "u1", starting_rental=False)
    assert (why, source, numbers["spent_cents"], numbers["daily_spend_limit_usd"]) == ("spend", OWN, 1000, 10.0)


def test_a_new_wallet_account_gets_the_stricter_of_its_own_and_the_default(db):
    _account(db.sf, "lower", wallet=True, age=timedelta(days=1), spend_cents=2000)  # own $20 < default $50
    limits = account_limits(db.sf, "lower")
    assert (limits["daily_spend_limit_cents"], limits["daily_spend_limit_source"]) == (2000, OWN)
    assert (limits["max_active_rentals"], limits["max_active_rentals_source"]) == (2, NEW_ACCOUNT)
    _account(db.sf, "higher", wallet=True, age=timedelta(days=1), spend_cents=90_000, max_rentals=1)
    limits = account_limits(db.sf, "higher")
    assert (limits["daily_spend_limit_cents"], limits["daily_spend_limit_source"]) == (5000, NEW_ACCOUNT)
    assert (limits["max_active_rentals"], limits["max_active_rentals_source"]) == (1, OWN)
    _charge(db.sf, "higher", 5000)
    why, source, numbers = account_room(db.sf, "higher", starting_rental=False)
    assert (why, source) == ("spend", NEW_ACCOUNT) and numbers["until"]  # the default's numbers, as before


# --- starting rentals -------------------------------------------------------------------


def _service(sf, uid="u1"):
    workload = SimpleNamespace(workload_id="w", owner_user_id=uid, public=False, metadata={}, kind="pod",
                               requirements=SimpleNamespace(supported_gpu_models=[], gpu_count=1))
    created = []
    cp = SimpleNamespace(
        repository=SimpleNamespace(get_workload=lambda _id: workload),
        online_gpu_models=lambda: ["rtx4090"],
        placement_preview=lambda w: (True, {}),
        create_deployment=lambda body: created.append(body) or SimpleNamespace(created=True),
    )
    svc = GatewayService(repository=SimpleNamespace(session_factory=sf, get_api_key=lambda _k: None),
                         control_plane=cp)
    svc._billing_repo = SimpleNamespace(get_balance=lambda _u: 10_000)
    return svc, created


def test_create_deployment_is_refused_at_the_account_s_limits(db, monkeypatch):
    from greencompute_gateway.application import services as services_module

    locked = []
    real_lock = services_module._account_rental_lock
    monkeypatch.setattr(services_module, "_account_rental_lock",
                        lambda svc, uid: (locked.append(uid), real_lock(svc, uid))[1])
    _account(db.sf, max_rentals=1)
    svc, created = _service(db.sf)
    svc.create_deployment({"workload_id": "w"}, user_id="u1", created_by_key_id="k1")
    _rental(db.sf)
    with pytest.raises(AccountLimitError) as exc:
        svc.create_deployment({"workload_id": "w"}, user_id="u1", created_by_key_id="k2")  # any key
    assert exc.value.reason == "active" and len(created) == 1
    assert locked == ["u1", "u1"]  # a limit on rentals at once: created under the account's lock
    svc.create_deployment({"workload_id": "w"}, user_id="u1", admin=True)  # staff aren't held to it

    _account(db.sf, "u2", spend_cents=500)
    _charge(db.sf, "u2", 500)
    svc2, _ = _service(db.sf, "u2")
    locked.clear()
    with pytest.raises(AccountLimitError) as exc:
        svc2.create_deployment({"workload_id": "w"}, user_id="u2")
    assert exc.value.reason == "spend" and locked == [None]  # a spend limit alone needs no lock


def test_a_new_wallet_account_still_hears_new_account_limit_when_the_default_binds(db):
    _account(db.sf, "w1", wallet=True, age=timedelta(days=1), max_rentals=10)
    _rental(db.sf, "w1")
    _rental(db.sf, "w1")
    svc, _ = _service(db.sf, "w1")
    with pytest.raises(NewAccountLimitError):
        svc.create_deployment({"workload_id": "w"}, user_id="w1")


def test_the_route_says_which_limit_with_the_numbers_and_how_to_raise_it(db, monkeypatch):
    _account(db.sf, max_rentals=1, spend_cents=250)
    _rental(db.sf)
    svc, _ = _service(db.sf)
    monkeypatch.setattr(routes, "service", svc)
    key = SimpleNamespace(user_id="u1", admin=False, key_id="k1", scopes=[], max_active_rentals=None,
                          daily_spend_limit_usd=None)
    with pytest.raises(HTTPException) as exc:
        routes._create_deployment(DeploymentCreateRequest(workload_id="w"), key)
    detail = exc.value.detail
    assert (exc.value.status_code, exc.value.error_code) == (403, "account_rental_limit_reached")
    assert (detail["max_active_rentals"], detail["active_rentals"]) == (1, 1)
    assert "PUT /platform/account/limits" in detail["hint"] and "Settings > Account limits" in detail["hint"]
    err = describe_error(403, detail, None, exc.value.error_code)
    assert "1 rentals running, and its limit is 1" in err["message"] and not err["retryable"]

    with session_scope(db.sf) as s:
        s.get(UserORM, "u1").max_active_rentals = None
    _charge(db.sf, "u1", 250)
    with pytest.raises(HTTPException) as exc:
        routes._create_deployment(DeploymentCreateRequest(workload_id="w"), key)
    detail = exc.value.detail
    assert exc.value.error_code == "account_spend_limit_reached"
    assert (detail["daily_spend_limit_usd"], detail["spent_last_24h_usd"]) == (2.5, 2.5)
    err = describe_error(403, detail, None, exc.value.error_code)
    assert "spent $2.50 in the last 24 hours, and its limit is $2.50" in err["message"]
    assert {"account_spend_limit_reached", "account_rental_limit_reached"} <= set(CODES)


def test_paid_model_requests_are_refused_at_the_account_s_spend_limit(db, monkeypatch):
    monkeypatch.setattr(routes.service, "repository", GatewayRepository(database_url=db.url))
    _account(db.sf, spend_cents=300)
    key = SimpleNamespace(user_id="u1", admin=False, key_id="k1", max_active_rentals=None, daily_spend_limit_usd=None)
    routes._enforce_account_spend_limit(key)
    _charge(db.sf, "u1", 300, kind="inference")
    with pytest.raises(HTTPException) as exc:
        routes._enforce_account_spend_limit(key)
    assert exc.value.error_code == "account_spend_limit_reached"
    routes._enforce_account_spend_limit(SimpleNamespace(user_id="u1", admin=True))  # staff pass
    _account(db.sf, "free")
    routes._enforce_account_spend_limit(SimpleNamespace(user_id="free", admin=False))  # no limit set


# --- running rentals ----------------------------------------------------------------------


def test_running_rentals_stop_at_the_account_s_spend_limit_the_way_a_key_s_do(db):
    """Same mechanism as stop_rentals_over_key_limits: the READY deployments
    are TERMINATED with the reason in last_error (a deployment.terminated
    event), whichever key started them; others' and non-ready ones stay."""
    svc = ControlPlaneService(repository=db.cp)
    published = []
    svc.bus = SimpleNamespace(publish=lambda subject, data: published.append((subject, data)))
    _account(db.sf, "capped", spend_cents=1000)
    _account(db.sf, "under", spend_cents=1000)
    _account(db.sf, "free")
    a = _rental(db.sf, "capped", key_id="k1")
    b = _rental(db.sf, "capped", key_id="k2")
    paused = _rental(db.sf, "capped", state="suspended")
    under = _rental(db.sf, "under")
    free = _rental(db.sf, "free")
    _charge(db.sf, "capped", 700, ref=a)
    _charge(db.sf, "capped", 300, kind="inference")
    _charge(db.sf, "under", 999)
    _charge(db.sf, "free", 99_999)

    assert sorted(svc.stop_rentals_over_account_limits()) == sorted([a, b])
    for dep_id in (a, b):
        dep = db.cp.get_deployment(dep_id)
        assert dep.state == DeploymentState.TERMINATED
        assert dep.last_error == ("stopped: its account reached its spend limit of $10.00 for the last 24 hours "
                                  "(Settings > Account limits)")
    assert db.cp.get_deployment(paused).state == DeploymentState.SUSPENDED
    assert db.cp.get_deployment(under).state == DeploymentState.READY
    assert db.cp.get_deployment(free).state == DeploymentState.READY
    assert [s for s, _ in published].count("deployment.terminated") == 2
    assert svc.stop_rentals_over_account_limits() == []

    # And a key's limit does it the same way (the comparison this mirrors).
    with session_scope(db.sf) as s:
        s.add(APIKeyORM(key_id="k9", user_id="free", name="agent", admin=False, scopes=[], secret="s9",
                        daily_spend_limit_cents=100))
    keyed = _rental(db.sf, "free", key_id="k9")
    _charge(db.sf, "free", 100, ref=keyed)
    assert svc.stop_rentals_over_key_limits() == [keyed]
    assert db.cp.get_deployment(keyed).state == DeploymentState.TERMINATED


def test_lowering_the_rentals_limit_below_what_runs_stops_nothing(db):
    _account(db.sf, max_rentals=1)
    running = [_rental(db.sf), _rental(db.sf)]
    svc = ControlPlaneService(repository=db.cp)
    assert svc.stop_rentals_over_account_limits() == []
    assert all(db.cp.get_deployment(d).state == DeploymentState.READY for d in running)
    assert account_room(db.sf, "u1")[0] == "active"  # but no new ones


# --- the API ---------------------------------------------------------------------------------


def call(method: str, path: str, key: str, body: dict | None = None) -> tuple[int, dict]:
    raw = json.dumps(body).encode() if body is not None else b""
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
        "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
        "headers": [(b"authorization", f"Bearer {key}".encode()), (b"content-type", b"application/json"),
                    (b"host", b"api.test")],
        "client": ("198.51.100.8", 1234), "server": ("api.test", 80),
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


@pytest.fixture
def api(db, monkeypatch):
    from greencompute_persistence import CredentialStore

    repo = GatewayRepository(database_url=db.url)
    monkeypatch.setattr(routes.service, "repository", repo)
    monkeypatch.setattr(security, "credential_store",
                        CredentialStore(engine=repo.engine, session_factory=repo.session_factory))
    _account(db.sf)
    with session_scope(db.sf) as s:
        for secret, scopes in (("full", []), ("read", ["read"]), ("rent", ["read", "rentals"]),
                               ("infer", ["inference"]), ("keys", ["keys", "read"])):
            s.add(APIKeyORM(key_id=f"k-{secret}", user_id="u1", name=secret, admin=False, scopes=scopes,
                            secret=secret, created_at=datetime.now(UTC)))
    return db


def test_any_key_of_the_account_reads_the_limits_and_where_it_stands(api):
    _rental(api.sf)
    _charge(api.sf, "u1", 1234)
    for key in ("full", "read", "rent", "infer"):
        status, body = call("GET", "/platform/account/limits", key)
        assert status == 200, (key, body)
    assert body == {
        "daily_spend_limit_usd": None, "max_active_rentals": None, "spent_last_24h_usd": 12.34,
        "active_rentals": 1, "new_account_limits": None,
        "in_force": {"daily_spend_limit_usd": None, "daily_spend_limit_source": None,
                     "max_active_rentals": None, "max_active_rentals_source": None},
    }


def test_only_a_full_access_key_changes_them(api):
    for key in ("read", "rent", "infer", "keys"):
        status, body = call("PUT", "/platform/account/limits", key, {"daily_spend_limit_usd": None})
        assert status == 403 and body["error"]["code"] == "scope_required", (key, body)
        status, _ = call("PATCH", "/platform/account/limits", key, {"max_active_rentals": 1000})
        assert status == 403
    # In-process (MCP-style, no request route to check): the route refuses too.
    scoped = SimpleNamespace(user_id="u1", admin=False, key_id="k-rent", scopes=["rentals"])
    with pytest.raises(HTTPException) as exc:
        routes._set_account_limits(routes.schemas.AccountLimitsUpdate(), scoped, replace=True)
    assert exc.value.error_code == "scope_required"

    status, body = call("PUT", "/platform/account/limits", "full", {"daily_spend_limit_usd": 100, "max_active_rentals": 5})
    assert status == 200 and (body["daily_spend_limit_usd"], body["max_active_rentals"]) == (100.0, 5)
    assert body["in_force"]["daily_spend_limit_source"] == "account"
    status, body = call("PATCH", "/platform/account/limits", "full", {"max_active_rentals": None})
    assert (body["daily_spend_limit_usd"], body["max_active_rentals"]) == (100.0, None)  # only what was sent
    status, body = call("PUT", "/platform/account/limits", "full", {"max_active_rentals": 3})
    assert (body["daily_spend_limit_usd"], body["max_active_rentals"]) == (None, 3)  # PUT sets both
    with session_scope(api.sf) as s:
        assert (s.get(UserORM, "u1").daily_spend_limit_cents, s.get(UserORM, "u1").max_active_rentals) == (None, 3)


def test_limits_are_validated(api):
    for bad in ({"daily_spend_limit_usd": 0.5}, {"daily_spend_limit_usd": 100_001}, {"max_active_rentals": 0},
                {"max_active_rentals": 1001}, {"daily_spend_limit": 10}):
        status, body = call("PUT", "/platform/account/limits", "full", bad)
        assert status == 422 and body["error"]["code"] == "validation_error", bad
    assert call("PUT", "/platform/account/limits", "full", {"daily_spend_limit_usd": 1, "max_active_rentals": 1000})[0] == 200


def test_a_new_wallet_account_sees_its_first_week_limits_in_force(api):
    _account(api.sf, "w1", wallet=True, age=timedelta(days=1), spend_cents=9000)
    with session_scope(api.sf) as s:
        s.add(APIKeyORM(key_id="k-w", user_id="w1", name="w", admin=False, scopes=[], secret="wallet-key",
                        created_at=datetime.now(UTC)))
    status, body = call("GET", "/platform/account/limits", "wallet-key")
    assert status == 200 and body["daily_spend_limit_usd"] == 90.0
    assert body["new_account_limits"]["daily_spend_limit_usd"] == 50
    assert body["in_force"] == {"daily_spend_limit_usd": 50.0, "daily_spend_limit_source": "new_account",
                                "max_active_rentals": 2, "max_active_rentals_source": "new_account"}


# --- migration ----------------------------------------------------------------------------------


def test_migration_0080_adds_and_removes_the_columns_on_sqlite(tmp_path):
    path = next(pathlib.Path("alembic/versions").glob("*_0080_*.py"))
    spec = importlib.util.spec_from_file_location("m0080", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert (m.revision, m.down_revision) == ("20261117_0080", "20261117_0079")
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE users (user_id VARCHAR(64) PRIMARY KEY, username VARCHAR(64))"))
        conn.execute(sa.text("INSERT INTO users VALUES ('u1', 'ada')"))

    def run(fn):
        with engine.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                fn()

    def columns():
        return {c["name"] for c in sa.inspect(engine).get_columns("users")}

    run(m.upgrade)
    assert {"daily_spend_limit_cents", "max_active_rentals"} <= columns()
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT daily_spend_limit_cents, max_active_rentals FROM users")).first() == (None, None)
    run(m.downgrade)
    assert not {"daily_spend_limit_cents", "max_active_rentals"} & columns()
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT username FROM users")).scalar() == "ada"
