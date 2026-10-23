"""V20 / L0：前向回测判据——任何「有信号」的结论必须再过一个确定性检验。

## 缺陷背景（docs/analysis/V19_AI_REDESIGN.md §2.3 实测）

现有唯一判据是置换检验。2026-10-23 实测：

* `ml_blue` 在 standard 档（B=200）下 `p=0.010`、`trusted=True`，换两个种子
  （0 / 7）分别是 `0.010` / `0.005`，**看起来稳定显著**；
* 同一份数据做严格前向回测：命中 `5.00%` vs 随机基线 `6.25%`，**z = -1.26**。

**置换检验显著 ≠ 可预测。** 所以 `signal_found` 需要第二道关。

## 本文件守什么

1. `walk_forward_check` 的口径正确（**不许**用 `per_issue.blue_hit` 累计——
   那是 `round(mean)`，5 注中 1 注会被 round 成 0，全样本恒为 0）；
2. 单次 z 过线但**多种子不稳健**时必须判否（实测 `stat:freq` 红球：
   z = 1.74 / 0.83 / -0.33，换种子就翻转）；
3. `combine_gates` 的双重门槛：置换 p 过但前向回测不过 → `monitor`；
4. 不可回放（`llm`）必须报 `unverifiable`，**不许**伪造 z 值，也不许因此放行；
5. ML 诊断顶层接入后，原本 `signal_found` 的通道会被前向回测降级。

## 恒真断言守卫

见 `tests/test_d15_no_vacuous_assertions.py`。每条断言绑定一个可变异的实现点。
"""
from __future__ import annotations

import math

import pytest

from lottery import walk_forward_gate as WFG


# --------------------------------------------------------------------------
# 合成数据：让 z 值可解析预期，不依赖真实库
# --------------------------------------------------------------------------

def _draws(n: int, seed: int = 3):
    import random
    rng = random.Random(seed)
    out = []
    for i in range(n):
        reds = sorted(rng.sample(range(1, 34), 6))
        out.append({"issue": f"{2003000 + i}", "date": None,
                    "reds": reds, "order": list(reds),
                    "blue": rng.randint(1, 16)})
    return out


def test_binomial_z_matches_definition():
    """`_binomial_z` 必须等于 (hits - n*p0)/sqrt(n*p0(1-p0))。

    变异点：改成不除标准差（只返回差值）—— 变红。
    """
    hits, n, p0 = 60.0, 1000.0, 0.0625
    exp = (hits - n * p0) / math.sqrt(n * p0 * (1 - p0))
    got = WFG._binomial_z(hits, n, p0)
    assert got is not None and abs(got - exp) < 1e-9, f"期望 {exp:.6f}，实际 {got}"


def test_binomial_z_returns_none_on_degenerate():
    """n=0 或 p0 越界时必须返回 None（不是 0，也不是 inf）。"""
    assert WFG._binomial_z(1.0, 0.0, 0.0625) is None
    assert WFG._binomial_z(1.0, 100.0, 0.0) is None
    assert WFG._binomial_z(1.0, 100.0, 1.0) is None


def test_blue_channel_uses_hit_rate_not_rounded_field():
    """蓝球命中数必须走 `blue_hit_rate × 总注数`，**不许**累计 `per_issue.blue_hit`。

    这是 V20 实测踩到的坑：`per_issue.blue_hit` 是
    ``round(mean(blue_hits))`` —— 5 注中 1 注时 mean=0.2、round=0，
    于是全样本累计恒为 0，得出 z=-14 的荒谬结论。

    变异点：把 hits 改回 `sum(per_issue.blue_hit)` —— 变红（z 会变成巨额负数）。
    """
    d = _draws(400)
    r = WFG.walk_forward_check(d, "stat:freq", n_tickets=5, start=200, color="blue")
    assert r["ok"] is True
    # 真实蓝球命中率应在 1/16 附近（合成数据也是均匀的），z 不该是 -14 这种量级
    assert r["hit_rate"] is not None
    assert 0.0 <= r["hit_rate"] <= 1.0, f"hit_rate 越界：{r['hit_rate']}"
    assert r["z"] is not None
    assert r["z"] > -5.0, (
        f"z={r['z']} 过小，符合『累计了 round(mean) 字段』的 bug 特征")


