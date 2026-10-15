"""V4-B：融合权重回归测试 —— 样本内 Brier + 1/brier 倒数放大 → walk-forward 样本外 + softmax + 上限 + 收缩。

背景（被证实的算法缺陷）：
    `engine._brier_blend` 原实现用**最近 50 期**计算样本内 Brier，再以
    `weights = max(0.01, 1.0 / max(brier/count, 0.001))` 定权。三重错误叠加：
      (a) 样本内：模型本身就是在这些期上拟合/平滑出来的，Brier 系统性偏乐观；
      (b) 1/brier 无上界：样本内 brier→0 时权重→+∞（brier=0.001 时约 149x 于
          均匀模型），是追逐噪声而非贝叶斯融合；
      (c) 无权重上限、无收缩：小样本抖动直接变成单一方法独裁。

    本文件锁定修复后的契约。其中 `test_leaky_model_does_not_dominate_weights`
    与 `test_weights_are_softmax_not_inverse_brier` 是**回归防护**：
    旧逻辑在这两例上必挂。
"""
from __future__ import annotations

import math
import random

import numpy as np
import pytest

from lottery import engine as E
from lottery import models as M


def _mk_draws(n: int = 400, seed: int = 7):
    """合成 n 期独立均匀开奖（双色球诚实口径：无可预测结构）。"""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        reds = sorted(rng.sample(range(1, 34), 6))
        out.append({
            "issue": f"{2026001 + i}",
            "date": f"2026-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}",
            "reds": reds,
            "blue": rng.randint(1, 16),
        })
    return out


def _leaky_model(draws, window: int = 50, n: int = 33):
    """构造一个「在评估窗口上样本内近乎完美」的作弊模型。

    它把最近 window 期的真实开奖直接编码进概率——等价于旧逻辑的极端过拟合
    情形（模型与评估数据同源）。它在样本外用任何预测力都换不来。
    """
    p = np.full(n, 0.01)
    for t in draws[-window:]:
        for r in t["reds"]:
            p[r - 1] += 1.0
    return p / p.sum() * 6


def _old_inverse_brier_weights(red_pool, draws, n_eval: int = 50):
    """逐字复刻旧逻辑，用于证明回归测试确实能抓住缺陷（新逻辑必与之不同）。"""
    w = {}
    for name, red_p in red_pool.items():
        brier, count = 0.0, 0
        for i in range(-n_eval, 0):
            tr = np.zeros(33)
            for r in draws[i]["reds"]:
                tr[r - 1] = 1.0
            brier += float(np.mean((np.asarray(red_p) - tr) ** 2))
            count += 1
        w[name] = max(0.01, 1.0 / max(brier / count, 0.001)) if count else 0.01
    tot = sum(w.values()) or 1.0
    return {k: v / tot for k, v in w.items()}


# ---------------------------------------------------------------------------
# 1. 回归防护：样本内极好 / 样本外无信息 的模型不得拿到压倒性权重
# ---------------------------------------------------------------------------

def test_leaky_model_does_not_dominate_weights():
    """回归防护（旧逻辑必挂）。

    注入一个在最近 50 期「样本内完美」的作弊模型。它无法用历史重建
    （不是 build_models 的成员）→ 必须走 fixed-fallback 等权参与，
    绝不允许靠样本内 Brier 抢到压倒性权重。
    """
    draws = _mk_draws(400)
    pool = dict(M.build_models(draws))
    cheat = _leaky_model(draws, window=50)
    pool["cheat"] = {"red": cheat, "blue": np.full(16, 1 / 16), "name": "cheat"}

    red_blend, _ = E._brier_blend(pool, draws)
    meta = red_blend.attrs
    weights = meta["weights"]["red"]

    # 作弊模型不得独裁：权重不超过上限，且不超过「均匀份额」的 1.5 倍
    uniform_share = 1.0 / len(weights)
    assert weights["cheat"] <= E.MAX_MODEL_WEIGHT
    assert weights["cheat"] <= uniform_share * 1.5, (
        f"作弊模型权重 {weights['cheat']:.4f} 相对均匀份额 {uniform_share:.4f} 过高")
    # 且必须被如实标注为不可重建 / fixed-fallback
    assert "cheat" in meta["fallback_methods"]["red"]
    assert meta["raw_briers"]["red"]["cheat"] is None
    assert meta["weight_source"] == "fixed-fallback"


