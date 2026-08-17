"""ML 概率模型（M2）：GBDT + 随机森林投票集成 + 概率校准 + 蓝球独立投票。

架构：
- 红球：33 个号码各训练一个 (RandomForest + HistGradientBoosting) 软投票分类器，
  再用 CalibratedClassifierCV(sigmoid) 把原始概率校准为诚实概率；
- 蓝球：16 选 1 拆成 16 个二分类，由 RF + HistGB + 经典 GBDT 三模型投票 + 校准；
- 输出：33 维红球 / 16 维蓝球概率（归一化），供 engine 的 Brier 加权融合使用；
- 评估：walk-forward 滚动评估（每 ML_REFIT_EVERY 期重训一次），对照均匀基线，
  给出 Brier / log-loss / 校准曲线（可靠性图）/ paired 显著性检验。

诚实边界：双色球为独立随机事件，ML 概率只用于结构配平与校准研究，
不承诺提高中奖率。
"""
from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional

import numpy as np

from . import config, features as F

try:
    from sklearn.ensemble import (
        GradientBoostingClassifier,
        HistGradientBoostingClassifier,
        RandomForestClassifier,
        VotingClassifier,
    )
    from sklearn.calibration import CalibratedClassifierCV
    HAS_SKLEARN = True
    HAS_HISTGB = True
except ImportError:
    HAS_SKLEARN = False
    HAS_HISTGB = False


_FEATURE_NAMES = [
    "freq_5", "freq_10", "freq_20", "freq_30", "freq_50", "freq_150",
    "omit_cur", "omit_avg", "omit_ratio",
    "appeared_1", "appeared_2", "appeared_3", "appeared_5", "appeared_10", "appeared_20",
    "zone_1", "zone_2", "zone_3",
    "sum_value", "span_value",
]

_BLUE_FEATURE_NAMES = [
    "freq_5", "freq_10", "freq_20", "freq_30", "freq_50", "freq_150",
    "omit_cur", "omit_avg", "omit_ratio",
    "appeared_1", "appeared_2", "appeared_3", "appeared_5", "appeared_10", "appeared_20",
    "sum_value",
]


# ---------- 特征工程 ----------

def _compute_features(history: List[Dict], number: int) -> List[float]:
    """为单个红球号码计算特征向量（与 M1 一致，保持可复现）。"""
    n = len(history)
    feats = []

    for w in (5, 10, 20, 30, 50, 150):
        sl = F.window_slice(history, w)
        f = F.red_frequency(sl)
        feats.append(float(f[number]))

    om_cur = F.current_omission_red(history)
    om_avg = F.avg_omission(F.red_frequency(history), n)
    feats.append(float(om_cur[number]))
    feats.append(float(om_avg[number]))
    feats.append(float(om_cur[number]) / max(float(om_avg[number]), 0.01))

    for k in (1, 2, 3, 5, 10, 20):
        k = min(k, n)
        appeared = sum(1 for d in history[-k:] if number in d["reds"])
        feats.append(float(appeared) / k)

    zc = F.zone_counts(history[-10:]) if n >= 10 else history
    z_counts = [0, 0, 0]
    for z in zc:
        for i in range(3):
            z_counts[i] += z[i]
    for zc_val in z_counts:
        feats.append(float(zc_val) / max(len(zc), 1))

    recent = history[-10:] if n >= 10 else history
    if recent:
        feats.append(float(np.mean(F.sums(recent))))
        feats.append(float(np.mean(F.span(recent))))
    else:
        feats.append(0.0)
        feats.append(0.0)

    return feats


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


# ---------- 模型构造 ----------

def _balanced_weights(y: np.ndarray) -> np.ndarray:
    """正样本权重 = 倒频率，抵消类别不平衡。"""
    pos = float(np.mean(y))
    if pos <= 0 or pos >= 1:
        return np.ones(len(y))
    w = np.where(y == 1, (1.0 - pos) / pos, 1.0)
    return w


def _make_rf() -> RandomForestClassifier:
    return RandomForestClassifier(
        n_estimators=max(20, config.ML_N_ESTIMATORS),
        max_depth=config.ML_MAX_DEPTH,
        random_state=42,
        n_jobs=-1,
        class_weight="balanced",
    )


def _make_histgb() -> HistGradientBoostingClassifier:
    return HistGradientBoostingClassifier(
        max_iter=max(25, config.ML_N_ESTIMATORS),
        max_depth=min(4, max(2, config.ML_MAX_DEPTH)),
        random_state=42,
    )


