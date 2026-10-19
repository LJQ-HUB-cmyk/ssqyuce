"""V12-10 L2 精确值服务 + V12-15 边际读数：可复现造票（纯函数）与分布查询封装。

本模块**不含任何统计逻辑**：所有数值都来自 ``engine.ticket_distribution()``
（V12 底座，组合数学恒等式，不改其逻辑）。这里只做两件事：

1. 可复现造票（``build_spread_tickets`` / ``build_overlap_tickets``）：
   同一 ``seed`` 永远产出同一注单（``random.Random(seed)``，无全局状态）。
2. 把 ``ticket_distribution()`` 的输出包成 API 可直接返回的载荷，并做
   「``method == exact_enum`` / ``prize_assumption`` 必填」两道断言。

造票口径（中性票组，不代表任何"选号优势"）：
  * ``spread``：把 6n 个号位尽量平均摊到 33 个红号上（每个号出现
    ``floor(6n/33)`` 或 ``ceil(6n/33)`` 次），再分到 n 注、保证注内不重号。
    n≤5 时自然得到 n 注两两互不相交（Ā=0，``var_ratio=1/9`` 当 n=5）。
    该构造同时是 ``optimal_portfolio(min_var)`` 的闭式最优（见 engine）。
  * ``overlap``：4 个共有红号 + 每注 2 个随机号（高重叠，Ā≈4）。
  * 蓝球：两 preset 均为 1..n 互不相同（``n≤16``），使红球尾部概率的对比不受
    蓝球干扰；``p_any_blue = min(n,16)/16`` 为闭式。
"""
from __future__ import annotations

import json
import random
from typing import Dict, List, Optional

R_MAX = 33
B_MAX = 16

PRESETS = ("spread", "overlap")


def build_spread_tickets(n: int, seed: int = 0) -> List[Dict]:
    """铺开票组：6n 个号位尽量平均摊到 33 个红号上（可复现）。

    n≤5 时 n 注两两互不相交（Ā=0）；n>5 时每个红号出现次数差至多 1
    （ΣA 最小，见 ``engine.optimal_portfolio`` 的凸性论证）。
    """
    n = int(n)
    if not 1 <= n <= B_MAX:
        raise ValueError(f"n 必须在 1..{B_MAX}，实际 {n}")
    rng = random.Random(int(seed))
    order = list(range(1, R_MAX + 1))
    rng.shuffle(order)
    slots_total = 6 * n
    base, extra = divmod(slots_total, R_MAX)
    # 号位多集：前 extra 个号（洗牌后顺序）多占一次 → 出现次数差至多 1
    multiset: List[int] = []
    for i, c in enumerate(order):
        multiset.extend([c] * (base + (1 if i < extra else 0)))
    assert len(multiset) == slots_total
    # 贪心分注：按多集顺序依次放入"当前不含该号"的下一注；
    # cnt_c ≤ ceil(6n/33) ≤ n 恒成立，故永不失败（确定性）。
    tickets: List[set] = [set() for _ in range(n)]
    cursor = 0
    for c in multiset:
        for j in range(n):
            idx = (cursor + j) % n
            if c not in tickets[idx]:
                tickets[idx].add(c)
                cursor = (idx + 1) % n
                break
        else:  # pragma: no cover - 纯防御（cnt_c ≤ n 时不可达）
            raise AssertionError(f"spread 分注失败：红号 {c} 无处可放")
    out = [{"reds": sorted(t), "blue": i + 1} for i, t in enumerate(tickets)]
    assert all(len(t["reds"]) == 6 for t in out)
    return out


def build_overlap_tickets(n: int, seed: int = 0) -> List[Dict]:
    """高重叠票组：4 个共有红号 + 每注 2 个随机号（可复现，Ā≈4）。"""
    n = int(n)
    if not 1 <= n <= B_MAX:
        raise ValueError(f"n 必须在 1..{B_MAX}，实际 {n}")
    rng = random.Random(int(seed))
    order = list(range(1, R_MAX + 1))
    rng.shuffle(order)
    shared = sorted(order[:4])
    rest = order[4:]
    out = []
    for i in range(n):
        extra = sorted(rng.sample(rest, 2))
        out.append({"reds": sorted(shared + extra), "blue": i + 1})
    return out


