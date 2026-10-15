"""lottery/rigor.py 的统计检验测试（V4-A）。

覆盖三件必须证明的事：
1. **正确性**：理论分布、校正公式与手算值一致（可独立验算的小例子）。
2. **有功效**：人为注入强偏置后，检验必须判 ``deviation``。
   一个永远输出「一切随机」的检验器是无用的——本文件用
   ``test_injected_bias_is_detected`` / ``test_power_*`` 显式证伪它。
3. **边界稳健**：空数据、单期、样本量过小、非法结构均不崩溃。

所有随机数据都用固定 seed，保证可复现。
"""
from __future__ import annotations

import math

import numpy as np
import pytest
from scipy import stats

from lottery import rigor


# --------------------------------------------------------------------------
# 测试数据构造
# --------------------------------------------------------------------------

def make_draws(n: int, seed: int = 42, weights=None, blue=None,
               reds=None) -> list:
    """生成 n 期测试开奖数据。

    weights: 长度 33 的红球抽样权重（None = 均匀）；用于注入偏置。
    blue:    长度 16 的蓝球权重（None = 均匀）。
    reds:    固定红球（每期相同）；用于构造确定性场景。
    """
    rng = np.random.default_rng(seed)
    out = []
    for i in range(n):
        if reds is not None:
            r = list(reds)
        else:
            p = np.ones(33) if weights is None else np.asarray(weights, dtype=float)
            p = p / p.sum()
            r = sorted(int(x) for x in rng.choice(np.arange(1, 34), size=6,
                                                   replace=False, p=p))
        if blue is not None:
            pb = np.asarray(blue, dtype=float)
            pb = pb / pb.sum()
            b = int(rng.choice(np.arange(1, 17), p=pb))
        else:
            b = int(rng.integers(1, 17))
        out.append({"issue": f"2026{i:03d}", "reds": r, "blue": b})
    return out


def _biased_weights(strength: float = 8.0, k: int = 6):
    """前 k 个号码权重为 strength、其余为 1 的偏置权重。"""
    w = np.ones(33)
    w[:k] = strength
    return w


# --------------------------------------------------------------------------
# 1. 理论分布正确性
# --------------------------------------------------------------------------

def test_sum_distribution_is_exact_and_normalized():
    """和值理论分布必须是 C(33,6) 的精确枚举：总数 1_107_568，支撑 [21,183]。"""
    sums, counts, total = rigor.sum_distribution()
    assert total == math.comb(33, 6) == 1107568
    assert int(sums[0]) == 21 and int(sums[-1]) == 183
    assert counts.sum() == total
    # 均值与方差可独立验算：mean = 6*17 = 102
    mean = float((sums * counts).sum() / total)
    sd = float(np.sqrt(((sums - mean) ** 2 * counts).sum() / total))
    assert mean == pytest.approx(102.0, abs=1e-9)
    assert sd == pytest.approx(21.42428528562855, rel=1e-12)


def test_theory_consecutive_rate_matches_closed_form():
    """至少一组连号的理论概率 = 1 - C(28,6)/C(33,6) ≈ 0.65985。"""
    expected = 1.0 - math.comb(28, 6) / math.comb(33, 6)
    assert rigor.theory_consecutive_rate() == pytest.approx(expected, rel=1e-12)
    assert rigor.theory_consecutive_rate() == pytest.approx(0.6598493, abs=1e-6)


def test_baseline_constants_match_derivation():
    """基线常量必须等于推导值：6*6/33 与超几何标准差。

    单注红球命中数 ~ Hypergeom(N=33, K=6, n=6)，其标准差为
    sqrt(n * (K/N) * (1-K/N) * (N-n)/(N-1)) = 0.86781…
    （已用 20 万次超几何抽样实证：经验 sd ≈ 0.8701，与此一致）
    """
    assert rigor.BASELINE_MEAN_HITS == pytest.approx(36 / 33)
    assert rigor.BASELINE_MEAN_HITS == pytest.approx(1.0909090909, abs=1e-9)
    var = 6 * (6 / 33) * (27 / 33) * (27 / 32)
    assert rigor.BASELINE_SD_HITS == pytest.approx(math.sqrt(var))
    assert rigor.BASELINE_SD_HITS == pytest.approx(0.8678128678198537, rel=1e-12)
    # 与蒙特卡洛经验标准差一致（容差 1%）
    rng = np.random.default_rng(1234)
    emp = rng.hypergeometric(6, 27, 6, size=200000).std()
    assert rigor.BASELINE_SD_HITS == pytest.approx(emp, rel=0.01)


def test_normal_cdf_ppf_roundtrip():
    """正态辅助函数与 scipy 一致（回退实现也不得偏离）。"""
    for q in (0.025, 0.5, 0.8, 0.975):
        assert rigor.normal_ppf(q) == pytest.approx(stats.norm.ppf(q), abs=1e-9)
    for x in (-3.0, -1.0, 0.0, 1.96):
        assert rigor.normal_cdf(x) == pytest.approx(stats.norm.cdf(x), abs=1e-12)


# --------------------------------------------------------------------------
# 2. 多重检验校正：手算可验证
# --------------------------------------------------------------------------

def test_bonferroni_hand_computed():
    """Bonferroni：p_adj = min(1, m*p)，m=5 时手算 [0.05,0.1,0.15,0.2,0.25]。"""
    out = rigor.bonferroni([0.01, 0.02, 0.03, 0.04, 0.05])
    assert out == pytest.approx([0.05, 0.10, 0.15, 0.20, 0.25])
    # 上界截断
    assert rigor.bonferroni([0.5, 0.9]) == [1.0, 1.0]
    assert rigor.bonferroni([]) == []


def test_benjamini_hochberg_hand_computed():
    """BH：p_(i)*m/i 的单调化。手算 [0.01,0.02,0.03,0.04,0.05] -> 全 0.05。

    逐步：0.01*5/1=0.05, 0.02*5/2=0.05, 0.03*5/3=0.05, 0.04*5/4=0.05, 0.05*5/5=0.05
    """
    out = rigor.benjamini_hochberg([0.01, 0.02, 0.03, 0.04, 0.05])
    assert out == pytest.approx([0.05] * 5)


