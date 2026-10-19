"""V13 P1a：A 档端点删除的守护测试（M4_SIMPLIFY §5）。

## 为什么要专门写这个文件

删掉 7 个端点之后，**最大的风险不是删错，而是有人（未来的我）无声地加回来**：
只要没人调用，加回来也不会有任何测试变红，于是「精简」在三个月后自动回滚。

因此这里做两件事：
1. **哨兵**：这些路径必须保持不存在（加回来就红，并指向评审依据）；
2. **保留面**：被删端点的**替代品**必须还在——否则"删掉了但没替代"就是功能损失。

## 删除依据（逐条）

| # | 端点 | 依据 |
|---|---|---|
| A1 | `/api/draws/latest` | 与 `/api/draws/history` 完全重叠，仅少一个 date 字段 |
| A2 | `/api/notify/status` | 纯 introspection，`notify` 模块本身仍被调度器使用 |
| A3 | `/api/eval/meta` | 第三层包装，内部两处已直接调 `db.load_eval_meta()` |
| A4 | `/api/mining/latest` | `mining_runs` 表 0 行、数据文件 14 天未更新 |
| A5 | `/api/llm_lab/run`（同步） | 前端只调 async 版；同步版让浏览器干等 1~8 分钟 |
| A6 | `/api/rules/closed-loop`（同步） | 同上，耗时更长 |
| A7 | `/api/eval/coverage` | 一次性蒙特卡洛对照实验，已被 l2_service 精确全枚举取代 |
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def _all_paths():
    from lottery import api_app
    out = set()
    for r in api_app.app.routes:
        p = getattr(r, "path", None)
        if p:
            out.add(p)
        ex = getattr(r, "effective_candidates", None)
        if callable(ex):
            for c in ex() or []:
                cp = getattr(c, "path", None)
                if cp:
                    out.add(cp)
    return out


REMOVED = [
    "/api/draws/latest",
    "/api/notify/status",
    "/api/eval/meta",
    "/api/mining/latest",
    "/api/llm_lab/run",
    "/api/rules/closed-loop",
    "/api/eval/coverage",
]

KEPT = [
    # 被删端点的替代品
    "/api/draws/history",          # 替代 draws/latest
    "/api/eval/cumulative",        # 替代 eval/meta 的读路径
    "/api/llm_lab/run/async",      # 替代 llm_lab/run
    "/api/rules/closed-loop/async",  # 替代 rules/closed-loop
    "/api/l2/distribution",        # 替代 eval/coverage（精确全枚举）
    "/api/portfolio/marginal",     # 同上
]


def test_removed_endpoints_stay_removed():
    paths = _all_paths()
    still = [p for p in REMOVED if p in paths]
    assert not still, (
        f"这些端点已按 V13 P1a 删除，却又出现在路由表里：{still}\n"
        "若确实要恢复，请先回应 docs/reviews/M4_SIMPLIFY.md §5 的删除依据")


def test_replacements_still_exist():
    """删掉的必须有替代品，否则不是「精简」而是「功能损失」。"""
    paths = _all_paths()
    missing = [p for p in KEPT if p not in paths]
    assert not missing, f"替代端点缺失：{missing}"


def test_no_frontend_call_to_removed_endpoints():
    """前端不得再引用被删端点（否则用户会看到 404）。"""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "web"
    blob = (root / "static" / "app.js").read_text(encoding="utf-8") + \
           (root / "index.html").read_text(encoding="utf-8")
    for p in REMOVED:
        # 排除 /async 后缀造成的误报
        assert '"' + p + '"' not in blob and "'" + p + "'" not in blob, \
            f"前端仍在调用已删除端点 {p}"


def test_bg_run_preserved(monkeypatch, tmp_path):
    """**回归**：删除 closed-loop 同步版时曾误删 `_bg_run`。

    它是 async 版任务的基础设施（也是 MU-2 contextvar 修复的落点），
    与"同步版端点"完全无关，不该被同一次删除波及。
    """
    from lottery import api_app, config, db
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    db.close()
    assert hasattr(api_app, "_bg_run"), "_bg_run 不见了 —— async 任务全部会坏"
    tid = api_app._bg_run("t", lambda p: (p(0.5, "半程"), {"note_zh": "done"})[1])
    assert tid.startswith("t_")
    # **必须等线程跑完再离开用例**：`_bg_run` 起的是 daemon 线程，
    # 若它在 pytest 解释器关闭时仍在调用 sqlite/scipy，会触发段错误把整个
    # 测试会话打挂（本文件第一版就崩了，退出码 139、149 个 C 扩展栈）。
    # 既有 test_v8_eras_tasks 的做法就是轮询 tasks 表直到终态 —— 这里照做。
    import time
    for _ in range(100):
        t = db.load_task(tid)
        if t and t["status"] in ("completed", "failed"):
            break
        time.sleep(0.05)
    assert db.load_task(tid)["status"] == "completed"


def test_user_llm_api_router_survives_pruning():
    """MU-2 新增的端点不能被"精简"误伤。"""
    paths = _all_paths()
    assert "/api/user/llm/config" in paths
    assert "/api/admin/llm/default" in paths