def build_tickets(n: int, preset: str = "spread", seed: int = 0) -> List[Dict]:
    """按 preset 造票（纯函数，同 seed 同结果）。"""
    p = str(preset or "spread").strip().lower()
    if p == "spread":
        return build_spread_tickets(n, seed)
    if p == "overlap":
        return build_overlap_tickets(n, seed)
    raise ValueError(f"未知 preset：{preset!r}（必须是 {PRESETS} 之一）")


# distribution 每个字段的方法标签（诚实口径：精确值 / 解析恒等式 / 假设常量）。
_FIELD_METHODS = {
    # C(33,6) 全枚举精确值
    "p_at_least_r": "exact_enum",
    "p_at_least_r_method": "label",
    "var_T_exact_enum": "exact_enum",
    # 组合数学解析恒等式（与样本无关，可全枚举验证）
    "union_coverage": "analytic_identity",
    "redundancy": "analytic_identity",
    "redundancy_neutral": "constant",
    "redundancy_sum": "analytic_identity",
    "redundancy_max": "constant",
    "expected_hits": "identity",
    "sigma1_sq": "constant",
    "n_sigma1_sq": "analytic_identity",
    "var_T": "analytic_identity",
    "var_ratio": "analytic_identity",
    "var_ratio_note": "note",
    # 独立假设近似（并排展示，不得当结论）
    "p_at_least_r_iid": "iid_approx",
    # 蓝球闭式
    "p_any_blue": "closed_form",
    "blue_coverage_count": "closed_form",
    # 单注期望 × n：与注单结构无关的常量；一/二等奖依赖 prize_assumption
    "ev_ratio_sixth": "single_ticket_ev",
    "ev_ratio_full": "single_ticket_ev",
    "ev_reward_yuan": "single_ticket_ev",
    "ev_ratio_full_strict": "single_ticket_ev",
    "ev_ratio_full_table_b": "single_ticket_ev",
    "ev_ratio_full_note": "note",
    # 奖金假设（浮动奖只能给假设常量，必须随结果展示）
    "prize_assumption": "published_assumption",
    "prize_assumption_detail": "published_assumption",
    "prize_cash": "published_assumption",
    "p_at_least_r_mc": "monte_carlo",
    "p_at_least_r_mc_detail": "monte_carlo",
    "n": "label",
}


def field_methods(distribution: Dict) -> Dict[str, str]:
    """distribution 里每个键的方法标签（未知键标 derived，不断言失败）。"""
    return {k: _FIELD_METHODS.get(k, "derived") for k in distribution}


def l2_distribution_payload(n: int = 5, preset: str = "spread", seed: int = 0,
                            tickets_json: Optional[str] = None) -> Dict:
    """V12-10 载荷：``ticket_distribution()`` 原样字段 + 方法断言 + 方法标签。

    ``tickets_json`` 给了就以它为准（``[{"reds":[...6...],"blue":1},...]``），
    否则按 ``preset``/``seed`` 造票。``method`` 非 ``exact_enum`` 或缺
    ``prize_assumption`` 即抛 ``AssertionError``（调用方转 500）。
    """
    from . import engine as E

    n = int(n)
    if not 1 <= n <= B_MAX:
        raise ValueError(f"n 必须在 1..{B_MAX}，实际 {n}")
    source = str(preset or "spread").strip().lower() or "spread"
    if tickets_json:
        try:
            raw = json.loads(tickets_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"tickets 不是合法 JSON：{exc}") from exc
        if not isinstance(raw, list) or not raw:
            raise ValueError("tickets 必须是非空数组")
        tickets = [{"reds": list(t["reds"]), "blue": t.get("blue", 0)} for t in raw]
        source = "custom"
    else:
        tickets = build_tickets(n, source, seed)
    dist = E.ticket_distribution(tickets, with_distribution_exact=True)
    if dist.get("p_at_least_r_method") != "exact_enum":  # pragma: no cover - 纯防御
        raise AssertionError(
            f"L2 分布必须走 exact_enum，实际 {dist.get('p_at_least_r_method')!r}")
    if not dist.get("prize_assumption"):
        raise AssertionError("prize_assumption 缺失：EV 口径不得无假设展示")
    if int(dist.get("n", -1)) != len(tickets):
        raise AssertionError("n 与注单数不一致")
    return {
        "ok": True,
        "n": int(dist["n"]),
        "preset": source,
        "seed": int(seed),
        "source": source,
        "tickets": [{"reds": [int(x) for x in t["reds"]],
                     "blue": int(t["blue"])} for t in tickets],
        "method": "exact_enum",
        "distribution": dist,
        "methods": field_methods(dist),
        "note_zh": ("中性读数：并集覆盖/冗余度/方差比/尾部概率均为组合结构事实 "
                    "（全枚举精确值，可复现）；返奖比与注单无关，期望回报为负是常态。"),
    }


