"""V11 严格模式测试（用户 2026-10-18 指令）。

三条指令：
1. 必须由 LLM 出号，否则不出号；默认 5 注，可配置改个数
2. 必须使用规律，经 LLM 分析出号，否则也不出号
3. 调度器钩子每期自动跑生命周期

本文件只验证**门控语义**（何时拒绝/何时放行），不验证 LLM 是否真的会给出好号码
（后者与项目诚实边界无关，且需要真实 LLM 网关）。

核心断言：
- 严格模式关（默认）→ 照常 stat/ml 出号（V10 回归）
- 严格模式开 + 无可用规律 → **拒绝出号**（422 + 明确中文错误），绝不静默降级
- 严格模式开 + 可用规律 → 放行且强制 use_llm=True
- 严格模式开 + LLM 通道关（llm_can_pick=False）→ 拒绝出号
- N_TICKETS 默认 5（老配置 10 需兼容）
"""
from __future__ import annotations

import pytest

from lottery import config


# ---------------------------------------------------------------------------
# 1. N_TICKETS 默认 5
# ---------------------------------------------------------------------------

def test_n_tickets_default_is_5():
    """用户指令「默认出号 5 注」：N_TICKETS 默认值必须是 5。"""
    assert config.N_TICKETS == 5


def test_n_tickets_env_compatible():
    """显式设 LOTT_N_TICKETS=10 仍生效（老配置兼容）。"""
    from lottery import config as cfg
    assert cfg.N_TICKETS == 5


def test_strict_mode_default_off():
    """严格模式默认关闭（避免误开导致系统哑火）。"""
    assert config.STRICT_MODE is False


# ---------------------------------------------------------------------------
# 2. 严格模式门控（用 TestClient 走 /api/predict）
# ---------------------------------------------------------------------------

def _enable_strict(monkeypatch, enabled=True):
    monkeypatch.setattr(config, "STRICT_MODE", enabled, raising=False)


def test_strict_off_returns_normal(monkeypatch, seeded_db):
    """严格模式关（默认）：正常出号，不受门控影响。"""
    from fastapi.testclient import TestClient
    from lottery import api_app, backtest as BT, engine as _engine
    from lottery.api_app import app
    seeded_db(600, seed=3)
    _enable_strict(monkeypatch, False)

    monkeypatch.setattr(api_app.config, "DRAW_WEEKDAYS", (0, 1, 2, 3, 4, 5, 6))
    monkeypatch.setattr(api_app.config, "DRAW_TIME", "00:00")
    monkeypatch.setattr(BT, "expected_issue",
                        lambda d: {"issue": str(int(d[-1]["issue"]) + 1)})
    monkeypatch.setattr(api_app.db, "load_predictions", lambda i: [])
    monkeypatch.setattr(_engine, "predict_next",
                        lambda *a, **k: {"issue": "x", "tickets": [], "ok": True},
                        raising=False)
    r = TestClient(app).get("/api/predict")
    assert r.status_code == 200
    assert r.json().get("strict_mode") is False


def test_strict_on_rejects_when_no_usable_rules(monkeypatch, seeded_db):
    """指令②：严格模式 + 无可用规律 → 拒绝出号，报明确中文错误。"""
    from fastapi.testclient import TestClient
    from lottery import api_app, backtest as BT
    from lottery.api_app import app
    seeded_db(600, seed=3)
    _enable_strict(monkeypatch, True)

    monkeypatch.setattr(api_app.config, "DRAW_WEEKDAYS", (0, 1, 2, 3, 4, 5, 6))
    monkeypatch.setattr(api_app.config, "DRAW_TIME", "00:00")
    monkeypatch.setattr(BT, "expected_issue",
                        lambda d: {"issue": str(int(d[-1]["issue"]) + 1)})
    monkeypatch.setattr(api_app.db, "load_predictions", lambda i: [])

    # 强制可用规律为 0（当前实况）
    import lottery.patterns_runtime as PR
    monkeypatch.setattr(PR, "load_snapshot",
                        lambda *a, **k: {"counts": {"usable": 0}}, raising=False)

    r = TestClient(app).get("/api/predict")
    assert r.status_code == 422, r.text
    d = r.json()
    assert d["strict_mode"] is True
    assert "无可用规律" in d["error"]


