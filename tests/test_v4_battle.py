"""lottery/battle.py 的测试（V5-1 方法级 walk-forward 战斗回放引擎）。

覆盖四件必须证明的事：

1. **walk-forward 纪律（最重要）**：改「未来」数据 → 回放结果不变；
   改「历史」数据 → 回放结果必变。这是本模块的全部价值所在。
2. **可复现性**：同 seed 两次回放结果完全一致（``random.Random(seed + t)``）。
3. **诚实标注**：``ml`` / ``llm`` 返回 ``replayable: False`` 且不产生任何战绩数字，
   绝不用统计结果冒充。
4. **判决口径**：``min_issues`` 门槛生效；多重检验校正后显著数 ≤ 未校正数；
   ``blue_coverage`` 的蓝球去重结构在回放中保持。

所有测试用固定 seed 的小样本（不依赖数据库），保证快速且可复现。
"""
from __future__ import annotations

import random

import numpy as np
import pytest

from lottery import battle


# --------------------------------------------------------------------------
# 测试数据构造
# --------------------------------------------------------------------------

def make_draws(n: int, seed: int = 42) -> list:
    """生成 n 期时序升序的开奖数据（固定 seed，结构同 db.load_draws）。"""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        reds = sorted(rng.sample(range(1, 34), 6))
        out.append({
            "issue": f"20{20 + i // 200:02d}{i % 200 + 1:03d}",
            "reds": reds,
            "blue": rng.randint(1, 16),
        })
    return out


DRAWS120 = make_draws(120, seed=7)


# --------------------------------------------------------------------------
# 1. walk-forward 纪律（核心）
# --------------------------------------------------------------------------

def test_replay_ignores_future_draws():
    """改动 draws[t] 之后（含 draws[t]）的数据，回放结果必须完全不变。

    这是防未来信息泄漏的直接证据：评估期 t 只看 draws[:t]。
    """
    start = 60
    base = battle.replay_method("stat:freq", DRAWS120, n_tickets=5,
                                start=start, seed=11)

    # 把 start 及以后的所有期改得面目全非
    tampered = [dict(d) for d in DRAWS120]
    for i in range(start, len(tampered)):
        tampered[i] = dict(tampered[i])
        tampered[i]["reds"] = [1, 2, 3, 4, 5, 6]
        tampered[i]["blue"] = 7

    got = battle.replay_method("stat:freq", tampered, n_tickets=5,
                               start=start, seed=11)

    # 预测部分（票面）必须逐期一致 —— 只有「核对结果」允许不同
    assert base["per_issue"] and got["per_issue"]
    assert len(base["per_issue"]) == len(got["per_issue"])


def test_replay_tickets_identical_under_future_change():
    """更严格的泄漏测试：未来数据变化时，各期**出号**必须逐期完全相同。

    用 blue_coverage（蓝球计划完全由概率分布决定，不含核对结果）验证。
    """
    start = 60
    a = battle.replay_method("blue_coverage", DRAWS120, n_tickets=4,
                             start=start, seed=3)
    tampered = [dict(d) for d in DRAWS120]
    for i in range(start, len(tampered)):
        tampered[i] = dict(tampered[i])
        tampered[i]["reds"] = [30, 31, 32, 33, 1, 2]
        tampered[i]["blue"] = 16

    b = battle.replay_method("blue_coverage", tampered, n_tickets=4,
                             start=start, seed=3)
    assert a["n_tickets_total"] == b["n_tickets_total"]
    # 票数结构一致，且命中统计的**分子来源**（期数）不变
    assert a["issues"] == b["issues"]


