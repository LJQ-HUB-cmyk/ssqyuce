"""E10：独立核查 data-empiricist 指出的唯一例外 sum_range(45-75, w30)。

三个独立检验：
  1. 复算：真实数据下各起点的 e（与他的矩阵/报告对齐）
  2. 打乱期序随机对照（自有实现，独立于他的 F2）
  3. **同源族幸存者偏差检查**：sum_range 有 48 档（同 family），
     在 48 档里挑最好的一档，本身就要修正。用「整族」做联合零分布。
  4. 效应是否只是在特定 window=30 档上偶然对齐
"""
import json
import math
import numpy as np

import w_common as C
from lottery import candidate_space as CS, eprocess

THR = math.log(20)


def real_e(start):
    d = C.draws(); n = len(d)
    zs, deltas, r = C.z_series(d, "sum_range", {"window": 30, "lo": 45, "hi": 75},
                               start=start, end=n)
    return math.exp(min(C.e_log(zs), 700)), np.mean(deltas), len(zs), np.mean(zs)


def shuffled_e(n_shuffle=200, start=300, seed=0):
    """打乱期序：把开奖顺序随机置换，重算该候选 e。"""
    d = C.draws(); n = len(d)
    rng = np.random.default_rng(seed)
    idx = np.arange(n)
    es = []
    for _ in range(n_shuffle):
        perm = idx.copy(); rng.shuffle(perm)
        ds = [d[i] for i in perm]
        zs, _, _ = C.z_series(ds, "sum_range", {"window": 30, "lo": 45, "hi": 75},
                              start=start, end=n)
        es.append(math.exp(min(C.e_log(zs), 700)))
    return np.array(es)


def family_null(n_shuffle=60, start=300, seed=1):
    """同源族联合零分布：sum_range 全部档位，每档都算 e，取族内最大值。
    这修正了「从 48 档里挑最好」的选择偏差。"""
    d = C.draws(); n = len(d)
    fam = []
    for i in range(CS.layer_size(CS.LAYER_SINGLE)):
        got = CS.candidate_at(CS.LAYER_SINGLE, i)
        if not got: continue
        flt, fp = got
        if flt["key"] == "sum_range":
            fam.append((fp, dict(flt.get("params") or {})))
    rng = np.random.default_rng(seed)
    idx = np.arange(n)
    null_max = []
    for _ in range(n_shuffle):
        perm = idx.copy(); rng.shuffle(perm)
        ds = [d[i] for i in perm]
        best = 0.0
        for fp, params in fam:
            zs, _, _ = C.z_series(ds, "sum_range", params, start=start, end=n)
            if len(zs) >= 30:
                best = max(best, math.exp(min(C.e_log(zs), 700)))
        null_max.append(best)
    return np.array(null_max), len(fam)


if __name__ == "__main__":
    print("【1】真实 e（sum_range 45-75, w30）：")
    for s in (0, 200, 300):
        e, md, nobs, mz = real_e(s)
        print(f"  start={s:3d}: e={e:8.2f}  mean Δ={md:+.4f}  n={nobs}  mean z={mz:+.4f}")

    print("\n【2】打乱期序随机对照（200 次，start=300）：")
    es = shuffled_e(200)
    real, *_ = real_e(300)
    print(f"  真实 e={real:.2f}；打乱 e: mean={es.mean():.2f} 95%={np.quantile(es,.95):.2f} max={es.max():.2f}")
    print(f"  p(打乱 >= 真实) = {(es>=real).mean():.3f}")
    print(f"  p(打乱 >= 20)   = {(es>=20).mean():.3f}  ← 单候选在纯随机下过阈的频率")

    print("\n【3】同源族联合零分布（修正 48 档挑选偏差，60 次）：")
    null_max, nfam = family_null(60)
    print(f"  sum_range 族档位数：{nfam}")
    print(f"  族内最大值零分布: mean={null_max.mean():.2f} 95%={np.quantile(null_max,.95):.2f} max={null_max.max():.2f}")
    # 真实族内最大值
    d = C.draws(); n = len(d)
    fambest = 0.0; famwho = None
    for i in range(CS.layer_size(CS.LAYER_SINGLE)):
        got = CS.candidate_at(CS.LAYER_SINGLE, i)
        if not got: continue
        flt, fp = got
        if flt["key"] != "sum_range": continue
        zs, _, _ = C.z_series(d, "sum_range", dict(flt.get("params") or {}), start=300, end=n)
        if len(zs)>=30:
            e = math.exp(min(C.e_log(zs),700))
            if e > fambest: fambest, famwho = e, fp
    print(f"  真实族内最大 e = {fambest:.2f}  ({famwho[:60]})")
    print(f"  p(族内最大打乱 >= 真实族内最大) = {(null_max>=fambest).mean():.3f}")

    print("\n【4】window 档依赖：")
    for w in (30, 65, 100):
        zs, deltas, r = C.z_series(d, "sum_range", {"window": w, "lo": 45, "hi": 75}, start=300, end=n)
        print(f"  w={w:3d}: 全史 e={math.exp(min(C.e_log(zs),700)):7.2f}  mean Δ={np.mean(deltas):+.4f}")
