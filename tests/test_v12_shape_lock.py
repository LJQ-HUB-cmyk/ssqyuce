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
| `engine.pass_constraints`（**生产门槛**） | **68.2992%**（3515 期） | 五维 + 三区 top-10 + `odd_range=[1..5]`（放宽） |
| `shape_rules.check_shape` 单用（奇偶 [2,3,4]） | 57.1541% | 奇偶用理论带，比生产严 |
| `enumerated_rates()["shape5"]` | 74.3088% | 五维形态（和值/跨度/奇偶/连号），**不含三区** |
| 理论兜底分支 | 34.1204% | `len(arr)<100` 才走，**生产不可达** |

## W02 修正：生产通过率改为「区间锁」，不锁死单值

原实现断言 `|rate − 0.682992| < 2e-3`。用真实库（**3516 期**）跑出来是
**0.686842**，差 0.00385 —— 不是 bug，而是这个量**依赖数据快照**：

* `constraint_ctx` 的 sum 分位 / `top_zones` / `odd_range` 全部由**最近 500 期**实算；
* 每来一期新开奖，窗口平移 → 约束带微变 → 通过率漂 ≈ ±0.5%；
* 双色球每周 3 期 ⇒ 锁死单值**每 3 期就假红一次**，最后只能靠"改锁值"放行，
  护栏退化成橡皮图章。

现在锁两件事（见 `test_production_pass_rate_is_locked_on_real_data`）：
1. 通过率必须落在 `RATE_BAND = (0.65, 0.72)` —— 容得下逐期漂移；
2. **不得**退化成 57.15%（`check_shape` 单用）或 34.12%（理论兜底）——
   这两个错误口径与正确值相差 11 / 34 个百分点，一旦口径被改坏必然越界。

