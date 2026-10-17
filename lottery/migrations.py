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

# 迁移前热备的兜底参数（V10 二次评审 A2）：只读连接 + 有限超时 + 分页拷贝，
# 避免在库被占用时无限重试导致进程挂死。
_BACKUP_TIMEOUT = 10.0          # 秒；sqlite3.connect(timeout=...)
_BACKUP_PAGE_CHUNK = 256        # 每批拷贝页数，减小长时间持锁窗口


def _log(msg: str) -> None:
    """迁移日志（与其他模块 ``[xxx]`` 前缀口径一致）。"""
    print(f"[migrations] {msg}")


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


def has_existing_data(conn: sqlite3.Connection) -> bool:
    """库内是否已有用户数据（而非刚创建的空库）。

    V10 二次评审 A1 修复背景
    ------------------------
    旧实现用 ``current > 0`` 作为"是否需要备份"的判据，但**没有 ``schema_version``
    表的老库** ``current_version()`` 返回 **0**，于是"最需要备份的老库升级"反而
    **一行备份都不做**——正好击中"生产库零备份"这个立项理由。

    改为按"有没有既有数据"判断：
      * 空库（刚 created、无任何用户表数据）→ 不备份（没有可丢的东西）；
      * **有数据的库（无论有没有 schema_version 表）→ 必须备份**。
    """
    # 除迁移自身维护的 schema_version 外，是否还有其它含数据的表
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for name in names:
        if name == "schema_version":
            continue
        try:
            row = conn.execute(f'SELECT 1 FROM "{name}" LIMIT 1').fetchone()
        except sqlite3.Error:
            continue
        if row is not None:
            return True
    return False


def backup_before_migrate(conn: sqlite3.Connection, version: int) -> Optional[Path]:
    """迁移前一致性热备（``Connection.backup``），返回备份文件路径。

    先写 ``.tmp`` 再 ``os.replace``，避免备份过程中留下的半截文件被误当作备份。

    V10 二次评审 A2 修复背景
    ------------------------
    旧实现直接用**调用方的** ``conn`` 做 ``Connection.backup()``。当调用方已有未提交
    事务时，``backup()`` 遇 BUSY 会无限重试 → 进程挂死，且让 SAVEPOINT 嵌套分支
    永远不可达（变异删掉该分支测试仍全绿 = 零覆盖）。

    改为：**另开一个只读连接**做热备，与调用方事务完全隔离：
      * 不参与调用方事务，因此不会触发 BUSY 自杀锁；
      * 也不会把调用方未提交的中间态写进备份。
    并对 ``backup()`` 设置页数上限与超时兜底，仍失败则抛错（fail-closed）。
    """
    dest = backup_path_for(conn, version)
    if dest is None:
        return None
    path = db_file_path(conn)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    if tmp.exists():
        tmp.unlink()

    # 独立只读连接：与调用方事务解耦，避免 BUSY 无限重试
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=_BACKUP_TIMEOUT)
    try:
        target = sqlite3.connect(str(tmp), timeout=_BACKUP_TIMEOUT)
        try:
            src.backup(target, pages=_BACKUP_PAGE_CHUNK)
        finally:
            target.close()
    finally:
        src.close()
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

    * ``backup=True``（默认）：有待执行迁移且**库内已有数据**时，先做一致性热备
      （见 :func:`backup_before_migrate`）；备份失败则**不迁移**（fail-closed）。
      **注意**：判据是"有没有既有数据"，不是"schema_version > 0"。老库（无
      ``schema_version`` 表、但含真实开奖数据）升级时 ``current == 0``，旧实现
      因此跳过备份——这是 V10 二次评审 A1 修复的真实缺陷。
    * 幂等：已是最新版本时直接返回，不备份、不写库。
    * 失败：整体回滚（含 DDL），``schema_version`` 不前进，异常原样抛出。
    """
    current = current_version(conn)
    pending = [(v, n, f) for v, n, f in MIGRATIONS if v > current]
    if not pending:
        return int(current)

    if backup:
        # A1：老库（current==0）同样必须备份——判据改为"库内是否有既有数据"
        if has_existing_data(conn):
            # fail-closed：备份不成功就不迁移，避免"迁移后才发现无路可退"
            backup_before_migrate(conn, current)
        else:
            _log("库内无既有数据，跳迁移前备份（空库无可丢内容）")

    _run_batch(conn, pending)
    return current_version(conn)


def status(conn: sqlite3.Connection) -> dict:
    return {"version": current_version(conn), "latest": latest_version()}
