"""规律库：把"规律"定义为 触发条件 + 候选动作（应选/应避号码集合）。

所有规律只用目标期之前的历史（walk-forward，禁止未来信息），
由 backtest.py 在历史上滚动验证并做显著性分级。
"""
from __future__ import annotations

from typing import Callable, Dict, List

import numpy as np

from . import features as F

REDS, BLUE = 33, 16


# ---------- 工具 ----------

def _comb_p(n, k):
    from math import comb
    return comb(n, k)


def red_base(k: int):
    """红球：k 个号码在单注 6 红中至少出现 1 个的理论概率与期望命中数。"""
    p0 = 1 - _comb_p(REDS - k, 6) / _comb_p(REDS, 6) if k <= REDS else 0.0
    return {"p0": p0, "exp": 6 * k / REDS}


def blue_base(k: int):
    """蓝球：k 个号码在单期 1 蓝中命中的理论概率。"""
    return {"p0": k / BLUE, "exp": k / BLUE}


# ---------- 规律定义 ----------

def _t_always(history: List[Dict]) -> bool:
    return True


def _act_blue_cold_back(history: List[Dict]) -> Dict:
    om = F.current_omission_blue(history)
    mask = np.where(om[1:] >= 20)[0] + 1
    if len(mask) == 0:
        return {}
    order = mask[np.argsort(-om[mask])]
    return {"blue_fav": [int(x) for x in order[:1]]}


def _act_red_freq_reversion(history: List[Dict]) -> Dict:
    freq = F.red_frequency(history)
    n = len(history)
    exp = n * 6 / REDS
    dev = (freq[1:] - exp) / np.sqrt(exp) if exp > 0 else np.zeros(REDS)
    mask = np.where(dev <= -2.0)[0] + 1
    if len(mask) == 0:
        return {}
    return {"red_fav": [int(x) for x in mask]}


def _act_red_omission_pressure(history: List[Dict]) -> Dict:
    om = F.current_omission_red(history)
    avg = F.avg_omission(F.red_frequency(history), len(history))
    ratio = np.divide(om[1:], avg[1:], out=np.ones(REDS), where=avg[1:] > 0)
    mask = np.where((om[1:] >= 25) & (ratio >= 1.5))[0] + 1
    if len(mask) == 0:
        return {}
    return {"red_fav": [int(x) for x in mask]}


def _act_zone_low_rebound(history: List[Dict]) -> Dict:
    sl = history[-6:] if len(history) >= 6 else history
    zc = F.zone_counts(sl)
    totals = [0, 0, 0]
    for z in zc:
        for i in range(3):
            totals[i] += z[i]
    low = int(np.argmin(totals))
    lo, hi = {0: (1, 11), 1: (12, 22), 2: (23, 33)}[low]
    return {"red_fav": list(range(lo, hi + 1))}


def _act_parity_rebound(history: List[Dict]) -> Dict:
    if not history:
        return {}
    odd = sum(1 for r in history[-1]["reds"] if r % 2 == 1)
    if odd >= 4:
        return {"red_fav": [x for x in range(1, REDS + 1) if x % 2 == 0]}
    if odd <= 2:
        return {"red_fav": [x for x in range(1, REDS + 1) if x % 2 == 1]}
    return {}


def _act_repeat(history: List[Dict]) -> Dict:
    if not history:
        return {}
    return {"red_fav": history[-1]["reds"], "blue_fav": [history[-1]["blue"]]}


def _act_adjacent(history: List[Dict]) -> Dict:
    if not history:
        return {}
    last = history[-1]["reds"]
    fav = set()
    for r in last:
        if r - 1 >= 1:
            fav.add(r - 1)
        if r + 1 <= REDS:
            fav.add(r + 1)
    bfav = set()
    b = history[-1]["blue"]
    if b - 1 >= 1:
        bfav.add(b - 1)
    if b + 1 <= BLUE:
        bfav.add(b + 1)
    return {"red_fav": sorted(fav), "blue_fav": sorted(bfav)}


def _act_recent_hot(history: List[Dict]) -> Dict:
    sl = history[-30:] if len(history) >= 30 else history
    freq = F.red_frequency(sl)
    top = np.argsort(freq[1:])[-6:][::-1] + 1
    bfreq = F.blue_frequency(sl)
    btop = np.argsort(bfreq[1:])[-2:][::-1] + 1
    return {"red_fav": [int(x) for x in top], "blue_fav": [int(x) for x in btop]}


def _act_recent_cold(history: List[Dict]) -> Dict:
    sl = history[-30:] if len(history) >= 30 else history
    freq = F.red_frequency(sl)
    bot = np.argsort(freq[1:])[:6] + 1
    return {"red_fav": [int(x) for x in bot]}


def _t_sum_extreme(history: List[Dict]) -> bool:
    if len(history) < 200:
        return len(history) >= 30
    s = F.sums(history)
    p85, p15 = np.percentile(s, 85), np.percentile(s, 15)
    return s[-1] >= p85 or s[-1] <= p15


