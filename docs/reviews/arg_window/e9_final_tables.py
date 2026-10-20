"""E9：报告用的最终数值表（全部可在真数据上复现）。"""
import math
import numpy as np

import w_common as C
from lottery import candidate_space as CS, eprocess

TAU = eprocess.DEFAULT_TAU
THR = math.log(20)
ALPHA = 0.05
SD = 0.8678


def mde_table():
    """各窗口下的最小可检效应（MDE）：固定样本 vs 增长窗口。

    用 e-process 解析首达时间反推：在窗口 T 内刚好能过阈的 δ_z。
    """
    print("【表A】各窗口「恰好在 T 期内首次过阈」所需的每期效应 δ_z 与对应 Δ（红球/期）")
    print(f"{'T':>6} {'δ_z 临界':>10} {'Δ=δ_z·σ':>10} {'≈% 命中率相对提升':>16}")
    for T in (150, 300, 500, 1000, 2000, 3216):
        # 二分求 δ_z
        lo, hi = 0.0, 2.0
        for _ in range(60):
            mid = (lo + hi) / 2
            s = mid * T
            denom = 1 + TAU * TAU * T
            logm = -0.5 * math.log(denom) + (TAU * TAU * s * s) / (2 * denom)
            if logm >= THR:
                hi = mid
            else:
                lo = mid
        dz = (lo + hi) / 2
        d_abs = dz * SD
        print(f"{T:>6} {dz:>10.4f} {d_abs:>10.4f} {d_abs/1.0909*100:>15.1f}%")


def ebh_threshold_table():
    """e-BH 阈值随 m（窗口数）膨胀。"""
    print("\n【表B】e-BH：选中第 k 名所需的 e 值（α=0.05，单因子 m_base=306）")
    print(f"{'窗口数':>6} {'m':>7} " + "".join(f"{'k='+str(k):>12}" for k in (1, 2, 3, 5, 10)))
    for nw in (1, 2, 3, 4):
        m = 306 * nw
        row = f"{nw:>6} {m:>7} "
        for k in (1, 2, 3, 5, 10):
            row += f"{m/(k*ALPHA):>12.0f}"
        print(row)
    print("\n【表B2】加入跨族交叉（72350）后的 m：")
    for label, base in (("仅单因子", 306), ("单因子+交叉", 306 + 72350)):
        for nw in (1, 3):
            m = base * nw
            print(f"  {label:12s} × {nw} 窗口 → m={m:>7}, k=1 需 e≥{m/ALPHA:>10.0f}")


def window_vs_power():
    """固定窗口 vs 增长窗口：在不同真实效应下的 log e（真数据载体，E5 的浓缩版）。"""
    print("\n【表C】真实数据载体上注入效应后的 log e（判据 ≥3.00）")
    d = C.draws(); n = len(d)
    key, params = "hot", {"window": 10, "k": 5}
    print(f"{'Δ':>6}" + "".join(f"{'fix'+str(W):>9}" for W in (150, 300, 500, 1000)) + f"{'grow':>9}")
    for delta in (0.0, 0.03, 0.05, 0.08, 0.10):
        row = f"{delta:>6.2f}"
        for W in (150, 300, 500, 1000):
            zs, _, _ = C.z_series(d, key, params, start=max(300, n - W), end=n)
            row += f"{C.e_log([z + delta/SD for z in zs]):>9.2f}"
        zs, _, _ = C.z_series(d, key, params, start=300, end=n)
        row += f"{C.e_log([z + delta/SD for z in zs]):>9.2f}"
        print(row)


if __name__ == "__main__":
    mde_table()
    ebh_threshold_table()
    window_vs_power()
