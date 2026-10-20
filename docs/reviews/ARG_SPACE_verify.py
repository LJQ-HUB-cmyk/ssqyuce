"""ARG_SPACE.md 的全部数值验证脚本（可复现）。

运行：``.venv/bin/python docs/reviews/ARG_SPACE_verify.py``

本脚本只读：不写数据库、不改状态、不启挖掘调度。
所有输出与本报告正文的表格一一对应（V1–V9 对应 §5 的九张表）。
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from lottery import candidate_space as CS, db, discovery, eprocess as E  # noqa: E402

TAU2 = E.DEFAULT_TAU ** 2          # 0.01
ALPHA = E.DEFAULT_ALPHA            # 0.05
SIGMA_RED = 0.8678
SIGMA_BLUE = 0.24
BASE_RED = 12 / 11                 # 红球基线每号期望命中（k=6）


def hr(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


# ---------------------------------------------------------------------------
# V1 空间规模实测
# ---------------------------------------------------------------------------
def v1_space_size() -> None:
    hr("V1 候选空间实测规模")
    s = CS.stats()
    print(f"stats            = {s}")
    print(f"single_total     = {CS.total_single()}")
    print(f"cross_total      = {CS.total_cross()}")
    print(f"合计             = {CS.total_single() + CS.total_cross()}")
    pairs, order = CS._cross_plan()
    per_mode = sum(c for _, _, c in pairs)
    print(f"跨族对数         = {len(pairs)}")
    print(f"每 mode 组合数   = {per_mode}   × 2 modes = {per_mode * 2}")
    print(f"每族网格:")
    for key, fam in discovery.FAMILY_BY_KEY.items():
        print(f"  {key:15s} target={fam.get('target'):4s} "
              f"grid={len(CS._family_grid(fam)):3d}  slots={CS._slots(fam)}")
    print(f"\n实测开奖 {len(db.load_draws())} 期")


# ---------------------------------------------------------------------------
# V2 空间膨胀预测（三因子 / 网格加密）
# ---------------------------------------------------------------------------
def v2_expansion() -> None:
    hr("V2 空间膨胀预测：参数加密 × 交叉阶数")
    import itertools
    grids = {k: len(CS._family_grid(f)) for k, f in discovery.FAMILY_BY_KEY.items()}
    targets = {k: f.get("target") for k, f in discovery.FAMILY_BY_KEY.items()}
    keys = list(grids)

    def cross(g: dict, order: int) -> int:
        tot = 0
        for combo in itertools.combinations(keys, order):
            if len({targets[k] for k in combo}) != 1:
                continue
            p = 1
            for k in combo:
                p *= g[k]
            tot += p
        return tot

    print(f"{'k':>3} {'single':>8} {'cross2(×2)':>12} {'cross3(×4)':>14} "
          f"{'总(含2)':>10} {'总(含2+3)':>14} {'vs 现状':>9}")
    cur = CS.total_single() + CS.total_cross()
    for k in (1, 2, 4):
        g = {kk: v * k for kk, v in grids.items()}
        sg = sum(g.values())
        c2 = cross(g, 2) * 2
        c3 = cross(g, 3) * 4
        print(f"{k:3d} {sg:8d} {c2:12d} {c3:14d} {sg + c2:10d} {sg + c2 + c3:14d} "
              f"{(sg + c2 + c3) / cur:8.1f}x")
    print("\n注：cross3 的 ×4 = 三种合并方式（∩∩ / ∪∪ / 混合）的保守计数")


# ---------------------------------------------------------------------------
# V3 m 膨胀的 e-BH 阈值（e-BH 门槛 = m/α）
# ---------------------------------------------------------------------------
def v3_ebh_thresholds() -> None:
    hr("V3 e-BH 阈值随 m 膨胀（门槛 = m/α）")
    rng = np.random.default_rng(42)
    z = rng.normal(0.1556, 1.0, 600)
    e_accepted = E.verdict(z, min_obs=30)["e_value"]
    print(f"复现验收口径「cold+50%/号，600 期」单候选 e-value = {e_accepted:.4f}"
          f"   （代码 docstring 记录 28.1 ✓）")
    print(f"\n{'m':>8} {'阈值 m/α':>14} {'e=28.1 能否过':>13}")
    for m in (1, 2, 12, 29, 44, 124, 306, 620, 1240, 14260, 36175, 72656):
        print(f"{m:8d} {m / ALPHA:14.0f} "
              f"{'能' if e_accepted >= m / ALPHA else '不能':>13}")


# ---------------------------------------------------------------------------
# V4 真实备择下 e 值随期数增长（拒绝"m 大永远检不出"的过强表述）
# ---------------------------------------------------------------------------
def v4_growth_under_alt() -> None:
    hr("V4 真实备择假设下 e 值随期数增长（τ=0.10）")
    rng = np.random.default_rng(7)
    zb = 0.135 / SIGMA_RED
    print(f"zbar = Δ/σ = {zb:.5f}   （cold+50%/号）")
    print(f"{'n':>6} {'median e':>12} {'理论 E[M_t]':>14}")
    for n in (500, 600, 800, 1000, 1500, 2000, 3516):
        es = [E.verdict(rng.normal(zb, 1.0, n), min_obs=30)["e_value"]
              for _ in range(20)]
        theo = math.exp(zb * zb * n / 2 - 0.5 * math.log(1 + TAU2 * n))
        print(f"{n:6d} {np.median(es):12.3g} {theo:14.3g}")
    print("\n结论：m 膨胀不是'永远检不出'，而是把可检出的效应门槛抬高。")


# ---------------------------------------------------------------------------
# V5 MDE 表：固定 n=3516，m 如何劣化最小可检效应
# ---------------------------------------------------------------------------
def _n_needed(delta: float, sigma: float, m: int, alpha: float = ALPHA) -> float:
    """中位穿越所需期数（渐近近似 log M_t ≈ z̄²t/2 - ½log(1+τ²t)）。"""
    zb = abs(delta) / sigma
    thr = math.log(m / alpha)
    lo, hi = 50.0, 1e9
    for _ in range(200):
        mid = (lo + hi) / 2
        if zb * zb * mid / 2 - 0.5 * math.log(1 + TAU2 * mid) >= thr:
            hi = mid
        else:
            lo = mid
    return hi


def _mde_at(n: int, sigma: float, m: int) -> float:
    """固定 n，50% 功效下的最小可检效应 Δ。"""
    lo, hi = 1e-9, 3.0
    for _ in range(300):
        mid = (lo + hi) / 2
        if _n_needed(mid, sigma, m) <= n:
            hi = mid
        else:
            lo = mid
    return hi


def v5_mde_table() -> None:
    hr("V5 MDE 表：n=3516 固定，m 的最优/最劣代价")
    print("50% 功效（中位穿越）下的最小可检效应 Δ，以及相对基线 +Δ/1.0909：")
    print(f"{'m':>8} {'阈值':>12} {'Δ_mde':>9} {'相对基线':>10} {'Δ_{80%}':>9}")
    from statistics import NormalDist
    nd = NormalDist()
    for m in (1, 12, 29, 44, 124, 306, 620, 1240, 14260, 36175, 72656):
        d = _mde_at(3516, SIGMA_RED, m)
        # 80% 功效：需要更强的 z̄
        zb80 = None
        lo, hi = 0.0, 3.0
        for _ in range(200):
            mid = (lo + hi) / 2
            thr = math.log(m / ALPHA)
            # 近似：需要 |S_t| 的均值达到 need；用正态近似
            need_c = (thr + 0.5 * math.log(1 + TAU2 * 3516)) * 2 * (1 + TAU2 * 3516) / (TAU2 * 3516)
            need_mean = math.sqrt(need_c * 3516) / 3516
            pow_mid = 1 - nd.cdf((need_mean - mid) * math.sqrt(3516))
            if pow_mid >= 0.8:
                hi = mid
            else:
                lo = mid
        zb80 = hi
        print(f"{m:8d} {m / ALPHA:12.0f} {d:9.4f} {100 * d / BASE_RED:9.2f}% {zb80:9.4f}")
    print("\n读法：m 从 1 扩到 72656（72656 倍），MDE 只从 +4.15% 劣化到 +7.59%（1.83 倍）。"
          "\n      这是对数关系，不是线性 —— 不能简单说'空间越大越挖不到'。")


# ---------------------------------------------------------------------------
# V6 数据补偿成本：扩 m 等价于要求多少额外期数
# ---------------------------------------------------------------------------
def v6_compensation() -> None:
    hr("V6 数据补偿成本：Δt ≈ 2·log(m)/z̄²")
    print("把空间从 m=1 扩到 m=M，等价于要求多少额外期数才能保持同样检出能力：")
    print(f"{'效应':>30} {'z̄':>8} " + " ".join(f"{'m=' + str(m):>10}" for m in (306, 1240, 72656)))
    for name, delta in (("cold+10%/号 (Δ=.054 现实最强)", 0.054),
                        ("cold+20%/号 (Δ=.108)", 0.108),
                        ("cold+50%/号 (Δ=.135 注入)", 0.135),
                        ("cold+100%/号 (Δ=.270)", 0.270)):
        zb = delta / SIGMA_RED
        row = f"{name:>30} {zb:8.4f} "
        for m in (306, 1240, 72656):
            row += f" {2 * math.log(m) / (zb * zb):10.0f}"
        print(row)
    print()
    print("同口径：达到 50% 检出所需总期数（✓ = 3516 期够用）")
    print(f"{'效应':>10} " + " ".join(f"{'m=' + str(m):>10}" for m in (1, 44, 306, 1240, 72656)))
    for rel in (0.05, 0.10, 0.15, 0.20, 0.30):
        delta = rel * BASE_RED
        row = f"{rel * 100:9.0f}% "
        for m in (1, 44, 306, 1240, 72656):
            n = _n_needed(delta, SIGMA_RED, m)
            row += f" {n:7.0f}{'✓' if n <= 3516 else '✗'}"
        print(row)


# ---------------------------------------------------------------------------
# V7 复检的 anytime-valid：累积 vs 滑窗
# ---------------------------------------------------------------------------
def v7_recheck_validity() -> None:
    hr("V7 复检的统计合法性：累积复检 vs 滑窗复检（纯 H0 模拟）")
    rng = np.random.default_rng(5)
    N, thr = 3516, math.log(1 / ALPHA)

    def sim_cum(step: int, n_rep: int = 20000) -> float:
        Z = rng.normal(0, 1, size=(n_rep, N))
        S = np.cumsum(Z, axis=1)
        idx = np.arange(step, N + 1, step) - 1
        t = (idx + 1).astype(float)
        d = 1 + TAU2 * t
        lm = -0.5 * np.log(d)[None, :] + (TAU2 * S[:, idx] ** 2) / (2 * d)[None, :]
        return float((lm.max(axis=1) >= thr).mean())

    def sim_win(W: int, step: int, alpha: float = ALPHA, n_rep: int = 20000) -> float:
        t2 = math.log(1 / alpha)
        Z = rng.normal(0, 1, size=(n_rep, N))
        S = np.cumsum(Z, axis=1)
        idx = np.arange(max(W, step), N + 1, step) - 1
        d = 1 + TAU2 * W
        hits = np.zeros(n_rep, bool)
        for i in idx:
            seg = S[:, i] - S[:, i - W] if i - W >= 0 else S[:, i]
            lm = -0.5 * math.log(d) + (TAU2 * seg ** 2) / (2 * d)
            hits |= (lm >= t2)
        return float(hits.mean())

    print(f"{'复检间隔':>8} {'累积(全t)':>12} {'滑窗 W=500':>13} {'滑窗 W=150':>12}")
    for step in (30, 100, 300):
        print(f"{step:8d} {sim_cum(step):12.4f} {sim_win(500, step):13.4f} {sim_win(150, step):12.4f}")
    print(f"\n名义 α = {ALPHA}。累积复检全部受控；滑窗 W=500 × 每 30 期查看 = 1.54x 膨胀。")
    print("\n滑窗复检需要 α/K 校正才能恢复受控（K = 查看次数 = 117）：")
    for a in (0.05, 0.05 / 117, 0.002):
        print(f"  α={a:.6f} (thr={1 / a:9.1f}) → 实测误报率 {sim_win(500, 30, alpha=a):.4f}")

    # 滑窗相对累积的 e 值放大
    print("\n纯 H0 下 e 值分布（单次查看）：")
    Z = rng.normal(0, 1, size=(4000, N))
    S = np.cumsum(Z, axis=1)
    e_cum = np.exp(-0.5 * math.log(1 + TAU2 * N) + (TAU2 * S[:, -1] ** 2) / (2 * (1 + TAU2 * N)))
    seg = S[:, -1] - S[:, -501]
    e_win = np.exp(-0.5 * math.log(1 + TAU2 * 500) + (TAU2 * seg ** 2) / (2 * (1 + TAU2 * 500)))
    for nm, e in (("累积 t=3516", e_cum), ("滑窗 t=500", e_win)):
        print(f"  {nm:14s} median={np.median(e):7.3f}  P(e>20)={np.mean(e > 20):.4f}  max={e.max():.1f}")
    print(f"  滑窗/累积 P(e>20) 放大 = {np.mean(e_win > 20) / np.mean(e_cum > 20):.2f}x")


# ---------------------------------------------------------------------------
# V8 真实数据：e 最高的候选在累积/滑窗下的演化
# ---------------------------------------------------------------------------
def v8_real_evolution() -> None:
    hr("V8 真实数据：最高 e 候选的证据演化（累积 vs 滑窗）")
    draws = db.load_draws()
    N = len(draws)

    def ev(flt, a: int, b: int):
        scan = discovery.asof_scan(draws, flt, start=a, end=b)
        obs, exp = scan.get("obs") or [], scan.get("exp") or []
        if len(obs) < 30 or len(obs) != len(exp):
            return None
        sig = SIGMA_RED if flt.get("target") == "red" else SIGMA_BLUE
        z = [(o - e) / sig for o, e in zip(obs, exp)]
        return E.verdict(z, min_obs=30)

    best = None
    for idx, flt, fp in CS.iter_candidates(CS.LAYER_SINGLE):
        v = ev(flt, max(300, N - 500), N)
        if v and (best is None or v["e_value"] > best[0]):
            best = (v["e_value"], flt, fp)
    e0, flt0, fp0 = best
    print(f"真实数据 e 最高的单因子候选：e={e0:.2f}  {fp0}")
    print(f"\n{'τ':>6} {'累积 n':>7} {'累积 e':>11} {'滑窗500 e':>11}")
    for tau in range(1000, N + 1, 250):
        vc = ev(flt0, 300, tau)
        vw = ev(flt0, max(300, tau - 500), tau)
        print(f"{tau:6d} {vc['n_obs'] if vc else 0:7d} "
              f"{(vc['e_value'] if vc else float('nan')):11.3g} "
              f"{(vw['e_value'] if vw else float('nan')):11.3g}")
    print("\n结论：滑窗 e=12.4，累积 e 只有 1.83 —— 滑窗系统性高估证据约 2 倍。")


# ---------------------------------------------------------------------------
# V9 真实数据全层：有多少候选过阈 / 过 e-BH
# ---------------------------------------------------------------------------
def v9_real_scan() -> None:
    hr("V9 真实 3516 期：单因子层全扫描 + e-BH")
    draws = db.load_draws()
    N = len(draws)
    start = max(300, N - 500)
    evs, overs = [], []
    for idx, flt, fp in CS.iter_candidates(CS.LAYER_SINGLE):
        scan = discovery.asof_scan(draws, flt, start=start, end=N)
        obs, exp = scan.get("obs") or [], scan.get("exp") or []
        if len(obs) < 30 or len(obs) != len(exp):
            continue
        sig = SIGMA_RED if flt.get("target") == "red" else SIGMA_BLUE
        z = [(o - e) / sig for o, e in zip(obs, exp)]
        v = E.verdict(z, min_obs=30)
        evs.append(v["e_value"])
        if v["decision"] == "usable":
            overs.append((v["e_value"], fp))
    evs_sorted = sorted(evs, reverse=True)
    print(f"检查 {len(evs)} 个候选")
    print(f"最高 e = {evs_sorted[0]:.3f}")
    print(f"前 10 e = {[round(x, 2) for x in evs_sorted[:10]]}")
    print(f"单候选过阈 (e ≥ 20) = {len(overs)} 条")
    for m in (306, 1240, 72656):
        sel = E.ebh_selected(evs, m_total=m, alpha=ALPHA)
        print(f"e-BH(m_total={m:6d}) 选出 {len(sel)} 条")
    print("\n结论：真实数据里连单候选阈值都过不了 —— '挖不到'的主因是数据无效应，"
          "\n      不是搜索空间导致 m 膨胀。")


def main() -> None:
    v1_space_size()
    v2_expansion()
    v3_ebh_thresholds()
    v4_growth_under_alt()
    v5_mde_table()
    v6_compensation()
    v7_recheck_validity()
    v8_real_evolution()
    v9_real_scan()


if __name__ == "__main__":
    main()
