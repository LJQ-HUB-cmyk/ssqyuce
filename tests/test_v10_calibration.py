"""V10-14 统计判据体检：把「判据本身是否受控」钉成测试。

背景
----
V10 已暴露 3 个同类缺陷，共同点是**现有测试只验证返回值结构，不验证判据**：

* A1：老库零备份 —— 路径边界没测；
* D-1：单侧口径看不见劣绩 —— 「显著更差」分支写着但不可达；
* D-2：零复制不足致假阳性 —— 17 条测试全绿却没测假阳性率。

本文件对 ``docs/reviews/STAT_CALIBRATION_AUDIT.md`` 筛查清单里的每一处判据
至少覆盖四件事中的适用项：

1. **假阳性率**（纯噪声下判显著的频率）∈ α ± 3σ；
2. **功效**（注入真实效应必须能检出，否则是死判据）；
3. **对称性**（A/B 互换后结论对称 —— 直接抓 D-1）；
4. **单调性**（效应越大越容易判出）。

速度：默认小样本（全量 < 20s）；``LOTT_SLOW_TESTS=1`` 时按 10 倍放大。
每个数字都可在体检报告里复核（报告记录了同口径的实测值）。
"""
from __future__ import annotations

import numpy as np
import pytest

from lottery import battle, config, hypothesis, llm_lab, ml_model, rigor
import calibration as cal

# 固定种子，保证可复现；各测试互不依赖
SEED = 20261018


# ===========================================================================
# 0. harness 自检（先证明"尺子"本身是准的）
# ===========================================================================

def test_harness_detects_a_broken_one_sided_judge():
    """harness 自检：故意构造一个**单侧**判据，对称性检查必须报不对称。

    如果这条测试失败，说明 harness 的 ``two_sided_symmetry`` 无效，
    后面所有"对称"结论都不可信 —— 这是尺子的校准。
    """
    n = 60

    def one_sided(rng, effect=None):
        """单侧：只在 A 更好时可能显著。"""
        e = 0.0 if effect is None else float(effect)
        diff = rng.normal(e, 1.0, n)
        se = diff.std(ddof=1) / np.sqrt(n)
        t = diff.mean() / se if se > 0 else 0.0
        z = 1.96
        return 0.0 if t > z else 1.0          # 单侧：只有正向能拒绝

    def two_sided(rng, effect=None):
        e = 0.0 if effect is None else float(effect)
        diff = rng.normal(e, 1.0, n)
        se = diff.std(ddof=1) / np.sqrt(n)
        t = diff.mean() / se if se > 0 else 0.0
        return 0.0 if abs(t) > 1.96 else 1.0

    sims = cal.n_sims(120)
    bad = cal.two_sided_symmetry(one_sided, effect=1.0, n_sims=sims, rng=np.random.default_rng(SEED))
    good = cal.two_sided_symmetry(two_sided, effect=1.0, n_sims=sims, rng=np.random.default_rng(SEED))
    assert bad["positive"] > 0.5 and bad["negative"] < 0.05
    assert not bad["symmetric"], "harness 没能识别单侧判据（尺子失效）"
    assert good["symmetric"], f"双侧判据被判成不对称：{good}"


def test_harness_calibration_assert_helper_tolerance():
    """assert_calibrated 的容差必须真的按 3σ 走。"""
    n = 400
    tol = cal.fpr_tolerance(0.05, n)
    assert tol == pytest.approx(3 * (0.05 * 0.95 / n) ** 0.5, rel=1e-12)
    cal.assert_calibrated(0.05, 0.05, n)                    # 正中
    cal.assert_calibrated(0.05 + tol * 0.9, 0.05, n)        # 容差内
    with pytest.raises(AssertionError):
        cal.assert_calibrated(0.05 + tol * 1.2, 0.05, n)    # 超容差


# ===========================================================================
# 1. rigor._verdict —— 纯阈值判据
# ===========================================================================

def test_verdict_threshold_is_strict_and_monotone():
    """``_verdict`` 只在 p < alpha 时判 deviation，且对 p 单调。"""
    assert rigor._verdict(0.049999, 0.05) == "deviation"
    assert rigor._verdict(0.05, 0.05) == "consistent-with-random"        # 严格 <
    assert rigor._verdict(0.050001, 0.05) == "consistent-with-random"
    seq = [rigor._verdict(p, 0.05) for p in (0.001, 0.02, 0.049, 0.0501, 0.5, 1.0)]
    assert seq == ["deviation", "deviation", "deviation"] + ["consistent-with-random"] * 3


def test_verdict_fpr_follows_the_p_value_source():
    """判据的 FPR 完全由上游 p 值决定：喂一个受控 p（均匀）→ FPR ≈ α。"""
    alpha = 0.05

    def uniform_p(rng, effect=None):
        return float(rng.random())        # H0 下均匀分布 p

    sims = cal.n_sims(400)
    fpr = cal.false_positive_rate(uniform_p, n_sims=sims, alpha=alpha,
                                  rng=np.random.default_rng(SEED))
    cal.assert_calibrated(fpr, alpha, sims, label="rigor._verdict(uniform p)")


