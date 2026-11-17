"""Migration 0081 (fleets.deleted_at) runs both ways on SQLite and marks the
fleets stopped so far as deleted (only DELETE ever stopped one)."""
import importlib.util
import pathlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def _migration():
    path = next(pathlib.Path("alembic/versions").glob("*_0081_*.py"))
    spec = importlib.util.spec_from_file_location("m0081", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(engine, fn):
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            fn()


def test_upgrade_marks_stopped_fleets_deleted_then_downgrades(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE fleets (fleet_id VARCHAR(64) PRIMARY KEY, state VARCHAR(16), "
                             "updated_at DATETIME)"))
        conn.execute(sa.text("INSERT INTO fleets VALUES ('f-live', 'active', '2026-11-10 10:00:00'), "
                             "('f-gone', 'stopped', '2026-11-11 12:00:00')"))
    m = _migration()
    assert (m.revision, m.down_revision) == ("20261118_0081", "20261117_0080")
    _run(engine, m.upgrade)
    with engine.connect() as conn:
        rows = dict(conn.execute(sa.text("SELECT fleet_id, deleted_at FROM fleets")).all())
    assert rows["f-live"] is None and str(rows["f-gone"]).startswith("2026-11-11 12:00:00")
    _run(engine, m.downgrade)
    assert "deleted_at" not in {c["name"] for c in sa.inspect(engine).get_columns("fleets")}


def test_on_postgres_it_sets_a_short_lock_timeout_first():
    import io

    m = _migration()
    for step in (m.upgrade, m.downgrade):
        buf = io.StringIO()
        ctx = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buf})
        with Operations.context(ctx):
            step()
        sql = buf.getvalue()
        assert sql.lstrip().startswith("SET LOCAL lock_timeout = '5s'"), sql
