"""E8：多窗口合并候选池时，e-BH 的「自补偿」到底能否抵消 m 膨胀？

真实计算（非推算）：
  1. 对每个候选 × 每个窗口，算 e 值；标记 e>=20 的为「过阈」。
  2. 方案 A（单窗口 500）：m=306
  3. 方案 B（三窗口 150/500/全史，合并池）：m=918，同一 FP 的每个窗口各占一个 pool 位置
  4. 计算每个方案下 e-BH 能选出几条。

同时给出「一个候选在 n 个窗口都过阈」时，它占的 rank 数 → 分母 k 的增长。
"""
import math
import numpy as np

import w_common as C
from lottery import candidate_space as CS, eprocess

ALPHA = 0.05
THR = math.log(20)


def ebh(e_vals, m, alpha=ALPHA):
    order = sorted(range(len(e_vals)), key=lambda i: -float(e_vals[i]))
    k = 0
    for rank, idx in enumerate(order, start=1):
        if e_vals[idx] >= m / (rank * alpha):
            k = rank
    return k


def main():
    d = C.draws(); n = len(d)
    cands = []
    for i in range(CS.layer_size(CS.LAYER_SINGLE)):
        got = CS.candidate_at(CS.LAYER_SINGLE, i)
        if not got:
            continue
        flt, fp = got
        if flt.get("target") != "red":
            continue
        cands.append((fp, flt["key"], dict(flt.get("params") or {})))
    print(f"红球单因子候选：{len(cands)}")

    windows = {"W150": 150, "W500": 500, "ALL": None}
    e_tab = {}
    for name, W in windows.items():
        start = 300 if W is None else max(300, n - W)
        col = []
        for fp, key, params in cands:
            zs, _, _ = C.z_series(d, key, params, start=start, end=n)
            col.append(math.exp(min(C.e_log(zs), 700)) if len(zs) >= 30 else 0.0)
        e_tab[name] = np.array(col)

    for name in windows:
        e = e_tab[name]
        print(f"  {name:5s}: max e={e.max():9.1f}  e>=20 的数={(e>=20).sum():2d}")

    # 方案 A：单窗口
    for name in windows:
        k = ebh(list(e_tab[name]), len(cands))
        print(f"方案A 单窗口 {name:5s} m={len(cands)} → 选中 {k}")

    # 方案 B：三窗口合并
    pool = np.concatenate([e_tab[m] for m in windows])
    k = ebh(list(pool), len(pool))
    print(f"方案B 三窗口合并 m={len(pool)} → 选中 {k}")

    # 跨窗口过阈的候选数（自补偿的物理基础）
    cross = {fp: 0 for fp, _, _ in cands}
    for name in windows:
        for i, e in enumerate(e_tab[name]):
            if e >= 20:
                cross[cands[i][0]] += 1
    n_multi = sum(1 for v in cross.values() if v >= 2)
    print(f"\n跨窗口同时过阈的候选数：{n_multi}（这决定自补偿规模）")
    for fp, c in sorted(cross.items(), key=lambda x: -x[1]):
        if c >= 2:
            print(f"  {c} 窗口：{fp[:70]}")

    # 自补偿理论上界：若一个信号在 n 个窗口都过阈且 e 相同，
    # 它占据 rank 1..n，此时需 e >= m/(k·α) 对 k=n 成立，即 e >= m/(n·α)
    print("\n自补偿上界分析（单信号、跨 n 窗口 e 相同、m 也随之乘 n）：")
    for nw in (1, 2, 3):
        m = len(cands) * nw
        # 若只有一个信号且它占满前 nw 个 rank（k=nw），所需 e
        need = m / (nw * ALPHA)
        print(f"  n_w={nw}: m={m:4d}, 占 k={nw} rank 时需 e≥{need:.0f}（= {len(cands)/ALPHA:.0f}，与 n_w 无关）")
    print("  → 结论：单信号自补偿**完全抵消** m 膨胀（阈值恒为 m_base/α）。")
    print("    但这要求「同一信号在所有窗口都过阈」。若只在个别窗口过阈，m 膨胀不被抵消。")


if __name__ == "__main__":
    main()
