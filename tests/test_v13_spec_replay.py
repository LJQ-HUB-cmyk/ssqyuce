"""测试 P3 规律协议与可重放判据 R1–R5 (M3 §7)。"""
from __future__ import annotations

import pytest
from lottery import rule_protocol, spec_replay, db


def test_rule_protocol_spec_exp_id_deterministic():
    spec1 = rule_protocol.HypothesisSpec("hot", {"window": 10, "k": 5}, "red")
    spec2 = rule_protocol.HypothesisSpec("hot", {"k": 5, "window": 10}, "red")
    assert spec1.exp_id() == spec2.exp_id()
    assert len(spec1.exp_id()) == 16


def test_spec_replay_r1_compilable():
    # 合法规格
    valid_spec = {"family_key": "hot", "params": {"window": 10, "k": 5}}
    flt = spec_replay.compile_spec(valid_spec)
    assert callable(flt.get("selector"))

    # 非法规格抛出 SpecCompileError
    with pytest.raises(spec_replay.SpecCompileError):
        spec_replay.compile_spec({"family_key": "non_existent_family", "params": {}})


def test_spec_replay_r5_requires_prereg_ok():
    spec = {"family_key": "hot", "params": {"window": 10, "k": 5}}
    # prereg_ok 为 True 才能通过
    rep_ok = spec_replay.replayable({"spec": spec, "prereg_ok": True})
    assert rep_ok["ok"] is True
    assert not rep_ok["reasons"]

    # prereg_ok 缺失或 False 则被拒
    rep_fail = spec_replay.replayable({"spec": spec, "prereg_ok": False})
    assert rep_fail["ok"] is False
    assert any("R5" in r for r in rep_fail["reasons"])


def test_eval_on_history_deterministic(seeded_db):
    seeded_db(100)
    draws = db.load_draws()
    spec = {"family_key": "hot", "params": {"window": 10, "k": 5}}
    compiled = spec_replay.compile_spec(spec)
    
    # 两次在同一历史切片执行结果完全相同 (R4)
    res1 = spec_replay.eval_on_history(compiled, draws)
    res2 = spec_replay.eval_on_history(compiled, draws)
    assert res1 == res2
    assert isinstance(res1, set)


def test_patterns_runtime_accepts_replayable_llm_hypothesis(monkeypatch):
    from lottery import patterns_runtime
    
    # 构造假数据
    hyp = {
        "id": "h_llm_test",
        "source": "llm",
        "status": "validated",
        "prereg_ok": True,
        "spec": {"family_key": "hot", "params": {"window": 10, "k": 5}},
        "statement": "LLM 提出的热号规律"
    }
    
    # 验证 spec_replay 能判定为 ok
    rep = spec_replay.replayable(hyp)
    assert rep["ok"] is True
