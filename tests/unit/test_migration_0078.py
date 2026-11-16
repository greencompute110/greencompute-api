"""Migration 0078 (wallet sign-in, sanctions) runs both ways on SQLite, the
tests' database, and leaves the columns the ORM expects. (Production is
Postgres; the operations used, create_table / add_column / batch drops,
are the portable ones.)"""
import importlib.util
import pathlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def _migration():
    path = next(pathlib.Path("alembic/versions").glob("*_0078_*.py"))
    spec = importlib.util.spec_from_file_location("m0078", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(engine, fn):
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            fn()


def _columns(engine, table):
    return {c["name"] for c in sa.inspect(engine).get_columns(table)}


def test_upgrade_then_downgrade_on_sqlite(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    with engine.begin() as conn:
        # The three tables it changes, as 0077 left them (the columns that matter).
        conn.execute(sa.text("CREATE TABLE users (user_id VARCHAR(64) PRIMARY KEY, username VARCHAR(64), email VARCHAR(255))"))
        conn.execute(sa.text("CREATE TABLE api_keys (key_id VARCHAR(64) PRIMARY KEY, user_id VARCHAR(64), secret VARCHAR(255))"))
        conn.execute(sa.text("CREATE TABLE verified_wallets (wallet_id VARCHAR(64) PRIMARY KEY, user_id VARCHAR(64), "
                             "chain VARCHAR(16), address VARCHAR(128))"))
        conn.execute(sa.text("INSERT INTO users VALUES ('u1', 'ada', 'ada@x.io')"))
    m = _migration()
    assert (m.revision, m.down_revision) == ("20261116_0078", "20261102_0077")

    _run(engine, m.upgrade)
    assert {"signup_method", "email_verified_at"} <= _columns(engine, "users")
    assert "issued_by_wallet" in _columns(engine, "api_keys")
    assert {"sanctions_hit", "sanctions_checked_at"} <= _columns(engine, "verified_wallets")
    assert {"nonce", "chain", "address", "action", "message", "expires_at", "used_at"} <= _columns(
        engine, "wallet_auth_challenges")
    assert {"source", "chain", "address", "label", "loaded_at"} <= _columns(engine, "sanctioned_addresses")
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT username FROM users")).scalar() == "ada"  # data kept

    _run(engine, m.downgrade)
    assert "signup_method" not in _columns(engine, "users")
    assert "issued_by_wallet" not in _columns(engine, "api_keys")
    assert not {"wallet_auth_challenges", "sanctioned_addresses"} & set(sa.inspect(engine).get_table_names())
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT email FROM users")).scalar() == "ada@x.io"
