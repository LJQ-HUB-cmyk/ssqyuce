"""检验：固定 k=6 的候选，其"选出的 6 个号"能否通过超几何检验（即集合本身是否非随机）。"""
import sys
sys.path.insert(0, '/root/DSH/应用/ssqyuce')
exec(open('/root/DSH/应用/ssqyuce/docs/reviews/arg_empirical/core.py').read())
from lottery import db, discovery, candidate_space as cs
import numpy as np
import math

d = db.load_draws()
N = len(d)
W = 300
SIGMA = 0.8678

targets = []
for i in range(306):
    c = cs.candidate_at('single', i)
    if not c:
        continue
    flt, fp = c
    r = discovery.asof_scan(d, flt, start=200, end=N)
    o = np.asarray(r['obs'], float)
    e = np.asarray(r['exp'], float)
    delta = o - e
    z = delta / SIGMA
    ev = float(np.exp(np.clip(log_e_from_sum(z[-W:].sum(), W), -700, 700)))
    if ev >= 20:
        sd_emp = delta[-W:].std(ddof=1)
        t = delta[-W:].mean() / (sd_emp / math.sqrt(W))
        targets.append((i, fp, ev, r['k_mean'], delta[-W:].mean(), sd_emp, t))

print(f"W={W} 下 e>=20 的候选（共 {len(targets)} 个）：")
print(f"{'candidate':<50} {'e':>8} {'k_mean':>7} {'meanΔ':>8} {'sd_emp':>8} {'t':>7}")
print('-' * 95)
for t in targets:
    print(f"{t[1][:48]:<50} {t[2]:8.2f} {t[3]:7.2f} {t[4]:+8.4f} {t[5]:8.4f} {t[6]:+7.2f}")

# 每个候选在 W=300 内的 obs 分布（是否集中）
print("\n各候选 W=300 内 obs/exp 的 t 检验（自由度 299，双尾 5% 临界 ±1.968）：")
for t in targets:
    i = t[0]
    sig = 'YES' if abs(t[6]) > 1.968 else 'no'
    print(f"  {t[1][:44]:<46} t={t[6]:+.2f}  → {'显著' if sig=='YES' else '不显著'}")

# 关键：这些候选在更深历史窗口（如 W=2000）是否仍显著？
print("\n同一批候选换更宽窗口复验（多重窗口一致性）：")
for t in targets:
    i = t[0]
    flt = cs.candidate_at('single', i)[0]
    r = discovery.asof_scan(d, flt, start=200, end=N)
    o = np.asarray(r['obs'], float); e = np.asarray(r['exp'], float)
    delta = o - e
    line = f"  {t[1][:40]:<42}"
    for WW in [300, 500, 1000, 2000]:
        if len(delta) < WW:
            line += f" W{WW}:--"
            continue
        seg = delta[-WW:]
        sd = seg.std(ddof=1)
        tt = seg.mean() / (sd/math.sqrt(WW)) if sd > 0 else 0
        line += f" W{WW}:{tt:+5.2f}"
    print(line)
