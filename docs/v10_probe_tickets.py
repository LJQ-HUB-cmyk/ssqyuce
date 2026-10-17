"""V10 方案探针（可复现，v2 已修正口径）：比较「随机 / 覆盖式 / 贪心非冗余」三种票单策略。

用法：
    cd /root/DSH/应用/ssqyuce && PYTHONPATH=. .venv/bin/python docs/v10_probe_tickets.py

================================ 重要更正（v2） =================================
本脚本 v1 版存在一处**口径缺陷**（由评审 R2 发现，Lead 已复现并修正）：

  v1 的 ``cov_batch`` 函数**完全没有使用 rng**，因此 300 期跑的是**同一组固定票**，
  而不是"每期重新铺号"。这导致：
    - 「覆盖式」一臂实际上是**一个固定的确定性票组**，与「随机」一臂（每期重抽）不可比；
    - 由此得到的「覆盖式 ≥4 红略降」**方向性结论不成立**。

  v2 修正：所有策略都在**每期独立使用 rng**，并显式给出
    (a) 该对比的**最小可检出效应（MDE）**，以判定它是不是"零功效"；
    (b) 95% CI 与配对 t 检验，而不是只看点估计。

================================ 结论口径（诚实版） =================================
本对比在 300 期 × 10 注口径下的 MDE ≈ **+40%（相对）**，远超任何合理效应量，
因此正确的结论是：

    「在可获得的样本量下**未检出**红球维度差异」，
    而**不是**「已经证明覆盖优化没有收益」。

覆盖优化唯一能在数学上确定的事情（与命中无关）：
  - 蓝球去重覆盖把「至少中 1 蓝」从 1-(15/16)^n 提到 min(n,16)/16（n=10 时 47.55%→62.50%）；
  - **但六等奖 EV 比恒为 0.15625，与 n 无关** —— 改变分布，不改变期望。
"""
import argparse
from math import comb, sqrt

import numpy as np

from lottery import db

R_MAX = 33


def hypergeom_pmf():
    """单注红球命中 K 的精确分布（超几何 N=33, K=6, n=6）。"""
    return np.array([comb(6, k) * comb(27, 6 - k) / comb(33, 6) for k in range(7)])


def mde_relative(n_periods, n_tickets, p4):
    """该口径下能检出的「≥4红期数占比」相对提升（两比例检验功效反推）。"""
    z_a, z_b = 1.959963985, 0.8416212336
    p_any = 1.0 - (1.0 - p4) ** n_tickets
    abs_delta = (z_a + z_b) * sqrt(2.0 * p_any * (1.0 - p_any) / n_periods)
    return p_any, abs_delta, abs_delta / p_any


def gen_random(rng, n):
    return [frozenset((rng.choice(R_MAX, 6, replace=False) + 1).tolist()) for _ in range(n)]


def gen_coverage(rng, n):
    """覆盖式：**每期重新**按号池铺号（v2 修正：真正使用 rng）。"""
    pool = list(range(1, R_MAX + 1))
    rng.shuffle(pool)
    return [frozenset(pool[(j * 6 + x) % R_MAX] for x in range(6)) for j in range(n)]


def gen_greedy(rng, n, pool_size=40):
    """贪心非冗余：从随机池里挑「边际新号最多」的 n 注。"""
    rest = gen_random(rng, pool_size)
    sel = []
    while len(sel) < n and rest:
        def gain(t):
            used = set().union(*sel) if sel else set()
            return len(t - used)
        best = max(rest, key=gain)
        rest.remove(best)
        sel.append(best)
    return sel


def metrics(batch, truth):
    ks = [len(t & truth) for t in batch]
    return (sum(ks), len(set().union(*batch)), sum(1 for k in ks if k >= 4))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--periods", type=int, default=300)
    ap.add_argument("--tickets", type=int, default=10)
    ap.add_argument("--seed", type=int, default=20261017)
    a = ap.parse_args()

    p = hypergeom_pmf()
    E = float(p @ np.arange(7))
    var = float((np.arange(7) ** 2) @ p - E ** 2)
    p4 = float(p[4:].sum())
    print(f"单注超几何基线: E[K]={E:.4f}  Var={var:.6f}  sd={sqrt(var):.6f}  "
          f"P(K>=4)={p4:.4%}  P(K>=5)={float(p[5:].sum()):.4%}")
    p_any, abs_d, rel_d = mde_relative(a.periods, a.tickets, p4)
    flag = "⚠ 零功效：任何合理效应都无法检出" if rel_d > 0.15 else "可检出"
    print(f"本口径功效: 单期「≥1注命中≥4红」≈{p_any:.3%}；n={a.periods} 期时"
          f"可检出绝对差 {abs_d:.3%}（相对 +{rel_d:.1%}）→ {flag}")

    draws = db.load_draws()
    rows = {"random": [], "coverage": [], "greedy": []}
    for i in range(len(draws) - a.periods, len(draws)):
        truth = frozenset(draws[i]["reds"])
        rng = np.random.default_rng(a.seed + i)      # 每期独立种子
        rows["random"].append(metrics(gen_random(rng, a.tickets), truth))
        rows["coverage"].append(metrics(gen_coverage(rng, a.tickets), truth))
        rows["greedy"].append(metrics(gen_greedy(rng, a.tickets), truth))

    print(f"\n{'策略':<10}{'累计红命中':>20}{'并集覆盖':>10}{'≥4红期数':>10}")
    for k, v in rows.items():
        arr = np.array(v, float)
        se = arr[:, 0].std(ddof=1) / np.sqrt(len(arr))
        print(f"{k:<10}{arr[:,0].mean():>10.3f} ±{1.96*se:<8.3f}{arr[:,1].mean():>10.2f}{int(arr[:,2].sum()):>10d}")

    print("\n配对差（相对 random）：")
    base = np.array(rows["random"])[:, 0]
    for k in ("coverage", "greedy"):
        d = np.array(rows[k])[:, 0] - base
        se = d.std(ddof=1) / np.sqrt(len(d))
        t = d.mean() / se if se > 0 else 0.0
        print(f"  {k:<9} Δ={d.mean():+.3f} ±{1.96*se:.3f}  t={t:+.2f}  → "
              f"{'显著' if abs(t) > 1.96 else '不显著'}")

    print("\n诚实结论：")
    print(f"  · 本对比 MDE ≈ +{rel_d:.1%}（相对），远超合理效应量 → 属**零功效**对比。")
    print("  · 因此只能表述为「未检出差异」，不能表述为「已证明无收益」。")
    print("  · 与命中无关但可确定：蓝球去重覆盖仅改变「中奖频率」，六等奖 EV 比恒为 0.15625。")


if __name__ == "__main__":
    main()
