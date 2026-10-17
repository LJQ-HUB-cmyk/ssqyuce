"""统计判据校准 harness（V10-14 / STAT_CALIBRATION_AUDIT 的复用工具，**不是测试文件**）。

为什么需要它
------------
V10 暴露出 3 个同类缺陷，共同点都是「现有测试只验证返回值结构，没验证判据本身是否受控」：

======================  ==================  ==========================================
缺陷                     类型                 怎么漏过去的
======================  ==================  ==========================================
A1 老库零备份            路径边界             只测了「新库」路径
D-1 单侧口径看不见劣绩     判据方向             「显著更差」分支写着但不可达
D-2 零复制不足致假阳性     判据强度             17 条测试全绿，但没测假阳性率
======================  ==================  ==========================================

本模块把「判据是否受控」变成可直接断言的四个量：

* :func:`false_positive_rate` —— 纯噪声（H0 成立）下判显著的频率，应 ≈ α；
* :func:`power` —— 注入真实效应后的检出率，应随效应增大而上升（否则是死判据）；
* :func:`monotonicity` —— 效应网格上的检出率必须单调不减；
* :func:`two_sided_symmetry` —— **A/B 互换后结论必须对称**；单侧判据会在「B 更好」
  时给出与「A 更好」完全不同的检出率，这条能直接抓 D-1。

约定（stat_fn）
---------------
被测判据是一个可调用对象 ``stat_fn(rng, effect=None)``，返回
``float``（未校正 p 值，越小越显著）或 ``dict``（自动从
``adjusted_p`` / ``p_value`` / ``p_uncorrected`` / ``p_two_sided`` 里按优先级取），
或者 ``bool``（已判定的「是否显著」，True 表示显著）。

* ``effect`` 为 ``None`` / ``0`` 表示**注入零效应（纯噪声）**；
* ``effect > 0`` 表示「A 组更好」，``effect < 0`` 表示「A 组更差」。

只接受一参的 ``stat_fn(rng)`` 也支持（用签名探测，缺 effect 时直接调用）。

速度控制
--------
所有函数都接受 ``n_sims``；默认取值很小（几十~几百次），设 ``LOTT_SLOW_TESTS=1``
后自动放大到 :data:`SLOW_MULTIPLIER` 倍。全量测试默认模式应在 20s 内跑完。
"""
from __future__ import annotations

import inspect
import math
import os
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

#: ``LOTT_SLOW_TESTS=1`` 时模拟次数的放大倍数（也只在这时才值得算得更准）
SLOW_MULTIPLIER = 10

#: p 值字段的读取优先级（越靠前越优先）——统一「判据用哪个 p」这件事
P_FIELD_PRIORITY = ("adjusted_p", "p_adjusted", "p_value", "p_uncorrected",
                    "p_two_sided", "p")


def slow_tests_enabled() -> bool:
    return os.environ.get("LOTT_SLOW_TESTS", "") not in ("", "0", "false", "False")


def n_sims(fast: int, slow: Optional[int] = None) -> int:
    """快速模式用 ``fast`` 次；``LOTT_SLOW_TESTS=1`` 时用 ``slow``（默认 fast×10）。"""
    return int(fast) * SLOW_MULTIPLIER if slow_tests_enabled() else int(fast)


# ---------------------------------------------------------------------------
# stat_fn 适配层
# ---------------------------------------------------------------------------

