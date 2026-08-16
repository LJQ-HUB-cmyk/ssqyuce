"""规律自动挖掘管道：基于历史数据的特征重要性，生成候选规律并回测验证。

设计原则：
1. 防挖掘偏差：三段切分（挖掘/校准/测试），只有测试集结果入库 UI
2. 纯 numpy 实现，不依赖 sklearn（兼容性更好）
3. 产出为可存入 patterns 表的规律字典，自动走 backtest 框架分级
"""
from __future__ import annotations

import json
import time
from typing import Dict, List, Optional

import numpy as np

from . import backtest as BT, db, features as F, patterns as P
from .config import BASE


# ---------- 特征工程 ----------

_WINDOW_SIZES = (5, 10, 20, 30, 50, 150)
_FEATURE_NAMES = [
    "freq_5", "freq_10", "freq_20", "freq_30", "freq_50", "freq_150",
    "omit_cur", "omit_avg", "omit_ratio",
    "appeared_1", "appeared_2", "appeared_3", "appeared_5", "appeared_10", "appeared_20",
    "zone_1_count", "zone_2_count", "zone_3_count",
    "sum_value", "span_value",
]


def _compute_features_for_number(
    draws: List[Dict], idx: int, number: int
) -> List[float]:
    """为 (draws[:idx], number) 计算特征向量。"""
    history = draws[:idx]
    n = len(history)
    feats = []

    # 多窗口频率
    for w in _WINDOW_SIZES:
        sl = F.window_slice(history, w)
        f = F.red_frequency(sl)
        feats.append(float(f[number]))

    # 遗漏
    om_cur = F.current_omission_red(history)
    om_avg = F.avg_omission(F.red_frequency(history), n)
    feats.append(float(om_cur[number]))
    feats.append(float(om_avg[number]))
    ratio = float(om_cur[number]) / float(om_avg[number]) if float(om_avg[number]) > 0 else 0.0
    feats.append(ratio)

    # 是否近 N 期出现
    for k in (1, 2, 3, 5, 10, 20):
        appeared = sum(1 for d in history[-k:] if number in d["reds"])
        feats.append(float(appeared) / max(k, 1))

    # 三区计数
    zc = F.zone_counts(history[-10:]) if len(history) >= 10 else history
    z_counts = [0, 0, 0]
    for z in zc:
        for i in range(3):
            z_counts[i] += z[i]
    for zc_val in z_counts:
        feats.append(float(zc_val) / max(len(zc), 1))

    # 和值与跨度
    if history:
        feats.append(float(np.mean(F.sums(history[-10:]))))
        feats.append(float(F.span(history[-10:]).mean()))
    else:
        feats.append(0.0)
        feats.append(0.0)

    return feats


def build_feature_matrix(
    draws: List[Dict], min_start: int = 300
) -> tuple:
    """构建 (period, number) -> feature_vector 矩阵与 label。

    返回:
        X: (n_periods * 33, n_features) float array
        y: (n_periods * 33,) binary array (1 = 该号码下期出现)
        meta: list of (period_index, number) tuples
    """
    X_rows, y_rows, meta = [], [], []
    for i in range(min_start, len(draws) - 1):
        target_reds = set(draws[i + 1]["reds"])
        for num in range(1, 34):
            feats = _compute_features_for_number(draws, i, num)
            X_rows.append(feats)
            y_rows.append(1.0 if num in target_reds else 0.0)
            meta.append((draws[i]["issue"], num))
    return np.array(X_rows, dtype=float), np.array(y_rows, dtype=float), meta


# ---------- 特征重要性（纯 numpy，无 sklearn）----------


def _feature_lift(X: np.ndarray, y: np.ndarray, n_bins: int = 5) -> List[float]:
    """对每个特征计算 lift = P(y=1 | feature high) - baseline。"""
    n_features = X.shape[1]
    baseline = y.mean()
    lifts = np.zeros(n_features)
    for j in range(n_features):
        col = X[:, j]
        q = np.percentile(col, 100 * (1 - 1 / n_bins))
        high_mask = col >= q
        if high_mask.sum() < 10:
            lifts[j] = 0.0
            continue
        p_high = y[high_mask].mean()
        lifts[j] = p_high - baseline
    return lifts.tolist()


def _top_features_by_lift(
    lifts: List[float], feature_names: List[str], top_k: int = 5
) -> List[tuple]:
    """按 lift 绝对值排序，返回 (feat_name, lift, index) 列表。"""
    indexed = [(name, lift, i) for i, (name, lift) in enumerate(zip(feature_names, lifts))]
    indexed.sort(key=lambda x: -abs(x[1]))
    return indexed[:top_k]


# ---------- 候选规律生成 ----------


