"""Walk-forward 回测框架：每条规律在历史上滚动验证，做显著性检验与分级。

原则：预测第 i+1 期时只允许使用前 i 期数据（严格防未来信息）。
分级：A = 显著性通过（BH 校正后 p<0.05）且方向符合假设；B = raw p<0.20 且方向符合；
      其余为 C（不参与预测，仅展示）。
"""
from __future__ import annotations

import math
from typing import Dict, List

import numpy as np
from scipy import stats

from . import db, features as F, patterns as P


def next_issue(issue: str) -> str:
    year = int(issue[:4])
    seq = int(issue[4:])
    seq += 1
    if seq > 999:
        year, seq = year + 1, 1
    return f"{year:04d}{seq:03d}"


def bh_adjust(pvals: List[float]) -> List[float]:
    """Benjamini-Hochberg FDR 校正。"""
    m = len(pvals)
    if m == 0:
        return []
    order = np.argsort(pvals)
    ranked = np.array(pvals)[order]
    adj = ranked * m / (np.arange(1, m + 1))
    # 单调约束
    for i in range(m - 2, -1, -1):
        adj[i] = min(adj[i], adj[i + 1])
    out = np.empty(m)
    out[order] = np.clip(adj, 0.0, 1.0)
    return out.tolist()


def backtest_pattern(pattern: Dict, draws: List[Dict], min_start: int = 120) -> Dict:
    """在 draws（时间升序）上滚动验证 pattern。min_start: 至少多少期历史才开始验证。"""
    key = pattern["key"]
    outcome = pattern["outcome"]
    kind = "red" if outcome == "red_fav" else ("blue" if outcome == "blue_fav" else "numeric")
    horizon = pattern.get("horizon", 1)

    hit_counts, fav_sizes, exp_vec, p0_vec = [], [], [], []
    n_triggers = 0
    numeric_vals, numeric_all, numeric_base = [], [], None

    for i in range(min_start, len(draws)):
        history = draws[:i]
        res = P.run_pattern(pattern, history)
        if not res["triggered"]:
            continue
        n_triggers += 1
        action = res["action"]

        if outcome == "numeric_sum":
            s = F.sums(history)
            mean = float(np.mean(s))
            target = draws[i]
            numeric_vals.append(abs(sum(target["reds"]) - mean))
            if numeric_base is None:
                numeric_all = [abs(sum(d["reds"]) - mean) for d in draws[min_start:]]
                numeric_base = float(np.mean(numeric_all))
            continue

        fav = action.get(outcome, [])
        if not fav:
            continue
        k = len(fav)
        fav_sizes.append(k)

        if kind == "red":
            exp_i = 6 * k / 33
            p0_i = 1 - math.comb(33 - k, 6) / math.comb(33, 6)
        else:  # blue
            exp_i = 1 - (1 - k / 16) ** horizon
            p0_i = exp_i

        targets = [draws[i + h] for h in range(horizon) if i + h < len(draws)]
        if kind == "red":
            hits_i = len(set(targets[0]["reds"]) & set(fav))
        else:
            hits_i = int(any(t["blue"] in fav for t in targets))
        hit_counts.append(hits_i)
        exp_vec.append(exp_i)
        p0_vec.append(p0_i)

    result = {"key": key, "name_zh": pattern["name_zh"], "kind": pattern["kind"],
              "desc": pattern["desc"], "horizon": horizon, "n": n_triggers,
              "backtest": {}}

    if outcome == "numeric_sum":
        if len(numeric_vals) >= 10 and numeric_base is not None:
            arr = np.array(numeric_vals)
            margin = float(numeric_base - np.mean(arr))  # 正 = 触发后更接近均值
            res_tt = stats.ttest_1samp(arr, np.mean(numeric_all))
            p_two = float(res_tt.pvalue)
            direction = "toward_mean" if margin > 0 else "away_from_mean"
            result["margin"] = margin
            result["p_value"] = min(1.0, p_two / 2 if direction == "toward_mean" else 1.0 - p_two / 2)
            result["direction"] = direction
            result["backtest"] = {
                "metric": "mean|sum-mean|", "avg_triggered": float(np.mean(arr)),
                "avg_baseline": numeric_base, "margin": margin,
            }
        else:
            result["margin"], result["p_value"], result["direction"] = 0.0, 1.0, "none"
        return result

    if n_triggers < 10 or not hit_counts:
        result.update(margin=0.0, p_value=1.0, p_adj=1.0, direction="none", grade="C")
        result["backtest"] = {"n": n_triggers, "avg_hits": 0.0, "expected": 0.0,
                              "hit_rate_at_least1": 0.0, "p0": 0.0, "avg_fav_size": 0.0}
        return result

    hits = np.array(hit_counts, dtype=float)
    exp = np.array(exp_vec, dtype=float)
    p0 = np.array(p0_vec, dtype=float)
    avg = float(hits.mean())
    avg_exp = float(exp.mean())
    at_least1 = int(np.sum(hits > 0))
    rate = at_least1 / n_triggers
    margin = avg - avg_exp
    direction = "above" if margin >= 0 else "below"
    # 逐触发期偏差 d_i = hits_i - exp_i 的单样本 t 检验（对零均值），单侧 p
    diffs = hits - exp
    t_res = stats.ttest_1samp(diffs, 0.0)
    p_two = float(t_res.pvalue)
    p_one = p_two / 2 if direction == "above" else 1.0 - p_two / 2
    result.update(margin=margin, p_value=float(np.clip(p_one, 0.0, 1.0)),
                  direction=direction, grade="C")
    result["backtest"] = {
        "n": n_triggers, "avg_hits": avg, "expected": avg_exp, "margin": margin,
        "hit_rate_at_least1": rate, "p0": float(p0.mean()),
        "avg_fav_size": float(np.mean(fav_sizes)) if fav_sizes else 0.0,
        "t_stat": float(t_res.statistic),
    }
    return result


