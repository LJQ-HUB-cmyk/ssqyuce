from fastapi.testclient import TestClient


def test_migrations_are_ordered_and_idempotent(tmp_path, monkeypatch):
    from lottery import db, migrations
    import sqlite3
    conn = sqlite3.connect(tmp_path / "migration.db")
    conn.row_factory = sqlite3.Row
    # V10-00d：断言改为「最新版本 = MIGRATIONS 最后一条」，以后新增迁移无需再改本测试
    latest = migrations.MIGRATIONS[-1][0]
    assert migrations.apply(conn) == latest
    # 幂等：再跑一次仍是同一版本，且不重复写入
    assert migrations.apply(conn) == latest
    assert conn.execute("select count(*) from schema_version").fetchone()[0] == len(migrations.MIGRATIONS)
    assert conn.execute("select max(version) from schema_version").fetchone()[0] == latest
    conn.close()


def test_api_token_optional_and_required(tmp_path, monkeypatch):
    monkeypatch.setenv("LOTT_HOME", str(tmp_path))
    monkeypatch.setenv("LOTT_DB", str(tmp_path / "api.db"))
    from lottery import config, db
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "api.db")
    config.API_TOKEN = "test-secret"
    db.close()
    from lottery.api_app import app
    client = TestClient(app)
    assert client.get("/api/info").status_code == 401
    assert client.get("/api/info", headers={"Authorization": "Bearer test-secret"}).status_code == 200
    assert client.get("/api/health", headers={"Authorization": "Bearer test-secret"}).json()["api_auth_enabled"] is True
    config.API_TOKEN = None
    db.close()
