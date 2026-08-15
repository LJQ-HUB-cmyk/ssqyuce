"""特征工程：长/中/短期多尺度统计指标（纯 numpy，无重依赖）。"""
from __future__ import annotations

from typing import Dict, List

import numpy as np

from . import config

R_MAX, B_MAX = 33, 16


# ---------- 基础统计 ----------

def red_frequency(draws: List[Dict]) -> np.ndarray:
    """返回形状 (34,) 数组，index 1..33 为各号码出现次数。"""
    f = np.zeros(R_MAX + 1, dtype=float)
    for d in draws:
        for r in d["reds"]:
            f[r] += 1
    return f


def blue_frequency(draws: List[Dict]) -> np.ndarray:
    f = np.zeros(B_MAX + 1, dtype=float)
    for d in draws:
        f[d["blue"]] += 1
    return f


def current_omission_red(draws: List[Dict]) -> np.ndarray:
    """每个红球距最近一次出现的期数（从未出现则为 len(draws)）。"""
    om = np.full(R_MAX + 1, len(draws), dtype=float)
    for idx, d in enumerate(reversed(draws)):
        for r in d["reds"]:
            if om[r] == len(draws):
                om[r] = idx
    return om


def current_omission_blue(draws: List[Dict]) -> np.ndarray:
    om = np.full(B_MAX + 1, len(draws), dtype=float)
    for idx, d in enumerate(reversed(draws)):
        if om[d["blue"]] == len(draws):
            om[d["blue"]] = idx
    return om


def avg_omission(freq: np.ndarray, n_draws: int) -> np.ndarray:
    cnt = freq.astype(float)
    cnt[cnt == 0] = 0.5  # 避免除零
    return n_draws / cnt


def sums(draws: List[Dict]) -> np.ndarray:
    return np.array([sum(d["reds"]) for d in draws], dtype=float)


def zone_counts(draws: List[Dict]) -> List[tuple]:
    out = []
    for d in draws:
        z1 = sum(1 for r in d["reds"] if 1 <= r <= 11)
        z2 = sum(1 for r in d["reds"] if 12 <= r <= 22)
        z3 = 6 - z1 - z2
        out.append((z1, z2, z3))
    return out


def odd_counts(draws: List[Dict]) -> np.ndarray:
    return np.array([sum(1 for r in d["reds"] if r % 2 == 1) for d in draws], dtype=float)


def consecutive_rate(draws: List[Dict]) -> float:
    if not draws:
        return 0.0
    hit = 0
    for d in draws:
        rs = sorted(d["reds"])
        if any(b - a == 1 for a, b in zip(rs, rs[1:])):
            hit += 1
    return hit / len(draws)


def same_tail_rate(draws: List[Dict]) -> float:
    if not draws:
        return 0.0
    hit = 0
    for d in draws:
        tails = sorted(r % 10 for r in d["reds"])
        if len(set(tails)) < 6:
            hit += 1
    return hit / len(draws)


def repeat_counts(draws: List[Dict]) -> np.ndarray:
    """相邻两期红球重叠个数序列（须按时间顺序传入）。"""
    out = []
    for prev, cur in zip(draws, draws[1:]):
        out.append(len(set(prev["reds"]) & set(cur["reds"])))
    return np.array(out, dtype=float)


def blue_repeat_rate(draws: List[Dict]) -> float:
    if len(draws) < 2:
        return 0.0
    hit = sum(1 for a, b in zip(draws, draws[1:]) if a["blue"] == b["blue"])
    return hit / (len(draws) - 1)


def ac_value(reds: List[int]) -> int:
    rs = sorted(reds)
    diffs = {abs(a - b) for i, a in enumerate(rs) for b in rs[i + 1:]}
    return len(diffs) - (len(rs) - 1)


# ---------- 汇总 ----------

def window_slice(draws: List[Dict], window: int):
    if window <= 0 or window >= len(draws):
        return draws
    return draws[-window:]


def red_stats(draws: List[Dict]) -> Dict:
    freq = red_frequency(draws)
    n = len(draws)
    om_cur = current_omission_red(draws)
    om_avg = avg_omission(freq, n)
    s = sums(draws)
    zc = zone_counts(draws)
    oc = odd_counts(draws)
    zc_hist = {}
    for z in zc:
        zc_hist[z] = zc_hist.get(z, 0) + 1
    rc = repeat_counts(draws)
    return {
        "n_draws": n,
        "freq": freq[1:].tolist(),
        "omission_current": om_cur[1:].tolist(),
        "omission_avg": om_avg[1:].tolist(),
        "sum_mean": float(np.mean(s)) if len(s) else 0,
        "sum_std": float(np.std(s)) if len(s) else 0,
        "sum_pct": {p: float(np.percentile(s, p)) if len(s) else 0 for p in (5, 25, 50, 75, 95)},
        "zone_hist": {f"{k[0]}-{k[1]}-{k[2]}": v for k, v in sorted(zc_hist.items(), key=lambda x: -x[1])},
        "odd_mean": float(np.mean(oc)) if len(oc) else 0,
        "odd_hist": {int(k): int(v) for k, v in zip(*np.unique(oc, return_counts=True))} if len(oc) else {},
        "consecutive_rate": consecutive_rate(draws),
        "same_tail_rate": same_tail_rate(draws),
        "repeat_mean": float(np.mean(rc)) if len(rc) else 0,
        "ac_mean": float(np.mean([ac_value(d["reds"]) for d in draws])) if draws else 0,
        "hot_top6": [int(x) for x in np.argsort(freq[1:])[-6:][::-1] + 1],
        "cold_top6": [int(x) for x in np.argsort(freq[1:])[:6] + 1],
        "omit_top6": [int(x) for x in np.argsort(om_cur[1:])[-6:][::-1] + 1],
    }


def blue_stats(draws: List[Dict]) -> Dict:
    freq = blue_frequency(draws)
    n = len(draws)
    om_cur = current_omission_blue(draws)
    om_avg = avg_omission(freq, n)
    return {
        "n_draws": n,
        "freq": freq[1:].tolist(),
        "omission_current": om_cur[1:].tolist(),
        "omission_avg": om_avg[1:].tolist(),
        "hot_top3": [int(x) for x in np.argsort(freq[1:])[-3:][::-1] + 1],
        "omit_top3": [int(x) for x in np.argsort(om_cur[1:])[-3:][::-1] + 1],
        "repeat_rate": blue_repeat_rate(draws),
    }


def compute_features(draws: List[Dict]) -> Dict:
    """基于完整历史（时间升序）计算各窗口统计；末尾为最新一期。"""
    out = {
        "issue": draws[-1]["issue"],
        "date": draws[-1]["date"],
        "last_reds": draws[-1]["reds"],
        "last_blue": draws[-1]["blue"],
        "windows": {},
    }
    for name, win in config.WINDOWS.items():
        sl = window_slice(draws, win)
        out["windows"][name] = {
            "red": red_stats(sl),
            "blue": blue_stats(sl),
        }
    out["recent"] = [
        {"issue": d["issue"], "reds": d["reds"], "blue": d["blue"], "sum": sum(d["reds"])}
        for d in draws[-20:]
    ]
    return out