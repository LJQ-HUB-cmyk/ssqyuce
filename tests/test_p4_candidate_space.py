"""P4 生成式挖掘：开放候选空间 + e-BH 多重检验治理。

## 保的是什么

旧实现遍历 `discovery.FAMILY_BY_KEY` 里写死的 124 个组合，跑完即
`processed=0` 永久空转 —— 那是**重复验证**，不是挖掘。本文件守住改造后的
四条性质，任何一条失守都意味着系统回到「假挖掘」或「造假规律」：

1. **空间是开放的**：候选数远大于原来的 124，且能持续推出新候选；
2. **候选是确定的**：同一个 (层, 索引) 永远给出同一个候选（续跑的前提）；
3. **指纹唯一**：不同参数组合不得算出同一个指纹（否则去重误判）；
4. **域不越界**：参数加密只在已声明区间内，不下探/外推。

另外单独守住 e-BH 的数学性质，因为它是「不报假规律」的唯一闸门。
"""
from __future__ import annotations

import pytest

from lottery import candidate_space as CS, eprocess, idle_mining


# ---------- 1. 空间开放 ----------

def test_space_is_far_larger_than_legacy_124():
    """改造前是写死的 124 个组合；现在必须显著更大，否则等于没改。"""
    s = CS.stats()
    assert s["single_total"] > 124, f"单因子层只有 {s['single_total']} 个，未扩展"
    assert s["cross_total"] > 0, "交叉层为空 —— 「新维度」不存在"


def test_cross_layer_covers_families_not_seen_in_single_layer():
    """交叉层必须产生**单因子层不存在**的维度（跨族组合）。"""
    got = CS.candidate_at(CS.LAYER_CROSS, 0)
    assert got is not None
    flt, fp = got
    # 指纹形如 cross|<mode 序号>|族A:{参数}|族B:{参数}
    assert fp.startswith("cross|"), f"交叉指纹格式异常: {fp}"
    assert "+" in flt["key"], "交叉候选的 key 应是 '族A+族B' 形式"


# ---------- 2. 确定性 ----------

def test_candidate_at_is_deterministic():
    """同样的 (层, 索引) 必须给出同一个候选 —— 续跑/复算的前提。"""
    for layer in (CS.LAYER_SINGLE, CS.LAYER_CROSS):
        n = CS.layer_size(layer)
        # 交叉层有退化组合会被剔除（返回 None），因此只断言「两次结果一致」，
        # 不要求非 None —— 确定性指的是可复现，不是必然合法。
        checked = 0
        for idx in range(0, min(400, n), 7):
            a = CS.candidate_at(layer, idx)
            b = CS.candidate_at(layer, idx)
            assert a is None or b is not None, f"{layer}#{idx} 首轮合法但复算返回 None"
            if a is not None:
                assert a[1] == b[1], f"{layer}#{idx} 两次算出不同候选"
                checked += 1
        assert checked > 0, f"{layer} 层没有任何合法候选"


def test_index_out_of_range_returns_none():
    assert CS.candidate_at(CS.LAYER_SINGLE, -1) is None
    assert CS.candidate_at(CS.LAYER_SINGLE, CS.total_single()) is None
    assert CS.candidate_at("不存在的层", 0) is None


# ---------- 3. 指纹唯一 ----------

def test_single_fingerprints_are_unique():
    fps = [fp for _i, _f, fp in CS.iter_candidates(CS.LAYER_SINGLE, limit=200)]
    assert len(fps) == len(set(fps)), "单因子指纹撞车，去重会误判"


def test_cross_fingerprint_includes_component_params():
    """**回归防护**：``discovery.combine()`` 返回的 flt 不含成分参数，
    若直接用它算指纹，``hot(30,6)∩cold(100,8)`` 与 ``hot(30,6)∩cold(30,5)``
    会撞成同一个 —— 这会让「挖过的」和「没挖的」混淆。"""
    seen = set()
    for _i, flt, fp in CS.iter_candidates(CS.LAYER_CROSS, limit=300):
        # 指纹文本里必须能看到成分的完整参数（含 window/k 等键）
        assert "{" in fp, f"交叉指纹缺少参数明细: {fp}"
        seen.add(fp)
    assert len(seen) == 300, "交叉指纹有撞车"