def test_weights_are_softmax_not_inverse_brier():
    """回归防护（旧逻辑必挂）：两个方法 Brier 相差一个数量级时，权重比必须受限。

    旧逻辑 w ∝ 1/brier → brier 差 10 倍则权重差 10 倍。
    新逻辑 w ∝ exp(-brier/T) 且经上限+收缩，权重比被严格压抑。
    """
    draws = _mk_draws(400)
    good_brier, bad_brier = 0.0148, 0.148
    w = E._softmax_weights({"good": good_brier, "bad": bad_brier},
                           E.BLEND_SOFTMAX_TEMP_RED)
    assert abs(sum(w.values()) - 1.0) < 1e-9
    # 旧逻辑在同样的 Brier 差下会给出 10x
    old = _old_inverse_brier_weights(
        {"good": np.full(33, 1 / 33), "bad": np.full(33, 1 / 33)}, draws)
    assert math.isclose(old["good"] / old["bad"], 1.0, rel_tol=1e-6)  # 旧逻辑对常数向量无法区分
    # 新逻辑：权重比被温度与上限严格压抑
    assert w["good"] / w["bad"] < 5.0, f"权重比 {w['good']/w['bad']:.2f} 过大，疑似回到倒数放大"
    # 上限是**最终**权重上限（收缩之后仍不得越界）
    assert w["good"] <= E.MAX_MODEL_WEIGHT + 1e-9
    # 收缩把权重拉向均匀（0.5）：未触顶时 raw 越大权重才越大，但不得突破上限
    assert 0.5 <= w["good"] <= E.MAX_MODEL_WEIGHT + 1e-9


def test_cap_binds_after_shrinkage_on_extreme_spread():
    """极端 Brier 差（100x）时上限必须真正生效，而不是被收缩掩盖。"""
    t = E.BLEND_SOFTMAX_TEMP_RED
    w = E._softmax_weights({"great": 0.001, "awful": 0.5}, t)
    assert max(w.values()) <= E.MAX_MODEL_WEIGHT + 1e-9
    assert math.isclose(max(w.values()), E.MAX_MODEL_WEIGHT, rel_tol=1e-9)
    assert abs(sum(w.values()) - 1.0) < 1e-9
    # 若没有上限，收缩后的数据权重会达到 ~0.69 —— 上限把它压回 0.6
    raw = np.exp(np.array([-0.001 / t, -0.5 / t]))
    raw = raw / raw.sum()
    unshrunken = (1 - E.WEIGHT_SHRINKAGE) * 0.5 + E.WEIGHT_SHRINKAGE * raw[0]
    assert unshrunken > E.MAX_MODEL_WEIGHT


def test_softmax_weights_match_documented_formula():
    """softmax 公式与文档一致：w ∝ exp(-brier/T)，再 (1-λ)·uniform + λ·data（最后套上限）。"""
    t = E.BLEND_SOFTMAX_TEMP_RED
    briers = {"a": 0.10, "b": 0.20}
    w = E._softmax_weights(briers, t)
    raw = np.exp(np.array([-0.10 / t, -0.20 / t]))
    raw = raw / raw.sum()
    lam = E.WEIGHT_SHRINKAGE
    expected = _cap_weights_ref((1 - lam) * 0.5 + lam * raw, E.MAX_MODEL_WEIGHT)
    assert math.isclose(w["a"], float(expected[0]), rel_tol=1e-6)
    assert math.isclose(w["b"], float(expected[1]), rel_tol=1e-6)
    assert abs(sum(w.values()) - 1.0) < 1e-9


def _cap_weights_ref(w, cap):
    """测试侧独立复刻上限语义（迭代截断 + 按比例回补）。"""
    w = np.asarray(w, dtype=float).copy()
    n = w.size
    if cap * n <= 1.0:
        return np.full(n, 1.0 / n)
    for _ in range(n + 1):
        over = w > cap + 1e-12
        if not over.any():
            break
        excess = float((w[over] - cap).sum())
        w[over] = cap
        free = ~over & (w < cap)
        if not free.any():
            break
        w[free] += excess * (w[free] / w[free].sum())
    w = np.clip(w, 0.0, cap)
    return w / w.sum()


