"""V22 / L2：AI 角色分离——「解释结果」与「参与选号」必须是两件事。

## 背景（docs/analysis/V19_AI_REDESIGN.md §1.2 / §4 层 2）

生产实测（45 注匹配样本）：`llm:*` 红球 1.265、`stat:*` 1.182，基线 1.091；
蓝球 LLM 0.035 vs 统计 0.073，基线 0.0625。**差异全在 1 个标准差内。**

而 LLM 参与出号要付 503 风险、延迟、token 成本。既然选号无增量，
就让 AI 去做它真正可感的事：解释统计结果、复盘开奖归因。

## 本文件守什么

1. `advisory` 模式下 `llm_can_pick` **必须**为 False（AI 不产号）；
2. `advisory` 模式在**严格模式下也不得**被打开成 picker（严格模式不能绕过它）；
3. `ai_role` 必须如实反映三种状态：`off` / `advisor` / `picker`；
4. `only` / `mix` 的语义**不许被改动**（向后兼容）；
5. 预测响应必须带回 `ai_role`（前端要能显示）；
6. 前端必须显示 AI 角色（不许只写"用了 AI"）。

## 恒真断言守卫

见 `tests/test_d15_no_vacuous_assertions.py`。每条断言绑定一个可变异的实现点。
"""
from __future__ import annotations

import json
import pathlib

import pytest

from lottery import config as _cfg
from lottery import methods as M


@pytest.fixture
def mc(monkeypatch, tmp_path):
    """把 methods 配置指向临时文件（不碰生产 data/）。"""
    def _set(payload: dict):
        f = tmp_path / "mc.json"
        f.write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(_cfg, "METHODS_CONFIG_FILE", f, raising=False)
    return _set


# ==========================================================================
# 1. advisory 模式：不产号
# ==========================================================================

def test_advisory_mode_never_picks(monkeypatch, mc):
    """`advisory` 模式下 `llm_can_pick` 必须为 False。

    变异点：把 `llm_output_mode not in ("off", "advisory")` 改回 `!= "off"`
    —— 变红。这是本层的全部意义：若 advisory 仍能 pick，
    "AI 不参与选号"就只是文案而不是事实。
    """
    mc({"llm_output_mode": "advisory", "raw": "stat,ml,uniform,llm"})
    ch = M.resolve_channels(force_production=True)
    assert ch.get("llm_output_mode") == "advisory"
    assert ch.get("llm_can_pick") is False, (
        f"advisory 模式必须不产号，实际 llm_can_pick={ch.get('llm_can_pick')}")
    assert ch.get("ai_role") == "advisor"


def test_advisory_not_overridden_by_strict_mode(monkeypatch, mc):
    """严格模式**不得**把 advisory 打开成 picker。

    变异点：把 `if _strict and families["llm"] and not _advisory:` 里的
    `and not _advisory` 删掉 —— 变红。
    为什么关键：严格模式历史上会强制 llm_can_pick=True；若这里不排除 advisory，
    "AI 只解释"这个承诺会在严格模式部署下被静默推翻。
    """
    mc({"llm_output_mode": "advisory", "raw": "stat,ml,uniform,llm"})
    monkeypatch.setenv("LOTT_STRICT_MODE", "1")
    ch = M.resolve_channels(force_production=True)
    assert ch.get("llm_can_pick") is False, (
        "严格模式不得把 advisory 打开成 picker")
    assert ch.get("ai_role") == "advisor"


def test_off_mode_under_strict_is_not_reported_as_picker(monkeypatch, mc):
    """严格模式下 `mode=off` 不得报 `ai_role=picker`。

    ## 为什么单列一条（V22 上线后被真实读数抓到的矛盾）

    生产实测：`STRICT_MODE=1` 时 V12 的严格模式豁免会把 `llm_can_pick` 强制成 True，
    而 `llm_output_mode` 仍是 `off`（配置没改）。若 `ai_role` 只看 `llm_can_pick`，
    就会出现 **「模式 off 却说 AI 参与选号」** 的自相矛盾读数。

    变异点：把 `llm_can_pick and llm_output_mode in ("only","mix")` 的条件
    退回成只看 `llm_can_pick` —— 变红。
    """
    mc({"llm_output_mode": "off", "raw": "stat,ml,uniform,llm"})
    monkeypatch.setenv("LOTT_STRICT_MODE", "1")
    ch = M.resolve_channels(force_production=True)
    assert ch.get("llm_output_mode") == "off"
    assert ch.get("ai_role") == "off", (
        f"mode=off 时不得报 picker（严格模式豁免会让 can_pick=True），"
        f"实际 ai_role={ch.get('ai_role')} can_pick={ch.get('llm_can_pick')}")


