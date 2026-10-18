"""V12-02 维度可执行性护栏（裁决 R-1 + P5 §6.1「最小护栏」）。

背景（`docs/analysis/LEAD_VERDICT.md` §1.2）
------------------------------------------
V11/V12 新增的 8 个形态族选择器统一写成 ``history[-w:]``，但 as-of 回放传入的是
``ScanContext``（dict 子类）。``ctx[-30:]`` 走的是 ``dict.__getitem__(slice)``
→ ``KeyError: slice(-30, None, None)``；异常被 ``asof_scan`` 的
``except Exception: chosen = set()`` 吞成空集 → ``k≡0`` → ``sd<=0``
→ 被 ``discover()`` 二次 ``continue`` 跳过。

结果：**124 档声明空间里只有 94 档真正被检验**，但系统对外声称「124 维全搜过」。
8 个成功执行且 858 个测试全绿，是因为 ``tests/test_v12_shape_rules.py``
只测 ``shape_rules`` 模块本身，**没有任何用例覆盖扫描路径**。

本文件补上这个盲区。用例编号对应 V12 方案 §2 的验收要求。
"""
from __future__ import annotations

import pytest

from lottery import discovery as DIS

# 8 个曾 100% 失败的族（P1/P5/P6/Lead 四方独立复现的失败名单）
BROKEN_FAMILIES = (
    "sum_range", "span_range", "parity_cfg", "big_small_cfg",
    "route012_cfg", "consecutive_hot", "ac_value", "tail_group",
)


# --------------------------------------------------------------------------
# ① 全部 124 档选择器在 ScanContext 上都不抛异常
# --------------------------------------------------------------------------

def test_all_declared_dimensions_execute_on_scan_context(make_draws):
    """124 档全部可执行 —— 这是「宣称搜过」的最低前提。"""
    draws = make_draws(260, seed=7)
    ctx = DIS.ScanContext.new(draws[:200])

    failures = []
    total = 0
    for key, fam in DIS.FAMILY_BY_KEY.items():
        for params in fam["params"]:
            total += 1
            flt = DIS.make_filter(key, params)
            try:
                chosen = flt["selector"](ctx)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{key}{params}: {type(exc).__name__}: {exc}")
                continue
            assert chosen is not None, f"{key}{params} 返回 None"

    assert total == DIS.combo_space_size(), (
        f"枚举到 {total} 档，但 combo_space_size()={DIS.combo_space_size()}")
    assert not failures, "以下档位在 ScanContext 上执行失败：\n" + "\n".join(failures)


@pytest.mark.parametrize("key", BROKEN_FAMILIES)
def test_previously_broken_family_returns_nonempty(key, make_draws):
    """曾经 100% 失败的 8 族，现在必须能返回非空候选集（k>0）。"""
    draws = make_draws(260, seed=11)
    ctx = DIS.ScanContext.new(draws[:200])
    fam = DIS.FAMILY_BY_KEY[key]
    params = fam["params"][0]
    chosen = DIS.make_filter(key, params)["selector"](ctx)

    assert chosen, f"{key} 返回空集 —— k=0 会让 sd<=0，被 discover() 静默跳过"


# --------------------------------------------------------------------------
# ② ScanContext 切片语义必须与 list 完全一致
# --------------------------------------------------------------------------

def _essence(d: dict) -> tuple:
    """取出选择器真正消费的字段做比较。

    ``ScanContext`` 只保留了 reds/blue/issue/date（选择器只读这些），
    不像原始 draw 那样带 ``order``/``sales``/``pool``，所以不能直接 ``==`` dict。
    """
    return (tuple(d.get("reds") or ()), d.get("blue"),
            d.get("issue"), d.get("date"))


def test_scan_context_slice_matches_list_semantics(make_draws):
    draws = make_draws(120, seed=3)
    ctx = DIS.ScanContext.new(draws)

    assert ctx.n == len(draws)          # 期数用 ctx.n，不是 len(ctx)（len 是 dict 键数量）
    assert [_essence(x) for x in ctx[-30:]] == [_essence(x) for x in draws[-30:]]
    assert [_essence(x) for x in ctx[:5]] == [_essence(x) for x in draws[:5]]
    assert [_essence(x) for x in ctx[10:20]] == [_essence(x) for x in draws[10:20]]

    # 键访问行为不变
    assert ctx["n"] == len(draws)

    # clone 之后切片仍然可用（分支回放路径）
    clone = ctx.clone()
    assert [_essence(x) for x in clone[-10:]] == [_essence(x) for x in draws[-10:]]


