import sys; sys.path.insert(0,'/root/DSH/应用/ssqyuce')
exec(open('/root/DSH/应用/ssqyuce/docs/reviews/arg_empirical/core.py').read())
from lottery import db
import numpy as np

d = draws()
Z, fps, keys = build_series(d, start=0, tag='base')
N = Z.shape[1]
WINDOWS = [150, 250, 300, 500, 750, 1000, 1500, None]

print(f"\n=== 数据: {N} 期, {len(fps)} 个单因子候选 ===\n")
print(f"{'W':>6} | {'max_e':>9} {'e95':>9} {'median':>9} | {'e>=20':>6} | {'e>=1':>6}")
for w in WINDOWS:
    e, S, t = e_of_window(Z, w)
    lab = str(w) if w else '全史'
    q95 = np.percentile(e, 95); med = np.median(e)
    mx = e.max()
    n20 = int((e >= 20).sum()); n1 = int((e >= 1).sum())
    print(f"{lab:>6} | {mx:9.3f} {q95:9.3f} {med:9.3f} | {n20:6d} | {n1:6d}")

# 各窗口 top5
print("\n=== 各窗口 top5 候选 (e-value 降序) ===")
for w in WINDOWS:
    e, S, t = e_of_window(Z, w)
    lab = str(w) if w else '全史'
    order = np.argsort(-e)[:5]
    print(f"\n-- W={lab} --")
    for rk, i in enumerate(order,1):
        print(f"  #{rk} e={e[i]:9.3f}  {fps[i][:45]}  | {keys[i]}")
