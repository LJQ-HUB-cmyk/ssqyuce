"""W01 部署前置加固的回归测试（多用户改造 P0）。

覆盖三项"上线前必须堵死"的暴露面 / 稳定性问题：

1. **文档端点默认关闭**：`/docs` `/redoc` `/openapi.json` **不在 `/api/` 前缀下**，
   天然绕过 `optional_api_auth` 中间件。生产实测曾返回 200，等于把全部路由、
   参数 schema 对外公开。本文件锁死"默认 404"。
2. **CORS 收敛到白名单**：原先 `allow_origins=["*"]`，任何第三方页面都能跨域读取。
   前端用相对路径 fetch 属同源，本不依赖 CORS。
3. **SQLite 并发加固**：`journal_mode=WAL` + `busy_timeout` + `synchronous=NORMAL`。
   全站 79 个同步端点共享一条全局连接，缺 WAL 时一次长写事务会让读请求阻塞并
   抛 `database is locked`。

数值口径（改动时必须显式更新本文件并说明理由）：
  * `BUSY_TIMEOUT_MS = 5000`
  * `JOURNAL_MODE = "wal"`
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

BUSY_TIMEOUT_MS = 5000
JOURNAL_MODE = "wal"


# --------------------------------------------------------------------------
# ① 文档端点默认关闭
# --------------------------------------------------------------------------

@pytest.fixture()
def client(isolated_env):
    """复用 conftest 的隔离环境（`_auto_isolate_lottery_paths` 已 autouse）。"""
    from lottery import api_app, db
    db.close()
    with TestClient(api_app.app) as c:
        yield c
    db.close()


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_docs_endpoints_disabled_by_default(client, path):
    """默认配置下三条文档路径必须是 404（不是 200、也不是 401）。"""
    r = client.get(path)
    assert r.status_code == 404, (
        f"{path} 返回 {r.status_code} —— 文档端点默认必须关闭。"
        "它们不在 /api/ 前缀下，任何加在 /api/* 的鉴权都保护不到。"
        "本地开发请显式设 LOTT_API_DOCS=1。"
    )


def test_docs_flag_reflects_config(isolated_env):
    """`config.API_DOCS_ENABLED` 必须由 `LOTT_API_DOCS` 驱动，默认 False。"""
    from lottery import config
    assert hasattr(config, "API_DOCS_ENABLED")
    assert config.API_DOCS_ENABLED is False, (
        "默认必须关闭；只有显式 LOTT_API_DOCS=1 才打开")


# --------------------------------------------------------------------------
# ② CORS 白名单
# --------------------------------------------------------------------------

def test_cors_allows_configured_origin(client):
    """白名单内的来源应拿到 `access-control-allow-origin`。"""
    from lottery import config
    allowed = config.CORS_ORIGINS[0]
    r = client.get("/api/health", headers={"Origin": allowed})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == allowed


def test_cors_rejects_unknown_origin(client):
    """未在白名单内的来源不得拿到放行头（且绝不能回显 `*`）。"""
    r = client.get("/api/health", headers={"Origin": "https://evil.example"})
    # 请求本身可以成功（同源策略由浏览器执行），但**不得**返回放行头
    acao = r.headers.get("access-control-allow-origin")
    assert acao != "*", "CORS 不得回落到通配符 *"
    assert acao != "https://evil.example", (
        "未授权的 Origin 被放行了 —— CORS 白名单失效")


def test_cors_origins_never_wildcard(isolated_env):
    """配置层面就禁止 `*`。"""
    from lottery import config
    assert "*" not in config.CORS_ORIGINS


# --------------------------------------------------------------------------
# ③ SQLite 并发加固
# --------------------------------------------------------------------------

def test_sqlite_pragmas_applied(isolated_env):
    """新连接必须带 WAL + busy_timeout + synchronous=NORMAL。"""
    from lottery import db
    db.close()
    conn = db.get_conn()
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    sync = conn.execute("PRAGMA synchronous").fetchone()[0]

    assert str(mode).lower() == JOURNAL_MODE, (
        f"journal_mode={mode}，期望 {JOURNAL_MODE}。缺 WAL 时读会被写事务阻塞")
    assert int(busy) == BUSY_TIMEOUT_MS, (
        f"busy_timeout={busy}，期望 {BUSY_TIMEOUT_MS}。缺它时撞锁立刻抛 database is locked")
    # synchronous: 0=OFF 1=NORMAL 2=FULL；WAL 下推荐 NORMAL
    assert int(sync) == 1, f"synchronous={sync}，WAL 下期望 NORMAL(1)"


def test_concurrent_write_waits_instead_of_failing_fast(isolated_env):
    """短时持锁的写事务：第二个写者应**等待**而不是立刻报 locked。

    这条是 W01 的核心价值验证（多用户后写并发只会更高）。

    测试设计要点（第一版写错过，记在这里防止再犯）：
    * 不能单线程"c1 BEGIN → c2 BEGIN"，因为 c2 会一直等到 busy_timeout 超时才报错
      ——那测的是"超时生效"，不是"等待后成功"。
    * 正确做法：让 c1 在**另一个线程**里持锁 0.3s 后提交，c2 立即尝试写。
      若 busy_timeout 生效，c2 会等待约 0.3s 后成功；若未生效，c2 会**立刻**抛
      `database is locked`。
    """
    import sqlite3
    import threading
    import time

    from lottery import config, db
    db.close()
    db.get_conn()  # 建表并应用 pragma

    db_path = str(config.DB_PATH)
    # 注意：sqlite3 连接默认 check_same_thread=True，不能跨线程使用，
    # 所以慢写者的连接必须在它自己的线程里创建。
    c2 = sqlite3.connect(db_path, timeout=5.0)
    c2.execute("PRAGMA busy_timeout=5000")

    hold_seconds = 0.3
    done = threading.Event()
    errors: list = []

    def _slow_writer():
        try:
            c1 = sqlite3.connect(db_path, timeout=5.0)
            c1.execute("PRAGMA busy_timeout=5000")
            c1.execute("BEGIN IMMEDIATE")
            c1.execute("INSERT INTO tasks(id, kind) VALUES('w01_a','test')")
            done.set()
            time.sleep(hold_seconds)
            c1.commit()
            c1.close()
        except Exception as e:  # noqa: BLE001
            errors.append(e)
            done.set()

    t = threading.Thread(target=_slow_writer)
    try:
        t.start()
        assert done.wait(timeout=5.0)   # 确保 c1 已持锁
        assert not errors, f"慢写者自身失败：{errors}"
        started = time.time()
        c2.execute("BEGIN IMMEDIATE")   # 应等待 c1 提交，而不是立即失败
        waited = time.time() - started
        c2.execute("INSERT INTO tasks(id, kind) VALUES('w01_b','test')")
        c2.commit()
        t.join(timeout=5.0)

        assert waited >= hold_seconds * 0.5, (
            f"c2 只等了 {waited:.3f}s 就拿到锁 —— 说明 c1 已提前释放，"
            "本用例没有真正验证等待行为")
        assert waited < 5.0, (
            f"c2 等了 {waited:.3f}s（接近 busy_timeout 上限）—— 异常")
    finally:
        t.join(timeout=5.0)
        c2.close()

    from lottery import db as _db
    _db.close()
    rows = {r["id"] for r in _db.get_conn().execute(
        "SELECT id FROM tasks WHERE id IN ('w01_a','w01_b')")}
    assert rows == {"w01_a", "w01_b"}, "两个写事务的结果都必须落库"


def test_write_lock_fails_fast_without_busy_timeout(isolated_env):
    """反向用例：**没有** busy_timeout 时必须立刻失败（证明上一条测的是真效果）。

    如果不加这条，上一条的"等待成功"可能只是碰巧，无法证明 busy_timeout 起了作用。
    """
    import sqlite3

    from lottery import config, db
    db.close()
    db.get_conn()

    c1 = sqlite3.connect(str(config.DB_PATH), timeout=0.0)
    c2 = sqlite3.connect(str(config.DB_PATH), timeout=0.0)
    c1.execute("PRAGMA busy_timeout=0")
    c2.execute("PRAGMA busy_timeout=0")
    try:
        c1.execute("BEGIN IMMEDIATE")
        c1.execute("INSERT INTO tasks(id, kind) VALUES('w01_c','test')")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            c2.execute("BEGIN IMMEDIATE")
        c1.commit()
    finally:
        c1.close()
        c2.close()
        db.close()
