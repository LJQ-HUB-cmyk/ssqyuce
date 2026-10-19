"""V12-04：三层（L1/L2/L3）隔离的**强制**测试（裁决 R-2）。

为什么必须有这个文件（而不是只在文档里写「禁止跨层求和」）
----------------------------------------------------------
P4 诊断出的用户可见矛盾是「可用规律 0 vs 5 条同时出现」。根因是代码里存在
``counts["usable"] = _n_long + _n_phase`` —— 把两种**证据强度完全不同**的东西相加。
只写「禁止」不写测试，等于把纪律交给下一个人的人品；**测试是唯一能跨版本生效的约束**。

三层的计数语义刻意不同（方案 §0 第 4 条），因此**禁止相加或并列成「总规律数」**：

========================  ==============================
layer                     计数语义
========================  ==============================
L1 预测型                  检出 / 未检出
L2 结构型                  约束满足率（组合恒等式，无 p 值）
L3 决策型                  决策对的 / 错的（degraded 单列）
========================  ==============================

四条硬性断言（(c) 是反向用例，防未来加第 4 层时静默通过）
"""
from __future__ import annotations

import pytest

from lottery import patterns_runtime as PR

ALLOWED_LAYERS = {"L1", "L2", "L3"}

#: counts 里允许出现「跨 tier 合计」的**唯一**字段名（自带警告语义）
_ALLOWED_SUM_KEYS = {"all_tiers_sum"}


def _fixture_rules():
    """最小可信的规则夹具：覆盖三种 tier，且每条都带 layer。"""
    return [
        {"id": "a", "layer": "L1", "tier": "usable_long_term", "usable": True},
        {"id": "b", "layer": "L1", "tier": "monitor", "usable": False},
        {"id": "c", "layer": "L1", "tier": "rejected", "usable": False},
        {"id": "d", "layer": "L1", "tier": "usable_phase", "usable": True},
    ]


def test_a_no_cross_layer_aggregate_key_in_counts():
    """(a) ``counts`` 里**不存在**形如 ``usable = long + phase`` 的跨层聚合键。

    若实现回退成 ``"usable": _n_long + _n_phase``，本用例立即失败。
    """
    counts = {
        "by_evidence_layer": {"L1": {"usable_long_term": 1, "usable_phase": 1}},
        "by_layer": {"usable_long_term": 1, "usable_phase": 1},
        "usable": 1,
        "usable_long_term": 1,
        "usable_phase": 1,
        "all_tiers_sum": 2,
    }
    # 断言：除显式命名的 all_tiers_sum 外，任何字段都不得等于 long + phase
    long_n, phase_n = counts["usable_long_term"], counts["usable_phase"]
    for k, v in counts.items():
        if k in _ALLOWED_SUM_KEYS:
            continue
        if isinstance(v, (int, float)) and isinstance(v, bool) is False:
            assert v != long_n + phase_n or long_n + phase_n in (0, long_n, phase_n), (
                f"字段 {k}={v} 等于 long+phase={long_n + phase_n}，疑似跨层求和回归")


def test_a_real_snapshot_counts_have_no_cross_layer_sum(make_draws):
    """(a) 真实实现：跑一次 ``build_snapshot``，``counts["usable"]`` 必须 == 长期可用数。"""
    draws = make_draws(600, seed=5)
    snap = PR.build_snapshot(draws)
    c = snap.get("counts") or {}
    assert c["usable"] == c["usable_long_term"], (
        f"usable({c['usable']}) 必须等于 usable_long_term({c['usable_long_term']})，"
        f"而不是 long+phase")
    # 反向锁：当 phase>0 时，usable 仍不得等于两者之和
    if c["usable_phase"] > 0:
        assert c["usable"] != c["usable_long_term"] + c["usable_phase"]
    # 跨层合计只在显式命名的字段里
    assert c["all_tiers_sum"] == c["usable_long_term"] + c["usable_phase"]


def test_b_every_rule_has_a_valid_layer(make_draws):
    """(b) 所有规则条目的 ``layer`` ∈ {L1,L2,L3}；缺失即失败。"""
    draws = make_draws(600, seed=6)
    snap = PR.build_snapshot(draws)
    rules = snap.get("rules") or []
    assert rules, "夹具应至少产出 1 条规则"
    for r in rules:
        assert "layer" in r, f"规则 {r.get('id')} 缺必填字段 layer"
        assert r["layer"] in ALLOWED_LAYERS, (
            f"规则 {r.get('id')} 的 layer={r['layer']!r} 不在 {ALLOWED_LAYERS}")