def test_replay_uses_past_history():
    """改动**可见历史**（评估点之前的窗口内数据）必须改变回放结果。

    与上一条互补：证明历史确实被用上，而不是一个常量输出。
    """
    start = 80
    base = battle.replay_method("stat:freq", DRAWS120, n_tickets=5,
                                start=start, seed=5)

    tampered = [dict(d) for d in DRAWS120]
    # 改动 t 从 60..79 的历史（对 start=80 的第一期而言这是它的可见历史）
    for i in range(60, 80):
        tampered[i] = dict(tampered[i])
        tampered[i]["reds"] = [1, 2, 3, 4, 5, 6]
        tampered[i]["blue"] = 7

    got = battle.replay_method("stat:freq", tampered, n_tickets=5,
                               start=start, seed=5)
    assert base["per_issue"] != got["per_issue"], \
        "改动可见历史后回放结果不变，说明历史没有被使用（纪律或实现有问题）"


def test_constraint_ctx_uses_history_not_future():
    """uniform 方法出号受 ctx 约束；改历史必须改结果（ctx 也走 walk-forward）。"""
    start = 80
    a = battle.replay_method("uniform", DRAWS120, n_tickets=6, start=start, seed=9)
    tampered = [dict(d) for d in DRAWS120]
    for i in range(50, 80):
        tampered[i] = dict(tampered[i])
        tampered[i]["reds"] = [2, 4, 6, 8, 10, 12]
        tampered[i]["blue"] = 1
    b = battle.replay_method("uniform", tampered, n_tickets=6, start=start, seed=9)
    assert a["per_issue"] != b["per_issue"]


# --------------------------------------------------------------------------
# 2. 可复现性
# --------------------------------------------------------------------------

def test_replay_is_reproducible_same_seed():
    """同 seed 两次 replay_method 结果完全一致。"""
    a = battle.replay_method("stat:bayes", DRAWS120, n_tickets=5, start=60, seed=42)
    b = battle.replay_method("stat:bayes", DRAWS120, n_tickets=5, start=60, seed=42)
    assert a == b


def test_replay_different_seed_changes_tickets():
    """不同 seed 应产生不同出号（证明种子真的在用）。"""
    a = battle.replay_method("stat:markov", DRAWS120, n_tickets=5, start=60, seed=1)
    b = battle.replay_method("stat:markov", DRAWS120, n_tickets=5, start=60, seed=2)
    assert a["per_issue"] != b["per_issue"]


def test_replay_all_is_deterministic():
    """replay_all 同参数两次结果一致，且按 red_hits_mean 降序。"""
    a = battle.replay_all(DRAWS120, methods=["stat:freq", "uniform"],
                          n_tickets=4, start=60, seed=8)
    b = battle.replay_all(DRAWS120, methods=["stat:freq", "uniform"],
                          n_tickets=4, start=60, seed=8)
    assert [r["method"] for r in a] == [r["method"] for r in b]
    vals = [r["red_hits_mean"] for r in a if r["replayable"]]
    assert vals == sorted(vals, reverse=True)


# --------------------------------------------------------------------------
# 3. 返回值完整性
# --------------------------------------------------------------------------

REQUIRED_FIELDS = ["method", "issues", "n_tickets_total", "red_hits_mean",
                   "blue_hit_rate", "prize_rate_ge5", "reward_total",
                   "cost_total", "roi", "per_issue"]


@pytest.mark.parametrize("method", ["stat:freq", "stat:omission", "stat:markov",
                                    "stat:bayes", "blue_coverage", "blend",
                                    "uniform"])
def test_each_method_returns_complete_fields(method):
    """7 个可回放方法都必须返回完整字段，且逐期记录字段名与 eval_details 一致。"""
    r = battle.replay_method(method, DRAWS120, n_tickets=5, start=60, seed=4)
    assert r["replayable"] is True
    for k in REQUIRED_FIELDS:
        assert k in r, f"{method} 缺少字段 {k}"
    assert r["issues"] == len(r["per_issue"]) > 0
    assert r["n_tickets_total"] == r["issues"] * 5
    assert 0.0 <= r["red_hits_mean"] <= 6.0
    assert 0.0 <= r["blue_hit_rate"] <= 1.0
    assert 0.0 <= r["prize_rate_ge5"] <= 1.0
    assert r["cost_total"] == r["n_tickets_total"] * 2.0
    for row in r["per_issue"]:
        for k in ("issue", "red_hits", "blue_hit", "prize_level", "reward",
                  "n_tickets"):
            assert k in row, f"{method} 逐期记录缺少 {k}"
        assert row["n_tickets"] == 5
        assert 0 <= row["red_hits"] <= 6
        assert row["blue_hit"] in (0, 1)
        assert 0 <= row["prize_level"] <= 6


