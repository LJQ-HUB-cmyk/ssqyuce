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


def _seed_data(conn):
    """播种一行业务数据，使 ``has_existing_data()`` 为 True。

    A1 之后"是否需要迁移前备份"按**库内有无既有数据**判断（而不是 schema_version>0），
    因此凡是要验证"备份被触发/备份失败要 fail-closed"的用例，都必须先播种数据。
    """
    conn.execute("CREATE TABLE IF NOT EXISTS draws (issue TEXT, reds TEXT, blue INTEGER)")
    conn.execute("INSERT INTO draws VALUES ('2003001', '1,2,3,4,5,6', 7)")
    conn.commit()
    return conn


def _table_names(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_failed_migration_rolls_back_whole_batch_and_keeps_backup(tmp_path, monkeypatch):
    """批次事务：全部待执行迁移要么一起成功，要么一起回滚；schema_version 不前进。"""
    from lottery import migrations

    conn = _seed_data(_fresh_db(tmp_path))
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

    conn = _seed_data(_fresh_db(tmp_path, "failclosed.db"))
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


# ---------- V10 二次评审 A1/A2 回归测试 ----------
# 说明：这两条对应 docs/reviews/V10_REVIEW_ROUND2_SUMMARY.md §二 A1/A2。
# 它们必须能捕获"退回旧实现"的回归——旧实现的失败方式分别是：
#   A1：老库（无 schema_version 表但有数据，current==0）升级时**不备份**；
#   A2：调用方已有事务时 backup() 无限重试 → 挂起（并使 SAVEPOINT 分支不可达）。


def test_legacy_db_without_schema_version_is_backed_up(tmp_path):
    """A1：**没有 schema_version 表**但有数据的老库，升级时必须留备份。

    旧实现判据是 ``current > 0``，而老库 ``current_version() == 0`` →
    跳过备份，正好击中"生产库零备份"这个立项理由。
    """
    from lottery import migrations

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # 造一个真实老库：有业务表与数据，但没有 schema_version 表
    conn.execute("CREATE TABLE draws (issue TEXT, reds TEXT, blue INTEGER)")
    conn.execute("INSERT INTO draws VALUES ('2026001', '1,2,3,4,5,6', 7)")
    conn.commit()
    assert migrations.current_version(conn) == 0
    assert migrations.has_existing_data(conn) is True

    migrations.apply(conn)

    backup = migrations.backup_path_for(conn, 0)
    assert backup is not None and backup.exists(), \
        "老库升级必须留迁移前备份（A1 回归）"
    # 备份内容可读且含原数据
    bconn = sqlite3.connect(backup)
    try:
        assert bconn.execute("SELECT count(*) FROM draws").fetchone()[0] == 1
    finally:
        bconn.close()
    conn.close()


def test_empty_db_does_not_create_backup(tmp_path):
    """A1 反面：空库没有可丢内容，不应产生备份文件。"""
    from lottery import migrations

    conn = _fresh_db(tmp_path, "empty.db")
    assert migrations.has_existing_data(conn) is False
    migrations.apply(conn)
    backup = migrations.backup_path_for(conn, 0)
    assert backup is not None and not backup.exists()
    conn.close()


def test_apply_with_caller_transaction_does_not_hang_and_keeps_it_open(tmp_path):
    """A2：调用方已有未提交事务时，apply 必须**在有限时间内完成**且不提交对方工作。

    旧实现用调用方的 conn 做 backup()，BUSY 时无限重试 → 挂起；
    且 SAVEPOINT 嵌套分支永远不可达。
    """
    from lottery import migrations

    path = tmp_path / "caller_tx.db"
    conn = sqlite3.connect(path, timeout=1)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE draws (issue TEXT)")
    conn.execute("INSERT INTO draws VALUES ('2026001')")
    conn.commit()

    conn.execute("BEGIN")
    conn.execute("INSERT INTO draws VALUES ('UNCOMMITTED')")
    assert conn.in_transaction is True

    migrations.apply(conn)                      # 不得挂起

    # 备份走独立只读连接，因此能看到"已提交"的数据（1 条），而不是未提交的 2 条
    backup = migrations.backup_path_for(conn, 0)
    assert backup is not None and backup.exists()
    bconn = sqlite3.connect(backup)
    try:
        n = bconn.execute("SELECT count(*) FROM draws").fetchone()[0]
    finally:
        bconn.close()
    assert n == 1, "迁移前备份不得包含调用方未提交的数据"

    # 调用方事务仍然开着，其未提交写入可被回滚 → 证明没被 apply 提交
    conn.rollback()
    rows = [r[0] for r in conn.execute("SELECT issue FROM draws")]
    assert "UNCOMMITTED" not in rows, "apply 不得自动提交调用方未完成的事务"
    conn.close()


def test_backup_does_not_use_caller_connection(tmp_path):
    """A2 结构保证：备份走**独立连接**，而不是调用方那条（可能持有事务的）连接。

    判据：调用方连接上挂一个"一旦被用来 backup 就报错"的守卫。
    由于 sqlite3.Connection.backup 是 C 层方法不可 monkeypatch，
    这里改为断言 ``backup_before_migrate`` 在**调用方已开事务**时仍能成功完成，
    且备份内容只含**已提交**数据（独立连接读不到对方未提交的写入）。
    """
    from lottery import migrations

    path = tmp_path / "iso.db"
    conn = _seed_data(sqlite3.connect(path))
    conn.row_factory = sqlite3.Row
    migrations.apply(conn)

    # 调用方开事务并写入未提交数据
    conn.execute("BEGIN")
    conn.execute("INSERT INTO draws VALUES ('UNCOMMITTED','x',1,2)")

    dest = migrations.backup_before_migrate(conn, migrations.current_version(conn))
    assert dest is not None and dest.exists()

    bconn = sqlite3.connect(dest)
    try:
        rows = [r[0] for r in bconn.execute("SELECT issue FROM draws")]
    finally:
        bconn.close()
    conn.rollback()

    assert "UNCOMMITTED" not in rows, \
        "备份走了独立连接，不应包含调用方未提交的数据"
    assert rows == ["2003001"]
    conn.close()
