"""深挖唯一跨窗稳定的候选 sum_range(45-75,w100,w65 等)：分段效应 + 打乱对照。"""
import sys
sys.path.insert(0, '/root/DSH/应用/ssqyuce')
exec(open('/root/DSH/应用/ssqyuce/docs/reviews/arg_empirical/core.py').read())
from lottery import db, discovery, candidate_space as cs
import numpy as np
import math

d = db.load_draws()
N = len(d)
SIGMA = 0.8678

# 找出 sum_range 的所有档位 + 其 k 行为
print("=== sum_range 族全部档位（全史效应）===")
rows = []
for i in range(306):
    c = cs.candidate_at('single', i)
    if not c:
        continue
    flt, fp = c
    if flt['key'] != 'sum_range':
        continue
    r = discovery.asof_scan(d, flt, start=200, end=N)
    o = np.asarray(r['obs'], float)
    e = np.asarray(r['exp'], float)
    delta = o - e
    t = delta.mean() / (delta.std(ddof=1) / math.sqrt(len(delta)))
    rows.append((fp, delta.mean(), t, r['k_mean']))

rows.sort(key=lambda x: -x[2])
print(f"{'candidate':<52} {'meanΔ':>8} {'t全史':>7} {'k':>5}")
for r in rows:
    print(f"{r[0][:50]:<52} {r[1]:+8.4f} {r[2]:+7.2f} {r[3]:5.1f}")

# 主打候选 top1
best = rows[0][0]
idx = [i for i in range(306) if cs.candidate_at('single', i) and
       cs.candidate_at('single', i)[1] == best][0]
flt = cs.candidate_at('single', idx)[0]
print(f"\n=== 主打候选: {best} ===")

r = discovery.asof_scan(d, flt, start=200, end=N)
o = np.asarray(r['obs'], float)
e = np.asarray(r['exp'], float)
delta = o - e

# 分段（每500期）
print("\n分段效应（每500期）：")
for a in range(0, len(delta), 500):
    seg = delta[a:a+500]
    if len(seg) < 50:
        break
    tt = seg.mean() / (seg.std(ddof=1)/math.sqrt(len(seg)))
    issue = d[200+a]['issue']
    print(f"  {issue} 期起 {len(seg)} 期: meanΔ={seg.mean():+.4f} t={tt:+6.2f}")

# 打乱对照：200 次
print("\n打乱期序 200 次对照（同一候选、同一全史窗口）：")
rng = np.random.default_rng(2024)
ev_real = float(np.exp(np.clip(log_e_from_sum((delta/SIGMA).sum(), len(delta)), -700, 700)))
ev_rand = []
for k in range(200):
    idx2 = rng.permutation(N)
    ds = [d[j] for j in idx2]
    rr = discovery.asof_scan(ds, flt, start=200, end=N)
    dd = np.asarray(rr['obs'], float) - np.asarray(rr['exp'], float)
    ev_rand.append(float(np.exp(np.clip(log_e_from_sum((dd/SIGMA).sum(), len(dd)), -700, 700))))
ev_rand = np.array(ev_rand)
print(f"  真实 e={ev_real:.3f}")
print(f"  随机 e: 均值={ev_rand.mean():.3f} 95%={np.percentile(ev_rand,95):.3f} 最大={ev_rand.max():.3f}")
print(f"  p(随机>=真实)={(ev_rand>=ev_real).mean():.3f}")