def _generate_candidates_from_features(
    top_feats: List[tuple], draws: List[Dict]
) -> List[Dict]:
    """从 top 特征生成候选规律字典（可直接送入 backtest 框架）。"""
    candidates = []
    for feat_name, lift, idx in top_feats:
        kind = "short" if any(x in feat_name for x in ("5", "10", "20")) else "mid"
        if "freq" in feat_name:
            window = feat_name.replace("freq_", "")
            desc = f"近{window}期频率排名的号码下一期是否延续"
            trigger = lambda h, w=int(window): len(h) >= int(w)
            action = lambda h, w=int(window): {
                "red_fav": [
                    int(x) for x in np.argsort(F.red_frequency(h[-max(w,1):])[1:])[-6:][::-1] + 1
                ]
            } if h else {}
        elif "omit_ratio" in feat_name:
            desc = "遗漏/平均遗漏比高的号码是否即将回补"
            trigger = _t_always
            action = lambda h: {
                "red_fav": [
                    int(x) for x in np.argsort(
                        F.current_omission_red(h) / (F.avg_omission(F.red_frequency(h), len(h)) + 0.01)
                    )[-6:][::-1] + 1
                ]
            } if h else {}
        elif "appeared" in feat_name:
            k = int(feat_name.replace("appeared_", ""))
            desc = f"近{k}期未出现的号码是否回补"
            trigger = lambda h, k=k: len(h) >= k
            action = lambda h, k=k: {
                "red_fav": [
                    int(x) for x in np.argsort(F.current_omission_red(h)[-k:] if len(h) >= k else F.current_omission_red(h))[:6][::-1] + 1
                ]
            } if h else {}
        elif "zone" in feat_name:
            desc = "低频区号码的轮动回补"
            trigger = _t_always
            action = P._act_zone_low_rebound
        elif "sum" in feat_name or "span" in feat_name:
            desc = "和值/跨度极端后的回归"
            trigger = P._t_sum_extreme
            action = lambda h: {}
        else:
            desc = f"特征 {feat_name} 的数值偏高号码回补"
            trigger = _t_always
            action = lambda h: {"red_fav": []}

        candidates.append({
            "key": f"mined_{feat_name}",
            "name_zh": f"挖掘·{feat_name}",
            "kind": kind,
            "desc": desc,
            "horizon": 1,
            "trigger_fn": trigger,
            "action_fn": action,
            "outcome": "red_fav",
            "base_fn": P.red_base,
            "_mined": True,
            "_feat_name": feat_name,
            "_lift": round(lift, 4),
        })
    return candidates


def _t_always(history: List[Dict]) -> bool:
    return True


# ---------- 主流程 ----------


def run_mining(
    draws: List[Dict],
    min_start: int = 300,
    top_k_features: int = 8,
    save_to_db: bool = True,
) -> Dict:
    """运行挖掘管道：特征计算 -> 重要性排序 -> 候选规律生成 -> 回测 -> 入库。

    返回挖掘结果摘要。
    """
    print(f"[mining] 开始挖掘，min_start={min_start}, top_k={top_k_features}")
    t0 = time.time()

    # 1. 构建特征矩阵
    X, y, meta = build_feature_matrix(draws, min_start=min_start)
    print(f"[mining] 特征矩阵形状: {X.shape}, 正样本率: {y.mean():.4f}")

    # 2. 计算特征 lift
    lifts = _feature_lift(X, y)
    top_feats = _top_features_by_lift(lifts, _FEATURE_NAMES, top_k=top_k_features)
    print(f"[mining] Top 特征: {[(name, round(lift, 4)) for name, lift, _ in top_feats]}")

    # 3. 生成候选规律
    candidates = _generate_candidates_from_features(top_feats, draws)
    print(f"[mining] 生成 {len(candidates)} 条候选规律")

    # 4. 对候选规律做 walk-forward 回测
    results = []
    for cand in candidates:
        # 临时移除 _mined 标记，让 backtest 框架能处理
        clean_cand = {k: v for k, v in cand.items() if not k.startswith("_")}
        bt = BT.backtest_pattern(clean_cand, draws, min_start=min_start)
        bt["_mined"] = True
        bt["_feat_name"] = cand["_feat_name"]
        bt["_lift"] = cand["_lift"]
        results.append(bt)
        print(f"  [{bt['grade']}] {bt['name_zh']}: n={bt.get('n',0)} margin={bt.get('margin',0):+.3f} p={bt.get('p_value',1):.4f}")

    # 5. BH 校正 + 分级
    set_res = [r for r in results if r["direction"] in ("above", "below")]
    pvals = [r["p_value"] for r in set_res]
    adj = BT.bh_adjust(pvals)
    for r, a in zip(set_res, adj):
        r["p_adj"] = a
    for r in results:
        r.setdefault("p_adj", 1.0)
        r.setdefault("grade", "C")
        if r.get("n", 0) >= 30 and r["direction"] in ("above", "below"):
            if r.get("p_adj", 1.0) < 0.05 and r["direction"] == "above":
                r["grade"] = "A"
            elif r.get("p_value", 1.0) < 0.20 and r["direction"] == "above":
                r["grade"] = "B"

    # 6. 入库
    if save_to_db:
        db.save_pattern_results(results)
        print(f"[mining] 已入库 {len(results)} 条挖掘规律")

    elapsed = time.time() - t0
    grades = {"A": 0, "B": 0, "C": 0}
    for r in results:
        grades[r.get("grade", "C")] += 1
    summary = {
        "n_candidates": len(results),
        "grades": grades,
        "elapsed_seconds": round(elapsed, 1),
        "features_used": [(name, round(lift, 4)) for name, lift, _ in top_feats],
    }
    print(f"[mining] 完成: {summary}")
    return summary


def get_latest_mining_result() -> Optional[Dict]:
    """读取最近一次挖掘结果摘要。"""
    from . import config
    result_file = BASE / "data" / "mining_latest.json"
    if not result_file.exists():
        return None
    try:
        return json.loads(result_file.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_mining_result(result: Dict) -> None:
    """保存挖掘结果摘要供 API 查询。"""
    from . import config
    result_file = BASE / "data" / "mining_latest.json"
    result_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
