"""Admin deployment list enriches rows with the owner's email/username so
sales/admin can identify the customer behind each deployment (the UUID
owner_user_id alone isn't searchable by a human)."""
from greencompute_gateway.transport.routes import _attach_owner_info
from greencompute_protocol import UserRecord


def test_attach_owner_info_adds_email_and_username():
    alice = UserRecord(user_id="u-alice", username="alice", email="alice@x.io")
    rows = [{"deployment_id": "d1", "owner_user_id": "u-alice"}]

    _attach_owner_info(rows, {alice.user_id: alice})

    assert rows[0]["owner_email"] == "alice@x.io"
    assert rows[0]["owner_username"] == "alice"


def test_attach_owner_info_skips_unknown_owner():
    rows = [{"deployment_id": "d1", "owner_user_id": "ghost"}, {"deployment_id": "d2"}]
    _attach_owner_info(rows, {})
    assert "owner_email" not in rows[0]
    assert "owner_email" not in rows[1]
