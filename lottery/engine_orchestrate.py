"""S9 组合覆盖优化（可插拔目标 + 蓝球职责边界）。

## 迁移说明（D-02 拆分 S9，最后一批）

内容从 `lottery/engine.py` 原 588–910 行**逐字迁移**，逻辑未改一行。
迁出：`COVERAGE_OBJECTIVES` / `DEFAULT_CUSTOM_WEIGHTS` / `_PMAX_SEARCH_DRAWS` /
`_normalize_coverage_objective` / `_custom_objective_weights` / `_PmaxEstimator` /
`_ticket_objective` / `_objective_meta` / `coverage_optimize` /
`coverage_optimize_compare`。

## 诚实边界（原文保留，本模块最容易被误读）

`coverage_optimize` 优化的是**给定目标函数**：
* `ev_conserving` —— 保持期望收益不变的前提下改善结构
* `pmax_high` —— 最大化「至少一注命中 ≥k 红」的概率
* `coverage_max` —— 最大化号码覆盖率

**这些都不是「提高中奖概率」**：
所有目标都由组合数学决定，优化只能在**已定的概率空间里挑组合**。
`E[命中注数]` 恒为 `n/16`（蓝球）、红球各奖级概率只由超几何分布决定 ——
**买什么组合，长期回报都是负的**（本项目单注返奖比 ≈52.3%）。

## 为什么本文件不 import engine

`from . import engine as E` 会在 engine 尚未初始化完时成环。
基础常量从 `engine_base` 直取，被复用函数从各子模块真源直取。
"""
from __future__ import annotations

import random
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from . import config      # noqa: F401
from .engine_base import R_MAX, B_MAX, TICKET_COST  # noqa: F401  纯常量
from .engine_distribution import (  # noqa: F401
    RED_PER_TICKET,
    REDUNDANCY_MAX,
    _exact_red_tail,
    _pairwise_stats,
    _popcount33,
    _red_mask,
    _validate_tickets,
    ticket_distribution,
)
from .engine_prize import portfolio_expected_value  # noqa: F401
from .engine_blue import blue_coverage_plan  # noqa: F401

COVERAGE_OBJECTIVES = ("ev_conserving", "pmax_high", "coverage", "custom_weight")

# custom_weight 的默认权重（可由 config.COVERAGE_OBJECTIVE_WEIGHTS 或调用参数覆盖）
DEFAULT_CUSTOM_WEIGHTS = {
    "pmax_high": 1.0,        # P(≥1 注 ≥r 红) 估计值（0..1）
    "union": 0.5,            # 并集覆盖 / 33（0..1）
    "neg_redundancy": 0.5,   # 1 − Ā/6（0..1，1 = 完全不重叠）
    "blue_coverage": 0.0,    # 蓝球覆盖度 / 16（仅在 plan 未接管蓝球时有效）
}

# 搜索期 P(≥1注≥r红) 的公共随机数 MC 样本量（越大越准、越慢；2000 档的
# 标准误 ≈ sqrt(p(1-p)/2000) ≈ 0.003，够排序用，最终值一律用精确枚举复核）
_PMAX_SEARCH_DRAWS = 2000


def _normalize_coverage_objective(objective: Optional[str]) -> str:
    """把目标名（含 V10-00a 别名表）规范化为 4 个合法值之一，未知值回退 ev_conserving。"""
    if not objective:
        return "ev_conserving"
    key = str(objective).strip().lower()
    aliases = getattr(config, "COVERAGE_OBJECTIVE_ALIASES", {}) or {}
    resolved = aliases.get(key, key)
    return resolved if resolved in COVERAGE_OBJECTIVES else "ev_conserving"


def _custom_objective_weights(weights: Optional[Dict] = None) -> Dict[str, float]:
    """合并「默认 ← config.COVERAGE_OBJECTIVE_WEIGHTS ← 调用参数」的权重表。"""
    merged = dict(DEFAULT_CUSTOM_WEIGHTS)
    for src in (getattr(config, "COVERAGE_OBJECTIVE_WEIGHTS", None), weights):
        if not isinstance(src, dict):
            continue
        for k, v in src.items():
            if k not in merged:
                continue
            try:
                merged[k] = float(v)
            except (TypeError, ValueError):
                continue
    return merged