def test_benjamini_hochberg_known_reference_example():
    """手算可验证的 BH 例子（含单调化步骤）。

    ps = [0.001, 0.008, 0.039, 0.041]，m=4：
      i=1: 0.001*4/1 = 0.004
      i=2: 0.008*4/2 = 0.016
      i=3: 0.039*4/3 = 0.052  -> 单调化 min(0.052, i=4: 0.041*4/4=0.041) = 0.041
      i=4: 0.041*4/4 = 0.041
    故 BH = [0.004, 0.016, 0.041, 0.041]。
    """
    ps = [0.001, 0.008, 0.039, 0.041]
    out = rigor.benjamini_hochberg(ps)
    assert out == pytest.approx([0.004, 0.016, 0.041, 0.041])
    # 单调化：p_(3) 与 p_(4) 相等（被后一项压下来）
    assert out[2] == pytest.approx(out[3])
    # 未单调化时第 3 项本应是 0.052，单调化把它降到 0.041
    assert out[2] < 0.039 * 4 / 3
    # 单项集合上 BH 为恒等（m=1, m/j=1）——正因如此，单策略场景不能用它冒充校正
    assert rigor.benjamini_hochberg([0.0146])[0] == pytest.approx(0.0146)


def test_bh_lead_reference_list_exact_match():
    """Lead 提供的 15 个 p 值参照：BH 前 4 项必须精确等于 [0.015, 0.06, 0.126, 0.126]。

    这是跨实现交叉验证：模块实现 vs 独立自写参考实现，要求 max abs diff == 0。
    """
    ps = [0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074, 0.205, 0.212,
          0.216, 0.222, 0.251, 0.269, 0.275, 0.34]
    got = rigor.benjamini_hochberg(ps)
    assert got[:4] == pytest.approx([0.015, 0.06, 0.126, 0.126])

    # 独立参考实现
    m = len(ps)
    order = sorted(range(m), key=lambda i: ps[i])
    ref = [0.0] * m
    running = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        running = min(running, ps[i] * m / rank)
        ref[i] = min(1.0, running)
    assert max(abs(a - b) for a, b in zip(got, ref)) == 0.0
    # 全部落在 (0, 1]，且单调不减
    assert all(0.0 < x <= 1.0 for x in got)
    assert all(a <= b + 1e-12 for a, b in zip(got, got[1:]))


def test_bh_multi_pvalue_reference_from_lead():
    """15 个 p 值的 BH 校正，与自写参考实现逐项对照（最大绝对误差必须为 0）。

    前 4 项解析值（m=15，从大到小累计最小值）：
      rank5: 0.011*15/5 = 0.033
      rank4: 0.009*15/4 = 0.03375 -> 与 rank5 的 0.033 取 min = 0.033
      rank3: 0.007*15/3 = 0.035  -> 取 min = 0.033
      rank2: 0.004*15/2 = 0.03   -> 取 min = 0.03
      rank1: 0.001*15/1 = 0.015  -> 取 min = 0.015
    因此 adjusted = [0.015, 0.03, 0.033, 0.033, 0.033, ...]
    """
    ps = [0.001, 0.004, 0.007, 0.009, 0.011, 0.02, 0.03, 0.04, 0.05,
          0.06, 0.07, 0.08, 0.09, 0.10, 0.20]
    got = rigor.benjamini_hochberg(ps)
    # 独立参考实现：按 p 升序遍历，从最大值侧累计 running minimum
    m = len(ps)
    order = sorted(range(m), key=lambda i: ps[i])
    ref = [0.0] * m
    running = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        running = min(running, ps[i] * m / rank)
        ref[i] = min(1.0, running)
    assert max(abs(a - b) for a, b in zip(got, ref)) == 0.0
    # 前两项解析值
    assert got[0] == pytest.approx(0.015)
    assert got[1] == pytest.approx(0.03)
    # 单调化：i=2 与 i=3 被更靠后的 rank5 值 0.033 压低，三者相等
    assert got[2] == pytest.approx(got[3]) == pytest.approx(got[4])
    assert got[2] == pytest.approx(0.033, abs=1e-9)


def test_bh_is_never_larger_than_bonferroni_and_monotone():
    """不变式：BH <= Bonferroni 恒成立；且 p 增大时 BH 单调不减。"""
    rng = np.random.default_rng(0)
    for _ in range(30):
        p = rng.random(int(rng.integers(2, 40)))
        b = rigor.bonferroni(p)
        h = rigor.benjamini_hochberg(p)
        assert all(hi <= bi + 1e-12 for hi, bi in zip(h, b))
        assert all(hi >= pi - 1e-12 for hi, pi in zip(h, p))  # 校正只会变大
    assert (rigor.benjamini_hochberg([0.01, 0.2])[0]
            <= rigor.benjamini_hochberg([0.05, 0.2])[0])
    assert rigor.benjamini_hochberg([]) == []


def test_adjust_pvalues_returns_both_in_input_order():
    """adjust_pvalues 同时返回两种校正且与输入同序（乱序输入验证）。"""
    ps = [0.20, 0.01, 0.05]
    out = rigor.adjust_pvalues(ps)
    assert set(out) == {"bonferroni", "benjamini_hochberg"}
    assert out["bonferroni"] == pytest.approx([0.60, 0.03, 0.15])
    # 同序：最小值出现在下标 1
    assert out["benjamini_hochberg"][1] == min(out["benjamini_hochberg"])


# --------------------------------------------------------------------------
# 3. randomness_report：真随机 vs 注入偏置（功效证明）
# --------------------------------------------------------------------------

