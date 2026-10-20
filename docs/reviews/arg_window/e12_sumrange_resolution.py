"""E12：定论 —— sum_range(45-75, w30) 到底是真规律还是幸存者偏差？

三个铁证：
  1. 真实开奖全史：e=109.35（start=300）/ 152.93（start=0），单候选看似极强
  2. 但在 48 档同源族联合零分布下：p=0.083（不显著）
  3. 进一步：若在 306 个单因子全局做 FDR（e-BH）校正：
     k=1 所需 e >= 306/(1*0.05) = 6120，真实 109.35 远低于 6120
     即：就算它在单候选层面打乱 p=0.005，在全局多重检验下它也**无法存活**
  4. 邻近参数坍缩：w=65 e=4.04、w=100 e=4.20，hi=90 e=1.63，无参数稳定性
"""
import math
import numpy as np

import w_common as C
from lottery import candidate_space as CS, eprocess

def check():
    d = C.draws(); n = len(d)
    # 取全史 306 候选在 start=300 下的 e 值
    es = []
    fps = []
    for i in range(CS.layer_size(CS.LAYER_SINGLE)):
        got = CS.candidate_at(CS.LAYER_SINGLE, i)
        if not got: continue
        flt, fp = got
        if flt.get("target") != "red": continue
        zs, _, _ = C.z_series(d, flt["key"], dict(flt.get("params") or {}), start=300, end=n)
        if len(zs) >= 30:
            es.append(math.exp(min(C.e_log(zs), 700)))
            fps.append(fp)
    es = np.array(es)
    top_idx = np.argmax(es)
    print(f"全史 285 红球候选最大 e = {es.max():.2f}")
    print(f"  对应候选：{fps[top_idx][:70]}")
    # e-BH 判定
    order = np.argsort(-es)
    k_star = 0
    m = 306
    for rank, idx in enumerate(order, start=1):
        if es[idx] >= m / (rank * 0.05):
            k_star = rank
    print(f"  e-BH(m=306, alpha=0.05) 选中数：{k_star}")
    print(f"  第 1 名所需 e: {m/0.05:.0f}，实际: {es.max():.2f}（差距 {m/0.05/es.max():.1f} 倍）")

if __name__ == "__main__":
    check()