class _PmaxEstimator:
    """搜索期的 P(≥1注≥r红) 估计器：固定种子「公共随机数」+ 逐注命中布尔向量缓存。

    为什么不用解析式：真实概率需要对 C(33,6)=1,107,568 种开奖全枚举（约 0.1~0.3s），
    模拟退火每步都要算，放不进搜索；而泊松二项忽略票间相关（5 注全同时高估 5 倍）。
    折中：搜索用同一批随机开奖（common random numbers，方差小、候选间可比），
    最终展示值仍由 ``ticket_distribution(with_distribution_exact=True)`` 精确给出。
    """

    def __init__(self, r: int = 4, n_draws: int = _PMAX_SEARCH_DRAWS, seed: int = 0):
        self.r = int(r)
        rng = np.random.default_rng(int(seed))
        keys = rng.random((int(n_draws), R_MAX))
        picks = np.argpartition(keys, RED_PER_TICKET, axis=1)[:, :RED_PER_TICKET]
        self.draw_masks = np.bitwise_or.reduce(
            np.left_shift(np.int64(1), picks.astype(np.int64)), axis=1).astype(np.uint64)
        self._cache: Dict[tuple, np.ndarray] = {}

    def _hit_ge(self, reds) -> np.ndarray:
        key = tuple(int(x) for x in reds)
        arr = self._cache.get(key)
        if arr is None:
            arr = _popcount33(self.draw_masks & np.uint64(_red_mask(key))) >= self.r
            self._cache[key] = arr
        return arr

    def p(self, tickets: List[Dict]) -> float:
        if not tickets:
            return 0.0
        any_hit = np.zeros(self.draw_masks.shape[0], dtype=bool)
        for t in tickets:
            any_hit |= self._hit_ge(t["reds"])
        return float(any_hit.mean())


def _ticket_objective(tickets: List[Dict], objective: str = "ev_conserving", r: int = 3,
                      *, estimator: Optional[_PmaxEstimator] = None,
                      weights: Optional[Dict] = None,
                      blue_owned_by_plan: bool = False) -> float:
    """覆盖优化目标函数（V10-03 可插拔；语义见本节顶部冻结契约）。

    参数
    ----
    objective : 4 个合法值（别名经 config.COVERAGE_OBJECTIVE_ALIASES 归一）。
    r         : 「命中 ≥r 红」的 r，只有 pmax_high / custom_weight 用到。
    estimator : pmax_high / custom_weight 用的 MC 估计器；缺省时退回精确枚举
                （单次约 0.1~0.3s，仅在直接调用本函数时发生，搜索里必须传）。
    blue_owned_by_plan : True 表示蓝球马上会被 blue_coverage_plan 改写
                → **目标里绝不能含蓝球项**（否则就是零梯度项）。

    注意：**没有任何模式包含 EV/奖金项**。同注数下 EV 恒等（n×单注 EV），
    把它写进目标只会给优化器注入常数噪声，还会诱导「优化能提高期望」的错误叙事。
    """
    if not tickets:
        return 0.0
    obj = _normalize_coverage_objective(objective)
    reds_list = [[int(x) for x in t["reds"]] for t in tickets]
    pair = _pairwise_stats(reds_list)
    union = pair["union_coverage"]
    abar = pair["abar"]
    distinct_blue = len({int(t.get("blue", 0)) for t in tickets
                         if 1 <= int(t.get("blue", 0)) <= B_MAX})
    # 蓝球项只在「蓝球不被 plan 接管」时才是真实自由度
    blue_term = 0.0 if blue_owned_by_plan else (distinct_blue / float(B_MAX))

    if obj == "coverage":
        # 旧口径：红球并集 + 1.5×蓝球覆盖数；蓝球被 plan 接管时该项删除（零梯度修复）
        return float(union) + (0.0 if blue_owned_by_plan else 1.5 * distinct_blue)

    if obj == "ev_conserving":
        # EV 恒等 → 只比结构：并集覆盖越大越好、冗余度 Ā 越小越好
        score = float(union) - float(abar)
        if not blue_owned_by_plan:
            score += 1.5 * distinct_blue      # 蓝球未被 plan 接管时，覆盖数是真实可优化量
        return float(score)

    if obj == "pmax_high":
        if estimator is not None:
            return float(estimator.p(tickets))
        return float(_exact_red_tail(_validate_tickets(tickets))["p_ge"].get(int(r), 0.0))

    # custom_weight：归一化项的加权和（不含量纲混杂；不含 EV）
    w = _custom_objective_weights(weights)
    p_high = (estimator.p(tickets) if estimator is not None
              else float(_exact_red_tail(_validate_tickets(tickets))["p_ge"].get(int(r), 0.0)))
    terms = {
        "pmax_high": p_high,
        "union": union / float(R_MAX),
        "neg_redundancy": 1.0 - (abar / REDUNDANCY_MAX if REDUNDANCY_MAX > 0 else 0.0),
        "blue_coverage": blue_term,
    }
    return float(sum(w.get(k, 0.0) * v for k, v in terms.items()))


