"""ARG_WINDOW 复现公共层：真实数据 + 规格化 z 序列（含重叠窗口的方差校正）。

关键点：`hot{window:W}` 的命中序列在 W 期窗口下**自相关**（同一次开奖会被后续
W-1 期重复计入）。naive 口径 (obs-exp)/SD_HITS 低估方差 → e-value 被夸大。
这里用「有限总体修正 + 重叠协方差」给出规格化方差：

    hit_t = Σ_{j in S_t} X_{t,j},  S_t = W 期窗口内 top-k 号
    Var(hit_t | 固定 S) = k p (1-p) (N-k)/(N-1)            （超几何，p=6/33, N=33）
    Cov(hit_t, hit_{t+s}) ≈ (k²/N) · ρ_s                    ρ_s = (W-s)/W

`sig2(W)` 取 s=0..W-1 的均值 —— 即窗口内平均每个位置的边际方差，
它精确等于「把 W 期算作 1 个观测」时的单期方差，也是滚动块长 W 时的极值口径。
"""
import math
from statistics import NormalDist

from lottery import db, discovery, eprocess

N_RED, PICK = 33, 6
P = PICK / N_RED
SD_HITS = discovery.SD_HITS
ND = NormalDist()


def draws():
    return db.load_draws()


def hg_var(k):
    """单期命中数的超几何方差（给定 k 个选号）。"""
    return k * P * (1 - P) * (N_RED - k) / (N_RED - 1)


def var_eff(k, W):
    """重叠窗口下的有效单期方差：s=0..W-1 上协方差的平均。"""
    v = hg_var(k)
    cross = (k * k / N_RED)
    tot = 0.0
    for s in range(W):
        rho = (W - s) / W
        tot += (v if s == 0 else 0.0) + cross * rho
    return tot / W


def z_series(d, key, params, start=300, end=None, correct=True):
    """返回 (z 序列, 原始 d 序列)。correct=True 用重叠校正方差。"""
    flt = discovery.make_filter(key, params)
    r = discovery.asof_scan(d, flt, start=start, end=end)
    obs, exp = r["obs"], r["exp"]
    deltas = [o - e for o, e in zip(obs, exp)]
    k = round(r["k_mean"]) if r.get("k_mean") else PICK
    target = flt.get("target")
    if target == "blue":
        base = k / 16.0
        sd = math.sqrt(base * (1 - base))
    else:
        sd = math.sqrt(var_eff(k, int(params.get("window") or 1))) if correct else SD_HITS
    return [x / sd for x in deltas], deltas, r


def e_log(zs, tau=eprocess.DEFAULT_TAU):
    return eprocess.log_e_value(zs, tau=tau)


def cross_n(logm, alpha=0.05):
    thr = math.log(1 / alpha)
    for i, v in enumerate(logm, start=1):
        if v >= thr:
            return i
    return None
