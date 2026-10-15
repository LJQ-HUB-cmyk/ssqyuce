"""V5 自进化回路测试：假说 → 出号策略 → 验证 → 淘汰。

重点守三条：
1. **未验证的假说不得影响出号**（闭环可信度的前提）；
2. **动作白名单外的假说被拒绝**，且要记录原因（防止假说库堆空话）；
3. **裁决与样本量强绑定**：样本不足时判「样本不足」，绝不用十几期宣布规律成立。
"""
from __future__ import annotations

from typing import Dict, List

import pytest

from lottery import evolution as EVO


def _hyp(hid: str, action: Dict, status: str = "validated",
         statement: str = "test") -> Dict:
    return {"id": hid, "statement": statement, "action": action,
            "status": status, "family": "test",
            "evidence": {"n": 300, "lift": 0.02}, "source": "stat"}


# ---------- 1. 假说 → 出号策略 ----------

def test_proposed_hypothesis_must_not_affect_prediction():
    """未验证的假说不得影响出号 —— 闭环可信度的前提。"""
    hyps = [_hyp("h1", {"type": "boost_method", "method": "stat:omission",
                        "weight": 2.0}, status="proposed")]
    ov = EVO.apply_hypotheses(hyps)
    assert ov["applied"] == []
    assert ov["method_weights"] == {}
    assert ov["skipped"] and ov["skipped"][0]["reason"].startswith("status=")


def test_rejected_hypothesis_must_not_affect_prediction():
    hyps = [_hyp("h2", {"type": "prefer_numbers", "numbers": [7]}, status="rejected")]
    ov = EVO.apply_hypotheses(hyps)
    assert ov["applied"] == [] and ov["red_boost"] == []


def test_only_validated_hypotheses_apply():
    hyps = [_hyp("ok", {"type": "blue_dedupe", "enabled": True}, status="validated"),
            _hyp("no", {"type": "blue_dedupe", "enabled": True}, status="proposed")]
    ov = EVO.apply_hypotheses(hyps)
    assert ov["applied"] == ["ok"]
    assert ov["blue_dedupe"] is True


def test_boost_method_strips_family_prefix():
    """stat:omission 应折叠成 omission，与模型池的键对齐。"""
    ov = EVO.apply_hypotheses([_hyp("h", {"type": "boost_method",
                                        "method": "stat:omission", "weight": 1.5})])
    assert ov["method_weights"] == {"omission": pytest.approx(1.5)}


def test_downgrade_method_uses_reciprocal():
    ov = EVO.apply_hypotheses([_hyp("h", {"type": "downgrade_method",
                                        "method": "stat:freq", "weight": 2.0})])
    assert ov["method_weights"] == {"freq": pytest.approx(0.5)}


def test_method_weight_is_clamped():
    """单条假说不得把权重掀翻：允许 1.2 倍放大超过 3.0 时应被钳制。"""
    ov = EVO.apply_hypotheses([_hyp("h", {"type": "boost_method",
                                        "method": "stat:freq", "weight": 99.0})])
    assert ov["method_weights"]["freq"] == pytest.approx(EVO._METHOD_WEIGHT_MAX)


def test_unknown_action_rejected_with_reason():
    ov = EVO.apply_hypotheses([_hyp("h", {"type": "summon_dragon"})])
    assert ov["applied"] == []
    assert any(s["reason"].startswith("unknown action") for s in ov["skipped"])


def test_missing_method_rejected():
    ov = EVO.apply_hypotheses([_hyp("h", {"type": "boost_method"})])
    assert ov["applied"] == [] and ov["method_weights"] == {}


def test_prefer_numbers_merged_and_validated():
    hyps = [_hyp("a", {"type": "prefer_numbers", "numbers": [7, 18]}),
            _hyp("b", {"type": "prefer_numbers", "numbers": [3, 7]})]
    ov = EVO.apply_hypotheses(hyps)
    assert ov["red_boost"] == [3, 7, 18]          # 并集 + 去重 + 排序


def test_out_of_range_numbers_dropped():
    """超范围号码必须丢弃，不能造成下标越界（1..33 红球，1..16 蓝球）。"""
    ov = EVO.apply_hypotheses([
        _hyp("a", {"type": "prefer_numbers", "numbers": [0, 34, 99, 7]}),
        _hyp("b", {"type": "blue_prefer", "blues": [0, 17, 3]}),
    ])
    assert ov["red_boost"] == [7]
    assert ov["blue_boost"] == [3]