PATTERNS: List[Dict] = [
    {
        "key": "blue_cold_back",
        "name_zh": "蓝球冷号回补",
        "kind": "long",
        "desc": "蓝球遗漏 ≥20 期后，下一期（或数期内）出现概率是否高于均匀基线 1/16",
        "horizon": 1,
        "trigger_fn": _t_always,
        "action_fn": _act_blue_cold_back,
        "outcome": "blue_fav",
        "base_fn": blue_base,
    },
    {
        "key": "red_freq_reversion",
        "name_zh": "红球频率均值回归",
        "kind": "long",
        "desc": "长期频率显著低于期望(z≤-2)的红球，下一期是否补出",
        "horizon": 1,
        "trigger_fn": _t_always,
        "action_fn": _act_red_freq_reversion,
        "outcome": "red_fav",
        "base_fn": red_base,
    },
    {
        "key": "red_omission_pressure",
        "name_zh": "红球遗漏压力",
        "kind": "long",
        "desc": "遗漏≥25 且超过自身平均遗漏 1.5 倍的红球，下一期是否回补",
        "horizon": 1,
        "trigger_fn": _t_always,
        "action_fn": _act_red_omission_pressure,
        "outcome": "red_fav",
        "base_fn": red_base,
    },
    {
        "key": "zone_low_rebound",
        "name_zh": "低活跃区回补（中期轮动）",
        "kind": "mid",
        "desc": "近6期三区合计最少的区间，其 11 个号码下一期命中是否高于随机",
        "horizon": 1,
        "trigger_fn": _t_always,
        "action_fn": _act_zone_low_rebound,
        "outcome": "red_fav",
        "base_fn": red_base,
    },
    {
        "key": "parity_rebound",
        "name_zh": "奇偶比例回归",
        "kind": "mid",
        "desc": "上期奇数≥4 选偶数、上期奇数≤2 选奇数，验证奇偶是否轮动",
        "horizon": 1,
        "trigger_fn": _t_always,
        "action_fn": _act_parity_rebound,
        "outcome": "red_fav",
        "base_fn": red_base,
    },
    {
        "key": "sum_regression",
        "name_zh": "和值回归",
        "kind": "mid",
        "desc": "上期和值处于历史极端(>P85 或 <P15)时，下一期和值是否更接近均值",
        "horizon": 1,
        "trigger_fn": _t_sum_extreme,
        "action_fn": lambda h: {},
        "outcome": "numeric_sum",
        "base_fn": None,
    },
    {
        "key": "repeat_trend",
        "name_zh": "重号延续",
        "kind": "short",
        "desc": "上期 6 个红球在下一期复出数量是否高于随机期望 6*6/33≈1.09",
        "horizon": 1,
        "trigger_fn": _t_always,
        "action_fn": _act_repeat,
        "outcome": "red_fav",
        "base_fn": red_base,
    },
    {
        "key": "adjacent_trend",
        "name_zh": "邻号延续",
        "kind": "short",
        "desc": "上期红球±1 邻号、蓝球±1 邻号在下一期命中是否高于随机",
        "horizon": 1,
        "trigger_fn": _t_always,
        "action_fn": _act_adjacent,
        "outcome": "red_fav",
        "base_fn": red_base,
    },
    {
        "key": "recent_hot",
        "name_zh": "短期热号",
        "kind": "short",
        "desc": "近30期最热的 6 个红球（+2 个蓝球）下一期是否延续热度",
        "horizon": 1,
        "trigger_fn": _t_always,
        "action_fn": _act_recent_hot,
        "outcome": "red_fav",
        "base_fn": red_base,
    },
    {
        "key": "recent_cold",
        "name_zh": "短期冷号",
        "kind": "short",
        "desc": "近30期最冷的 6 个红球下一期是否反弹",
        "horizon": 1,
        "trigger_fn": _t_always,
        "action_fn": _act_recent_cold,
        "outcome": "red_fav",
        "base_fn": red_base,
    },
    {
        "key": "blue_repeat",
        "name_zh": "蓝球延续",
        "kind": "short",
        "desc": "上期蓝球下一期重复命中率 vs 1/16",
        "horizon": 1,
        "trigger_fn": _t_always,
        "action_fn": lambda h: {"blue_fav": [h[-1]["blue"]]} if h else {},
        "outcome": "blue_fav",
        "base_fn": blue_base,
    },
]


def run_pattern(pattern: Dict, history: List[Dict]) -> Dict:
    """在给定历史上执行规律，返回触发状态与动作。"""
    out = {"triggered": bool(pattern["trigger_fn"](history)), "action": {}}
    if out["triggered"]:
        out["action"] = pattern["action_fn"](history) or {}
    return out


def active_pattern_keys(history_len: int) -> List[str]:
    """各窗口所需最少历史期数（大致），供提示词与预测使用。"""
    return [p["key"] for p in PATTERNS]