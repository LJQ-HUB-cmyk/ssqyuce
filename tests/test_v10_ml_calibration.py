"""V10-08：ML 校准诊断 + 置换重要性 vs 打乱标签对照 + 模型缓存治理。

规格：``docs/V10_IMPROVEMENT_PLAN.md`` §3.8（V10-08）、
``docs/reviews/R1_ARCHITECTURE_REVIEW.md`` V10-08 行 / P2-5。

本文件的立场（决定了每条断言为什么这么写）
--------------------------------------------
双色球开奖是独立随机事件。**在真实数据上，一个正确实现的概率模型应该输出接近
均匀分布、并且学不到任何东西**。所以「重要性很高」「校准出明显偏离均匀的分布」
不是模型厉害的证据，而是过拟合/数据泄漏的信号。反过来，本任务给 ML 通道一套
**能否被信任的判据**：

* 真实特征重要性必须**显著高于**「把 y 打乱后重训同样结构」的对照（max/mean
  统计量 + 置换检验 p 值）；不显著 → ``trusted=False``，通道降级 monitor；
* 打乱标签训练的模型**必须**给出不显著（自证，防止检验本身假阳性）；
* 注入真实信号时必须能检出（功效，防止检验形同虚设）；
* 可靠性图偏差必须落在二项抽样噪声带内（不把抽样波动当校准失败）；
* Brier 分解三项必须自洽（恒等式误差只允许浮点级）。

测试表
------
T1   Brier 分解恒等式（手工小例子）
T2   Brier 分解恒等式（随机大批量、多桶数）
T3   ``_bin_metrics`` 暴露分解三项且可靠性图分箱计数守恒
T4   可靠性曲线在纯噪声数据上落在二项噪声带内（z 默认 Bonferroni）
T5   ``reliability_noise_band`` 能识别出人为的校准偏差
T6   置换重要性对「唯一有信息的列」敏感（统计量本身正确）
T7   自证：打乱标签训练的模型 → ``importance_significant=False`` / ``trusted=False``
T8   功效：注入条件信号 → ``importance_significant=True`` / ``trusted=True``
T9   ``trusted`` 判据在噪声/信号两种数据上给出正确结论
T10  ``shuffled_label_test`` 对样本不足/单一类别优雅降级
T11  ``MODEL_SCHEMA_VERSION`` 变化 → cache key 变化（R1 P2-5 回归）
T12  cache key 随数据版本变化、同输入稳定
T13  ``_model_path`` 落在 ``DATA_DIR/ml_models`` 且文件名带版本号
T14  ``prune_model_cache`` 只保留最近 keep 个（按 mtime），dry_run 不删
T15  通道级诊断：纯随机开奖数据 → ``trusted=False``（诚实结论）
T16  通道级诊断：注入条件依赖 → ``trusted=True``（端到端功效）
T17  通道级诊断：联合零分布统计量与 per-number 明细结构完整
"""
from __future__ import annotations

import datetime as _dt
import random

import numpy as np
import pytest

from lottery import config, ml_model


# ---------- 公共构造 ----------

def _noise_xy(n: int = 2000, d: int = 20, seed: int = 0, rate: float = 0.18):
    """纯噪声数据集：y 与 X 完全独立。"""
    rng = np.random.RandomState(seed)
    X = rng.normal(size=(n, d))
    y = (rng.uniform(size=n) < rate).astype(float)
    return X, y, [f"f{i}" for i in range(d)]


def _signal_xy(n: int = 2000, d: int = 20, seed: int = 0):
    """条件信号数据集：y 只由第 0 列驱动（该列是「近期出现次数」，可解释性强）。"""
    rng = np.random.RandomState(seed)
    X = rng.normal(size=(n, d))
    X[:, 0] = rng.poisson(1.0, size=n)              # 0..6 的计数型特征
    p = np.clip(0.35 * (1.0 + 1.5 * X[:, 0]) / 4.0, 0.03, 0.90)
    y = (rng.uniform(size=n) < p).astype(float)
    return X, y, [f"f{i}" for i in range(d)]


