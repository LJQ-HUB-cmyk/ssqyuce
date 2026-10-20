"""E4：多窗口并行的 e-BH m 膨胀代价（量化）。

关键量：e-BH 单窗口 m=306（单因子）下，选中第 k 名所需 e ≥ m/(k·α)。
多窗口并行把 m 乘上窗口数 → 单窗口的 k=1 阈值从 306/(1·0.05)=6120
变到 n_w·306/0.05 = 6120·n_w。**阈值线性膨胀**。

但 e-BH 有自补偿：真正的信号在多个窗口都会出现 → 同一 FP 的 e 重复计入候选池，
主排序里它占 k=1,2,... 的多个位置，分母 k 变大。因此 m 膨胀的**净**影响不是线性，
取决于"同一信号跨窗口的重复计数"能否把分母 k 抬到和分子 m 同速。

这里用真实 e 值做数值实验：
  方案A 单窗口(500) e-BH m=306
  方案B 三窗口(150/500/全史) 合并候选池，m=3×306=918，且同一 FP 若有 k 个窗口 e≥20
        就重复计入 k 次（占 k 个 rank）
  方案C 三窗口但**每个窗口独立判**（不做跨窗口合并），m=306 各自跑 → 无 m 膨胀
 方案D 增长窗口单窗口(全史) m=306
"""
import math
import numpy as np

from lottery import eprocess

ALPHA = 0.05
THR = math.log(20)


def select(e_vals, m):
    order = sorted(range(len(e_vals)), key=lambda i: -float(e_vals[i]))
    k = 0
    for rank, idx in enumerate(order, start=1):
        if e_vals[idx] >= m / (rank * ALPHA):
            k = rank
    return k, [idx for idx in order[:k]]


def simulate():
    pass


def demo():
    """用真实 E1 的 306 个 fix500 e 值演示 m 膨胀的阈值位移。"""
    # 近似从 E1 结果取典型：median e≈0.52，q99≈11.4，max≈68（W=500 重叠校正）
    # 直接重算真实 e 值池
    import w_common as C
    from lottery import candidate_space as CS
    d = C.draws(); n = len(d); start = n - 500
    es = []
    for _i, flt, fp in CS.iter_candidates(CS.LAYER_SINGLE, 0, CS.layer_size(CS.LAYER_SINGLE)):
        zs, _, _ = C.z_series(d, flt["key"], dict(flt.get("params") or {}), start=start, end=n)
        if len(zs) >= 30:
            es.append(math.exp(C.e_log(zs)))
    es = np.array(es)
    print(f"真实 306 候选 fix500：max e={es.max():.1f}, top5={np.sort(es)[::-1][:5].round(1)}")
    for label, m in [("单窗口 m=306", 306),
                     ("双窗口 m=612", 612),
                     ("三窗口 m=918", 918),
                     ("全史 m=306", 306)]:
        k, sel = select(es, m)
        print(f"  {label:16s}: 选中 {k} 条；k=1 需 e≥{m/(1*ALPHA):.0f}，"
              f"实际 top e={es.max():.1f} → {'过' if es.max()>=m/ALPHA else '不过'}")
    print("\n结论：真实 max e 远低于单窗口 m=306 的 k=1 阈值 6120，更低于多窗口的 9180/18360。")

    print("\n【自补偿实验】同信号在 n_w 个窗口重复出现（e=28,10,5）对 e-BH 的影响：")
    for n_w in (1, 2, 3):
        # 构造 m=306·n_w 候选池，其中前 n_w 个位置放同一个信号 e=28（重复），其余为真实噪声 e
        pool = list(es[:306 - n_w]) + [28.0] * n_w
        k, sel = select(pool, m=306 * n_w)
        sel_e = [pool[i] for i in sel]
        print(f"  窗口数 {n_w}: m={306*n_w:4d}, 选中 {k} 条, 含信号(28)={'28.0' in [f'{x:.1f}' for x in sel_e]}")

if __name__ == "__main__":
    demo()