def run_all_backtests(draws: List[Dict], min_start: int = 300) -> List[Dict]:
    results = [backtest_pattern(p, draws, min_start=min_start) for p in P.PATTERNS]
    # BH 校正（仅对 set 类结果）
    for r in results:
        r["sample_size"] = r.get("n", 0)
    set_res = [r for r in results if r["direction"] in ("above", "below")]
    pvals = [r["p_value"] for r in set_res]
    adj = bh_adjust(pvals)
    for r, a in zip(set_res, adj):
        r["p_adj"] = a
    for r in results:
        r.setdefault("p_adj", 1.0)
        r.setdefault("grade", "C")
        claimed = r.get("claimed_direction", "above")  # 规律假设方向（默认高于随机）
        if r.get("n", 0) >= 30 and r["direction"] in ("above", "below"):
            if r.get("p_adj", 1.0) < 0.05:
                if r["direction"] == claimed:
                    r["grade"] = "A"
                else:
                    r["grade"] = "C"
                    r["refuted"] = True
            elif r["p_value"] < 0.20 and r["direction"] == claimed:
                r["grade"] = "B"
        if r.get("refuted"):
            r["backtest"]["note"] = (
                "显著但方向与假设相反：该类别号码的命中显著低于随机，" 
                "假设被证伪（过拟合/后见之明典型）。"
            )
        db.save_pattern_results([r])
    return results


def summarize(results: List[Dict]) -> Dict:
    grades = {"A": 0, "B": 0, "C": 0}
    for r in results:
        grades[r.get("grade", "C")] += 1
    return {
        "total": len(results),
        "grades": grades,
        "significant": [r for r in results if r.get("grade") in ("A", "B")],
        "notes": "彩票为随机事件：样本外回测能有效识别过拟合规律。"
                 "未发现显著规律属于正常且诚实的结论。",
    }