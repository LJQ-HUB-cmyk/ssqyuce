#!/usr/bin/env python3
"""「阶段性规律可用」研究的可复现脚本。

对应报告：docs/reviews/PHASE_USABLE_STUDY.md
用法：cd /root/DSH/应用/ssqyuce && PYTHONPATH=. .venv/bin/python docs/reviews/PHASE_USABLE_STUDY.py
"""
import json
import numpy as np

from lottery import db, discovery

W = 150          # 一个"阶段"的期数（与 ERA_BLOCK 对齐）
POOL = [("hot", {"window": 30, "k": 6}), ("hot", {"window": 50, "k": 6}),
        ("hot", {"window": 100, "k": 6}), ("cold", {"window": 30, "k": 6}),
        ("cold", {"window": 50, "k": 6}), ("repeat", {"back": 1}),
        ("neighbor", {"back": 1, "offset": 1}), ("omission_high", {"k": 6})]


def _delta_matrix(draws):
    """每个维度 × 每期的 (观测 - 期望)。"""
    rows = []
    for key, params in POOL:
        r = discovery.asof_scan(draws, discovery.make_filter(key, params), start=200, end=len(draws))
        rows.append(np.asarray(r["obs"], float) - np.asarray(r["exp"], float))
    return np.vstack(rows)


def check1_status_distribution(draws):
    res = discovery.discover(draws, top=40)
    from collections import Counter
    cnt = Counter((it.get("regime") or {}).get("status") for it in (res.get("items") or []))
    print("【检验1】91 组维度扫描后的状态分布：", dict(cnt))
    print("  → persistent/emerging 条数：",
          cnt.get("persistent", 0), "/", cnt.get("emerging", 0))


def check2_recent_vs_history(draws):
    N = len(draws)
    print("\n【检验2】历史 vs 最近 150 期的效应量 Δ（观测-期望）：")
    for key, params in POOL:
        f = discovery.make_filter(key, params)
        h = discovery.asof_scan(draws, f, start=200, end=N - W)
        r = discovery.asof_scan(draws, f, start=N - W, end=N)
        hd = np.asarray(h["obs"], float) - np.asarray(h["exp"], float)
        rd = np.asarray(r["obs"], float) - np.asarray(r["exp"], float)
        z = rd.mean() / (rd.std(ddof=1) / np.sqrt(len(rd))) if rd.std(ddof=1) > 0 else 0
        print(f"  {key} {json.dumps(params):28s} 历史 {hd.mean():+.4f}  最近 {rd.mean():+.4f} (z={z:+.2f})")


def check3_random_control(draws, n_windows=200, seed=0):
    """随机窗口对照：看"多维度同向"是否能被随机解释。"""
    N = len(draws)
    def n_pos(a, b):
        c = 0
        for key, params in POOL:
            r = discovery.asof_scan(draws, discovery.make_filter(key, params), start=a, end=b)
            d = np.asarray(r["obs"], float) - np.asarray(r["exp"], float)
            if len(d) and d.mean() > 0:
                c += 1
        return c
    real = n_pos(N - W, N)
    rng = np.random.default_rng(seed)
    rand = np.array([n_pos(int(a), int(a) + W)
                     for a in rng.integers(200, N - W, n_windows)])
    print(f"\n【检验3】真实最近窗口正偏离 {real}/{len(POOL)} 个")
    print(f"  随机窗口下 >= {real} 个的概率：{(rand >= real).mean():.2%}")
    print("  → 若该概率不低（如 >5%），说明'同向性'不能作为信号")


def check4_rolling_best(draws):
    """决定性检验：滚动用"最近阶段最佳"维度出号，看长期是否优于基线。"""
    M = _delta_matrix(draws)
    obs_baseline = 6 * 6 / 33
    # 从 asof_scan 重建"命中"：Δ = 命中 - 期望，基线期望对每个维度一致（6*6/33）
    hits = []
    for t in range(W, M.shape[1]):
        best = int(np.argmax(M[:, t - W:t].mean(axis=1)))
        hits.append(M[best, t] + obs_baseline)
    hits = np.array(hits)
    d = hits - obs_baseline
    se = d.std(ddof=1) / np.sqrt(len(d))
    print(f"\n【检验4】滚动选『最近 {W} 期最佳』出号，{len(hits)} 期：")
    print(f"  平均命中 {hits.mean():.4f} vs 基线 {obs_baseline:.4f}")
    print(f"  Δ={d.mean():+.4f} ± {1.96*se:.4f}   t={d.mean()/se:+.2f}  "
          f"{'显著' if abs(d.mean()/se) > 1.96 else '不显著'}")


def check5_persistence(draws):
    """必要条件检验：上一窗口好，能否预测下一窗口也好。"""
    M = _delta_matrix(draws)
    xs, ys = [], []
    for a in range(0, M.shape[1] - 2 * W + 1, W):
        xs.extend(M[:, a:a + W].mean(axis=1).tolist())
        ys.extend(M[:, a + W:a + 2 * W].mean(axis=1).tolist())
    r = np.corrcoef(xs, ys)[0, 1]
    tol = 1.96 / np.sqrt(len(xs))
    print(f"\n【检验5】相邻窗口相关性 r = {r:+.4f}（n={len(xs)}，95% 容差 ±{tol:.4f}）")
    print(f"  → {'有持续性' if abs(r) > tol else '无持续性：上一窗口好不能预测下一窗口'}")


if __name__ == "__main__":
    draws = db.load_draws()
    print(f"数据：{len(draws)} 期（{draws[0]['issue']} – {draws[-1]['issue']}）\n")
    check1_status_distribution(draws)
    check2_recent_vs_history(draws)
    check3_random_control(draws)
    check4_rolling_best(draws)
    check5_persistence(draws)
    print("\n完整结论见 docs/reviews/PHASE_USABLE_STUDY.md")
