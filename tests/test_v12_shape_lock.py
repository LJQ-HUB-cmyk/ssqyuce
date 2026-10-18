"""V12-12 形态约束通过率锁值测试（裁决 R-3）。

背景：同一个「形态约束通过率」曾出现**三个不同数字**
------------------------------------------------------
| 来源 | 值 | 错在哪 |
|---|---|---|
| P6 | 34.12% | 误取 `shape_constraint_ctx` 的 `len(arr)<100` **理论兜底分支**，生产永不进入 |
| Lead | 39.69% | 手搓判据时把 `top_zones` 写成前 5 个（真实是 **top-10**） |
| **真值** | **68.2992%** | `engine.pass_constraints` 全枚举 756460 / 1107568 |

三方都是认真测量，错在**口径不同却没有标注**。本文件把这个数锁死，
并同时锁住相邻的几个口径，防止将来又出现「同一个词三个值」。

口径表（务必分清）
------------------
| 口径 | 值 | 说明 |
|---|---|---|
| `engine.pass_constraints`（**生产门槛**） | **68.2992%** | 五维 + 三区 top-10 + `odd_range=[1..5]`（放宽） |
| `shape_rules.check_shape` 单用（奇偶 [2,3,4]） | 57.1541% | 奇偶用理论带，比生产严 |
| `enumerated_rates()["shape5"]` | 74.3088% | 五维形态（和值/跨度/奇偶/连号），**不含三区** |
| 理论兜底分支 | 34.1204% | `len(arr)<100` 才走，**生产不可达** |
"""
from __future__ import annotations

from itertools import combinations

import pytest

from lottery import engine as ENG
from lottery import shape_rules as SR

N_TOTAL = 1107568  # C(33,6)

# 锁值（全枚举精确值，任何改动都必须显式更新这里并说明理由）
PRODUCTION_RATE = 0.6829921415395944   # engine.pass_constraints
SHAPE5_RATE = 0.743088                  # enumerated_rates()["shape5"]


def _all_combos():
    return list(combinations(range(1, 34), 6))


# --------------------------------------------------------------------------
# ① 生产通过率锁值（主锁）
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# 重要口径说明（V12-12 二次修正）
# --------------------------------------------------------------------------
# 68.2992% 是 **真实 3515 期数据** 上的数，**不是**一个与数据无关的常量：
# `engine.constraint_ctx` 的 odd_range / sum 带 / top_zones **全部由最近 500 期
# 实算**，因此换一批数据（例如合成数据）通过率就会变。
#
# 实测：真实数据 odd_range=[1,2,3,4,5] -> 68.2992%
#       合成数据 odd_range=[1,2,3,4,5,6] -> 68.6229%
#
# 所以本文件用两种方式加锁：
#   ① **结构锁**（始终可跑）：锁定口径的**形状**（top-10、odd_range 来自实测频次、
#      sum/span 来自近 500 期分位），不依赖具体数据集；
#   ② **真值锁**（有真实库时才跑）：在真实库上断言 68.2992%。
# 这样既不会因合成数据而假红，又不会失去对真值的守护。

def _has_real_db() -> bool:
    """检测当前是否连着真实库（有真实 3515 期数据）。"""
    try:
        from lottery import db
        n = db.count_draws()
        return n and n > 3000
    except Exception:  # noqa: BLE001
        return False


requires_real_db = pytest.mark.skipif(
    not _has_real_db(),
    reason="需要真实库（>3000 期）；conftest 默认隔离到临时库")


@requires_real_db
def test_production_pass_rate_is_locked_on_real_data():
    """**真值锁**：真实 3515 期上 ``engine.pass_constraints`` 全枚举 = 68.2992%。

    如果本用例失败：
      1. 先确认是不是 `constraint_ctx` 的口径变了（odd_range / top_zones 个数）；
      2. 若确为有意改动，必须同时更新 V12-12 文档与 UI 文案；
      3. **不得**为了让测试变绿而放宽容差。
    """
    from lottery import db
    draws = db.load_draws()
    ctx = ENG.constraint_ctx(draws)
    passed = sum(1 for c in _all_combos() if ENG.pass_constraints(list(c), 1, ctx))
    rate = passed / N_TOTAL

    assert abs(rate - PRODUCTION_RATE) < 2e-3, (
        f"真实数据生产形态通过率 {rate:.6f} 偏离锁值 {PRODUCTION_RATE:.6f}。"
        "若确为有意改动，请同步更新锁值与文档，而不是放宽容差。")


