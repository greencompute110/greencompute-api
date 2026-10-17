"""Website admins by account: an admin adds an existing account by email; the
website server (provision secret) then gets that account a managed admin key.
The account's own keys stay non-admin, an ordinary key can't fetch the admin
key, and removing the admin deletes it."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from greencompute_gateway.application.services import WEBSITE_ADMIN_KEY_NAME, GatewayService
from greencompute_gateway.infrastructure.repository import GatewayRepository
from greencompute_gateway.transport import routes, security
from greencompute_persistence import CredentialStore, session_scope
from greencompute_persistence.orm import UserORM
from greencompute_protocol import APIKeyCreateRequest

PROVISION = "provision-secret-for-tests"


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = GatewayRepository(database_url=f"sqlite:///{tmp_path / 'a.sqlite'}", bootstrap=True)
    with session_scope(repo.session_factory) as s:
        s.add(UserORM(user_id="u-ana", username="ana", email="ana@green-compute.com", balance_credits=0))
        s.add(UserORM(user_id="u-bob", username="bob", email="bob@example.com", balance_credits=0))
    svc = GatewayService(repository=repo)
    monkeypatch.setattr(routes, "service", svc)
    monkeypatch.setattr(security, "credential_store",
                        CredentialStore(engine=repo.engine, session_factory=repo.session_factory))
    monkeypatch.setenv("GREENCOMPUTE_PROVISION_SECRET", PROVISION)
    monkeypatch.delenv("GREENCOMPUTE_ADMIN_API_KEY", raising=False)
    own = svc.create_api_key(APIKeyCreateRequest(name="default", user_id="u-ana"), caller_user_id="u-ana",
                             allow_user_id_override=True)
    root = repo.save_api_key(own.model_copy(update={"key_id": "root", "name": "root", "admin": True,
                                                    "user_id": None, "secret": "gk_root_admin"}))
    return SimpleNamespace(svc=svc, repo=repo, own=own, root=root)


def website_key(secret, user_id):
    return routes.website_admin_key({"user_id": user_id}, authorization=None, x_api_key=secret)


def test_an_admin_account_gets_a_working_admin_key(env):
    added = routes.add_admin({"email": "  ANA@Green-Compute.com "}, authorization=None, x_api_key="gk_root_admin")
    assert (added["user_id"], added["username"]) == ("u-ana", "ana")
    first = website_key(PROVISION, "u-ana")
    assert first["admin"] is True
    assert security.credential_store.get_api_key_by_secret(first["key"]).admin is True
    assert website_key(PROVISION, "u-ana")["key"] == first["key"]  # one key, reused


def test_everyone_else_gets_no_admin_key(env):
    assert website_key(PROVISION, "u-bob") == {"admin": False, "key": None}


def test_an_accounts_own_key_cannot_fetch_the_admin_key(env):
    env.svc.add_admin("ana@green-compute.com", added_by="root")
    with pytest.raises(HTTPException) as exc:
        website_key(env.own.secret, "u-ana")
    assert exc.value.status_code in (401, 403)
    assert security.credential_store.get_api_key_by_secret(env.own.secret).admin is False


def test_only_admins_manage_admins(env):
    with pytest.raises(HTTPException) as exc:
        routes.add_admin({"email": "bob@example.com"}, authorization=None, x_api_key=env.own.secret)
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        routes.add_admin({"email": "nobody@example.com"}, authorization=None, x_api_key="gk_root_admin")
    assert exc.value.status_code == 404 and "sign up" in exc.value.detail


def test_removing_an_admin_ends_access_immediately(env):
    env.svc.add_admin("ana@green-compute.com", added_by="root")
    key = website_key(PROVISION, "u-ana")["key"]
    routes.remove_admin("ana@green-compute.com", authorization=None, x_api_key="gk_root_admin")
    assert security.credential_store.get_api_key_by_secret(key) is None
    assert website_key(PROVISION, "u-ana")["admin"] is False
    assert routes.list_admins(authorization=None, x_api_key="gk_root_admin") == []


def test_the_managed_key_is_hidden_from_the_accounts_own_key_list(env):
    env.svc.add_admin("ana@green-compute.com", added_by="root")
    website_key(PROVISION, "u-ana")
    names = [k["name"] for k in routes.list_api_keys(authorization=None, x_api_key=env.own.secret)]
    assert names == ["default"]
    admin_view = [k["name"] for k in routes.list_api_keys(authorization=None, x_api_key="gk_root_admin")]
    assert WEBSITE_ADMIN_KEY_NAME in admin_view