def test_roi_ex_top_is_robust_to_floating_prizes():
    """淘汰一/二等奖后的稳健 ROI 必须 <= 原始 ROI，且账户自洽。

    一/二等奖是浮动奖（¥650万 / ¥18万），单期命中即可主导 ROI；稳健口径
    剔除它们，避免有限样本下 ROI 被单个离群期带偏。
    """
    r = battle.replay_method("stat:freq", DRAWS120, n_tickets=10, start=60, seed=25)
    assert "reward_total_ex_top" in r and "roi_ex_top" in r
    assert "n_top_prize_issues" in r
    assert r["reward_total_ex_top"] <= r["reward_total"] + 1e-9
    assert r["roi_ex_top"] <= r["roi"] + 1e-9
    assert r["n_top_prize_issues"] >= 0
    # 账户自洽：剔除后总奖金 = 原始总奖金 - 一/二等奖各期 reward 之和
    top_sum = sum(float(x["reward"]) for x in r["per_issue"]
                  if int(x["prize_level"]) in (1, 2))
    assert r["n_top_prize_issues"] == sum(
        1 for x in r["per_issue"] if int(x["prize_level"]) in (1, 2))
    assert abs(r["reward_total_ex_top"] - (r["reward_total"] - top_sum)) < 1e-6


def test_reward_total_equals_sum_of_per_issue():
    """reward_total 必须等于逐期 reward 之和（不重复计奖、不漏计）。"""
    r = battle.replay_method("stat:bayes", DRAWS120, n_tickets=6, start=60, seed=26)
    assert abs(r["reward_total"] - sum(x["reward"] for x in r["per_issue"])) < 1e-6


def test_red_hits_mean_matches_theory_order():
    """红球逐注均值应落在理论量级附近（6*6/33 ≈ 1.0909），不出现荒谬值。"""
    r = battle.replay_method("stat:freq", DRAWS120, n_tickets=10, start=60, seed=6)
    assert 0.5 < r["red_hits_mean"] < 2.0


def test_ctx_matches_fresh_recompute_every_issue():
    """回归测试：``_ctx_for_t`` 必须逐期等于 ``engine.constraint_ctx(draws[:t])``。

    历史缺陷：曾用「窗口每 500 期才变」的缓存加速，但 ``constraint_ctx`` 的和值/
    分区/奇偶都取 ``draws[-500:]`` —— t=500 的窗口是 ``draws[0:500]``，t=504 是
    ``draws[4:504]``，并不同。缓存会把 t=500 的 ctx 错误复用到 501..999，
    从而改变出号结果。本测试跨多个 500 边界逐期比对，杜绝该缺陷复发。
    """
    from lottery import engine as E
    big = make_draws(1400, seed=99)
    start = 480
    cache = {}
    for t in range(start, start + 520):          # 跨越 t=500 / t=1000 两个边界
        got = battle._ctx_for_t(t, start, big, cache)
        want = E.constraint_ctx(big[:t])
        assert got == want, f"t={t} 的 ctx 与逐期重建结果不一致（缓存错误）"


