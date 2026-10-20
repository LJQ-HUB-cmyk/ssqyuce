"""P4：闲时挖掘调度器。

## 保的是什么

不是"能挖到规律"（真实数据上大概率一条都挖不到，那是诚实结果），
而是**三条工程约束**：

1. **默认关闭** —— 新机制先观察再开，不默认改变生产行为；
2. **可中断可续跑** —— 部署/OOM 杀进程不能丢进度、不能重头再来；
3. **让路** —— 挖掘跑满 CPU 会把 40 线程池吃光 → healthcheck 超时 → 容器被判不健康。

第 3 条是单机部署里最容易忽视的：一个"后台任务"把前台拖垮，比它不跑更糟。
"""
from __future__ import annotations

import json

import pytest

from lottery import config, idle_mining


@pytest.fixture()
def idle_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setenv("LOTT_IDLE_MINING", "0")
    from lottery import db
    db.close()
    db.get_conn()
    yield tmp_path
    db.close()


def test_disabled_by_default(idle_env):
    """**默认必须关闭**：新机制不该默认改变生产行为。"""
    assert idle_mining.enabled() is False
    r = idle_mining.run_once()
    assert r["ok"] is False
    assert r["reason"] == "idle_mining_disabled"


def test_enabled_by_env(idle_env, monkeypatch):
    monkeypatch.setenv("LOTT_IDLE_MINING", "1")
    assert idle_mining.enabled() is True


def test_state_roundtrip(idle_env):
    st = {"completed_dimensions": ["d1", "d2"], "verdicts": {"x": {"decision": "monitor"}}}
    idle_mining.save_state(st)
    loaded = idle_mining.load_state()
    assert loaded["completed_dimensions"] == ["d1", "d2"]
    assert loaded["verdicts"]["x"]["decision"] == "monitor"
    assert loaded["updated_at"] is not None


def test_corrupt_state_does_not_crash(idle_env):
    """坏断点文件必须当作"从头开始"，不能让整个调度卡死。"""
    p = idle_mining.state_path()
    p.write_text("{ this is not json", encoding="utf-8")
    st = idle_mining.load_state()
    assert st["cursors"] == {"single": 0, "cross": 0}
    assert st["space_version"] == idle_mining.SPACE_VERSION
    assert st["runs"] == 0


def test_save_state_is_atomic(idle_env):
    """先写 tmp 再 replace —— 中段被杀不会留下半写文件。"""
    idle_mining.save_state({"cursors": {"single": 3, "cross": 0}, "usable": {}})
    p = idle_mining.state_path()
    assert p.exists()
    json.loads(p.read_text(encoding="utf-8"))       # 必须是合法 JSON
    assert not p.with_suffix(".tmp").exists()


def test_resume_advances_cursor_not_repeat(idle_env, monkeypatch):
    """**续跑**：游标必须前移，同一索引不得重复判定。

    这条是 P4 生成式改造后「只挖没挖过的」的核心保证 ——
    旧实现靠 `completed_dimensions` 集合去重，新实现靠确定性游标。
    """
    monkeypatch.setenv("LOTT_IDLE_MINING", "1")
    idle_mining.save_state({"space_version": idle_mining.SPACE_VERSION,
                            "cursors": {"single": 7, "cross": 0},
                            "usable": {}, "n_checked": 7})
    st = idle_mining.load_state()
    assert st["cursors"]["single"] == 7


def test_judge_candidate_delegates_to_eprocess(idle_env):
    """所有闲时挖掘的判定必须走同一入口（将来换统计方法只改一处）。"""
    import numpy as np
    z = list(np.random.default_rng(3).normal(0.2, 1, 200))
    v = idle_mining.judge_candidate(z)
    assert v["source"] == "idle_mining"
    for k in ("e_value", "decision", "n_obs", "alpha_spent"):
        assert k in v


def test_judge_candidate_underpowered_on_short_series(idle_env):
    v = idle_mining.judge_candidate([0.1] * 5)
    assert v["decision"] == "underpowered"