def _accepts_effect(stat_fn: Callable) -> bool:
    """探测 stat_fn 是否接受第二个位置参数（effect）。"""
    try:
        sig = inspect.signature(stat_fn)
    except (TypeError, ValueError):          # 内建函数 / C 函数
        return True
    params = list(sig.parameters.values())
    positional = [p for p in params
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    if any(p.kind == p.VAR_POSITIONAL for p in params):
        return True
    return len(positional) >= 2


def call_stat(stat_fn: Callable, rng: np.random.Generator,
              effect: Optional[float] = None):
    """按被测判据的签名调用它（带 / 不带 effect 都支持）。"""
    if _accepts_effect(stat_fn):
        return stat_fn(rng, effect)
    return stat_fn(rng)


def p_from(result) -> Optional[float]:
    """从判据返回值里取出「用于判显著的 p 值」；无法取出时返回 None。"""
    if result is None:
        return None
    if isinstance(result, (bool, np.bool_)):
        return 0.0 if bool(result) else 1.0
    if isinstance(result, (int, float, np.floating, np.integer)):
        val = float(result)
        return None if not math.isfinite(val) else min(1.0, max(0.0, val))
    if isinstance(result, dict):
        for key in P_FIELD_PRIORITY:
            if key in result and result[key] is not None:
                try:
                    val = float(result[key])
                except (TypeError, ValueError):
                    continue
                if math.isfinite(val):
                    return min(1.0, max(0.0, val))
        # 只有 verdict 字符串时按语义翻译
        verdict = str(result.get("verdict") or ""
                      ).lower().replace("_", "-")
        if verdict.startswith("significant") or verdict in ("improved", "worse"):
            return 0.0
        if verdict in ("not-significant", "notsignificant", "no-signal"):
            return 1.0
    return None


def is_significant(result, alpha: float = 0.05) -> Optional[bool]:
    """判据是否给出「显著」结论；无法判定时返回 None（**当作不算显著**）。"""
    p = p_from(result)
    if p is None:
        return None
    return bool(p < alpha)


# ---------------------------------------------------------------------------
# 四个核心度量
# ---------------------------------------------------------------------------

def false_positive_rate(stat_fn: Callable, n_sims: int = 200,
                        alpha: float = 0.05,
                        rng: Optional[np.random.Generator] = None,
                        effect: Optional[float] = None) -> float:
    """H0（纯噪声 / 零效应）下判显著的频率。判据受控时应 ≈ α。

    ``effect`` 默认 None（= 零效应）；个别判据把「零效应」编码成 0.0，
    传 ``effect=0.0`` 亦可（本函数两种都能用）。
    """
    rng = rng if rng is not None else np.random.default_rng(0)
    hits = 0
    for _ in range(int(n_sims)):
        res = call_stat(stat_fn, rng, effect)
        if is_significant(res, alpha):
            hits += 1
    return hits / float(n_sims)


def power(stat_fn: Callable, effect: float, n_sims: int = 200,
          alpha: float = 0.05,
          rng: Optional[np.random.Generator] = None) -> float:
    """注入真实 ``effect`` 后的检出率（「判显著」的频率）。"""
    return false_positive_rate(stat_fn, n_sims=n_sims, alpha=alpha, rng=rng,
                               effect=float(effect))


def monotonicity(stat_fn: Callable, effect_grid: Sequence[float],
                 n_sims: int = 100, alpha: float = 0.05,
                 rng: Optional[np.random.Generator] = None,
                 tol: float = 0.0) -> Dict:
    """效应网格上的检出率必须单调不减。

    返回 ``{"effects": [...], "rates": [...], "monotone": bool, "violations": [...]}``。
    ``tol`` 允许的回落幅度（默认 0，严格单调；小样本下建议给 0.05~0.1）。
    """
    rng = rng if rng is not None else np.random.default_rng(1)
    grid = [float(e) for e in effect_grid]
    rates = [power(stat_fn, e, n_sims=n_sims, alpha=alpha, rng=rng) for e in grid]
    violations: List[Tuple[float, float, float, float]] = []
    for (e0, r0), (e1, r1) in zip(zip(grid, rates), zip(grid[1:], rates[1:])):
        if abs(e1) >= abs(e0) and r1 + tol < r0:
            violations.append((e0, r0, e1, r1))
    return {"effects": grid, "rates": rates, "violations": violations,
            "monotone": not violations}


def two_sided_symmetry(stat_fn: Callable, effect: float = 1.0,
                       n_sims: int = 200, alpha: float = 0.05,
                       rng: Optional[np.random.Generator] = None,
                       tol: float = 0.10) -> Dict:
    """**A/B 互换后结论是否对称**（直接抓单侧口径 / D-1）。

    同一个真实效应量 ``|effect|``：A 更好时应被检出，A 更差时也应以差不多的
    概率被检出（双侧判据）。**单侧判据**在「A 更差」时的检出率会塌到 ≈0，
    差异 = 不对称量。

    返回 ``{"positive": rate+, "negative": rate-, "asymmetry": |rate+ - rate-|,
    "symmetric": bool, "tol": tol}``。
    """
    rng = rng if rng is not None else np.random.default_rng(2)
    r_pos = power(stat_fn, abs(float(effect)), n_sims=n_sims, alpha=alpha, rng=rng)
    r_neg = power(stat_fn, -abs(float(effect)), n_sims=n_sims, alpha=alpha, rng=rng)
    asym = abs(r_pos - r_neg)
    return {"positive": r_pos, "negative": r_neg, "asymmetry": asym,
            "symmetric": bool(asym <= tol), "tol": float(tol)}


# ---------------------------------------------------------------------------
# 断言辅助（α ± 3σ 容差）
# ---------------------------------------------------------------------------

def fpr_tolerance(alpha: float, n_sims: int, sigmas: float = 3.0) -> float:
    """二项抽样下 FPR 估计的 3σ 容差：3·sqrt(α(1−α)/n)。"""
    return float(sigmas) * math.sqrt(max(0.0, alpha * (1.0 - alpha)) / max(1, int(n_sims)))


def assert_calibrated(fpr: float, alpha: float, n_sims: int,
                      sigmas: float = 3.0, label: str = "") -> None:
    """断言 FPR 落在 ``α ± sigmas·sqrt(α(1−α)/n)`` 内（判据受控）。"""
    tol = fpr_tolerance(alpha, n_sims, sigmas)
    if abs(float(fpr) - float(alpha)) > tol:
        raise AssertionError(
            f"{label + ': ' if label else ''}假阳性率 {fpr:.4f} 偏离 α={alpha} 超过 "
            f"{sigmas:g}σ 容差 ±{tol:.4f}（n_sims={n_sims}）")
    return None


def assert_power_at_least(rate: float, floor: float, label: str = "") -> None:
    """断言功效不低于 ``floor``（防止「死判据」：永远判不显著）。"""
    if float(rate) < float(floor):
        raise AssertionError(f"{label + ': ' if label else ''}功效 {rate:.3f} 低于下限 {floor}")


def assert_symmetric(sym: Dict, label: str = "") -> None:
    """断言 :func:`two_sided_symmetry` 的结果对称。"""
    if not sym.get("symmetric"):
        raise AssertionError(
            f"{label + ': ' if label else ''}判据不对称：A 更好检出 {sym['positive']:.3f}，"
            f"A 更差检出 {sym['negative']:.3f}，不对称量 {sym['asymmetry']:.3f} > "
            f"容差 {sym['tol']:.2f}（典型病因：用单侧 p 判双边问题）")


# ---------------------------------------------------------------------------
# 常用模拟器（体检测试与实现共用，避免每处重写一遍抽样）
# ---------------------------------------------------------------------------

def paired_hit_series(rng: np.random.Generator, n: int, *,
                      n_options_a: int = 6, n_options_b: int = 6,
                      pool: int = 33, picks: int = 6,
                      bias_a: float = 0.0, bias_b: float = 0.0) -> Tuple[np.ndarray, np.ndarray]:
    """模拟「A / B 两种选号策略」逐期命中数（超几何），可注入偏置。

    做法：每期从 ``pool`` 个红号里随机抽 ``picks`` 个作为开奖结果；策略 A/B 各自
    用带偏置的权重抽 ``n_options`` 个号。``bias>0`` 让该策略更容易抽到开奖号
    （= 更强），``bias<0`` 更容易避开（= 更弱）。返回 (a_hits, b_hits)。
    """
    a_hits = np.empty(n, dtype=float)
    b_hits = np.empty(n, dtype=float)

    def _draw(bias: float, k: int) -> np.ndarray:
        if bias == 0.0:
            weights = np.ones(pool)
        else:
            weights = np.exp(bias * rng.normal(size=pool))
        weights = weights / weights.sum()
        return rng.choice(pool, size=min(k, pool), replace=False, p=weights)

    for i in range(n):
        draw = rng.choice(pool, size=picks, replace=False)
        a = _draw(float(bias_a), n_options_a)
        b = _draw(float(bias_b), n_options_b)
        a_hits[i] = len(set(a.tolist()) & set(draw.tolist()))
        b_hits[i] = len(set(b.tolist()) & set(draw.tolist()))
    return a_hits, b_hits


def two_sample_diff(rng: np.random.Generator, n: int, effect: float,
                    sd: float = 1.0) -> Tuple[np.ndarray, np.ndarray]:
    """零假设/备择假设都精确可控的配对差值模拟（检验器自检用）。"""
    a = rng.normal(0.0, sd, n)
    b = a - float(effect)          # 配对：差值恒为 effect（含共同噪声）
    return a, b


def binom_rate_with_ci(k: int, n: int, z: float = 1.96) -> Dict[str, float]:
    """比例 + Wald 置信区间（报告里给实测 fpr 时用）。"""
    p = k / float(n) if n else 0.0
    se = math.sqrt(p * (1 - p) / n) if n else 0.0
    return {"p": float(p), "se": float(se),
            "lo": float(max(0.0, p - z * se)), "hi": float(min(1.0, p + z * se)),
            "n": int(n), "k": int(k)}
