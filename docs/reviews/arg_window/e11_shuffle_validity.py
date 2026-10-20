"""E11：打乱期序检验是否有效？—— 检查 sum_range 的统计量在打乱下的分布。

疑点：E10 里打乱 e 的 max=231.97，远高于真实 109.35，且 95% 分位只有 8.23。
这种「极小中位数 + 极长尾」的分布说明打乱并未把统计量变成干净的零分布。
原因可能：打乱破坏了开奖的「期序」但 sum_range 的 selector 是
「近 W 期和值落在 [lo,hi] 的历史期之后，哪些号最常开」——
打乱后和值区间匹配的样本量骤减，多数时候 selector 返回空集/近空集，
导致 exp 极小、命中 0，z 序列退化。于是分布是「大量 0 + 少数偶然大」。
"""
import math
import numpy as np

import w_common as C
from lottery import discovery

def probe():
    d = C.draws(); n = len(d)
    flt = discovery.make_filter("sum_range", {"window": 30, "lo": 45, "hi": 75})
    # 真实 K 分布
    r = discovery.asof_scan(d, flt, start=300, end=n)
    ks = [s["k"] for s in r["hit_series"]]
    print(f"【真实】selector 选号数 k：mean={np.mean(ks):.2f} min={min(ks)} max={max(ks)}")
    print(f"  k==0 的期数占比：{np.mean([k==0 for k in ks]):.3f}")

    rng = np.random.default_rng(0)
    idx = np.arange(n)
    for trial in range(3):
        perm = idx.copy(); rng.shuffle(perm)
        ds = [d[i] for i in perm]
        rr = discovery.asof_scan(ds, flt, start=300, end=n)
        ks2 = [s["k"] for s in rr["hit_series"]]
        print(f"  打乱#{trial}: k mean={np.mean(ks2):.2f} min={min(ks2)} max={max(ks2)} "
              f"k==0占比={np.mean([k==0 for k in ks2]):.3f}")

if __name__ == "__main__":
    probe()
    print("\n【结论判定】若打乱后 k==0 占比远高于真实，则打乱检验不构成有效零分布，")
    print("p 值不可信 —— 必须改用「保留期序、只置换选择集」的对照。")