def test_random_data_is_consistent_with_random():
    """固定 seed 的真随机数据必须判 consistent-with-random。"""
    draws = make_draws(1500, seed=20240101)
    rep = rigor.randomness_report(draws)
    assert rep["verdict"] == "consistent-with-random"
    assert rep["n_valid"] == 1500
    assert rep["tests"]["red_frequency"]["verdict"] == "consistent-with-random"
    assert rep["tests"]["blue_frequency"]["verdict"] == "consistent-with-random"
    # 自由度口径
    assert rep["tests"]["red_frequency"]["df"] == 32
    assert rep["tests"]["blue_frequency"]["df"] == 15
    # 真随机下 p 值不应极端
    assert rep["tests"]["red_frequency"]["p_value"] > 0.01
    assert rep["tests"]["blue_frequency"]["p_value"] > 0.01


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_random_data_never_spuriously_deviates(seed):
    """功效检验的对照面：5 个不同 seed 的真随机数据都不许报 deviation。

    防止「永远报 deviation」的退化实现。
    """
    rep = rigor.randomness_report(make_draws(1200, seed=seed))
    assert rep["verdict"] == "consistent-with-random", (
        f"seed={seed} 的真随机数据被误判为 deviation："
        f"{ {k: v['p_value'] for k, v in rep['tests'].items()} }"
    )


def test_injected_bias_is_detected():
    """核心功效证明：人为把前 6 个红球权重提高 8 倍，检验必须判 deviation。

    若本用例失败，说明 randomness_report 是无功效的「永远说随机」检验器。
    """
    draws = make_draws(1500, seed=7, weights=_biased_weights(strength=8.0))
    rep = rigor.randomness_report(draws)
    assert rep["verdict"] == "deviation"
    rf = rep["tests"]["red_frequency"]
    assert rf["verdict"] == "deviation"
    assert rf["p_value"] < 1e-6  # 极强偏置应给出极小 p
    assert rep["n_deviation"] >= 1
    # 说明：蓝球未注入偏置，但单项检验在 5% 水平下本就有 ~5% 概率假阳性，
    # 因此不断言蓝球一定 consistent（那会是脆弱的过拟合断言）。
    # 只要求：红球是主导的、极端显著的那一项。
    assert rf["statistic"] > rep["tests"]["blue_frequency"]["statistic"]


def test_injected_blue_bias_is_detected():
    """蓝球维度同样有功效：把 1 号蓝球权重提高 12 倍必须检出。"""
    bw = np.ones(16)
    bw[0] = 12.0
    rep = rigor.randomness_report(make_draws(1200, seed=9, blue=bw))
    assert rep["tests"]["blue_frequency"]["verdict"] == "deviation"
    assert rep["tests"]["blue_frequency"]["p_value"] < 1e-3


def test_power_increases_with_sample_size():
    """功效随样本量单调增强：同样偏置下，样本越多 p 值越小。"""
    w = _biased_weights(strength=2.0)  # 中等偏置，小样本未必检出
    p_small = rigor.randomness_report(make_draws(300, seed=11, weights=w))[
        "tests"]["red_frequency"]["p_value"]
    p_large = rigor.randomness_report(make_draws(3000, seed=11, weights=w))[
        "tests"]["red_frequency"]["p_value"]
    assert p_large < p_small


def test_randomness_report_structure_and_mde():
    """返回结构完整，且 min_detectable_effect 与手算公式一致。

    δ = (z_{1-α/2} + z_{power}) * sd / sqrt(n)
    """
    n = 3000
    draws = make_draws(n, seed=13)
    rep = rigor.randomness_report(draws)
    required = {"verdict", "n_draws", "n_valid", "alpha", "tests",
                "min_detectable_effect", "baseline", "warnings", "method"}
    assert required <= set(rep)
    for name, t in rep["tests"].items():
        assert {"statistic", "df", "p_value", "verdict"} <= set(t), name
        assert t["verdict"] in ("consistent-with-random", "deviation",
                                "insufficient-data")

    mde = rep["min_detectable_effect"]
    z_a = stats.norm.ppf(0.975)
    z_p = stats.norm.ppf(0.8)
    expected = (z_a + z_p) * rigor.BASELINE_SD_HITS / math.sqrt(n)
    assert mde["n"] == n
    assert mde["delta_hits"] == pytest.approx(expected, rel=1e-9)
    assert mde["relative_lift"] == pytest.approx(expected / rigor.BASELINE_MEAN_HITS,
                                                 rel=1e-9)
    # 3514 期真实场景下 MDE ≈ 3.76%（与已知结论一致）
    mde_real = rigor._min_detectable_effect(3514)
    assert mde_real["relative_lift"] == pytest.approx(0.0376, abs=1e-3)


def test_randomness_report_detects_structural_bias_zone():
    """结构型偏置也有功效：全部取 1-11 区的号码，三区分布必须偏离。"""
    draws = []
    rng = np.random.default_rng(21)
    for i in range(800):
        r = sorted(int(x) for x in rng.choice(np.arange(1, 12), size=6, replace=False))
        draws.append({"issue": f"X{i}", "reds": r, "blue": int(rng.integers(1, 17))})
    rep = rigor.randomness_report(draws)
    assert rep["tests"]["zone_count"]["verdict"] == "deviation"
    assert rep["tests"]["red_sum"]["verdict"] == "deviation"  # 和值必然偏低
    assert rep["verdict"] == "deviation"


# --------------------------------------------------------------------------
# 4. min_sample_size：单调性 + 手算一致
# --------------------------------------------------------------------------

def test_min_sample_size_matches_hand_computation():
    """与手算公式一致：n = ceil(((z_{1-α/2}+z_power)*sd/effect)**2)。

    (z_a+z_p) = 1.959964 + 0.841621 = 2.801585
    effect=0.05, sd=0.8678128678198537 -> (2.801585*0.86781/0.05)^2 = 2363.13… -> 2364
    """
    z = stats.norm.ppf(0.975) + stats.norm.ppf(0.8)
    assert z == pytest.approx(2.801585, abs=1e-6)

    sd = rigor.BASELINE_SD_HITS
    effect = 0.05
    expected = math.ceil((z * sd / effect) ** 2)
    assert rigor.min_sample_size(effect, sd) == expected

    # 精确手算值（用未截断的 z 计算，避免手写常量引入 ±1 误差）
    assert rigor.min_sample_size(0.05, sd) == 2365
    # 1% 相对提升（effect = 0.01*36/33 = 0.01090909 命中数）需要约 5 万期
    n_1pct = rigor.min_sample_size(0.01 * 36 / 33, sd)
    assert n_1pct == math.ceil((z * sd / (0.01 * 36 / 33)) ** 2)
    assert n_1pct == 49669
    # 5% 相对提升所需样本量（前端「还需多少期」的典型数值）
    assert rigor.min_sample_size(0.05 * 36 / 33, sd) == 1987


