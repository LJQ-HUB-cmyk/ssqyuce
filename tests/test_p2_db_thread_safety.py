"""P2 稳定性：SQLite 连接必须线程安全（真实生产故障的回归）。

## 故障现场

2026-10-20 生产 `/api/auth/login` 持续 500：

```
sqlite3.OperationalError: database is locked
```

容器重启即恢复，但会复发，且没有任何请求量异常。

## 根因

历史实现是 **一条连接 + `check_same_thread=False` + 40 线程池，且无锁**：

```python
_CONN = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=5.0)
```

`check_same_thread=False` 只关掉了 Python 的跨线程**检查**，并没有让连接变
线程安全。多线程同时发语句时：

* 事务边界互相串（A 的 commit 提交了 B 未完成的写）；
* 一个线程开着写事务没提交、另一个线程来写 → **自己等自己的锁** →
  `database is locked`。

第二种是 `busy_timeout` **救不了**的：它等的是同进程内另一条语句持有的锁，
不会随时间释放。这就是为什么"设了 5 秒超时仍然报锁"。

## 修法

每线程一条连接（SQLite 官方推荐）。`get_conn()` 签名不变 → 200+ 调用点零改动。
跨连接写冲突由 SQLite 文件锁 + busy_timeout 正常串行化（这与自锁本质不同）。
"""
from __future__ import annotations

import threading

import pytest

from lottery import config, db


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    db.close()
    db.get_conn()
    yield
    db.close()


def test_concurrent_writes_do_not_self_lock(fresh_db):
    """**核心回归**：8 线程并发写不得出现 `database is locked`。

    旧的单连接实现下这条会失败（自己等自己的锁）。
    """
    errors = []

    def worker(n: int):
        try:
            for i in range(25):
                conn = db.get_conn()
                conn.execute(
                    "INSERT OR REPLACE INTO draws"
                    "(issue,date,r1,r2,r3,r4,r5,r6,blue) VALUES (?,?,?,?,?,?,?,?,?)",
                    (f"2026{n:02d}{i:03d}", "2026-01-01", 1, 2, 3, 4, 5, 6, (i % 16) + 1))
                conn.commit()
        except Exception as e:      # noqa: BLE001
            errors.append(f"worker{n}: {type(e).__name__}: {e}")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发写出现错误（连接不是线程安全的）：{errors[:3]}"
    total = db.get_conn().execute("SELECT COUNT(*) FROM draws").fetchone()[0]
    assert total == 8 * 25, f"数据不完整：{total} != 200"


def test_each_thread_gets_its_own_connection(fresh_db):
    """不同线程拿到的必须是**不同**的连接对象。"""
    seen = {}

    def grab(n: int):
        seen[n] = id(db.get_conn())

    threads = [threading.Thread(target=grab, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(set(seen.values())) == 4, f"多个线程共用同一连接：{seen}"


def test_same_thread_reuses_connection(fresh_db):
    """同一线程内必须复用同一个连接（否则每次调用都新建，WAL 会爆）。"""
    assert id(db.get_conn()) == id(db.get_conn())


def test_concurrent_read_and_write(fresh_db):
    """读写并发：读不阻塞写、写不阻塞读（WAL 的收益，必须真的成立）。"""
    errors = []
    stop = threading.Event()

    def reader():
        try:
            while not stop.is_set():
                db.get_conn().execute("SELECT COUNT(*) FROM draws").fetchone()
        except Exception as e:      # noqa: BLE001
            errors.append(f"reader: {type(e).__name__}: {e}")

    def writer():
        try:
            for i in range(40):
                conn = db.get_conn()
                conn.execute(
                    "INSERT OR REPLACE INTO draws"
                    "(issue,date,r1,r2,r3,r4,r5,r6,blue) VALUES (?,?,?,?,?,?,?,?,?)",
                    (f"2027{i:04d}", "2027-01-01", 1, 2, 3, 4, 5, 6, 1))
                conn.commit()
        except Exception as e:      # noqa: BLE001
            errors.append(f"writer: {type(e).__name__}: {e}")
        finally:
            stop.set()

    rs = [threading.Thread(target=reader) for _ in range(3)]
    w = threading.Thread(target=writer)
    for t in rs:
        t.start()
    w.start()
    w.join()
    for t in rs:
        t.join(2)

    assert not errors, f"读写并发出错：{errors[:3]}"


def test_close_is_per_thread(fresh_db):
    """`close()` 只关当前线程的连接，不影响其它线程。

    这是多线程语义最容易写错的一处：若 `close()` 关掉全局连接，
    别的正在处理的请求会在下一次操作时炸掉 —— 而且是**间歇性**的，
    因为取决于关闭时那个线程在做什么。
    """
    result = {}
    got_conn = threading.Event()
    proceed = threading.Event()

    def hold():
        conn = db.get_conn()
        got_conn.set()
        proceed.wait(timeout=3)
        try:
            conn.execute("SELECT 1").fetchone()
            result["ok"] = True
        except Exception as e:      # noqa: BLE001
            result["ok"] = False
            result["err"] = f"{type(e).__name__}: {e}"

    t = threading.Thread(target=hold)
    t.start()
    assert got_conn.wait(timeout=3), "工作线程没能拿到连接"

    db.get_conn()      # 主线程也拿一个
    db.close()         # 只应关主线程的
    proceed.set()
    t.join(timeout=5)

    assert result.get("ok") is True, (
        f"close() 关掉了别的线程的连接，那个线程的操作会炸：{result}")
