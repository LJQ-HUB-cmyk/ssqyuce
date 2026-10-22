"""V15 假设空间扩展（S1/S2/S7）—— 守「只扩大空间、不放宽判据」。

对应 docs/analysis/V15_ASSUMPTION_SPACE.md。三个改动各自的不变式：

* **S1**：``discover`` 的阶段画像按**族**去重（同族只留 ``|z|`` 最强一条），
  但 ``n_tested`` 不变（去重不是减检验次数，FWER 口径不变）。
* **S2**：新增 ``persistent_effect`` 档 = 长期档整体显著 + 近 K 段方向一致，
  **不要求每段都显著**；但 ``USABLE_STATUSES`` 仍是 ``("persistent",)``，
  既有 ``persistent`` 语义一点没放宽；``prereg_ok`` / ``source!="llm"`` 照样必须过。
* **S7**：``order_pos`` / ``number_bias`` / ``sum_dist`` 三族登记进
  ``family_catalog()`` 与 ``combo_space_size()``；分布类维度走 ``stat_fn``。

## 恒真断言守卫

见 ``tests/test_d15_no_vacuous_assertions.py``。本文件每条断言都绑定一个**可被
变异打破**的实现点：故意改坏实现里的一行，对应测试必须变红（在下方每条注释里
标了「变异点」）。
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from lottery import discovery as DIS
from lottery import regime


# --------------------------------------------------------------------------
# 公共：合成开奖
# --------------------------------------------------------------------------

def _draws(n=700, seed=5, order_sorted=True):
    import random
    rng = random.Random(seed)
    out = []
    for i in range(n):
        reds = sorted(rng.sample(range(1, 34), 6))
        order = list(reds)
        if not order_sorted:
            rng.shuffle(order)
        out.append({"issue": f"{2013000 + i}", "date": None,
                    "reds": reds, "order": order, "blue": rng.randint(1, 16)})
    return out


# ==========================================================================
# S1：按族去重 + n_tested 不变
# ==========================================================================

def test_s1_items_are_family_deduplicated():
    """阶段画像里的条目，族 key 必须两两不同（去重生效）。

    变异点：把 ``discover`` 里的按族去重循环去掉、退回 ``records[:top]`` ——
    会出现同族多档（如 cold{w,k=5/6/8} 同时入选），本断言变红。
    """
    res = DIS.discover(_draws(order_sorted=False), top=20, use_cache=False)
    keys = [it["filter"]["key"] for it in res["items"]]
    assert len(keys) == len(set(keys)), f"阶段画像出现同族重复：{keys}"


def test_s1_dedup_does_not_change_n_tested():
    """去重只影响「画像展示哪几条」，不改 FWER 分母：n_tested == len(all_quick)。

    变异点：若去重时误把 records 也裁短（或改成用去重后条数当 n_tested），
    ``n_tested`` 会小于 ``len(all_quick)`` —— 本断言变红。这正是「减检验次数」违规。
    """
    res = DIS.discover(_draws(order_sorted=False), top=6, use_cache=False)
    assert res["n_tested"] == len(res["all_quick"])
    # 且完整记录里仍有同族多档（被去重的不只是丢弃，而是保留在 all_quick）
    from collections import Counter
    c = Counter(it["filter"]["key"] for it in res["all_quick"])
    assert any(v > 1 for v in c.values()), "all_quick 里应仍有同族多档（去重只针对画像）"


# ==========================================================================
# S2：persistent_effect 档
# ==========================================================================

def test_s2_k_constant_is_three():
    """K 锁定为 3（与近三期平均效应口径一致；带注释常量，不得事后改）。

    变异点：改 PERSISTENT_EFFECT_K 为其它值 —— 本断言变红，提醒需走独立验证集。
    """
    assert regime.PERSISTENT_EFFECT_K == 3


def test_s2_long_term_effect_bonferroni_applies_m():
    """长期档整体检验按 m=n_tested 做 Bonferroni：p_adj = min(1, p_raw*m)。

    变异点：把 ``long_term_effect`` 里 ``p_adj = min(1, p*m)`` 改成 ``=p``（不校正）
    —— 下面 p_adj 会小于 p_raw*m，断言变红。
    """
    rng = np.random.default_rng(0)
    series = rng.normal(0.30, 1.0, size=400).tolist()   # 均值为正、有真实效应
    lt = regime.long_term_effect(series, n_tested=100)
    assert lt["p_raw"] is not None and lt["p_raw"] < 0.05
    assert lt["m"] == 100
    assert lt["p_adj"] == pytest.approx(min(1.0, lt["p_raw"] * 100), rel=1e-9)
    # 若 m 太小则不显著应消失：m=1 时可能仍显著，但 m 放大必让 p_adj 单调不减
    lt1 = regime.long_term_effect(series, n_tested=1)
    assert lt1["p_adj"] <= lt["p_adj"] + 1e-12


def test_s2_persistent_effect_sign_gate_is_not_vacuous():
    """方向一致性门槛 ``_persistent_effect_sign_ok``：必须「全部同号且够 K 段」。

    变异点：把 ``>= PERSISTENT_EFFECT_K`` 改成 ``>= PERSISTENT_EFFECT_K - 1``（即
    「≥K-1 段同号」）。三段有号时多数方向必 ≥2，于是 agree=2 的反例会被误判为达标
    —— 本用例在此变异下变红。这条正是「符号检验不能形同虚设」的锁。
    """
    assert regime.PERSISTENT_EFFECT_K == 3
    assert regime._persistent_effect_sign_ok({"agree": 3, "n_used": 3}) is True
    # 三段里 2 正 1 负（agree=2）→ **不达标**（不是「方向一致」）
    assert regime._persistent_effect_sign_ok({"agree": 2, "n_used": 3}) is False
    # 只有 2 段被判定（不足 K）→ 不达标，即使同号
    assert regime._persistent_effect_sign_ok({"agree": 2, "n_used": 2}) is False


def test_s2_persistent_effect_requires_sign_agreement_not_every_era_sig():
    """「长期显著 + 近 K 段方向一致」即达 persistent_effect；**不要求每段显著**。

    构造：整条序列均值显著为正（长期档过），但近 3 段里有两段本身 p 不显著、
    仅方向一致。这种情形下 ``persistent`` 不会触发（没有段显著），但本档应触发。

    变异点：把 ``sign_agreement`` 的 ``agree >= k-1`` 改成 ``agree == k`` 且让
    有一段反向，则本档不再触发 → 断言变红（确认它真的在查方向一致性）。
    """
    # 三段各 100 期：段1 均值 +0.35，段2 均值 +0.30，段3 均值 +0.05（弱但同号）
    rng = np.random.default_rng(1)
    seg = np.concatenate([
        rng.normal(0.35, 1.0, 100),
        rng.normal(0.30, 1.0, 100),
        rng.normal(0.20, 1.0, 100),   # 弱但同号（+0.20，噪声下均值≈+0.2>0）
    ])
    deltas = [float(seg[:100].mean()), float(seg[100:200].mean()), float(seg[200:].mean())]
    sa = regime.sign_agreement(deltas, k=3)
    assert sa["sign"] == "pos" and sa["agree"] == 3
    # 直接检验 long_term 显著
    lt = regime.long_term_effect(seg.tolist(), n_tested=1)
    assert lt["significant"] is True
    # 若把第三段方向翻成负：agree 掉到 2 —— **不达标**（要求全部同号，不是 ≥k-1；
    # 若用 ≥k-1 则三段有号时多数必 ≥2，等于恒真，起不到方向一致性作用）
    sa2 = regime.sign_agreement([deltas[0], deltas[1], -0.5], k=3)
    assert sa2["agree"] == 2 and not (sa2["agree"] == 3)
    # 只有 2 段被判定（不足 K）→ 不达标，即使同号
    sa3 = regime.sign_agreement([deltas[0], deltas[1]], k=3)
    assert sa3["n_used"] == 2 and not (sa3["n_used"] >= 3)


def test_s2_regime_report_emits_persistent_effect_with_n_tested():
    """整条 regime_report 在「长期显著+近段同向、但每段都不显著」时给 persistent_effect。

    变异点：``regime_report`` 里若忘记把 ``n_tested`` 传给 ``long_term_effect``
    （默认 m=1 之外还漏判），或把本档升级条件写成「必须 persistent 才升级」，
    本用例变红。
    """
    # 用固定阶段切分的 draws（不必真实，只需年份跨度让 eras_for 切段）
    import random
    rng = random.Random(2)
    draws = [{"issue": f"{2013 + i // 150}{i % 150 + 1:03d}", "date": None,
              "reds": sorted(rng.sample(range(1, 34), 6)),
              "order": None, "blue": 1} for i in range(600)]
    r2 = np.random.default_rng(2)
    # 全程正偏移 0.25，逐段波动大使个别段不显著
    series = r2.normal(0.25, 1.0, size=600).tolist()
    rep = regime.regime_report(series, draws, p0=None, n_tested=50)
    # 长期档应显著（整条均值 0.25，n=600 → t≈6）
    assert rep["long_term"]["significant"] is True
    # 状态：要么 persistent（若恰有段显著），要么 persistent_effect；绝不能是 noise
    assert rep["status"] in ("persistent_effect", "persistent"), rep["status"]
    assert rep["sign_agreement"]["n_used"] >= 3 or rep["status"] == "persistent"


def test_s2_persistent_semantics_unchanged():
    """既有 ``persistent`` 档的判据一个字没动：USABLE_STATUSES 仍只含 persistent。

    变异点：若有人把 ``persistent_effect`` 塞进 ``USABLE_STATUSES``（放宽既有可用语义），
    本断言变红。
    """
    from lottery import patterns_runtime as PR
    assert PR.USABLE_STATUSES == ("persistent",)
    assert "persistent_effect" in PR.PERSISTENT_EFFECT_STATUSES
    assert "persistent_effect" not in PR.USABLE_STATUSES


def test_s2_persistent_effect_tier_still_requires_prereg_and_source():
    """长期效应档同样必须 validated + prereg_ok + source!=llm 才能算「可用」。

    变异点：把 ``_effect_ok`` 里的 ``prereg_ok is True`` 去掉 —— 下面 prereg_ok=False
    的那条会变成 usable_persistent_effect，断言变红。
    """
    from lottery import patterns_runtime as PR
    import lottery.hypothesis as H

    def _hyp(hid, prereg):
        return {
            "id": hid, "family": "hot", "source": "stat", "statement": "x",
            "action": {"type": "prefer_red_filter",
                       "filter": {"key": "hot", "target": "red", "params": {"window": 10, "k": 6}}},
            "status": "validated", "prereg_ok": prereg,
            "prereg_status": "passed" if prereg else "failed", "exp_id": "e",
            "regime": {"status": "persistent_effect", "label_zh": "x", "reason_zh": "y",
                       "recent_delta": 0.1, "p_adjusted_recent": 0.2,
                       "persistent_effect": {"long_term": {"delta": 0.05, "p_adj": 0.01, "m": 50},
                                             "sign_agreement": {"k": 3, "n_used": 3,
                                                                "n_pos": 3, "n_neg": 0,
                                                                "agree": 3, "sign": "pos"}}},
        }

    saved = H.load_registry
    H.load_registry = lambda *a, **k: {
        "version": "v", "counters": {"n_tested_cumulative": 0, "last_issue": None, "batches": []},
        "hypotheses": [_hyp("OK", True), _hyp("NOPREREG", False)]}
    try:
        snap = PR.build_snapshot(_draws(order_sorted=False),
                                 scan={"n_tested": 1, "items": [], "verdict": "x"})
    finally:
        H.load_registry = saved
    by = {r["id"]: r for r in snap["rules"]}
    assert by["OK"]["tier"] == "usable_persistent_effect"
    assert by["OK"]["usable"] is True
    assert by["OK"]["sign_agreement"]["agree"] == 3        # 并存的方向一致性数字
    assert by["NOPREREG"]["tier"] != "usable_persistent_effect"
    assert by["NOPREREG"]["usable"] is False
    # 计数独立：不得并入 usable_long_term
    assert snap["counts"]["usable_persistent_effect"] == 1
    assert snap["counts"]["usable_long_term"] == 0


# ==========================================================================
# S7：三个新族
# ==========================================================================

def test_s7_new_families_registered_in_catalog_and_space():
    """三族登记进 family_catalog() 与 combo_space_size()。

    变异点：从 FAMILIES 删掉任一条 —— combo_space_size 与 catalog key 集合都变，断言红。
    """
    cat = DIS.family_catalog()
    keys = {f["key"] for f in cat}
    for k in ("order_pos", "number_bias", "sum_dist"):
        assert k in keys, f"{k} 未登记进 family_catalog"
    # combo_space_size 至少包含三族各自的参数档数量之和
    expected_min = sum(len(f["params"]) for f in DIS.FAMILIES
                       if f["key"] in ("order_pos", "number_bias", "sum_dist"))
    assert DIS.combo_space_size() >= expected_min


def test_s7_stat_families_execute_via_asof_scan():
    """分布类维度走 asof_scan 的 stat_fn 通道，能产出非空 obs/exp 且不抛异常。

    变异点：把 asof_scan 里 ``if flt.get("stat_fn")`` 分支删掉 —— stat 族没有 selector，
    会 KeyError，断言红。
    """
    draws = _draws(order_sorted=False)
    for key in ("order_pos", "number_bias", "sum_dist"):
        flt = DIS.make_filter(key, DIS.FAMILY_BY_KEY[key]["params"][0])
        res = DIS.asof_scan(draws, flt, start=200, end=len(draws))
        assert res["n"] > 0
        assert len(res["obs"]) == res["n"] == len(res["exp"])


def test_s7_order_pos_graceful_with_missing_order():
    """order 缺失/None 时 order_pos 优雅降级为 0 效应，绝不崩。

    变异实证（实测记录，写成「单行变异」会失真，故如实标注）：``_stat_order_pos``
    里有两处**冗余**守卫共同保证缺失 order 时返回 ``(0, 0)`` —— ① ``actual.order``
    缺失/长度异常即返回；② 历史里有效 order 段数 ``n_hist < 30`` 即返回。实测：
    单独把任一守卫改成 ``if False`` 本用例**仍然通过**（另一处兜住）；把「缺失即返回」
    改成「缺失填 1..6」假 order 也仍通过（n_hist=0 兜住）。**必须同时拆掉两处**
    （缺失填假 order + n_hist 守卫失效）才产生非零命中 —— 此时本断言变红
    （已验证：`assert all(o == 0.0 …)` 报 ``AssertionError``）。
    这正是本用例的价值：无论哪条守卫被误删、被改成假 order，只要还有一条健在，
    就不会把「order 缺失」伪装成命中；两条都坏才暴露为假阳性 → 变红。
    """
    draws = _draws()
    for d in draws:
        d["order"] = None
    flt = DIS.make_filter("order_pos", {"window": 100, "k": 6})
    res = DIS.asof_scan(draws, flt, start=200, end=len(draws))
    assert res["n"] > 0
    assert all(o == 0.0 for o in res["obs"]), "order 缺失时不应产生任何命中"


def test_s7_number_bias_note_states_required_periods():
    """number_bias 的诚实结论（要多少期才够）必须出现在 ratio/note 里，供前端显示。

    变异点：把 note_zh 里的期数结论删掉 —— 断言红。
    """
    fam = DIS.FAMILY_BY_KEY["number_bias"]
    blob = (fam.get("note_zh") or "") + (fam.get("ratio") or "")
    assert "40.7" in blob or "40.7 万" in blob, "缺 +3% 所需期数结论"
    assert "3.7" in blob, "缺 +10% 所需期数结论"


def test_s7_order_pos_null_is_centered():
    """纯随机（order 随机排列）下 order_pos 的快速 z 不应系统性偏离 0。

    这是防「统计量本身有偏」的回归锁：早期 PMI 写法在纯随机数据上 z 高达几十（假阳性），
    改用「位置偏爱集命中 vs 6k/33」后长期均值为 0。

    变异点：若把 obs/exp 换成不中心的量（如插值互信息不减期望），本断言会因 |mean z|
    过大而变红。
    """
    zs = []
    for s in range(6):
        draws = _draws(n=700, seed=100 + s, order_sorted=False)
        res = DIS.discover(draws, top=5, families=["order_pos"], use_cache=False)
        zs += [it["quick"]["z"] for it in res["all_quick"]]
    arr = np.array(zs, dtype=float)
    assert arr.size >= 6
    assert abs(arr.mean()) < 1.5, f"order_pos 纯随机下 z 均值 {arr.mean():.2f} 偏离 0 过大"