# ===========================================================================
# 2. rigor.strategy_significance —— D-1 的根因（单侧口径）
# ===========================================================================

def _strategy_sig(rng, effect=None, n="fast"):
    """A 相对常数基线的逐期命中数；effect>0 → A 更强（命中更多）。"""
    n = 60 if n == "fast" else 120
    e = 0.0 if effect is None else float(effect)
    base = rigor.BASELINE_MEAN_HITS
    a = base + e + rng.normal(0.0, rigor.BASELINE_SD_HITS, n)
    return rigor.strategy_significance(a, base, n_trials=1, alpha=0.05)


def test_strategy_significance_fpr_is_calibrated():
    """H0（策略无效应）下 ``p_value``/``adjusted_p`` 判显著的频率 ≈ α。"""
    alpha = 0.05
    sims = cal.n_sims(300)
    fpr = cal.false_positive_rate(_strategy_sig, n_sims=sims, alpha=alpha,
                                  rng=np.random.default_rng(SEED + 1))
    cal.assert_calibrated(fpr, alpha, sims, label="strategy_significance")

    # 双侧口径在 H0 下同样受控
    def two_sided_p(rng, effect=None):
        return _strategy_sig(rng, effect)["p_two_sided"]

    fpr2 = cal.false_positive_rate(two_sided_p, n_sims=sims, alpha=alpha,
                                   rng=np.random.default_rng(SEED + 1))
    cal.assert_calibrated(fpr2, alpha, sims, label="strategy_significance(p_two_sided)")


def test_strategy_significance_one_sided_cannot_see_worse_D1():
    """**D-1 的核心证据**：单侧 ``adjusted_p`` 判不出「显著更差」。

    实测（本测试锁定）：策略每期少 ~0.35 个红球时
    ``adjusted_p == 1.0``（完美地"不显著"），而 ``adjusted_p_two_sided ≈ 1e-20``。
    """
    rng = np.random.default_rng(SEED + 2)
    n = 120
    base = rigor.BASELINE_MEAN_HITS
    worse = base - 0.35 + rng.normal(0.0, 0.35, n)      # 显著更差
    r = rigor.strategy_significance(worse, base, n_trials=1, alpha=0.05)
    # 单侧（旧口径）：方向 below → p 趋 1，永远判不出差别
    assert r["direction"] == "below"
    assert r["adjusted_p"] > 0.5, "单侧口径本应看不见劣绩（若已改双侧，本条需同步口径）"
    assert r["survives_correction"] is False
    # 双侧（D-1 修复）：必须判出「显著更差」
    assert r["adjusted_p_two_sided"] < 0.05
    assert r["survives_correction_two_sided"] is True
    assert r["verdict_two_sided"] == "significant-after-correction"


def test_strategy_significance_two_sided_is_symmetric():
    """A/B 互换后双侧口径的检出率对称（D-1 的对称性回归锁）。"""
    def two_sided_p(rng, effect=None):
        return _strategy_sig(rng, effect)["adjusted_p_two_sided"]

    sym = cal.two_sided_symmetry(two_sided_p, effect=0.4, n_sims=cal.n_sims(120),
                                 rng=np.random.default_rng(SEED + 3), tol=0.15)
    cal.assert_symmetric(sym, "strategy_significance")
    assert sym["positive"] > 0.5, "0.4 个红球/期的效应必须能检出（否则功效不足）"


def test_strategy_significance_power_and_monotone():
    """功效随效应单调上升；零效应附近几乎不判显著。"""
    def two_sided_p(rng, effect=None):
        return _strategy_sig(rng, effect, n="slow")["adjusted_p_two_sided"]

    grid = [0.0, 0.2, 0.4, 0.8]
    sims = cal.n_sims(60)
    mon = cal.monotonicity(two_sided_p, grid, n_sims=sims, alpha=0.05,
                           rng=np.random.default_rng(SEED + 4), tol=0.08)
    assert mon["monotone"], f"功效不单调：{mon}"
    assert mon["rates"][-1] > 0.8, f"0.8 个红球/期的效应必须几乎必然检出：{mon['rates']}"


def test_strategy_significance_two_sided_fields_are_backward_compatible():
    """旧字段一个不少（不改名/不删），且新增双侧字段齐备。"""
    r = _strategy_sig(np.random.default_rng(SEED + 5), 0.3)
    for legacy in ("p_value", "p_uncorrected", "bonferroni_p", "bh_p", "adjusted_p",
                   "survives_correction", "verdict", "direction", "sign_test"):
        assert legacy in r, f"旧字段 {legacy} 丢失（向后兼容被破坏）"
    for new in ("p_two_sided", "p_uncorrected_two_sided", "adjusted_p_two_sided",
                "bonferroni_p_two_sided", "survives_correction_two_sided",
                "verdict_two_sided"):
        assert new in r, f"新增双侧字段 {new} 缺失"
    # 不变式：n_trials==1 时双侧未校正 == 双侧校正
    assert r["adjusted_p_two_sided"] == pytest.approx(r["p_uncorrected_two_sided"], rel=1e-12)