def _make_classic_gbdt() -> GradientBoostingClassifier:
    return GradientBoostingClassifier(
        n_estimators=max(20, min(60, config.ML_N_ESTIMATORS)),
        max_depth=min(3, max(2, config.ML_MAX_DEPTH)),
        learning_rate=0.05,
        random_state=42,
    )


def _red_voting() -> VotingClassifier:
    """红球二分类投票：RF + HistGB（速度优先）。"""
    ests = [("rf", _make_rf())]
    if HAS_HISTGB:
        ests.append(("gb", _make_histgb()))
    return VotingClassifier(estimators=ests, voting="soft", n_jobs=-1)


def _blue_voting() -> VotingClassifier:
    """蓝球二分类投票：RF + HistGB + 经典 GBDT（蓝球样本更少，多为胜者）。"""
    ests = [("rf", _make_rf())]
    if HAS_HISTGB:
        ests.append(("gb", _make_histgb()))
    ests.append(("gbdt", _make_classic_gbdt()))
    return VotingClassifier(estimators=ests, voting="soft", n_jobs=-1)


def _calibrated(clf, X, y, cv: int = 3):
    """sigmoid（Platt）校准；样本不足时退化为原分类器。"""
    if len(np.unique(y)) < 2 or len(y) < 30:
        return clf
    try:
        cal = CalibratedClassifierCV(estimator=clf, method="sigmoid", cv=cv)
        cal.fit(X, y)
        return cal
    except Exception:  # noqa: BLE001
        return clf


# ---------- 数据集构建（per-number） ----------

def _build_red_datasets(draws: List[Dict], min_start: int) -> Dict[int, tuple]:
    """返回 {num: (X, y)}，X 每行 = 用 draws[:i+1] 预测 draws[i+1]。"""
    out: Dict[int, tuple] = {num: ([], []) for num in range(1, 34)}
    for i in range(min_start, len(draws) - 1):
        history = draws[:i + 1]
        target_reds = set(draws[i + 1]["reds"])
        for num in range(1, 34):
            feats = _compute_features(history, num)
            out[num][0].append(feats)
            out[num][1].append(1 if num in target_reds else 0)
    return {num: (np.array(x, dtype=float), np.array(y, dtype=float))
            for num, (x, y) in out.items()}


def _build_blue_datasets(draws: List[Dict], min_start: int) -> Dict[int, tuple]:
    out: Dict[int, tuple] = {num: ([], []) for num in range(1, 17)}
    for i in range(min_start, len(draws) - 1):
        history = draws[:i + 1]
        target_blue = draws[i + 1]["blue"]
        for num in range(1, 17):
            feats = _compute_blue_features(history, num)
            out[num][0].append(feats)
            out[num][1].append(1 if num == target_blue else 0)
    return {num: (np.array(x, dtype=float), np.array(y, dtype=float))
            for num, (x, y) in out.items()}


def _split(X: np.ndarray, y: np.ndarray, test_ratio: float = 0.2):
    n_test = max(1, int(len(X) * test_ratio))
    return X[:-n_test], y[:-n_test], X[-n_test:], y[-n_test:]


def _bin_metrics(p: np.ndarray, y: np.ndarray, n_bins: int = 10) -> Dict:
    """校准曲线（可靠性图）分箱统计 + ECE。"""
    p = np.clip(p, 1e-6, 1 - 1e-6)
    bins = np.linspace(0.0, 1.0, n_bins + 1)
    idx = np.clip(np.digitize(p, bins) - 1, 0, n_bins - 1)
    rows = []
    ece, total = 0.0, max(1, len(p))
    for b in range(n_bins):
        m = idx == b
        cnt = int(m.sum())
        if cnt == 0:
            rows.append({"bin": f"{bins[b]:.2f}-{bins[b+1]:.2f}", "n": 0,
                         "mean_pred": None, "freq": None})
            continue
        mp = float(np.mean(p[m]))
        fr = float(np.mean(y[m]))
        rows.append({"bin": f"{bins[b]:.2f}-{bins[b+1]:.2f}", "n": cnt,
                     "mean_pred": round(mp, 4), "freq": round(fr, 4)})
        ece += cnt * abs(mp - fr)
    return {"bins": rows, "ece": round(ece / total, 4)}


# ---------- 训练 ----------