def test_min_sample_size_monotonicity():
    """n 随 |effect| 增大而减小、随 sd 增大而增大、随 power 增大而增大。"""
    ns_effect = [rigor.min_sample_size(e, 0.8678128678198537)
                 for e in (0.5, 0.2, 0.1, 0.05, 0.02)]
    # effect 递减 -> 所需样本量必须递增（逐个严格比较，避免排序方向写反）
    assert all(a < b for a, b in zip(ns_effect, ns_effect[1:])), ns_effect
    assert ns_effect == [24, 148, 592, 2365, 14778]

    ns_sd = [rigor.min_sample_size(0.05, s) for s in (0.5, 1.0, 2.0)]
    assert ns_sd == sorted(ns_sd)

    ns_power = [rigor.min_sample_size(0.05, 0.8678128678198537, power=p)
                for p in (0.5, 0.8, 0.95)]
    assert ns_power == sorted(ns_power)

    # alpha 更严格 -> 需要更多样本
    assert rigor.min_sample_size(0.05, 0.8678128678198537, alpha=0.01) > \
        rigor.min_sample_size(0.05, 0.8678128678198537, alpha=0.05)


def test_min_sample_size_sign_and_degenerate():
    """负效应量取绝对值；effect=0 或无变异 -> 极大哨兵值。"""
    sd = 0.8678128678198537
    assert rigor.min_sample_size(-0.05, sd) == rigor.min_sample_size(0.05, sd)
    assert rigor.min_sample_size(0.0, sd) == 10 ** 9
    assert rigor.min_sample_size(0.05, 0.0) == 10 ** 9
    assert rigor.min_sample_size(1e9, sd) == 1  # 至少返回 1


def test_min_sample_size_extreme_effects_do_not_crash():
    """极端边界不崩溃：极小/NaN/inf effect 一律封顶为哨兵值，绝不返回小数。

    回归：曾经 effect=1e-300 会触发 ``OverflowError``（浮点 ** 2 溢出），
    让调用方崩溃——而「effect→0 需要无限样本」本应由哨兵值表达。
    """
    sd = 0.8678128678198537
    for e in (0.0, -0.0, 1e-300, 1e-200, 1e-50, float("nan"), float("inf")):
        n = rigor.min_sample_size(e, sd)
        assert n == 10 ** 9, f"effect={e!r} -> {n}"
    # 极小但仍在可表达范围内的 effect 仍然精确
    assert rigor.min_sample_size(1e-4, sd) == 591098485


def test_min_sample_size_monotone_across_full_range():
    """全范围弱单调：effect 递减时 n 非递减（含封顶区间），不会出现局部塌陷。"""
    sd = 0.8678128678198537
    effs = [1.0, 0.5, 0.2, 0.1, 0.05, 0.02, 0.01, 1e-3, 1e-6, 1e-9, 1e-300]
    ns = [rigor.min_sample_size(e, sd) for e in effs]
    assert all(a <= b for a, b in zip(ns, ns[1:])), list(zip(effs, ns))
    # 严格递增区间（封顶之前）必须严格
    strict = [rigor.min_sample_size(e, sd) for e in (1.0, 0.5, 0.2, 0.1, 0.05)]
    assert all(a < b for a, b in zip(strict, strict[1:])), strict


def test_min_sample_size_2d_grid_monotonicity():
    """二维网格穷举：effect 增大或 sd 减小时，n 必须不增。"""
    rng = np.random.default_rng(20260101)
    for _ in range(200):
        e1, e2 = sorted(rng.uniform(0.005, 1.0, 2))
        s1, s2 = sorted(rng.uniform(0.05, 5.0, 2))
        # effect 越大 -> n 越小
        assert rigor.min_sample_size(e2, s1) <= rigor.min_sample_size(e1, s1)
        # sd 越小 -> n 越小
        assert rigor.min_sample_size(e1, s1) <= rigor.min_sample_size(e1, s2)


# --------------------------------------------------------------------------
# 5. strategy_significance：校正语义 + 假阳性抑制
# --------------------------------------------------------------------------

def test_strategy_significance_uncorrected_vs_corrected():
    """手算场景：+3.1% 提升、n=3514、t≈2.18 的经典「看似显著」。

    该提升在 n_trials=1 时未被单侧 p<0.05 拒绝原假设前应保持诚实，
    在 n_trials=68（搜索数十种策略）时必须 survives_correction=False。
    """
    rng = np.random.default_rng(31)
    base = rng.hypergeometric(6, 27, 6, size=3514).astype(float)
    lifted = base * 1.031  # +3.1%
    one = rigor.strategy_significance(lifted, 36 / 33, n_trials=1)
    many = rigor.strategy_significance(lifted, 36 / 33, n_trials=68)
    # 校正单调：搜索越多策略，校正后 p 越大（或相等）
    assert many["adjusted_p"] >= one["adjusted_p"] - 1e-12
    assert many["bonferroni_p"] == pytest.approx(min(1.0, many["p_uncorrected"] * 68))
    # 未校正 p 很小但 n_trials 很大时不构成证据
    if many["p_uncorrected"] > 0.002:
        assert many["survives_correction"] is False
    assert "n_trials" in many["warning_zh"] or "校正" in many["warning_zh"]


def test_strategy_significance_pure_noise_is_not_significant():
    """纯噪声：真·随机命中序列（均值=基线）必须不显著。"""
    rng = np.random.default_rng(77)
    hits = rng.hypergeometric(6, 27, 6, size=3000).astype(float)
    res = rigor.strategy_significance(hits, 36 / 33, n_trials=1)
    assert res["survives_correction"] is False
    assert res["verdict"] == "not-significant"
    assert res["adjusted_p"] > 0.05


def test_strategy_significance_strong_effect_survives():
    """强且真实的提升（每期恒定 +3 命中）必须 survives_correction=True。

    证明校正不是「一律否掉」的退化实现。
    """
    res = rigor.strategy_significance([4.0] * 60, 1.0909, n_trials=50)
    assert res["survives_correction"] is True
    assert res["verdict"] == "significant-after-correction"
    assert res["adjusted_p"] < 0.05
    assert res["t"] in (float("inf"),) or res["t"] > 0