# ---------------------------------------------------------------------------
# 2. 权重上限与收缩
# ---------------------------------------------------------------------------

def test_no_method_exceeds_max_model_weight():
    """任何方法权重都不得超过 MAX_MODEL_WEIGHT（默认 0.6）。"""
    draws = _mk_draws(400)
    pool = dict(M.build_models(draws))
    # 注入一个极端尖锐的模型：单点压 6，试图霸占融合
    spike = np.full(33, 1e-6)
    spike[0] = 6.0
    pool["spike"] = {"red": spike, "blue": np.full(16, 1 / 16), "name": "spike"}

    red_blend, blue_blend = E._brier_blend(pool, draws)
    for name, v in red_blend.attrs["weights"]["red"].items():
        assert v <= E.MAX_MODEL_WEIGHT + 1e-9, f"{name} 权重 {v} 超过上限"
    for name, v in red_blend.attrs["weights"]["blue"].items():
        assert v <= E.MAX_MODEL_WEIGHT + 1e-9, f"{name} 蓝球权重 {v} 超过上限"
    assert E.MAX_MODEL_WEIGHT == 0.6


def test_weight_shrinkage_pulls_toward_uniform():
    """收缩生效：λ=0.5 时数据权重只能把权重拉动一半，最小权重有下界。"""
    draws = _mk_draws(400)
    pool = dict(M.build_models(draws))
    red_blend, _ = E._brier_blend(pool, draws)
    weights = list(red_blend.attrs["weights"]["red"].values())
    n = len(weights)
    uniform = 1.0 / n
    # 收缩保证没有任何方法被压到 0；每个权重至少是均匀份额的一半（λ=0.5）
    assert min(weights) >= 0.5 * uniform - 1e-9, f"存在被过度压制的权重: {min(weights)}"
    assert red_blend.attrs["shrinkage"] == 0.5
    assert abs(sum(weights) - 1.0) < 1e-6


def test_cap_weights_redistributes_without_exceeding_cap():
    """_cap_weights 把超上限的质量按比例回补，且不制造新的越界。"""
    out = E._cap_weights(np.array([0.95, 0.03, 0.02]), 0.6)
    assert abs(float(out.sum()) - 1.0) < 1e-9
    assert float(out.max()) <= 0.6 + 1e-12
    # cap 不可行（cap*n <= 1）时退化为均匀
    flat = E._cap_weights(np.array([0.9, 0.1]), 0.4)
    assert np.allclose(flat, 0.5)


# ---------------------------------------------------------------------------
# 3. 不可重建方法 → fixed-fallback 且被正确标注
# ---------------------------------------------------------------------------

def test_ml_like_method_falls_back_and_is_flagged():
    """无法用历史回放的方法（模拟 ml 通道）→ 等权参与且标注 fixed-fallback。"""
    draws = _mk_draws(400)
    rng = np.random.default_rng(3)
    pool = dict(M.build_models(draws))
    mlp = rng.random(33) + 0.1
    pool["ml"] = {"red": mlp / mlp.sum() * 6, "blue": np.full(16, 1 / 16), "name": "ml"}

    red_blend, _ = E._brier_blend(pool, draws)
    meta = red_blend.attrs
    assert meta["weight_source"] == "fixed-fallback"
    assert "ml" in meta["fallback_methods"]["red"]
    assert meta["raw_briers"]["red"]["ml"] is None
    # 可重建的方法仍由样本外 Brier 定权（有真实数值）
    assert meta["raw_briers"]["red"]["freq"] is not None
    assert meta["raw_briers"]["red"]["freq"] > 0


def test_all_reconstructible_methods_report_walk_forward():
    """模型池全部可重建时，weight_source 必须是 walk-forward。"""
    draws = _mk_draws(400)
    pool = M.build_models(draws)
    red_blend, _ = E._brier_blend(pool, draws)
    meta = red_blend.attrs
    assert meta["weight_source"] == "walk-forward"
    assert meta["n_eval"] == 50
    assert meta["fallback_methods"]["red"] == []
    for name in pool:
        assert meta["raw_briers"]["red"][name] is not None
        assert meta["weights"]["red"][name] > 0