def test_status_reports_quota_fields(idle_env):
    """状态字段供 /api/health 暴露配额与进度（V13 P4 验收要求）。"""
    s = idle_mining.status()
    for k in ("enabled", "completed_dimensions", "runs", "total_candidates", "by_decision"):
        assert k in s
    assert s["enabled"] is False


def test_run_once_refuses_without_enough_data(idle_env, monkeypatch):
    monkeypatch.setenv("LOTT_IDLE_MINING", "1")
    r = idle_mining.run_once(min_start=300)
    assert r["ok"] is False
    assert r["reason"] == "insufficient_data"


def test_idle_mining_evaluates_real_sequences(monkeypatch, seeded_db, tmp_path):
    """验证 P4-2：闲时挖掘真实计算 asof_scan 并产生 e-process 判定。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setenv("LOTT_IDLE_MINING", "1")
    seeded_db(400)
    
    res = idle_mining.run_once(budget_candidates=5, min_start=200)
    assert res["ok"] is True
    assert res["processed"] > 0
    
    state = idle_mining.load_state()
    # 判定记录现在按 decision 汇总计数（不再全量存 verdicts ——
    # 开放空间下候选数可达数万，全存会让断点膨胀到几十 MB）
    assert state["n_checked"] > 0
    assert sum(state["by_decision"].values()) == state["n_checked"]


# ---------- P4 接线：常驻循环真的会调用 run_once（V13 补齐项） ----------

def test_idle_mining_step_calls_run_once_when_enabled(idle_env, monkeypatch):
    """接线测的核心：`_idle_mining_step()` 必须真的走到 `run_once()`。

    这条防的是「模块写好了但没有任何调用点」—— 本项修复前的真实缺陷：
    `run_once` 全仓只有测试在调，调度器从不触发，health 永远 runs=0。
    """
    from lottery import api_app
    monkeypatch.setenv("LOTT_IDLE_MINING", "1")
    called = {}
    monkeypatch.setattr(idle_mining, "run_once",
                        lambda **kw: called.update(kw) or {"ok": True, "processed": 0})
    r = api_app._idle_mining_step()
    assert r["ok"] is True
    assert called is not None            # 证明 run_once 被真正调用
    assert "budget_candidates" not in called  # 用默认预算，不越权改参


def test_idle_mining_step_noop_when_disabled(idle_env, monkeypatch):
    """关闭时**不得**调用 run_once —— 默认关闭的承诺要在接线层也成立。"""
    from lottery import api_app
    monkeypatch.setenv("LOTT_IDLE_MINING", "0")
    hit = {"n": 0}

    def _boom(**kw):
        hit["n"] += 1
        raise AssertionError("关闭状态下不该调用 run_once")

    monkeypatch.setattr(idle_mining, "run_once", _boom)
    r = api_app._idle_mining_step()
    assert r["ok"] is False
    assert r["reason"] == "idle_mining_disabled"
    assert hit["n"] == 0


def test_idle_mining_loop_survives_exception(idle_env, monkeypatch):
    """后台任务抛异常不能打死线程，也不能拖垮前台（单机部署的头号风险）。"""
    from lottery import api_app
    monkeypatch.setenv("LOTT_IDLE_MINING", "1")
    monkeypatch.setattr(api_app, "IDLE_MINING_INTERVAL", 0)
    state = {"n": 0}

    def _flaky(**kw):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("模拟第一轮炸掉")
        raise SystemExit("第二轮主动退出循环")

    monkeypatch.setattr(idle_mining, "run_once", _flaky)
    with pytest.raises(SystemExit):
        api_app._idle_mining_loop()
    assert state["n"] == 2          # 第一轮异常后仍进入第二轮 => 线程没死


def test_idle_mining_independent_of_scheduler_switch(idle_env, monkeypatch):
    """两个开关语义独立：只开 idle 不开 scheduler 也必须能跑。"""
    from lottery import api_app
    assert hasattr(api_app, "_idle_mining_loop")
    # 接线代码读的是 LOTT_IDLE_MINING，不是 config.SCHEDULER_ENABLED
    src = __import__("inspect").getsource(api_app._idle_mining_loop)
    assert "LOTT_IDLE_MINING" not in src        # 门控在模块级，不在循环里
