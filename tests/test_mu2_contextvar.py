"""MU-2：contextvars 必须穿过 ThreadPoolExecutor（用量记账的前提）。

## 这条测试保的是一类**静默失效**

`ThreadPoolExecutor` 提交的可调用对象**不继承** contextvars。而 `llm_client`
是按 contextvar 判断"这次调用算谁的"（Lead 裁决 9：contextvar 只留给无法改签名
的深层库）。

于是 `engine.llm_tickets()` 里并发发起 LLM 调用时，工作线程拿到的
`current_user_id()` 恒为 None → 记账落空 → **配额永远显示 0，且不报任何错**。

这不是理论问题，是本轮实测出来的：真实 LLM 出号成功（`llm_used=true`、
拿到 5 注），但 `today_tokens_used` 仍是 0。失败方向是"用户以为有限额、
其实什么都没算"——只能在某天发现额度早被用光却没人拦。

修法是标准做法：提交处 `copy_context()`，线程内 `.copy().run(fn)`。
"""
from __future__ import annotations

import contextvars

import pytest

from lottery import config, db


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LOTT_HOME", str(tmp_path))
    monkeypatch.setenv("LOTT_DB", str(tmp_path / "t.db"))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config, "LLM_CONFIG_FILE", config.DATA_DIR / "llm_config.json")
    db.close()
    db.get_conn()
    yield tmp_path
    db.close()


def test_contextvar_does_not_cross_threadpool_by_default(env):
    """**反向用例**：先证明不加处理时确实是丢的。

    没有这条，修好之后没人知道原来的坑长什么样；也没法判断
    「是不是本来就传得过去、我做了无用功」。
    """
    from concurrent.futures import ThreadPoolExecutor

    cv: contextvars.ContextVar = contextvars.ContextVar("t", default=None)
    cv.set(42)

    with ThreadPoolExecutor(max_workers=1) as ex:
        got = ex.submit(lambda: cv.get()).result()

    assert got is None, (
        "ThreadPoolExecutor 竟然继承了 contextvar —— 那说明 Python 行为变了，"
        "`engine.llm_tickets()` 里的 copy_context 可以简化，但请先确认版本差异")


def test_copy_context_does_cross_threadpool(env):
    """修法本身有效：copy_context().run() 能把值带进工作线程。"""
    from concurrent.futures import ThreadPoolExecutor

    cv: contextvars.ContextVar = contextvars.ContextVar("t2", default=None)
    cv.set(7)
    parent = contextvars.copy_context()

    with ThreadPoolExecutor(max_workers=2) as ex:
        got = [ex.submit(lambda: parent.copy().run(cv.get)).result() for _ in range(3)]

    assert got == [7, 7, 7]


def test_shared_context_object_cannot_be_entered_concurrently(env):
    """**为什么必须 `.copy()` 而不能直接共用同一个 Context**。

    把同一个 Context 交给多个线程会抛 RuntimeError（不允许并发进入）。
    这是修法里最容易写错的一步，用测试固定住。
    """
    from concurrent.futures import ThreadPoolExecutor

    cv: contextvars.ContextVar = contextvars.ContextVar("t3", default=None)
    cv.set(1)
    shared = contextvars.copy_context()

    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = [ex.submit(shared.run, cv.get) for _ in range(4)]
        results = []
        for f in futs:
            try:
                results.append(f.result())
            except RuntimeError as e:
                results.append(("RuntimeError", str(e)[:40]))
    # 允许全部成功（若执行器串行化）或部分报错，但**不能**静默给出错误的值
    assert all(r == 1 or (isinstance(r, tuple) and r[0] == "RuntimeError")
               for r in results), results


def test_engine_source_uses_copy_context_in_threadpool(env):
    """**源码级哨兵**：`llm_tickets()` 提交线程时必须携带上下文。

    为什么不端到端驱动 `llm_tickets()`：它依赖 stats/patterns 等一整套输入，
    为了测"上下文是否穿过线程池"去构造那些输入，只会让用例又脆又慢，
    而且它一旦在某层提前 return，用例就变成"没走到并发路径"的假绿
    （本文件第一版正是这样失败的）。

    真正要钉死的是一条**结构纪律**：只要这里用 ThreadPoolExecutor 提交
    LLM 调用，就必须有 `copy_context()`；否则用量记账静默落空。
    """
    from pathlib import Path
    src = Path(__file__).resolve().parent.parent / "lottery" / "engine.py"
    text = src.read_text(encoding="utf-8")
    seg_start = text.index("with ThreadPoolExecutor(max_workers=min(len(calls), 4)) as ex:")
    # 往前看 30 行内必须出现 copy_context
    head = text[max(0, seg_start - 1500):seg_start]
    assert "copy_context" in head, (
        "llm_tickets 在 ThreadPoolExecutor 提交前没有 copy_context —— "
        "工作线程拿不到 current_user_id()，用量记账会静默落空")
    assert "_parent_ctx.copy().run(" in text, "必须对每个任务 .copy() 后再 run（Context 不能并发进入）"


def _peek_uid():
    from lottery import api_app
    return api_app.current_user_id()