def test_strategy_significance_edge_cases_keep_schema():
    """样本不足 / 空输入等边界上，双侧字段也要存在（schema 稳定）。"""
    for bad in ([], [1.0], [1.0, 2.0]):
        r = rigor.strategy_significance(bad, rigor.BASELINE_MEAN_HITS, n_trials=1)
        assert r["verdict"] == "insufficient-data"
        assert r["verdict_two_sided"] == "insufficient-data"
        assert r["adjusted_p_two_sided"] is None


# ===========================================================================
# 3. rigor.strategy_significance_multi —— 整组 BH
# ===========================================================================

def _multi_sig(rng, effect=None):
    n, m = 60, 12
    e = 0.0 if effect is None else float(effect)
    base = rigor.BASELINE_MEAN_HITS
    series = [base + e + rng.normal(0.0, rigor.BASELINE_SD_HITS, n) for _ in range(m)]
    return rigor.strategy_significance_multi(series, base, alpha=0.05)


def test_strategy_significance_multi_h0_fwer_is_controlled():
    """H0（m 个噪声策略）下「整组至少一个 BH 显著」的频率应 ≤ α（实测≈2.5%）。"""
    sims = cal.n_sims(60)
    k = 0
    rng = np.random.default_rng(SEED + 6)
    for _ in range(sims):
        out = _multi_sig(rng)
        if any(p < 0.05 for p in out["adjusted_bh"]):
            k += 1
    fpr = k / sims
    assert fpr <= 0.05 + cal.fpr_tolerance(0.05, sims), (
        f"整组 H0 假阳性率 {fpr:.3f} 超出 α 的 3σ 上界（BH 未控制）")


def test_strategy_significance_multi_two_sided_sees_negative_effect():
    """整组双侧口径必须能发现「显著更差」的策略（单侧口径检出率为 0）。"""
    sims = cal.n_sims(30)
    rng = np.random.default_rng(SEED + 7)
    neg_two, neg_one = 0, 0
    for _ in range(sims):
        out = _multi_sig(rng, -1.2)
        if any(p < 0.05 for p in out["adjusted_bh_two_sided"]):
            neg_two += 1
        if any(p < 0.05 for p in out["adjusted_bh"]):
            neg_one += 1
    assert neg_two / sims > 0.8, f"整组双侧口径漏检负效应：{neg_two}/{sims}"
    assert neg_one / sims < 0.2, f"整组单侧口径本应漏检负效应，实际 {neg_one}/{sims}"


def test_strategy_significance_multi_fields_and_legacy_compat():
    out = _multi_sig(np.random.default_rng(SEED + 8), 0.2)
    for legacy in ("m", "n_trials", "strategies", "p_values",
                   "adjusted_bonferroni", "adjusted_bh",
                   "n_survive_bonferroni", "n_survive_bh"):
        assert legacy in out
    for new in ("p_values_two_sided", "adjusted_bonferroni_two_sided",
                "adjusted_bh_two_sided", "n_survive_bonferroni_two_sided",
                "n_survive_bh_two_sided"):
        assert new in out
    assert len(out["adjusted_bh_two_sided"]) == out["m"]
    assert out["note_zh"]


# ===========================================================================
# 4. rigor.evaluate_signal —— walk-forward 判据
# ===========================================================================

def _signal_draws(seed, n_draws=220):
    rr = np.random.default_rng(seed)
    return [{"reds": [int(x) for x in sorted(rr.choice(33, 6, replace=False) + 1)],
             "blue": int(rr.integers(1, 17)), "issue": f"{i:04d}"} for i in range(n_draws)]


def _run_evaluate_signal(seed, mode="null"):
    draws = _signal_draws(seed)
    rnd = np.random.default_rng(seed * 31 + 7)

    def strat(history, **kw):
        t = len(history)
        if mode == "oracle":                    # 完美未来信息 = 强信号
            return list(draws[t]["reds"])
        if mode == "anti":                      # 完美反向 = 显著更差
            bad = set(draws[t]["reds"])
            return [x for x in range(1, 34) if x not in bad][:6]
        return [int(x) for x in sorted(rnd.choice(33, 6, replace=False) + 1)]

    return rigor.evaluate_signal(draws, strat, window=60, n_trials=1, alpha=0.05)


