"""B-2：大众偏好度模型（popularity）。

## 这个模块的定位（测试也必须守住这条）

它预测**人群行为**（`p1` 一等奖中奖注数），**不**预测开奖号码。
号码已被本项目反复证明不可预测（红球 χ² p=0.065、条件熵只降 0.0076 bit、
最强候选 max-stat 校正后 p=0.2995）。

因此本文件的测试重点不是"预测准不准"（增量 R² 只有 0.77%，本来就不准），
而是：
1. 特征计算对不对（形态定义不能漂）；
2. 口径是否一致（阈值与预测值必须在同一空间 —— 曾因跳过这一步出过 bug）；
3. **诚实性**：输出必须带披露文本，且**不得**给出精确注数；
4. 时序切分正确（不能随机 KFold 造成未来信息泄漏）。
"""
from __future__ import annotations

import pytest

from lottery import popularity as P


# ---------- 1. 特征计算 ----------

def test_features_basic_shape():
    f = P.features_of([1, 2, 3, 4, 5, 6])
    assert f["birthday_count"] == 6.0        # 全部 ≤31
    assert f["same_tail"] == 0.0             # 1..6 个位各不相同
    assert f["consecutive"] == 5.0           # 1-2,2-3,3-4,4-5,5-6
    assert f["sum_reds"] == 21.0
    assert f["spread"] == 5.0
    assert f["even_count"] == 3.0
    assert f["zone_count"] == 1.0            # 全在 1-11 区


def test_features_birthday_excludes_32_33():
    """32/33 不在生日号范围（1-31），这对「大众偏好」是关键区分。"""
    f = P.features_of([5, 17, 23, 28, 32, 33])
    assert f["birthday_count"] == 4.0, "32/33 不应计入生日号"


def test_features_same_tail():
    """同尾号 = 6 - 不同个位数。"""
    f = P.features_of([1, 11, 21, 2, 12, 22])
    # 个位 {1,1,1,2,2,2} → 2 种 → same_tail = 4
    assert f["same_tail"] == 4.0
    # 1/2 → 1-11 区；11/12 → 1-11 与 12-22；21/22 → 12-22
    # 实际覆盖 {1-11, 12-22} 两个区（没有 23-33）
    assert f["zone_count"] == 2.0, "本组号只覆盖两个区（无 23-33）"


def test_features_zone_count_three_zones():
    f = P.features_of([1, 12, 23, 2, 13, 24])
    assert f["zone_count"] == 3.0            # 1-11, 12-22, 23-33 全覆盖


def test_features_reject_wrong_count():
    with pytest.raises(ValueError):
        P.features_of([1, 2, 3, 4, 5])
    with pytest.raises(ValueError):
        P.features_of([1, 2, 3, 4, 5, 6, 7])


def test_feature_vector_order_matches_names():
    v = P.feature_vector([1, 2, 3, 4, 5, 6])
    assert len(v) == len(P.FEATURE_NAMES)
    f = P.features_of([1, 2, 3, 4, 5, 6])
    for name, val in zip(P.FEATURE_NAMES, v):
        assert f[name] == val


# ---------- 2. 数值内核 ----------

def test_ols_recovers_exact_linear_relation():
    """无噪声线性数据上，OLS 必须精确还原系数（含截距）。"""
    X = [[1.0, 2.0], [2.0, 1.0], [3.0, 4.0], [4.0, 3.0], [5.0, 6.0]]
    y = [3.0 * a - 2.0 * b + 7.0 for a, b in X]
    coef, icpt = P._ols(X, y)
    assert coef[0] == pytest.approx(3.0, abs=1e-4)
    assert coef[1] == pytest.approx(-2.0, abs=1e-4)
    assert icpt == pytest.approx(7.0, abs=1e-4)


def test_ts_predict_is_expanding_window_not_random():
    """时序预测必须是「用过去预测未来」，不能混入未来信息。

    验证方法：第 1 折的预测只能从 `size` 处开始，
    之前的位置必须是 NaN（说明没有用后面的数据反过来预测前面）。
    """
    n = 60
    X = [[float(i)] for i in range(n)]
    y = [float(i) * 2 for i in range(n)]
    pred = P._ts_predict(X, y, folds=5)
    size = n // 6
    assert all(pred[i] != pred[i] for i in range(size)), \
        "扩展窗口的第一段不应有预测值（没有历史可用）"
    assert all(pred[i] == pred[i] for i in range(size, size * 2)), \
        "第二段开始应有预测值"


def test_r2_perfect_and_degenerate():
    assert P._r2([1, 2, 3], [1, 2, 3]) == pytest.approx(1.0)
    # 常数真值 → ss_tot = 0，返回 0 而不是除零
    assert P._r2([5, 5, 5], [1, 2, 3]) == 0.0


def test_quantile_interpolates():
    vals = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert P._quantile(vals, 0.0) == 1.0
    assert P._quantile(vals, 1.0) == 5.0
    assert P._quantile(vals, 0.5) == 3.0
    assert P._quantile(vals, 0.25) == 2.0


# ---------- 3. 档位口径（曾出过 bug，必须锁死） ----------

