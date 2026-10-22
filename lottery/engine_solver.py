"""S10 组合求解器（`optimal_portfolio` 及其搜索/平衡辅助）。

## 迁移说明（D-02 拆分 S10）

内容从 `lottery/engine.py` 原 1297 行至文件末尾**逐字迁移**，逻辑未改一行。
迁出：`PORTFOLIO_OBJECTIVES` / `PORTFOLIO_P_MAX_AT_LEAST_R` /
`_balanced_min_sum_a` / `_balanced_counts` / `_balanced_portfolio` /
`_portfolio_plan` / `_candidate_pool` / `_exact_p_at_least_r` /
`optimal_portfolio` / `_search_max_p_at_least_r`。

## 诚实边界（原文保留）

`optimal_portfolio` 优化的是**给定目标函数**（最小方差 / 最大 P(≥1中蓝) 等），
**不是中奖概率**。所有目标都由组合数学决定 —— 优化只能在「已定的概率空间」里挑组合，
**无法改变任何一注的中奖概率**。

## 为什么本文件不 import engine

`from . import engine as E` 会在 engine 尚未初始化完时形成环（第 7 批首版因此
全部 26 个组合测试 ImportError）。基础常量从 `engine_base` 直取，
被复用的函数从各子模块真源直取 —— **不经过薄壳**。
"""
from __future__ import annotations

import itertools
import random
from math import comb
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from . import config      # noqa: F401
from .engine_base import R_MAX, B_MAX, TICKET_COST  # noqa: F401  纯常量
from .engine_distribution import (  # noqa: F401
    RED_PER_TICKET,
    _exact_red_tail,
    _red_mask,
    _validate_tickets,
    _var_ratio,
    ticket_distribution,
)
from .engine_constraints import pass_constraints  # noqa: F401

PORTFOLIO_OBJECTIVES = ("min_var", "max_p_any_blue", "max_p_at_least_r")
PORTFOLIO_P_MAX_AT_LEAST_R = 4          # L2-e 的「≥r 红」取 r=4（与方案 §1.2 L2-b 同口径）


def _balanced_min_sum_a(n: int) -> int:
    """ΣA 的全局最优值：6n 个号位均摊到 33 个红号 ⇒ Σ_x C(cnt_x,2)。

    ``ΣA = Σ_x C(cnt_x,2)``（对每个红号数同时出现在几注里）。
    交换论证：若 cnt_a ≥ cnt_b+2，把 a 的一次出现换成 b 使 Σ C(cnt,2) 下降
    cnt_a−1−(cnt_b) > 0，故最优必为均摊；凸性 ⇒ 均摊值即全局最优。
    """
    n = int(n)
    s = 0
    for c in _balanced_counts(n):
        s += c * (c - 1) // 2
    return int(s)


def _balanced_counts(n: int) -> List[int]:
    """6n 个号位在 33 个红号上的均摊出现次数（floor/ceil(6n/33)，差 ≤1）。"""
    base, extra = divmod(6 * int(n), R_MAX)
    return [base + (1 if i < extra else 0) for i in range(R_MAX)]


