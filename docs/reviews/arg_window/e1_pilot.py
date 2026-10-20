"""E1：真实数据上的 e-process 零分布标定 —— 单候选 / 306 候选 / 窗口长度。

回答：「一条规律都挖不到」是判据太严，还是数据本来就没有？（含重叠校正前后对比）
"""
import numpy as np

import w_common as C
from lottery import candidate_space as CS, eprocess

THR = np.log(1 / eprocess.DEFAULT_ALPHA)


def run(W, n_single=None, correct=True):
    d = C.draws()
    n = len(d)
    start = max(300, n - W)
    logms, fs = [], []
    for idx, flt, fp in CS.iter_candidates(CS.LAYER_SINGLE, 0, n_single or CS.layer_size(CS.LAYER_SINGLE)):
        params = dict(flt.get("params") or {})
        zs, _, r = C.z_series(d, flt["key"], params, start=start, end=n, correct=correct)
        if len(zs) < 30:
            continue
        logms.append(C.e_log(zs))
        fs.append((fp, flt["key"], params, len(zs), np.mean(zs) if zs else 0))
    logms = np.array(logms)
    e = np.exp(np.clip(logms, -700, 700))
    return {
        "W": W, "m": len(logms),
        "log_e_median": float(np.median(logms)),
        "e_median": float(np.exp(np.median(logms))),
        "e_q90": float(np.quantile(e, .90)),
        "e_q99": float(np.quantile(e, .99)),
        "e_max": float(e.max()) if len(e) else 0,
        "cross_count": int((logms >= THR).sum()),
        "top": sorted(zip(e.tolist(), [f[1:] for f in fs]), key=lambda x: -x[0])[:3],
    }


def exact_theory(m):
    """单候选纯噪声下 P(e>=20) ~ 1/20=5%；m 个候选未校正的期望过阈数 = m*α。"""
    return m * eprocess.DEFAULT_ALPHA


if __name__ == "__main__":
    print(f"{'W':>6} {'m':>5} {'median e':>9} {'q90':>8} {'q99':>9} {'max e':>10} "
          f"{'过阈':>4} {'理论 m*0.05':>10}")
    for W in (150, 300, 500, 1000, 2000, 3216):
        r = run(W)
        print(f"{W:>6} {r['m']:>5} {r['e_median']:>9.3f} {r['e_q90']:>8.2f} "
              f"{r['e_q99']:>9.2f} {r['e_max']:>10.2f} {r['cross_count']:>4} "
              f"{exact_theory(r['m']):>10.1f}")
    print("\n--- top3 (W=500, 重叠校正) ---")
    for e, meta in run(500)["top"]:
        print(f"  e={e:8.2f}  {meta[0]:18s} {meta[1]}")
    print("\n--- 不校正方差（现有 production 口径 σ=0.8678）vs 校正 ---")
    for W in (150, 500, 1000):
        a, b = run(W, correct=False), run(W, correct=True)
        print(f"  W={W:5d}: 未校正 median e={a['e_median']:7.2f} max e={a['e_max']:9.2f} 过阈 {a['cross_count']:2d}"
              f" | 校正后 median e={b['e_median']:7.2f} max e={b['e_max']:9.2f} 过阈 {b['cross_count']:2d}")
