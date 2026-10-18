"""V11 阶段可用档（phase_usable）测试。

规格与依据：``docs/reviews/PHASE_USABLE_STUDY.md``
用户诉求：不要只用一套极严标准把一切判死，阶段性规律「只要当前可用」也应能指导预测。

本档的设计原则（每条都要有测试守着）：
1. **默认关闭** —— 研究显示该假设未获数据支持（相邻窗口无持续性），不能默认开启；
2. **打开后阶段类可进入候选** —— 满足用户诉求，但必须过前置注册 + 未被判负；
3. **两类分开计数与度量** —— `usable_long_term` / `usable_phase` 不得混在一起；
4. **"长期"档的行为不因本档开关而改变** —— 打开阶段档不能放宽长期档的门槛。

注意：这些测试只验证**档位接线**，不验证"阶段规律有效"——后者是研究问题，
结论见 PHASE_USABLE_STUDY.md（不成立）。
"""
from __future__ import annotations

import random

import pytest

from lottery import config, patterns_runtime as PR

MIN_DRAWS = 600


def _draws(n: int = MIN_DRAWS, seed: int = 3):
    rng = random.Random(seed)
    out = []
    for i in range(n):
        reds = sorted(rng.sample(range(1, 34), 6))
        out.append({"issue": f"{2003001 + i}", "date": None,
                    "reds": reds, "blue": rng.randint(1, 16)})
    return out


def _hyp(hid: str, regime_status: str, *, prereg: bool = True,
         hyp_status: str = "validated", source: str = "stat",
         params: dict | None = None) -> dict:
    """构造一条可回放的假说（维度用 hot 的合法档位）。"""
    return {
        "id": hid, "family": "hot", "source": source,
        "statement": f"测试用假说 {hid}",
        "action": {"type": "prefer_red_filter",
                   "filter": {"key": "hot",
                              "params": params or {"window": 10, "k": 6},
                              "target": "red"}},
        "status": hyp_status,
        "regime": {"status": regime_status, "label_zh": "x", "reason_zh": "y",
                   "recent_delta": 0.1, "p_adjusted_recent": 0.01},
        "prereg_ok": prereg,
        "prereg_status": "passed" if prereg else "failed",
        "exp_id": f"exp-{hid}",
    }


def _snapshot(monkeypatch, hyps, phase_enabled: bool):
    """注入假说库并生成快照（不 mock build_snapshot 内部）。"""
    from lottery import hypothesis

    monkeypatch.setattr(config, "LOTT_PHASE_USABLE", phase_enabled, raising=False)
    monkeypatch.setattr(hypothesis, "load_registry", lambda *a, **kw: {
        "version": "v10-1",
        "counters": {"n_tested_cumulative": 0, "last_issue": None, "batches": []},
        "hypotheses": hyps,
    })
    return PR.build_snapshot(_draws(), scan={"n_tested": 1, "items": [], "verdict": "x"})


def _by_id(snap):
    return {r["id"]: r for r in snap["rules"]}


# ---------------------------------------------------------------------------
# 1. 默认关闭：阶段类不得进入
# ---------------------------------------------------------------------------

def test_phase_off_by_default():
    """默认配置下 LOTT_PHASE_USABLE 必须为 False。

    研究结论（PHASE_USABLE_STUDY.md 检验5）：相邻窗口相关性 r=+0.124，
    95% 容差 ±0.151 → 无持续性。即「当前阶段有效」预测不了「下期有效」，
    因此**不能默认开启**。
    """
    assert config.LOTT_PHASE_USABLE is False


def test_emerging_is_monitor_when_phase_disabled(monkeypatch):
    """关闭时：emerging（仅最近阶段显著）只能是 monitor，不能 usable。"""
    snap = _snapshot(monkeypatch, [_hyp("e1", "emerging")], phase_enabled=False)
    r = _by_id(snap)["e1"]
    assert r["usable"] is False
    assert r["tier"] == "monitor"
    assert r["usable_kind"] is None


def test_fading_is_monitor_when_phase_disabled(monkeypatch):
    """关闭时：fading（上一阶段有效、最近失效）也只能是 monitor。"""
    snap = _snapshot(monkeypatch, [_hyp("f1", "fading")], phase_enabled=False)
    r = _by_id(snap)["f1"]
    assert r["usable"] is False and r["tier"] == "monitor"


# ---------------------------------------------------------------------------
# 2. 打开后：阶段类可进入，但门槛不放宽
# ---------------------------------------------------------------------------