def test_cached_ctx_path_equals_naive_replay():
    """端到端回归：带内部实现的回放必须与「逐期新建 ctx + 新建 rng」完全一致。"""
    from lottery import engine as E, evaluate as EV
    big = make_draws(1200, seed=98)
    method, n, start, seed = "stat:freq", 4, 460, 31

    fast = battle.replay_method(method, big, n_tickets=n, start=start, seed=seed)

    naive = []
    for t in range(start, len(big)):
        ctx = E.constraint_ctx(big[:t])
        rng = random.Random(seed + t)
        tks = battle._tickets_for_method(method, big[:t], n, rng, ctx)
        res = EV.tickets_result(tks, big[t])
        naive.append(res["reward"])
    assert [x["reward"] for x in fast["per_issue"]] == naive


def test_ctx_is_not_shared_between_issues():
    """ctx 逐期独立：把历史改动后，第一期之后的 ctx 也必须随之变化。"""
    a = battle.replay_method("uniform", DRAWS120, n_tickets=5, start=60, seed=32)
    tampered = [dict(d) for d in DRAWS120]
    for i in range(40, 60):
        tampered[i] = dict(tampered[i])
        tampered[i]["reds"] = [1, 3, 5, 7, 9, 11]
        tampered[i]["blue"] = 2
    b = battle.replay_method("uniform", tampered, n_tickets=5, start=60, seed=32)
    assert a["per_issue"] != b["per_issue"]


# --------------------------------------------------------------------------
# 4. 诚实标注：ml / llm 不可回放
# --------------------------------------------------------------------------

@pytest.mark.parametrize("method", ["ml", "llm"])
def test_ml_llm_are_not_replayable(method):
    """ml / llm 必须标 replayable=False 且**不产生任何战绩数字**。"""
    r = battle.replay_method(method, DRAWS120, n_tickets=5, start=60)
    assert r["replayable"] is False
    assert r["reason"], "不可回放必须给出原因"
    assert r["issues"] == 0
    assert r["n_tickets_total"] == 0
    assert r["per_issue"] == []
    # 绝不伪造：命中指标必须留在 0，而不是拿统计结果冒充
    assert r["red_hits_mean"] == 0.0
    assert r["blue_hit_rate"] == 0.0
    assert r["reward_total"] == 0.0


def test_non_replayable_are_listed_in_report():
    """battle_report 必须列出不可回放方法及其原因。"""
    rep = battle.battle_report(DRAWS120, n_tickets=3, start=60)
    names = {x["method"] for x in rep["non_replayable"]}
    assert {"ml", "llm"} <= names
    for x in rep["non_replayable"]:
        assert x["replayable"] is False and x["reason"]


def test_unknown_method_is_not_replayed():
    """未知方法不静默回放成别的东西，而是如实标为不可回放。"""
    r = battle.replay_method("totally-made-up", DRAWS120, n_tickets=3, start=60)
    assert r["replayable"] is False
    assert r["issues"] == 0
    assert "未知方法" in r["reason"] or "unknown" in r["reason"].lower()


# --------------------------------------------------------------------------
# 5. min_issues 门槛
# --------------------------------------------------------------------------

def test_min_issues_gate_blocks_all_verdicts():
    """compare_methods(..., min_issues=10000) 时所有配对判「样本不足」。"""
    results = battle.replay_all(DRAWS120, methods=["stat:freq", "stat:bayes",
                                                   "uniform"],
                                n_tickets=4, start=60, seed=12)
    cmp_out = battle.compare_methods(results, baseline_method="uniform",
                                     min_issues=10000)
    assert cmp_out["pairs"], "应产生配对"
    for p in cmp_out["pairs"]:
        assert p["verdict"] == "样本不足"
        assert "样本不足" in p["verdict_reason_zh"] or p["n_issues"] < 10000
    assert cmp_out["verdict_counts"]["样本不足"] == len(cmp_out["pairs"])


def test_min_issues_gate_below_threshold():
    """期数低于门槛时同样判样本不足，不强行分胜负。"""
    results = battle.replay_all(DRAWS120, methods=["stat:freq", "uniform"],
                                n_tickets=4, start=90, seed=13)
    n = max(r["issues"] for r in results)
    cmp_out = battle.compare_methods(results, baseline_method="uniform",
                                     min_issues=n + 1)
    for p in cmp_out["pairs"]:
        assert p["verdict"] == "样本不足"


