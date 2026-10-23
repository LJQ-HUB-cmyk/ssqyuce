"""V18：严格模式**换门控依据**（L1 优先 / L2 兜底）—— 守"凭据诚实"。

## 背景（docs/analysis/V17_STRICT_MODE_EVALUATION.md）

严格模式原先硬性要求 `usable_replayable > 0`（L1 统计规律），而 L1 在 3515 期上
需要 **+28%** 真实效应才可达（n=150、m_cum≈3000 ⇒ z=4.31），真实数据最强只有 **+5.0%**
⇒ 差 5.6 倍，换算需 **31 年**。再叠加 `FROZEN_ISSUES=150`（≈1 年）冻结期
⇒ 严格模式**永久不出号**。

V18 改为「**L1 有则用 L1；无则用 L2 结构约束兜底**」，并在响应里如实标注
`evidence_basis ∈ {L1_statistical, L2_structural}`。

## 本文件守什么（核心是"不许冒充"）

1. L1 为空时，**不得**声称走的是 L1（这是本次改动最大的诚实风险）；
2. L1 与 L2 是两个独立计数，`evidence_l1_replayable` 必须如实反映 `usable_replayable`；
3. L2 兜底后门控**确实能通过**（否则改了等于没改）；
4. 两者都不可用时**仍然拒绝**（兜底不是无底线放行）；
5. `ALPHA` / `ERA_FAMILY_M` / `FROZEN_ISSUES` **一字不动**（本次只换依据，不动判据）。

## 恒真断言守卫

见 `tests/test_d15_no_vacuous_assertions.py`。每条断言绑定一个可变异的实现点。
"""
from __future__ import annotations

import pytest

from lottery import routes_predict as RP


# --------------------------------------------------------------------------
# 强制三种凭据状态（不依赖真实数据，避免测试随数据演进变红）
# --------------------------------------------------------------------------

def _force_l1(monkeypatch, n: int):
    from lottery import patterns_runtime as PRT
    monkeypatch.setattr(PRT, "load_snapshot",
                        lambda *a, **k: {"counts": {"usable_replayable": n}})


def _force_l2(monkeypatch, ok: bool):
    from lottery import shape_rules as SR
    if ok:
        complete = {"sum_min": 65.0, "sum_max": 134.0, "span_min": 17.0,
                    "span_max": 31.0, "top_zones": [(2, 2, 2)], "odd_range": [2, 3, 4],
                    "max_consecutive": 2}
        monkeypatch.setattr(SR, "shape_constraint_ctx", lambda draws: dict(complete))
    else:
        monkeypatch.setattr(SR, "shape_constraint_ctx",
                            lambda draws: {"sum_min": None, "sum_max": None,
                                           "span_min": None, "span_max": None,
                                           "top_zones": [], "odd_range": None,
                                           "max_consecutive": None})


def _force_strict_on(monkeypatch):
    from lottery import config
    monkeypatch.setattr(config, "STRICT_MODE", True, raising=False)


def _force_llm_ok(monkeypatch):
    from lottery import methods
    monkeypatch.setattr(methods, "resolve_channels",
                        lambda *a, **k: {"llm_can_pick": True, "decision_disabled": False})


# ==========================================================================
# 1. 依据判定：三种状态各有唯一正确答案（核心诚实性）
# ==========================================================================

def test_basis_is_L1_when_replayable_rules_exist(monkeypatch, make_draws):
    """有可回放规律时必须报 L1（更强的凭据不能被埋没）。

    变异点：把优先顺序反过来（先判 L2）—— 变红。
    """
    _force_l1(monkeypatch, 3)
    _force_l2(monkeypatch, True)
    ev = RP.strict_evidence_basis(make_draws)
    assert ev["basis"] == "L1_statistical", f"有 L1 时应优先报 L1，实际 {ev['basis']!r}"
    assert ev["n_replayable"] == 3


def test_basis_is_L2_when_L1_empty(monkeypatch, make_draws):
    """L1 为 0 时，依据必须是 L2_structural —— **不得**冒充 L1。

    变异点：把 `elif _ok_ctx` 分支的 basis 写成 "L1_statistical" —— 变红。
    这是本次改动最大的诚实风险：L2 通过只是"结构合规"，不是"找到规律"。
    """
    _force_l1(monkeypatch, 0)
    _force_l2(monkeypatch, True)
    ev = RP.strict_evidence_basis(make_draws)
    assert ev["basis"] == "L2_structural", f"L1 为 0 时应报 L2，实际 {ev['basis']!r}"
    assert ev["n_replayable"] == 0, "必须如实带回 L1=0，不能隐藏"
    assert "不代表" in ev["note_zh"], "L2 文案必须声明「不代表存在可预测规律」"


