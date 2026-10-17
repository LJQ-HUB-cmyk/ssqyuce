"""V10-00d 迁移加固测试：批次事务回滚 + 迁移前自动备份 + health 暴露 schema_version。

验收口径（``docs/reviews/V10_REVIEW_SUMMARY.md`` §四 V10-00d）：
注入一个故意失败的迁移 → 必须**整体回滚**、``schema_version`` 不前进，
且迁移前的自动备份文件存在。
"""
from __future__ import annotations

import sqlite3

import pytest


def _fresh_db(tmp_path, name="mig.db"):
    conn = sqlite3.connect(tmp_path / name)
    conn.row_factory = sqlite3.Row
    return conn


def _table_names(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_failed_migration_rolls_back_whole_batch_and_keeps_backup(tmp_path, monkeypatch):
    """批次事务：全部待执行迁移要么一起成功，要么一起回滚；schema_version 不前进。"""
    from lottery import migrations

    conn = _fresh_db(tmp_path)
    latest = migrations.MIGRATIONS[-1][0]
    assert migrations.apply(conn) == latest          # 先到最新版本（建立真实基线）
    before_tables = _table_names(conn)

    def _ok_migration(c):
        c.execute("CREATE TABLE v10_ok_marker (x INTEGER)")

    def _failing_migration(c):
        c.execute("CREATE TABLE v10_fail_marker (x INTEGER)")
        raise RuntimeError("injected migration failure")

    injected = list(migrations.MIGRATIONS) + [
        (latest + 1, "v10 injected ok", _ok_migration),
        (latest + 2, "v10 injected fail", _failing_migration),
    ]
    monkeypatch.setattr(migrations, "MIGRATIONS", injected)

    with pytest.raises(RuntimeError, match="injected migration failure"):
        migrations.apply(conn)

    # 1) schema_version 不前进（没有半迁移状态）
    assert migrations.current_version(conn) == latest
    assert conn.execute("SELECT COALESCE(MAX(version),0) FROM schema_version").fetchone()[0] == latest
    # 2) 同一批里"先成功"的那条迁移也一起回滚（批次事务，不是逐条提交）
    after_tables = _table_names(conn)
    assert "v10_ok_marker" not in after_tables, "同一批中已执行的迁移必须一起回滚"
    assert "v10_fail_marker" not in after_tables, "失败迁移自己建的 DDL 也必须回滚"
    # 3) 结构未变
    assert after_tables == before_tables
    # 4) 迁移前自动备份存在（带版本号）
    backup = migrations.backup_path_for(conn, latest)
    assert backup is not None and backup.exists(), f"迁移前备份缺失：{backup}"
    # 备份内容可读，且与迁移前一致（schema_version == latest）
    bconn = sqlite3.connect(str(backup))
    try:
        assert bconn.execute("SELECT COALESCE(MAX(version),0) FROM schema_version").fetchone()[0] == latest
    finally:
        bconn.close()
    conn.close()


def test_migrations_are_idempotent_and_have_no_pending_backup(tmp_path, monkeypatch):
    """正常迁移：幂等（版本 = MIGRATIONS 最后一条），无待执行迁移时不再产生备份。"""
    from lottery import migrations

    conn = _fresh_db(tmp_path, "idem.db")
    latest = migrations.MIGRATIONS[-1][0]
    assert migrations.apply(conn) == latest
    assert migrations.apply(conn) == latest
    assert migrations.apply(conn) == latest

    rows = conn.execute("SELECT version FROM schema_version ORDER BY version").fetchall()
    assert [r[0] for r in rows] == [v for v, _, _ in migrations.MIGRATIONS]
    assert migrations.current_version(conn) == latest
    # 幂等调用没有待执行迁移 → 不做多余备份
    assert migrations.backup_path_for(conn, latest) is not None
    assert not migrations.backup_path_for(conn, latest).exists()
    conn.close()


def test_backup_failure_aborts_migration(tmp_path, monkeypatch):
    """fail-closed：迁移前备份失败 → 不得迁移（版本不前进、DDL 未落地）。"""
    from lottery import migrations

    conn = _fresh_db(tmp_path, "failclosed.db")
    latest = migrations.MIGRATIONS[-1][0]
    assert migrations.apply(conn) == latest

    def _m3(c):
        c.execute("CREATE TABLE v10_should_not_exist (x)")

    monkeypatch.setattr(migrations, "MIGRATIONS",
                        list(migrations.MIGRATIONS) + [(latest + 1, "v10", _m3)])

    def _broken_backup(_conn, _version):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(migrations, "backup_before_migrate", _broken_backup)
    with pytest.raises(OSError, match="disk full"):
        migrations.apply(conn)

    assert migrations.current_version(conn) == latest
    assert "v10_should_not_exist" not in _table_names(conn)
    conn.close()


def test_apply_in_memory_db_skips_backup(tmp_path):
    """内存库没有文件可备份：不得报错，迁移照常完成。"""
    from lottery import migrations

    conn = sqlite3.connect(":memory:")
    latest = migrations.MIGRATIONS[-1][0]
    assert migrations.apply(conn) == latest
    assert migrations.backup_path_for(conn, latest) is None
    conn.close()


def test_health_exposes_schema_version():
    """/api/health 必须暴露 schema_version / schema_latest（部署与回滚时可直接核对）。"""
    from fastapi.testclient import TestClient
    from lottery import api_app, migrations

    r = TestClient(api_app.app).get("/api/health")
    assert r.status_code == 200
    j = r.json()
    assert j["schema_version"] == migrations.MIGRATIONS[-1][0]
    assert j["schema_latest"] == migrations.MIGRATIONS[-1][0]