def test_tier_thresholds_and_prediction_share_scale():
    """**回归防护**：档位阈值与预测值必须在同一空间。

    2026-10-21 实测 bug：`describe()` 把乘过 `sales/1e8` 的预测值
    拿去和按**未乘销量**标定的阈值比较 —— 结果 3056/3428 条历史期次
    全被判成 "high"，档位完全失效（三档变一档）。

    修法：档位一律用标准化空间（每亿销量的注数）的预测值。
    """
    m = P.PopularityModel()
    # 手工设一个已知模型，避免依赖真实库
    m.coef_ = [0.0] * len(P.FEATURE_NAMES)
    m.intercept_ = 2.8
    m.fitted = True
    m.tier_lo = 2.6
    m.tier_hi = 3.0

    d_no_sales = m.describe([1, 2, 3, 4, 5, 6])
    d_with_sales = m.describe([1, 2, 3, 4, 5, 6], sales=3.3e8)

    assert d_no_sales["tier"] == d_with_sales["tier"], (
        "传不传 sales 都不应改变档位 —— 档位是形态属性，不是销量属性。"
        f"得到 {d_no_sales['tier']} vs {d_with_sales['tier']}"
    )
    assert d_no_sales["tier"] == "mid", "2.8 落在 [2.6, 3.0) 应判 mid"


def test_tier_boundaries():
    assert P.tier_of(2.0, 2.6, 3.0) == "low"
    assert P.tier_of(2.6, 2.6, 3.0) == "low"     # 含下界
    assert P.tier_of(2.8, 2.6, 3.0) == "mid"
    assert P.tier_of(3.0, 2.6, 3.0) == "high"    # 含上界
    assert P.tier_of(9.9, 2.6, 3.0) == "high"


# ---------- 4. 诚实性（本模块最重要的约束） ----------

def test_describe_never_returns_exact_ticket_count():
    """输出里**不得**出现精确预测注数 —— 只有档位。

    R² 只有 0.77%，给出"预计 7.3 注中奖"这类数字会被当成精确知识，
    这属于本项目明令禁止的夸大。
    """
    m = P.PopularityModel()
    m.coef_ = [0.1] * len(P.FEATURE_NAMES)
    m.intercept_ = 2.7
    m.fitted = True
    m.tier_lo, m.tier_hi = 2.6, 3.0
    out = m.describe([1, 2, 3, 4, 5, 6])

    for banned in ("predicted_p1", "p1_estimate", "expected_winners"):
        assert banned not in out, f"输出不该含 {banned}（精确注数会误导）"
    assert "tier" in out and "tier_label" in out


def test_describe_always_carries_caveat_and_honesty():
    """每个档位都必须带 caveat，且顶层必须带 honesty 披露。"""
    m = P.PopularityModel()
    m.coef_ = [0.1] * len(P.FEATURE_NAMES)
    m.intercept_ = 2.7
    m.fitted = True
    m.tier_lo, m.tier_hi = 2.6, 3.0

    out = m.describe([1, 2, 3, 4, 5, 6])
    assert out["caveat"], "缺 caveat"
    assert "不提高中奖概率" in out["honesty"] or "不受选号影响" in out["honesty"], \
        "诚实披露必须明确说明不提高中奖概率"
    assert "52.3" in out["honesty"], "必须披露期望收益仍为负"


def test_all_tiers_have_labels_and_caveats():
    for t in ("low", "mid", "high"):
        assert t in P.TIER_LABELS
        assert t in P.TIER_CAVEATS
        assert P.TIER_CAVEATS[t], f"{t} 缺 caveat"


def test_unfitted_model_returns_not_ok():
    m = P.PopularityModel()
    assert m.describe([1, 2, 3, 4, 5, 6])["ok"] is False
    assert m.predict_p1([1, 2, 3, 4, 5, 6]) is None


# ---------- 5. 真实数据上拟合（有真实库才跑） ----------

def test_fit_on_real_data_reports_honest_effect_size():
    """真实库上拟合：增量 R² 必须**很小**且 p 值合理。

    这是诚实性守卫：如果哪天增量 R² 突然跳到 0.1 以上，
    要么是真找到了强信号（需要人工复核），要么是数据泄漏（更可能）。
    实测基线：R² ≈ 0.008~0.019，置换 p ≈ 0.005~0.02。
    """
    from lottery import db
    if not db.count_draws() or db.count_draws() < 3000:
        pytest.skip("需要真实库（>3000 期）")

    m = P.PopularityModel()
    info = m.fit(db.load_draws())
    assert info["ok"], info

    # 效应真实但极小：上界防止数据泄漏/意外强信号
    assert 0.0 <= info["r2_increment"] < 0.10, (
        f"增量 R²={info['r2_increment']} 异常 —— 超出「极小效应」的合理范围，"
        f"优先怀疑数据泄漏（如随机切分、目标里混入特征）"
    )
    # 不显著时也不该假装显著：p 值必须在合理区间
    assert 0.0 < info["p_value"] <= 0.5
    # 系数必须有符号含义可解释（生日号、同尾应为正）
    coef = info["coefficients"]
    assert coef["birthday_count"] > 0, "生日号越多应越热门"
    assert coef["same_tail"] > 0, "同尾越多应越热门"


def test_fit_rejects_insufficient_data():
    m = P.PopularityModel()
    info = m.fit([{"reds": [1, 2, 3, 4, 5, 6], "p1": 5, "sales": 1e8}] * 10)
    assert info["ok"] is False
    assert info["reason"] == "insufficient_data"


def test_fit_skips_draws_missing_prize_or_sales():
    """缺 p1 或 sales 的期次必须被跳过，不能当成 0 参与训练。"""
    base = {"reds": [1, 2, 3, 4, 5, 6], "p1": 5, "sales": 2e8}
    draws = [base] * 400
    draws += [{"reds": [7, 8, 9, 10, 11, 12], "p1": None, "sales": 2e8}] * 100
    draws += [{"reds": [7, 8, 9, 10, 11, 12], "p1": 5, "sales": 0}] * 100
    m = P.PopularityModel()
    info = m.fit(draws)
    assert info["ok"]
    assert info["n_train"] == 400, f"应只用 400 期有效数据，实际 {info['n_train']}"
