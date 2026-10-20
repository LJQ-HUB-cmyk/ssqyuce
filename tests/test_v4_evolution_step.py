"""V5：调度器自动进化（_evolution_step）与 API 契约测试。

守三件事：
1. **每期自动进化真的会做 A/B 验证**——否则假说永远停在 proposed，
   进化就成了只挖不验的空转（这正是本次要修的缺陷）。
2. **失败不得影响当期预测**：进化任何环节异常都必须被吞掉并记录，
   绝不能让调度器整体挂掉、导致预测不生成。
3. **手动触发与自动调度走同一条路径**，避免两处逻辑漂移。
"""
from __future__ import annotations

from typing import Dict, List

import pytest


def _draws(n: int = 400) -> List[Dict]:
    return [{"issue": str(2026000 + i),
             "reds": sorted({((i * 7 + j * 5) % 33) + 1 for j in range(6)}),
             "blue": (i % 16) + 1,
             "date": f"2026-01-{(i % 28) + 1:02d}"} for i in range(n)]


# ---------- 1. _evolution_step 必须做 A/B 验证 ----------

def test_evolution_step_runs_ab_verification(monkeypatch):
    """核心回归：必须对 proposed 假说跑 A/B 验证，否则假说永远无法晋升。"""
    from lottery import api_app, evolution, hypothesis

    fake_hyps = [
        {"id": "h1", "statement": "s1", "status": "proposed",
         "action": {"type": "prefer_numbers", "numbers": [7]}, "evidence": {"n": 300}},
        {"id": "h2", "statement": "s2", "status": "proposed",
         "action": {"type": "blue_uniform", "enabled": True}, "evidence": {"n": 300}},
        {"id": "h3", "statement": "s3", "status": "validated",
         "action": {"type": "blue_dedupe"}, "evidence": {"n": 300}},
    ]
    monkeypatch.setattr(hypothesis, "mine_hypotheses",
                        lambda *a, **k: {"hypotheses": [], "stat_count": 0,
                                         "llm_count": 0, "llm_used": False})
    monkeypatch.setattr(hypothesis, "register_hypotheses",
                        lambda *a, **k: {"registered": 0, "deduped": 0, "total": 3})
    monkeypatch.setattr(hypothesis, "load_registry", lambda *a, **k: fake_hyps)

    verified_ids: List[str] = []

    def _fake_ab(h, draws, **kw):
        verified_ids.append(h.get("id"))
        return {"method": None, "hyp_id": h.get("id"), "n_issues": 600,
                "delta_red_hits": 0.0, "delta_blue_rate": 0.0}

    monkeypatch.setattr(evolution, "verify_hypothesis_ab", _fake_ab)
    monkeypatch.setattr(hypothesis, "resolve_registry",
                        lambda *a, **k: {"validated": [], "rejected": [],
                                         "insufficient": [], "unchanged": 2})
    monkeypatch.setattr(evolution, "evolution_status",
                        lambda *a, **k: {"by_status": {"proposed": 2}, "active_overrides": {}})
    monkeypatch.setattr(api_app.db, "load_draws", lambda: _draws(400))

    out = api_app._evolution_step(limit=400, use_llm=False)
    assert out.get("ok") is True
    # 只验证 proposed 的两条，不应重复验证已 validated 的 h3
    assert sorted(verified_ids) == ["h1", "h2"], f"应只验证 proposed，实际 {verified_ids}"
    assert out["verified"] == 2
    assert "resolution" in out and "by_status" in out


def test_evolution_step_skips_when_no_draws(monkeypatch):
    from lottery import api_app
    monkeypatch.setattr(api_app.db, "load_draws", lambda: [])
    out = api_app._evolution_step()
    assert out.get("ok") is False
    assert "暂无开奖数据" in str(out.get("reason"))


def test_evolution_step_survives_single_hypothesis_failure(monkeypatch):
    """单条假说验证抛异常时，其余假说仍要被验证（不能一条坏全轮）。"""
    from lottery import api_app, evolution, hypothesis

    fake = [{"id": "bad", "statement": "b", "status": "proposed",
             "action": {"type": "blue_dedupe"}},
            {"id": "good", "statement": "g", "status": "proposed",
             "action": {"type": "blue_dedupe"}}]
    monkeypatch.setattr(hypothesis, "mine_hypotheses",
                        lambda *a, **k: {"hypotheses": [], "stat_count": 0, "llm_count": 0})
    monkeypatch.setattr(hypothesis, "register_hypotheses", lambda *a, **k: {"registered": 0})
    monkeypatch.setattr(hypothesis, "load_registry", lambda *a, **k: fake)
    monkeypatch.setattr(hypothesis, "resolve_registry",
                        lambda *a, **k: {"validated": [], "rejected": [], "insufficient": []})
    monkeypatch.setattr(evolution, "evolution_status",
                        lambda *a, **k: {"by_status": {}, "active_overrides": {}})

    calls = []

    def _ab(h, draws, **kw):
        calls.append(h.get("id"))
        if h.get("id") == "bad":
            raise RuntimeError("boom")
        return {"method": None, "hyp_id": h.get("id"), "n_issues": 600,
                "delta_red_hits": 0.0, "delta_blue_rate": 0.0}

    monkeypatch.setattr(evolution, "verify_hypothesis_ab", _ab)
    monkeypatch.setattr(api_app.db, "load_draws", lambda: _draws(400))

    out = api_app._evolution_step(limit=400, use_llm=False)
    assert set(calls) == {"bad", "good"}      # 两条都被尝试
    assert out["verified"] == 1              # 只有一条成功
    assert out.get("ok") is True


# ---------- 2. 失败不得影响预测生成 ----------