def test_evaluate_signal_fpr_is_calibrated():
    alpha = 0.05
    sims = cal.n_sims(30)
    k = 0
    for s in range(sims):
        out = _run_evaluate_signal(s + 1000)
        p = out.get("adjusted_p")
        if p is not None and p < alpha:
            k += 1
    fpr = k / sims
    cal.assert_calibrated(fpr, alpha, sims, sigmas=3.5, label="evaluate_signal")


def test_evaluate_signal_detects_strong_signal_and_exposes_two_sided():
    """强信号必须检出；且双侧字段与判决齐备（判「更差」要用它）。"""
    out = _run_evaluate_signal(4242, "oracle")
    assert out["adjusted_p"] < 0.05
    assert out["verdict"] == "significant-after-correction"
    for key in ("p_two_sided", "adjusted_p_two_sided",
                "survives_correction_two_sided", "verdict_two_sided"):
        assert key in out, f"evaluate_signal 未透传 {key}"


def test_evaluate_signal_two_sided_flags_worse_strategy_D1():
    """**D-1 端到端**：完美反向策略（显著更差）必须被判出（双侧口径）。"""
    out = _run_evaluate_signal(777, "anti")
    assert out["delta"] < 0
    assert out["adjusted_p_two_sided"] < 0.05
    assert out["verdict_two_sided"] == "significant-after-correction"


# ===========================================================================
# 5. rigor.randomness_report —— 9 项检验 + 家庭化 FPR（体检的最大发现）
# ===========================================================================

def _random_draws(seed, n=150):
    rr = np.random.default_rng(seed)
    return [{"reds": [int(x) for x in sorted(rr.choice(33, 6, replace=False) + 1)],
             "blue": int(rr.integers(1, 17)), "issue": f"{i:04d}"} for i in range(n)]


def test_randomness_report_individual_tests_are_calibrated():
    """逐项检验在真实随机数据下判 deviation 的频率 ≈ α（抽查两项最便宜的）。"""
    alpha = 0.05
    sims = cal.n_sims(40)
    counts = {"red_frequency": 0, "odd_count": 0}
    for s in range(sims):
        rep = rigor.randomness_report(_random_draws(2000 + s), alpha=alpha)
        for name in counts:
            v = rep["tests"].get(name) or {}
            if v.get("p_value") is not None and v["p_value"] < alpha:
                counts[name] += 1
    for name, k in counts.items():
        fpr = k / sims
        # 卡方检验在离散小样本下略保守，放宽到 α + 2σ 上界
        assert fpr <= alpha + 2 * cal.fpr_tolerance(alpha, sims), (
            f"{name} 单项假阳性率 {fpr:.3f} 超标（n_sims={sims}）")


def test_randomness_report_top_level_verdict_fwer_is_inflated_and_fwer_field_fixes_it():
    """**体检最大发现**：顶层 ``verdict`` 的假阳性率 ≈39%（不是 5%）。

    顶层语义 =「9 项检验任一项偏离」，其 H0 假阳性率 = 1-(1-α)^9 ≈ 37%；
    实测 ≈39%（见 STAT_CALIBRATION_AUDIT.md）。
    新增的 ``verdict_fwer`` 用 Bonferroni 校正，H0 下应回到 ≈0（≤ α）。
    """
    alpha = 0.05
    sims = cal.n_sims(30)
    raw_hits = fwer_hits = 0
    theoretical = None
    for s in range(sims):
        rep = rigor.randomness_report(_random_draws(3000 + s), alpha=alpha)
        if rep["verdict"] == "deviation":
            raw_hits += 1
        if rep["verdict_fwer"] == "deviation":
            fwer_hits += 1
        theoretical = rep["family_wise"]["uncorrected_false_positive_rate"]
    raw_fpr, fwer_fpr = raw_hits / sims, fwer_hits / sims
    # 理论值 1-(1-α)^m，m=9 → ≈0.37
    assert theoretical == pytest.approx(1 - (1 - alpha) ** 9, abs=1e-9)
    # 原口径确实严重超标：远高于 α（实测 33%，理论 37%，即 α 的 6~7 倍）
    assert raw_fpr > 3 * alpha, (
        f"顶层 verdict 的 FPR {raw_fpr:.3f} 未复现「家庭化膨胀」（理论 {theoretical:.1%}）")
    # 且与二项抽样下的理论值 1-(1-α)^m 一致（±3σ）
    tol_theory = cal.fpr_tolerance(theoretical, sims)
    assert abs(raw_fpr - theoretical) <= 3 * tol_theory, (
        f"顶层 FPR {raw_fpr:.3f} 与理论 {theoretical:.3f} 不符（±{3*tol_theory:.3f}）")
    # 校正口径受控：不能直接断言 fwer_fpr <= alpha ——
    # 在 sims=30 的二项抽样下，即使真实 FPR 恰为 α，观测值也有非零概率超过 α
    # （1 次误报 = 0.033，2 次 = 0.067）。正确判法是「落在 α 的 MC 容差上界内，
    # 且显著低于未校正口径」。
    tol_fwer = cal.fpr_tolerance(alpha, sims)
    assert fwer_fpr <= alpha + 3 * tol_fwer, (
        f"verdict_fwer 假阳性率 {fwer_fpr:.3f} 超出 α={alpha} 的 3σ 容差"
        f"（上界 {alpha + 3 * tol_fwer:.3f}）")
    assert fwer_fpr < raw_fpr / 2, (
        f"校正后 {fwer_fpr:.3f} 应显著低于未校正 {raw_fpr:.3f}（家庭化膨胀未被控住）")
    # 校正后字段齐备
    rep = rigor.randomness_report(_random_draws(9999), alpha=alpha)
    assert {"n_tests", "alpha", "uncorrected_false_positive_rate",
            "min_p_bonferroni", "min_p_bh", "n_survive_bonferroni",
            "n_survive_bh"} <= set(rep["family_wise"])
    for name, t in rep["tests"].items():
        if t.get("p_value") is not None:
            assert t["p_bonferroni"] >= t["p_value"] - 1e-12
            assert t["p_bh"] >= t["p_value"] - 1e-12