def _make_random_draws(n: int = 700, seed: int = 42):
    """纯随机开奖数据（每期 6 个红球均匀抽样）。"""
    rng = random.Random(seed)
    start = _dt.date(2013, 1, 1)
    out = []
    for i in range(n):
        reds = sorted(rng.sample(range(1, 34), 6))
        out.append({
            "issue": f"{2013 + i // 150}{i % 150 + 1:03d}",
            "date": (start + _dt.timedelta(days=3 * i)).isoformat(),
            "reds": reds, "order": list(reds), "blue": rng.randint(1, 16),
        })
    return out


def _make_conditional_draws(n: int = 700, seed: int = 42, boost: float = 3.0,
                            lookback: int = 2):
    """注入**条件依赖**的开奖数据：近 ``lookback`` 期内出现过的号码，本期权重 × ``boost``。

    为什么用「条件依赖」而不是「给某号码加固定权重」：给某号码固定加权只改变它的
    **边际命中率**，特征与标签的条件分布不变——一个正确实现的模型本就学不到它，
    拿它做功效测试会得到「检测不出」的结论（这不是检验的缺陷，而是注入方式不对）。
    这里注入的是真正的「热号效应」（近期出现 → 本期概率升高），模型有可学习的结构。

    为什么用 0/1 的二元热号而不是「次数越多权重越高」：后者把信息压进罕见尾部
    （号码在 5 期内出现 ≥2 次是小概率），300~600 期样本里可检出效应太小；二元化
    让热/冷两组都有足够样本，功效测试才有意义。
    """
    rng = random.Random(seed)
    start = _dt.date(2013, 1, 1)
    out, recent = [], []
    for i in range(n):
        hot = {num: any(num in r for r in recent[-lookback:]) for num in range(1, 34)}
        pool, picked = list(range(1, 34)), []
        for _ in range(6):
            ws = [boost if hot[num] else 1.0 for num in pool]
            total = sum(ws)
            r = rng.uniform(0, total)
            acc, k = 0.0, 0
            for j, w in enumerate(ws):
                acc += w
                if acc >= r:
                    k = j
                    break
            picked.append(pool.pop(k))
        reds = sorted(picked)
        recent.append(reds)
        out.append({
            "issue": f"{2013 + i // 150}{i % 150 + 1:03d}",
            "date": (start + _dt.timedelta(days=3 * i)).isoformat(),
            "reds": reds, "order": list(reds), "blue": rng.randint(1, 16),
        })
    return out


# ---------- T1/T2：Brier 分解恒等式 ----------

def test_t1_brier_decomposition_identity_handmade():
    """T1：三个例子（完美校准 / 系统性高估 / 常数预测）都必须满足
    ``brier = reliability - resolution + uncertainty``（±1e-9）。"""
    cases = [
        (np.array([0.5, 0.5, 0.5, 0.5]), np.array([1.0, 0.0, 1.0, 0.0])),   # 校准良好
        (np.array([0.9, 0.9, 0.9, 0.9]), np.array([0.0, 1.0, 0.0, 0.0])),   # 系统性高估
        (np.array([0.2, 0.2, 0.2, 0.2]), np.array([0.0, 0.0, 1.0, 0.0])),   # 常数预测
    ]
    for p, y in cases:
        d = ml_model.brier_decomposition(p, y, n_bins=10)
        assert abs(d["reliability"] - d["resolution"] + d["uncertainty"] - d["brier"]) < 1e-9
        assert d["identity_gap"] == pytest.approx(0.0, abs=1e-9)
        assert d["uncertainty"] == pytest.approx(float(np.mean(y) * (1 - np.mean(y))), abs=1e-9)


def test_t2_brier_decomposition_identity_random():
    """T2：随机数据 × 多桶数，恒等式必须始终成立（±1e-9），且各项非负。"""
    rng = np.random.RandomState(7)
    for n_bins in (3, 5, 10, 20):
        for seed in (1, 2, 3):
            p = rng.uniform(0.02, 0.98, size=1500)
            y = (rng.uniform(size=1500) < 0.3).astype(float)
            d = ml_model.brier_decomposition(p, y, n_bins=n_bins)
            assert abs(d["reliability"] - d["resolution"] + d["uncertainty"]
                       - d["brier"]) < 1e-9, (n_bins, seed, d)
            assert d["reliability"] >= -1e-12
            assert d["resolution"] >= -1e-12
            assert d["uncertainty"] >= -1e-12


# ---------- T3：分箱与分解一起暴露 ----------

