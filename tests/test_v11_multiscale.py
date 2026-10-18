"""V11 多尺度扫描测试。

设计依据：``docs/reviews/PHASE_USABLE_STUDY.md`` 与 Lead 的实测。

本模块要守住的**核心语义**（每条都要有测试）：
1. **MDE 随窗口变短而放大** —— 这是「短窗口不等于更容易找规律」的数学基础；
2. **``credible`` 必须同时满足"显著"与"效应达到 MDE"** —— 这是防止把噪声当发现的关键；
3. **短窗口的"显著"应被标成 ``noise-level``，而不是可信发现**；
4. **推荐值语义正确**（``short-noise`` 用于"只有短档显著、长档不显著"这种典型假象）。

诚实边界：本模块**不承诺**能找到规律。在真实数据上正确的输出通常是
"全部 not-significant" 或 "short-noise"，那正是系统诚实工作的表现。
"""
from __future__ import annotations

import random

import numpy as np
import pytest

from lottery import multiscale as MS


def _draws(n: int = 1200, seed: int = 7):
    rng = random.Random(seed)
    out = []
    for i in range(n):
        reds = sorted(rng.sample(range(1, 34), 6))
        out.append({"issue": f"{2003001 + i}", "date": None,
                    "reds": reds, "blue": rng.randint(1, 16)})
    return out


def _biased_draws(n: int = 1200, boost: float = 8.0, seed: int = 7):
    """注入强偏置：1~6 号权重 ×boost（用于验证"真能检出"这条路径是通的）。"""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        pool = list(range(1, 34))
        w = [boost if x <= 6 else 1.0 for x in pool]
        picked = []
        for _ in range(6):
            total = sum(w)
            r = rng.uniform(0, total)
            acc = 0.0
            k = 0
            for j, ww in enumerate(w):
                acc += ww
                if acc >= r:
                    k = j
                    break
            picked.append(pool.pop(k))
            w.pop(k)
        out.append({"issue": f"{2003001 + i}", "date": None,
                    "reds": sorted(picked), "blue": rng.randint(1, 16)})
    return out


# ---------------------------------------------------------------------------
# 1. MDE：窗口越短，检出下限越高
# ---------------------------------------------------------------------------

def test_mde_increases_as_window_shrinks():
    """窗口越短 → MDE 越大（这是"短窗口不等于更容易找规律"的数学基础）。"""
    m30 = MS.mde_for(30)["abs"]
    m60 = MS.mde_for(60)["abs"]
    m150 = MS.mde_for(150)["abs"]
    m3515 = MS.mde_for(3515)["abs"]
    assert m30 > m60 > m150 > m3515, (m30, m60, m150, m3515)


def test_mde_matches_theoretical_formula():
    """MDE 必须等于 (z_α + z_β)·σ/√n，不得随手取整。"""
    m = MS.mde_for(150)
    expect = (1.959964 + 0.841621) * 0.8678 / np.sqrt(150)
    assert m["abs"] == pytest.approx(expect, rel=1e-6)
    assert m["rel"] == pytest.approx(expect / (6 * 6 / 33), rel=1e-6)


def test_mde_rel_is_reported_as_ratio_of_baseline():
    """相对 MDE 应以 1.0909 为分母（rel = abs / baseline）。

    注意区分两个数：150 期的 **abs MDE = 0.1985**（绝对 Δ），
    而 **rel = 0.1985 / 1.0909 ≈ 0.182**（相对基线 +18.2%）。
    """
    m = MS.mde_for(150)
    assert m["abs"] == pytest.approx(0.1985, abs=0.001)
    assert m["rel"] == pytest.approx(0.1985 / (6 * 6 / 33), abs=0.002)
    assert m["rel"] == pytest.approx(0.1820, abs=0.002)


# ---------------------------------------------------------------------------
# 2. 档位定义：5 档、刻意不含 70~100
# ---------------------------------------------------------------------------

def test_five_scales_defined_with_expected_windows():
    keys = [s["key"] for s in MS.SCALES]
    assert keys == ["all", "long", "medium", "mid", "short"], keys
    windows = {s["key"]: s["window"] for s in MS.SCALES}
    assert windows["all"] is None
    assert windows["long"] == 150
    assert windows["medium"] == 300
    assert windows["mid"] == 60
    assert windows["short"] == 40


def test_scale_windows_deliberately_avoid_70_to_100():
    """刻意不含 70~100 期档：Lead 实测该区间延续率掉回随机（44~56%）。

    这条测试锁定该设计决定——若有人后来加了 80 期档，这里会红，
    从而强制他回来读 PHASE_USABLE_STUDY.md 的依据。
    """
    for s in MS.SCALES:
        w = s["window"]
        if w is None:
            continue
        assert not (70 <= w <= 100), f"窗口 {w} 落在实测最差的 70~100 区间"


def test_each_scale_carries_role_description():
    """每档都要有中文用途说明（前端直接展示，缺了用户看不懂这档能干什么）。"""
    for s in MS.SCALES:
        assert s["label_zh"] and s["role_zh"], s


# ---------------------------------------------------------------------------
# 3. credible 判据：显著 **且** 效应达到 MDE
# ---------------------------------------------------------------------------

def test_scale_scan_returns_all_scales():
    r = MS.scale_scan(_draws(), "hot", {"window": 30, "k": 6})
    assert set(r["scales"]) == {"all", "long", "medium", "mid", "short"}
    for v in r["scales"].values():
        assert "verdict" in v


