"""E2b：τ=0.10 序列 e-process 的**有限样本校准曲线**（纯噪声）。

e-process 的 Ville 界 P(sup_t M_t >= 1/α) <= α 是**上界**，在小 t 时极松。
要回答「窗口多大才够灵敏」，必须标定：在 H0 下，观测 t 期后被判显著的**实际**概率。
"""
import math
import numpy as np
from lottery import eprocess

TAU = eprocess.DEFAULT_TAU
THR = math.log(1 / eprocess.DEFAULT_ALPHA)


def h0_cross_rate(T, n_sims=4000, tau=TAU, seed=0):
    """纯 iid N(0,1) 下，在 t<=T 内首次越过 log(1/α) 的频率（含任意停止 → anytime 口径）。"""
    rng = np.random.default_rng(seed)
    z = rng.standard_normal((n_sims, T))
    crossed = np.zeros(n_sims, dtype=bool)
    # 逐 t 复算（向量化，避免 4000xT 次 Python 循环）
    s = np.zeros(n_sims)
    tau2 = tau * tau
    for i in range(T):
        s += z[:, i]
        t = i + 1
        logm = -0.5 * math.log(1 + tau2 * t) + (tau2 * s * s) / (2 * (1 + tau2 * t))
        crossed |= (logm >= THR)
    return float(crossed.mean())


def power_at(delta, T, n_sims=2000, tau=TAU, seed=0):
    """iid N(delta,1) 下 T 期内过阈的频率。"""
    rng = np.random.default_rng(seed)
    z = rng.standard_normal((n_sims, T)) + delta
    s = np.zeros(n_sims)
    tau2 = tau * tau
    crossed = np.zeros(n_sims, dtype=bool)
    for i in range(T):
        s += z[:, i]
        t = i + 1
        logm = -0.5 * math.log(1 + tau2 * t) + (tau2 * s * s) / (2 * (1 + tau2 * t))
        crossed |= (logm >= THR)
    return float(crossed.mean())


if __name__ == "__main__":
    print("【H0 校准】纯噪声下 t 期内任意时刻过阈的实际频率（名义上界 5%）：")
    print(f"{'T':>6} {'实际过阈率':>10} {'上界(1/α)':>10}")
    for T in (150, 300, 500, 1000, 2000, 3216):
        r = h0_cross_rate(T, n_sims=3000, seed=T)
        print(f"{T:>6} {r*100:>9.2f}% {'5.00%':>10}")

    print("\n【功效】iid N(δ,1) 下 T 期内过阈频率（δ=z 单位/期）：")
    hdr = "  δ\\T  " + "".join(f"{T:>9}" for T in (150, 300, 500, 1000, 2000, 3216))
    print(hdr)
    for d in (0.03, 0.05, 0.08, 0.10, 0.15, 0.25):
        row = f"{d:6.2f}  "
        for T in (150, 300, 500, 1000, 2000, 3216):
            row += f"{power_at(d, T, n_sims=800, seed=1)*100:8.1f}%"
        print(row)
    print("\n注：红球口径 δ_z = Δ/(σ/√?) —— 每期命中差的 z 单位。")
    print("     Δ=+0.05 红球/期 ≈ z=0.058；Δ=+0.10 ≈ z=0.115；Δ=+0.054（cold+10%）= z≈0.062")