def test_production_ctx_shape_is_expected(seeded_db):
    """**结构锁**：锁定造成 68.30% vs 57.15% 差异的口径形状。

    这两个口径一旦被改，68.2992% 就不再成立：
      * ``top_zones`` 必须取 **10** 个（不是 5、也不是兜底的 4）
      * ``odd_range`` 必须由**实测频次≥2%** 得出（不是硬编码理论带 [2,3,4]）
        —— 真实数据下它是 [1,2,3,4,5]，但合成数据可能多一档，故只断言范围形态
      * ``sum_min/max`` 来自近 500 期 5/95 分位
      * ``span_min/max`` 来自近 500 期 5/95 分位
    """
    draws = seeded_db(600, seed=43)
    ctx = ENG.constraint_ctx(draws)

    assert len(ctx["top_zones"]) == 10, len(ctx["top_zones"])
    # odd_range 是动态的：必须是用「频次≥2%」筛出来的子集，且不等于纯理论带
    assert isinstance(ctx["odd_range"], list) and ctx["odd_range"], ctx["odd_range"]
    assert set(ctx["odd_range"]).issubset(set(range(0, 7))), ctx["odd_range"]
    # 放宽口径的特征：至少包含 1 或 5 这样的「理论罕见但实测不算罕见」的档
    assert len(ctx["odd_range"]) >= 5, (
        f"odd_range={ctx['odd_range']} 太窄，疑似退回理论带 [2,3,4] "
        "——那会把通过率从 68.30% 拉回 57.15%")
    assert ctx["span_min"] is not None and ctx["span_max"] is not None


# --------------------------------------------------------------------------
# ② 相邻口径也要锁，防止再「同一个词三个值」
# --------------------------------------------------------------------------

def test_enumerated_shape5_rate_locked():
    """五维形态（不含三区）全枚举通过率锁定。"""
    er = SR.enumerated_rates()
    assert abs(er["shape5"] - SHAPE5_RATE) < 2e-3, er["shape5"]
    # 全枚举总数必须正确
    assert er["n_total"] == float(N_TOTAL)


def test_shape_constraint_ctx_short_history_uses_fallback():
    """短历史（<100 期）才走兜底；用它算通过率会得到 34.12% 的错值。"""
    short = [{"reds": [1, 2, 3, 4, 5, 6], "blue": 1, "issue": "x"}] * 10
    ctx = SR.shape_constraint_ctx(short)
    # 兜底分支特征：4 个区组 + span 7~32
    assert len(ctx["top_zones"]) == 4, ctx["top_zones"]
    assert (ctx["span_min"], ctx["span_max"]) == (7.0, 32.0)


# --------------------------------------------------------------------------
# ③ 反向用例（P6 要求）：改动口径必须让锁值测试失败
# --------------------------------------------------------------------------

def test_reverse_case_top_zones_shrunk_must_fail_lock(seeded_db, monkeypatch):
    """若有人把 top_zones 从 10 改成 5，锁值必须**失败**（复刻 Lead 当年错法）。"""
    draws = seeded_db(600, seed=53)
    from lottery import engine

    real_ctx = engine.constraint_ctx

    def _shrunk(draws_arg):
        c = real_ctx(draws_arg)
        c["top_zones"] = c["top_zones"][:5]   # 复刻 top-5 错误
        return c

    monkeypatch.setattr(engine, "constraint_ctx", _shrunk)
    ctx = engine.constraint_ctx(draws)
    passed = sum(1 for c in _all_combos() if engine.pass_constraints(list(c), 1, ctx))
    rate = passed / N_TOTAL

    # top-5 会让通过率明显掉下来 → 锁值断言会失败，说明护栏有效
    assert abs(rate - PRODUCTION_RATE) >= 2e-3, (
        "把 top_zones 缩到 5 之后通过率竟然仍等于锁值 —— 锁值测试形同虚设")


# --------------------------------------------------------------------------
# ④ shape_rules_report 不得再出现手写估计值
# --------------------------------------------------------------------------

def test_report_uses_enumerated_rates_not_handwritten(seeded_db):
    draws = seeded_db(600, seed=59)
    rep = SR.shape_rules_report(draws)
    er = SR.enumerated_rates()

    by_key = {r["key"]: r for r in rep["rules"]}
    assert by_key["sum"]["theoretical_rate"] == pytest.approx(er["sum"])
    assert by_key["span"]["theoretical_rate"] == pytest.approx(er["span"])
    assert by_key["parity"]["theoretical_rate"] == pytest.approx(er["odd"])
    assert by_key["consec"]["theoretical_rate"] == pytest.approx(er["consec"])

    # 不再有手写的 0.90 / 0.95 / 0.99
    for key, hand in (("sum", 0.90), ("span", 0.95), ("consec", 0.99)):
        assert by_key[key]["theoretical_rate"] != hand, (
            f"{key} 仍是手写估计值 {hand}，V12-12 未生效")

    # 必须把精确率一并暴露，供 UI 与审计引用
    assert "enum_rates" in rep
    for k in ("sum", "span", "odd", "consec", "zones"):
        assert k in rep["enum_rates"]