def test_red_channel_reports_per_issue_mean_not_percent():
    """红球口径是「每期平均命中个数」，`hit_rate` 应在 0~6 之间（不是 0~1 比率）。

    变异点：红球误用蓝球公式（除以注数）—— 变红。
    """
    d = _draws(400)
    r = WFG.walk_forward_check(d, "stat:freq", n_tickets=5, start=200, color="red")
    assert r["ok"] is True
    assert 0.0 <= r["hit_rate"] <= 6.0, f"红球 hit_rate 应为命中个数，实际 {r['hit_rate']}"
    assert abs(r["baseline"] - 6 * 6 / 33) < 1e-6, "红球基线应为 6*6/33"


# ==========================================================================
# 多种子稳健性：这是本次最重要的防线
# ==========================================================================

def test_multi_seed_fields_present():
    """结果必须带 `z_by_seed` / `z_median` / `robust` 三件套。

    变异点：删掉多种子循环 —— 变红（字段缺失）。
    """
    d = _draws(400)
    r = WFG.walk_forward_check(d, "stat:freq", n_tickets=5, start=200)
    for k in ("z_by_seed", "z_median", "robust"):
        assert k in r, f"缺少稳健性字段 {k}"
    assert isinstance(r["z_by_seed"], list) and len(r["z_by_seed"]) >= 2


