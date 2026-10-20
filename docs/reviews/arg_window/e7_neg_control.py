"""E7：E6 检验2 的负相关 r=-0.11 是真反转还是噪声？—— 纯随机开奖对照。

在「33 号每期随机 6 个」的纯随机世界里，重复 E6 检验2 的整体流程，
看 r 的零分布。若真实数据的 r=-0.11 落在零分布中间，则「反转」不可信。
"""
import math
import numpy as np

SD = 0.8678
N_RED, PICK = 33, 6
BLOCKS = 6
W = 500
N_CAND = 60          # 用 60 个 mock 候选（随机 top-k 选择集），足以估 r 的零分布


def sim_once(rng):
    T = 300 + BLOCKS * W
    draws = [set(rng.choice(N_RED, PICK, replace=False).tolist()) for _ in range(T)]
    # mock 候选：每期随机选 5~8 个号（模拟不同 k 的筛选集），计算命中差
    M = np.zeros((N_CAND, BLOCKS))
    for c in range(N_CAND):
        k = 5 + (c % 4)
        hits = []
        for t in range(T):
            sel = rng.choice(N_RED, k, replace=False)
            hits.append(len(draws[t] & set(sel)) - 6 * k / 33)
        hits = np.array(hits)
        for j in range(BLOCKS):
            a = 300 + j * W
            M[c, j] = hits[a:a + W].mean() / SD
    xs, ys = M[:, :-1].ravel(), M[:, 1:].ravel()
    return float(np.corrcoef(xs, ys)[0, 1])


if __name__ == "__main__":
    rng = np.random.default_rng(42)
    rs = np.array([sim_once(rng) for _ in range(120)])
    print(f"纯随机开奖下，相邻块 mean z 相关 r 的零分布（{len(rs)} 次仿真）：")
    print(f"  mean={rs.mean():+.4f}  sd={rs.std(ddof=1):.4f}  "
          f"2.5%={np.quantile(rs,.025):+.4f}  97.5%={np.quantile(rs,.975):+.4f}")
    real = -0.1088
    inside = (np.quantile(rs, .025) <= real <= np.quantile(rs, .975))
    print(f"  真实数据 r = {real:+.4f} → {'落在零分布 95% 内（=噪声）' if inside else '超出零分布（=异常）'}")
    print(f"  真实 r 的等效双侧 p ≈ {2*min((rs<=real).mean(), (rs>=real).mean()):.3f}")
    print("\n注：E6 的 95% 容差 ±0.0519 是**独立样本**口径；相邻块本身不独立（同块候选相关），")
    print("    因此该容差低估了 r 的零分布宽度 —— 这正是必须做本仿真的理由。")