# ---------------------------------------------------------------------------
# 4. walk-forward 严格性
# ---------------------------------------------------------------------------

def test_walk_forward_never_uses_future_data():
    """严格样本外：截断历史必须改变评估结果（证明评估确实只用过去的数据）。

    若实现偷看未来，那么把「未来部分」替换成完全不同的开奖，
    评估出的 Brier 也不会变。这里断言它会变。
    """
    draws = _mk_draws(400, seed=11)
    n_eval = 50
    # 池里给出「声明维度」；算分一律用历史重建结果
    pool = {"freq": {"red": np.zeros(33), "blue": np.zeros(16)}}
    wf_a = E._walk_forward_brier(pool, draws, n_eval)
    # 把评估窗口内的开奖整体换掉（未来变了）
    mutated = [dict(d) for d in draws]
    rng = random.Random(999)
    for i in range(len(draws) - n_eval, len(draws)):
        mutated[i] = {**mutated[i], "reds": sorted(rng.sample(range(1, 34), 6))}
    wf_b = E._walk_forward_brier(pool, mutated, n_eval)
    assert wf_a["freq"]["red"] != wf_b["freq"]["red"], "评估结果未随评估期真实开奖变化"

    # 而改「评估窗口之前、但落在重建读取范围内」的历史也必须改变结果。
    # 注意：freq_model 只看最近 30 期，评估从 t=350 起 → t=350 时读取 d[320:350]。
    # 故这里改 d[320:350]（真正被重建读到的历史），而不是 d[0:50]（任何窗口都不读）。
    mutated2 = [dict(d) for d in draws]
    for i in range(len(draws) - n_eval - 30, len(draws) - n_eval):
        mutated2[i] = {**mutated2[i], "reds": [1, 2, 3, 4, 5, 6]}
    wf_c = E._walk_forward_brier(pool, mutated2, n_eval)
    assert wf_a["freq"]["red"] != wf_c["freq"]["red"], "重建未使用历史数据"


def test_walk_forward_brier_is_above_in_sample_brier():
    """样本外 Brier 必须 ≥（不优于）样本内 Brier —— 这正是旧逻辑的乐观偏差。"""
    draws = _mk_draws(400, seed=5)
    n_eval = 50
    wf = E._walk_forward_brier({"freq": {"red": np.zeros(33), "blue": np.zeros(16)}},
                               draws, n_eval)
    oos = wf["freq"]["red"]

    # 样本内：用全量历史拟合出的 freq 概率，去评估同一批期
    in_sample_model = M.freq_model(draws)
    p = np.asarray(in_sample_model["red"], dtype=float)
    ins = 0.0
    for i in range(len(draws) - n_eval, len(draws)):
        tr = np.zeros(33)
        for r in draws[i]["reds"]:
            tr[r - 1] = 1.0
        ins += float(np.mean((p - tr) ** 2))
    ins /= n_eval
    assert oos >= ins - 1e-12, f"样本外 Brier {oos} 竟优于样本内 {ins}，评估泄漏了未来"


# ---------------------------------------------------------------------------
# 5. 归一化性质
# ---------------------------------------------------------------------------

def test_normalization_and_no_nan():
    """sum(red)=6、sum(blue)=1、无 NaN/负值。"""
    draws = _mk_draws(400)
    pool = dict(M.build_models(draws))
    red_blend, blue_blend = E._brier_blend(pool, draws)

    assert red_blend.shape == (33,)
    assert blue_blend.shape == (16,)
    assert math.isclose(float(red_blend.sum()), 6.0, rel_tol=1e-9)
    assert math.isclose(float(blue_blend.sum()), 1.0, rel_tol=1e-9)
    assert np.isfinite(red_blend).all() and np.isfinite(blue_blend).all()
    assert (red_blend >= 0).all() and (blue_blend >= 0).all()
    assert (blue_blend > 0).all(), "蓝球不应出现 0 概率（blue_specialist 恒在混合内）"


