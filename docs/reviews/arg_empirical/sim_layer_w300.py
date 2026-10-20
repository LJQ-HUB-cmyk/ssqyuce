"""整层随机对照：306 个单因子候选在 W=300 窗口下 e>=20 的个数，是否超出随机期望。"""
import sys
sys.path.insert(0, '/root/DSH/应用/ssqyuce')
exec(open('/root/DSH/应用/ssqyuce/docs/reviews/arg_empirical/core.py').read())
from lottery import db, discovery, candidate_space as cs
import numpy as np

SIGMA = 0.8678
d = db.load_draws()
N = len(d)
W = 300
FLTS = [x[0] for x in (cs.candidate_at('single', i) for i in range(306)) if x]


def layer_e(ds):
    es = []
    for flt in FLTS:
        r = discovery.asof_scan(ds, flt, start=200, end=len(ds))
        o = np.asarray(r['obs'], float)
        e = np.asarray(r['exp'], float)
        z = (o - e) / SIGMA
        es.append(float(np.exp(np.clip(log_e_from_sum(z[-W:].sum(), W), -700, 700))))
    return np.array(es)


rng = np.random.default_rng(11)
e_real = layer_e(d)
n_real = int((e_real >= 20).sum())
print(f"真实 W={W}: e>=20 个数 = {n_real} (max_e={e_real.max():.1f})", flush=True)

ns = []
NREP = int(sys.argv[1]) if len(sys.argv) > 1 else 30
for r in range(NREP):
    idx = rng.permutation(N)
    ds = [d[i] for i in idx]
    e_s = layer_e(ds)
    ns.append(int((e_s >= 20).sum()))
    print(f"  shuffle {r}: {ns[-1]}", flush=True)

ns = np.array(ns)
print(f"\n随机 {NREP} 次: e>=20 个数 均值={ns.mean():.2f} 最大={ns.max()} 中位={np.median(ns):.0f}")
print(f"p(随机>=真实 {n_real}) = {(ns >= n_real).mean():.3f}")