def train_red_models(
    draws: List[Dict], min_start: Optional[int] = None
) -> Optional[Dict]:
    """训练 33 个红球号码：集成投票 + sigmoid 校准。返回模型与指标。"""
    if not HAS_SKLEARN:
        print("[ml] sklearn 未安装，跳过 ML 模型")
        return None
    min_start = min_start or config.ML_MIN_START
    if len(draws) < min_start + 10:
        print(f"[ml] 历史数据不足（{len(draws)} < {min_start + 10}），跳过训练")
        return None
    t0 = time.time()
    datasets = _build_red_datasets(draws, min_start)

    models: Dict[int, object] = {}
    metrics: Dict[int, Dict] = {}
    all_bin_p, all_bin_y = [], []

    for num in range(1, 34):
        X, y = datasets[num]
        X_tr, y_tr, X_te, y_te = _split(X, y)
        if len(np.unique(y_tr)) < 2 or len(y_tr) < 20:
            continue
        clf = _red_voting()
        try:
            clf.fit(X_tr, y_tr, sample_weight=_balanced_weights(y_tr))
            cal = _calibrated(clf, X_tr, y_tr, cv=config.ML_CAL_CV)
        except Exception as e:  # noqa: BLE001
            print(f"[ml] 红球号码 {num} 训练失败: {e}")
            continue
        models[num] = cal

        if len(X_te) > 0:
            p_raw = clf.predict_proba(X_te)[:, 1]
            p_cal = cal.predict_proba(X_te)[:, 1]
            b_raw = float(np.mean((p_raw - y_te) ** 2))
            b_cal = float(np.mean((p_cal - y_te) ** 2))
            eps = 1e-9
            ll_raw = float(-np.mean(y_te * np.log(np.clip(p_raw, eps, 1)) +
                                   (1 - y_te) * np.log(np.clip(1 - p_raw, eps, 1))))
            ll_cal = float(-np.mean(y_te * np.log(np.clip(p_cal, eps, 1)) +
                                    (1 - y_te) * np.log(np.clip(1 - p_cal, eps, 1))))
            metrics[num] = {
                "brier_raw": round(b_raw, 4), "brier_cal": round(b_cal, 4),
                "logloss_raw": round(ll_raw, 4), "logloss_cal": round(ll_cal, 4),
                "n_test": int(len(y_te)),
            }
            all_bin_p.extend(p_cal.tolist())
            all_bin_y.extend(y_te.tolist())

    if not models:
        return None
    calibration = _bin_metrics(np.array(all_bin_p), np.array(all_bin_y))
    result = {
        "models": models,
        "metrics": metrics,
        "calibration": calibration,
        "feature_names": _FEATURE_NAMES,
        "train_seconds": round(time.time() - t0, 1),
    }
    if metrics:
        avg_b = float(np.mean([m["brier_cal"] for m in metrics.values()]))
        print(f"[ml] 红球模型训练完成，{len(models)} 个号码，"
              f"平均测试 Brier(校准后)={avg_b:.4f}，ECE={calibration['ece']:.4f}，"
              f"耗时 {result['train_seconds']}s")
    return result


def train_blue_models(
    draws: List[Dict], min_start: Optional[int] = None
) -> Optional[Dict]:
    """训练 16 个蓝球号码：三模型投票 + 校准（蓝球独立建模）。"""
    if not HAS_SKLEARN:
        return None
    min_start = min_start or config.ML_MIN_START
    if len(draws) < min_start + 10:
        return None
    t0 = time.time()
    datasets = _build_blue_datasets(draws, min_start)

    models: Dict[int, object] = {}
    metrics: Dict[int, Dict] = {}
    all_bin_p, all_bin_y = [], []

    for num in range(1, 17):
        X, y = datasets[num]
        X_tr, y_tr, X_te, y_te = _split(X, y)
        if len(np.unique(y_tr)) < 2 or len(y_tr) < 20:
            continue
        clf = _blue_voting()
        try:
            clf.fit(X_tr, y_tr, sample_weight=_balanced_weights(y_tr))
            cal = _calibrated(clf, X_tr, y_tr, cv=config.ML_CAL_CV)
        except Exception as e:  # noqa: BLE001
            print(f"[ml] 蓝球号码 {num} 训练失败: {e}")
            continue
        models[num] = cal
        if len(X_te) > 0:
            p_raw = clf.predict_proba(X_te)[:, 1]
            p_cal = cal.predict_proba(X_te)[:, 1]
            b_raw = float(np.mean((p_raw - y_te) ** 2))
            b_cal = float(np.mean((p_cal - y_te) ** 2))
            eps = 1e-9
            ll_cal = float(-np.mean(y_te * np.log(np.clip(p_cal, eps, 1)) +
                                    (1 - y_te) * np.log(np.clip(1 - p_cal, eps, 1))))
            metrics[num] = {
                "brier_raw": round(b_raw, 4), "brier_cal": round(b_cal, 4),
                "logloss_cal": round(ll_cal, 4), "n_test": int(len(y_te)),
            }
            all_bin_p.extend(p_cal.tolist())
            all_bin_y.extend(y_te.tolist())

    if not models:
        return None
    calibration = _bin_metrics(np.array(all_bin_p), np.array(all_bin_y))
    return {
        "models": models,
        "metrics": metrics,
        "calibration": calibration,
        "feature_names": _BLUE_FEATURE_NAMES,
        "train_seconds": round(time.time() - t0, 1),
    }