> 注：本文件默认在**隔离环境**跑（无真实库 → 真值用例 skip）。
> 要真正校验生产数值，用真实库专用通道：
> `LOTT_TEST_REAL_DB=1 .venv/bin/python -m pytest tests/test_v12_shape_lock.py -q`
"""
from __future__ import annotations

from itertools import combinations

import pytest

from lottery import engine as ENG
from lottery import shape_rules as SR

N_TOTAL = 1107568  # C(33,6)

# 锁值（全枚举精确值，任何改动都必须显式更新这里并说明理由）
PRODUCTION_RATE = 0.6829921415395944   # engine.pass_constraints（**3515 期**时的实测值）
SHAPE5_RATE = 0.743088                  # enumerated_rates()["shape5"]

# ---------------------------------------------------------------------------
# W02 新增：生产口径的**可接受区间**与两个"错误口径"参照值
# ---------------------------------------------------------------------------
# 为什么生产通过率不能锁死单一数值：
#   `constraint_ctx` 的 sum 分位 / top_zones / odd_range 全部由**最近 500 期**实算，
#   所以每来一期新开奖，窗口平移 → 约束带微变 → 通过率漂 ±0.5% 左右。
#   实测两个快照：3515 期 → 0.682992；3516 期（多 2026119）→ 0.686842。
#   若断言"精确等于某一期快照"，这条用例每 3 期开奖就假红一次。
#
# 改为：锁"落在合理区间 + 不等于任何已知错误口径"。
#   区间宽度（±3.5 个百分点）足以容纳逐期漂移，
#   同时把两个错误口径（相差 11 / 34 个百分点）挡在外面。
RATE_BAND = (0.65, 0.72)

# 错误口径①：`shape_rules.check_shape` 单用（奇偶硬编码 [2,3,4]，比生产严）
SHAPE_ONLY_RATE = 0.571541
# 错误口径②：`shape_constraint_ctx` 的 `len(arr)<100` 理论兜底分支（生产不可达）
FALLBACK_RATE = 0.341204


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
    """检测当前是否连着真实库（有真实 3515 期数据）。

    **注意（W02 修复）**：这个函数必须在**用例执行期**调用，不能在模块导入期
    求值后缓存成 ``skipif`` 装饰器 —— 那样会与 ``conftest`` 的 DB 隔离**时序脱钩**：
      * 模块导入/收集期：``conftest`` 的 autouse fixture 还没生效，这里读到**真实库**
        3516 期 → 判定"有真库" → 不 skip；
      * 用例执行期：``conftest`` 已把 ``config.DB_PATH`` 重定向到**空临时库** →
        ``db.load_draws()`` 返回 ``[]`` → ``constraint_ctx([])`` 抛异常。
    结果就是"守卫以为有数据、实际没数据"，表现为一个与业务无关的
    ``IndexError`` 假红。改为执行期判定即可彻底消除。
    """
    try:
        from lottery import db
        n = db.count_draws()
        return bool(n and n > 3000)
    except Exception:  # noqa: BLE001
        return False


def _require_real_db(why: str = "需要真实库（>3000 期）；conftest 默认隔离到临时库"):
    """执行期守卫：没有真实库就 skip（而不是让 np.percentile 抛 IndexError）。"""
    if not _has_real_db():
        pytest.skip(why)


def test_production_pass_rate_is_locked_on_real_data():
    """**真值区间锁**：真实库上 ``engine.pass_constraints`` 全枚举通过率必须落在
    ``[0.65, 0.72]``，且**不得**退化成两个已知错误口径。

    ## 为什么不再锁死单一数值（W02 修正，重要）

    原实现断言 ``abs(rate - 0.682992) < 2e-3``。用真实库跑出来是 **0.686842**，
    差 0.00385 —— 但这不是 bug：

    * 该常量是 **3515 期**（2003001–2026118）时算的；
    * 生产库现在是 **3516 期**（多出 2026119）；
    * ``constraint_ctx`` 取**最近 500 期**估计 sum 分位 / top_zones / odd_range，
      窗口平移一期 → 约束带微变 → 通过率从 68.2992% 变 68.6842%。

    也就是说：**这个量依赖数据快照，每 3 期开奖就会漂一次**，"锁死单值"必然
    周期性假红，最终只能靠"改锁值"放行——那就把护栏变成了橡皮图章。

    ## 现在锁什么

    锁**口径形状**而不是数据快照值。这条用例要防的是"口径被改坏"，而口径
    一旦改坏，通过率会**跳变**而不是微漂：

    | 口径 | 通过率 | 判定 |
    |---|---:|---|
    | 生产口径（五维 + 三区 top-10 + odd_range 实测筛） | ~68.3–68.7% | ✅ 期望区间 |
    | ``shape_rules.check_shape`` 单用（奇偶硬编码 [2,3,4]） | 57.15% | ❌ 越界 |
    | ``len(arr)<100`` 理论兜底分支（生产不可达） | 34.12% | ❌ 越界 |

    区间 ``[0.65, 0.72]`` 同时满足：容得下逐期微漂（±0.5%），挡得住两种错误口径
    （差 11 和 34 个百分点）。

    ## 失败时怎么办

    1. 先看断言信息里打印的 ``n_draws`` 与 ``rate``，确认不是数据异常；
    2. 若 rate 掉到 0.65 以下，几乎一定是 ``top_zones`` 个数或 ``odd_range`` 口径变了
       —— 去看 ``test_production_ctx_shape_is_expected``（结构锁）先挂没挂；
    3. **不得**为了让测试变绿而放宽区间；区间只在口径**有意**变更时同步更新，
       并同时更新 V12-12 文档与 UI 文案。
    """
    _require_real_db()
    from lottery import db
    draws = db.load_draws()
    ctx = ENG.constraint_ctx(draws)
    passed = sum(1 for c in _all_combos() if ENG.pass_constraints(list(c), 1, ctx))
    rate = passed / N_TOTAL
    n_draws = len(draws)
    last_issue = draws[-1]["issue"] if draws else "?"

    info = (f"真实数据口径：n_draws={n_draws}, last_issue={last_issue}, "
            f"rate={rate:.6f}（3515 期基线 {PRODUCTION_RATE:.6f}，"
            f"漂移 {rate - PRODUCTION_RATE:+.6f}；3516 期实测 0.686842）")

    # ① 必须落在生产口径的合理区间
    assert RATE_BAND[0] <= rate <= RATE_BAND[1], (
        f"生产形态通过率 {rate:.6f} 落在 [{RATE_BAND[0]}, {RATE_BAND[1]}] 之外。{info}")

    # ② 必须**不是**两个已知错误口径（这才是本用例真正的护栏价值）
    assert abs(rate - SHAPE_ONLY_RATE) > 5e-3, (
        f"通过率退化成 check_shape 单用口径（57.15%）——三区/odd_range 口径被改坏了。{info}")
    assert abs(rate - FALLBACK_RATE) > 5e-3, (
        f"通过率退化成理论兜底分支（34.12%，生产不可达）——"
        f"shape_constraint_ctx 的 len<100 分支被误触发。{info}")

    # ③ 数据量必须够（否则 500 期窗口不成立）
    assert n_draws >= 500, f"历史数据不足 500 期，近 500 期窗口失效。{info}"



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
    """若有人把 top_zones 从 10 改成 5，**区间锁必须失败**（复刻 Lead 当年错法）。

    W02 起锁的是 `RATE_BAND` 而不是单一值，所以这里断言"缩小后**掉出**区间"，
    这才是"护栏真的能拦住错误口径"的证明。
    """
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

    # top-5 会让通过率明显掉出区间 → 区间锁会失败，说明护栏有效
    assert not (RATE_BAND[0] <= rate <= RATE_BAND[1]), (
        f"把 top_zones 缩到 5 之后通过率仍落在区间 {RATE_BAND} 内"
        f"（实测 {rate:.6f}）—— 区间锁形同虚设，拦不住错误口径")


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


# --------------------------------------------------------------------------
# ⑤ W02：真值锁的守卫必须在**执行期**判定（不许在收集期缓存）
# --------------------------------------------------------------------------

def test_empty_draws_raises_typed_error_not_indexerror():
    """空输入必须抛**类型化**异常，而不是让 numpy 的 IndexError 抛穿。

    回归背景：`constraint_ctx([])` 曾因 `np.percentile([], 5)` 抛
    `IndexError: index -1 is out of bounds for axis 0 with size 0`。
    那种错误对调用方毫无意义（看不出是"没数据"还是"numpy 版本变了"），
    且极易被 `except Exception` 静默吞掉后表现为"预测结果为 0 条"。
    """
    with pytest.raises(ENG.InsufficientDataError) as ei:
        ENG.constraint_ctx([])
    # 必须是 InsufficientDataError，且它继承 ValueError 便于上游统一处理
    assert isinstance(ei.value, ValueError)
    assert "至少 1 期" in str(ei.value)


def test_empty_draws_does_not_raise_indexerror():
    """反向用例：确认不是 IndexError（防止有人改回裸 numpy 调用）。"""
    try:
        ENG.constraint_ctx([])
    except IndexError as e:  # pragma: no cover - 只有回归时才会走到
        pytest.fail(f"空输入又抛了 IndexError（W02 回归）：{e}")
    except ENG.InsufficientDataError:
        pass  # 期望路径


def test_real_db_guard_is_evaluated_at_runtime(monkeypatch, isolated_env):
    """守卫必须在**执行期**反映当前 DB，而不是收集期的快照。

    模拟"conftest 已把 DB 隔离到空临时库"的状态：此时
    `_has_real_db()` 必须返回 False（→ 用例 skip），
    而不是像旧实现那样因为收集期缓存了 True 而让用例跑下去崩掉。
    """
    from lottery import db
    assert db.count_draws() == 0, "隔离环境应为空库"
    assert _has_real_db() is False, (
        "隔离到空库后 _has_real_db() 仍返回 True —— 守卫又变成收集期快照了")