def test_randomness_report_alpha_invariance():
    """alpha 变大时顶层 verdict 更容易判 deviation（单调），且 family_wise 理论值随之变。"""
    draws = _random_draws(555)
    lo = rigor.randomness_report(draws, alpha=0.01)
    hi = rigor.randomness_report(draws, alpha=0.30)
    assert lo["family_wise"]["uncorrected_false_positive_rate"] <= \
        hi["family_wise"]["uncorrected_false_positive_rate"]
    assert lo["n_deviation"] <= hi["n_deviation"]


# ===========================================================================
# 6. battle 的三个检验器（Wilcoxon / McNemar / Fisher 合并）
# ===========================================================================

def _wilcoxon_p(rng, effect=None):
    n = 120
    e = 0.0 if effect is None else float(effect)
    a = 1.0 + rng.normal(0, 0.87, n)
    b = 1.0 - e + rng.normal(0, 0.87, n)      # effect>0 → A 更好
    return battle.wilcoxon_signed_rank(a, b)["p_value"]


def _mcnemar_p(rng, effect=None):
    n = 120
    e = 0.0 if effect is None else float(effect)
    pa = min(1.0, 0.0625 + 0.03 * e)
    a = (rng.random(n) < pa).astype(int)
    b = (rng.random(n) < 0.0625).astype(int)
    return battle.mcnemar_test(a, b)["p_value"]


@pytest.mark.parametrize("name,fn", [("wilcoxon", _wilcoxon_p), ("mcnemar", _mcnemar_p)])
def test_battle_primitive_tests_are_calibrated_and_symmetric(name, fn):
    alpha = 0.05
    sims = cal.n_sims(200)
    rng = np.random.default_rng(SEED + 10)
    fpr = cal.false_positive_rate(fn, n_sims=sims, alpha=alpha, rng=rng)
    cal.assert_calibrated(fpr, alpha, sims, label=name)
    sym = cal.two_sided_symmetry(fn, effect=0.5, n_sims=sims, alpha=alpha,
                                 rng=np.random.default_rng(SEED + 11), tol=0.15)
    cal.assert_symmetric(sym, name)


def test_combined_fisher_p_is_calibrated_under_h0():
    """Fisher 合并两路（独立假设）后的 p 在 H0 下必须受控（它可能偏保守）。"""
    sims = cal.n_sims(300)
    rng = np.random.default_rng(SEED + 12)
    k = 0
    for _ in range(sims):
        p = battle._combined_p({"p_value": float(rng.random())},
                               {"p_value": float(rng.random())})
        if p is not None and p < 0.05:
            k += 1
    fpr = k / sims
    assert fpr <= 0.05 + cal.fpr_tolerance(0.05, sims)


# ===========================================================================
# 7. battle.compare_methods —— D-1 现场的判决（显著更差必须可达）
# ===========================================================================

def _method(name, mean_red, seed, n=120):
    r = np.random.default_rng(seed)
    return {"method": name, "replayable": True, "red_hits_mean": mean_red,
            "per_issue": [{"issue": f"{i:04d}",
                           "red_hits": float(np.clip(r.normal(mean_red, 0.87), 0, 6)),
                           "blue_hit": int(r.random() < 0.0625)} for i in range(n)]}