def test_strategy_significance_sign_test_hand_computed():
    """符号检验：10 期全为正差，双侧二项 p = 2 * 0.5^10 = 0.001953。"""
    res = rigor.strategy_significance([2.0] * 10 + [0.0] * 0, 1.0, n_trials=1)
    st = res["sign_test"]
    assert st["n_pos"] == 10 and st["n_neg"] == 0
    assert st["p_value"] == pytest.approx(2 * 0.5 ** 10)
    # 正负各半 -> p = 1
    res2 = rigor.strategy_significance([2.0, 0.0, 2.0, 0.0], 1.0, n_trials=1)
    assert res2["sign_test"]["p_value"] == pytest.approx(1.0)


def test_strategy_significance_per_period_baseline():
    """逐期基线序列（对照策略配对检验）正常工作且要求等长。"""
    rng = np.random.default_rng(5)
    s = rng.normal(1.2, 0.9, size=200).round()
    b = rng.normal(1.1, 0.9, size=200).round()
    res = rigor.strategy_significance(s, b, n_trials=3)
    assert res["n"] == 200
    assert res["baseline_mean"] == pytest.approx(float(np.mean(b)))
    with pytest.raises(ValueError):
        rigor.strategy_significance(s, b[:100], n_trials=1)


def test_strategy_significance_zero_variance_nonzero_is_decisive():
    """退化但决定性：每期差值恒为同一非零常数 -> p=0，不是「不可用」。"""
    res = rigor.strategy_significance([5.0] * 30, 1.0, n_trials=10)
    assert res["p_value"] == 0.0
    assert res["survives_correction"] is True
    # 差值恒为 0 -> 明确无效应
    flat = rigor.strategy_significance([1.0] * 30, 1.0, n_trials=10)
    assert flat["p_value"] == 1.0
    assert flat["survives_correction"] is False


def test_n_trials_semantics_forces_larger_adjustment():
    """n_trials 的作用必须真实生效：同一个 p，n_trials 越高校正后 p 越大。"""
    rng = np.random.default_rng(101)
    hits = rng.hypergeometric(6, 27, 6, size=1000).astype(float) * 1.02
    p1 = rigor.strategy_significance(hits, 36 / 33, n_trials=1)["bonferroni_p"]
    p10 = rigor.strategy_significance(hits, 36 / 33, n_trials=10)["bonferroni_p"]
    p100 = rigor.strategy_significance(hits, 36 / 33, n_trials=100)["bonferroni_p"]
    assert p1 <= p10 <= p100
    # n_trials<=0 退化为 1，不崩溃
    assert rigor.strategy_significance(hits, 36 / 33, n_trials=0)["n_trials"] == 1


# --------------------------------------------------------------------------
# 5b. 回归：多重检验校正绝不能被静默废掉（Lead 复核发现的真实缺陷）
# --------------------------------------------------------------------------

def test_regression_adjusted_p_never_equals_uncorrected_when_n_trials_gt_1():
    """回归用例（核心）：n_trials=7, p_uncorrected≈0.0146 -> adjusted_p≈0.1022，不显著。

    缺陷复现：原实现写 ``benjamini_hochberg([p])[0]``，m=1 使 BH 退化为恒等函数、
    返回值恰等于未校正 p，再被 ``min(bonferroni_p, bh_p)`` 选中，于是
    Bonferroni（正确的 0.1022）被静默丢弃，该假阳性被判为 significant。
    本用例锁死该行为，防止回归。
    """
    p_unc = 0.0146
    n_trials = 7
    # 构造一个未校正 p 值接近 0.0146 的命中序列（t≈2.2, n=3514）
    rng = np.random.default_rng(20260101)
    base = rng.hypergeometric(6, 27, 6, size=3514).astype(float)
    target = 36 / 33 * 1.031
    hits = base + (target - base.mean())
    res = rigor.strategy_significance(hits, 36 / 33, n_trials=n_trials)

    # 校正后必须约等于 p_uncorrected * 7（Bonferroni），而不是 p_uncorrected
    assert res["bonferroni_p"] == pytest.approx(
        min(1.0, res["p_uncorrected"] * n_trials))
    assert res["adjusted_p"] == pytest.approx(res["bonferroni_p"])
    assert res["adjusted_p"] >= res["p_uncorrected"]
    # BH 在单策略场景不可计算，必须为 None，绝不能被当成「更优值」参与 min()
    assert res["bh_p"] is None
    assert res["adjust_method"] == "bonferroni"
    assert "not-applicable-single-p" in res["bh_note"]
    # 关键判定：这个搜索出来的「优势」不显著
    assert res["survives_correction"] is False
    assert res["verdict"] == "not-significant"


def test_regression_adjusted_p_is_monotone_in_n_trials():
    """不变式：n_trials > 1 时 adjusted_p >= p_uncorrected，且随 n_trials 单调不减。"""
    rng = np.random.default_rng(4242)
    hits = rng.hypergeometric(6, 27, 6, size=800).astype(float) * 1.02 + 0.01
    prev = -1.0
    for m in (1, 2, 7, 50, 500):
        res = rigor.strategy_significance(hits, 36 / 33, n_trials=m)
        assert res["p_uncorrected"] is not None
        # 不变式 1：校正只会让 p 变大
        assert res["adjusted_p"] >= res["p_uncorrected"] - 1e-12, (m, res)
        # 不变式 2：随 n_trials 单调不减
        assert res["adjusted_p"] >= prev - 1e-12, (m, res["adjusted_p"], prev)
        prev = res["adjusted_p"]
        if m > 1:
            assert res["adjusted_p"] == pytest.approx(
                min(1.0, res["p_uncorrected"] * m))
            assert res["bh_p"] is None


