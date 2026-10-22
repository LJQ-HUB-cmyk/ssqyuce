"""V15 假设空间扩展——双侧验收脚本（注入必检出 + 纯随机不误报）。

对应方案 docs/analysis/V15_ASSUMPTION_SPACE.md，三条改动 S1 / S2 / S7。

## 为什么必须有「两个方向」

这是本项目的验收传统（见 docs/analysis/P2_STATS_WHY_ZERO.md）：
- **纯随机不误报**：喂纯随机开奖，`persistent_effect` 的误判率必须贴住名义 α。
  零假设下（长期档 BH/Bonferroni 后 p<0.05），单假设 Type-I ≤ α，
  族 FWER 在 Bonferroni(m=n_tested) 口径下也 ≤ α=0.05。实测明显超出 = 实现 bug。
- **注入必检出**：向数据注入已知效应，看能否检出。检不出不是失败，检成「不该检出的
  也检出（假阳）」才是失败。

## 用法

    .venv/bin/python qa-tools/v15_acceptance.py --quick   # 快速版（默认，几分钟内）
    .venv/bin/python qa-tools/v15_acceptance.py --full    # 完整版（200 次重复）

## 注入口径（先写死再跑，预注册精神，跑完不改窗口/箱数凑显著）

- ``null``         ：均匀独立开奖（order 为红球的随机排列）。
- ``number_10pct`` ：6 个固定号各 +10% 抽中权重（P2「6 号各 +10%」最强现实效应）。
- ``order_35pct``  ：第 1 位开奖号对「号码 1..11」的抽中权重 ×1.35（P2 order_35pct 场景）。

随机种子全部固定，见各 CASE 的 seed 字段。
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from typing import Dict, List, Optional, Sequence, Tuple

from lottery import discovery, regime

# ---- 固定的注入参数（预注册：先写死，跑完不许回头调）----
NUMBER_BIAS_TARGET = (3, 9, 15, 21, 27, 33)   # 6 个被注入 +10% 的号
NUMBER_BIAS_MULT = 1.10
ORDER_BIAS_POS = 0                            # 被注入位置（第 1 位）
ORDER_BIAS_NUMS = tuple(range(1, 12))         # 该位置偏好的号码集合
ORDER_BIAS_MULT = 1.35

# discover 快速窗口：验收用小窗口控成本；结论对窗口不敏感（窗口只影响快速筛选阶段）
FAST_WINDOW = 300
MIN_WINDOW_START = 200
N_DRAWS = 550


def _weighted_reds(rng: random.Random, weights: Dict[int, float]) -> List[int]:
    """不放回加权抽样 6 个红球。weights 缺省号权重 1.0。"""
    pool = list(range(1, 34))
    wts = [weights.get(n, 1.0) for n in pool]
    chosen: List[int] = []
    for _ in range(6):
        tot = sum(wts)
        r = rng.random() * tot
        acc = 0.0
        pick_i = len(pool) - 1
        for i, w in enumerate(wts):
            acc += w
            if r <= acc:
                pick_i = i
                break
        chosen.append(pool.pop(pick_i))
        wts.pop(pick_i)
    return sorted(chosen)


def make_case(mode: str, seed: int, n: int = N_DRAWS) -> List[Dict]:
    """按注入模式生成合成开奖序列。order 一律是红球的随机排列（真实开奖顺序）。"""
    rng = random.Random(seed)
    orng = random.Random(seed ^ 0x9E3779B9)
    weights: Dict[int, float] = {}
    if mode == "number_10pct":
        weights = {num: NUMBER_BIAS_MULT for num in NUMBER_BIAS_TARGET}
    draws: List[Dict] = []
    for i in range(n):
        if weights:
            reds = _weighted_reds(rng, weights)
        else:
            reds = sorted(rng.sample(range(1, 34), 6))
        order = list(reds)
        orng.shuffle(order)
        if mode == "order_35pct":
            # 第 ORDER_BIAS_POS 位：若当前号不在偏好集合，按 1.35 倍权重从中重抽一个
            # 未被其他位占用的偏好号（概率性替换，模拟 +35% 位置偏置）。
            others = set(order)
            others.discard(order[ORDER_BIAS_POS])
            cand = [x for x in ORDER_BIAS_NUMS if x not in others]
            if cand and orng.random() < (ORDER_BIAS_MULT - 1.0):
                order[ORDER_BIAS_POS] = orng.choice(cand)
        draws.append({
            "issue": f"{2013000 + i}",
            "date": None,
            "reds": reds,
            "order": order,
            "blue": rng.randint(1, 16),
        })
    return draws


def _status_of(result: Dict, key: str) -> Optional[str]:
    """在 all_quick 里找某族的最强一条，返回其阶段状态（若有画像）。"""
    best = None
    for it in result.get("all_quick", []):
        if it["filter"]["key"] != key:
            continue
        if best is None or abs(it["quick"]["z"]) > abs(best["quick"]["z"]):
            best = it
    return best.get("status") if best else None


def _best_z(result: Dict, key: str) -> float:
    z = 0.0
    for it in result.get("all_quick", []):
        if it["filter"]["key"] == key:
            z = max(z, abs(it["quick"]["z"]))
    return z


def _count_status(result: Dict, status: str) -> int:
    return sum(1 for it in result.get("items", []) if it.get("status") == status)


# --------------------------------------------------------------------------
# 方向一：纯随机不误报（persistent_effect 族 FWER）
# --------------------------------------------------------------------------

def run_null(reps: int) -> Dict:
    """纯随机：统计每次扫描后 persistent_effect / persistent 的误判。

    只看阶段画像（``items``）里出现的档位 —— 这正是会进「规律列表」的候选。
    单次「出现 ≥1 条 persistent_effect」即算一次族级误报。
    """
    fe_hits = 0
    pe_hits = 0
    max_pe = 0
    for s in range(reps):
        draws = make_case("null", seed=10000 + s)
        res = discovery.discover(draws, top=20, fast_window=FAST_WINDOW,
                                 min_window_start=MIN_WINDOW_START, use_cache=False)
        n_effect = _count_status(res, "persistent_effect")
        n_pers = _count_status(res, "persistent")
        if n_effect > 0:
            fe_hits += 1
            max_pe = max(max_pe, n_effect)
        if n_pers > 0:
            pe_hits += 1
    return {
        "reps": reps,
        "persistent_effect_any_rate": fe_hits / reps,
        "persistent_any_rate": pe_hits / reps,
        "max_persistent_effect_count": max_pe,
        "theory_fwer_upper": 0.05,
    }


# --------------------------------------------------------------------------
# 方向二：注入必检出
# --------------------------------------------------------------------------

def run_inject(mode: str, key: str, reps: int, hit_status=("persistent", "persistent_effect"),
               zin=3.0) -> Dict:
    """注入：统计目标族被检出（阶段状态命中或 |z|≥zin）的比例。"""
    det = 0
    zs: List[float] = []
    for s in range(reps):
        draws = make_case(mode, seed=20000 + s)
        res = discovery.discover(draws, top=20, fast_window=FAST_WINDOW,
                                 min_window_start=MIN_WINDOW_START, use_cache=False)
        z = _best_z(res, key)
        zs.append(z)
        st = _status_of(res, key)
        if (st in hit_status) or (z >= zin):
            det += 1
    return {
        "reps": reps,
        "detect_rate": det / reps,
        "mean_best_z": sum(zs) / len(zs) if zs else 0.0,
        "max_best_z": max(zs) if zs else 0.0,
    }


# --------------------------------------------------------------------------
# S1：候选稀释修复前后，注入族能否进入阶段画像（top-N）
# --------------------------------------------------------------------------

def run_s1_dilution(reps: int) -> Dict:
    """S1：注入「6 号各 +10%」后，检查注入族（number_bias）与更强族的条目是否被
    同族参数档挤占。指标：每次运行 items(top=20) 里出现的**不同族数**。

    去重前（旧实现）多档同族会挤占名额 → 不同族数少；去重后 = min(top, 族数)。
    """
    distinct_old = 0
    distinct_new = 0
    for s in range(reps):
        draws = make_case("number_10pct", seed=30000 + s)
        # 新实现（当前代码）：items 已按族去重
        res = discovery.discover(draws, top=20, fast_window=FAST_WINDOW,
                                 min_window_start=MIN_WINDOW_START, use_cache=False)
        k_new = len({it["filter"]["key"] for it in res["items"]})
        distinct_new += k_new
        # 模拟旧实现：对全部 records（all_quick）按 |z| 排序取前 20，不去重
        old_top = sorted(res["all_quick"], key=lambda r: -abs(r["quick"]["z"]))[:20]
        k_old = len({it["filter"]["key"] for it in old_top})
        distinct_old += k_old
    return {
        "reps": reps,
        "avg_distinct_families_old_no_dedup": distinct_old / reps,
        "avg_distinct_families_new_dedup": distinct_new / reps,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="完整版：200 次重复")
    ap.add_argument("--quick", action="store_true", help="快速版（默认）")
    ap.add_argument("--json", default=None, help="把结果写入该 JSON 文件")
    args = ap.parse_args()

    reps_null = 200 if args.full else 20
    reps_inj = 60 if args.full else 12
    reps_s1 = 40 if args.full else 10
    report: Dict = {"mode": "full" if args.full else "quick",
                    "reps_null": reps_null, "reps_inject": reps_inj, "reps_s1": reps_s1}
    t0 = time.time()

    print(f"[V15 验收] 模式={'full' if args.full else 'quick'} "
          f"null_reps={reps_null} inject_reps={reps_inj} s1_reps={reps_s1}", flush=True)
    print("== 方向一：纯随机不误报 ==", flush=True)
    report["null"] = run_null(reps_null)
    print(json.dumps(report["null"], ensure_ascii=False), flush=True)

    print("== 方向二：注入必检出 ==", flush=True)
    report["inject_number_10pct"] = run_inject("number_10pct", "number_bias", reps_inj)
    print("number_10pct ->", json.dumps(report["inject_number_10pct"], ensure_ascii=False), flush=True)
    report["inject_order_35pct"] = run_inject("order_35pct", "order_pos", reps_inj)
    print("order_35pct ->", json.dumps(report["inject_order_35pct"], ensure_ascii=False), flush=True)

    print("== S1：候选稀释 ==", flush=True)
    report["s1_dilution"] = run_s1_dilution(reps_s1)
    print(json.dumps(report["s1_dilution"], ensure_ascii=False), flush=True)

    report["elapsed_sec"] = round(time.time() - t0, 1)
    print(f"[完成] 用时 {report['elapsed_sec']}s", flush=True)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"[已写入] {args.json}", flush=True)


if __name__ == "__main__":
    main()