def test_t3_bin_metrics_exposes_decomposition_and_conserves_counts():
    """T3：``_bin_metrics`` 既要给可靠性图（含样本量），也要给分解三项与 ECE；
    分箱样本量之和必须等于总样本量（不许丢样本）。"""
    rng = np.random.RandomState(3)
    p = rng.uniform(0.05, 0.95, size=997)
    y = (rng.uniform(size=997) < 0.4).astype(float)
    out = ml_model._bin_metrics(p, y, n_bins=10)
    for key in ("bins", "ece", "brier", "reliability", "resolution",
                "uncertainty", "brier_sample", "brier_decomposition"):
        assert key in out, key
    assert sum(int(b["n"]) for b in out["bins"]) == 997
    assert len(out["bins"]) == 10
    # 逐样本 Brier 与分箱 Brier 只差「箱内预测方差项」，必为非负
    assert out["brier_sample"] >= out["brier"] - 1e-12
    assert abs(out["brier_decomposition"]["brier"] - out["brier"]) < 1e-9


# ---------- T4/T5：可靠性图的噪声带 ----------

def test_t4_reliability_curve_within_noise_band_on_pure_noise():
    """T4：纯噪声数据上（模型什么都没学到）可靠性曲线必须落在二项噪声带内。

    这是「不把抽样波动当校准失败」的自证：如果这里报出置信的校准偏差，说明噪声带
    的判定过严（例如 10 个桶各自 z=2 却不做多重比较校正，假警报率约 37%）。
    """
    X, y, names = _noise_xy(n=4000, seed=11)
    res = ml_model.shuffled_label_test(
        X, y, names, n_repeats=2, n_null=2, n_estimators=25, max_depth=2)
    band = res["calibration"]["noise_band"]
    assert band["within_noise_band"] is True, band
    assert band["n_outside"] == 0
    assert band["n_checked"] >= 1
    # Bonferroni 校正后的 z 应大于 2（10 桶 → 2.81）
    assert band["z"] >= 2.0
    # 纯噪声下「净技能」应接近 0（real 上不应出现明显 resolution 优势）
    assert res["calibration"]["resolution"] < 0.01


def test_t5_noise_band_flags_deliberate_miscalibration():
    """T5：人为把预测整体放大 3 倍 → 噪声带必须报出校准偏差（带不是永远为真）。

    注意噪声带本身是一个 **α=0.05 的检验**（Bonferroni 后族错误率 5%），所以「校准
    良好的数据恰好落带外」本身就会以 ~5% 概率发生，不能用单次「在带内」当断言。
    这里改成断言**区分度**：校准良好时偏差与带边界的距离很小，系统性高估时偏差
    远超边界。
    """
    rng = np.random.RandomState(5)
    p_true = rng.uniform(0.1, 0.4, size=3000)
    y = (rng.uniform(size=3000) < p_true).astype(float)
    good = ml_model._bin_metrics(p_true, y)
    bad = ml_model._bin_metrics(np.clip(p_true * 3.0, 0.0, 1.0), y)
    gb = ml_model.reliability_noise_band(good["bins"])
    bb = ml_model.reliability_noise_band(bad["bins"])
    assert gb["n_outside"] <= 1, gb                       # 最多一个抽样波动
    assert gb["worst_excess"] < 0.01, gb                  # 良好校准：几乎贴着带边界
    assert bb["within_noise_band"] is False
    assert bb["n_outside"] >= 2 and bb["worst_excess"] > 0.1, bb


# ---------- T6：置换重要性统计量本身 ----------

def test_t6_permutation_importance_finds_the_only_informative_column():
    """T6：21 列里只有第 0 列有信息 → 重要性必须把第 0 列排到第一且远大于其余。"""
    rng = np.random.RandomState(13)
    n, d = 1200, 21
    X = rng.normal(size=(n, d))
    logit = 2.0 * X[:, 0] - 1.5
    y = (rng.uniform(size=n) < 1 / (1 + np.exp(-logit))).astype(float)
    clf = ml_model._make_diag_learner(25, 2, seed=42)
    clf.fit(X[:900], y[:900])
    pi = ml_model.permutation_importance(clf, X[900:], y[900:],
                                         [f"f{i}" for i in range(d)],
                                         n_repeats=4, seed=1)
    rows = sorted(pi["importances"], key=lambda r: -r["importance"])
    assert rows[0]["feature"] == "f0"
    assert rows[0]["importance"] > 5 * max(r["importance"] for r in rows[1:])