def test_normalization_holds_under_adaptive_and_degenerate_pools():
    """adaptive 打折与退化模型池下归一化仍成立。"""
    draws = _mk_draws(400)
    pool = M.build_models(draws)
    red_blend, blue_blend = E._brier_blend(pool, draws, adaptive={"freq": 0.5, "markov": 0.0})
    assert math.isclose(float(red_blend.sum()), 6.0, rel_tol=1e-9)
    assert math.isclose(float(blue_blend.sum()), 1.0, rel_tol=1e-9)
    assert np.isfinite(red_blend).all()

    # 只有均匀模型
    rb, bb = E._brier_blend({"uniform": M.uniform_model()}, draws)
    assert math.isclose(float(rb.sum()), 6.0, rel_tol=1e-9)
    assert math.isclose(float(bb.sum()), 1.0, rel_tol=1e-9)


# ---------------------------------------------------------------------------
# 6. 数据不足优雅退化
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("n", [0, 1, 5, 50, 120, 299])
def test_insufficient_data_degrades_gracefully(n):
    """数据不足（<300 期）时不崩、归一化正确、标注 insufficient_history。"""
    draws = _mk_draws(n) if n else []
    pool = M.build_models(draws) if draws else {"uniform": M.uniform_model()}
    red_blend, blue_blend = E._brier_blend(pool, draws)
    assert red_blend.shape == (33,)
    assert blue_blend.shape == (16,)
    assert math.isclose(float(red_blend.sum()), 6.0, rel_tol=1e-9)
    assert math.isclose(float(blue_blend.sum()), 1.0, rel_tol=1e-9)
    assert np.isfinite(red_blend).all() and np.isfinite(blue_blend).all()
    assert red_blend.attrs["weight_source"] == "fixed-fallback"
    assert red_blend.attrs["reason"] == "insufficient_history"


def test_eval_window_truncated_to_leave_training_history():
    """n_eval = min(50, len(draws)-300)：保留 300 期训练历史。"""
    draws = _mk_draws(310)
    pool = M.build_models(draws)
    red_blend, _ = E._brier_blend(pool, draws)
    assert red_blend.attrs["n_eval"] == 10  # 310-300

    draws2 = _mk_draws(400)
    rb2, _ = E._brier_blend(M.build_models(draws2), draws2)
    assert rb2.attrs["n_eval"] == 50        # 截断到窗口上限


# ---------------------------------------------------------------------------
# 7. 与既有调用方的兼容性（签名 / 返回类型 / adaptive 语义）
# ---------------------------------------------------------------------------

def test_signature_and_return_type_unchanged():
    """签名与返回类型保持兼容：仍返回 (ndarray, ndarray)，支持算术与 sum。"""
    import inspect
    sig = inspect.signature(E._brier_blend)
    assert list(sig.parameters) == ["models_dict", "draws", "adaptive"]
    assert sig.parameters["adaptive"].default is None

    draws = _mk_draws(400)
    red_blend, blue_blend = E._brier_blend(M.build_models(draws), draws)
    assert isinstance(red_blend, np.ndarray) and isinstance(blue_blend, np.ndarray)
    # 子类不得破坏既有算术/索引行为
    assert math.isclose(float((red_blend + red_blend).sum()), 12.0, rel_tol=1e-9)
    assert float(red_blend[0]) >= 0.0
    assert np.asarray(red_blend).shape == (33,)


def test_adaptive_downweight_reduces_that_method_weight():
    """adaptive 打折语义保持可用：打折的方法权重下降。"""
    draws = _mk_draws(400)
    pool = M.build_models(draws)
    base, _ = E._brier_blend(pool, draws)
    cut, _ = E._brier_blend(pool, draws, adaptive={"freq": 0.1})
    assert cut.attrs["weights"]["red"]["freq"] < base.attrs["weights"]["red"]["freq"]
    assert math.isclose(float(cut.sum()), 6.0, rel_tol=1e-9)


def test_predict_next_end_to_end_still_works():
    """端到端：predict_next 仍能产出请求注数，且不因新融合逻辑报错。"""
    draws = _mk_draws(400)
    res = E.predict_next(draws, use_llm=False, use_ml=False,
                         n_tickets=6, persist=False, rng=random.Random(4))
    assert res["actual_tickets"] == 6
    keys = [(tuple(t["reds"]), t["blue"]) for t in res["tickets"]]
    assert len(keys) == len(set(keys)), "输出注不得重复"