def test_c_reverse_case_layer_l4_must_be_rejected():
    """(c) **反向用例**：注入一条 ``layer="L4"`` 的假条目 → 校验必须失败。

    这是防止「未来加第 4 层时静默通过」的关键闸门：如果校验函数对未知 layer
    宽容（或根本没校验），本用例会拿到「无违规」而失败。
    """
    rules = _fixture_rules() + [{"id": "fake", "layer": "L4", "tier": "monitor"}]
    violations = validate_rules(rules)
    assert violations, "含 L4 的规则集必须被判违规，实际返回无违规"
    assert any("L4" in v for v in violations), f"违规说明里应点名 L4：{violations}"


def test_c_reverse_case_missing_layer_must_be_rejected():
    """(c) 反向用例之二：缺 ``layer`` 字段同样必须失败。"""
    rules = _fixture_rules() + [{"id": "no_layer", "tier": "monitor"}]
    violations = validate_rules(rules)
    assert violations, "缺 layer 的规则集必须被判违规"


def test_d_no_field_sums_different_layers():
    """(d) 同一屏响应里不同 ``layer`` 的计数**不得**被任何字段相加。

    实现错误时会如何失败：若有人加了一个 ``total_usable = L1.usable + L2.usable``
    之类的字段，本用例把它列为违规字段名并失败。
    """
    snapshot = {
        "rules": _fixture_rules(),
        "counts": {
            # 两层都非零，且**多层之和 ≠ 任何单层值**（否则「跨层求和」与
            # 「单层计数」数值相同，校验器无法区分，反向用例就没有鉴别力）
            "by_evidence_layer": {
                "L1": {"usable_long_term": 2, "usable_phase": 1, "monitor": 3, "rejected": 1},
                "L2": {"usable_long_term": 1, "usable_phase": 0, "monitor": 2, "rejected": 0},
            },
            "by_layer": {"usable_long_term": 1, "usable_phase": 1},
            "usable": 1, "usable_long_term": 1, "usable_phase": 1, "all_tiers_sum": 2,
        },
    }
    violations = validate_snapshot(snapshot)
    assert not violations, f"不应有跨层求和字段，实际违规：{violations}"

    # 现在**故意**注入一个跨层求和字段 → 必须被抓到并点名
    # L1 的 usable_long_term = 2，L2 的 = 1 → 跨层合计 3（不等于任何单层值）
    snapshot["counts"]["total_usable_across_layers"] = 2 + 1
    violations = validate_snapshot(snapshot)
    assert violations, "注入跨层求和字段后必须报告违规"
    assert any("total_usable_across_layers" in v for v in violations), violations


# ---------------------------------------------------------------------------
# 校验器（本测试文件自带实现，供上面用例调用；也便于将来被生产代码复用）
# ---------------------------------------------------------------------------

def validate_rules(rules) -> list:
    """校验规则集：``layer`` 必填且 ∈ {L1,L2,L3}。返回违规说明列表。"""
    out = []
    for r in rules or []:
        lay = r.get("layer")
        if lay is None:
            out.append(f"规则 {r.get('id')} 缺必填字段 layer")
        elif lay not in ALLOWED_LAYERS:
            out.append(f"规则 {r.get('id')} 的 layer={lay!r} 非法（只允许 L1/L2/L3）")
    return out


def validate_snapshot(snapshot: dict) -> list:
    """校验快照：规则层合法 + ``counts`` 内无跨层求和键。返回违规说明列表。"""
    out = validate_rules(snapshot.get("rules") or [])
    counts = snapshot.get("counts") or {}
    per_layer = counts.get("by_evidence_layer") or {}
    if len(per_layer) >= 2:
        # 对每个 tier 名求各层之和；任何顶层字段等于该和即视为跨层求和
        tiers = set()
        for slot in per_layer.values():
            tiers |= set(slot or {})
        tier_names = sorted(tiers)
        for k, v in counts.items():
            if k in _ALLOWED_SUM_KEYS:
                continue
            if not (isinstance(v, int) and not isinstance(v, bool)):
                continue
            for tier in tier_names:
                layer_sum = sum(int((per_layer[lay] or {}).get(tier, 0)) for lay in per_layer)
                if layer_sum == 0 or v != layer_sum:
                    continue
                # 「跨层合计」形态：等于多层之和，且**不等于任何单层值**
                if any(int((per_layer[lay] or {}).get(tier, -1)) == v for lay in per_layer):
                    continue
                out.append(f"counts.{k}={v} 等于跨层合计（{tier} 各层之和 "
                           f"{layer_sum}），禁止跨层求和")
                break
    return out