def test_regression_n_trials_1_means_no_correction():
    """n_trials=1（未做多重搜索）时确实无需校正：adjusted_p == p_uncorrected。"""
    rng = np.random.default_rng(99)
    hits = rng.hypergeometric(6, 27, 6, size=500).astype(float)
    res = rigor.strategy_significance(hits, 36 / 33, n_trials=1)
    assert res["adjusted_p"] == pytest.approx(res["p_uncorrected"])
    assert res["adjust_method"] == "none"
    # n_trials<=0 视为 1
    res0 = rigor.strategy_significance(hits, 36 / 33, n_trials=0)
    assert res0["n_trials"] == 1
    assert res0["adjust_method"] == "none"


def test_regression_bh_single_p_is_not_silently_used():
    """单 p 值场景下 bh_p 必须为 None，且绝不等于 p_uncorrected（防线本身）。"""
    rng = np.random.default_rng(7)
    sig = rng.normal(1.2, 1.0, size=400)
    res = rigor.strategy_significance(sig, 1.0, n_trials=20)
    assert res["bh_p"] is None
    assert res["bh_p"] != res["p_uncorrected"]
    assert res["survives_correction"] == (res["adjusted_p"] < 0.05)
    # 若 p_uncorrected 本身就 > 0.05，校正后必然也不显著
    if res["p_uncorrected"] >= 0.05:
        assert res["survives_correction"] is False


def test_strategy_significance_multi_is_real_bh():
    """多策略接口对整组 p 值做真正的 BH（BH <= Bonferroni，通过数不少于）。"""
    rng = np.random.default_rng(2024)
    # 10 个纯噪声策略：应当基本没有通过校正的（假阳性控制）
    strategies = [rng.hypergeometric(6, 27, 6, size=600).astype(float)
                  for _ in range(10)]
    res = rigor.strategy_significance_multi(strategies, 36 / 33, alpha=0.05)
    assert res["m"] == 10 and res["n_trials"] == 10
    assert len(res["adjusted_bh"]) == 10
    assert len(res["adjusted_bonferroni"]) == 10
    # BH <= Bonferroni 逐项成立
    assert all(b <= f + 1e-12 for b, f in
               zip(res["adjusted_bh"], res["adjusted_bonferroni"]))
    # 纯噪声下不应有大量策略通过 Bonferroni
    assert res["n_survive_bonferroni"] <= 1
    for s in res["strategies"]:
        assert s["bh_p"] is not None
        assert s["adjusted_p"] == pytest.approx(
            min(s["bonferroni_p"], s["bh_p"]))
        assert s["survives_correction"] == (s["adjusted_p"] < 0.05)

    # 混入一个真实强信号：必须被检出
    strong = np.full(600, 4.0)
    mixed = strategies[:4] + [strong] + strategies[5:9]
    res2 = rigor.strategy_significance_multi(mixed, 36 / 33, alpha=0.05)
    assert res2["strategies"][4]["survives_correction"] is True
    assert res2["n_survive_bonferroni"] >= 1
    assert res2["n_survive_bh"] >= res2["n_survive_bonferroni"]

    # 空输入
    assert rigor.strategy_significance_multi([], 1.09)["m"] == 0


# --------------------------------------------------------------------------
# 6. evaluate_signal：walk-forward
# --------------------------------------------------------------------------

def _hot_strategy(history, top_k: int = 6):
    """历史频率最高的 top_k 个号码。

    注意只在下标 1..33 中排序：下标 0 是占位（号码从 1 开始），若一并参与
    argsort 会在计数并列时被选中，返回非法号码 0，被 rigor 过滤后使 k < top_k。
    """
    cnt = np.zeros(34)
    for d in history:
        for r in d["reds"]:
            cnt[r] += 1
    ranked = np.argsort(cnt[1:])[-top_k:] + 1
    return [int(x) for x in ranked]


def test_evaluate_signal_walk_forward_structure():
    """walk-forward 结构正确：只评估 window 之后的期次，无未来信息。"""
    draws = make_draws(300, seed=55)
    res = rigor.evaluate_signal(draws, _hot_strategy, window=100)
    assert res["n_predictions"] == 200
    assert res["n"] == 200
    assert res["window"] == 100
    assert res["avg_k"] == pytest.approx(6.0)
    assert res["baseline"] == pytest.approx(36 / 33, rel=1e-9)
    assert len(res["hit_series"]) == 200
    # 首条预测对应下标 window 的那一期
    assert res["hit_series"][0]["issue"] == draws[100]["issue"]
    assert {"mean", "t", "p_value", "adjusted_p", "survives_correction",
            "verdict", "sign_test", "errors"} <= set(res)


def test_evaluate_signal_hot_strategy_on_random_data_is_not_significant():
    """真随机数据上的热号策略不应显著——这是最重要的诚实性断言。"""
    draws = make_draws(1500, seed=56)
    res = rigor.evaluate_signal(draws, _hot_strategy, window=300, n_trials=36)
    assert res["survives_correction"] is False
    assert res["verdict"] == "not-significant"
    assert res["adjusted_p"] > 0.05
    # 命中数应落在理论基线附近（±20%）
    assert res["mean"] == pytest.approx(36 / 33, rel=0.2)


def test_evaluate_signal_detects_real_advantage():
    """功效证明：当「策略」确实能命中 6 个红球时，必须判为显著。

    构造每期都是 [1,2,3,4,5,6] 的数据，策略固定返回同样 6 个号：
    命中恒为 6，远超基线 1.0909，必须 significant。
    """
    draws = make_draws(300, seed=57, reds=[1, 2, 3, 4, 5, 6])

    def fixed(history, **kw):
        return [1, 2, 3, 4, 5, 6]

    res = rigor.evaluate_signal(draws, fixed, window=50, n_trials=36)
    assert res["mean"] == pytest.approx(6.0)
    assert res["delta"] > 4.0
    assert res["survives_correction"] is True
    assert res["verdict"] == "significant-after-correction"


def test_evaluate_signal_baseline_scales_with_k():
    """基线随选号个数 k 变化：只选 1 个号时基线 = 6*1/33 = 0.1818。

    若不做这个修正，「少选号码」会被误判为策略变差。
    """
    draws = make_draws(400, seed=58)

    def one(history, **kw):
        return [7]

    res = rigor.evaluate_signal(draws, one, window=100)
    assert res["avg_k"] == pytest.approx(1.0)
    assert res["baseline"] == pytest.approx(6 / 33, rel=1e-9)
    assert res["mean"] <= 1.0


