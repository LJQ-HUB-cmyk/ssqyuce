"""E5（决定性）：注入真实效应，比较各窗口口径的检出率。

方法：在真实 3516 期开奖序列上，对某一候选的「命中序列」注入偏置，
使该候选的真实效应 Δ（红球/期）从 0 变为 Δ，然后看各窗口口径能否检出。

注入方式（保结构）：不改开奖，而是给 z 序列加一个常数偏移 δ = Δ/σ_t。
这是**理想化**的（等价于该效应真实存在且稳定），用于比较口径灵敏度，
不代表任何具体家族。

口径：fix150 / fix300 / fix500 / fix1000 / grow(全史 3216)
判据：log e >= log 20（单候选 α=0.05，anytime-valid）
"""
import math
import numpy as np

import w_common as C
from lottery import candidate_space as CS, eprocess

THR = math.log(20)
SD = 0.8678   # 红球每期命中数标准差（决策用；与 production 一致）


def inject(zs, delta):
    return [z + delta / SD for z in zs]


def main():
    d = C.draws()
    n = len(d)
    # 用一个真实候选拿真实 z 序列（不影响结论：只是取其噪声实现）
    flt = CS.candidate_at(CS.LAYER_SINGLE, 0)[0]
    key, params = flt["key"], dict(flt.get("params") or {})
    if key != "hot":
        for i in range(CS.layer_size(CS.LAYER_SINGLE)):
            f = CS.candidate_at(CS.LAYER_SINGLE, i)[0]
            if f["key"] == "hot":
                key, params = "hot", dict(f.get("params") or {})
                break

    print(f"注入载体候选：{key} {params}；Δ 单位=红球命中/期；σ={SD}")
    print(f"\n{'Δ':>6} {'z/期':>7}" + "".join(f"{'fix'+str(W):>9}" for W in (150, 300, 500, 1000))
          + f"{'grow3216':>10}")
    for delta in (0.0, 0.02, 0.03, 0.05, 0.08, 0.10, 0.15):
        row = f"{delta:>6.2f} {delta/SD:>7.3f}"
        for W in (150, 300, 500, 1000):
            start = max(300, n - W)
            zs, _, _ = C.z_series(d, key, params, start=start, end=n)
            le = C.e_log(inject(zs, delta))
            row += f"{le:>9.2f}"
        zs, _, _ = C.z_series(d, key, params, start=300, end=n)
        row += f"{C.e_log(inject(zs, delta)):>10.2f}"
        print(row)
    print(f"\n判据：log e ≥ {THR:.2f}（e ≥ 20）")
    print("\n→ 真实效应固定时，增长窗口的 log e 增长最快（证据随 T 线性累积），")
    print("  但它同时带着 -0.5·log(1+τ²t) 的惩罚项；短窗口在无信号时更接近 0。")


def theory_first_crossing():
    print("\n【解析】给定每期效应 δ_z，增长窗口首次过阈所需期数（τ=0.10, α=0.05）：")
    print(f"{'Δ(红球/期)':>12} {'δ_z':>7} {'首次过阈期数':>12} {'≈年':>6}")
    for delta in (0.02, 0.03, 0.05, 0.08, 0.10, 0.15, 0.20):
        t = eprocess.expected_crossing_issues(delta, sigma=SD)
        print(f"{delta:>12.2f} {delta/SD:>7.3f} {str(t):>12} {(t/150.4 if t else 0):>6.1f}")


if __name__ == "__main__":
    main()
    theory_first_crossing()
