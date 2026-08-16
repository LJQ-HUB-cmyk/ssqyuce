"""号码诊断：输入自选号码，给出结构画像 + 历史相似注命中分布。"""
from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np

from . import features as F


def structure_profile(reds: List[int], blue: int, draws: List[Dict]) -> Dict:
    """计算号码的结构画像（和值/奇偶/三区/跨度/AC/热冷等）。"""
    rs = sorted(reds)
    n = len(draws)
    s = sum(reds)
    odd = sum(1 for r in reds if r % 2 == 1)
    z1 = sum(1 for r in reds if 1 <= r <= 11)
    z2 = sum(1 for r in reds if 12 <= r <= 22)
    z3 = 6 - z1 - z2
    span = rs[-1] - rs[0]
    ac = F.ac_value(reds)
    consec = any(b - a == 1 for a, b in zip(rs, rs[1:]))
    same_tail = len(set(r % 10 for r in reds)) < 6

    freq = F.red_frequency(draws) if draws else np.zeros(34)
    om_cur = F.current_omission_red(draws) if draws else np.zeros(34)
    om_avg = F.avg_omission(freq, n) if draws else np.zeros(34)

    hot = sum(1 for r in reds if freq[r] > n * 6 / 33 * 1.2)
    cold = sum(1 for r in reds if freq[r] < n * 6 / 33 * 0.8)
    omit_top = sorted([(r, int(om_cur[r]), float(om_avg[r])) for r in reds], key=lambda x: -x[1])

    return {
        "reds": rs,
        "blue": blue,
        "sum": s,
        "sum_pct_low": float(np.percentile(F.sums(draws), 15)) if draws else 0,
        "sum_pct_high": float(np.percentile(F.sums(draws), 85)) if draws else 0,
        "sum_in_range": (float(np.percentile(F.sums(draws), 15)) <= s <= float(np.percentile(F.sums(draws), 85))) if draws else False,
        "odd_count": odd,
        "even_count": 6 - odd,
        "zone_counts": [z1, z2, z3],
        "span": span,
        "ac": ac,
        "has_consecutive": consec,
        "has_same_tail": same_tail,
        "hot_count": hot,
        "cold_count": cold,
        "omit_detail": [{"num": r, "omit_cur": oc, "omit_avg": round(oa, 1)} for r, oc, oa in omit_top],
        "prime_count": sum(1 for r in reds if r in {2,3,5,7,11,13,17,19,23,29,31}),
        "size_count_small": sum(1 for r in reds if 1 <= r <= 16),
        "route_0_count": sum(1 for r in reds if r % 3 == 0),
    }


def find_similar_draws(
    draws: List[Dict],
    reds: List[int],
    blue: int,
    n_similar: int = 20,
    max_diff: int = 4,
) -> List[Dict]:
    """在历史中找出结构相似的注：汉明距离（红球差异数）<= max_diff。"""
    rs_set = set(reds)
    scored = []
    for d in draws:
        drs_set = set(d["reds"])
        diff = len(rs_set ^ drs_set)  # 对称差 = 需要替换的号码数
        blue_match = 1 if d["blue"] == blue else 0
        scored.append((diff, blue_match, d))
    scored.sort(key=lambda x: (x[0], -x[1]))
    results = []
    for diff, bm, d in scored:
        if diff > max_diff:
            continue
        if len(results) >= n_similar:
            break
        r = len(rs_set & set(d["reds"]))
        b = 1 if blue == d["blue"] else 0
        results.append({
            "issue": d["issue"],
            "date": d["date"],
            "reds": d["reds"],
            "blue": d["blue"],
            "red_hit": r,
            "blue_hit": bool(b),
            "similarity_diff": diff,
        })
    return results


def diagnose(
    reds: List[int], blue: int, draws: List[Dict]
) -> Dict:
    """完整的号码诊断报告。"""
    if len(set(reds)) != 6 or not all(1 <= r <= 33 for r in reds):
        return {"error": "红球必须是 6 个 1-33 的不重复整数"}
    if not (1 <= blue <= 16):
        return {"error": "蓝球必须是 1-16 的整数"}

    profile = structure_profile(reds, blue, draws)
    similar = find_similar_draws(draws, reds, blue)

    # 统计相似注的命中分布
    red_hits_dist = {}
    blue_hit_count = 0
    for s in similar:
        rh = s["red_hit"]
        red_hits_dist[rh] = red_hits_dist.get(rh, 0) + 1
        if s["blue_hit"]:
            blue_hit_count += 1

    return {
        "profile": profile,
        "similar_count": len(similar),
        "similar_red_hits_dist": red_hits_dist,
        "similar_blue_hit_rate": round(blue_hit_count / max(1, len(similar)), 3),
        "similar_draws": similar[:10],
        "note": "相似注命中分布仅供参考，不构成中奖预测。",
    }
