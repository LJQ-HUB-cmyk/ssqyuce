"""SQLite schema migrations for M4.5（V10-00d 加固：批次事务 + 迁移前自动备份）.

Migrations are ordered, idempotent, and recorded in schema_version. Existing
installations are safe because each migration checks its target objects.

V10-00d 加固点（对应 ``docs/reviews/R4_RELEASE_REVIEW.md`` P0-C 第 1 条
「迁移是懒加载自动执行的，无回滚路径」）：

1. **批次事务**：所有待执行迁移在**同一个事务**里跑完；任一迁移抛异常 →
   整体 ``ROLLBACK``，``schema_version`` 一行都不会前进（不会留下"跑到一半"的
   半迁移状态），异常原样抛出。
   * 注意 SQLite/Python 的坑：``sqlite3`` 的 LEGACY 隔离级别（默认
     ``isolation_level=""``）对 **DDL 不开启隐式事务**，``CREATE TABLE`` 会立即
     自带提交，事后 ``ROLLBACK`` 撤不回来。因此这里显式在 ``isolation_level=None``
     （autocommit）下 ``BEGIN IMMEDIATE``，保证 DDL 也在同一事务内。
   * 若调用方**已有未提交事务**，改用 ``SAVEPOINT`` 嵌套：失败只回滚到迁移开始处，
     **绝不提交**调用方未完成的工作。
2. **迁移前自动备份**：真正有待执行迁移时，先用 ``sqlite3.Connection.backup()``
   （一致性热备，不是 ``cp``）备份到**与被迁移库同目录**的
   ``<db 文件名>.pre-migrate-v<当前版本>``（例如
   ``data/ssq.db.pre-migrate-v2``）。备份失败 → **中止迁移**（fail-closed）。
   全新库（``schema_version`` 还是 0、没有既有数据）不做无意义备份。
3. ``status()`` 语义不变，供 ``/api/health`` 暴露 ``schema_version``。
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple


def _ensure_reconcile_runs(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS reconcile_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        checked_at REAL NOT NULL,
        secondary_url TEXT DEFAULT '',
        status TEXT NOT NULL,
        summary_json TEXT NOT NULL DEFAULT '{}'
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reconcile_runs_checked_at ON reconcile_runs(checked_at)")


def _ensure_legacy_columns(conn: sqlite3.Connection) -> None:
    for table, column, definition in (
        ("predictions", "evidence_json", "TEXT DEFAULT ''"),
        ("draws", "source", "TEXT NOT NULL DEFAULT '17500'"),
    ):
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not columns:
            continue
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


MIGRATIONS: List[Tuple[int, str, Callable[[sqlite3.Connection], None]]] = [
    (1, "reconcile audit table", _ensure_reconcile_runs),
    (2, "legacy provenance columns", _ensure_legacy_columns),
]

_SCHEMA_VERSION_DDL = """CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at REAL NOT NULL
)"""


# ---------- 版本与备份 ----------

def current_version(conn: sqlite3.Connection) -> int:
    """读取当前 schema_version（表不存在视为 0）。"""
    try:
        row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()
    except sqlite3.OperationalError:      # 表还不存在
        return 0
    if row is None:
        return 0
    # 注意：列名是 COALESCE(MAX(version),0)，用下标取更稳（row_factory 可能是 Row 或 tuple）
    return int(row[0])


def latest_version() -> int:
    return MIGRATIONS[-1][0] if MIGRATIONS else 0


def db_file_path(conn: sqlite3.Connection) -> Optional[Path]:
    """连接对应的主库文件路径；``:memory:`` / 临时库返回 None。"""
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
    except sqlite3.Error:
        return None
    for row in rows:
        try:
            name, path = row[1], row[2]
        except (IndexError, KeyError):
            continue
        if name == "main" and path:
            return Path(path)
    return None


def backup_path_for(conn: sqlite3.Connection, version: int) -> Optional[Path]:
    """迁移前备份的目标路径：``<库文件>.pre-migrate-v<version>``（与库同目录）。"""
    path = db_file_path(conn)
    if path is None:
        return None
    return path.with_name(f"{path.name}.pre-migrate-v{int(version)}")


def backup_before_migrate(conn: sqlite3.Connection, version: int) -> Optional[Path]:
    """迁移前一致性热备（``Connection.backup``），返回备份文件路径。

    先写 ``.tmp`` 再 ``os.replace``，避免备份过程中留下的半截文件被误当作备份。
    """
    dest = backup_path_for(conn, version)
    if dest is None:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    for stale in (tmp,):
        if stale.exists():
            stale.unlink()
    target = sqlite3.connect(str(tmp))
    try:
        conn.backup(target)
    finally:
        target.close()
    os.replace(str(tmp), str(dest))
    return dest


# ---------- 批次事务执行 ----------

def _execute_pending(conn: sqlite3.Connection,
                     pending: Sequence[Tuple[int, str, Callable[[sqlite3.Connection], None]]]) -> None:
    conn.execute(_SCHEMA_VERSION_DDL)
    for version, name, migration in pending:
        migration(conn)
        conn.execute("INSERT INTO schema_version(version, name, applied_at) VALUES (?,?,?)",
                     (version, name, time.time()))


def _run_batch(conn: sqlite3.Connection,
               pending: Sequence[Tuple[int, str, Callable[[sqlite3.Connection], None]]]) -> None:
    """在**一个事务**里执行全部待执行迁移；任一失败 → 整体回滚。"""
    if conn.in_transaction:
        # 调用方已有未提交事务：用 SAVEPOINT 嵌套，失败只回滚到迁移开始处
        conn.execute("SAVEPOINT ssq_migrations")
        try:
            _execute_pending(conn, pending)
        except BaseException:
            conn.execute("ROLLBACK TO ssq_migrations")
            conn.execute("RELEASE ssq_migrations")
            raise
        conn.execute("RELEASE ssq_migrations")
        return

    original_isolation = conn.isolation_level
    try:
        # autocommit + 显式 BEGIN：LEGACY 模式下 DDL 不会被包进事务，必须显式开
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            _execute_pending(conn, pending)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        try:
            conn.isolation_level = original_isolation
        except sqlite3.Error:      # pragma: no cover - 仅在连接已关闭等极端情况
            pass


def apply(conn: sqlite3.Connection, *, backup: bool = True) -> int:
    """执行全部待执行迁移，返回执行后的 schema_version。

    * ``backup=True``（默认）：有待执行迁移且当前版本 > 0 时，先做一致性热备
      （见 :func:`backup_before_migrate`）；备份失败则**不迁移**。
    * 幂等：已是最新版本时直接返回，不备份、不写库。
    * 失败：整体回滚（含 DDL），``schema_version`` 不前进，异常原样抛出。
    """
    current = current_version(conn)
    pending = [(v, n, f) for v, n, f in MIGRATIONS if v > current]
    if not pending:
        return int(current)

    if backup and current > 0:
        # fail-closed：备份不成功就不迁移，避免"迁移后才发现无路可退"
        backup_before_migrate(conn, current)

    _run_batch(conn, pending)
    return current_version(conn)


def status(conn: sqlite3.Connection) -> dict:
    return {"version": current_version(conn), "latest": latest_version()}
