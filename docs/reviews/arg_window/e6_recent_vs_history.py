"""E6：直接检验「近期更容易找到规律」这个直觉 —— 用真实 3516 期。

三个独立检验：
  检验1：滚动的「最近 W 期效应量标准差」 vs 全史 → 近期波动是否更大（更容易出"看起来显著"）？
  检验2：把候选空间冻结，比较「最近 W 期」内过阈候选数 与该 W 期长度下随机期望
  检验3：「近期过阈」能否预测「下一段同向」（持续性）—— 跨 8 个非重叠 W 期
  检验4：多窗口过阈候选的**重叠度**：近期冒头的候选，在全史口径下是否也高？
"""
import math
import numpy as np

import w_common as C
from lottery import candidate_space as CS, eprocess

THR = math.log(20)


def zmatrix(W, start=None):
    """返回 (候选 × 期) 的 z 矩阵（红球单因子）。"""
    d = C.draws(); n = len(d)
    start = max(300, n - W) if start is None else start
    rows, meta = [], []
    for i in range(CS.layer_size(CS.LAYER_SINGLE)):
        got = CS.candidate_at(CS.LAYER_SINGLE, i)
        if not got:
            continue
        flt, fp = got
        if flt.get("target") != "red":
            continue
        zs, _, _ = C.z_series(d, flt["key"], dict(flt.get("params") or {}), start=start, end=n)
        if len(zs) < 30:
            continue
        rows.append(zs); meta.append((fp, flt["key"], dict(flt.get("params") or {})))
    return np.array(rows), meta


def check1_volatility():
    print("【检验1】各窗口下候选效应量的离散度（|mean z| 的分布）")
    print(f"{'W':>6} {'|mean z| 中位':>13} {'p90':>8} {'p99':>8} {'max':>7} {'过阈':>5} {'理论 m·α':>9}")
    for W in (150, 300, 500, 1000, 2000, 3216):
        M, meta = zmatrix(W)
        mz = np.abs(M.mean(axis=1))
        loges = np.array([C.e_log(r.tolist()) for r in M])
        print(f"{W:>6} {np.median(mz):>13.4f} {np.quantile(mz,.9):>8.4f} "
              f"{np.quantile(mz,.99):>8.4f} {mz.max():>7.4f} {(loges>=THR).sum():>5} "
              f"{len(meta)*0.05:>9.1f}")


def check2_persistence():
    print("\n【检验2】上一窗口的 top-20 候选，在下一窗口是否仍高？（持续性）")
    W = 500
    d = C.draws(); n = len(d)
    blocks = [(a, a + W) for a in range(300, n - W + 1, W)]
    print(f"  非重叠块数：{len(blocks)}")
    # 对每个候选，计算每块内的 mean z
    cands = []
    for i in range(CS.layer_size(CS.LAYER_SINGLE)):
        got = CS.candidate_at(CS.LAYER_SINGLE, i)
        if not got:
            continue
        flt, fp = got
        if flt.get("target") != "red":
            continue
        cands.append((fp, flt["key"], dict(flt.get("params") or {})))
    M = np.zeros((len(cands), len(blocks)))
    for j, (a, b) in enumerate(blocks):
        for i, (fp, key, params) in enumerate(cands):
            zs, _, _ = C.z_series(d, key, params, start=a, end=b)
            M[i, j] = np.mean(zs) if zs else 0.0
    xs, ys = M[:, :-1].ravel(), M[:, 1:].ravel()
    r = np.corrcoef(xs, ys)[0, 1]
    n_obs = len(xs)
    tol = 1.96 / math.sqrt(n_obs)
    print(f"  相邻块 mean z 相关：r = {r:+.4f}（n={n_obs}，95% 容差 ±{tol:.4f}）")
    print(f"  → {'有持续性' if abs(r) > tol else '无持续性'}")

    # top 候选的下一块表现
    top_rate, base_rate = [], []
    for j in range(M.shape[1] - 1):
        top = np.argsort(-M[:, j])[:20]
        top_rate.append(M[top, j + 1].mean())
        base_rate.append(M[:, j + 1].mean())
    print(f"  上块 top20 的下一块 mean z = {np.mean(top_rate):+.4f}；全体均值 = {np.mean(base_rate):+.4f}")
    se = np.std([np.mean(top_rate)]) if False else np.std(top_rate, ddof=1) / math.sqrt(len(top_rate))
    print(f"  Δ = {np.mean(top_rate)-np.mean(base_rate):+.4f}，t = {(np.mean(top_rate)-np.mean(base_rate))/se:+.2f}")


def check3_overlap():
    print("\n【检验3】近期(500) 与全史 的 top 候选重叠度")
    M5, meta = zmatrix(500)
    Mg, _ = zmatrix(3216, start=300)
    # 对齐：zmatrix 的候选顺序一致
    n = min(len(meta), len(M5), len(Mg))
    m5 = np.abs(M5[:n].mean(axis=1))
    mg = np.abs(Mg[:n].mean(axis=1))
    for K in (10, 20, 50):
        a = set(np.argsort(-m5)[:K].tolist())
        b = set(np.argsort(-mg)[:K].tolist())
        print(f"  top{K} 重叠：{len(a & b)}/{K}（随机期望 {K*K/n:.1f}）")
    from scipy.stats import spearmanr
    print(f"  |mean z| 排序相关：Spearman = {spearmanr(m5, mg).statistic:+.3f}")


if __name__ == "__main__":
    check1_volatility()
    check2_persistence()
    check3_overlap()
