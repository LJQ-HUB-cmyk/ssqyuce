"""e-process 判定器（P4 闲时挖掘的统计核心）。

## 为什么闲时挖掘**必须**用 e-process，而不是固定样本量的校正

"闲时全天候挖掘"意味着**反复看数据、随时可能停止**。这在统计上叫
optional continuation —— 而固定样本量的任何校正（包括 Bonferroni）都会被
"看多少次"摧毁：

* 每天看一次、连续看 30 天，α=0.05 的检验实际错误率约 **1-(1-0.05)^30 ≈ 78%**；
* 全量 Bonferroni 也救不了，因为它假设"检验次数是**事先固定**的"，
  而闲时挖掘的次数取决于数据长什么样（看到有苗头就多看几次）。

e-process 是专为这种场景设计的：它对**任意停止规则**都有效
（Ville 不等式：`P(sup_t M_t >= 1/α) <= α`），看多少次都不膨胀。

## 数学构造（标准混合超鞅，Howard et al. 2021）

观测 `X_i` 标准化为 `z_i`（H0 下 ≈ N(0,1)），`S_t = Σ_{i<=t} z_i`，
先验 `θ ~ N(0, τ²)`：

    M_t = 1/sqrt(1+τ²t) · exp( τ²S_t² / (2(1+τ²t)) )

    E_H0[M_t] <= 1,    P_H0(∃t: M_t >= 1/α) <= α

`α=0.05` ⇒ 阈值 `1/α = 20`，`log 20 = 2.996`。

## τ 的选择

τ 决定"对哪个效应量最敏感"。M2 §4.3 用实测效应（σ=0.8678）标定后建议
**τ = 0.10**：在"小效应慢一点、大效应快一点"之间最平衡。

    cold +10%/号（Δ=0.054）→ 约 2490 期首次穿越
    cold +50%/号（Δ=0.135）→ 约 400 期

## 这个模块的边界

**只做数学，不碰 IO、不碰 DB、不碰配置**。这样它能被独立测试到
（"注入偏置必检出 + 纯随机不误报"是项目的一贯验收口径），
调度层再怎么改都不会动到判定逻辑。
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence

#: 默认先验尺度（M2 §4.3 标定结果）
DEFAULT_TAU = 0.10
#: 默认显著性水平
DEFAULT_ALPHA = 0.05


def log_e_value(z_scores: Sequence[float], *, tau: float = DEFAULT_TAU) -> float:
    """返回 log M_t（对数域，避免长序列下溢）。

    `z_scores` 是已标准化的观测值序列。空序列返回 0（= M_0 = 1，未积累证据）。
    """
    t = len(z_scores)
    if t <= 0:
        return 0.0
    s = float(sum(z_scores))
    tau2 = tau * tau
    denom = 1.0 + tau2 * t
    # log M_t = -0.5*log(1+τ²t) + τ²S_t²/(2(1+τ²t))
    return -0.5 * math.log(denom) + (tau2 * s * s) / (2.0 * denom)


def e_value(z_scores: Sequence[float], *, tau: float = DEFAULT_TAU) -> float:
    """返回 M_t（非对数域）。长序列可能溢出为 inf，判断用 `verdict()`。"""
    return math.exp(log_e_value(z_scores, tau=tau))


def threshold(alpha: float = DEFAULT_ALPHA) -> float:
    """拒绝 H0 的阈值 `1/alpha`。"""
    if not (0.0 < alpha < 1.0):
        raise ValueError("alpha 必须在 (0,1) 之间")
    return 1.0 / alpha


def is_significant(z_scores: Sequence[float], *, tau: float = DEFAULT_TAU,
                   alpha: float = DEFAULT_ALPHA) -> bool:
    """e-value 是否已越过 `1/alpha`（anytime-valid 的拒绝判据）。"""
    return log_e_value(z_scores, tau=tau) >= math.log(threshold(alpha))


def first_crossing(z_scores: Sequence[float], *, tau: float = DEFAULT_TAU,
                   alpha: float = DEFAULT_ALPHA) -> Optional[int]:
    """首次越过阈值的期数（1-based）；从未越过返回 None。

    用于回答"这个效应多快能被发现"，也是标定 τ 的工具。
    """
    log_thr = math.log(threshold(alpha))
    tau2 = tau * tau
    s = 0.0
    for i, z in enumerate(z_scores, start=1):
        s += float(z)
        denom = 1.0 + tau2 * i
        log_m = -0.5 * math.log(denom) + (tau2 * s * s) / (2.0 * denom)
        if log_m >= log_thr:
            return i
    return None


def verdict(z_scores: Sequence[float], *, tau: float = DEFAULT_TAU,
            alpha: float = DEFAULT_ALPHA,
            min_obs: int = 30) -> Dict:
    """给出可审计的判定（供 L-C 报告层直接渲染）。

    返回字段刻意与 M2 §4.2 的报告层要求对齐 —— **缺任一字段前端应拒绝渲染**：
    `e_value` / `log_e_value` / `alpha_spent` / `n_obs` / `threshold` / `decision`。
    """
    n = len(z_scores)
    log_m = log_e_value(z_scores, tau=tau)
    thr = threshold(alpha)
    sig = log_m >= math.log(thr)
    if n < min_obs:
        decision = "underpowered"        # 样本不足，不下结论（也**不**当作"有效"）
    elif sig:
        decision = "usable"
    else:
        decision = "monitor"             # 尚无证据，但保持监控（不是"否决"）
    return {
        "e_value": math.exp(log_m) if log_m < 700 else float("inf"),
        "log_e_value": log_m,
        "alpha_spent": alpha if sig else 0.0,
        "n_obs": n,
        "threshold": thr,
        "tau": tau,
        "decision": decision,
        "note_zh": (
            "样本不足，不下结论" if decision == "underpowered"
            else "e-value 越过 1/α，可在任意停止规则下拒绝 H0" if sig
            else "尚无足够证据；e-process 允许继续观察而不膨胀错误率"),
    }


def expected_crossing_issues(delta: float, sigma: float = 0.8678,
                             *, tau: float = DEFAULT_TAU,
                             alpha: float = DEFAULT_ALPHA) -> Optional[int]:
    """给定真实效应，估算首次穿越所需的期数（用于 `planned_mde` 与资源规划）。

    `delta` 是相对基线的绝对效应（如 +0.054 表示每号多 0.054 次命中）。
    这是近似解析估算，用于规划，不是判定依据。
    """
    if sigma <= 0:
        return None
    zbar = abs(delta) / sigma
    if zbar <= 1e-9:
        return None
    # 用首达时间的粗略近似：E[M_t] 的下界 exp(τ²(zbar·t)²/(2(1+τ²t))) 达 1/α
    # ⇒ τ²(zbar·t)²/(2(1+τ²t)) ≈ log(1/α)
    # 解一元二次可得，这里直接用数值搜索，稳健且够快
    log_thr = math.log(threshold(alpha))
    tau2 = tau * tau
    for t in range(1, 200000):
        s_over_denom = tau2 * (zbar * t) ** 2 / (2.0 * (1.0 + tau2 * t))
        if s_over_denom - 0.5 * math.log(1.0 + tau2 * t) >= log_thr:
            return t
    return None