def _balanced_portfolio(n: int, seed: int = 0, max_rounds: int = 5000) -> List[List[int]]:
    """号位均摊的最小冗余构造（确定性；n≤5 时即两两互不相交）。

    每个红号按 ``_balanced_counts`` 分配到**不同注**上（注内不重号），随机化
    分配；若某注长度 ≠ 6，用**修复**（从超长注搬号到欠长注，保持不重号）降低
    ``Σ(len−6)²``，必然终止。n≤5 时每注恰 6 个号，一次即成功。
    """
    n = int(n)
    counts = _balanced_counts(n)
    for round_no in range(int(max_rounds)):
        rng = random.Random(int(seed) * 1_000_003 + round_no)
        tickets: List[set] = [set() for _ in range(n)]
        for num in range(1, R_MAX + 1):
            c = counts[num - 1]
            if c == 0:
                continue
            for s in rng.sample(range(n), c):      # 同一号落在不同注 ⇒ 注内不重号
                tickets[s].add(num)
        if all(len(t) == RED_PER_TICKET for t in tickets):
            return [sorted(t) for t in tickets]
        # 修复：从超长注搬号到欠长注（号→注保持一一对应，不破坏 ΣA 最优）
        fixable = True
        for _ in range(4000):
            over = max(range(n), key=lambda i: len(tickets[i]))
            under = min(range(n), key=lambda i: len(tickets[i]))
            if len(tickets[over]) == RED_PER_TICKET:
                break
            movable = [x for x in sorted(tickets[over]) if x not in tickets[under]]
            if not movable:
                fixable = False
                break
            tickets[over].discard(movable[0])
            tickets[under].add(movable[0])
        if fixable and all(len(t) == RED_PER_TICKET for t in tickets):
            return [sorted(t) for t in tickets]
    raise AssertionError(f"号位均摊构造失败（n={n}）：修复轮次耗尽")  # pragma: no cover


def _portfolio_plan(n: int, notes: str = "spread", seed: int = 0) -> List[Dict]:
    """L3-4 / L2-e 的基准票组（与 ``l2_service.build_tickets`` 同口径的本地实现）。

    ``spread``：号位均摊（n≤5 互不相交）；``overlap``：4 个共有号 + 每注 2 个随机号。
    蓝球 1..n 互不相同（n≤16）⇒ P(≥1蓝) 取闭式上限。
    """
    n = int(n)
    if not 1 <= n <= B_MAX:
        raise ValueError(f"n 必须在 1..{B_MAX}，实际 {n}")
    key = str(notes or "spread").strip().lower()
    if key == "spread":
        reds_list = _balanced_portfolio(n, seed)
    elif key == "overlap":
        rng = random.Random(int(seed))
        order = list(range(1, R_MAX + 1))
        rng.shuffle(order)
        shared = list(order[:4])
        rest = list(order[4:])
        reds_list = [sorted(shared + rng.sample(rest, 2)) for _ in range(n)]
    else:
        raise ValueError(f"未知 preset：{notes!r}（spread | overlap）")
    return [{"reds": r, "blue": i + 1} for i, r in enumerate(reds_list)]


def _candidate_pool(r: int = PORTFOLIO_P_MAX_AT_LEAST_R,
                    pool_size: int = 200, seed: int = 0,
                    constraints: Optional[Dict] = None) -> List[Dict]:
    """硬约束候选池：均匀随机抽 ``pool_size`` 个形态合规的 6 红组合（确定性）。

    ``constraints`` 给了就走 ``pass_constraints``；否则**不过滤**（穷举锚点必须用
    未过滤的全空间，否则「与穷举差 ≤1e-9」比较的两边口径不同）。
    兜底（真实数据出现过）：约束过严导致池 < n+1 时放宽到不过滤，并在
    ``search.constraint_fallback`` 如实回报 —— 不静默改变口径。
    """
    import itertools
    rng = random.Random(int(seed))
    all_reds = list(range(1, R_MAX + 1))
    cands: List[List[int]] = []
    seen = set()
    tries = 0
    need = max(int(pool_size), 1)
    while len(cands) < need and tries < need * 50:
        tries += 1
        c = tuple(sorted(rng.sample(all_reds, RED_PER_TICKET)))
        if c in seen:
            continue
        seen.add(c)
        if constraints is not None and not pass_constraints(list(c), 1, constraints):
            continue
        cands.append(list(c))
    return [{"reds": c, "blue": 0} for c in cands]


def _exact_p_at_least_r(tickets: List[Dict], r: int) -> float:
    """全枚举精确 P(≥1 注命中 ≥r 红)。"""
    return float(_exact_red_tail(_validate_tickets(tickets))["p_ge"].get(int(r), 0.0))