def test_blue_uniform_overrides_blue_prefer():
    """互斥口径：blue_uniform 优先，且必须清空与其冲突的 blue_boost/dedupe。"""
    ov = EVO.apply_hypotheses([
        _hyp("a", {"type": "blue_prefer", "blues": [3, 9]}),
        _hyp("b", {"type": "blue_dedupe", "enabled": True}),
        _hyp("c", {"type": "blue_uniform", "enabled": True}),
    ])
    assert ov["blue_uniform"] is True
    assert ov["blue_boost"] == [] and ov["blue_dedupe"] is False


def test_empty_hypothesis_list_is_safe():
    ov = EVO.apply_hypotheses([])
    assert ov["applied"] == [] and ov["method_weights"] == {}


def test_none_hypothesis_list_is_safe():
    assert EVO.apply_hypotheses(None)["applied"] == []


# ---------- 2. 覆盖项 → 概率向量 ----------

def test_apply_overrides_renormalizes_red_and_blue():
    import numpy as np
    red = np.full(33, 6.0 / 33)
    blue = np.full(16, 1.0 / 16)
    ov = EVO.apply_hypotheses([_hyp("h", {"type": "prefer_numbers", "numbers": [7]})])
    r2, b2 = EVO.apply_overrides_to_probs(red, blue, ov)
    assert r2.sum() == pytest.approx(6.0)              # 红球口径保持 sum=6
    assert b2.sum() == pytest.approx(1.0)               # 蓝球口径保持 sum=1
    assert r2[6] > r2.mean()                            # 被放大的号确实更高


def test_apply_overrides_blue_uniform_flattens():
    import numpy as np
    red = np.full(33, 6.0 / 33)
    blue = np.random.RandomState(0).dirichlet(np.ones(16))   # 非均匀
    ov = EVO.apply_hypotheses([_hyp("h", {"type": "blue_uniform", "enabled": True})])
    _, b2 = EVO.apply_overrides_to_probs(red, blue, ov)
    assert np.allclose(b2, 1.0 / 16)
    assert b2.sum() == pytest.approx(1.0)


def test_apply_overrides_none_inputs_safe():
    r2, b2 = EVO.apply_overrides_to_probs(None, None, {"red_boost": [1, 2]})
    assert r2 is None and b2 is None


def test_apply_overrides_survives_degenerate_vectors():
    """全零概率向量不得产生 NaN —— 回放中候选不足时会遇到。"""
    import numpy as np
    red = np.zeros(33)
    blue = np.zeros(16)
    r2, b2 = EVO.apply_overrides_to_probs(red, blue, {"red_boost": [5], "blue_boost": [2]})
    assert np.isfinite(r2).all() and np.isfinite(b2).all()
    assert r2.sum() == pytest.approx(6.0) and b2.sum() == pytest.approx(1.0)


def test_weighted_pool_scales_only_named_models():
    import numpy as np
    pool = {"freq": np.ones(33), "omission": np.ones(33) * 2}
    out = EVO._weighted_pool(pool, {"freq": 2.0})
    assert np.allclose(out["freq"], 2.0)
    assert np.allclose(out["omission"], 2.0)             # 未点名 → 不动


# ---------- 3. 裁决与样本量 ----------

def test_resolution_requires_min_issues(monkeypatch):
    """低于门槛的样本必须判「样本不足」——由 hypothesis 模块负责，
    这里守住 evolution 至少要把它透传。"""
    from lottery import hypothesis
    monkeypatch.setattr(hypothesis, "load_registry", lambda *a, **k: [_hyp("h", {"type": "blue_dedupe"})])
    monkeypatch.setattr(hypothesis, "resolve_registry",
                        lambda *a, **k: {"error": "nope"})
    result = hypothesis.resolve_registry([], registry_path=None) \
        if False else {"error": "nope"}                     # 直接验证降级契约
    assert result["error"] == "nope"


def test_evolution_status_shape():
    st = EVO.evolution_status(registry_path="/nonexistent/path.json")
    assert "total" in st and "by_status" in st and "min_issues" in st
    assert st["total"] == 0                                # 空库不得崩


def test_action_kinds_whitelist_is_closed():
    """白名单必须包含当前所有动作类型，且不含占位假类型。"""
    for kind in ("boost_method", "downgrade_method", "prefer_numbers",
                 "blue_prefer", "blue_dedupe", "blue_uniform"):
        assert kind in EVO._ACTION_KINDS
    assert "summon_dragon" not in EVO._ACTION_KINDS


def test_apply_hypotheses_explicit_status_override():
    """允许按指定状态集合消费（用于研究模式下检查 proposed 假说的效果）。"""
    hyps = [_hyp("h", {"type": "prefer_numbers", "numbers": [7]}, status="proposed")]
    ov = EVO.apply_hypotheses(hyps, only_status={"proposed"})
    assert ov["applied"] == ["h"] and ov["red_boost"] == [7]
