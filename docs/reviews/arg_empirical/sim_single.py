import sys; sys.path.insert(0,'/root/DSH/应用/ssqyuce')
exec(open('/root/DSH/应用/ssqyuce/docs/reviews/arg_empirical/core.py').read())
from lottery import db, discovery, candidate_space as cs
import numpy as np, random

d = db.load_draws()
N = len(d)
SIGMA = 0.8678

def scan_hits(draws_list, flt):
    r = discovery.asof_scan(draws_list, flt, start=0, end=len(draws_list))
    o=np.asarray(r['obs'],float); e=np.asarray(r['exp'],float)
    return (o-e)/SIGMA

# 要验证的高 e 候选（窗口 None = 全史累积）
TARGETS = [
  ('cold_w200_k7',   ('cold', {'window':200,'k':7}),          1000),
  ('hot_w10_k6',     ('hot',  {'window':10,'k':6}),           300),
  ('sum_45_75_w30',  ('sum_range', {'window':30,'lo':45,'hi':75}), None),
]

def shuffle_draws(draws_list, rng):
    """打乱期号顺序（保留每期内容，破坏时间结构）。"""
    idx = rng.permutation(len(draws_list))
    return [draws_list[i] for i in idx]

print("=== 随机模拟：打乱期序 200 次 ===")
print(f"{'target':<18} {'真实 max_e':>12} {'随机 max_e 均值':>16} {'随机95%':>10} {'p(随机>=真实)':>14}")
for name,(key,params),W in TARGETS:
    flt = discovery.make_filter(key, params)
    z_real = scan_hits(d, flt)
    # 真实：在窗口 W 下的 e（用最后一个 W 期）
    if W:
        S=z_real[-W:].sum(); t=W
    else:
        S=z_real.sum(); t=len(z_real)
    e_real = float(np.exp(np.clip(log_e_from_sum(S,t),-700,700)))
    # 随机
    rng = np.random.default_rng(42)
    e_rand = []
    for _ in range(200):
        ds = shuffle_draws(d, rng)
        z = scan_hits(ds, flt)
        if W: S2=z[-W:].sum(); t2=W
        else: S2=z.sum(); t2=len(z)
        e_rand.append(float(np.exp(np.clip(log_e_from_sum(S2,t2),-700,700))))
    e_rand=np.array(e_rand)
    p = (e_rand>=e_real).mean()
    print(f"{name:<18} {e_real:12.3f} {e_rand.mean():16.3f} {np.percentile(e_rand,95):10.3f} {p:14.3f}")
    print(f"    随机 e 最大={e_rand.max():.2f} 最小={e_rand.min():.2f}  >20 的比率={(e_rand>=20).mean():.2%}")
