"""E3：固定窗口 vs 增长窗口 vs 滚动重启窗口 —— 在真实 3516 期上的 e-process 轨迹对比。

三种口径：
  FIX(W)   固定最近 W 期（现状 500）
  GROW     从 min_start 到当前期的全部数据（无截断，"持续累积"）
  ROLL(W)  滚动：只用最近 W 期，且 e-process 每期重启（等价于 FIX，但给出每期判定）
  PAGE     增长 + 重启（Page/CUSUM 式：证据掉回 1 以下就重启，抗 regime switch）

输出：每个候选在这四种口径下的 log e 终值，以及"首次过阈期号"。
真数据上若无信号，四者都不过阈 —— 关键是**相对灵敏度**（e 的排序是否稳定）与
**e 的绝对值随 T 的增长**（任何真实效应对 GROW 最有利）。
"""
import math
import numpy as np

import w_common as C
from lottery import candidate_space as CS, eprocess

TAU = eprocess.DEFAULT_TAU
THR = math.log(20)


def page_logm(zs, tau=TAU, reset_below=1.0):
    """Page 型重启：M_t 掉到 reset_below 之下则从 1 重启（丢弃历史证据）。"""
    tau2 = tau * tau
    s = 0.0
    m = 1.0
    ln_m = 0.0
    best = 0.0
    for i, z in enumerate(zs, start=1):
        s += z
        ln_m = -0.5 * math.log(1 + tau2 * i) + (tau2 * s * s) / (2 * (1 + tau2 * i))
        if math.exp(ln_m) < reset_below and i > 10:
            s = 0.0
            ln_m = 0.0
    return ln_m


def evaluate(W_list=(150, 300, 500, 1000)):
    d = C.draws()
    n = len(d)
    rows = []
    for idx, flt, fp in CS.iter_candidates(CS.LAYER_SINGLE, 0, CS.layer_size(CS.LAYER_SINGLE)):
        params = dict(flt.get("params") or {})
        if flt.get("target") != "red":
            continue
        rec = {"fp": fp, "key": flt["key"], "params": params}
        for W in W_list:
            start = max(300, n - W)
            zs, _, _ = C.z_series(d, flt["key"], params, start=start, end=n)
            rec[f"fix{W}"] = C.e_log(zs)
            rec[f"page{W}"] = page_logm(zs)
        zs_all, _, _ = C.z_series(d, flt["key"], params, start=300, end=n)
        rec["grow"] = C.e_log(zs_all)
        rec["grow_page"] = page_logm(zs_all)
        rec["n_grow"] = len(zs_all)
        rows.append(rec)
    return rows


if __name__ == "__main__":
    rows = evaluate()
    print(f"红球单因子候选数：{len(rows)}")
    cols = ["fix150", "fix300", "fix500", "fix1000", "grow", "grow_page"]
    print(f"\n{'口径':>12} {'median log e':>13} {'max log e':>10} {'过阈数':>7}")
    for c in cols:
        v = np.array([r[c] for r in rows])
        print(f"{c:>12} {np.median(v):>13.2f} {v.max():>10.2f} {(v>=THR).sum():>7}")

    # 排序稳定性：固定 500 vs 增长全史
    from scipy.stats import spearmanr
    a = [r["fix500"] for r in rows]
    b = [r["grow"] for r in rows]
    print(f"\n排序一致性 Spearman(fix500, grow) = {spearmanr(a, b).statistic:+.3f}")
    print(f"排序一致性 Spearman(fix150, fix500) = "
          f"{spearmanr([r['fix150'] for r in rows], a).statistic:+.3f}")

    print("\ntop8 by grow：")
    for r in sorted(rows, key=lambda x: -x["grow"])[:8]:
        print(f"  grow={r['grow']:+7.2f} fix500={r['fix500']:+7.2f} page500={r['page500']:+7.2f} "
              f"  {r['key']:16s} {r['params']}")