def _objective_meta(objective: str, r: int, *, blue_owned_by_plan: bool,
                    estimator: Optional[_PmaxEstimator] = None) -> Dict:
    """目标函数的自述元数据（供 compare / 前端说明「这个目标在优化什么、不优化什么」）。"""
    obj = _normalize_coverage_objective(objective)
    return {
        "objective": obj,
        "objective_input": objective,
        "r": int(r),
        "optimizes_red_structure": obj in ("ev_conserving", "pmax_high", "coverage", "custom_weight"),
        "optimizes_ev": False,          # 恒 False：任何模式都不允许把目标写成「提高期望」
        "ev_identity_note": "同注数下 EV 恒为 n×单注 EV，与票单结构无关；本目标不含 EV 项",
        "blue_owned_by_plan": bool(blue_owned_by_plan),
        # 蓝球被 plan 接管 ⇒ 目标里的蓝球项对最终输出零梯度，故已从目标中删除
        "gradient_blue_degenerate": bool(blue_owned_by_plan),
        "pmax_estimator": ("mc_common_random_numbers" if (estimator is not None
                                                          and obj in ("pmax_high", "custom_weight"))
                           else None),
        "note_zh": ("蓝球由 blue_coverage_plan 闭式负责；目标只作用于红球子问题。"
                    "pmax_high 的方差代价须用 ticket_distribution()['var_ratio'] 一并展示。"),
    }


def coverage_optimize(pool: List[Dict], n_tickets: int, r: int = 3,
                      n_blues: int = 4,
                      trials: int = 3000, rng: Optional[random.Random] = None,
                      objective: str = "expected_value",
                      blue_probs: Optional[np.ndarray] = None,
                      objective_weights: Optional[Dict] = None) -> List[Dict]:
    """U4 覆盖优化器：从候选池选 n_tickets 注（贪心初始化 + 模拟退火精修）。

    objective（V10-03 可插拔，别名经 config.COVERAGE_OBJECTIVE_ALIASES 归一）：
      - "ev_conserving" / 旧名 "expected_value"（默认）：EV 守恒 —— 目标里**没有
        EV 项**（EV 恒等），只比红球结构（并集覆盖 − 冗余度 Ā）。
      - "pmax_high"：直接最大化 P(≥1 注命中 ≥r 红)；搜索期用公共随机数 MC 估计，
        结果须配 ``ticket_distribution()`` 的精确概率与 ``var_ratio`` 展示方差代价。
      - "coverage"：旧口径（红球并集 + 蓝球覆盖数），保留向后兼容。
      - "custom_weight"：用户偏好加权（``objective_weights`` 或
        config.COVERAGE_OBJECTIVE_WEIGHTS）。

    **蓝球职责边界（冻结）**：``blue_probs`` 给定时，蓝球由
    :func:`blue_coverage_plan` 闭式决定并在最后统一改写；因此此时**任何目标里都
    不含蓝球项**（含蓝球项就是零梯度，只会往红球选择里注入蓝球噪声）。
    ``blue_probs`` 未给定时蓝球保持候选池原值，此时蓝球覆盖数才是真实自由度。

    诚实边界：本优化器改变的是**中奖频率的分布**（覆盖结构 / 方差），
    **不改变期望收益**；任何把目标写成「提高期望」的实现都是错的。
    """
    rng = rng if rng is not None else random.Random()
    pool = [dict(t) for t in pool]
    obj = _normalize_coverage_objective(objective)
    if n_tickets <= 0 or not pool:
        return []

    # 蓝球覆盖计划前置：先把蓝球按概率降序铺满（n<=16 时互不相同）
    plan = blue_coverage_plan(n_tickets, blue_probs) if blue_probs is not None else None
    blue_owned = plan is not None
    estimator = None
    if obj in ("pmax_high", "custom_weight"):
        estimator = _PmaxEstimator(r=r, n_draws=_PMAX_SEARCH_DRAWS,
                                   seed=rng.getrandbits(31))

    def _score(sel: List[Dict]) -> float:
        return _ticket_objective(sel, obj, r, estimator=estimator,
                                 weights=objective_weights,
                                 blue_owned_by_plan=blue_owned)

    if len(pool) <= n_tickets:
        out = pool[:n_tickets]
        if plan:
            for t, b in zip(out, plan):
                t["blue"] = int(b)
        return out

    # 目标退化（候选池里所有注的红球完全相同）→ 任何选择目标值相同，退火无意义
    distinct_red_sets = {tuple(sorted(int(x) for x in t["reds"])) for t in pool}
    objective_degenerate = len(distinct_red_sets) == 1

    # 贪心：逐步加入使目标函数增益最大的一注
    selected: List[Dict] = []
    remaining = list(pool)
    cur = 0.0
    while len(selected) < n_tickets and remaining:
        best, best_gain = None, None
        for t in remaining:
            sc = _score(selected + [t])
            gain = sc - cur
            if best_gain is None or gain > best_gain:
                best, best_gain = t, gain
        remaining.remove(best)
        selected.append(best)
        cur = _score(selected)

    # 模拟退火：交换单注（目标退化时跳过——判据是「极差为 0」，不是拍一个迭代数）
    current = list(selected)
    cur_score = cur
    best_sel = list(current)
    best_score = cur_score
    for it in range(0 if objective_degenerate else trials):
        if not remaining:
            remaining = [t for t in pool if t not in current]
        if not remaining:
            break
        i = rng.randrange(len(current))
        j = rng.randrange(len(remaining))
        old_t = current[i]
        new_t = remaining[j]
        current[i] = new_t
        nb_score = _score(current)
        delta = nb_score - cur_score
        # 接受更优；等分时以随温度衰减的概率接受（探索），随迭代降温
        if delta > 0 or rng.random() < (0.5 / (1 + it ** 0.6)):
            cur_score = nb_score
            if nb_score > best_score:
                best_score = nb_score
                best_sel = list(current)
        else:
            current[i] = old_t
    out = best_sel[:n_tickets]
    if plan:
        # 计划重排蓝球：保证 n<=16 时覆盖满不同蓝球（覆盖是结构性保证）
        for t, b in zip(out, plan):
            t["blue"] = int(b)
            t["blue_planned"] = True
    return out