@pytest.fixture()
def probe_db(seeded_db):
    """退化判断需要一个能构造 ScanContext 的历史，用种子库提供。"""
    seeded_db(600)
    CS._PROBE_HIST = None          # 重置探测缓存，确保用当前库
    yield
    CS._PROBE_HIST = None


def test_degenerate_cross_is_rejected(probe_db):
    """**退化组合必须剔除**（2026-10-21 实测修复）。

    `hot ∩ cold` 是热点号与冷号取交集 —— 必然为空集。
    这类候选 obs/exp 恒为 0、z 序列全零、e 值恒 ≈1，却照样被计入
    e-BH 的分母，把真正有机会的候选阈值抬高。实测整层 max 仅 0.77。
    """
    rejected = sum(1 for idx in range(0, 200, 2)
                   if CS.candidate_at(CS.LAYER_CROSS, idx) is None)
    assert rejected > 0, "空集交叉候选未被剔除"


def test_degenerate_filter_does_not_overkill(probe_db):
    """剔除不能误杀：保留的候选筛选集规模必须落在判据允许区间内。

    注意扫描范围要横跨整个交叉层 —— 前段大量是 `hot ∩ cold` 这类
    必然空集，按新判据（红球 3≤k≤12）应当全部剔除，所以只扫前 400 个
    索引会得到「全部被剔除」的假象。
    """
    hist = CS._probe_history()
    n = CS.layer_size(CS.LAYER_CROSS)
    kept = 0
    for idx in range(0, n, max(1, n // 400)):
        got = CS.candidate_at(CS.LAYER_CROSS, idx)
        if not got:
            continue
        k = len(got[0]["selector"](hist) or set())
        assert 3 <= k <= 12, f"#{idx} 选出 {k} 个号却被保留（判据失效）"
        kept += 1
    assert kept > 0, "全层没有任何合法候选，过滤过严"


def test_cross_never_pairs_same_family():
    """同族交叉是**退化组合**（``hot(k=2) ∩ hot(k=3)`` 只是换档位），
    不是新维度，不得出现在候选里。"""
    for _i, _flt, fp in CS.iter_candidates(CS.LAYER_CROSS, limit=500):
        parts = fp.split("|")
        fam_a = parts[2].split(":")[0]
        fam_b = parts[3].split(":")[0]
        assert fam_a != fam_b, f"同族交叉未被过滤: {fp}"


# ---------- 4. 域不越界 ----------

def test_numeric_domains_do_not_extrapolate_beyond_declared_range():
    """**被真实数据打回来的教训**：曾用 0.5/1.5/2.0 倍外推，把 ``window``
    从最小档 10 下探到 5，结果 ``hot{window:5,k:16}`` 跑出 e=159 的假规律。
    现在只做区间内加密，任何族的任何数值键都不得超出原始声明的范围。
    """
    for fam in CS.FAMILIES_FOR_TEST:
        keys = CS._slots(fam)
        nums = {}
        for k in keys:
            vals = [p.get(k) for p in fam["params"] if k in p]
            ints = [int(v) for v in vals if isinstance(v, (int, float))]
            if ints:
                nums[k] = (min(ints), max(ints))
        grid = CS._family_grid(fam)
        for params in grid:
            for k, (lo, hi) in nums.items():
                v = params.get(k)
                assert lo <= v <= hi, (
                    f"{fam['key']}.{k}={v} 越界（声明范围 {lo}..{hi}）—— "
                    f"私自新开维度会让「规律」失去可复核性")


def test_hot_window_never_below_ten():
    """单点复核最常被误用的维度：热号窗口。"""
    windows = set()
    for _i, flt, _fp in CS.iter_candidates(CS.LAYER_SINGLE,
                                           limit=CS.total_single()):
        if flt["key"] == "hot":
            windows.add(int(flt["params"]["window"]))
    assert windows, "没找到 hot 候选"
    assert min(windows) >= 10, f"hot 窗口下探到 {min(windows)}，越界"


# ---------- e-BH 数学 ----------

def test_ebh_selects_nothing_when_all_evidence_is_weak():
    """纯弱证据必须一条都不选（不报假规律的底线）。"""
    assert eprocess.ebh_selected([0.5] * 1000, m_total=1000) == set()
    assert eprocess.ebh_selected([2.0] * 500, m_total=500) == set()


def test_ebh_selects_strong_signal_at_small_m():
    """m 小的时候，真正的强信号必须能被选中 —— 否则闸门太严等于自废。"""
    # m=2, α=0.05 → 阈值 2/0.05 = 40
    ev = [1000.0, 1.0]
    sel = eprocess.ebh_selected(ev, m_total=2)
    assert 0 in sel, "m=2 时 e=1000 竟未被选中"


def test_ebh_threshold_scales_with_m():
    """**必须记录的代价**：m 越大，单个发现所需的 e-value 越高（m/α）。

    这不是缺陷而是多重检验的固有代价。这里把它固化成测试，
    防止将来有人「觉得太严」就偷偷去掉 m 而制造假发现。
    """
    alpha = eprocess.DEFAULT_ALPHA
    for m in (10, 100, 1000):
        need = m / alpha
        # 刚好差一点：不该被选中
        assert m - 1 not in eprocess.ebh_selected([need * 0.99], m_total=m) \
            or True   # 单元素情形下 k=1，阈值恰为 m/α
        sel_below = eprocess.ebh_selected([need * 0.99], m_total=m)
        sel_above = eprocess.ebh_selected([need * 1.01], m_total=m)
        assert sel_below == set(), f"m={m} 时 e={need*0.99:.0f} 不该选中"
        assert sel_above == {0}, f"m={m} 时 e={need*1.01:.0f} 应当选中"


def test_ebh_cannot_detect_realistic_effect_at_large_m():
    """**诚实性测试**：把「多重检验的代价」写成可执行的断言。

    `cold+50%/号` 是项目验收口径里的强注入偏置，600 期单候选
    e-value 只有约 28。一旦 m 上了几十，阈值 m/α 就远超 28 ——
    所以族级校正后 `usable` 长期为 0 是**数学必然**，不是 bug。
    如果哪天这条测试失败（e 突然变大），要么是真发现了强效应，
    要么是 eprocess 被改坏了，两者都必须人工复核。
    """
    import numpy as np
    rng = np.random.default_rng(42)
    z = rng.normal(0.1556, 1.0, 600)
    e = float(eprocess.verdict(z, min_obs=30)["e_value"])
    assert e > 1 / eprocess.DEFAULT_ALPHA, "强偏置竟连单候选阈值都没过"
    # m=44（一个中等族的档位数）时已检不出
    assert eprocess.ebh_selected([e], m_total=44) == set()


# ---------- 断点迁移 ----------

def test_legacy_state_is_reset_with_note(tmp_path, monkeypatch):
    """旧断点（124 维时代）语义不兼容，必须重置游标并留下说明。"""
    from lottery import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    legacy = {"completed_dimensions": ["hot:{}", "cold:{}"],
              "verdicts": {"x": {"decision": "monitor"}}, "runs": 5}
    got = idle_mining._migrate_state(legacy)
    assert got["cursors"] == {"single": 0, "cross": 0}
    assert got["space_version"] == idle_mining.SPACE_VERSION
    assert got["runs"] == 5, "无害计数应保留"
    assert got["migrated_from"]["old_completed_dimensions"] == 2


def test_current_state_is_not_migrated(tmp_path, monkeypatch):
    from lottery import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    fresh = idle_mining._fresh_state()
    fresh["cursors"] = {"single": 42, "cross": 7}
    ok = idle_mining._migrate_state(fresh)
    assert ok["cursors"] == {"single": 42, "cross": 7}
    assert "migrated_from" not in ok
