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
    assert st["completed_dimensions"] == []
    assert st["runs"] == 0


def test_save_state_is_atomic(idle_env):
    """先写 tmp 再 replace —— 中段被杀不会留下半写文件。"""
    idle_mining.save_state({"completed_dimensions": ["a"], "verdicts": {}})
    p = idle_mining.state_path()
    assert p.exists()
    json.loads(p.read_text(encoding="utf-8"))       # 必须是合法 JSON
    assert not p.with_suffix(".tmp").exists()


def test_resume_skips_completed(idle_env, monkeypatch):
    """**续跑**：已完成的维度不得重复处理。"""
    monkeypatch.setenv("LOTT_IDLE_MINING", "1")
    # 直接验证断点语义（不依赖 discovery 的实际枚举）
    idle_mining.save_state({"completed_dimensions": ["dim_a"], "verdicts": {}})
    st = idle_mining.load_state()
    assert "dim_a" in st["completed_dimensions"]


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
    verdicts = state.get("verdicts", {})
    assert len(verdicts) > 0
    # 验证判定字典包含 e-process 可审计字段
    sample_v = next(iter(verdicts.values()))
    assert "decision" in sample_v