# ---------- T7/T8/T9：核心判据（自证 / 功效 / trusted） ----------

_SELF_CHECK_SEEDS = (0, 1, 7, 99, 2024)


@pytest.fixture(scope="module")
def shuffled_label_runs():
    """自证：对同一份数据把 y **整列打乱**（打乱标签训练）后跑 K 次检验。

    为什么不是只跑一次：``shuffled_label_test`` 是一个 α=0.05 的检验，单次运行本身
    就有 5% 概率（正确地）报显著——单次断言「必须不显著」等于要求检验假阳性率为 0，
    那反而会让检验失去功效。正确的自证方式是看**多个独立打乱**下的拒绝率是否被
    控制在 α 附近（期望 ≤1/5 次）。
    """
    X, y, names = _signal_xy(n=1000, seed=0)
    runs = []
    for seed in _SELF_CHECK_SEEDS:
        y_shuffled = np.random.RandomState(seed).permutation(y)
        runs.append(ml_model.shuffled_label_test(
            X, y_shuffled, names, n_repeats=2, n_null=20,
            n_estimators=15, max_depth=2))
    return runs


@pytest.fixture(scope="module")
def injected_signal_result():
    """功效：数据里真的种了条件信号 → 检验必须能检出。"""
    X, y, names = _signal_xy(n=2000, seed=0)
    return ml_model.shuffled_label_test(
        X, y, names, n_repeats=3, n_null=20, n_estimators=25, max_depth=2)


def test_t7_shuffled_label_control_is_not_significant(shuffled_label_runs):
    """T7（自证）：打乱标签训练出来的模型，重要性**不应**显著高于打乱标签对照。

    断言拒绝率被控制在 α 附近（5 次里至多 1 次），而不是「必须一次都不显著」；
    若这里超过 1 次，说明检验在纯噪声上就频繁假阳性，整个 V10-08 判据作废。
    """
    assert all(r["ok"] is True for r in shuffled_label_runs)
    n_sig = sum(1 for r in shuffled_label_runs if r["importance_significant"])
    assert n_sig <= 1, [(r["p_value"], r["best_feature"]) for r in shuffled_label_runs]
    for r in shuffled_label_runs:
        assert r["p_value"] >= r["min_attainable_p"] - 1e-12
        assert r["tier"] in ("monitor", "signal_found")
        # 有信号的可信度判据必须与 significance 一致（不允许自相矛盾）
        assert r["trusted"] is r["importance_significant"]


def test_t8_injected_signal_is_detected(injected_signal_result):
    """T8（功效）：注入的条件信号必须被检出（否则判据没有意义）。"""
    r = injected_signal_result
    assert r["ok"] is True
    assert r["importance_significant"] is True, (r["p_value"], r["best_feature"])
    assert r["p_value"] < r["alpha"]
    assert r["best_feature"] == "f0"
    assert r["best_importance"] > r["null_max_p95"]


def test_t9_trusted_verdict_is_correct_on_both_datasets(shuffled_label_runs,
                                                        injected_signal_result):
    """T9：``trusted`` 判据在噪声数据上基本为 False、在有信号数据上必须为 True。"""
    noisy_trusted = sum(1 for r in shuffled_label_runs if r["trusted"])
    assert noisy_trusted <= 1, [(r["p_value"], r["best_feature"]) for r in shuffled_label_runs]
    assert injected_signal_result["trusted"] is True
    assert injected_signal_result["tier"] == "signal_found"
    for r in list(shuffled_label_runs) + [injected_signal_result]:
        assert isinstance(r["reason"], str) and r["reason"]
        assert isinstance(r["reasons"], list) and r["reasons"]
        assert isinstance(r["warnings"], list)
    # 单特征 p 值表必须齐全、且与「显著性」一致
    table = injected_signal_result["importance"]
    assert len(table) == len(injected_signal_result["feature_names"])
    assert table[0]["feature"] == "f0"
    assert table[0]["significant"] is True
    assert all("p_value" in row and "null_p95" in row for row in table)


# ---------- T10：边界降级 ----------

