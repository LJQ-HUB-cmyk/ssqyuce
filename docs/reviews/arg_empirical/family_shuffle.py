"""族级打乱对照：sum_range 48 档，每次打乱取族内最大 e，构造族级零分布。

这是比"单候选打乱"更严格的口径 —— 它把"从 48 档里挑最好的"这个选择行为
本身计入零分布，正好对应幸存者偏差（survivorship bias）。
"""
import sys
sys.path.insert(0, '/root/DSH/应用/ssqyuce')
exec(open('/root/DSH/应用/ssqyuce/docs/reviews/arg_empirical/core.py').read())
from lottery import db, discovery, candidate_space as cs
import numpy as np
import math

d = db.load_draws()
N = len(d)
SIGMA = 0.8678
START = 0

# sum_range 族全部 48 档
FAM = []
for i in range(306):
    c = cs.candidate_at('single', i)
    if not c:
        continue
    flt, fp = c
    if flt['key'] == 'sum_range':
        FAM.append((flt, fp))
print(f"sum_range 族档位数 = {len(FAM)}", flush=True)


def fam_max_e(draws_list):
    """族内最大 e（全史累积）。"""
    best = 0.0
    for flt, fp in FAM:
        r = discovery.asof_scan(draws_list, flt, start=START, end=len(draws_list))
        o = np.asarray(r['obs'], float)
        e = np.asarray(r['exp'], float)
        z = (o - e) / SIGMA
        ev = float(np.exp(np.clip(log_e_from_sum(z.sum(), len(z)), -700, 700)))
        if ev > best:
            best = ev
    return best


e_real = fam_max_e(d)
print(f"真实数据族内最大 e = {e_real:.2f}", flush=True)

rng = np.random.default_rng(303)
vals = []
NREP = int(sys.argv[1]) if len(sys.argv) > 1 else 40
for k in range(NREP):
    idx = rng.permutation(N)
    ds = [d[i] for i in idx]
    vals.append(fam_max_e(ds))
    print(f"  shuffle {k}: {vals[-1]:.2f}", flush=True)

vals = np.array(vals)
print(f"\n族级零分布（{NREP} 次打乱）:")
print(f"  均值={vals.mean():.2f}  中位={np.median(vals):.2f}  95%分位={np.percentile(vals,95):.2f}  最大={vals.max():.2f}")
print(f"  真实 {e_real:.2f} 的分位 = {(vals < e_real).mean()*100:.1f}%")
print(f"  p(族级随机 >= 真实) = {(vals >= e_real).mean():.3f}")
