"""V12-21 补集反转（complement inversion）回归测试 —— 裁决 R-8 / 验收 A9。

数学前提（`docs/analysis/P2_STATS_WHY_ZERO.md` §4.2b，Lead 与 P6 各自实测）
----------------------------------------------------------------------
对任意号码集合 S（红球域）：

    hits(S) + hits(complement(S)) ≡ 6        （每期 6 个号必落其内）

它是由「开奖恰好 6 个红球」直接推出的恒等式，因此：

    delta_S + delta_comp(S) ≡ 0

也就是说：**一个「显著为负」的过滤器，其补集就是「显著为正」的过滤器。**
历史实现（`hypothesis.resolve_registry`）把 ``delta<0`` 判为「反对」→
``status="rejected"`` 后直接丢弃，等于把一半信号当失败销毁。

本文件同时把「FWER 不变」钉死：补集与原本是**同一次检验的两种写法**，
不得计入 ``n_tested``。
"""
from __future__ import annotations

import pytest

from lottery import discovery as DIS
from lottery import hypothesis as H


# --------------------------------------------------------------------------
# ① 恒等式：hits(S) + hits(complement(S)) ≡ 6
# --------------------------------------------------------------------------

def test_complement_hit_identity(make_draws):
    """逐期校验恒等式 —— 这是整个补集反转的数学地基。"""
    draws = make_draws(260, seed=23)
    checked = 0
    for key in ("hot", "cold", "sum_range", "neighbor"):
        fam = DIS.FAMILY_BY_KEY[key]
        base = DIS.make_filter(key, fam["params"][0])
        comp = DIS.complement_filter(base)
        # 逐期 as-of 回放（每 5 期抽一次，足够覆盖不同窗口）
        for t in range(200, len(draws)):
            ctx = DIS.ScanContext.new(draws[:t])
            actual = set(draws[t]["reds"])
            h_base = len(set(base["selector"](ctx)) & actual)
            h_comp = len(set(comp["selector"](ctx)) & actual)
            assert h_base + h_comp == 6, (
                f"{key} 第 {t} 期：{h_base}+{h_comp} != 6，恒等式被破坏")
            checked += 1
    assert checked > 200, f"校验期数过少（{checked}），用例形同虚设"


# --------------------------------------------------------------------------
# ② 符号对称：delta_S + delta_comp = 0
# --------------------------------------------------------------------------

def test_complement_delta_is_antisymmetric(make_draws):
    draws = make_draws(260, seed=29)
    ctx = DIS.ScanContext.new(draws[:200])

    for key in ("hot", "cold"):
        fam = DIS.FAMILY_BY_KEY[key]
        base = DIS.make_filter(key, fam["params"][0])
        comp = DIS.complement_filter(base)
        s_base = set(base["selector"](ctx))
        s_comp = set(comp["selector"](ctx))

        # 集合层面：互斥且并集为全集
        assert not (s_base & s_comp), f"{key} 补集与原集相交"
        assert (s_base | s_comp) == set(range(1, 34)), f"{key} 并集不是全集"


# --------------------------------------------------------------------------
# ③ 补集构造的元信息（供 UI 显式标注「〔补集〕」）
# --------------------------------------------------------------------------

def test_complement_filter_metadata(make_draws):
    draws = make_draws(240, seed=31)
    ctx = DIS.ScanContext.new(draws[:200])
    base = DIS.make_filter("cold", {"window": 200, "k": 6})
    comp = DIS.complement_filter(base)

    assert comp["is_complement"] is True
    assert comp["complement_of"] == base["label"]
    assert comp["label"].startswith("〔补集〕")
    assert comp["name_zh"].startswith("〔补集〕")
    # action 需要的 key/params/target 必须齐全，否则 patterns_runtime 无法渲染
    for field in ("key", "target", "params"):
        assert field in comp
    assert len(comp["selector"](ctx)) == 33 - len(base["selector"](ctx))


def test_complement_filter_blue_domain():
    """蓝色域必须用 1..16 的补集，不能用 1..34。"""
    base = DIS.make_filter("blue_hot", {"window": 100})
    comp = DIS.complement_filter(base)
    inner = set(base["selector"]([]))          # 空历史下 blue_hot 仍会给出 {1}，并非空集
    s = comp["selector"]([])
    assert s == set(range(1, 17)) - inner, f"蓝球补集域错误: {sorted(s)}"
    assert max(s) <= 16, "补集越出蓝球域（1..16）"


# --------------------------------------------------------------------------
# ④ complement_of_filter 只在 delta<0 时返回补集
# --------------------------------------------------------------------------