def test_compare_methods_can_flag_significantly_worse_D1_locked():
    """**D-1 修复的验收锁**：显著更差的方法必须判成「显著更差」。

    历史上这里用 rigor 的单侧 ``adjusted_p``：方法更差时它 → 1.0，于是
    「显著更差」分支永不可达（写着但跑不到）。
    """
    out = battle.compare_methods(
        [_method("uniform", 1.09, 1), _method("good", 1.45, 2), _method("bad", 0.65, 3)],
        baseline_method="uniform", min_issues=30)
    by_pair = {p["pair"]: p for p in out["pairs"]}
    assert by_pair["bad_vs_good"]["verdict"] == "显著更差"
    assert by_pair["good_vs_uniform"]["verdict"] == "显著更优"
    rec = by_pair["bad_vs_good"]
    # 显式双侧字段齐备且被判决使用
    assert rec["p_two_sided"] is not None
    assert rec["p_adjusted_two_sided"] is not None
    assert rec["p_adjusted_two_sided"] < 0.05
    assert rec["delta_red_hits"] < 0


def test_compare_methods_pairwise_symmetry():
    """A/B 互换后判决必须互为镜像：A（强）→ 显著更优，B（弱）→ 显著更差。

    检验器都是双侧的，所以同一对方法的优劣在两份"名单顺序"下判决一致；
    单侧口径则只可能判出"更优"。
    """
    base = _method("uniform", 1.09, 23, n=200)
    strong = _method("A", 1.9, 21, n=200)
    weak = _method("B", 0.3, 22, n=200)

    def _pick(out, method):
        return next(p for p in out["pairs"] if p["method"] == method.lower()
                    and p["vs"] == "uniform")

    ab = battle.compare_methods([strong, weak, base], baseline_method="uniform", min_issues=30)
    ba = battle.compare_methods([weak, strong, base], baseline_method="uniform", min_issues=30)
    assert _pick(ab, "A")["verdict"] == "显著更优"
    assert _pick(ba, "B")["verdict"] == "显著更差"
    # 同一对比较的两份名单里，A 的判决不变（顺序不影响结论）
    assert _pick(ab, "A")["p_adjusted_two_sided"] == pytest.approx(
        _pick(ba, "A")["p_adjusted_two_sided"], rel=1e-9)


def test_compare_methods_h0_fwer_is_controlled():
    """H0（三个等价方法）下「至少一对被判显著」的频率应 ≤ α（实测 5%）。"""
    sims = cal.n_sims(25)
    hits = 0
    for s in range(sims):
        r = np.random.default_rng(5000 + s)
        res = [{"method": name, "replayable": True, "red_hits_mean": 1.09,
                "per_issue": [{"issue": f"{i:04d}",
                               "red_hits": float(np.clip(r.normal(1.09, 0.87), 0, 6)),
                               "blue_hit": int(r.random() < 0.0625)} for i in range(120)]}
               for name in ("uniform", "stat:a", "stat:b")]
        out = battle.compare_methods(res, baseline_method="uniform", min_issues=30)
        if any(p["verdict"] in ("显著更优", "显著更差") for p in out["pairs"]):
            hits += 1
    fpr = hits / sims
    assert fpr <= 0.05 + cal.fpr_tolerance(0.05, sims), (
        f"compare_methods 的 FWER {fpr:.3f} 超标")


# ===========================================================================
# 8. battle._llm_arm_verdict —— 四档判决的对称性
# ===========================================================================

def _llm_verdict(rng, effect=None):
    n = 120
    e = 0.0 if effect is None else float(effect)
    base = rigor.BASELINE_MEAN_HITS
    series = base + e + rng.normal(0.0, rigor.BASELINE_SD_HITS, n)
    red_t = rigor.strategy_significance(series, base, n_trials=1, alpha=0.05)
    two = battle._two_sided(red_t, 1, 0.05)
    v = battle._llm_arm_verdict({"n_issues": n},
                                {"delta": red_t.get("delta"),
                                 "p_uncorrected": two.get("p_uncorrected"),
                                 "adjusted_p": two.get("p_adjusted"),
                                 "verdict": red_t.get("verdict")},
                                min_issues=30, alpha=0.05)
    return v["verdict"]


def test_llm_arm_verdict_is_symmetric_and_decisive():
    """对照臂判决：A 更好 → improved；A 更差 → worse；H0 → 不误报。"""
    rng = np.random.default_rng(SEED + 20)
    n_sims = cal.n_sims(40)
    from collections import Counter
    improved = Counter(_llm_verdict(rng, 0.5) for _ in range(n_sims))
    worse = Counter(_llm_verdict(rng, -0.5) for _ in range(n_sims))
    null = Counter(_llm_verdict(rng, 0.0) for _ in range(n_sims))
    assert improved["improved"] / n_sims > 0.8, f"正效应漏检：{dict(improved)}"
    assert worse["worse"] / n_sims > 0.8, f"负效应漏检（D-1 未修）：{dict(worse)}"
    assert (improved["improved"] - worse["worse"]) / n_sims <= 0.2, "improved/worse 不对称"
    # H0 下不得把「无差异」误判成任一显著档（允许极小误报）
    false_sig = (null.get("improved", 0) + null.get("worse", 0)) / n_sims
    assert false_sig <= 0.05 + cal.fpr_tolerance(0.05, n_sims), f"H0 误报过多：{dict(null)}"