# ---------- 模块级缓存（数据版本变化才重训） ----------

_ML_CACHE: Dict[str, Dict] = {}
_CACHE_LOCK = threading.Lock()
_TRAIN_EVENT: Optional[threading.Event] = None
_TRAIN_KEY: Optional[str] = None
_TRAIN_LOCK = threading.Lock()


def _cache_key(draws: List[Dict]) -> Optional[str]:
    if not draws:
        return None
    return f"{len(draws)}:{draws[-1]['issue']}"


def ml_peek(draws: List[Dict]) -> Optional[Dict]:
    """非阻塞查看缓存（从不触发训练）。"""
    key = _cache_key(draws)
    if key is None:
        return None
    with _CACHE_LOCK:
        return _ML_CACHE.get(key)


def ml_training_in_progress() -> bool:
    with _TRAIN_LOCK:
        return _TRAIN_EVENT is not None


def ml_ready(draws: List[Dict]) -> bool:
    """缓存是否已就绪（不触发训练）。供 engine 查询。"""
    return ml_peek(draws) is not None


def _model_path(key: str) -> "object":
    return config.DATA_DIR / "ml_models" / f"{key}.joblib"


def get_ml_models(draws: List[Dict], force: bool = False) -> Optional[Dict]:
    """返回 {"red":..., "blue":..., "key":..., "ready": bool}。

    数据版本未变则命中缓存；多个调用方并发训练时只训一次，
    其余线程等待同一训练完成后直接读缓存。
    """
    global _TRAIN_EVENT, _TRAIN_KEY
    if not HAS_SKLEARN or not config.ML_ENABLED:
        return None
    key = _cache_key(draws)
    if key is None:
        return None
    with _CACHE_LOCK:
        if not force and key in _ML_CACHE:
            return _ML_CACHE[key]
        if not force and _ML_CACHE.get("error", {}).get("key") == key:
            return None

    # 磁盘持久化：重建容器/重启后直接从 data 卷加载，无需重训
    if not force:
        path = _model_path(key)
        if path.exists():
            try:
                import joblib
                entry = joblib.load(path)
                if entry and entry.get("key") == key and entry.get("red") and entry.get("blue"):
                    with _CACHE_LOCK:
                        _ML_CACHE.clear()
                        _ML_CACHE[key] = entry
                    print(f"[ml] 已从磁盘加载模型（{key}）")
                    return entry
            except Exception as e:  # noqa: BLE001
                print(f"[ml] 磁盘模型加载失败，重新训练: {e}")

    # 已有同 key 训练在进行：等待其完成
    with _TRAIN_LOCK:
        wait_ev = _TRAIN_EVENT if (_TRAIN_EVENT is not None and _TRAIN_KEY == key) else None
    if wait_ev is not None:
        wait_ev.wait()
        with _CACHE_LOCK:
            if key in _ML_CACHE:
                return _ML_CACHE[key]
        return None

    # 抢到训练权
    ev = threading.Event()
    with _TRAIN_LOCK:
        if _TRAIN_EVENT is not None and _TRAIN_KEY == key:
            wait_ev = _TRAIN_EVENT
            _TRAIN_EVENT = None  # 本线程不重复训练
        else:
            wait_ev = None
            _TRAIN_EVENT = ev
            _TRAIN_KEY = key
    if wait_ev is not None:
        wait_ev.wait()
        with _CACHE_LOCK:
            if key in _ML_CACHE:
                return _ML_CACHE[key]
        return None

    try:
        red = train_red_models(draws)
        blue = train_blue_models(draws)
        if red is None:
            with _CACHE_LOCK:
                _ML_CACHE["error"] = {"key": key, "reason": "数据不足"}
            return None
        entry = {
            "red": red, "blue": blue, "key": key,
            "trained_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "red_brier": round(float(np.mean([m["brier_cal"]
                                              for m in red["metrics"].values()])), 4)
            if red["metrics"] else None,
            "blue_brier": round(float(np.mean([m["brier_cal"]
                                               for m in blue["metrics"].values()])), 4)
            if blue and blue["metrics"] else None,
        }
        with _CACHE_LOCK:
            _ML_CACHE.clear()
            _ML_CACHE[key] = entry
        try:
            import joblib
            path = _model_path(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            joblib.dump(entry, path)
            print(f"[ml] 模型已持久化到 {path.name}")
        except Exception as e:  # noqa: BLE001
            print(f"[ml] 模型持久化失败: {e}")
        return entry
    finally:
        with _TRAIN_LOCK:
            _TRAIN_EVENT = None
            _TRAIN_KEY = None
        ev.set()


def ml_status(draws: List[Dict]) -> Dict:
    """供 /api/ml/status 使用。"""
    if not HAS_SKLEARN:
        return {"enabled": False, "sklearn": False, "reason": "scikit-learn 未安装"}
    if not config.ML_ENABLED:
        return {"enabled": False, "sklearn": True, "reason": "LOTT_ML_ENABLED=0"}
    entry = ml_peek(draws)
    if entry is None:
        reason = "后台训练中…" if ml_training_in_progress() else "尚未训练（首个预测请求自动触发）"
        return {"enabled": True, "sklearn": True, "ready": False, "reason": reason}
    return {
        "enabled": True, "sklearn": True, "ready": True,
        "key": entry["key"], "trained_at": entry["trained_at"],
        "red_brier": entry["red_brier"], "blue_brier": entry["blue_brier"],
        "red_calibration_ece": entry["red"]["calibration"]["ece"],
        "blue_calibration_ece": entry["blue"]["calibration"]["ece"],
        "red_train_seconds": entry["red"]["train_seconds"],
        "blue_train_seconds": entry["blue"]["train_seconds"],
    }


def start_ml_warmup(draws: List[Dict]) -> None:
    """后台线程预热 ML 模型，避免首个预测请求阻塞。"""
    if not HAS_SKLEARN or not config.ML_ENABLED or not draws:
        return
    def _run():
        try:
            get_ml_models(draws)
        except Exception as e:  # noqa: BLE001
            print(f"[ml] 后台预热失败: {e}")
    t = threading.Thread(target=_run, daemon=True)
    t.start()


# ---------- 预测接口 ----------

def predict_red_probs(ml_red: Optional[Dict], draws: List[Dict],
                      normalize: bool = True) -> np.ndarray:
    """使用 ML 模型预测下期红球概率（33 维）。"""
    if ml_red is None or not HAS_SKLEARN or "models" not in ml_red:
        return np.full(33, 1.0 / 33)
    history = draws
    probs = np.zeros(33)
    for num in range(1, 34):
        if num in ml_red["models"]:
            feats = np.array([_compute_features(history, num)])
            try:
                probs[num - 1] = ml_red["models"][num].predict_proba(feats)[0, 1]
            except Exception:  # noqa: BLE001
                probs[num - 1] = 0.0
    s = probs.sum()
    if s <= 0:
        return np.full(33, 1.0 / 33)
    if normalize:
        probs = probs / s * 6  # 期望命中 6 个（与融合层一致）
    else:
        probs = probs / s
    return probs


def predict_blue_probs(ml_blue: Optional[Dict], draws: List[Dict]) -> np.ndarray:
    """使用 ML 模型预测下期蓝球概率（16 维，归一化为概率分布）。"""
    if ml_blue is None or not HAS_SKLEARN or "models" not in ml_blue:
        return np.full(16, 1.0 / 16)
    history = draws
    probs = np.zeros(16)
    for num in range(1, 17):
        if num in ml_blue["models"]:
            feats = np.array([_compute_blue_features(history, num)])
            try:
                probs[num - 1] = ml_blue["models"][num].predict_proba(feats)[0, 1]
            except Exception:  # noqa: BLE001
                probs[num - 1] = 0.0
    s = probs.sum()
    if s <= 0:
        return np.full(16, 1.0 / 16)
    return probs / s


def get_ml_metrics(ml_red: Optional[Dict], ml_blue: Optional[Dict]) -> Dict:
    """聚合 ML 模型评估指标（供 API/前端展示）。"""
    metrics = {}
    if ml_red and ml_red.get("metrics"):
        ms = list(ml_red["metrics"].values())
        metrics["red_avg_brier_raw"] = round(float(np.mean([m["brier_raw"] for m in ms])), 4)
        metrics["red_avg_brier_cal"] = round(float(np.mean([m["brier_cal"] for m in ms])), 4)
        metrics["red_avg_logloss_raw"] = round(float(np.mean([m["logloss_raw"] for m in ms])), 4)
        metrics["red_avg_logloss_cal"] = round(float(np.mean([m["logloss_cal"] for m in ms])), 4)
        metrics["red_ece"] = ml_red["calibration"]["ece"]
        metrics["red_n_test"] = int(sum(m["n_test"] for m in ms))
        metrics["red_calibration"] = ml_red["calibration"]["bins"]
    if ml_blue and ml_blue.get("metrics"):
        ms = list(ml_blue["metrics"].values())
        metrics["blue_avg_brier_raw"] = round(float(np.mean([m["brier_raw"] for m in ms])), 4)
        metrics["blue_avg_brier_cal"] = round(float(np.mean([m["brier_cal"] for m in ms])), 4)
        metrics["blue_avg_logloss_cal"] = round(float(np.mean([m["logloss_cal"] for m in ms])), 4)
        metrics["blue_ece"] = ml_blue["calibration"]["ece"]
        metrics["blue_n_test"] = int(sum(m["n_test"] for m in ms))
        metrics["blue_calibration"] = ml_blue["calibration"]["bins"]
    return metrics


# ---------- walk-forward 滚动评估（ML vs 均匀基线 + paired 检验） ----------

def evaluate_ml_walkforward(
    draws: List[Dict],
    min_start: Optional[int] = None,
    window: Optional[int] = None,
    refit_every: Optional[int] = None,
    train_window: Optional[int] = 800,
    seed: int = 7,
) -> Dict:
    """滚动评估 ML 概率质量：每 refit_every 期用此刻历史重训一次（防未来信息），
    逐期比较 ML 概率与均匀基线的 Brier / log-loss，并做 paired 显著性检验。

    train_window: 重训时最多使用最近 N 期历史（评估专用，控制耗时；
                  生产预测仍用全量历史训练）。
    """
    if not HAS_SKLEARN:
        return {"ok": False, "error": "scikit-learn 未安装，无法评估"}
    if not config.ML_ENABLED:
        return {"ok": False, "error": "ML 模型已停用（LOTT_ML_ENABLED=0）"}
    min_start = min_start or config.ML_MIN_START
    window = window or config.ML_EVAL_WINDOW
    refit_every = refit_every or config.ML_REFIT_EVERY

    if len(draws) < min_start + 15:
        return {"ok": False, "error": f"历史数据不足（{len(draws)} 期，需 ≥{min_start + 15}）"}

    start = max(min_start, len(draws) - window)
    t0 = time.time()

    red_ml_b, red_uni_b, blue_ml_b, blue_uni_b = [], [], [], []
    red_ml_ll, red_uni_ll, blue_ml_ll, blue_uni_ll = [], [], [], []
    cal_p, cal_y = [], []  # 红球 pooled
    cal_pb, cal_yb = [], []  # 蓝球 pooled
    refits = 0
    red_model = blue_model = None

    for i in range(start, len(draws)):
        history = draws[:i]
        target = draws[i]
        if (i - start) % refit_every == 0 and i > min_start + 5:
            hist = history[-train_window:] if train_window and len(history) > train_window else history
            tmin = min(min_start, max(50, int(len(hist) * 0.6)))
            red_model = train_red_models(hist, min_start=tmin)
            blue_model = train_blue_models(hist, min_start=tmin)
            refits += 1

        # 红球
        oh_r = np.zeros(33)
        for r in target["reds"]:
            oh_r[r - 1] = 1.0
        p_r = predict_red_probs(red_model, history, normalize=False)
        p_r = np.clip(p_r, 1e-6, 1 - 1e-6)
        u_r = np.full(33, 1.0 / 33)
        red_ml_b.append(float(np.mean((p_r - oh_r) ** 2)))
        red_uni_b.append(float(np.mean((u_r - oh_r) ** 2)))
        red_ml_ll.append(float(-np.mean(oh_r * np.log(p_r) + (1 - oh_r) * np.log(1 - p_r))))
        red_uni_ll.append(float(-np.mean(oh_r * np.log(u_r) + (1 - oh_r) * np.log(1 - u_r))))
        cal_p.extend(p_r.tolist())
        cal_y.extend(oh_r.tolist())

        # 蓝球
        oh_b = np.zeros(16)
        oh_b[target["blue"] - 1] = 1.0
        p_b = predict_blue_probs(blue_model, history)
        p_b = np.clip(p_b, 1e-6, 1 - 1e-6)
        u_b = np.full(16, 1.0 / 16)
        blue_ml_b.append(float(np.mean((p_b - oh_b) ** 2)))
        blue_uni_b.append(float(np.mean((u_b - oh_b) ** 2)))
        blue_ml_ll.append(float(-np.mean(oh_b * np.log(p_b) + (1 - oh_b) * np.log(1 - p_b))))
        blue_uni_ll.append(float(-np.mean(oh_b * np.log(u_b) + (1 - oh_b) * np.log(1 - u_b))))
        cal_pb.extend(p_b.tolist())
        cal_yb.extend(oh_b.tolist())

    if not red_ml_b:
        return {"ok": False, "error": "评估窗口为空"}

    def _pair_p(delta: List[float]) -> Dict:
        """delta>0 表示 ML 更优（基线 Brier 更大）。"""
        from scipy import stats as _st
        d = np.array(delta)
        nz = d[d != 0]
        if len(nz) == 0:
            return {"p": 1.0, "method": "no-difference", "mean_delta": 0.0}
        mean_d = float(np.mean(d))
        try:
            if len(nz) >= 5:
                res = _st.wilcoxon(nz)
                return {"p": round(float(res.pvalue), 4), "method": "wilcoxon",
                        "mean_delta": round(mean_d, 5)}
            pos = int(np.sum(nz > 0))
            pv = 2 * min(_st.binom.cdf(pos, len(nz), 0.5),
                         1 - _st.binom.cdf(pos - 1, len(nz), 0.5))
            return {"p": round(float(min(pv, 1.0)), 4), "method": "sign-test",
                    "mean_delta": round(mean_d, 5)}
        except Exception:  # noqa: BLE001
            return {"p": 1.0, "method": "n/a", "mean_delta": round(mean_d, 5)}

    res = {
        "ok": True,
        "n_issues": len(red_ml_b),
        "window": window,
        "refit_every": refit_every,
        "train_window": train_window,
        "refits": refits,
        "seconds": round(time.time() - t0, 1),
        "red": {
            "brier_ml": round(float(np.mean(red_ml_b)), 4),
            "brier_uniform": round(float(np.mean(red_uni_b)), 4),
            "logloss_ml": round(float(np.mean(red_ml_ll)), 4),
            "logloss_uniform": round(float(np.mean(red_uni_ll)), 4),
            "paired": _pair_p([round(a - b, 6) for a, b in zip(red_uni_b, red_ml_b)]),
            "calibration": _bin_metrics(np.array(cal_p), np.array(cal_y)).get("bins", []),
        },
        "blue": {
            "brier_ml": round(float(np.mean(blue_ml_b)), 4),
            "brier_uniform": round(float(np.mean(blue_uni_b)), 4),
            "logloss_ml": round(float(np.mean(blue_ml_ll)), 4),
            "logloss_uniform": round(float(np.mean(blue_uni_ll)), 4),
            "paired": _pair_p([round(a - b, 6) for a, b in zip(blue_uni_b, blue_ml_b)]),
            "calibration": _bin_metrics(np.array(cal_pb), np.array(cal_yb)).get("bins", []),
        },
        "conclusion": (
            "ML 概率与均匀基线通常无显著差异（随机彩票的正常结论）；"
            "Brier/log-loss/校准曲线用于量化模型是否被校准，而非承诺命中率提升。"
        ),
    }
    print(f"[ml] walk-forward 评估完成：{res['n_issues']} 期 / {refits} 次重训，"
          f"耗时 {res['seconds']}s")
    return res


# 兼容别名（供旧调用点使用）
train_red_model = train_red_models
train_blue_model = train_blue_models