def test_single_seed_pass_but_unstable_is_rejected(monkeypatch):
    """单次 z 过线但多种子中位数不过 → 必须 `passed=False`。

    变异点：把实现里的 `if z > WF_Z_MIN and out.get("robust")` 改回只看单次 z
    —— 本用例变红（第一版就是栽在这里：我只验了一个手造字典，
    从没调用真实代码，所以变异跑过去没红）。

    做法：monkeypatch `battle.replay_method`，让**第一次调用**返回高 z 的结果、
    **后续调用**（多种子稳健性检查）返回低 z，从而精确复现
    「单次过线、多种子不稳」这一真实场景（`stat:freq` 红球实测 1.74/0.83/-0.33）。
    """
    from lottery import battle as BT

    calls = {"n": 0}

    def _fake(method, draws, *, n_tickets=5, start=300, seed=42, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            # 第一次：红球均值远高于基线，制造一个大 z（**要有方差**，
            # 否则实现里 `if sd > 0` 会让 z=None 并提前 return，多种子根本不会跑）
            per = [{"red_hits": 2.0 if i % 2 else 4.0, "blue_hit": 0, "n_tickets": 5}
                   for i in range(60)]
            return {"replayable": True, "per_issue": per,
                    "blue_hit_rate": 0.0625, "n_tickets_per_issue": 5}
        # 后续（稳健性检查）：均值贴着基线但**必须有方差**，
        # 否则实现里 `if sd > 0` 会跳过、不产生 z（第一版就在这踩空）。
        per = [{"red_hits": 0.0 if i % 2 else 2.18, "blue_hit": 0, "n_tickets": 5}
               for i in range(60)]
        return {"replayable": True, "per_issue": per,
                "blue_hit_rate": 0.0625, "n_tickets_per_issue": 5}

    monkeypatch.setattr(BT, "replay_method", _fake)
    r = WFG.walk_forward_check([{"issue": "x"}], "stat:freq",
                              n_tickets=5, start=0, color="red")
    assert calls["n"] >= 2, "实现必须真的跑了多种子检查（否则这条断言没意义）"
    assert r["z"] is not None and r["z"] > WFG.WF_Z_MIN, (
        f"前提不成立：第一次的 z 应过线，实际 {r['z']}")
    assert r["robust"] is False, f"多种子应判为不稳健，实际 robust={r['robust']}"
    assert r["passed"] is False, (
        "单次 z 过线但多种子不稳健时必须判否 —— 否则换个种子就翻转的噪声会被当成信号")


# ==========================================================================
# 双重门槛合成
# ==========================================================================

def test_combine_gates_requires_both():
    """置换 p 过 + 前向回测过 → signal_found；任一不过 → monitor。

    变异点：把 `passed_perm and passed_wf` 改成 `or` —— 变红。
    """
    wf_ok = {"replayable": True, "z": 2.0, "passed": True}
    wf_bad = {"replayable": True, "z": -0.06, "passed": False}

    g = WFG.combine_gates(0.01, wf_ok)
    assert g["tier"] == "signal_found", f"两道都过应为 signal_found，实际 {g['tier']}"

    g2 = WFG.combine_gates(0.01, wf_bad)
    assert g2["tier"] == "monitor", (
        f"置换过但前向回测不过必须降级（这是 ml_blue 的真实情形），实际 {g2['tier']}")
    assert "walk_forward" in g2["gates_failed"]

    g3 = WFG.combine_gates(0.9, wf_ok)
    assert g3["tier"] == "monitor", "置换不显著不得因前向回测过而放行"
    assert "permutation" in g3["gates_failed"]


def test_combine_gates_marks_unverifiable():
    """不可回放时必须 `unverifiable=True` 且降级，**不许**放行也不许伪造 z。

    变异点：把 `unverifiable` 分支改成直接 return signal_found —— 变红。
    """
    wf_unrep = {"replayable": False, "z": None, "passed": False}
    g = WFG.combine_gates(0.01, wf_unrep)
    assert g["tier"] == "monitor"
    assert g["unverifiable"] is True
    assert g["passed_wf"] is False


def test_combine_gates_none_inputs():
    """perm_p 为 None（未做置换检验）时必须 monitor。"""
    g = WFG.combine_gates(None, {"replayable": True, "z": 3.0, "passed": True})
    assert g["tier"] == "monitor"
    assert "permutation" in g["gates_failed"]


# ==========================================================================
# 不可回放方法
# ==========================================================================

def test_llm_is_not_replayable_and_gives_no_z():
    """`llm` 不可回放 → `replayable=False`、`z=None`、`passed=False`。

    变异点：给不可回放方法伪造 z=0 或其他数字 —— 变红。
    """
    d = _draws(400)
    r = WFG.walk_forward_check(d, "llm", n_tickets=5, start=200)
    assert r["replayable"] is False
    assert r["z"] is None, "不可回放的方法不得给出 z 值（那会是伪造的证据）"
    assert r["passed"] is False
    assert "不可历史回放" in r["reason_zh"]


# ==========================================================================
# ML 诊断顶层接入
# ==========================================================================

def test_ml_diagnose_exposes_walk_forward_gate():
    """`diagnose_ml_channels` 必须带上 walk_forward 与 dual_gate 两件产出一一。

    变异点：删掉 ml_model 里接入 WFG 的那段 —— 变红。

    用极小的诊断规模跑（max_issues 小、单号码），只为验证**接线**，
    不验证统计结论（那需要长时间运行）。
    """
    from lottery import ml_model as ML
    d = _draws(400)
    out = ML.diagnose_ml_channels(d, mode="fast", max_numbers=1, max_issues=200)
    assert out.get("ok") is True, f"诊断未成功：{out.get('error')}"
    assert "walk_forward" in out, "顶层必须带前向回测结果"
    ch = out["channels"].get("ml_blue") or {}
    assert "dual_gate" in ch, "每个通道必须带双重门槛判定"
    assert ch["dual_gate"]["tier"] in ("signal_found", "monitor")
    # fast 档本来就会被降级；这里只要求 dual_gate 存在且字段自洽
    assert ch["dual_gate"]["unverifiable"] in (True, False)
