"""ML 概率模型：基于历史数据的 GBDT/随机森林分类器，输出 33 维红球 + 16 维蓝球概率。

设计原则：
1. walk-forward 滚动训练，防止未来信息泄露
2. 输出校准后的概率（温度缩放）
3. 与现有统计模型融合，提供增量信号
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional

import numpy as np

from . import config, features as F

try:
    from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
    from sklearn.calibration import CalibratedClassifierCV
    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False


# ---------- 特征工程 ----------

_FEATURE_NAMES = [
    "freq_5", "freq_10", "freq_20", "freq_30", "freq_50", "freq_150",
    "omit_cur", "omit_avg", "omit_ratio",
    "appeared_1", "appeared_2", "appeared_3", "appeared_5", "appeared_10", "appeared_20",
    "zone_1", "zone_2", "zone_3",
    "sum_value", "span_value",
]


def build_training_data(
    draws: List[Dict], min_start: int = 300, test_ratio: float = 0.2
) -> tuple:
    """构建训练/测试数据集（walk-forward 分割）。

    返回:
        X_train, y_train, X_test, y_test, feature_names
    """
    if not HAS_SKLEARN:
        print("[ml] sklearn 未安装，跳过 ML 模型")
        return None, None, None, None, _FEATURE_NAMES

    print(f"[ml] 构建训练数据，min_start={min_start}")
    
    # 收集所有样本
    all_X, all_y, all_issues = [], [], []
    
    for i in range(min_start, len(draws) - 1):
        history = draws[:i + 1]
        target_reds = set(draws[i + 1]["reds"])
        
        for num in range(1, 34):
            feats = _compute_features(history, num)
            all_X.append(feats)
            all_y.append(1 if num in target_reds else 0)
            all_issues.append(draws[i]["issue"])
    
    X = np.array(all_X, dtype=float)
    y = np.array(all_y, dtype=float)
    
    # walk-forward 分割：最后 20% 作为测试集
    n_test = int(len(X) * test_ratio)
    X_train, y_train = X[:-n_test], y[:-n_test]
    X_test, y_test = X[-n_test:], y[-n_test:]
    
    print(f"[ml] 训练集: {X_train.shape}, 正样本率: {y_train.mean():.4f}")
    print(f"[ml] 测试集: {X_test.shape}, 正样本率: {y_test.mean():.4f}")
    
    return X_train, y_train, X_test, y_test, _FEATURE_NAMES


def _compute_features(history: List[Dict], number: int) -> List[float]:
    """为单个号码计算特征向量。"""
    n = len(history)
    feats = []
    
    # 多窗口频率
    for w in (5, 10, 20, 30, 50, 150):
        sl = F.window_slice(history, w)
        f = F.red_frequency(sl)
        feats.append(float(f[number]))
    
    # 遗漏
    om_cur = F.current_omission_red(history)
    om_avg = F.avg_omission(F.red_frequency(history), n)
    feats.append(float(om_cur[number]))
    feats.append(float(om_avg[number]))
    feats.append(float(om_cur[number]) / max(float(om_avg[number]), 0.01))
    
    # 近期出现次数
    for k in (1, 2, 3, 5, 10, 20):
        k = min(k, n)
        appeared = sum(1 for d in history[-k:] if number in d["reds"])
        feats.append(float(appeared) / k)
    
    # 三区计数
    zc = F.zone_counts(history[-10:]) if n >= 10 else history
    z_counts = [0, 0, 0]
    for z in zc:
        for i in range(3):
            z_counts[i] += z[i]
    for zc_val in z_counts:
        feats.append(float(zc_val) / max(len(zc), 1))
    
    # 和值与跨度
    recent = history[-10:] if n >= 10 else history
    if recent:
        feats.append(float(np.mean(F.sums(recent))))
        feats.append(float(np.mean(F.span(recent))))
    else:
        feats.append(0.0)
        feats.append(0.0)
    
    return feats


# ---------- 模型训练 ----------


def train_red_model(
    draws: List[Dict], min_start: int = 300
) -> Optional[Dict]:
    """训练红球概率模型（33 个独立二分类器）。"""
    if not HAS_SKLEARN:
        return None
    
    X_train, y_train, X_test, y_test, feat_names = build_training_data(draws, min_start)
    if X_train is None:
        return None
    
    models = {}
    metrics = {}
    
    for num in range(1, 34):
        # 该号码作为正样本的数据
        mask = (np.array([m % 33 + 1 for m in range(len(y_train))]) == num)
        if mask.sum() < 10:
            continue
            
        X_num = X_train[mask]
        y_num = y_train[mask]
        
        # 使用随机森林（比 GBDT 更快）
        clf = RandomForestClassifier(
            n_estimators=50,
            max_depth=5,
            random_state=42,
            n_jobs=-1,
            class_weight='balanced'
        )
        clf.fit(X_num, y_num)
        
        # 在测试集上评估
        test_mask = (np.array([m % 33 + 1 for m in range(len(y_test))]) == num)
        if test_mask.sum() > 0:
            X_test_num = X_test[test_mask]
            y_test_num = y_test[test_mask]
            pred_proba = clf.predict_proba(X_test_num)[:, 1]
            brier = float(np.mean((pred_proba - y_test_num) ** 2))
            metrics[num] = {"brier": brier, "n_test": int(test_mask.sum())}
        
        models[num] = clf
    
    result = {
        "models": models,
        "metrics": metrics,
        "feature_names": feat_names,
    }
    print(f"[ml] 红球模型训练完成，平均测试 Brier: {np.mean([m['brier'] for m in metrics.values()]):.4f}")
    return result


def train_blue_model(
    draws: List[Dict], min_start: int = 300
) -> Optional[Dict]:
    """训练蓝球概率模型（16 个独立二分类器）。"""
    if not HAS_SKLEARN:
        return None
    
    print("[ml] 构建蓝球训练数据...")
    
    all_X, all_y = [], []
    for i in range(min_start, len(draws) - 1):
        history = draws[:i + 1]
        target_blue = draws[i + 1]["blue"]
        
        for num in range(1, 17):
            feats = _compute_blue_features(history, num)
            all_X.append(feats)
            all_y.append(1 if num == target_blue else 0)
    
    X = np.array(all_X, dtype=float)
    y = np.array(all_y, dtype=float)
    
    # walk-forward 分割
    n_test = int(len(X) * 0.2)
    X_train, y_train = X[:-n_test], y[:-n_test]
    X_test, y_test = X[-n_test:], y[-n_test:]
    
    clf = RandomForestClassifier(
        n_estimators=50,
        max_depth=5,
        random_state=42,
        class_weight='balanced'
    )
    clf.fit(X_train, y_train)
    
    pred_proba = clf.predict_proba(X_test)[:, 1]
    brier = float(np.mean((pred_proba - y_test) ** 2))
    
    print(f"[ml] 蓝球模型训练完成，测试 Brier: {brier:.4f}")
    
    return {
        "model": clf,
        "metrics": {"brier": brier, "n_test": int(len(y_test))},
        "feature_names": _BLUE_FEATURE_NAMES,
    }


_BLUE_FEATURE_NAMES = [
    "freq_5", "freq_10", "freq_20", "freq_30", "freq_50", "freq_150",
    "omit_cur", "omit_avg", "omit_ratio",
    "appeared_1", "appeared_2", "appeared_3", "appeared_5", "appeared_10", "appeared_20",
    "sum_value",
]


def _compute_blue_features(history: List[Dict], number: int) -> List[float]:
    """蓝球特征向量。"""
    n = len(history)
    feats = []
    
    for w in (5, 10, 20, 30, 50, 150):
        sl = history[-w:] if w <= n else history
        f = F.blue_frequency(sl)
        feats.append(float(f[number]))
    
    om_cur = F.current_omission_blue(history)
    om_avg = F.avg_omission(F.blue_frequency(history), n)
    feats.append(float(om_cur[number]))
    feats.append(float(om_avg[number]))
    feats.append(float(om_cur[number]) / max(float(om_avg[number]), 0.01))
    
    for k in (1, 2, 3, 5, 10, 20):
        k = min(k, n)
        appeared = sum(1 for d in history[-k:] if d["blue"] == number)
        feats.append(float(appeared) / k)
    
    if n >= 10:
        feats.append(float(np.mean(F.sums(history[-10:]))))
    else:
        feats.append(0.0)
    
    return feats


# ---------- 预测接口 ----------


def predict_red_probs(ml_model: Optional[Dict], draws: List[Dict]) -> np.ndarray:
    """使用 ML 模型预测下期红球概率。"""
    if ml_model is None or not HAS_SKLEARN:
        return np.full(33, 1.0 / 33)
    
    history = draws  # 使用全部历史作为特征输入
    probs = np.zeros(33)
    
    for num in range(1, 34):
        if num in ml_model["models"]:
            feats = np.array([_compute_features(history, num)])
            prob = ml_model["models"][num].predict_proba(feats)[0, 1]
            probs[num - 1] = prob
    
    # 归一化
    probs = probs / probs.sum() * 6  # 期望命中 6 个
    return probs


def predict_blue_probs(ml_model: Optional[Dict], draws: List[Dict]) -> np.ndarray:
    """使用 ML 模型预测下期蓝球概率。"""
    if ml_model is None or not HAS_SKLEARN or "model" not in ml_model:
        return np.full(16, 1.0 / 16)
    
    history = draws
    feats = np.array([_compute_blue_features(history, num) for num in range(1, 17)])
    probs = ml_model["model"].predict_proba(feats)[:, 1]
    
    # 归一化
    probs = probs / probs.sum()
    return probs


def get_ml_metrics(ml_red: Optional[Dict], ml_blue: Optional[Dict]) -> Dict:
    """获取 ML 模型评估指标。"""
    metrics = {}
    if ml_red and "metrics" in ml_red:
        avg_brier = np.mean([m["brier"] for m in ml_red["metrics"].values()]) if ml_red["metrics"] else 0
        metrics["red_avg_brier"] = round(avg_brier, 4)
        metrics["red_n_test"] = ml_red["metrics"].get("n_test", 0)
    if ml_blue and "metrics" in ml_blue:
        metrics["blue_brier"] = round(ml_blue["metrics"].get("brier", 0), 4)
        metrics["blue_n_test"] = ml_blue["metrics"].get("n_test", 0)
    return metrics