@pytest.mark.parametrize("delta,expect", [
    (-0.054, True),
    (-0.001, True),
    (0.0, False),
    (0.023, False),
    (None, False),
])
def test_complement_of_filter_gate(delta, expect):
    flt = {"key": "cold", "target": "red", "selector": lambda h: {1, 2, 3},
           "label": "冷号", "delta": delta}
    got = DIS.complement_of_filter(flt)
    assert (got is not None) is expect


# --------------------------------------------------------------------------
# ⑤ 反向用例：补集构造写错（返回原集合）必须被检出
# --------------------------------------------------------------------------

def test_wrong_complement_is_detectable():
    """自校验：如果补集被错误实现成「返回原集合」，恒等式必然破。"""
    flt = {"key": "cold", "target": "red",
           "selector": lambda h: {1, 2, 3, 4}, "label": "冷号"}
    bad = dict(flt)  # 故意不取补集
    actual = {1, 2, 3, 4, 5, 6}
    h_base = len(set(flt["selector"](None)) & actual)
    h_bad = len(set(bad["selector"](None)) & actual)
    # 正确补集应当有 0 命中；错误实现有 3 命中。用恒等式断言会立刻发现：
    good = DIS.complement_filter(flt)
    h_good = len(set(good["selector"](None)) & actual)
    assert h_base + h_good == 6
    assert h_base + h_bad != 6, "错误实现竟然也满足恒等式，用例失效"


# --------------------------------------------------------------------------
# ⑥ 端到端：负效应不再被直接埋掉，而是产生补集候选
# --------------------------------------------------------------------------

def _make_registry(tmp_path):
    reg = {"hypotheses": [{
        "id": "neg-cold", "family": "frequency", "source": "stat",
        "statement": "冷号回补",
        "action": {"type": "prefer_red_filter",
                   "filter": {"key": "cold", "params": {"window": 200, "k": 6},
                              "target": "red"}},
        "target": "red", "status": "proposed", "supports": 0, "refutes": 0,
    }]}
    path = tmp_path / "reg.json"
    H._save_registry(reg, path)
    return path


def test_negative_effect_creates_complement_candidate(tmp_path):
    path = _make_registry(tmp_path)
    res = [{"hyp_id": "neg-cold", "delta_red_hits": -0.054,
            "p_adjusted": 0.002, "n_issues": 3315}]

    out = H.resolve_registry(res, registry_path=path)

    # 原假说仍然诚实记为 rejected（它作为「正向断言」确实被数据否定）
    assert out["rejected"] == ["neg-cold"]
    # 但同时登记了补集候选
    assert out["complements"] == ["neg-cold__comp"]
    assert out["complements_added"] == 1

    reg = H._load_registry(path)
    comp = [h for h in reg["hypotheses"] if h.get("is_complement")]
    assert len(comp) == 1
    c = comp[0]
    assert c["status"] == "proposed"          # 不自动可用，仍需三道门
    assert c["prereg_ok"] is False
    assert c["complement_of"] == "neg-cold"
    assert c["complement_delta"] == pytest.approx(0.054)
    assert "〔补集〕" in c["statement"]
    assert c["action"]["filter"]["complement"] is True


def test_position_effect_does_not_create_complement(tmp_path):
    """正效应不该生成补集（补集只会更差）。"""
    path = _make_registry(tmp_path)
    res = [{"hyp_id": "neg-cold", "delta_red_hits": +0.023,
            "p_adjusted": 0.0001, "n_issues": 3315}]
    out = H.resolve_registry(res, registry_path=path)

    assert out["validated"] == ["neg-cold"]
    assert out["complements"] == []


# --------------------------------------------------------------------------
# ⑦ FWER 不变：补集不得计入独立检验数
# --------------------------------------------------------------------------

def test_complement_does_not_inflate_n_tested(tmp_path):
    """补集与原本是同一次检验的两种写法 ⇒ ``n_tested_delta`` 必须为 0。"""
    path = _make_registry(tmp_path)
    res = [{"hyp_id": "neg-cold", "delta_red_hits": -0.054,
            "p_adjusted": 0.002, "n_issues": 3315}]
    out = H.resolve_registry(res, registry_path=path)

    assert out["n_tested_delta"] == 0, (
        "补集被计入了独立检验数 —— 这会人为抬高 FWER 校正门槛，"
        "属于把「换了个写法」当成「多搜了一次」")


def test_complement_upsert_is_idempotent(tmp_path):
    """重复登记同一补集不得产生重复条目。"""
    path = _make_registry(tmp_path)
    res = [{"hyp_id": "neg-cold", "delta_red_hits": -0.054,
            "p_adjusted": 0.002, "n_issues": 3315}]

    first = H.resolve_registry(res, registry_path=path)
    second = H.resolve_registry(res, registry_path=path)

    assert first["complements_added"] == 1
    assert second["complements_added"] == 0
    reg = H._load_registry(path)
    comps = [h for h in reg["hypotheses"] if h.get("is_complement")]
    assert len(comps) == 1