def test_t10_shuffled_label_test_degrades_gracefully():
    """T10：样本不足 / 单一类别 / 空输入 → ok=False 且 trusted=False（不抛异常）。"""
    X, y, names = _noise_xy(n=10, d=3, seed=1)
    r = ml_model.shuffled_label_test(X, y, names, n_null=2)
    assert r["ok"] is False and r["trusted"] is False and r["tier"] == "monitor"

    X2 = np.random.RandomState(0).normal(size=(200, 3))
    r2 = ml_model.shuffled_label_test(X2, np.zeros(200), ["a", "b", "c"], n_null=2)
    assert r2["ok"] is False and r2["trusted"] is False

    r3 = ml_model.shuffled_label_test(np.zeros((0, 3)), np.zeros(0), ["a", "b", "c"])
    assert r3["ok"] is False and "空数据集" in r3["error"]


# ---------- T11/T12/T13：cache key 与 MODEL_SCHEMA_VERSION ----------

def test_t11_cache_key_changes_with_model_schema_version(make_draws, monkeypatch):
    """T11（R1 P2-5 回归）：``MODEL_SCHEMA_VERSION`` 一变，cache key 必须变。

    历史缺陷：``_cache_key`` 只有 ``"{期数}:{末期号}"``，改特征定义后 key 不变 →
    ``data/ml_models/<key>.joblib``（55MB/个）被静默复用，出现「改了没生效」。
    """
    draws = make_draws(120, seed=5)
    monkeypatch.setattr(config, "MODEL_SCHEMA_VERSION", "v10-08-a")
    key_a = ml_model._cache_key(draws)
    monkeypatch.setattr(config, "MODEL_SCHEMA_VERSION", "v10-08-b")
    key_b = ml_model._cache_key(draws)
    assert key_a != key_b
    assert "v10-08-a" in key_a and "v10-08-b" in key_b
    # 路径也要跟着变，否则仍会命中旧文件
    assert ml_model._model_path(key_a) != ml_model._model_path(key_b)


def test_t12_cache_key_depends_on_data_and_is_stable(make_draws, monkeypatch):
    """T12：key 随（期数, 末期号）变化；同样输入必须稳定（可复现）。"""
    monkeypatch.setattr(config, "MODEL_SCHEMA_VERSION", "1")
    d100 = make_draws(100, seed=1)
    d150 = make_draws(150, seed=1)
    assert ml_model._cache_key(d100) != ml_model._cache_key(d150)
    assert ml_model._cache_key(d100) == ml_model._cache_key(list(d100))
    assert ml_model._cache_key([]) is None
    # 版本号是 key 的前缀组成（便于人工排查是哪个版本训的）
    assert ml_model._cache_key(d100).startswith("v1:")


def test_t13_model_path_lives_under_data_dir(isolated_env, monkeypatch):
    """T13：模型缓存必须落在当前 DATA_DIR/ml_models（不写死生产路径）。"""
    monkeypatch.setattr(config, "MODEL_SCHEMA_VERSION", "3")
    key = ml_model._cache_key([{"issue": "2013001"}])
    path = ml_model._model_path(key)
    assert path.parent == ml_model.model_cache_dir()
    assert path.parent == config.DATA_DIR / "ml_models"
    assert path.name == f"{key}.joblib"


# ---------- T14：磁盘治理 ----------

def test_t14_prune_model_cache_keeps_newest_and_never_touches_draws(isolated_env):
    """T14：``prune_model_cache(keep=2)`` 只留最近 2 个 joblib，并报告释放空间。

    安全性：``data/ml_models/*.joblib`` 是**可重训缓存**（由 ``data/ssq.db`` 的开奖
    历史训练得到），不是源数据；因此删除只影响「下次要重训」，不会丢数据。
    非 joblib 文件（例如 .gitkeep / 日志）必须不动。
    """
    d = ml_model.model_cache_dir()
    d.mkdir(parents=True, exist_ok=True)
    files = []
    for i in range(5):
        f = d / f"v1:{i}.joblib"
        f.write_bytes(b"x" * (10 * (i + 1)))
        import os
        os.utime(f, (1_000_000 + i, 1_000_000 + i))  # 明确 mtime 顺序
        files.append(f)
    keep_marker = d / "README.txt"
    keep_marker.write_text("not a model cache")

    dry = ml_model.prune_model_cache(keep=2, dry_run=True)
    assert len(dry["kept"]) == 2 and len(dry["removed"]) == 3
    assert all(f.exists() for f in files), "dry_run 不得删文件"

    out = ml_model.prune_model_cache(keep=2)
    assert len(out["kept"]) == 2 and len(out["removed"]) == 3
    assert out["freed_bytes"] == dry["freed_bytes"] > 0
    # 删掉的是最旧的 3 个，保留最新的 2 个
    assert set(out["removed"]) == {f.name for f in files[:3]}
    assert set(out["kept"]) == {f.name for f in files[3:]}
    assert not files[0].exists() and not files[1].exists() and not files[2].exists()
    assert files[3].exists() and files[4].exists()
    assert keep_marker.exists(), "非 joblib 文件不得被清理"