def test_scheduler_evolution_failure_does_not_block_prediction(monkeypatch):
    """进化失败必须被吞掉：调度器仍要走到「生成预测」这一步。"""
    from lottery import api_app

    monkeypatch.setattr(api_app.config, "DRAW_WEEKDAYS", (0, 1, 2, 3, 4, 5, 6))
    monkeypatch.setattr(api_app.config, "DRAW_TIME", "00:00")

    class _F:
        @staticmethod
        def fetch_and_update():
            return {"inserted_new": 1}

    monkeypatch.setattr(api_app, "data_fetcher", _F, raising=False)

    from lottery import evaluate, notify, backtest as BT
    monkeypatch.setattr(evaluate, "online_check", lambda: {"ok": True})
    monkeypatch.setattr(notify, "notify_after_check", lambda r: "skipped")
    monkeypatch.setattr(api_app.db, "load_draws", lambda: _draws(400))
    monkeypatch.setattr(BT, "next_issue", lambda i: "2026401")

    def _boom(*a, **k):
        raise RuntimeError("evolution exploded")

    monkeypatch.setattr(api_app, "_evolution_step", _boom)

    gen_calls = []

    # 注意：_scheduler_step 内部是 `from . import ... engine ...`（函数内导入），
    # 所以必须 patch **源模块 lottery.engine** 上的 predict_next，
    # 而不是 api_app.engine —— 后者不会影响函数内新解析的名字。
    from lottery import engine as _engine
    gen_calls = []

    def _fake_predict(draws, use_llm=True, **kw):
        gen_calls.append(use_llm)
        return {"tickets": []}

    monkeypatch.setattr(_engine, "predict_next", _fake_predict, raising=False)
    monkeypatch.setattr(api_app.db, "load_predictions", lambda i: [])

    api_app._scheduler_step()                # 不得抛异常
    assert gen_calls == [True], (
        "进化失败后仍必须生成当期预测；"
        f"实际 predict_next 调用 = {gen_calls}")


def test_scheduler_skips_prediction_when_already_generated(monkeypatch):
    """已存在当期预测时不应重复生成（幂等守卫，避免浪费 LLM 成本）。"""
    from lottery import api_app, backtest as BT, engine as _engine, evaluate, notify

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
    monkeypatch.setattr(api_app.db, "load_draws", lambda: _draws(400))
    monkeypatch.setattr(BT, "next_issue", lambda i: "2026401")
    monkeypatch.setattr(api_app.db, "load_predictions", lambda i: [{"issue": i}])

    calls = []
    monkeypatch.setattr(_engine, "predict_next",
                        lambda *a, **k: calls.append(1), raising=False)

    api_app._scheduler_step()
    assert calls == [], "已有当期预测时不得重复生成"


# ---------- 3. 手动触发与自动调度同路径 ----------

def test_api_run_endpoint_uses_same_step_as_scheduler(monkeypatch):
    """`/api/evolution/run` 必须调用 _evolution_step，避免两处逻辑漂移。

    V12 变更（2026-10-19，CPU 治理）：`_evolution_step` 新增 ``force`` 参数
    （幂等闸门：同一期数据在自动调度下只跑一次；手动触发传 ``force=True`` 绕过）。
    桩必须同步接收 ``force``，并断言手动路径**确实传了 True** —— 否则「用户点了
    却因为幂等闸门什么都没跑」会成为一个静默的功能缺陷。
    """
    from fastapi.testclient import TestClient

    from lottery import api_app
    monkeypatch.setattr(api_app.db, "load_draws", lambda: _draws(400))

    called = {}

    def _step(limit=0, use_llm=True, force=False):
        called["limit"] = limit
        called["use_llm"] = use_llm
        called["force"] = force
        return {"ok": True, "verified": 0}

    from lottery import evolution_service
    monkeypatch.setattr(evolution_service, "_evolution_step", _step)
    client = TestClient(api_app.app)
    r = client.post("/api/evolution/run?limit=400&use_llm=0")
    assert r.status_code == 200, r.text
    assert called.get("limit") == 400
    assert called.get("use_llm") is False
    assert called.get("force") is True, "手动触发必须绕过幂等闸门（force=True）"
    assert r.json().get("ok") is True


def test_api_run_endpoint_returns_503_when_module_missing(monkeypatch):
    """模块缺失必须降级 503，而不是 500。"""
    import builtins
    from fastapi.testclient import TestClient

    from lottery import api_app
    real_import = builtins.__import__

    def _fake(name, *a, **kw):
        if name.startswith("lottery."):
            raise ImportError("simulated")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _fake)
    r = TestClient(api_app.app).post("/api/evolution/run")
    assert r.status_code == 503
    assert r.json().get("available") is False


# ---------- 4. V5 交付必须被记录（版本/里程碑） ----------

def test_v5_delivery_is_recorded():
    """V5 自进化批次必须有版本提升与里程碑条目，且不破坏 V4 的历史记录。"""
    from lottery import config

    def _ver(v: str):
        return tuple(int(x) for x in str(v).split(".") if x.isdigit())

    assert _ver(config.APP_VERSION) >= (2, 1, 0), (
        f"V5 交付后 APP_VERSION 应 >= 2.1.0，实际 {config.APP_VERSION}")
    # V6（2026-10-V6）继续向前：版本号只增不减，构建标识必须指向当前批次。
    assert _ver(config.APP_BUILD.split("-")[-1].lstrip("V")) >= (5,), (
        f"APP_BUILD 应为 V5 或更新批次，实际 {config.APP_BUILD}")
    assert "V5" in config.APP_MILESTONES, "APP_MILESTONES 缺少 V5 条目"
    assert config.APP_MILESTONES["V5"].get("status") == "done"
    # V4 的历史记录不得因 V5 而丢失
    assert "V4" in config.APP_MILESTONES
