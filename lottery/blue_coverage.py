"""蓝球覆盖优化（B-3）：把 `engine.blue_coverage_plan` 的闭式最优接出来，
并**强制**携带诚实披露。

## 这个模块存在的唯一理由

`engine.py` 里已经有一个数学上正确的 `blue_coverage_plan()`：
n 注的蓝球取互不相同时，``P(≥1 注中蓝) = min(n,16)/16``，
而随机重复只有 ``1-(15/16)^n``。这个提升是**组合恒等式**，全枚举可证，零造假风险。

但产品审查员实测发现：这个收益**只在前端角落以小字显示**，
最诚实的端点 `/api/portfolio/expected-value` 前端从未调用。
于是用户看到的仍是"智能预测"，「唯一真实的数学收益」被藏在没有人读的地方。

本模块因此做两件事：
1. **把计划与披露绑成一个对象** —— 调用方拿到 `plan` 就必然拿到 `disclosure`，
   没有"只取数字不看边界"的路径；
2. **把三个数字并排给出**：概率、成本、期望回收。
   单看"中奖概率从 47.6% 提到 62.5%"是激励性的，但配上
   "成本 20 元、期望回收 3.13 元"才是完整的真相。

## 三个必须同屏的数字（任何一次调用都必须全部返回）

以 10 注为例（实测口径）：

| 量 | 值 |
|---|---|
| P(≥1 注中蓝) 随机重复 | 47.55% |
| P(≥1 注中蓝) 去重覆盖 | **62.50%** |
| 成本 | 20 元 |
| 六等奖期望回收 | 3.125 元 |
| **六等奖返奖比** | **15.625%（与 n 无关）** |
| 整注（一~六等全计）返奖比 | ≈ 52.3%（含浮动奖假设） |

**返奖比恒为 15.6%（六等奖口径）这件事，是覆盖优化不改变期望的直接证据。**
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

#: 单注价格（元）
TICKET_PRICE = 2.0
#: 六等奖固定奖金（元）
SIXTH_PRIZE = 5.0
#: 蓝球池大小
B_MAX = 16


def p_any_blue_random_repeat(n: int) -> float:
    """随机重复抽样下「至少中 1 个蓝球」的概率 ``1-(15/16)^n``。

    这是**基线**：不知道去重、每注独立随机选蓝球时的概率。
    """
    if n <= 0:
        return 0.0
    return 1.0 - ((B_MAX - 1) / B_MAX) ** n


def p_any_blue_covered(n: int) -> float:
    """去重覆盖下「至少中 1 个蓝球」的概率 ``min(n,16)/16``。

    这是本模块交付的**上界**，且是可达的（n≤16 时取互不相同的蓝球即可）。
    """
    if n <= 0:
        return 0.0
    return min(int(n), B_MAX) / float(B_MAX)


def coverage_plan(n_tickets: int,
                  blue_probs: Optional[Sequence[float]] = None) -> List[int]:
    """蓝球分配计划（委托 `engine.blue_coverage_plan`，保持单一实现）。

    这里不重新实现数学 —— 只在 engine 不可用时给出均匀回退，
    避免"两套蓝球逻辑各说各话"（这正是本项目反复出问题的地方）。
    """
    if n_tickets <= 0:
        return []
    try:
        from . import engine
        return list(engine.blue_coverage_plan(int(n_tickets), blue_probs))
    except Exception:            # noqa: BLE001 —— 引擎异常不该让接口 500
        k = int(n_tickets)
        base = list(range(1, B_MAX + 1))
        return (base * ((k // B_MAX) + 1))[:k]


def optimize(n_tickets: int,
             blue_probs: Optional[Sequence[float]] = None,
             *, full_ev_ratio: Optional[float] = None) -> Dict[str, Any]:
    """返回「蓝球覆盖计划 + 三个必须同屏的数字 + 披露文本」。

    `full_ev_ratio` 由调用方从 `engine.prize_expectation(...)["ev_ratio_full"]`
    传入（含浮动奖假设）；不传时只给六等奖口径，并在披露里说明"未含浮动奖"。
    """
    n = max(0, int(n_tickets))
    plan = coverage_plan(n, blue_probs)
    distinct = len(set(plan))

    p_cover = p_any_blue_covered(n)
    p_repeat = p_any_blue_random_repeat(n)
    cost = n * TICKET_PRICE
    ev_sixth = p_cover * SIXTH_PRIZE
    ev_ratio_sixth = (ev_sixth / cost) if cost > 0 else 0.0

    out: Dict[str, Any] = {
        "n_tickets": n,
        "plan": plan,
        "blue_coverage_count": distinct,
        "p_any_blue_covered": round(p_cover, 6),
        "p_any_blue_random_repeat": round(p_repeat, 6),
        "p_gain_ratio": round((p_cover / p_repeat) if p_repeat > 0 else 1.0, 4),
        "cost_yuan": round(cost, 2),
        "ev_sixth_yuan": round(ev_sixth, 4),
        "ev_ratio_sixth": round(ev_ratio_sixth, 6),
        "net_sixth_yuan": round(ev_sixth - cost, 4),
    }
    if full_ev_ratio is not None:
        out["ev_ratio_full"] = round(float(full_ev_ratio), 6)
        out["ev_full_yuan"] = round(float(full_ev_ratio) * cost, 4)

    # ---- 强制披露（不可省略、不可拆开单独展示）----
    lines = [
        f"同样买 {n} 注：「至少中一次蓝球（¥5）」的概率从 "
        f"{p_repeat * 100:.2f}% 提升到 {p_cover * 100:.2f}%。",
        f"但成本是 {cost:.0f} 元，六等奖期望回收 {ev_sixth:.2f} 元，"
        f"净亏 {cost - ev_sixth:.2f} 元。",
        f"六等奖返奖比恒为 {ev_ratio_sixth * 100:.2f}%，**与注数无关** —— "
        f"覆盖优化改变的是中奖分布，不是期望收益。",
    ]
    if full_ev_ratio is None:
        lines.append("（未含一/二等奖浮动奖；整注口径需配合奖金假设表查看。）")
    else:
        lines.append(
            f"整注（一~六等全计）返奖比约 {float(full_ev_ratio) * 100:.1f}%，"
            f"**长期仍为负期望**。")
    lines.append(
        "中头奖概率恒为 1/17,721,088，**不受选号或覆盖策略影响**。")
    out["disclosure"] = "\n".join(lines)
    out["disclosure_lines"] = lines
    out["honesty_flags"] = {
        "increases_hit_probability": False,
        "increases_expected_return": False,
        "increases_prize_frequency": True,
    }
    return out