# ---------- T15/T16/T17：通道级诊断（端到端） ----------

@pytest.fixture(scope="module")
def channel_random_result():
    draws = _make_random_draws(700, seed=42)
    return ml_model.diagnose_ml_channel(
        draws, min_start=300, numbers=[1, 7, 14, 20],
        n_repeats=3, n_null=20, n_estimators=20, max_depth=2)


@pytest.fixture(scope="module")
def channel_signal_result():
    draws = _make_conditional_draws(700, seed=42, boost=3.0)
    return ml_model.diagnose_ml_channel(
        draws, min_start=300, numbers=[1, 7, 14, 20],
        n_repeats=3, n_null=20, n_estimators=20, max_depth=2)


def test_t15_channel_diagnosis_is_untrusted_on_pure_random_draws(channel_random_result):
    """T15（诚实结论）：纯随机开奖数据上，ML 通道必须被判为不可信。

    这正是生产环境的预期结果：双色球没有可学习的结构，谁声称有就是过拟合。
    """
    r = channel_random_result
    assert r["ok"] is True
    assert r["importance_significant"] is False, (r["p_value"], r["T_mean"])
    assert r["trusted"] is False
    assert r["tier"] == "monitor"
    assert "monitor" in r["reason"]


def test_t16_channel_diagnosis_detects_injected_conditional_dependence(channel_signal_result):
    """T16（端到端功效）：注入「近期频率 → 本期概率」的条件依赖 → 通道必须检出。"""
    r = channel_signal_result
    assert r["ok"] is True
    assert r["importance_significant"] is True, (
        r["p_value"], r["T_mean"], r["null_T_mean_p95"], r["numbers"])
    assert r["trusted"] is True
    assert r["tier"] == "signal_found"


def test_t17_channel_diagnosis_structure(channel_random_result, channel_signal_result):
    """T17：结论块结构完整、校准摘要与 per-number 明细自洽。"""
    for r in (channel_random_result, channel_signal_result):
        for key in ("channel", "color", "numbers", "per_number", "p_value",
                    "p_value_mean_stat", "p_value_max_stat", "T_mean", "T_max",
                    "null_T_mean_p95", "null_T_max_p95", "min_attainable_p",
                    "n_null", "importance_significant", "trusted", "tier",
                    "reason", "calibration_summary", "n_samples_per_number",
                    "power_note"):
            assert key in r, key
        assert r["channel"] == "ml_red"
        assert len(r["per_number"]) == len(r["numbers"]) == 4
        # 主判据 p 值取「平均号码内最强重要性」统计量
        assert r["p_value"] == r["p_value_mean_stat"]
        assert 0.0 < r["min_attainable_p"] <= 0.05
        cs = r["calibration_summary"]
        assert cs["n_numbers"] == 4
        # 逐号码的分解三项非负、分箱齐全（精确恒等式由 T1/T2 在函数层断言）
        for d in r["per_number"]:
            assert d["n_test"] >= 1 and d["n_samples"] > d["n_test"]
            assert d["best_feature"] in ml_model._FEATURE_NAMES
            assert len(d["calibration"]) == 10
            assert d["reliability"] >= -1e-12 and d["resolution"] >= -1e-12
            assert d["uncertainty"] > 0
            assert 0.0 <= d["ece"] <= 1.0
            assert d["top_features"]
            assert d["best_importance"] == pytest.approx(d["top_features"][0]["importance"])

    # 纯随机数据的统计量应低于有信号数据（判据的区分度，不只是单个阈值）
    assert channel_signal_result["T_mean"] > channel_random_result["T_mean"]