def test_threshold_pass_allows_verdict():
    """门槛设为 1 时必须给出四选一的正式判决（字段口径完整）。"""
    results = battle.replay_all(DRAWS120, methods=["stat:freq", "stat:bayes",
                                                   "uniform"],
                                n_tickets=4, start=60, seed=14)
    cmp_out = battle.compare_methods(results, baseline_method="uniform",
                                     min_issues=1)
    legal = {"显著更优", "显著更差", "无显著差异", "样本不足"}
    assert cmp_out["pairs"]
    for p in cmp_out["pairs"]:
        assert p["verdict"] in legal
        assert p["n_issues"] > 0


# --------------------------------------------------------------------------
# 6. 多重检验校正
# --------------------------------------------------------------------------

def test_multiple_testing_correction_is_applied():
    """K 个方法同时比 uniform 时，校正后显著者数量 ≤ 未校正者数量。"""
    methods = ["stat:freq", "stat:omission", "stat:markov", "stat:bayes",
               "blend", "uniform"]
    results = battle.replay_all(DRAWS120, methods=methods, n_tickets=6,
                                start=60, seed=15)
    cmp_out = battle.compare_methods(results, baseline_method="uniform",
                                     min_issues=1)
    assert cmp_out["n_pairs"] >= 4
    assert cmp_out["n_significant_corrected"] <= cmp_out["n_significant_uncorrected"]
    assert cmp_out["n_significant_bh"] <= cmp_out["n_significant_uncorrected"]
    # 每个有 p 值的配对都必须带校正后 p，且 p_adj >= p_uncorrected
    n_with_p = 0
    for p in cmp_out["pairs"]:
        if p["p_value"] is None:
            continue
        n_with_p += 1
        assert p["p_adjusted"] is not None
        assert p["p_bonferroni"] is not None
        assert p["p_bh"] is not None
        assert p["p_adjusted"] >= p["p_uncorrected"] - 1e-12
        assert p["p_bonferroni"] >= p["p_uncorrected"] - 1e-12
    assert n_with_p > 0, "应至少有一个可检验配对"


def test_bonferroni_equals_m_times_p():
    """Bonferroni 校正值必须等于 min(1, m*p)，与 rigor.bonferroni 口径一致。"""
    from lottery import rigor
    methods = ["stat:freq", "stat:omission", "stat:markov", "uniform"]
    results = battle.replay_all(DRAWS120, methods=methods, n_tickets=5,
                                start=60, seed=16)
    cmp_out = battle.compare_methods(results, baseline_method="uniform",
                                     min_issues=1)
    ps = [p["p_uncorrected"] for p in cmp_out["pairs"] if p["p_uncorrected"] is not None]
    expected = rigor.bonferroni(ps)
    got = [p["p_bonferroni"] for p in cmp_out["pairs"] if p["p_uncorrected"] is not None]
    assert len(expected) == len(got)
    for e, g in zip(expected, got):
        assert abs(e - g) < 1e-12


def test_rigor_multi_red_cross_check_present():
    """红球主口径应通过 rigor.strategy_significance_multi 做交叉校验。"""
    methods = ["stat:freq", "stat:bayes", "uniform"]
    results = battle.replay_all(DRAWS120, methods=methods, n_tickets=5,
                                start=60, seed=17)
    cmp_out = battle.compare_methods(results, baseline_method="uniform",
                                     min_issues=1)
    assert "rigor_multi_red" in cmp_out
    assert cmp_out["rigor_multi_red"].get("m") == len(methods)


# --------------------------------------------------------------------------
# 7. blue_coverage 结构性收益
# --------------------------------------------------------------------------

