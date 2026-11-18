import pytest

import db


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Every test starts with an empty leads table (SQLite by default, Postgres if DATABASE_URL is set)."""
    if db.IS_POSTGRES:
        db.run_sql("DROP TABLE IF EXISTS leads")
    else:
        monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    db.init_db()