def coverage_optimize_compare(pool: List[Dict], n_tickets: int, r: int = 3,
                              n_blues: int = 4, trials: int = 3000,
                              rng: Optional[random.Random] = None,
                              objective: str = "expected_value",
                              blue_probs: Optional[np.ndarray] = None,
                              objective_weights: Optional[Dict] = None) -> Dict:
    """覆盖优化前后对比（V4）：返回优化前的朴素基线指标与优化后的指标。

    优化前基线 = 候选池按置信度降序取前 n 注（不做任何覆盖优化）。
    返回 ``{"before": {...}, "after": {...}, "improvement": {...}, "objective": ...,
    "objective_effective": ..., "objective_meta": {...}}``。

    V10-03 新增 ``objective_meta``：
      * ``gradient_blue_degenerate=True`` —— 蓝球被 plan 接管、目标里已无蓝球项；
      * ``var_ratio_before`` / ``var_ratio_after`` —— 方差代价（pmax_high 必须展示）；
      * ``p_at_least_r_before/after`` —— 「至少一注命中 ≥r 红」，pmax_high 时用精确枚举。
    ``objective`` 字段保持调用方传入的原值（V4 兼容），规范化后的值放
    ``objective_effective``。
    """
    rng = rng if rng is not None else random.Random()
    pool = [dict(t) for t in pool]
    obj = _normalize_coverage_objective(objective)
    before = sorted(pool, key=lambda x: -float(x.get("confidence", 0.0)))[:n_tickets]
    after = coverage_optimize(pool, n_tickets, r=r, n_blues=n_blues,
                              trials=trials, rng=rng, objective=obj,
                              blue_probs=blue_probs,
                              objective_weights=objective_weights)
    b = portfolio_expected_value(before)
    a = portfolio_expected_value(after)
    plan_used = blue_probs is not None
    exact = obj == "pmax_high"
    dist_b = ticket_distribution(before, with_distribution_exact=exact, r_values=(r,))
    dist_a = ticket_distribution(after, with_distribution_exact=exact, r_values=(r,))
    meta = _objective_meta(obj, r, blue_owned_by_plan=plan_used)
    meta.update({
        "var_ratio_before": float(dist_b["var_ratio"]),
        "var_ratio_after": float(dist_a["var_ratio"]),
        "redundancy_before": float(dist_b["redundancy"]),
        "redundancy_after": float(dist_a["redundancy"]),
        "union_coverage_before": int(dist_b["union_coverage"]),
        "union_coverage_after": int(dist_a["union_coverage"]),
        f"p_at_least_{int(r)}_before": float(dist_b["p_at_least_r"][str(int(r))]),
        f"p_at_least_{int(r)}_after": float(dist_a["p_at_least_r"][str(int(r))]),
        "p_at_least_r_method": dist_a["p_at_least_r_method"],
    })
    return {
        "before": b,
        "after": a,
        "improvement": {
            "blue_coverage_count": a["blue_coverage_count"] - b["blue_coverage_count"],
            "blue_hit_prob": a["blue_hit_prob"] - b["blue_hit_prob"],
            "blue_hit_prob_relative": (
                (a["blue_hit_prob"] - b["blue_hit_prob"]) / b["blue_hit_prob"]
                if b["blue_hit_prob"] > 0 else 0.0),
            "expected_reward": a["expected_reward"] - b["expected_reward"],
            "ev_ratio": a["ev_ratio"] - b["ev_ratio"],
        },
        "objective": objective,
        "objective_effective": obj,
        "objective_meta": meta,
        "jackpot_assumption": a["jackpot_assumption"],
    }