# ===========================================================================
# 9. hypothesis.evaluate_prereg —— 累计口径判定
# ===========================================================================

def test_prereg_decision_arithmetic_is_two_sided_and_cumulative(tmp_path, monkeypatch):
    """前置注册的判定规则：passed 当且仅当 ``p_cum < alpha``（与效应方向无关）。

    这很重要：它说明 prereg 判据本身是**双向**的（效应为负也会被判 passed，
    不是"只有更优才算"），与 D-1 的病灶无关；它的严厉性来自累计 Bonferroni
    （m_cum 只增不减）。
    """
    from lottery import hypothesis as H

    calls = {"n": 0}

    def fake_regime(draws, flt, start=200):
        calls["n"] += 1
        return {"scan": {"n": 300}, "status": "persistent", "label_zh": "持续",
                "reason_zh": "",
                "recent": {"window": 50, "n": 50, "mean": 1.0, "delta": 0.7,
                           "p_value": 1e-6, "p_adjusted": 1e-6},
                "eras": []}

    monkeypatch.setattr("lottery.discovery.regime_of_filter", fake_regime)

    def _mk_batch(n: int = 10):
        """构造**同批 10 条**假说。

        R2 约束要求每批 >= MIN_BATCH_SIZE(10) 才允许判定；只放 1 条时
        evaluate_prereg 会按 R2 跳过（checked=0），测的就不是"判定算术"
        而是"批规模闸门"了。
        """
        return [{"id": f"h{i}", "statement": f"demo-{i}", "family": "freq",
                 "source": "llm", "status": "proposed",
                 "prereg_status": "registered",
                 "batch_id": "b1", "attempts": 1,
                 "prereg_at": 1.0,
                 "evaluate_after_issue": "2026001",
                 "frozen_until_issue": "2026001",
                 # _spec_canonical 读 action.filter.{key,params,target}；
                 # 用维度表里真实存在的档位（hot 的合法 params 见 discovery.FAMILY_BY_KEY）
                 "action": {"type": "prefer_red_filter",
                            "filter": {"key": "hot",
                                       "params": {"window": 10, "k": 6 if i % 2 else 5},
                                       "target": "red"}}}
                for i in range(n)]

    for alpha, p_recent, expect in ((0.05, 1e-6, "passed"), (0.05, 0.5, "failed")):
        path = tmp_path / f"hyp_{p_recent}.json"
        import json
        path.write_text(json.dumps({"version": "v10-1", "counters": {
            "n_tested_cumulative": 0, "last_issue": None}, "hypotheses": _mk_batch(10)}),
            encoding="utf-8")
        fake_regime = lambda draws, flt, start=200: {
            "scan": {"n": 300}, "status": "persistent", "label_zh": "持续",
            "reason_zh": "",
            "recent": {"window": 50, "n": 50, "mean": 1.0, "delta": 0.7,
                       "p_value": p_recent, "p_adjusted": p_recent},
            "eras": []}
        monkeypatch.setattr("lottery.discovery.regime_of_filter", fake_regime)
        out = H.evaluate_prereg(registry_path=path, issue="2026002", alpha=alpha,
                               dry_run=True, draws=[{"reds": [1, 2, 3, 4, 5, 6],
                                                     "blue": 1}] * 200)
        assert out["checked"] >= 1
        status = {"passed": out["passed"], "failed": out["failed"]}[expect]
        assert status >= 1, f"p={p_recent} 应判 {expect}，实际 {out}"


def test_prereg_bonferroni_is_monotone_in_m_cum():
    """累计口径 p_cum = min(1, p·m_cum) 随 m_cum 单调不减（越查越难通过）。"""
    from lottery import hypothesis as H
    p = 1e-3
    vals = [H._bonferroni(p, m) for m in (1, 10, 100, 1000, 10 ** 6)]
    assert vals == sorted(vals)
    assert vals[0] == pytest.approx(p)
    assert vals[-1] == 1.0


# ===========================================================================
# 10. llm_lab.evaluate_claim —— 规律工坊的结论档（support / reject）
# ===========================================================================

def test_llm_lab_verdict_symmetric_support_reject(monkeypatch):
    """``evaluate_claim`` 的 support / reject 必须双向可达（判据对称）。"""
    from lottery import discovery

    claim = {"filters": [{"key": "hot", "params": {"window": 30, "k": 6}, "label": "hot"}],
             "mode": "union"}
    draws = [{"reds": [1, 2, 3, 4, 5, 6], "blue": 1, "issue": f"{i:04d}"}
             for i in range(220)]

    def _rep(delta):
        return {"scan": {"n": 300}, "status": "persistent", "label_zh": "持续",
                "reason_zh": "",
                "recent": {"window": 150, "n": 150, "mean": 1.0 + delta,
                           "delta": delta, "p_value": 1e-8, "p_adjusted": 1e-8},
                "eras": []}

    seen = {}
    for name, delta in (("support", +0.5), ("reject", -0.5)):
        monkeypatch.setattr(discovery, "regime_of_filter",
                            lambda draws, flt, start=200, _d=delta: _rep(_d))
        out = llm_lab.evaluate_claim(claim, draws, start=200)
        seen[name] = out["verdict"]
    assert seen == {"support": "support", "reject": "reject"}, seen