def test_basis_is_none_when_neither_available(monkeypatch, make_draws):
    """两者都不可用时必须是 none（兜底不是无底线放行）。"""
    _force_l1(monkeypatch, 0)
    _force_l2(monkeypatch, False)
    ev = RP.strict_evidence_basis(make_draws)
    assert ev["basis"] == "none", f"两者皆无时应为 none，实际 {ev['basis']!r}"


# ==========================================================================
# 2. 门控行为：L2 兜底**确实能通过**，但两者皆无仍拒绝
# ==========================================================================

def test_gates_pass_when_only_L2_available(monkeypatch, make_draws):
    """核心断言：L1=0 但 L2 可用时，门控必须**通过**（否则这次改动等于没改）。

    变异点：把 L1/L2 的择一逻辑改回「L1 硬性要求」—— 变红。
    这也正是 V18 之前的状态：严格模式永久不出号。
    """
    _force_strict_on(monkeypatch)
    _force_l1(monkeypatch, 0)
    _force_l2(monkeypatch, True)
    _force_llm_ok(monkeypatch)
    errs = RP.strict_mode_gates(make_draws, "2099999")
    keys = [e[0] for e in errs]
    assert keys == [], f"L1=0 但 L2 可用时应放行，实际未通过：{keys}"


def test_gates_reject_when_no_basis(monkeypatch, make_draws):
    """两者都不可用 → 必须拒绝，且 gate 名能说明是"没有凭据"。

    变异点：删掉 `else: _errs.append(("no_evidence_basis", ...))` —— 变红。
    """
    _force_strict_on(monkeypatch)
    _force_l1(monkeypatch, 0)
    _force_l2(monkeypatch, False)
    _force_llm_ok(monkeypatch)
    errs = RP.strict_mode_gates(make_draws, "2099999")
    keys = [e[0] for e in errs]
    assert "no_evidence_basis" in keys, f"两者皆无时必须拒绝，实际 {keys}"


# ==========================================================================
# 3. 判据一字不动（本次只换依据，不动门槛）
# ==========================================================================

def test_alpha_and_era_constants_unchanged():
    """`ALPHA` / `ERA_FAMILY_M` / `FROZEN_ISSUES` 必须保持原值 —— 不趁改依据偷偷放宽。

    变异点：把任一常量调小（例如 ERA_FAMILY_M 32→8）—— 变红。
    """
    from lottery import regime, hypothesis
    assert regime.ALPHA == 0.05, f"ALPHA 必须仍是 0.05，实际 {regime.ALPHA}"
    assert regime.ERA_FAMILY_M == 32, f"ERA_FAMILY_M 必须仍是 32，实际 {regime.ERA_FAMILY_M}"
    assert regime.ERA_ADJ_MODE == "locked", f"adj_mode 必须仍是 locked，实际 {regime.ERA_ADJ_MODE}"
    assert hypothesis.FROZEN_ISSUES == 150, (
        f"FROZEN_ISSUES 必须仍是 150（V18 未批准改动它），实际 {hypothesis.FROZEN_ISSUES}")


def test_persistent_effect_tier_still_not_in_usable():
    """`USABLE_STATUSES` 必须仍是 ("persistent",) —— 不借这次改动把新档塞进 usable。"""
    from lottery import patterns_runtime as PRT
    assert PRT.USABLE_STATUSES == ("persistent",), (
        f"USABLE_STATUSES 被改动了：{PRT.USABLE_STATUSES}")


# ==========================================================================
# 4. 前端必须显示依据类型（不许只写"严格模式"）
# ==========================================================================

def test_frontend_shows_evidence_basis():
    """前端在严格模式下必须显示凭据类型（统计规律 / 结构约束）。

    变异点：删掉 `res.evidence_basis` 的分支 —— 变红。
    **必须先剥注释**（`_source_assert.js_code`），否则注释里的字样会造成假绿。
    """
    import pathlib
    from tests._source_assert import js_code

    p = pathlib.Path(__file__).resolve().parents[1] / "web" / "static" / "app.js"
    src = js_code(p.read_text(encoding="utf-8"))
    missing = [k for k in ("evidence_basis", "L1_statistical", "L2_structural")
               if k not in src]
    assert not missing, f"前端未显示凭据类型，缺少：{missing}"