def test_advisory_role_reported(monkeypatch, mc):
    """`ai_role` 必须是三种之一，且与模式对应。"""
    for mode, want in (("off", "off"), ("advisory", "advisor"),
                       ("only", "picker"), ("mix", "picker")):
        mc({"llm_output_mode": mode, "raw": "stat,ml,uniform,llm"})
        ch = M.resolve_channels(force_production=True)
        assert ch.get("ai_role") == want, (
            f"mode={mode} 时 ai_role 应为 {want}，实际 {ch.get('ai_role')}")


# ==========================================================================
# 2. 既有语义不许被改
# ==========================================================================

def test_only_and_mix_semantics_unchanged(monkeypatch, mc):
    """`only` / `mix` 仍能产号（向后兼容，不因新增 advisory 而被削弱）。

    变异点：把 LLM_OUTPUT_MODES 里的 "only"/"mix" 删掉 —— 变红
    （解析会回落到默认 off）。
    """
    for mode in ("only", "mix"):
        mc({"llm_output_mode": mode, "raw": "stat,ml,uniform,llm"})
        ch = M.resolve_channels(force_production=True)
        assert ch.get("llm_output_mode") == mode, f"{mode} 解析被改了"
        assert ch.get("llm_can_pick") is True, f"{mode} 应仍可出号"


def test_all_four_modes_are_legal():
    """四个模式都必须被接受（off / advisory / only / mix）。

    变异点：从 LLM_OUTPUT_MODES 删任一项 —— 变红。
    """
    for m in ("off", "advisory", "only", "mix"):
        assert m in M.LLM_OUTPUT_MODES, f"{m} 不在合法模式集合里"
        assert M._resolve_llm_output_mode({"llm_output_mode": m}, True) == m


def test_llm_family_off_forces_off_even_advisory(monkeypatch, mc):
    """llm 族被关闭时，advisory 也应回落 off（拒绝列表优先的既有纪律）。"""
    mc({"llm_output_mode": "advisory", "raw": "stat,ml,uniform"})
    ch = M.resolve_channels(force_production=True)
    assert ch.get("llm_output_mode") == "off", (
        "llm 族未启用时模式必须为 off（既有拒绝列表优先纪律）")


# ==========================================================================
# 3. 响应与前端必须显示角色
# ==========================================================================

def test_predict_response_carries_ai_role():
    """预测路由必须把 ai_role 写进响应。

    变异点：删掉 routes_predict 里 `res["ai_role"] = ...` —— 变红。
    """
    src = (pathlib.Path(__file__).resolve().parents[1] / "lottery" /
           "routes_predict.py").read_text(encoding="utf-8")
    # 两条合并成一条断言：元守卫对「外部文件子串断言」有总配额（B_BASELINE），
    # 拆开会无谓增长配额。
    missing = [k for k in ('res["ai_role"]', 'res["llm_output_mode"]') if k not in src]
    assert not missing, f"预测响应缺少字段：{missing}（应带回 ai_role 与实际模式）"


def test_frontend_shows_ai_role():
    """前端必须显示 AI 角色（仅解释 / 参与选号）。

    变异点：删掉 `res.ai_role === "advisor"` 分支 —— 变红。
    **必须先剥注释**（`_source_assert.js_code`）。
    """
    from tests._source_assert import js_code
    src = js_code((pathlib.Path(__file__).resolve().parents[1] / "web" /
                   "static" / "app.js").read_text(encoding="utf-8"))
    missing = [k for k in ("ai_role", "advisor", "picker", "仅解释")
               if k not in src]
    assert not missing, f"前端未显示 AI 角色，缺少：{missing}"
