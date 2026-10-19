"""Deleting an account at its owner's request (UK GDPR erasure): refused while
anything is still running; otherwise keys, wallets, secrets and the profile go,
remaining credit is forfeited on the ledger, and payment records stay under the
anonymised user id."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from greencompute_gateway.application import billing_monitor
from greencompute_gateway.application.services import GatewayService
from greencompute_gateway.infrastructure.repository import GatewayRepository
from greencompute_gateway.transport import routes, security
from greencompute_persistence import CredentialStore, session_scope
from greencompute_persistence.orm import (
    AdminAccountORM,
    CryptoInvoiceORM,
    DeploymentORM,
    LedgerEntryORM,
    UserORM,
    UserSecretORM,
    VerifiedWalletORM,
    WorkloadORM,
)
from greencompute_protocol import APIKeyCreateRequest, UserRegistrationRequest
from sqlalchemy import select

PROVISION = "provision-secret-for-tests"
CONFIRM = {"confirm": "DELETE"}


def _workload(wid, owner, **extra):
    return WorkloadORM(
        workload_id=wid, owner_user_id=owner, name=wid, image="ghcr.io/x/pod:latest", kind="pod",
        security_tier="standard", pricing_class="standard", requirements={"gpu_count": 1}, runtime={},
        lifecycle={}, tags=["mine"], public=False, **extra,
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = GatewayRepository(database_url=f"sqlite:///{tmp_path / 'a.sqlite'}", bootstrap=True)
    now = datetime.now(UTC)
    with session_scope(repo.session_factory) as s:
        s.add(UserORM(user_id="u-ana", username="ana", email="ana@example.com", display_name="Ana",
                      bio="hi", profile_metadata={"company": "Ana Ltd"}, balance_credits=1234))
        s.add(UserORM(user_id="u-bob", username="bob", email="bob@example.com", balance_credits=500))
        s.add(LedgerEntryORM(entry_id="l1", user_id="u-ana", amount_cents=2000, balance_after=2000,
                             kind="topup", description="Card top-up", created_at=now - timedelta(days=2)))
        s.add(LedgerEntryORM(entry_id="l2", user_id="u-ana", amount_cents=-766, balance_after=1234,
                             kind="usage", created_at=now - timedelta(days=1)))
        s.add(LedgerEntryORM(entry_id="l3", user_id="u-bob", amount_cents=500, balance_after=500,
                             kind="topup", created_at=now - timedelta(days=1)))
        s.add(VerifiedWalletORM(wallet_id="w1", user_id="u-ana", chain="evm", address="0xabc"))
        s.add(VerifiedWalletORM(wallet_id="w2", user_id="u-bob", chain="evm", address="0xdef"))
        s.add(UserSecretORM(secret_id="s1", user_id="u-ana", name="HF_TOKEN", value="hf_secret"))
        s.add(AdminAccountORM(user_id="u-ana", email="ana@example.com"))
        for iid, status in (("inv-open", "pending"), ("inv-paid", "confirmed")):
            s.add(CryptoInvoiceORM(invoice_id=iid, user_id="u-ana", currency="usdc-base", amount_crypto=10.0,
                                   amount_usd=10.0, total_credits=1000, deposit_address="0xdeposit",
                                   status=status, expires_at=now + timedelta(hours=1)))
        s.add(_workload("wl-pod", "u-ana", ingress_host="ana-pod.inference.green-compute.com",
                        display_name="Ana's pod", readme="notes",
                        metadata_json={"env": {"API_TOKEN": "x"}, "command": "python run.py",
                                       "ssh_public_keys": ["ssh-ed25519 AAAA ana@laptop"],
                                       "hf_token": "hf_abc", "ports": [8888]}))
        s.add(_workload("wl-bob", "u-bob", metadata_json={"env": {"A": "b"}}))
        s.add(DeploymentORM(deployment_id="d-old", workload_id="wl-pod", owner_user_id="u-ana",
                            state="terminated", requested_instances=1, ssh_private_key="-----BEGIN KEY-----",
                            hourly_rate_cents=40))
        s.add(DeploymentORM(deployment_id="d-bob", workload_id="wl-bob", owner_user_id="u-bob",
                            state="ready", requested_instances=1, ssh_private_key="-----BEGIN BOB-----"))
    svc = GatewayService(repository=repo)
    monkeypatch.setattr(routes, "service", svc)
    monkeypatch.setattr(security, "credential_store",
                        CredentialStore(engine=repo.engine, session_factory=repo.session_factory))
    monkeypatch.setenv("GREENCOMPUTE_PROVISION_SECRET", PROVISION)
    monkeypatch.delenv("GREENCOMPUTE_ADMIN_API_KEY", raising=False)

    def key(user_id, name="default", **extra):
        return svc.create_api_key(APIKeyCreateRequest(name=name, user_id=user_id, **extra),
                                  caller_user_id=user_id, allow_user_id_override=True)

    return SimpleNamespace(svc=svc, repo=repo, key=key, ana=key("u-ana"), bob=key("u-bob"))


def _run(env, state):
    with session_scope(env.repo.session_factory) as s:
        s.add(DeploymentORM(deployment_id=f"d-{state}", workload_id="wl-pod", owner_user_id="u-ana",
                            state=state, requested_instances=1))


def _user(env, user_id):
    with session_scope(env.repo.session_factory) as s:
        return s.get(UserORM, user_id)


def test_preview_shows_the_credit_that_would_be_lost(env):
    out = routes.account_deletion_preview(None, authorization=None, x_api_key=env.ana.secret)
    assert out["balance_cents"] == 1234
    assert out["credit_lost_usd"] == 12.34
    assert out["can_delete"] is True and out["active_deployments"] == []
    assert out["api_keys"] == 1 and out["verified_wallets"] == 1


@pytest.mark.parametrize("state", ["pending", "pulling", "ready", "suspended"])
def test_refused_while_a_rental_or_deployment_is_active(env, state):
    _run(env, state)
    preview = routes.account_deletion_preview(None, authorization=None, x_api_key=env.ana.secret)
    assert preview["can_delete"] is False
    assert [d["deployment_id"] for d in preview["active_deployments"]] == [f"d-{state}"]
    with pytest.raises(HTTPException) as exc:
        routes.delete_account(CONFIRM, authorization=None, x_api_key=env.ana.secret)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "active_deployments"
    assert exc.value.detail["deployments"][0]["state"] == state
    # Nothing was touched.
    assert security.credential_store.get_api_key_by_secret(env.ana.secret) is not None
    assert _user(env, "u-ana").email == "ana@example.com"


def test_failed_and_terminated_deployments_do_not_block(env):
    _run(env, "failed")
    assert routes.delete_account(CONFIRM, authorization=None, x_api_key=env.ana.secret)["deleted"] is True


def test_needs_the_confirmation_word(env):
    for body in ({}, {"confirm": "delete"}, {"confirm": "yes"}):
        with pytest.raises(HTTPException) as exc:
            routes.delete_account(body, authorization=None, x_api_key=env.ana.secret)
        assert exc.value.status_code == 400
    assert _user(env, "u-ana").email == "ana@example.com"


def test_deleting_erases_access_and_personal_data_but_keeps_payment_records(env):
    admin_key = env.svc.website_admin_key("u-ana")
    agent_key = env.key("u-ana", name="agent")
    out = routes.delete_account(CONFIRM, authorization=f"Bearer {env.ana.secret}", x_api_key=None)
    assert out["deleted"] is True and out["already_deleted"] is False
    assert out["credit_forfeited_cents"] == 1234
    assert out["api_keys_revoked"] == 3 and out["wallets_removed"] == 1 and out["secrets_deleted"] == 1

    # Every key of the account stops working, the website's and its admin key included.
    for secret in (env.ana.secret, agent_key.secret, admin_key):
        assert security.credential_store.get_api_key_by_secret(secret) is None
    with pytest.raises(HTTPException) as exc:
        routes.account_deletion_preview(None, authorization=None, x_api_key=env.ana.secret)
    assert exc.value.status_code == 401

    with session_scope(env.repo.session_factory) as s:
        user = s.get(UserORM, "u-ana")
        assert (user.email, user.display_name, user.bio) == (None, None, None)
        assert user.username == "deleted-uana"  # from the id, not the old name
        assert set(user.profile_metadata) == {"deleted_at"}
        assert user.balance_credits == 0
        # Ledger kept, plus one entry recording the forfeited credit.
        ledger = s.scalars(select(LedgerEntryORM).where(LedgerEntryORM.user_id == "u-ana")
                           .order_by(LedgerEntryORM.created_at)).all()
        assert [e.kind for e in ledger] == ["topup", "usage", "account_closed"]
        assert (ledger[-1].amount_cents, ledger[-1].balance_after) == (-1234, 0)
        assert s.scalars(select(VerifiedWalletORM).where(VerifiedWalletORM.user_id == "u-ana")).all() == []
        assert s.scalars(select(UserSecretORM).where(UserSecretORM.user_id == "u-ana")).all() == []
        assert s.get(AdminAccountORM, "u-ana") is None
        # Payment records stay; an open invoice can no longer be credited.
        assert s.get(CryptoInvoiceORM, "inv-open").status == "rejected"
        assert s.get(CryptoInvoiceORM, "inv-paid").status == "confirmed"
        # Workload and deployment rows stay (they explain the charges), minus
        # anything personal or secret.
        wl = s.get(WorkloadORM, "wl-pod")
        assert wl.metadata_json == {"ports": [8888]}
        assert (wl.ingress_host, wl.display_name, wl.readme, wl.tags, wl.public) == (None, None, None, [], False)
        dep = s.get(DeploymentORM, "d-old")
        assert dep.ssh_private_key is None and dep.hourly_rate_cents == 40 and dep.owner_user_id == "u-ana"
        # The balance still equals the ledger, so the billing monitor stays quiet.
        assert billing_monitor.ledger_drift(s) == []


def test_other_accounts_are_untouched(env):
    routes.delete_account(CONFIRM, authorization=None, x_api_key=env.ana.secret)
    assert security.credential_store.get_api_key_by_secret(env.bob.secret) is not None
    with session_scope(env.repo.session_factory) as s:
        bob = s.get(UserORM, "u-bob")
        assert (bob.email, bob.balance_credits) == ("bob@example.com", 500)
        assert s.get(VerifiedWalletORM, "w2") is not None
        assert s.get(WorkloadORM, "wl-bob").metadata_json == {"env": {"A": "b"}}
        assert s.get(DeploymentORM, "d-bob").ssh_private_key == "-----BEGIN BOB-----"


def test_a_debt_is_not_written_off(env):
    with session_scope(env.repo.session_factory) as s:
        s.get(UserORM, "u-ana").balance_credits = -300
        s.add(LedgerEntryORM(entry_id="l4", user_id="u-ana", amount_cents=-1534, balance_after=-300,
                             kind="storage", created_at=datetime.now(UTC)))
    out = routes.delete_account(CONFIRM, authorization=None, x_api_key=env.ana.secret)
    assert out["credit_forfeited_cents"] == 0
    assert _user(env, "u-ana").balance_credits == -300


def test_scoped_and_admin_keys_cannot_delete(env):
    scoped = env.key("u-ana", name="reader", scopes=["read"])
    token = security.current_request_route.set(("POST", "/platform/account/delete"))
    try:
        with pytest.raises(HTTPException) as exc:
            routes.delete_account(CONFIRM, authorization=None, x_api_key=scoped.secret)
        assert exc.value.status_code == 403
    finally:
        security.current_request_route.reset(token)
    admin_key = env.svc.website_admin_key("u-ana")
    with pytest.raises(HTTPException) as exc:
        routes.delete_account(CONFIRM, authorization=None, x_api_key=admin_key)
    assert exc.value.status_code == 403
    # A user_id in the body is ignored for an account's own key: Bob's key
    # only ever reaches Bob's account (refused here: his rental is running).
    with pytest.raises(HTTPException) as exc:
        routes.delete_account({**CONFIRM, "user_id": "u-ana"}, authorization=None, x_api_key=env.bob.secret)
    assert exc.value.status_code == 409
    assert [d["deployment_id"] for d in exc.value.detail["deployments"]] == ["d-bob"]
    assert _user(env, "u-ana").email == "ana@example.com"


def test_the_website_server_can_delete_by_id_and_retry(env):
    with pytest.raises(HTTPException) as exc:
        routes.delete_account(CONFIRM, authorization=None, x_api_key=PROVISION)
    assert exc.value.status_code == 400  # must say whose account
    out = routes.delete_account({**CONFIRM, "user_id": "u-ana"}, authorization=None, x_api_key=PROVISION)
    assert out["deleted"] is True and out["already_deleted"] is False
    again = routes.delete_account({**CONFIRM, "user_id": "u-ana"}, authorization=None, x_api_key=PROVISION)
    assert again == {"deleted": True, "already_deleted": True, "user_id": "u-ana"}
    with pytest.raises(HTTPException) as exc:
        routes.account_deletion_preview("u-ana", authorization=None, x_api_key=PROVISION)
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        routes.delete_account({**CONFIRM, "user_id": "u-nobody"}, authorization=None, x_api_key=PROVISION)
    assert exc.value.status_code == 404


def test_signing_up_again_with_the_same_email_starts_a_new_account(env):
    routes.delete_account(CONFIRM, authorization=None, x_api_key=env.ana.secret)
    fresh = env.svc.register_user(UserRegistrationRequest(username="ana", email="ana@example.com"))
    assert fresh.user_id != "u-ana"
    assert fresh.balance_credits == 0
