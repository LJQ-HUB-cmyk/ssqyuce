"""V4 硬约束、加权采样与混合分布质量（S1/S2/S4）。

## 迁移说明（D-02 拆分 S1/S2/S4）

内容从 `lottery/engine.py` 原 34–143 行**逐字迁移**，逻辑未改一行。
迁出：`_last_sums` / `constraint_ctx` / `pass_constraints` / `_weighted_sample` /
`sample_stat_ticket` / `ensemble_mass`。

**`ensemble_mass` 依赖 `_BlendArray`** —— 那已在 `engine_blend.py`（第 2 批迁出）。
本模块通过薄壳 `E` 取用，**不直接 import 子模块**，避免跨子模块硬依赖。

## 诚实边界（原文保留）

`pass_constraints` 做的是**硬过滤**（和值区间、奇偶、三区、连号等）。
硬过滤**改变不了任何一注的中奖概率** —— 它只在「已定分布」上做条件筛选。
若说它"提高命中率"是错的；它只是让选出的号更符合历史形态（可能是错觉）。
"""
from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np

from . import config      # noqa: F401
from .engine_base import R_MAX, B_MAX, TICKET_COST  # noqa: F401  纯常量，无循环
from . import features as F  # noqa: E741  与拆前 engine.py 相同的别名


class _InsufficientDataError(ValueError):
    """历史数据为空 → 无法估计硬约束上下文。

    本类是 `engine.InsufficientDataError` 的**真源**（engine 反向 import 它）。
    `constraint_ctx` 抛它，调用方 `except ENG.InsufficientDataError` 能接住 ——
    因为两边是同一个类对象。
    """




def _last_sums(draws: List[Dict], n: int = 500) -> np.ndarray:
    return F.sums(draws[-n:]) if len(draws) >= n else F.sums(draws)


def constraint_ctx(draws: List[Dict]) -> Dict:
    # W02 前置条件：**显式**拒绝空输入，不让 IndexError 从 numpy 内部抛穿。
    # 只拦"完全没有数据"这一种；只要有 ≥1 期就按既有口径估计
    # （walk-forward / 回放会传入很短的切片，那是合法用法）。
    if not draws:
        raise _InsufficientDataError(
            "constraint_ctx 需要至少 1 期开奖数据，当前传入为空列表"
            "（常见原因：数据库未初始化、或测试被隔离到空临时库）。"
        )
    s = _last_sums(draws)
    zc = F.zone_counts(draws[-500:] if len(draws) >= 500 else draws)
    zc_hist = {}
    for z in zc:
        zc_hist[z] = zc_hist.get(z, 0) + 1
    top_zones = [k for k, _ in sorted(zc_hist.items(), key=lambda x: -x[1])[:10]]
    oc = F.odd_counts(draws[-500:] if len(draws) >= 500 else draws)
    odd_hist = {}
    for o in oc:
        odd_hist[int(o)] = odd_hist.get(int(o), 0) + 1
    odd_range = sorted(
        (k for k, v in odd_hist.items() if v / max(1, len(oc)) >= 0.02)
    )
    from . import shape_rules as _SR
    _sc = _SR.shape_constraint_ctx(draws)
    return {
        "sum_min": float(np.percentile(s, 5)),
        "sum_max": float(np.percentile(s, 95)),
        "top_zones": top_zones,
        "odd_range": odd_range if odd_range else [2, 3, 4],
        # V12：跨度 / 连号 —— 组合恒等带（理论兜底，不随历史波动）
        "span_min": float(_sc.get("span_min", 17.0)),
        "span_max": float(_sc.get("span_max", 31.0)),
        "max_consecutive": int(_sc.get("max_consecutive", 2)),
    }


def pass_constraints(reds: List[int], blue: int, ctx: Dict) -> bool:
    """形态硬约束：和值 / 三区 / 奇偶（既有）+ 跨度 / 连号（V12 扩展）。

    ctx 来自 ``constraint_ctx``（三维）或 ``shape_rules.shape_constraint_ctx``
    （五维）。缺 span/max_consecutive 键时跳过对应维度（向后兼容）。
    """
    s = sum(reds)
    if not (ctx.get("sum_min", -1e9) <= s <= ctx.get("sum_max", 1e9)):
        return False
    z1 = sum(1 for r in reds if 1 <= r <= 11)
    z2 = sum(1 for r in reds if 12 <= r <= 22)
    z3 = 6 - z1 - z2
    if (z1, z2, z3) not in ctx.get("top_zones", [(1, 3, 2), (2, 2, 2)]):
        return False
    odd = sum(1 for r in reds if r % 2 == 1)
    if odd not in ctx.get("odd_range", [2, 3, 4]):
        return False
    # V12：跨度与连号（shape_rules 提供）
    sp = max(reds) - min(reds)
    if not (ctx.get("span_min", -1e9) <= sp <= ctx.get("span_max", 1e9)):
        return False
    rs = sorted(reds)
    ngroups = sum(1 for i in range(len(rs) - 1)
                  if rs[i + 1] == rs[i] + 1 and (i == 0 or rs[i] != rs[i - 1] + 1))
    return ngroups <= int(ctx.get("max_consecutive", 9))


# ---------- 统计采样 ----------

def _weighted_sample(weights: np.ndarray, k: int, rng: random.Random) -> List[int]:
    pool = list(range(1, len(weights) + 1))
    w = weights.astype(float).copy()
    chosen = []
    for _ in range(k):
        if w.sum() <= 0:
            w = np.ones_like(w)
        p = (w / w.sum()).tolist()
        pick = rng.choices(pool, weights=p, k=1)[0]
        chosen.append(pick)
        w[pick - 1] = 0.0
    return sorted(chosen)


def sample_stat_ticket(red_blend: np.ndarray, blue_blend: np.ndarray,
                       ctx: Dict, rng: random.Random, uniform: bool = False) -> Optional[Dict]:
    """按混合概率加权不放回抽样，硬约束不过则重试。"""
    for _ in range(60):
        if uniform:
            reds = sorted(rng.sample(range(1, R_MAX + 1), 6))
            blue = rng.randint(1, B_MAX)
        else:
            reds = _weighted_sample(red_blend, 6, rng)
            blue = _weighted_sample(blue_blend, 1, rng)[0]
        if pass_constraints(reds, blue, ctx):
            return {"reds": reds, "blue": blue}
    return None


def ensemble_mass(red_blend: np.ndarray, blue_blend: np.ndarray,
                  reds: List[int], blue: int) -> float:
    """候选号在混合分布上的概率质量（0-100 比例尺）。"""
    r_mass = float(np.mean(red_blend[np.array(reds) - 1]))
    b_mass = float(blue_blend[blue - 1])
    norm_r = float(np.max(red_blend))
    norm_b = float(np.max(blue_blend))
    if norm_r <= 0:
        norm_r = 1.0
    if norm_b <= 0:
        norm_b = 1.0
    return 100.0 * (0.75 * r_mass / norm_r + 0.25 * b_mass / norm_b)