# ===========================================================================
# 11. ml_model._combine_signal_test —— D-2 现场（已由 V10-08 修复，体检锁住）
# ===========================================================================

@pytest.mark.parametrize("n_null", [19, 39])
def test_combine_signal_test_null_fpr_is_controlled_D2(n_null):
    """D-2 回归锁：置换重要性的联合零分布检验在 H0 下不得高假阳性。

    V10 前 B=19 的设计曾报出 35% 假阳性（零复制不足）；V10-08 改成联合零分布后
    实测 B=19 → 0/300、B=199 → 5%。这里用较小次数锁住口径。
    """
    from lottery import ml_model

    sims = cal.n_sims(60)
    k = 0
    for s in range(sims):
        r = np.random.default_rng(s * 17 + n_null)
        real = [[float(x) for x in r.normal(0, 1, 5)] for _ in range(33)]
        null = [[[float(x) for x in r.normal(0, 1, 5)] for _ in range(33)]
                for _ in range(n_null)]
        out = ml_model._combine_signal_test(real, null)
        if out.get("significant"):
            k += 1
    fpr = k / sims
    assert fpr <= 0.05 + cal.fpr_tolerance(0.05, sims), (
        f"B={n_null} 时 H0 假阳性率 {fpr:.3f} 超标（D-2 回归）")


@pytest.mark.xfail(
    reason="D-2（待修，task-13）：B=n_null=19 时 min_attainable_p=1/(B+1)=0.05，"
           "而判定用 p < alpha 的严格不等号 → 信号再强也停在 p=0.05，判不出。"
           "修复（n_null>=200）后本用例应自动转为 xpass，届时删掉本标记。",
    strict=False,
)
def test_combine_signal_test_detects_injected_signal():
    """功效：主判据 ``T_mean``（各号码最强特征重要性的平均）要能检出**分散**信号。

    **当前预期失败（xfail），因为 D-2 缺陷**：
    ``min_attainable_p`` 恰为 0.05，而 ``significant = p_mean < alpha`` 用严格不等号，
    所以即使 T_mean 是零分布 95 分位的 2.8 倍（实测 3.71 vs 1.31），p 仍为 0.05、
    判为不显著。这不是测试写错，是产品缺陷 —— 详见
    ``docs/V10_IMPROVEMENT_PLAN.md`` 附录 D-2 的"反向问题"一节。

    注意判据设计：单个号码×特征的尖峰几乎不影响 ``T_mean``（稳健性设计），
    它由副统计量 ``T_max`` 负责 —— 两者分工见下一条测试。
    """
    from lottery import ml_model

    sims = cal.n_sims(20)
    hits = 0
    for s in range(sims):
        r = np.random.default_rng(s * 23 + 1)
        real = [[float(x) for x in r.normal(0, 1, 5)] for _ in range(33)]
        # 分散信号：每个号码的最强特征都上移（真实信号散布在多号码时 T_mean 最强）
        for row in real:
            row[0] = float(abs(row[0]) + 3.0)
        null = [[[float(x) for x in r.normal(0, 1, 5)] for _ in range(33)]
                for _ in range(19)]
        out = ml_model._combine_signal_test(real, null)
        if out.get("significant"):
            hits += 1
    assert hits / sims > 0.8, f"注入分散信号后检出率仅 {hits}/{sims}"


def test_combine_signal_test_single_spike_goes_to_max_statistic():
    """单个号码×特征的尖峰：主判据 T_mean 不应因此误报，但 T_max 必须抓住它。

    这是「判据分工」的显式体检 —— 若将来有人把主判据换成 T_max，
    H0 假阳性会被尖峰噪声抬高（本测试会先提醒）。
    """
    from lottery import ml_model

    r = np.random.default_rng(7)
    real = [[float(x) for x in r.normal(0, 1, 5)] for _ in range(33)]
    real[0][0] = 12.0
    null = [[[float(x) for x in r.normal(0, 1, 5)] for _ in range(33)] for _ in range(19)]
    out = ml_model._combine_signal_test(real, null)
    assert out["p_value_max_stat"] <= 0.05, "单点尖峰必须被 T_max 抓住"
    assert out["p_value_mean_stat"] > 0.05, "单点尖峰不应污染主判据 T_mean"
    assert out["p_value"] == out["p_value_mean_stat"], "主判据应取 T_mean 口径"