def optimal_portfolio(n: int, objective: str = "min_var",
                      constraints: Optional[Dict] = None, seed: int = 0,
                      *, r: int = PORTFOLIO_P_MAX_AT_LEAST_R,
                      pool_size: int = 200,
                      candidates: Optional[List[Dict]] = None) -> Dict:
    """V12-11 预算内最优组合求解器（L2-e）：n 注 + 目标 + 可选硬约束 → 解。

    objective
    ---------
    ``min_var``
        最小化 ``var_ratio``（⇔ 最小化冗余度 Ā）。**精确最优**：均摊构造达到
        ``ΣA* = Σ_x C(cnt_x,2)``（n≤5 时 Ā=0、var_ratio=1/9；n=11 时 var_ratio=0）。
        ``constraints`` 会筛掉均摊解里的违规注，再用候选池里的合规注替换
        （局部替换，``search.exhaustive=False``）。
    ``max_p_any_blue``
        仅优化蓝球覆盖：``P(≥1蓝) = min(n,16)/16``（闭式最优，与红球/约束无关）。
    ``max_p_at_least_r``
        在候选池（``candidates`` 或随机合规池，硬约束走 ``pass_constraints``）里
        用贪心初始化 + 1-swap 局部搜索最大化**全枚举精确** ``p_at_least_r[r]``；
        候选池 ≤ ``2**12`` 时先随机抽 4096 个子集做初筛，再对前若干子集做精确枚举。

    返回值（必须齐全）
    ------------------
    ``tickets`` / ``objective`` / ``var_ratio`` / ``redundancy`` /
    ``ev_ratio_full`` / ``ev_ratio_note`` / ``search``（策略 / 评估次数 / 是否穷举）/
    ``p_at_least_r`` / ``p_any_blue`` / ``prize_assumption`` / ``n``。

    **诚实边界**：``ev_ratio_full`` 恒等于单注返奖比 × 0.5，与注单结构**逐位无关**
    —— 本求解器不改变、也不宣称改变期望收益（见 ``ev_ratio_note``）。
    """
    obj = str(objective or "min_var").strip().lower()
    if obj not in PORTFOLIO_OBJECTIVES:
        raise ValueError(f"未知 objective：{objective!r}（必须是 {PORTFOLIO_OBJECTIVES}）")
    n = int(n)
    if not 1 <= n <= B_MAX:
        raise ValueError(f"n 必须在 1..{B_MAX}，实际 {n}")
    r = int(r)
    seed = int(seed)
    evals = 0
    exhaustive = False
    constraint_fallback = False

    if obj == "max_p_any_blue":
        reds_list = _balanced_portfolio(n, seed)
        tickets = [{"reds": rd, "blue": i + 1} for i, rd in enumerate(reds_list)]
        strategy = "closed_form_blue_coverage"
        exhaustive = True
        evals = 1            # 闭式解构造一次即可（没有搜索）
    elif obj == "min_var":
        reds_list = _balanced_portfolio(n, seed)
        tickets = [{"reds": rd, "blue": i + 1} for i, rd in enumerate(reds_list)]
        evals = 1
        strategy = "balanced_redistribution(exact_optimal)"
        exhaustive = True
        if constraints is not None:
            bad = [i for i, rd in enumerate(reds_list)
                   if not pass_constraints(rd, 1, constraints)]
            if bad:
                pool = _candidate_pool(r, pool_size=max(pool_size, n * 4), seed=seed,
                                       constraints=constraints)
                accepted = [t for t in pool
                            if pass_constraints(t["reds"], 1, constraints)]
                if len(accepted) < len(bad) + 1 and candidates is None:
                    constraint_fallback = True
                    accepted = _candidate_pool(r, pool_size=max(pool_size, n * 4), seed=seed,
                                               constraints=None)
                for idx in bad:
                    if accepted:
                        tickets[idx] = {"reds": list(accepted.pop(0)["reds"]),
                                        "blue": tickets[idx]["blue"]}
                        evals += 1
                exhaustive = False       # 无约束时是精确最优；有约束时是替换解
                strategy = "balanced_redistribution+constraint_repair"
    else:  # max_p_at_least_r
        if candidates is not None:
            pool = _validate_tickets(candidates)
            if constraints is not None:
                pool = [t for t in pool if pass_constraints(t["reds"], 1, constraints)]
        else:
            pool = _candidate_pool(r, pool_size=pool_size, seed=seed,
                                   constraints=constraints)
        if len(pool) < n:
            raise ValueError(f"候选池只有 {len(pool)} 注，不足 n={n}；请放宽 constraints")
        tickets, evals, exhaustive = _search_max_p_at_least_r(pool, n, r, seed=seed)
        for i, t in enumerate(tickets):
            t["blue"] = i + 1
        strategy = ("greedy_then_1swap_local_search(exact_enum"
                    + (",prefilter_4096" if len(pool) > 24 else "") + ")")

    dist = ticket_distribution(tickets, with_distribution_exact=True)
    return {
        "n": int(n),
        "objective": obj,
        "r": (r if obj == "max_p_at_least_r" else None),
        "tickets": [{"reds": [int(x) for x in t["reds"]], "blue": int(t["blue"])}
                    for t in tickets],
        "union_coverage": dist["union_coverage"],
        "redundancy": dist["redundancy"],
        "redundancy_neutral": dist["redundancy_neutral"],
        "var_ratio": dist["var_ratio"],
        "var_ratio_theoretical_min": float(_var_ratio(
            n, _balanced_min_sum_a(n) / max(n * (n - 1) // 2, 1))),
        "p_at_least_r": dist["p_at_least_r"],
        "p_at_least_r_method": dist["p_at_least_r_method"],
        "p_any_blue": dist["p_any_blue"],
        "ev_ratio_full": dist["ev_ratio_full"],
        "prize_assumption": dist["prize_assumption"],
        "ev_ratio_note": ("EV 与注单结构**无关**：同注数下期望回报 = n × 单注 EV，"
                          "返奖比恒为单注口径（ev_ratio_full）。本求解器只改变"
                          "P(≥1注≥r红) / 方差 / 覆盖等**分布形状**，不改变、也不宣称"
                          "提高期望收益。"),
        "search": {
            "strategy": strategy,
            "evals": int(evals),
            "objective_evaluations": int(evals),
            "exhaustive": bool(exhaustive),
            "n_candidates": (len(candidates) if candidates is not None
                             else None if obj != "max_p_at_least_r" else None),
            "seed": seed,
            "constraint_fallback": bool(constraint_fallback),
            "constraints_applied": bool(constraints is not None),
        },
        "note_zh": ("组合结构最优解：它是**组合数学**结论（对固定红号空间成立），"
                    "不依赖历史数据，因此不需要样本量/ p 值。"),
    }


def _search_max_p_at_least_r(pool: List[Dict], n: int, r: int,
                             seed: int = 0) -> tuple:
    """贪心 + 1-swap 局部搜索最大化全枚举精确 P(≥1注≥r红)。

    返回 ``(tickets, evals, exhaustive)``。候选池 ≤ 4096 时会对**全部** C(m,n)
    子集做精确复算（``exhaustive=True``，m≤12、n≤5 时 m 取 12 → 792 次枚举 ≈ 30s
    太慢，故仅当 C(m,n) ≤ 32 时做全查；其余情况对初筛前 24 名做精确复算）。
    """
    import itertools
    from math import comb

    rng = random.Random(int(seed))
    masks = [_red_mask(t["reds"]) for t in pool]
    m = len(pool)
    evals = 0

    # 初筛：m 大时随机抽 4096 个 n-子集（只用 O(n) 的并集覆盖 / 冗余做代理排序，
    # 不做 110 万注全枚举）；m 小（≤10）时直接全组合。
    n_sub = comb(m, n)
    if n_sub <= 4096:
        subsets = list(itertools.islice(itertools.combinations(range(m), n), n_sub))
    else:
        seen = set()
        subsets = []
        while len(subsets) < 4096:
            s = tuple(sorted(rng.sample(range(m), n)))
            if s in seen:
                continue
            seen.add(s)
            subsets.append(s)

    def _proxy(idx_tuple):
        # 代理：并集覆盖越大越好（低重叠），再按 ΣA 越小越好
        union = 0
        for i in idx_tuple:
            union |= masks[i]
        cnt = bin(union).count("1")
        sum_a = 0
        for a in range(len(idx_tuple)):
            for b in range(a + 1, len(idx_tuple)):
                sum_a += bin(masks[idx_tuple[a]] & masks[idx_tuple[b]]).count("1")
        return (-cnt, sum_a, idx_tuple)

    ranked = sorted(subsets, key=_proxy)
    # 阶段一：**贪心构造**——从空集开始，每步加入使精确 P 增益最大的一注。
    # 贪心路径上的每个前缀都是一个"已评估解"，最终解 ≥ 贪心解（局部搜索只接受改进）。
    greedy_idx: List[int] = []
    cur_p = 0.0
    remaining = set(range(m))
    for _ in range(n):
        order = sorted(remaining, key=lambda i: _proxy(tuple(sorted(greedy_idx + [i]))))
        head = order[: min(len(order), 24 if m > 24 else len(order))]
        scored_step = []
        for i in head:
            trial = greedy_idx + [i]
            p = _exact_p_at_least_r([pool[j] for j in trial], r)
            evals += 1
            scored_step.append((p, i))
        scored_step.sort(key=lambda x: (-x[0], x[1]))
        greedy_idx.append(scored_step[0][1])
        cur_p = scored_step[0][0]
        remaining.discard(greedy_idx[-1])
    # 精确复算代理排序的前 24 名（每个约 0.05~0.3s，取决于 n），与贪心解取最优
    scored = [(cur_p, tuple(sorted(greedy_idx)))]
    for idx_tuple in ranked[:24]:
        sel = [pool[i] for i in idx_tuple]
        p = _exact_p_at_least_r(sel, r)
        evals += 1
        scored.append((p, tuple(sorted(idx_tuple))))
    scored.sort(key=lambda x: (-x[0], x[1]))
    best_p, best_idx = scored[0]
    best_idx = tuple(best_idx)

    # 阶段二：1-swap 局部搜索——换掉一注（遍历池内所有替换），只接受严格改进。
    # 从 max(贪心解, 初筛最优) 出发，故终解 ≥ 两者（单调不降）。
    improved = True
    while improved and evals < 600:
        improved = False
        cur = set(best_idx)
        for out_pos in range(n):
            for cand in range(m):
                if cand in cur:
                    continue
                trial = list(best_idx)
                trial[out_pos] = cand
                p = _exact_p_at_least_r([pool[i] for i in trial], r)
                evals += 1
                if p > best_p + 1e-15:
                    best_p, best_idx, improved = p, tuple(sorted(trial)), True
                    break
            if improved:
                break
            if evals >= 600:
                break
        if evals >= 600:
            break

    # 小候选池时对**全部** C(m,n) ≤ 32 的子集做精确复算，确认局部搜索没漏掉最优
    exhaustive = False
    if n_sub <= 32:
        exhaustive = True
        for idx_tuple in subsets:
            if tuple(sorted(idx_tuple)) == best_idx:
                continue
            p = _exact_p_at_least_r([pool[i] for i in idx_tuple], r)
            evals += 1
            if p > best_p + 1e-15:
                best_p, best_idx = p, tuple(sorted(idx_tuple))

    tickets = [dict(pool[i]) for i in best_idx]
    return tickets, evals, exhaustive