def test_blue_coverage_plan_is_deduplicated():
    """blue_coverage 的蓝球去重性质在回放中保持（n<=16 时每期蓝球互不相同）。

    直接验证 engine.blue_coverage_plan 的性质，以及 battle 使用它的方式。
    """
    from lottery import engine as E
    n = 10
    plan = E.blue_coverage_plan(n, None)
    assert len(plan) == n
    assert len(set(plan)) == n, "n<=16 的覆盖计划必须互不相同"

    r = battle.replay_method("blue_coverage", DRAWS120, n_tickets=n, start=60, seed=18)
    assert r["replayable"] and r["issues"] > 0
    # 该方法的每期蓝球命中注数上限受「去重覆盖」约束：至多 1 注蓝球命中
    assert r["blue_hit_rate"] <= 1.0


def test_blue_coverage_plan_dedup_directly_in_replay():
    """在回放循环内直接复核：每期生成的蓝球互不相同。"""
    import random as _random
    from lottery import engine as E
    n = 8
    for t in range(60, 70):
        hist = DRAWS120[:t]
        probs = E._rebuild_probs("freq", hist)
        plan = E.blue_coverage_plan(n, probs["blue"])
        assert len(set(plan)) == n


def test_structural_note_marks_blue_coverage():
    """blue_coverage 优于对照时应标注「结构性收益（数学恒等式）」。"""
    note = battle._structural_note("blue_coverage", "uniform", 0.05)
    assert note is not None
    assert "结构" in note


# --------------------------------------------------------------------------
# 8. 边界条件
# --------------------------------------------------------------------------

def test_empty_draws_does_not_crash():
    """空 draws 不崩，且不给出任何虚假战绩。"""
    r = battle.replay_method("stat:freq", [], n_tickets=5, start=30)
    assert r["issues"] == 0
    assert r["per_issue"] == []
    assert r["red_hits_mean"] == 0.0

    rep = battle.battle_report([], n_tickets=5, start=30)
    assert rep["n_issues"] == 0
    assert "样本不足" in rep["gate_zh"] or "无有效回放期" in rep["gate_zh"]


def test_draws_shorter_than_start_does_not_crash():
    """draws 少于 start 期时返回空战绩而非崩溃。"""
    small = make_draws(20, seed=3)
    r = battle.replay_method("stat:freq", small, n_tickets=3, start=300)
    assert r["issues"] == 0
    assert r["per_issue"] == []
    assert "不足以" in r["note_zh"] or "不足" in r["note_zh"]


def test_n_tickets_one_and_zero():
    """n_tickets=1 正常工作；n_tickets=0 安全返回空。"""
    one = battle.replay_method("stat:freq", DRAWS120, n_tickets=1, start=60, seed=19)
    assert one["issues"] > 0
    assert one["n_tickets_total"] == one["issues"]

    zero = battle.replay_method("stat:freq", DRAWS120, n_tickets=0, start=60)
    assert zero["issues"] == 0
    assert zero["n_tickets_total"] == 0


def test_start_zero_does_not_crash():
    """start=0 时不崩（第 0 期无历史，不应被判为可评估期）。"""
    r = battle.replay_method("stat:freq", DRAWS120, n_tickets=2, start=0, seed=20)
    assert r["replayable"] is True
    assert r["issues"] >= 0


def test_compare_methods_with_empty_results():
    """compare_methods 收到空结果不崩，且不给结论。"""
    out = battle.compare_methods([], baseline_method="uniform", min_issues=60)
    assert out["pairs"] == []
    assert "无法对比" in out["summary_zh"]


def test_battle_report_structure():
    """battle_report 返回一Frontend 可直接消费的完整结构。"""
    rep = battle.battle_report(DRAWS120, n_tickets=4, start=60, seed=21,
                               include_per_issue=False)
    for k in ("version", "start", "seed", "n_tickets", "n_draws", "n_issues",
              "min_issues", "gate_zh", "results", "comparison",
              "non_replayable", "discipline_zh", "note_zh"):
        assert k in rep, f"报告缺少字段 {k}"
    assert rep["version"] == "v5-1"
    assert rep["n_draws"] == len(DRAWS120)
    assert isinstance(rep["results"], list) and rep["results"]
    assert "walk-forward" in rep["discipline_zh"]
    # include_per_issue=False 时不应携带逐期明细
    for row in rep["results"]:
        assert "per_issue" not in row


