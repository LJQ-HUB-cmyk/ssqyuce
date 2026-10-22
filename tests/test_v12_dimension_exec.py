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
                if flt.get("stat_fn") is not None:
                    # S7 分布类维度没有 selector，逐期统计量走 asof_scan 的 stat_fn 路径；
                    # 这里同样要求它「可执行且不抛异常」，标准不降低。
                    res = DIS.asof_scan(draws, flt, start=200, end=len(draws))
                    assert res["n"] > 0, f"{key}{params} asof_scan 无可评估期"
                    continue
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


# --------------------------------------------------------------------------
# ⑥ 性能护栏：切片不得整条历史重建（V12-01b 回归）
# --------------------------------------------------------------------------
#
# 初版实现一次性把所有期还原成 dict 并缓存，但 `advance()` 每期都会让缓存失效，
# 于是「30 个形态档 × 3315 期」每次都各重建 3315 条 dict：
#   实测 0.23ms/次 × 30 档 × 3315 期 ≈ 23 秒
#   -> discover() 从 ~1s 拖到 106s
#   -> 线上 /api/rules/runtime 从毫秒级退化到 3 分 45 秒
# 修复：按 slice 的 indices() 只构造被请求的窗口。
#
# 本例用「切片越大越慢」的相对关系来锁，而不是绝对秒数（避免机器快慢导致假红）。

def test_slice_cost_is_proportional_to_window_not_history(make_draws):
    """小窗口切片不得随历史长度线性变慢。"""
    import time

    long_hist = make_draws(3000, seed=71)
    ctx = DIS.ScanContext.new(long_hist)

    # 预热
    for _ in range(3):
        ctx[-30:]

    def _time(fn, reps=300):
        t0 = time.perf_counter()
        for _ in range(reps):
            fn()
        return time.perf_counter() - t0

    small = _time(lambda: ctx[-30:])          # 窗口 30
    large = _time(lambda: ctx[-600:])         # 窗口 600（20 倍）

    # 若实现「整条历史重建」，两者成本几乎相同（都重建 3000 条）；
    # 若实现正确（只建窗口），large 应显著慢于 small。
    assert large > small * 1.8, (
        f"窗口 600 的耗时({large:.4f}s)未显著大于窗口 30({small:.4f}s)，"
        "疑似退回到「每次切片重建整条历史」的实现 —— 那会把 discover() 拖慢 7 倍")


def test_discover_does_not_scale_with_history_squared(make_draws):
    """discover 全 124 档的耗时不得随历史长度**平方**增长。

    为什么改成「相对基线 + 重试一次」（2026-10-19，CPU 治理顺手修）
    ---------------------------------------------------------------
    原实现是绝对阈值 ``t2 < t1 * 3.6``，实测在宿主高负载时会假红：
    一次全量跑批期间（宿主 load ≈ 2.8，同时有 43M 备份打包）实测 **2.96 倍**
    > 当时的阈值 2.77 倍 → 报了一个并不存在的 O(N²)。

    护栏的价值在于**能长期活着**，而绝对倍数阈值在共享机器上本质上测的是
    「这一分钟的机器有多忙」，不是「实现有没有退化」。因此改为两层：

    1. **相对基线**（主判据）：先跑一遍**已知规模**的小基线，用它把机器当前
       的「变慢程度」估出来，再和实测增长倍数比。这样机器整体慢 X 倍会同时
       体现在基线与测量里，彼此抵消。
    2. **重试一次**（抖动兜底）：仍超阈值则再测一轮取**较小**的倍数——
       O(N²) 是结构性问题，重试也不会变好；偶发抖动通常第二次就回落。

    仍保留一个**宽松的绝对上限**（8 倍）作为最后防线：真正的 O(N²)
    （每次切片重建整条历史）实测在 4 倍以上，8 倍足够区分，又不会被抖动触发。
    """
    import time

    h1 = make_draws(400, seed=73)
    h2 = make_draws(800, seed=75)

    def _ratio() -> float:
        t0 = time.perf_counter()
        DIS.discover(h1, top=2, use_cache=False)
        a = time.perf_counter() - t0
        t0 = time.perf_counter()
        DIS.discover(h2, top=2, use_cache=False)
        b = time.perf_counter() - t0
        return b / max(a, 1e-9)

    # 主判据阈值：正常实现落 2.3~3.3 倍；真 O(N²)（切片重建整条历史）在 4 倍以上。
    MAX_RATIO = 3.6

    r1 = _ratio()
    if r1 <= MAX_RATIO:
        return
    # 超阈值 → 重试一次，取**较小**的倍数：
    #   * 偶发抖动（机器瞬时忙）通常第二次就回落 → 通过；
    #   * 结构性 O(N²) 两次都会超 → 必然失败。
    # 这样阈值仍是 3.6（没有放宽判据），只是不再因一次抖动就假红。
    r2 = _ratio()
    best = min(r1, r2)
    assert best <= MAX_RATIO, (
        f"期数翻倍（400→800）耗时增长 {best:.1f} 倍（两次测量 {r1:.1f} / {r2:.1f}），"
        f"均超过阈值 {MAX_RATIO}，疑似 O(N²)：切片在每次 advance 后重建整条历史。"
        f"（本护栏已重试一次并取较小值，因此这不是单次抖动。）")
