import sys; sys.path.insert(0,'/root/DSH/应用/ssqyuce')
exec(open('/root/DSH/应用/ssqyuce/docs/reviews/arg_empirical/core.py').read())
from lottery import db
import numpy as np
from scipy.stats import spearmanr

d = draws()
Z, fps, keys = build_series(d, start=0, tag='base')
N = Z.shape[1]

# ===== D. 分段方向一致性 =====
# 每 500 期一段，共约 7 段。对每段，候选效应方向 = sign(mean(obs-exp))，再用 z 是否显著
SEG = 500
seg_bounds = list(range(200, N, SEG))   # 从 200 期后开始分
print("=== D. 分段（每 500 期）效应方向一致性 ===")
print(f"分段起点: {seg_bounds} (共 {len(seg_bounds)} 段)\n")

# top10 候选维度 = 按全史 e-value 排名前 10
e_full,_,_ = e_of_window(Z, None)
top10_idx = np.argsort(-e_full)[:10]
print("按全史 e-value 排名前10的候选维度：")
for rk,i in enumerate(top10_idx,1):
    print(f"  #{rk} e={e_full[i]:.3f} {fps[i][:48]}")
print()

# 对每段算每个 top10 候选的效应方向 & 显著性
print("每段的效应方向（+正/-负/*显著z>1.96）：")
print(f"{'候选':<46} " + "".join(f"  S{k}({seg_bounds[k]//1000}千)" for k in range(len(seg_bounds))))
for i in top10_idx:
    row = f"{fps[i][:44]:<46} "
    for k, a in enumerate(seg_bounds):
        b = min(a+SEG, N)
        seg = Z[i, a:b]*SIGMA
        dm = seg.mean()
        se = seg.std(ddof=1)/np.sqrt(len(seg)) if len(seg)>1 and seg.std(ddof=1)>0 else np.nan
        z = dm/se if se==se and se>0 else 0.0
        sign = '+' if dm>0 else ('-' if dm<0 else '0')
        star = '*' if abs(z)>1.96 else ' '
        row += f"  {sign}{star}"
    print(row)

# 统计：有多少个维度在 ≥3 段方向一致
print("\n全候选维度在各段的效应方向（非仅top10）：")
count_same = 0
for i in range(len(fps)):
    signs=[]
    for a in seg_bounds:
        b=min(a+SEG,N)
        dm=(Z[i,a:b]*SIGMA).mean()
        signs.append(1 if dm>0 else (-1 if dm<0 else 0))
    nz = [s for s in signs if s!=0]
    if len(nz)>=3 and (nz.count(1)>=len(nz)-1 or nz.count(-1)>=len(nz)-1):
        count_same += 1
print(f"在 ≥{len(seg_bounds)} 段中，方向一致(≥{len(seg_bounds)-1}段同号)的候选数: {count_same} / {len(fps)}")
print(f"  随机期望(~{1/2**(len(seg_bounds)-1):.0%}×306): 约 {306*(1/2**(len(seg_bounds)-1)):.1f} 个")

# ===== E. 时间序列分段：每候选 e-value 最高窗口段 =====
print("\n=== E. 每个候选 e-value 最高的时间窗口段 ===")
# 用滚动窗口 W=500 找每个候选的最佳起始位置
W = 500
best_start = np.zeros(len(fps), int)
best_e = np.zeros(len(fps))
for i in range(len(fps)):
    es = np.array([ np.exp(np.clip(log_e_from_sum(Z[i,a:a+W].sum(), W), -700, 700)) for a in range(200, N-W+1) ])
    k = int(np.argmax(es))
    best_start[i]=200+k; best_e[i]=es[k]
# 按年份分布
year_of = {}
for i in range(len(fps)):
    s = best_start[i]
    yr = d[s]['issue'][:4]
    year_of[yr] = year_of.get(yr,0)+1
print("各候选 best-500期段 起始年份分布：")
for yr in sorted(year_of):
    print(f"  {yr}: {year_of[yr]} 个候选最佳段以该年开头")
# 最高e的候选
print("\ne-value 最高的 top10 候选的最佳窗口段：")
order = np.argsort(-best_e)[:10]
for rk,i in enumerate(order,1):
    s=best_start[i]; yr=d[s]['issue'][:4]
    print(f"  #{rk} best_e={best_e[i]:9.3f} 起始{s}({yr}) e全史={e_full[i]:7.3f}  {fps[i][:45]}")