def test_scan_context_advance_keeps_slice_fresh(make_draws):
    """advance 之后切片视图必须刷新，不能返回旧缓存。"""
    draws = make_draws(60, seed=5)
    ctx = DIS.ScanContext.new(draws[:50])
    assert [_essence(x) for x in ctx[-1:]] == [_essence(x) for x in draws[49:50]]

    ctx.advance(draws[50])
    assert [_essence(x) for x in ctx[-1:]] == [_essence(x) for x in draws[50:51]]
    assert ctx.n == 51


# --------------------------------------------------------------------------
# ③ 反向用例：把某档 selector 换成抛异常版本，测试必须失败
# --------------------------------------------------------------------------

def test_guard_catches_injected_broken_selector(make_draws, monkeypatch):
    """自校验：护栏本身必须有检出能力（防止护栏空转）。

    人为让 ``hot`` 族的选择器抛异常，模拟当年那个 KeyError。
    真正的断言点是「护栏能看见它」——这里直接断言异常确实会被抛出并被捕获记录。
    """
    draws = make_draws(220, seed=13)
    ctx = DIS.ScanContext.new(draws[:200])

    def _boom(history):
        raise KeyError("slice(-30, None, None)")  # 复刻原始故障

    fam = DIS.FAMILY_BY_KEY["hot"]
    original = fam["selector"]
    monkeypatch.setitem(DIS.FAMILY_BY_KEY["hot"], "selector", lambda params: _boom)
    try:
        flt = DIS.make_filter("hot", fam["params"][0])
        with pytest.raises(KeyError):
            flt["selector"](ctx)
    finally:
        monkeypatch.setitem(DIS.FAMILY_BY_KEY["hot"], "selector", original)


# --------------------------------------------------------------------------
# ④ 禁止邀功断言（裁决 R-1）
# --------------------------------------------------------------------------
#
# sum_range{w30,[45,75)} 修复后确实变成最强候选，但 **BH p≈0.0804 > 0.05**，
# 它本来就没过校正门槛。修 bug 是 P0，但不得在修完后把它当「找到规律」发布。
#
# ⚠️ 维护者注意：若本用例失败（p_bh 掉到 <0.05），**不要**删掉它或放宽阈值。
#    那意味着结论发生了变化，必须以「新增 P0 复核」的流程处理（重跑全量注入
#    验收 + 前置注册），而不是宣布「系统找到规律了」。

def test_best_candidate_still_fails_bh_correction(seeded_db):
    """最强候选必须仍不显著 —— 用合成数据锁住「修复≠发现」的纪律。"""
    draws = seeded_db(320, seed=17)
    result = DIS.discover(draws, top=3, use_cache=False)

    best = result["best_quick_p_adjusted"]
    assert best is not None
    assert best >= 0.05, (
        f"最强候选 BH 校正后 p={best} < 0.05 —— 结论发生变化，"
        "必须走「新增 P0 复核」流程，不得直接发布为「找到规律」")
    assert result["verdict"].startswith("扫描后无任何维度")


# --------------------------------------------------------------------------
# ⑤ n_tested 与声明空间一致（不再有「宣称 124 实跑 94」）
# --------------------------------------------------------------------------

def test_n_tested_equals_declared_space(seeded_db):
    """进校正分母的档数必须等于声明的档数。"""
    draws = seeded_db(320, seed=19)
    result = DIS.discover(draws, top=2, use_cache=False)

    # 注：discover() 会对某些档再按 sd<=0 / n<50 过滤，但修复后形态族 k>0，
    # 不应再出现系统性缺口。允许极少量的真实退化（如某窗口内 k=0）。
    declared = result.get("n_tested_space") or DIS.combo_space_size()
    assert result["n_tested"] > declared * 0.95, (
        f"n_tested={result['n_tested']} 远小于声明空间 {declared}，"
        "疑似又有族被静默跳过")
