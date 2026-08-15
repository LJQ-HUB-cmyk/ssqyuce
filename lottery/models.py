"""统计基线模型：频率加权、遗漏回补、马尔可夫转移、贝叶斯(Dirichlet)更新。

每个模型对红球输出 33 维概率、对蓝球输出 16 维概率，供集成引擎加权融合。
"""
from __future__ import annotations

from typing import Dict, List

import numpy as np

from . import features as F

R_MAX, B_MAX = 33, 16


def _norm(p: np.ndarray) -> np.ndarray:
    s = p.sum()
    return p / s if s > 0 else np.full_like(p, 1.0 / len(p))


def freq_model(draws: List[Dict], decay: float = 0.0) -> Dict:
    """频率加权：近 30 期（可选指数衰减）频率 + 均匀平滑。"""
    n = min(len(draws), 30)
    sl = draws[-n:] if n else []
    rf = np.zeros(R_MAX + 1)
    bf = np.zeros(B_MAX + 1)
    for i, d in enumerate(sl):
        w = np.exp(-decay * (n - 1 - i)) if decay > 0 else 1.0
        for r in d["reds"]:
            rf[r] += w
        bf[d["blue"]] += w
    pr = _norm(rf[1:] + 0.5)
    pb = _norm(bf[1:] + 0.3)
    return {"red": pr, "blue": pb, "name": "freq"}


def omission_model(draws: List[Dict]) -> Dict:
    """遗漏回补：当前遗漏 / 平均遗漏 越大权重越高（与频率模型形成对照）。"""
    n = len(draws)
    om_r = F.current_omission_red(draws)
    avg_r = F.avg_omission(F.red_frequency(draws), n)
    ratio_r = np.divide(om_r[1:], avg_r[1:], out=np.ones(R_MAX), where=avg_r[1:] > 0)
    om_b = F.current_omission_blue(draws)
    avg_b = F.avg_omission(F.blue_frequency(draws), n)
    ratio_b = np.divide(om_b[1:], avg_b[1:], out=np.ones(B_MAX), where=avg_b[1:] > 0)
    return {"red": _norm(ratio_r + 0.1), "blue": _norm(ratio_b + 0.1), "name": "omission"}


def markov_model(draws: List[Dict]) -> Dict:
    """一阶转移：给定本期开出的号码，下一期各号码的条件概率（全历史累计）。"""
    T = np.zeros((R_MAX + 1, R_MAX + 1))  # T[i][j]: i 本期出现 -> j 下期出现
    B = np.zeros((B_MAX + 1, B_MAX + 1))
    for a, b in zip(draws, draws[1:]):
        for i in a["reds"]:
            for j in b["reds"]:
                T[i][j] += 1
        B[a["blue"]][b["blue"]] += 1
    last = draws[-1]
    pr = np.zeros(R_MAX + 1)
    for i in last["reds"]:
        row = T[i]
        if row.sum() > 0:
            pr += row / row.sum()
        else:
            pr += np.full(R_MAX + 1, 1.0 / (R_MAX + 1))
    pb = np.zeros(B_MAX + 1)
    row_b = B[last["blue"]]
    if row_b.sum() > 0:
        pb = row_b / row_b.sum()
    else:
        pb = np.full(B_MAX + 1, 1.0 / (B_MAX + 1))
    return {"red": _norm(pr[1:]), "blue": _norm(pb[1:]), "name": "markov"}


def bayes_model(draws: List[Dict], alpha: float = 1.0) -> Dict:
    """Dirichlet-Multinomial 后验预测概率（时间衰减计数 + 平滑先验）。"""
    n = len(draws)
    lam = 0.02  # 指数衰减速率
    rf = np.zeros(R_MAX + 1)
    bf = np.zeros(B_MAX + 1)
    for i, d in enumerate(draws):
        w = np.exp(-lam * (n - 1 - i))
        for r in d["reds"]:
            rf[r] += w
        bf[d["blue"]] += w
    pr = (rf[1:] + alpha) / (rf[1:].sum() + R_MAX * alpha)
    pb = (bf[1:] + alpha) / (bf[1:].sum() + B_MAX * alpha)
    return {"red": pr, "blue": pb, "name": "bayes"}


def uniform_model() -> Dict:
    return {
        "red": np.full(R_MAX, 1.0 / R_MAX),
        "blue": np.full(B_MAX, 1.0 / B_MAX),
        "name": "uniform",
    }


def build_models(draws: List[Dict]) -> Dict[str, Dict]:
    models = [freq_model(draws), omission_model(draws), markov_model(draws), bayes_model(draws)]
    return {m["name"]: m for m in models}