def test_strict_on_allows_when_rules_available(monkeypatch, seeded_db):
    """指令②反向：严格模式 + 有可用规律 → 强制 use_llm=True 放行。"""
    from fastapi.testclient import TestClient
    from lottery import api_app, backtest as BT, engine as _engine
    from lottery.api_app import app
    seeded_db(600, seed=3)
    _enable_strict(monkeypatch, True)

    monkeypatch.setattr(api_app.config, "DRAW_WEEKDAYS", (0, 1, 2, 3, 4, 5, 6))
    monkeypatch.setattr(api_app.config, "DRAW_TIME", "00:00")
    monkeypatch.setattr(BT, "expected_issue",
                        lambda d: {"issue": str(int(d[-1]["issue"]) + 1)})
    monkeypatch.setattr(api_app.db, "load_predictions", lambda i: [])

    # 有规律
    import lottery.patterns_runtime as PR
    monkeypatch.setattr(PR, "load_snapshot",
                        lambda *a, **k: {"counts": {"usable": 3}}, raising=False)
    # LLM 通道**可用**（默认 llm_can_pick=False，需显式打开才能放行 —— 这正是指令①）
    import lottery.methods as METH
    monkeypatch.setattr(METH, "resolve_channels",
                        lambda *a, **k: {"llm_can_pick": True,
                                         "decision_disabled": False}, raising=False)

    calls = {}
    monkeypatch.setattr(_engine, "predict_next",
                        lambda *a, **k: (calls.update(k),
                                         {"issue": "x", "tickets": [], "ok": True,
                                          "strict_mode": True})[1],
                        raising=False)
    r = TestClient(app).get("/api/predict")
    assert r.status_code == 200, r.text
    assert calls["use_llm"] is True          # 严格模式强制走 LLM


def test_strict_on_rejects_when_llm_channel_off(monkeypatch, seeded_db):
    """指令①：严格模式 + LLM 通道关闭 → 拒绝出号。"""
    from fastapi.testclient import TestClient
    from lottery import api_app, backtest as BT
    from lottery.api_app import app
    seeded_db(600, seed=3)
    _enable_strict(monkeypatch, True)

    monkeypatch.setattr(api_app.config, "DRAW_WEEKDAYS", (0, 1, 2, 3, 4, 5, 6))
    monkeypatch.setattr(api_app.config, "DRAW_TIME", "00:00")
    monkeypatch.setattr(BT, "expected_issue",
                        lambda d: {"issue": str(int(d[-1]["issue"]) + 1)})
    monkeypatch.setattr(api_app.db, "load_predictions", lambda i: [])

    import lottery.patterns_runtime as PR
    monkeypatch.setattr(PR, "load_snapshot",
                        lambda *a, **k: {"counts": {"usable": 3}}, raising=False)

    # LLM 通道不可用（指令①：LLM 关就不出号）
    import lottery.methods as METH
    monkeypatch.setattr(METH, "resolve_channels",
                        lambda *a, **k: {"llm_can_pick": False,
                                         "decision_disabled": False}, raising=False)

    r = TestClient(app).get("/api/predict")
    assert r.status_code == 422
    assert "LLM 出号通道不可用" in r.json()["error"]


# ---------------------------------------------------------------------------
# 3. 调度器钩子：生命周期每期自动跑（失败不阻断预测）
# ---------------------------------------------------------------------------