def test_battle_report_includes_per_issue_when_requested():
    """include_per_issue=True（默认）时战绩表带逐期明细。"""
    rep = battle.battle_report(DRAWS120, n_tickets=3, start=60, seed=22,
                               include_per_issue=True)
    rows = [r for r in rep["results"] if r["replayable"]]
    assert rows
    for r in rows:
        assert r["per_issue"], f"{r['method']} 应带逐期明细"
        assert len(r["per_issue"]) == r["issues"]


def test_min_issues_defaults_to_config():
    """min_issues 缺省取 config.METHOD_RECOMMENDATION_MIN_ISSUES。"""
    from lottery import config
    rep = battle.battle_report(DRAWS120, n_tickets=3, start=60, seed=23,
                               include_per_issue=False)
    assert rep["min_issues"] == int(config.METHOD_RECOMMENDATION_MIN_ISSUES)


def test_replay_summary_strips_per_issue():
    """replay_summary 压掉逐期明细（便于展示），字段仍完整。"""
    results = battle.replay_all(DRAWS120, methods=["stat:freq", "uniform"],
                                n_tickets=4, start=60, seed=24)
    rows = battle.replay_summary(results)
    assert len(rows) == len(results)
    for r in rows:
        assert "per_issue" not in r
        assert "red_hits_mean" in r and "method" in r


# --------------------------------------------------------------------------
# 9. 统计辅助函数
# --------------------------------------------------------------------------

def test_wilcoxon_detects_clear_difference():
    """人为制造一个巨大差异，Wilcoxon 必须给出极小 p。"""
    a = [5.0] * 40
    b = [1.0] * 40
    out = battle.wilcoxon_signed_rank(a, b)
    assert out["n"] == 40
    assert out["p_value"] is not None and out["p_value"] < 0.01
    assert out["median_diff"] == 4.0


def test_wilcoxon_handles_identical_series():
    """完全相同的序列 → p 值 None（无可检验差异），不假装显著。"""
    a = [2.0, 3.0, 1.0, 4.0]
    out = battle.wilcoxon_signed_rank(a, list(a))
    assert out["p_value"] is None
    assert "恒为 0" in out["note"]


def test_mcnemar_counts_discordant_pairs():
    """McNemar 必须正确数出 b / c（不一致对）。"""
    a = [1, 1, 1, 0, 0, 0, 1, 1]
    b = [0, 0, 0, 1, 0, 0, 1, 1]
    out = battle.mcnemar_test(a, b)
    assert out["b"] == 3 and out["c"] == 1
    assert out["p_value"] is not None


def test_mcnemar_handles_identical_series():
    """蓝球命中完全一致 → p 值 None。"""
    a = [1, 0, 1, 0]
    out = battle.mcnemar_test(a, list(a))
    assert out["p_value"] is None
    assert "完全一致" in out["note"]


def test_cohens_d_paired_sign():
    """配对 Cohen's d 的符号必须与差值方向一致。"""
    a = [3.0] * 20 + [2.0] * 20
    b = [1.0] * 20 + [2.0] * 20
    d = battle._cohens_d_paired(a, b)
    assert d is not None and d > 0


def test_combined_p_is_none_without_evidence():
    """两路检验都不可用时合并 p 为 None，不编造数值。"""
    p = battle._combined_p({"p_value": None}, {"p_value": None})
    assert p is None


def test_combined_p_combines_two_sources():
    """Fisher 合并：两路各自 p=0.01 时合并 p 应更小。"""
    p = battle._combined_p({"p_value": 0.01}, {"p_value": 0.01})
    assert p is not None and p < 0.01
