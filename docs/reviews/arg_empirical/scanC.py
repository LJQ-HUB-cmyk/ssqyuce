import sys; sys.path.insert(0,'/root/DSH/应用/ssqyuce')
exec(open('/root/DSH/应用/ssqyuce/docs/reviews/arg_empirical/core.py').read())
from lottery import db
import numpy as np

d = draws()
Z, fps, keys = build_series(d, start=0, tag='base')
N = Z.shape[1]

def effect_in_window(Z, a, b):
    """候选在 [a,b) 期的平均效应 Δ = mean(obs-exp) 每期红球。"""
    seg = Z[:, a:b] * SIGMA
    return seg.mean(axis=1)

print("=== C. 窗口间稳定性/持续性 ===")
print("候选维度 = 306 个单因子，效应量 = 该窗口内 mean(obs-exp)（每期红球数）\n")

# (a) 相邻非重叠窗口（对齐 PHASE_USABLE_STUDY 的 W=150 滑动）
for W in [150, 250, 500]:
    pairs = []
    a = 200
    while a + 2*W <= N:
        e1 = effect_in_window(Z, a, a+W)
        e2 = effect_in_window(Z, a+W, a+2*W)
        pairs.append(np.stack([e1, e2]))
        a += W
    if not pairs: continue
    X = np.concatenate([p[0] for p in pairs]); Y = np.concatenate([p[1] for p in pairs])
    r = np.corrcoef(X, Y)[0,1]
    n = len(X)
    tol = 1.96/math.sqrt(n)
    print(f"W={W}: 相邻窗口效应量相关性 r = {r:+.4f} (n={n} 窗口-候选对, 95%容差 ±{tol:.4f}) → {'有持续性' if abs(r)>tol else '无持续性'}")

# (b) 相邻窗口的 e-value 相关（同候选）
print("\n--- 相邻窗口 e-value 相关性（同候选） ---")
def evec(Z, a, b):
    S = Z[:, a:b].sum(axis=1); t = b-a
    return np.exp(np.clip(log_e_from_sum(S, t), -700, 700))
for W in [150, 250, 500]:
    evs=[]
    a=200
    while a+2*W <= N:
        evs.append(np.stack([evec(Z,a,a+W), evec(Z,a+W,a+2*W)]))
        a += W
    if not evs: continue
    X=np.concatenate([p[0] for p in evs]); Y=np.concatenate([p[1] for p in evs])
    # 用 log e 更稳
    r = np.corrcoef(np.log1p(X), np.log1p(Y))[0,1]
    print(f"W={W}: 相邻窗口 log(e) 相关性 r = {r:+.4f} (n={len(X)})")

# (c) 更大的窗口是否更稳定？对同一候选，跨窗口用 Spearman 秩相关对比
print("\n--- 同一候选在不同窗口下 top 是否一致（秩相关） ---")
from scipy.stats import spearmanr
e_windows = {}
for w in [150,250,300,500,750,1000,1500,None]:
    e,_,_ = e_of_window(Z,w)
    e_windows[str(w) if w else 'full'] = e
ref = e_windows['full']
for w in [150,250,300,500,750,1000,1500]:
    rho,_ = spearmanr(ref, e_windows[str(w)])
    print(f"全史 vs W={w}: Spearman ρ = {rho:+.4f}")