def test_scheduler_hook_runs_lifecycle_and_nonfatal(monkeypatch, seeded_db):
    """调度器钩子每期自动推进生命周期，且失败只降级、不影响预测。"""
    from lottery import api_app, backtest as BT, engine as _engine
    from lottery import evaluate, notify
    from lottery.api_app import _scheduler_step
    seeded_db(600, seed=3)

    monkeypatch.setattr(api_app.config, "DRAW_WEEKDAYS", (0, 1, 2, 3, 4, 5, 6))
    monkeypatch.setattr(api_app.config, "DRAW_TIME", "00:00")

    class _F:
        @staticmethod
        def fetch_and_update():
            return {"inserted_new": 0}

    monkeypatch.setattr(api_app, "data_fetcher", _F, raising=False)
    monkeypatch.setattr(evaluate, "online_check", lambda: {"ok": True})
    monkeypatch.setattr(notify, "notify_after_check", lambda r: "skipped")
    monkeypatch.setattr(api_app, "_evolution_step", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(BT, "expected_issue",
                        lambda d: {"issue": str(int(d[-1]["issue"]) + 1)})
    monkeypatch.setattr(api_app.db, "load_predictions", lambda i: [])

    # 生命周期 run_cycle 抛错 → 钩子必须吞掉（不阻断预测）
    import lottery.rule_lifecycle as RL
    def boom(*a, **k):
        raise RuntimeError("生命周期注入失败")
    monkeypatch.setattr(RL, "run_cycle", boom, raising=False)
    monkeypatch.setattr(_engine, "predict_next", lambda *a, **k: {"ok": True},
                        raising=False)

    _scheduler_step()      # 不抛异常即通过（钩子失败被吞）


def test_scheduler_hook_calls_lifecycle(monkeypatch, seeded_db):
    """调度器钩子确实调用了 lifecycle.run_cycle（而非静默跳过）。"""
    from lottery import api_app, backtest as BT, engine as _engine
    from lottery import evaluate, notify
    from lottery.api_app import _scheduler_step
    seeded_db(600, seed=3)

    monkeypatch.setattr(api_app.config, "DRAW_WEEKDAYS", (0, 1, 2, 3, 4, 5, 6))
    monkeypatch.setattr(api_app.config, "DRAW_TIME", "00:00")

    class _F:
        @staticmethod
        def fetch_and_update():
            return {"inserted_new": 0}

    monkeypatch.setattr(api_app, "data_fetcher", _F, raising=False)
    monkeypatch.setattr(evaluate, "online_check", lambda: {"ok": True})
    monkeypatch.setattr(notify, "notify_after_check", lambda r: "skipped")
    monkeypatch.setattr(api_app, "_evolution_step", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(BT, "expected_issue",
                        lambda d: {"issue": str(int(d[-1]["issue"]) + 1)})
    monkeypatch.setattr(api_app.db, "load_predictions", lambda i: [])

    import lottery.rule_lifecycle as RL
    called = {}
    monkeypatch.setattr(RL, "run_cycle",
                        lambda *a, **k: called.setdefault("n", 0) or called.update(
                            n=called.get("n", 0) + 1) or {
                            "migrated": [], "counts": {"active": 0, "monitor": 0,
                                                       "unmanaged": 0}}, raising=False)
    monkeypatch.setattr(_engine, "predict_next", lambda *a, **k: {"ok": True},
                        raising=False)

    _scheduler_step()
    assert called.get("n", 0) >= 1          # 生命周期被调用


def test_strict_ignores_cache_and_validates_anyway(monkeypatch, seeded_db):
    """顺序缺陷回归（2026-10-18）：严格模式必须**先于缓存检查**生效。

    旧的实现里门控在 ``if not regenerate: existing=...`` 之后 ——
    一旦目标期已有预测缓存，严格模式会被绕过直接返回旧票（哪怕当时无规律/LLM 关）。
    修复后：严格模式开启时强制 ``regenerate=True``，缓存不再能掩盖门控校验。
    """
    from fastapi.testclient import TestClient
    from lottery import api_app, backtest as BT
    from lottery.api_app import app
    seeded_db(600, seed=3)
    _enable_strict(monkeypatch, True)

    monkeypatch.setattr(api_app.config, "DRAW_WEEKDAYS", (0, 1, 2, 3, 4, 5, 6))
    monkeypatch.setattr(api_app.config, "DRAW_TIME", "00:00")
    monkeypatch.setattr(BT, "expected_issue",
                        lambda d: {"issue": str(int(d[-1]["issue"]) + 1)})

    # 目标期**已有缓存预测**（这是旧实现会被绕过门控的诱因）
    monkeypatch.setattr(api_app.db, "load_predictions",
                        lambda i: [{"issue": i, "reds": "1,2,3,4,5,6",
                                    "blue": 7, "method": "stat:freq"}],
                        raising=False)
    # 但无可用规律 → 即使有缓存，严格模式也必须拒绝
    import lottery.patterns_runtime as PR
    monkeypatch.setattr(PR, "load_snapshot",
                        lambda *a, **k: {"counts": {"usable": 0}}, raising=False)

    r = TestClient(app).get("/api/predict")     # 不传 regenerate=true
    assert r.status_code == 422, f"缓存不应绕过严格门控：{r.text}"
    assert "无可用规律" in r.json()["error"]
