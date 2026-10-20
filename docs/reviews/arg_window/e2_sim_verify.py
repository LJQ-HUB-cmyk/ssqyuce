"""E2：仿真验证「重叠校正方差」确实是 H0 下的正确零分布。

生成 33 个号、每期 6 个（真实红球口径），用 hot{window:W,k} 的 as-of 真流程跑，
看 e-value 的分布是否在校正下符合「单候选 P(e>=20)≈5%、306 候选期望 m*0.05 过阈」。
对照：不校正（production σ=0.8678）会系统夸大 e（假阳性膨胀）。
"""
import math
import numpy as np

from lottery import eprocess

N, K, W = 33, 6, 200
RNG = np.random.default_rng(20261020)
THR = math.log(20)


def hg_var(k=N*0):  # k=选号数；此处我们固定选号数为常数 c
    pass


def make_series(c, T, rng, window):
    """每期随机 6 个号；hot 选择 = window 内 top-c 号；计算每期命中（真 as-of）。"""
    hist = np.zeros((window, N), dtype=np.int8)
    obs, ksz = [], []
    for t in range(T):
        draw = rng.choice(N, size=K, replace=False)
        if t >= window:
            cnt = hist.sum(axis=0)
            top = np.argpartition(-cnt, c)[:c]
            hit = len(set(draw) & set(top))
            obs.append(hit)
            ksz.append(c)
        # 推进
        if t < window:
            hist[t % window] = 0
        else:
            row = (t - 1) % window
            hist[row] = 0
        if t >= window:
            hist[t % window] = draw[0] * 0  # placeholder; 用滚动计数更稳
    return obs


def rolling_count_window(seq, W):
    return np.convolve(seq, np.ones(W, dtype=int), mode="full")[:len(seq)]


def asof_counts(draws, W, c):
    """真 as-of：维护滚动计数。draws: list of set(号码)。返回 (obs序列, k序列)。"""
    cnt = np.zeros(N, dtype=np.int64)
    buf = []
    obs, ks = [], []
    for draw in draws:
        if len(buf) < W:
            buf.append(draw)
            for x in draw:
                cnt[x] += 1
        else:
            top = np.argpartition(-cnt, c)[:c]
            obs.append(len(set(draw) & set(top)))
            ks.append(c)
            # 弹出最旧
            old = buf.pop(0)
            for x in old:
                cnt[x] -= 1
            buf.append(draw)
            for x in draw:
                cnt[x] += 1
    return np.array(obs), np.array(ks)


def run_sim(n_sims=60, c=6, W=200, T=1600, seed=0):
    rng = np.random.default_rng(seed)
    p = K / N
    crosses_corr, crosses_naive = [], []
    for s in range(n_sims):
        draws = [rng.choice(N, size=K, replace=False).tolist() for _ in range(T)]
        obs, ks = asof_counts(draws, W, c)
        # 校正口径
        v = hg_var_eff(c, W)
        zc = (obs - c * p) / math.sqrt(v)
        # naive 口径
        zn = (obs - c * p) / math.sqrt(c * p * (1 - p) * (N - c) / (N - 1))
        ec = eprocess.log_e_value(zc.tolist())
        en = eprocess.log_e_value(zn.tolist())
        crosses_corr.append(ec >= THR)
        crosses_naive.append(en >= THR)
    return (np.mean(crosses_corr), np.mean(crosses_naive))


def hg_var_eff(c, W):
    v = c * p * (1 - p) * (N - c) / (N - 1)
    cross = (c * c / N)
    tot = 0.0
    for s in range(W):
        rho = (W - s) / W
        tot += (v if s == 0 else 0.0) + cross * rho
    return tot / W


if __name__ == "__main__":
    p = K / N
    c = 6
    print(f"单候选 P(e>=20) 理论 = 0.05（α=0.05）。")
    print("仿真：随机开奖（H0 真），W=200, c=6, 每链 1600 期：")
    for n_sims in (60, 120):
        cr, nv = run_sim(n_sims=n_sims, c=c, W=200, T=1600, seed=7)
        print(f"  n_sims={n_sims:4d}: 校正口径过阈率={cr*100:6.2f}%  "
              f"未校正(production)过阈率={nv*100:6.2f}%")
    print("→ 校正口径应 ≈5%（名义 α）；未校正系统偏高 = 假阳性膨胀。")
