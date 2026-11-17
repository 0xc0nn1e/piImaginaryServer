from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, MetaData, String, Table, create_engine, inspect, select


def test_ignite_upgrade_preserves_legacy_sessions() -> None:
    """Exercise the appended revision on a populated pre-Ignite table."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    metadata = MetaData()
    sessions = Table(
        "web_sessions", metadata, Column("session_token_digest", String(64), primary_key=True)
    )
    metadata.create_all(engine)
    path = Path(__file__).parents[2] / "migrations/versions/0014_ignite_auth.py"
    spec = importlib.util.spec_from_file_location("ignite_revision", path)
    assert spec is not None and spec.loader is not None
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    with engine.begin() as connection:
        connection.execute(sessions.insert().values(session_token_digest="d" * 64))
        with Operations.context(MigrationContext.configure(connection)):
            revision.upgrade()
        upgraded = Table("web_sessions", MetaData(), autoload_with=connection)
        assert connection.execute(select(upgraded)).one() == ("d" * 64, None)
        inspector = inspect(connection)
        assert "oidc_login_flows" in inspector.get_table_names()
        assert inspector.get_indexes("oidc_login_flows")[0]["column_names"] == ["expires_at"]
        with Operations.context(MigrationContext.configure(connection)):
            revision.downgrade()
        assert connection.scalar(select(sessions.c.session_token_digest)) == "d" * 64
        assert "oidc_login_flows" not in inspect(connection).get_table_names()
    engine.dispose()
