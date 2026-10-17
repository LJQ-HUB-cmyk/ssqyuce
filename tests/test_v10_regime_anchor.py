"""V10-07：regime 阶段边界**锚定化** —— 修一个当下就存在的不可复现缺陷。

背景（`docs/reviews/R2_STATISTICS_REVIEW.md` §3.3.4 / `S2_PLAN_FEASIBILITY_REVIEW.md`）
-----------------------------------------------------------------------------
V8 起的 ``build_adaptive_eras`` 从**最新一期往过去**切段（``range.py`` 旧 :139-154）。
于是**每加一期，所有历史段的边界都整体平移**：

    真实库实测（3515 期 vs 3500 期，block=150）：23 段**无一例外**全部漂移 15 期
    P1: (0,200) → (0,215)；P2: (200,350) → (215,365)；…；P23: (3350,3500) → (3365,3515)

历史段的样本变了，p 值自然跟着变 —— 用户在两次登录会看到「同一个 2023 年段」给出
**两个不同的 p 值**。这是"不可复现"，比"没有 CUSUM"严重得多（CUSUM 已被评审判定不可用
并删除，见 ``docs/V10_IMPROVEMENT_PLAN.md`` §0.2 删除台账）。

本文件的验收立场
----------------
1. **锚定**：第 k 段边界 = ``[k×block, (k+1)×block)``，只由起点与 k 决定；
2. **同一份数据两次运行 → 逐段逐字段完全相同**（精确 ``==``，不是近似）；
3. **加数据不改历史**：``draws[:N]`` 与 ``draws[:N+k]`` 上，前 N 期已覆盖的段
   其边界与 p 值**必须逐字节相等**；
4. **反向用例**：同一条断言对旧实现（``anchor_mode="legacy"``）**必须失败** ——
   否则这个测试抓不住回归；
5. **多重性分母锁定**：分母取固定常数而非当前段数，否则"段数变化 → 历史段校正后 p 变"
   会把刚修掉的不可复现性从 ``p_value`` 挪到 ``p_adjusted`` 上。

注意：本文件只依赖 ``lottery.regime``，不碰生产库、不碰容器、不需要网络。
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from lottery import regime

BLOCK = 150


# ---------- 夹具 ----------

def _draws(n: int, seed: int = 7) -> list:
    """构造 n 期确定性开奖。

    **关键**：期号/年份只依赖下标 i，不依赖 n —— 于是 ``_draws(n + k)[:n] == _draws(n)``，
    这才是"同一份历史 + 追加新数据"的正确夹具（否则测的就不是数据追加，而是换了数据）。
    """
    rng = np.random.default_rng(seed)
    out = []
    for i in range(n):
        year = 2003 + i // 150                 # 每 150 期一年：3515 期 → 2003…2026
        out.append({
            "issue": f"{year}{i % 150 + 1:03d}",
            "date": f"{year}-06-01",
            "reds": sorted(int(x) for x in rng.choice(np.arange(1, 34), 6, replace=False)),
            "blue": int(rng.integers(1, 17)),
        })
    return out


def _series(n: int, seed: int = 3, effect: tuple = ()) -> list:
    """构造一条确定性逐期序列；``effect`` 可指定 [(from, to, delta)] 注入真实效应。"""
    rng = np.random.default_rng(seed)
    x = rng.normal(0.0, 1.0, n)
    for a, b, d in effect:
        x[a:b] += d
    return [float(v) for v in x]


def _tail_complete_eras(eras, covered_n: int):
    """取出「被前 covered_n 期完整覆盖」的段（末段若跨出 covered_n 则不计入）。"""
    return [e for e in eras if int(e["end"]) <= covered_n]


# ---------- 1. 默认口径必须是 anchored ----------

def test_default_anchor_mode_is_anchored():
    """默认必须走锚定切分 —— 否则本任务的修复等于没生效。"""
    assert regime.ERA_ANCHOR_MODE == "anchored"
    assert regime.ERA_SCHEMA == 2
    # 默认入口（discovery / api_app / patterns_runtime 都走这条）必须等于锚定实现
    d = _draws(1200)
    assert regime.build_adaptive_eras(d) == regime.build_anchored_eras(d)
    assert regime.eras_for(d) == regime.build_anchored_eras(d)
    # 旧实现必须仍然可达（对照用），但不得是默认
    legacy = regime.build_adaptive_eras(d, anchor_mode="legacy")
    assert legacy != regime.build_anchored_eras(d)


# ---------- 2. 边界就是「起点 + k×块大小」 ----------

def test_anchored_boundaries_are_anchor_plus_k_times_block():
    """2003001 起、块 150：前 3 段边界必须分别是 [0,150) / [150,300) / [300,450)。"""
    d = _draws(1200)
    assert d[0]["issue"] == "2003001"
    eras = regime.build_anchored_eras(d, block=BLOCK)
    assert [(e["start"], e["end"]) for e in eras[:3]] == [(0, 150), (150, 300), (300, 450)]
    assert [e["n"] for e in eras[:3]] == [150, 150, 150]
    for k, e in enumerate(eras):
        assert e["start"] == k * BLOCK, f"第 {k + 1} 段起点必须等于 k×block"
        assert e["end"] == min((k + 1) * BLOCK, len(d))
        assert e["anchor"] == k * BLOCK
        assert e["block_index"] == k
        assert e["block_size"] == BLOCK
        assert e["anchor_issue"] == d[e["start"]]["issue"]
        assert e["era_schema"] == 2
    # 首尾相接、完整覆盖
    assert eras[0]["start"] == 0 and eras[-1]["end"] == len(d)
    for a, b in zip(eras, eras[1:]):
        assert a["end"] == b["start"]


def test_era_dict_keeps_v6_field_names():
    """返回结构必须向后兼容：既有消费方（patterns_runtime/discovery/前端）按这些 key 取值。"""
    d = _draws(600)
    e = regime.build_anchored_eras(d, block=BLOCK)[0]
    for key in ("name", "short", "start_year", "end_year", "start", "end", "n",
                "first_issue", "last_issue"):
        assert key in e, f"既有字段 {key} 不得丢失"
    assert e["name"].startswith("P1") and e["short"] == "P1"


# ---------- 3. 末段允许不完整，但起点仍锚定 ----------

def test_tail_era_is_incomplete_with_real_n_and_fixed_anchor():
    """末段不足一个完整块：``complete=False`` + ``n`` 为实际期数，**起点**不漂。"""
    d = _draws(1355)                      # 1355 = 9×150 + 5
    eras = regime.build_anchored_eras(d, block=BLOCK)
    assert len(eras) == 10
    tail = eras[-1]
    assert tail["complete"] is False
    assert tail["n"] == 5 and (tail["start"], tail["end"]) == (1350, 1355)
    assert all(e["complete"] is True for e in eras[:-1])

    # 加 5 期后末段仍是同一个锚定位置（只是变长），历史段一个都不动
    d2 = _draws(1360)
    eras2 = regime.build_anchored_eras(d2, block=BLOCK)
    assert eras2[-1]["start"] == 1350 and eras2[-1]["n"] == 10
    assert eras2[:-1] == eras[:-1], "只有末段可以变长，历史段必须逐字段不变"


def test_no_partial_era_when_length_is_multiple_of_block():
    d = _draws(1200)
    eras = regime.build_anchored_eras(d, block=BLOCK)
    assert len(eras) == 8 and all(e["complete"] for e in eras)


# ---------- 4. 硬断言一：同一份数据两次运行，p 值完全一致 ----------

def test_same_data_twice_yields_identical_p_values():
    """逐段逐字段精确比对（``==``，不是 ``approx``）—— 相同输入必须给出相同输出。"""
    d = _draws(1500)
    s = _series(1500, effect=((300, 450, 0.5),))
    r1 = regime.regime_report(s, d, block=BLOCK)
    r2 = regime.regime_report(s, d, block=BLOCK)
    assert r1["eras"] == r2["eras"], "同一份数据两次运行必须逐段逐字段相同"
    for a, b in zip(r1["eras"], r2["eras"]):
        assert a["p_value"] == b["p_value"]           # 精确相等（float 同一对象值）
        assert a["p_adjusted"] == b["p_adjusted"]
        assert a["delta"] == b["delta"]
    assert r1["status"] == r2["status"]
    assert r1["recent"]["p_value"] == r2["recent"]["p_value"]
    assert r1["recent"]["p_adjusted"] == r2["recent"]["p_adjusted"]
    # 段边界也必须逐字段相同
    assert regime.build_adaptive_eras(d) == regime.build_adaptive_eras(d)


# ---------- 5. 硬断言二（核心验收）：加数据不改变历史 ----------

@pytest.mark.parametrize("extra", [1, 10, 37, 150, 400])
def test_appending_draws_never_moves_or_revalues_history(extra):
    """``draws[:N]`` 与 ``draws[:N+extra]``：前 N 期已覆盖的段，边界与 p 值必须完全一致。

    这是"新数据不改变历史"的直接证明，也是本任务的核心验收。
    """
    n = 1350                              # 9 个完整段，正好是块边界
    d1, d2 = _draws(n), _draws(n + extra)
    s1 = _series(n, effect=((0, 150, -0.4),))            # 老段里放一个真实效应，让 p 有辨识度
    s2 = s1 + _series(n + extra)[n:]                     # 追加的期用独立数据
    r1 = regime.regime_report(s1, d1, block=BLOCK)
    r2 = regime.regime_report(s2, d2, block=BLOCK)
    e1 = regime.build_anchored_eras(d1, block=BLOCK)
    e2 = regime.build_anchored_eras(d2, block=BLOCK)

    kept = _tail_complete_eras(e1, n)                    # 前 N 期完整覆盖的段
    assert len(kept) == 9
    assert [x["start"] for x in kept] == [0, 150, 300, 450, 600, 750, 900, 1050, 1200]
    for i, era in enumerate(kept):
        assert e2[i] == era, f"{era['short']} 的边界/期号被新数据改动了"
        a, b = r1["eras"][i], r2["eras"][i]
        assert a["p_value"] == b["p_value"], f"{era['short']} 的 p 值随新数据变化"
        assert a["p_adjusted"] == b["p_adjusted"], f"{era['short']} 的校正后 p 随新数据变化"
        assert a["delta"] == b["delta"]
        assert a["n"] == b["n"]
    # 新数据只可能"追加"，不可能删段
    assert len(e2) >= len(e1)


def test_history_is_stable_across_many_appends():
    """连续加 20 次、每次 7 期：第 1 段的 (边界, p) 从头到尾不变（批量硬断言）。"""
    base = 1200
    d = _draws(base + 20 * 7)
    s = _series(base + 20 * 7, effect=((0, 150, 0.6),))
    first = None
    for k in range(21):
        n = base + 7 * k
        r = regime.regime_report(s[:n], d[:n], block=BLOCK)
        snap = (r["eras"][0]["era"], r["eras"][0]["n"], r["eras"][0]["p_value"],
                r["eras"][0]["p_adjusted"], r["eras"][0]["delta"])
        eras = regime.build_anchored_eras(d[:n], block=BLOCK)
        bsnap = (eras[0]["start"], eras[0]["end"], eras[0]["anchor_issue"])
        if first is None:
            first = (snap, bsnap)
        assert (snap, bsnap) == first, f"第 {k} 次追加后 P1 变了"


# ---------- 6. 反向用例：旧实现在同一条断言下必然失败 ----------

def test_legacy_adaptive_split_fails_the_same_assertion():
    """**反向用例**：把同一条断言跑在旧实现上，它**必须失败** —— 证明测试真能抓回归。

    旧实现（``_legacy_adaptive_eras``）从**最新一期往过去**切，``end`` 恒为 ``n``，
    所以每加一期每一段的下标都整体漂移：这里加 10 期，9 段**全部**平移 10 期，
    样本换了 → p 值全变。若哪天有人把默认改回旧逻辑，上面那条 anchored 断言会立刻红。
    """
    n = 1350
    d1, d2 = _draws(n), _draws(n + 10)
    s1 = _series(n, effect=((0, 150, -0.4),))
    s2 = s1 + _series(n + 10)[n:]
    e1 = regime.build_adaptive_eras(d1, block=BLOCK, anchor_mode="legacy")
    e2 = regime.build_adaptive_eras(d2, block=BLOCK, anchor_mode="legacy")
    r1 = regime.regime_report(s1, d1, block=BLOCK, anchor_mode="legacy")
    r2 = regime.regime_report(s2, d2, block=BLOCK, anchor_mode="legacy")

    drifted = [i for i, (a, b) in enumerate(zip(e1, e2)) if (a["start"], a["end"]) != (b["start"], b["end"])]
    assert len(drifted) == 9, "旧实现下 9 段应当**全部**漂移（这正是缺陷）"
    for i in drifted:
        # 末段恒为 n → 每段 end 都前进 10 期；start 也前进 10 期（P1 例外：它被钉在 0，
        # 因为旧实现把余数并入最早段，所以 P1 的「起点不动、终点前移」正是样本被换掉的证据）
        assert (e2[i]["end"] - e1[i]["end"]) == 10, "末段锚在 n 上 → 每段终点都漂移新增期数"
        if i > 0:
            assert (e2[i]["start"] - e1[i]["start"]) == 10, "旧实现下各段起点整体后移"
        else:
            assert e1[0]["start"] == e2[0]["start"] == 0
            assert e1[0]["n"] != e2[0]["n"], "P1 吸收了驻留余数 → 分期数被新数据改变"
        assert r1["eras"][i]["p_value"] != r2["eras"][i]["p_value"], \
            "边界漂移后同一历史段的 p 值必须发生变化（不可复现的直接证据）"

    # 同一条断言下，锚定实现必须是**通过**的（对照，证明断言本身可满足）
    a1 = regime.build_adaptive_eras(d1, block=BLOCK, anchor_mode="anchored")
    a2 = regime.build_adaptive_eras(d2, block=BLOCK, anchor_mode="anchored")
    for i in range(len(_tail_complete_eras(a1, n))):
        assert (a1[i]["start"], a1[i]["end"]) == (a2[i]["start"], a2[i]["end"])
    assert r1["era_anchor_mode"] == "legacy" and a1[0]["era_schema"] == 2


# ---------- 7. 多重性口径：分母锁定 ----------

def test_multiplicity_denominator_is_locked_not_current_era_count():
    """分母必须是固定上限，与当前段数无关 —— 否则"加一期→段数变→历史段 p_adj 变"。"""
    d = _draws(1409)
    r = regime.regime_report(_series(1409), d, block=BLOCK)
    assert r["adj_mode"] == "locked"
    assert r["m_adj_denom"] == regime.ERA_FAMILY_M      # 常数，不是 9/10 之类的当前段数
    assert r["m_adj"] == 9                              # 实际被测段数（末段 59 期 < 60 未测）
    assert r["m_adj_locked"] is True and r["m_adj_overflow"] is False


def test_locked_denominator_keeps_history_stable_when_era_count_changes():
    """段数从 9 变 10 的临界点：locked 口径下历史段 p_adjusted 一字不变；BH 口径下会变。

    这就是为什么必须"锚定 + 分母锁定"两件事一起做：
    只锚定边界、却用当前段数做 BH，用户的"校正后 p"依旧会随新数据跳动。
    """
    s1409, s1410 = _series(1409, effect=((150, 300, 0.55),)), _series(1410, effect=((150, 300, 0.55),))
    d1409, d1410 = _draws(1409), _draws(1410)
    locked1 = regime.regime_report(s1409, d1409, block=BLOCK, adj_mode="locked")
    locked2 = regime.regime_report(s1410, d1410, block=BLOCK, adj_mode="locked")
    assert locked1["m_adj"] == 9 and locked2["m_adj"] == 10, "本用例要跨过'已测段数变化'的临界点"
    for i in range(9):
        assert locked1["eras"][i]["p_adjusted"] == locked2["eras"][i]["p_adjusted"], \
            f"locked 口径下 {locked1['eras'][i]['era']} 的校正后 p 不得随段数变化"
    # 对照：BH 口径会把分母从 9 变成 10，历史段的校正后 p 随之改变（因此不作为默认）
    bh1 = regime.regime_report(s1409, d1409, block=BLOCK, adj_mode="bh")
    bh2 = regime.regime_report(s1410, d1410, block=BLOCK, adj_mode="bh")
    assert bh1["m_adj_denom"] == 9 and bh2["m_adj_denom"] == 10
    assert any(bh1["eras"][i]["p_adjusted"] != bh2["eras"][i]["p_adjusted"] for i in range(9)), \
        "BH 口径下分母随段数增长，历史段 p 必然漂移 —— 这正是 locked 口径要避免的"


def test_locked_adjustment_is_plain_bonferroni_with_constant():
    """locked 口径的定义：``p_adj = min(1, p × ERA_FAMILY_M)``，只依赖自身 p。"""
    ps = [1e-6, 0.001, 0.02, 0.5, 0.9]
    adj = regime.family_adjust_locked(ps)
    for p, a in zip(ps, adj):
        assert a == pytest.approx(min(1.0, p * regime.ERA_FAMILY_M), rel=1e-12, abs=0.0)
    assert regime.family_adjust_locked([None]) == [None]
    # 同族其它 p 值变化不影响某一段的结果（与 BH 的本质区别）
    assert regime.family_adjust_locked([0.001]) == [regime.family_adjust_locked([0.001, 1e-9, 0.3])[0]]


def test_m_adj_total_is_eras_times_dimensions():
    """``m_adj_total = 阶段数 × 维度数``：把"段 × 维度"的联合搜索规模显式给出（R2 建议 2）。"""
    d = _draws(1200)
    s = _series(1200)
    r1 = regime.regime_report(s, d, block=BLOCK)
    assert r1["m_adj_total"] == len(r1["eras"]) * 1 and r1["m_adj_n_dims"] == 1
    r20 = regime.regime_report(s, d, block=BLOCK, n_dims=20)
    assert r20["m_adj_total"] == len(r20["eras"]) * 20
    # 它只用于审计/展示，不参与 p_adjusted 计算（否则分母又会随段数漂移）
    assert [e["p_adjusted"] for e in r1["eras"]] == [e["p_adjusted"] for e in r20["eras"]]


def test_reserved_denominator_overflow_is_flagged_not_silently_widened(monkeypatch):
    """超出预留上限时必须**显式报警**，而不是静默扩大分母（静默扩分母就是漂移）。"""
    monkeypatch.setattr(regime, "ERA_FAMILY_M", 2)
    d = _draws(1200)
    r = regime.regime_report(_series(1200), d, block=BLOCK)
    assert r["m_adj_denom"] == 2
    assert r["m_adj"] > 2 and r["m_adj_overflow"] is True
    assert "预留上限" in r["note_zh"]


# ---------- 8. 末段未达门槛时，状态机不得把"样本不足"说成"正在衰减" ----------

def test_untested_tail_era_does_not_fake_fading():
    """末段是不完整块且不足 ``min_era_n`` 时，'当前阶段'取最近**被判定过**的段。

    否则"当前块才攒了 20 期"会被误报成"正在衰减（fading）"—— 把数据不足说成证据消失，
    正是本仓库明令禁止的措辞错位。
    """
    d = _draws(1370)                     # 9 完整段 + 末段 20 期（< MIN_ERA_N=60）
    eras = regime.build_anchored_eras(d, block=BLOCK)
    assert len(eras) == 10 and eras[-1]["n"] == 20 and eras[-1]["complete"] is False
    adj = [0.5] * 9 + [None]             # 前 9 段都不显著，末段未判定
    st = regime.regime_status(eras, adj, [0.01] * 9 + [None], None, len(d))
    assert st["status"] == "noise"

    # 较早段与最近**被判定**段都显著、末段未判定 → persistent（而不是 fading）
    adj2 = [0.5] * 9 + [None]
    adj2[3] = 0.001
    adj2[8] = 0.001
    st2 = regime.regime_status(eras, adj2, [0.3] * 9 + [None], 8, len(d))
    assert st2["status"] == "persistent", f"末段未判定时不得判为 {st2['status']}"
    assert "尚不足以判定" in st2["reason_zh"]
    assert st2["latest_era"] == eras[8]["name"]


# ---------- 9. 真实规模（3515 期）下的形状 ----------

def test_real_scale_shape_23_full_eras_plus_short_tail():
    """3515 期 / 块 150：23 个完整段 + 1 个 65 期短末段 = 24 段。

    旧实现是 23 段、最老段 215 期（因余数被并入最早段）—— 那个"215"本身就是
    "从末尾切"的产物：数据一多，最老段长度就会变。锚定后最老段恒为 150。
    """
    d = _draws(3515)
    eras = regime.build_anchored_eras(d, block=BLOCK)
    assert len(eras) == 24
    assert sum(1 for e in eras if e["complete"]) == 23
    assert eras[0]["n"] == 150, "最老段必须是完整的 150 期（旧实现是 215）"
    assert eras[-1]["n"] == 65 and eras[-1]["complete"] is False
    assert eras[-1]["start"] == 23 * BLOCK
    assert eras[-1]["end"] == 3515

    legacy = regime.build_adaptive_eras(d, block=BLOCK, anchor_mode="legacy")
    assert len(legacy) == 23 and legacy[0]["n"] == 215
    assert legacy[0]["era_schema"] == 1


# ---------- 10. 既有口径不回归 ----------

def test_calendar_mode_is_untouched():
    """自然年口径（供 LLM 上下文与人读对照）不受本次改动影响。"""
    d = _draws(3515)
    cal = regime.eras_for(d, mode="calendar")
    assert cal == regime.build_eras(d)
    assert len(cal) == 9
    r = regime.regime_report(_series(3515), d, era_mode="calendar")
    assert r["era_mode"] == "calendar" and r["era_block"] is None


def test_regime_report_keeps_consumed_fields():
    """``discovery`` / ``api_app`` / ``patterns_runtime`` / 前端实际消费的字段必须齐全。"""
    d = _draws(1200)
    r = regime.regime_report(_series(1200), d, block=BLOCK)
    for key in ("label", "era_mode", "era_block", "n_total", "m_adj", "n_sig_eras",
                "eras", "recent", "status", "label_zh", "reason_zh",
                "earliest_era", "latest_era", "last_significant_era", "note_zh"):
        assert key in r, f"消费方读取的字段 {key} 不得丢失"
    assert r["era_mode"] in ("adaptive", "calendar")
    for e in r["eras"]:
        for key in ("era", "n", "p_value", "p_adjusted"):
            assert key in e
    assert set(["window", "n", "mean", "delta", "p_value", "p_adjusted"]) <= set(r["recent"])


def test_empty_and_tiny_inputs_are_safe():
    assert regime.build_anchored_eras([]) == []
    assert regime.build_adaptive_eras([], anchor_mode="legacy") == []
    d = _draws(10)
    eras = regime.build_anchored_eras(d, block=BLOCK)
    assert len(eras) == 1 and eras[0]["n"] == 10 and eras[0]["complete"] is False
    r = regime.regime_report(_series(10), d, block=BLOCK)
    assert r["status"] == "insufficient"


def test_p_values_are_finite_or_none():
    """锚定 + locked 不得产出 NaN/inf（前端会直接显示成 "NaN"）。"""
    d = _draws(1500)
    r = regime.regime_report(_series(1500, effect=((600, 750, 0.3),)), d, block=BLOCK)
    for e in r["eras"]:
        for key in ("p_value", "p_adjusted", "delta"):
            v = e.get(key)
            assert v is None or math.isfinite(float(v)), f"{e['era']}.{key}={v!r} 不是有限值"