def test_emerging_becomes_usable_phase_when_enabled(monkeypatch):
    """打开时：emerging + 前置注册通过 + 未被判负 → usable_phase（用户诉求）。"""
    snap = _snapshot(monkeypatch, [_hyp("e2", "emerging")], phase_enabled=True)
    r = _by_id(snap)["e2"]
    assert r["usable"] is True
    assert r["tier"] == "usable_phase"
    assert r["usable_kind"] == "phase"
    assert snap["counts"]["usable_phase"] == 1
    assert snap["counts"]["usable_long_term"] == 0


def test_phase_requires_prereg_even_when_enabled(monkeypatch):
    """打开时，阶段类**仍然**必须通过前置注册（prereg_ok），否则不可用。

    这是本档最重要的防线：阶段类本来就更弱，若再不受前置注册约束，
    就等于把「搜索出来的假阳性」直接变成投注。
    """
    snap = _snapshot(monkeypatch, [_hyp("e3", "emerging", prereg=False)],
                     phase_enabled=True)
    r = _by_id(snap)["e3"]
    assert r["usable"] is False
    assert r["tier"] == "monitor"


def test_phase_rejects_llm_source(monkeypatch):
    """阶段类同样排除 source=="llm"（llm 不可回放、规律本身不存在）。"""
    snap = _snapshot(monkeypatch, [_hyp("e4", "emerging", source="llm")],
                     phase_enabled=True)
    assert _by_id(snap)["e4"]["usable"] is False


def test_persistent_uses_long_term_tier_regardless_of_phase_switch(monkeypatch):
    """长期档（persistent + validated + prereg）在开关打开/关闭下都应是 usable_long_term。"""
    for enabled in (False, True):
        snap = _snapshot(monkeypatch, [_hyp("p1", "persistent")], phase_enabled=enabled)
        r = _by_id(snap)["p1"]
        assert r["usable"] is True, f"enabled={enabled}"
        assert r["tier"] == "usable_long_term", f"enabled={enabled}"
        assert r["usable_kind"] == "long_term"


def test_phase_switch_does_not_relax_long_term_requirements(monkeypatch):
    """打开阶段档**不能**放宽长期档门槛：persistent 但缺 prereg_ok 仍不可用。"""
    snap = _snapshot(monkeypatch, [_hyp("p2", "persistent", prereg=False)],
                     phase_enabled=True)
    r = _by_id(snap)["p2"]
    assert r["usable"] is False, "阶段档的开启不得连带放宽长期档"
    assert r["tier"] == "monitor"


# ---------------------------------------------------------------------------
# 3. 两类必须分开计数（混算就说不清"当前在用的是什么"）
# ---------------------------------------------------------------------------

def test_counts_separate_long_term_and_phase(monkeypatch):
    """同时存在长期与阶段类时，计数必须分开且 usable = 两者之和。"""
    hyps = [
        _hyp("L1", "persistent", params={"window": 10, "k": 5}),
        _hyp("E1", "emerging", params={"window": 10, "k": 6}),
        _hyp("E2", "emerging", params={"window": 10, "k": 8}),
        _hyp("F1", "fading", params={"window": 10, "k": 4}),
    ]
    snap = _snapshot(monkeypatch, hyps, phase_enabled=True)
    c = snap["counts"]
    assert c["usable_long_term"] == 1, c
    assert c["usable_phase"] == 3, c
    assert c["usable"] == c["usable_long_term"] + c["usable_phase"], c
    assert c["phase_usable_enabled"] is True


def test_counts_phase_zero_when_disabled(monkeypatch):
    """关闭时 usable_phase 必须为 0、usable 只等于长期数。"""
    hyps = [_hyp("L2", "persistent", params={"window": 10, "k": 5}),
            _hyp("E3", "emerging", params={"window": 10, "k": 6})]
    snap = _snapshot(monkeypatch, hyps, phase_enabled=False)
    c = snap["counts"]
    assert c["usable_long_term"] == 1
    assert c["usable_phase"] == 0
    assert c["usable"] == 1
    assert c["phase_usable_enabled"] is False


# ---------------------------------------------------------------------------
# 4. tier 图例必须诚实（把研究结论写进界面文案）
# ---------------------------------------------------------------------------

def test_tier_legend_discloses_phase_caveat(monkeypatch):
    """tier 图例里的 usable_phase 必须包含"未获支持/以实测为准"类警示语。

    这是诚实的硬要求：不能让用户以为打开这个开关就等于"找到了规律"。
    """
    snap = _snapshot(monkeypatch, [], phase_enabled=True)
    legend = snap["counts"]["tier_legend"]
    assert "usable_phase" in legend
    text = legend["usable_phase"]
    assert ("未支持" in text) or ("实测" in text), f"阶段档图例缺乏警示：{text}"