def _prize_prob(distribution: Dict) -> float:
    """P(≥1注中任意奖) = P_red4 + (1−P_red4)·k/16（红蓝开奖独立的精确合成）。

    单注中奖 ⟺ 蓝中（六等奖 0+1 即中）或红≥4（4+0=五等奖）；
    给定红球开奖，蓝球部分概率为 k/16（k=不同蓝球数），与红球开奖独立。
    P_red4 取全枚举精确值，故合成值也是精确值。
    """
    p4 = float(distribution["p_at_least_r"]["4"])
    k = int(distribution.get("blue_coverage_count", 0))
    return p4 + (1.0 - p4) * min(k, B_MAX) / float(B_MAX)


def marginal_reading(n: int = 10, preset: str = "spread") -> Dict:
    """V12-15 边际读数：由 n 注加到 n+1 注的中性读数（只给数字，不给购买建议）。

    两档均用同一 ``preset`` 的中性票组 + 全枚举精确值差分。返回体**不得**出现
    「划算」「推荐」等购买建议文案（测试用 grep 计数锁死为 0）。
    """
    from . import engine as E

    n = int(n)
    if not 1 <= n < B_MAX:
        raise ValueError(f"n 必须在 1..{B_MAX - 1}（需能加到 n+1 注），实际 {n}")
    p = str(preset or "spread").strip().lower() or "spread"
    if p not in PRESETS:
        raise ValueError(f"未知 preset：{preset!r}（必须是 {PRESETS} 之一）")
    d_n = E.ticket_distribution(build_tickets(n, p), with_distribution_exact=True)
    d_m = E.ticket_distribution(build_tickets(n + 1, p), with_distribution_exact=True)
    for d in (d_n, d_m):
        if d.get("p_at_least_r_method") != "exact_enum":  # pragma: no cover - 纯防御
            raise AssertionError("边际读数必须走 exact_enum")
        if not d.get("prize_assumption"):
            raise AssertionError("prize_assumption 缺失")
    p_n = _prize_prob(d_n)
    p_m = _prize_prob(d_m)
    delta_pp = (p_m - p_n) * 100.0
    ev = float(d_n["ev_ratio_full"])
    exp_ret = -2.0 * (1.0 - ev)
    return {
        "ok": True,
        "n": n,
        "n_next": n + 1,
        "method": "exact_enum",
        "tickets_note": "中性票组（preset=spread 时 n≤5 互不相交，n>5 号位尽量摊平）",
        "preset": p,
        "p_prize_n": float(p_n),
        "p_prize_next": float(p_m),
        "delta_p_at_least_one_prize_pp": float(delta_pp),
        "expected_return_yuan": float(exp_ret),
        "ev_ratio_full": float(ev),
        "prize_assumption": d_n["prize_assumption"],
        "reading_zh": (
            f"由 {n} 注加到 {n + 1} 注（每注 ¥2）：P(≥1注中奖)由 {p_n:.4%} 变为 "
            f"{p_m:.4%}（{delta_pp:+.3f} 个百分点，全枚举精确值差分）；"
            f"新增 1 注的期望回报为 ¥{exp_ret:.4f}。"
            "以上为中性数值呈现，不评价是否值得，也不改变任何期望。"
        ),
    }