def test_evaluate_signal_kwargs_are_passed_through():
    """**kw 透传给 strategy_fn。"""
    draws = make_draws(200, seed=59)
    res6 = rigor.evaluate_signal(draws, _hot_strategy, window=100, top_k=6)
    res10 = rigor.evaluate_signal(draws, _hot_strategy, window=100, top_k=10)
    assert res6["avg_k"] == pytest.approx(6.0)
    assert res10["avg_k"] == pytest.approx(10.0)
    assert res10["baseline"] == pytest.approx(6 * 10 / 33, rel=1e-9)


def test_evaluate_signal_strategy_errors_do_not_crash():
    """策略抛异常时逐期捕获，不中断整体评估，errors 计数正确。"""
    draws = make_draws(120, seed=60)

    def boom(history, **kw):
        raise RuntimeError("boom")

    res = rigor.evaluate_signal(draws, boom, window=20)
    assert res["n_predictions"] == 0
    assert res["errors"] == 100
    assert res["verdict"] == "insufficient-data"

    # 返回空选号也计入 errors
    def empty(history, **kw):
        return []

    res2 = rigor.evaluate_signal(draws, empty, window=20)
    assert res2["n_predictions"] == 0
    assert res2["errors"] == 100


# --------------------------------------------------------------------------
# 7. 边界：空数据 / 单期 / 样本量过小 / 非法结构
# --------------------------------------------------------------------------

def test_empty_and_tiny_inputs_do_not_crash():
    """空列表、None、单期、样本量过小：返回 insufficient-data 而非抛异常。"""
    for bad in ([], None, [{"issue": "1", "reds": [1, 2, 3, 4, 5, 6], "blue": 7}]):
        rep = rigor.randomness_report(bad)
        assert rep["verdict"] == "insufficient-data"
    # 空输入下 MDE 的 n 为 0（无法评估功效），且不抛异常
    assert rigor.randomness_report([])["min_detectable_effect"]["n"] == 0
    # 样本量低于 30 期同样不可下任何结论
    for n in (2, 10, 29):
        rep = rigor.randomness_report(make_draws(n, seed=1))
        assert rep["verdict"] == "insufficient-data"
        assert rep["tests"]["red_frequency"]["verdict"] == "insufficient-data"
    # 单期数据允许没有相邻期检验
    rep1 = rigor.randomness_report(make_draws(1, seed=1))
    assert rep1["tests"]["red_repeat"]["verdict"] == "insufficient-data"
    # 恰好 30 期：达到门槛，应给出真实检验（且不崩溃）
    rep30 = rigor.randomness_report(make_draws(30, seed=1))
    assert rep30["verdict"] in ("consistent-with-random", "deviation")


def test_malformed_draws_are_filtered():
    """结构非法的期次被过滤，不污染统计，也不抛异常。"""
    good = make_draws(100, seed=61)
    bad = [
        {"reds": [1, 2, 3], "blue": 5},                     # 红球不足 6 个
        {"reds": [1, 2, 3, 4, 5, 6, 7], "blue": 5},         # 红球 7 个
        {"reds": [1, 1, 2, 3, 4, 5], "blue": 5},            # 重复号码
        {"reds": [1, 2, 3, 4, 5, 34], "blue": 5},           # 越界
        {"reds": [1, 2, 3, 4, 5, 6], "blue": 17},           # 蓝球越界
        {"blue": 5},                                        # 缺 reds
        {"reds": [1, 2, 3, 4, 5, 6]},                       # 缺 blue
        {"reds": None, "blue": 5},                          # 类型错误
    ]
    rep = rigor.randomness_report(good + bad)
    assert rep["n_valid"] == 100
    assert rep["n_draws"] == 108
    assert np.isfinite(rep["tests"]["red_frequency"]["statistic"])


def test_insufficient_sample_semantics_is_explicit_none():
    """样本量不足的语义约定（与 Lead 确认的设计意图）：

    - ``p_uncorrected`` / ``adjusted_p`` / ``bonferroni_p`` **一律为 None**，
      表示「不可计算」，而不是 1.0。理由：前端/API 会直接渲染这些字段，
      ``1.0`` 会被误读成「检验过且完全无效应」，``None`` 才诚实表达「无法判定」。
    - ``verdict`` 为 ``"insufficient-data"``，``survives_correction`` 为 False。
    - ``p_value`` 保留符号检验的退化值仅供诊断，调用方不应据此下结论。
    """
    for n_bad, hits in ((0, []), (1, [2.0]), (2, [2.0, 1.0])):
        res = rigor.strategy_significance(hits, 1.09, n_trials=1)
        assert res["n"] == n_bad
        assert res["verdict"] == "insufficient-data"
        assert res["p_uncorrected"] is None, res
        assert res["adjusted_p"] is None, res
        assert res["bonferroni_p"] is None, res
        assert res["survives_correction"] is False
    # 恰好在门槛（3 期）上必须给出真实校正值
    ok = rigor.strategy_significance([2.0, 0.0, 3.0], 1.09, n_trials=4)
    assert ok["n"] == 3
    assert ok["p_uncorrected"] is not None
    assert ok["adjusted_p"] is not None
    assert ok["adjusted_p"] >= ok["p_uncorrected"]


def test_evaluate_signal_returns_full_significance_structure():
    """evaluate_signal 必须返回可供 API 联调的完整字段（Lead 的联调清单）。"""
    draws = make_draws(400, seed=808)
    res = rigor.evaluate_signal(draws, _hot_strategy, window=150, n_trials=5)
    required = {
        "window", "n_predictions", "n", "mean", "baseline", "delta",
        "relative_lift", "avg_k", "t", "p_value", "p_uncorrected",
        "bonferroni_p", "bh_p", "adjusted_p", "survives_correction",
        "sign_test", "verdict", "hit_series", "errors", "n_trials", "alpha",
        "baseline_mean_hits", "baseline_sd_hits", "method", "message_zh",
    }
    assert required <= set(res), required - set(res)
    # 数值字段类型正确
    assert isinstance(res["mean"], float)
    assert isinstance(res["baseline"], float)
    assert isinstance(res["t"], float)
    assert isinstance(res["p_uncorrected"], float)
    assert isinstance(res["adjusted_p"], float)
    assert isinstance(res["survives_correction"], bool)
    assert isinstance(res["verdict"], str)
    assert res["n_predictions"] == 250
    # 校正不变式在 evaluate_signal 上同样成立
    assert res["adjusted_p"] >= res["p_uncorrected"] - 1e-12
    assert res["adjusted_p"] == pytest.approx(res["bonferroni_p"])
    # hit_series 带期号，便于前端定位
    assert res["hit_series"][0]["issue"] == draws[150]["issue"]
    assert {"issue", "hits", "expected", "k"} <= set(res["hit_series"][0])


