"""P4：e-process 判定器（闲时挖掘的统计核心）。

## 验收口径（项目传统，必须两条同时成立）

* **注入偏置必检出** —— 有真实信号时必须能发现；
* **纯随机不误报** —— 没有信号时错误率不超过名义 α。

第二条尤其重要：要求"必检出"而不约束误报，就会造出一个永远喊"找到规律了"的系统。

## 为什么是 e-process 而不是 Bonferroni

闲时挖掘 = 反复看数据、随时可能停（optional continuation）。
固定样本量的校正在这种场景下会被"看多少次"摧毁：
每天看一次、连看 30 天，α=0.05 的实际错误率约 1-(1-0.05)^30 ≈ 78%。
e-process 对**任意停止规则**都有效（Ville 不等式），看多少次都不膨胀。
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from lottery import eprocess as E


def test_empty_sequence_gives_m0_equals_1():
    """空序列 = 未积累证据，M_0 必须是 1（不是 0）。"""
    assert E.e_value([]) == pytest.approx(1.0)
    assert E.log_e_value([]) == pytest.approx(0.0)


def test_threshold_is_inverse_alpha():
    assert E.threshold(0.05) == pytest.approx(20.0)
    assert E.threshold(0.01) == pytest.approx(100.0)


def test_threshold_rejects_invalid_alpha():
    with pytest.raises(ValueError):
        E.threshold(0.0)
    with pytest.raises(ValueError):
        E.threshold(1.5)


def test_calibration_matches_m2_documented_value():
    """标定对齐 M2 §4.3：cold+50%/号（Δ=0.135）约 400 期首次穿越。

    这条防止 τ 或公式被无意改动 —— 改了标定就等于改了系统对
    "多小的效应值得等"的承诺。
    """
    n = E.expected_crossing_issues(0.135, sigma=0.8678, tau=0.10, alpha=0.05)
    assert n is not None
    assert 300 <= n <= 500, f"标定漂移：期望约 400 期，实际 {n}"

    # 更小的效应应该需要更久
    n_small = E.expected_crossing_issues(0.054, sigma=0.8678, tau=0.10)
    assert n_small > n


def test_injected_bias_is_detected():
    """**注入偏置必检出**：有真实效应时必须能发现。"""
    rng = np.random.default_rng(42)
    z = rng.normal(0.1556, 1.0, 600)      # cold+50%/号 的效应量
    assert E.first_crossing(z) is not None, "600 期的真实效应竟未被检出"
    v = E.verdict(z, min_obs=30)
    assert v["decision"] == "usable"
    assert v["alpha_spent"] == pytest.approx(0.05)


def test_pure_random_does_not_inflate_error_rate():
    """**纯随机不误报**：anytime-valid 的核心承诺。

    模拟 200 条纯随机序列，每条 500 期且**随时可停**。
    名义 α=0.05，实测误报率必须 ≤ 0.08（留一点蒙特卡洛余量）。
    """
    rng = np.random.default_rng(7)
    trials = 200
    false_pos = sum(
        1 for _ in range(trials)
        if E.first_crossing(rng.normal(0.0, 1.0, 500)) is not None)
    rate = false_pos / trials
    assert rate <= 0.08, (
        f"纯随机误报率 {rate:.3f} 超过名义 α=0.05 太远 —— "
        f"e-process 的 anytime-valid 保证被破坏了")


def test_underpowered_is_not_usable():
    """样本不足时**不下结论** —— 尤其不能因为"还没拒绝"就当成有效。"""
    v = E.verdict([0.1] * 5, min_obs=30)
    assert v["decision"] == "underpowered"
    assert "样本不足" in v["note_zh"]


def test_verdict_carries_audit_fields():
    """L-C 报告层要求：每条结论必须自带可审计字段（缺任一前端应拒绝渲染）。"""
    v = E.verdict(list(np.random.default_rng(1).normal(0.2, 1, 100)))
    for k in ("e_value", "log_e_value", "alpha_spent", "n_obs",
              "threshold", "tau", "decision", "note_zh"):
        assert k in v, f"缺少可审计字段 {k}"


def test_log_domain_avoids_overflow():
    """长序列下 e_value 会溢出，但 log_e_value 必须仍然有限。"""
    z = list(np.random.default_rng(2).normal(0.3, 1, 5000))
    log_m = E.log_e_value(z)
    assert math.isfinite(log_m)
    v = E.verdict(z)
    assert v["e_value"] == float("inf") or math.isfinite(v["e_value"])


def test_no_signal_stays_monitor_not_rejected():
    """没有证据时状态是 `monitor`（继续观察），不是 `rejected`。

    这个区别很重要：`rejected` 意味着"已判定无效"，
    而 `monitor` 只表示"目前还没有足够证据"。混用会让系统过早放弃。
    """
    rng = np.random.default_rng(99)
    z = rng.normal(0.0, 1.0, 150)
    v = E.verdict(z, min_obs=30)
    assert v["decision"] in ("monitor", "usable")
    if not E.is_significant(z):
        assert v["decision"] == "monitor"


# ---------- 方差口径（D3，2026-10-21 生产事故） ----------

def test_sigma_matches_hypergeometric_exactly():
    """**必须与 scipy 超几何精确值一致**（含有限总体校正）。

    这里钉死具体数值：旧代码写死 `sigma=0.8678`（= k=6 的红球值），
    对 k=11 的真实值 1.0607 会低估方差 1.22 倍，导致 z 被放大、
    e 值高估 61 倍，31 条并集候选被误判为「可用」。
    """
    cases = {1: 0.3857, 3: 0.6468, 6: 0.8678, 8: 0.9642, 10: 1.0340, 11: 1.0607, 12: 1.0824}
    for k, want in cases.items():
        got = E.sigma_hypergeom(k, "red")
        assert abs(got - want) < 5e-4, f"k={k} 得 {got:.4f}，应为 {want}"


def test_sigma_blue_matches_binomial():
    """蓝球：Var = k·p·(1-p)，p=1/16。"""
    for k, want in {1: 0.2421, 2: 0.3423, 3: 0.4193}.items():
        got = E.sigma_hypergeom(k, "blue")
        assert abs(got - want) < 5e-4, f"blue k={k} 得 {got:.4f}，应为 {want}"


def test_sigma_grows_with_k_until_finite_population_kicks_in():
    """筛选集越大方差越大 —— 但**超过一半号码后有限总体校正会让方差回落**。

    k=6（理论期望值）→ 0.868，k=11 → 1.061，k=15 → 1.120 峰值，
    k=20 → 1.099 已开始回落。这不是 bug：从 33 个号里选 20 个以上时，
    命中数被上界 6 压住，方差必然收缩。
    """
    rising = [E.sigma_hypergeom(k) for k in (1, 3, 6, 10, 15)]
    assert rising == sorted(rising), f"k≤15 区间 sigma 应单调增长: {rising}"
    assert E.sigma_hypergeom(20) < E.sigma_hypergeom(15), "k 接近总体大小时应回落"


def test_sigma_zero_when_k_is_zero():
    """空筛选集没有方差可言，返回 0 让调用方跳过（不产生假 z）。"""
    assert E.sigma_hypergeom(0) == 0.0


def test_empty_periods_are_counted_not_skipped():
    """**空期（exp=0）必须计入 z=0，不能跳过**（2026-10-21 第二次事故）。

    跳过空期是典型的选择偏差 —— 只统计「筛选条件触发了」的期，
    等于把 80% 的观测扔掉，剩下的看起来自然高度一致。

    实测 `hot{k=7,w=10} ∩ omission_high{k=10}`：
        跳过空期 n=641  e=11 351  ← 假阳性
        保留空期 n=3217 e=1.75    ← 正确
    高估 6490 倍。
    """
    scan = {"obs": [0.0, 2.0, 3.0], "exp": [0.0, 1.0, 1.5], "k_mean": 6}
    z = E.z_scores_from_scan(scan, "red")
    assert len(z) == 3, "空期被跳过了 —— 这是选择偏差"
    assert z[0] == 0.0, "空期应贡献 z=0（命中必然为 0）"


def test_all_empty_scan_returns_empty():
    """整段筛选集恒为空时返回空序列，交由调用方判 underpowered。"""
    scan = {"obs": [0.0] * 100, "exp": [0.0] * 100, "k_mean": 0.0}
    assert E.z_scores_from_scan(scan, "red") == []


def test_union_does_not_inflate_e_value_beyond_threshold():
    """**回归防护**：并集候选按正确方差定标后，e 值应大幅回落。

    实测 `hot{k=7,w=100} ∪ sum_range` 的 k_mean=11.21：
      旧口径（固定 0.8678）e=34612 → 假阳性
      新口径（ka=11.21 → sigma=1.0656）e≈568 → 合理
    这里用合成序列锁定「k 大时必须用大 sigma」这一行为。
    """
    rng = np.random.default_rng(7)
    # 构造 k=11 的扫描结果：持续的正向偏离（模拟真实存在的中等效应）
    obs = [2.10 + rng.normal(0, 1.0, 1)[0] for _ in range(500)]
    exp = [2.04] * 500
    scan = {"obs": obs, "exp": exp, "k_mean": 11.21}
    z_new = E.z_scores_from_scan(scan, "red")
    e_new = E.verdict(z_new, min_obs=30)["e_value"]
    z_old = [(o - e) / 0.8678 for o, e in zip(obs, exp)]
    e_old = E.verdict(z_old, min_obs=30)["e_value"]
    assert e_new < e_old, "按 k 定标后 e 值应低于固定 sigma 的口径"
    # sigma 比为 1.0656/0.8678 = 1.228，e 值按平方放大，故 >1.3 是可断言的
    assert e_old / max(e_new, 1e-9) > 1.3, f"低估方差导致高估不足: {e_old/e_new:.2f}x"
