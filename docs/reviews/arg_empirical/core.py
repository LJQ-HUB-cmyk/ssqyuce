"""共享计算核心：缓存 asof_scan，任意窗口/前缀 e-value、MDE。"""
import sys
sys.path.insert(0, '/root/DSH/应用/ssqyuce')
import numpy as np, math, json, os, pickle, time
from lottery import db, discovery, eprocess, candidate_space as cs

SIGMA = 0.8678            # rigor.BASELINE_SD_HITS，理论超几何 sd
TAU = eprocess.DEFAULT_TAU
ALPHA = eprocess.DEFAULT_ALPHA
WINDOWS = [150, 250, 300, 500, 750, 1000, 1500, None]   # None = 全史

_cache_dir = '/tmp/arg_cache'
os.makedirs(_cache_dir, exist_ok=True)

def draws():
    return db.load_draws()

# ---------- 1) z 序列缓存 ----------
def build_series(d, start=0, layer='single', n=None, tag='base'):
    """对每个候选跑 asof_scan(start=0..N)，缓存 (obs-exp)/sigma 序列。"""
    N = len(d)
    n = cs.layer_size(layer) if n is None else n
    path = f'{_cache_dir}/series_{tag}_{layer}_{start}_{n}.npz'
    if os.path.exists(path):
        z = np.load(path, allow_pickle=True)
        return z['Z'], list(z['fps']), list(z['keys'])
    t0 = time.time()
    Z, fps, keys = [], [], []
    for i in range(n):
        c = cs.candidate_at(layer, i)
        if c is None:
            continue
        flt, fp = c
        r = discovery.asof_scan(d, flt, start=start, end=N)
        o = np.asarray(r['obs'], float); e = np.asarray(r['exp'], float)
        z = (o - e) / SIGMA
        Z.append(z); fps.append(fp); keys.append(flt['key'])
    L = min(len(x) for x in Z)
    Z = np.vstack([x[:L] for x in Z])
    np.savez(path, Z=Z, fps=np.array(fps, dtype=object), keys=np.array(keys, dtype=object))
    print(f'[build] {layer} n={len(fps)} L={L} in {time.time()-t0:.1f}s', flush=True)
    return Z, fps, keys

# ---------- 2) e-process ----------
def log_e_from_sum(S, t, tau=TAU):
    """给定累计和 S 与样本数 t，返回 log M_t（向量化）。"""
    S = np.asarray(S, float)
    tau2 = tau * tau
    denom = 1.0 + tau2 * t
    return -0.5 * np.log(denom) + (tau2 * S * S) / (2.0 * denom)

def e_of_window(Z, w=None, tau=TAU):
    """每个候选在「最后 w 期」的 e-value。w=None → 全史。"""
    if w is None:
        S = Z.sum(axis=1); t = Z.shape[1]
    else:
        S = Z[:, -w:].sum(axis=1); t = min(w, Z.shape[1])
    return np.exp(np.clip(log_e_from_sum(S, t, tau), -700, 700)), S, t

# ---------- 3) 解析 MDE（固定样本 α=0.05） ----------
def mde_fixed(w, sigma=SIGMA, alpha=ALPHA, side='one'):
    """固定样本量 w 下，单候选双侧 z 检验可检出的最小 Δ（每期多命中红球数）。"""
    from scipy.stats import norm
    z_crit = norm.ppf(1 - alpha/2) if side == 'two' else norm.ppf(1 - alpha)
    return z_crit * sigma / math.sqrt(w)

def mde_power(w, sigma=SIGMA, alpha=ALPHA, power=0.80, side='two'):
    """达到 power 所需的最小 Δ（含第二类错误）。"""
    from scipy.stats import norm
    za = norm.ppf(1 - alpha/2) if side == 'two' else norm.ppf(1 - alpha)
    zb = norm.ppf(power)
    return (za + zb) * sigma / math.sqrt(w)

def mde_eprocess(w, sigma=SIGMA, tau=TAU, alpha=ALPHA):
    """e-process 在 w 期时首次穿越所需的 Δ（解析反解，单候选）。"""
    log_thr = math.log(1.0/alpha)
    tau2 = tau*tau
    denom = 1.0 + tau2*w
    # 令 -0.5log(denom) + tau2*(zbar*w)^2/(2 denom) = log_thr
    # ⇒ (zbar*w)^2 = 2*denom*(log_thr+0.5log denom)/tau2
    val = 2.0*denom*(log_thr + 0.5*math.log(denom))/tau2
    if val <= 0:
        return 0.0
    zbar = math.sqrt(val)/w
    return zbar*sigma