def test_evaluate_signal_sufficient_data_has_significance_fields():
    """样本充足时 evaluate_signal 的显著性字段必须为真实数值（非 None）。"""
    draws = make_draws(300, seed=909)
    res = rigor.evaluate_signal(draws, _hot_strategy, window=100, n_trials=3)
    assert res["n_predictions"] == 200
    assert res["p_uncorrected"] is not None
    assert res["adjusted_p"] is not None
    assert res["bonferroni_p"] is not None
    assert res["verdict"] in ("not-significant", "significant-after-correction")
    assert isinstance(res["message_zh"], str) and res["message_zh"]


def test_strategy_significance_and_evaluate_signal_empty_inputs():
    """strategy_significance / evaluate_signal 的空与退化输入。"""
    empty = rigor.strategy_significance([], 1.09, n_trials=5)
    assert empty["verdict"] == "insufficient-data"
    assert empty["survives_correction"] is False

    # 单期
    one = rigor.strategy_significance([2.0], 1.09, n_trials=1)
    assert one["verdict"] == "insufficient-data"
    # 样本量 < 3 时不得给出「已校正」的结论：校正字段为 None，且不显著。
    # （p_value 仍保留符号检验的退化值供参考，但绝不能被当作检验结论使用。）
    assert one["p_uncorrected"] is None
    assert one["adjusted_p"] is None
    assert one["bonferroni_p"] is None
    assert one["survives_correction"] is False
    assert "不足" in one["message_zh"]

    # window 不小于数据量、window<1、非可调用对象
    draws = make_draws(50, seed=62)
    assert rigor.evaluate_signal(draws, _hot_strategy, window=50)["n_predictions"] == 0
    assert rigor.evaluate_signal(draws, _hot_strategy, window=999)["verdict"] == "insufficient-data"
    assert rigor.evaluate_signal(draws, _hot_strategy, window=0)["verdict"] == "insufficient-data"
    assert rigor.evaluate_signal(draws, None, window=10)["verdict"] == "insufficient-data"
    assert rigor.evaluate_signal([], _hot_strategy, window=10)["verdict"] == "insufficient-data"
    assert rigor.evaluate_signal(None, _hot_strategy, window=10)["verdict"] == "insufficient-data"


def test_chi_square_uniformity_insufficient_and_degenerate():
    """chi_square_uniformity 的退化输入返回 insufficient-data。"""
    assert rigor.chi_square_uniformity([1])["verdict"] == "insufficient-data"
    assert rigor.chi_square_uniformity([1, 0])["verdict"] == "insufficient-data"
    assert rigor.chi_square_uniformity([])["verdict"] == "insufficient-data"
    # 完全均匀 -> statistic=0, p=1
    out = rigor.chi_square_uniformity([10, 10, 10, 10])
    assert out["statistic"] == pytest.approx(0.0)
    assert out["p_value"] == pytest.approx(1.0)
    assert out["df"] == 3


def test_module_is_pure_no_db_config_or_network_imports():
    """硬要求守卫：rigor 只依赖 stdlib + numpy/scipy。

    不得 import 项目内其他模块（db/config/features…）、不得有网络或文件 IO。
    用源码静态检查，防止后续有人悄悄引入副作用依赖。
    """
    import pathlib
    import re
    src = pathlib.Path(rigor.__file__).read_text(encoding="utf-8")
    # 只允许 stdlib + numpy/scipy
    allowed = {"__future__", "itertools", "math", "typing", "functools",
               "numpy", "scipy"}
    for m in re.finditer(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)",
                         src, re.MULTILINE):
        top = m.group(1).split(".")[0]
        assert top in allowed, f"rigor.py 引入了不允许的依赖: {m.group(1)}"
    # 不得有项目内相对导入 / 网络 / 文件 IO / 环境变量
    for bad in ("from .", "import requests", "import urllib", "import socket",
                "sqlite3", "os.environ", "open(", "import config"):
        assert bad not in src, f"rigor.py 含被禁止的用法: {bad!r}"


def test_module_has_no_global_mutable_state_leak():
    """交错调用不同数据集，结果必须与顺序无关（无全局状态污染）。"""
    d1 = make_draws(60, seed=901, reds=[1, 2, 3, 4, 5, 6])
    d2 = make_draws(60, seed=902, reds=[2, 3, 4, 5, 6, 7])
    a1 = rigor.randomness_report(d1)
    _ = rigor.randomness_report(d2)          # 中间插入另一次调用
    a2 = rigor.randomness_report(d1)
    assert a1 == a2


def test_nan_and_inf_hits_are_filtered():
    """NaN/inf 命中值必须被过滤，不得污染均值与检验。"""
    res = rigor.strategy_significance(
        [1.0, float("nan"), 2.0, float("inf"), 1.5], 1.0, n_trials=1)
    assert res["n"] == 3
    assert np.isfinite(res["mean"])


def test_report_is_pure_and_deterministic():
    """纯函数性：同样输入两次调用结果完全一致；输入不被修改。"""
    draws = make_draws(400, seed=63)
    snapshot = [dict(d, reds=list(d["reds"])) for d in draws]
    a = rigor.randomness_report(draws)
    b = rigor.randomness_report(draws)
    assert a == b
    assert draws == snapshot  # 未被就地修改
    assert a["tests"]["red_frequency"]["statistic"] == \
        b["tests"]["red_frequency"]["statistic"]