def test_pure_noise_credible_rate_is_calibrated_not_zero():
    """纯噪声下 credible 的**出现率应受控（≈α 量级），而不是要求恒为 0**。

    为什么不能断言"绝不出现"：``credible`` 内含 p<0.05，而 α=0.05 的检验
    在 5 个尺度上本来就预期有 ~0.25 次假阳性/维度。要求恒为 0 等于要求
    假阳性率为 0——那会让检验同时失去功效（这是 V10 D-2 的教训）。

    正确做法：跨多个随机种子统计 credible 出现率，断言它落在 α 的量级内
    （这里取 ≤20%，远低于"假阳性 35%"那种失控状态）。
    """
    hits = 0
    seeds = list(range(20))
    for sd in seeds:
        r = MS.scale_scan(_draws(2000, seed=sd), "hot", {"window": 30, "k": 6})
        if any(v.get("credible") for v in r["scales"].values()):
            hits += 1
    rate = hits / len(seeds)
    assert rate <= 0.20, f"纯噪声下 credible 出现率 {rate:.0%} 过高（疑似判据失控）"


def test_pure_noise_prefers_not_significant_verdicts():
    """纯噪声下多数档应是 not-significant（而不是 noise-level）。"""
    r = MS.scale_scan(_draws(2000, seed=13), "repeat", {"back": 1})
    verdicts = [v.get("verdict") for v in r["scales"].values()
                if v.get("verdict") != "insufficient"]
    assert verdicts.count("not-significant") >= 3, verdicts


def test_strong_bias_is_detected_as_credible():
    """注入极强偏置（×8 权重给 1~6 号）→ 长档必须给出 credible。

    这条是**功效检验**：如果连这么强的效应都检不出，说明判据坏了
    （V10 的 D-2 缺陷正是"信号再强也判不出"）。
    """
    r = MS.scale_scan(_biased_draws(1500, boost=8.0), "hot", {"window": 30, "k": 6})
    cred = [k for k, v in r["scales"].items() if v.get("credible")]
    assert cred, f"强偏置未被判为 credible：{r['scales']}"
    assert r["recommendation"] == "watch"


def test_credible_requires_effect_above_mde():
    """逐档校验：凡是 credible 的，效应量必须 ≥ 该档 MDE（判据自洽）。"""
    r = MS.scale_scan(_biased_draws(1500, boost=8.0), "hot", {"window": 30, "k": 6})
    for k, v in r["scales"].items():
        if v.get("verdict") == "insufficient":
            continue
        if v.get("credible"):
            assert abs(v["delta"]) >= v["mde_abs"] - 1e-9, (k, v)


def test_noise_level_verdict_means_significant_but_below_mde():
    """verdict == 'noise-level' 必须满足：显著但效应 < MDE。"""
    r = MS.scale_scan(_draws(2000, seed=17), "hot", {"window": 50, "k": 6})
    for k, v in r["scales"].items():
        if v.get("verdict") == "noise-level":
            assert v["significant"] is True, (k, v)
            assert abs(v["delta"]) < v["mde_abs"], (k, v)


# ---------------------------------------------------------------------------
# 4. recommendation 语义
# ---------------------------------------------------------------------------

def test_recommendation_none_when_nothing_significant():
    r = MS.scale_scan(_draws(2000, seed=19), "hot", {"window": 30, "k": 6})
    if not any(v.get("significant") for v in r["scales"].values()):
        assert r["recommendation"] == "none"


def test_short_noise_requires_short_significant_and_long_not():
    """``short-noise`` 只能出现在"短档显著而长档不显著"时（典型多尺度假阳性）。"""
    r = MS.scale_scan(_draws(2000, seed=23), "hot", {"window": 30, "k": 6})
    if r["recommendation"] == "short-noise":
        long_sig = bool(r["scales"].get("all", {}).get("significant")
                        or r["scales"].get("long", {}).get("significant"))
        short_sig = bool(r["scales"].get("short", {}).get("significant")
                         or r["scales"].get("mid", {}).get("significant"))
        assert short_sig and not long_sig, r["scales"]


def test_recommendation_values_are_from_known_set():
    allowed = {"watch", "short-noise", "regime-shift", "weak", "none", "invalid"}
    for seed in (1, 2, 3):
        r = MS.scale_scan(_draws(800, seed=seed), "hot", {"window": 30, "k": 6})
        assert r["recommendation"] in allowed, r["recommendation"]


# ---------------------------------------------------------------------------
# 5. 批量扫描与健壮性
# ---------------------------------------------------------------------------

def test_multi_scale_scan_summary_structure():
    res = MS.multi_scale_scan(_draws(1200), only_interesting=False)
    assert res["n_scanned"] >= 5
    assert isinstance(res["summary"], dict)
    assert "note_zh" in res
    assert all("recommendation" in r for r in res["results"])


def test_multi_scale_scan_only_interesting_filters_none():
    res = MS.multi_scale_scan(_draws(1200), only_interesting=True)
    assert all(r.get("recommendation") != "none" for r in res["results"])


def test_invalid_dimension_is_handled_gracefully():
    """非法维度不得抛异常，应返回 error 标记。"""
    r = MS.scale_scan(_draws(600), "not_a_real_family", {})
    assert r.get("error") or r.get("recommendation") == "invalid"


def test_short_window_insufficient_is_marked():
    """数据太少时，窗口超出可用期数应标 insufficient 而不是给结论。"""
    r = MS.scale_scan(_draws(120), "hot", {"window": 30, "k": 6})
    insuff = [k for k, v in r["scales"].items() if v.get("verdict") == "insufficient"]
    assert insuff, r["scales"]


def test_all_results_are_json_serializable():
    """返回体必须能直接 json.dumps（前端/API 直接消费，不能有 numpy 对象）。"""
    import json
    r = MS.scale_scan(_draws(1000), "hot", {"window": 30, "k": 6})
    json.dumps(r)          # 不抛异常即通过
    res = MS.multi_scale_scan(_draws(1000), only_interesting=False)
    json.dumps(res)
