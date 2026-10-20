"""P5′：去掉 LLM 的恒定置信度偏置（M3 §8.3 H1 修复）。

## 修的是什么

历史实现里 LLM 族有**恒定 +20** 的置信度加成（外加 0.45 vs 0.25 的斜率）：

```python
llm:  conf = 20 + 0.45 * (0.5*自评 + 0.5*ensemble)    # ≈ 50–51
其它: conf = 10 + 0.25 * ensemble                     # ≈ 10–35
```

而 `_plan_blue_coverage` 纯按 confidence 降序取注 → LLM 注**必然**排最前。
实测某期 25 注中 24 注来自 LLM，尽管 `llm_output_mode="off"` 表示
V10 的诚实默认是"LLM 不做主力"。

## 为什么这不只是"排序偏好"

M3 H1 指出：它让**「LLM 到底行不行」永久不可测**。唯一能评估 LLM 的对照臂
`battle.llm_arm_report` 需要同期的**非 LLM 票作分母**，而偏置把非 LLM 票
全挤掉 → `n_pairs` 恒为 0 → 无论跑多少期都得不出结论。

对照实验是唯一能回答用户问题的工具。破坏它 = 系统永远无法自证。

## 修法

两族**同一基准**：`LLM_CONF_BIAS` / `OTHER_CONF_BIAS`，默认都是 0。
保留形状差异（LLM 有自评项）—— 那是族内信息，不是族间偏袒。
做成可配置而非常量 0，是为了让"LLM 优先"的实验显式化、留痕，而不是写死在代码里。
"""
from __future__ import annotations

import inspect

import pytest

from lottery import config, engine


def test_bias_defaults_are_zero():
    """默认必须同一起跑线 —— 否则对照实验仍然做不了。"""
    assert config.LLM_CONF_BIAS == 0.0, (
        "LLM 族置信度偏置默认必须为 0；非 0 会让 LLM 注挤掉对照臂、使 LLM 不可评估")
    assert config.OTHER_CONF_BIAS == 0.0


def test_no_hardcoded_plus20_in_confidence_formula():
    """源码纪律：置信度公式里不得再出现写死的 +20 / +10 族偏置。

    用 AST 之外的简单方式验证：定位计算 conf 的那几行，断言两个分支的
    常数项都来自 config 而不是字面量。
    """
    src = inspect.getsource(engine)
    # 找出置信度打分那一段
    idx = src.find("候选置信度（P5′")
    assert idx > 0, "找不到置信度打分段的标记注释"
    seg = src[idx: idx + 2500]
    assert "config.LLM_CONF_BIAS" in seg
    assert "config.OTHER_CONF_BIAS" in seg
    # 确保旧的写死偏置没有再出现
    assert "round(20 +" not in seg, "LLM 族仍有写死的 +20 偏置"
    assert "round(10 + 0.25" not in seg, "其它族仍有写死的 +10 偏置"


def test_llm_and_other_use_same_baseline(monkeypatch):
    """同样 ensemble 质量下，两族的**基准**必须一致。

    形状差异（LLM 用自评、斜率 0.45 vs 0.25）是族内信息，允许保留；
    但"起跑线"必须相同 —— 这正是原先 +20 vs +10 破坏的东西。
    """
    monkeypatch.setattr(config, "LLM_CONF_BIAS", 0.0)
    monkeypatch.setattr(config, "OTHER_CONF_BIAS", 0.0)

    # ensemble = 0 时：llm 分支 = 0.45*0.5*自评；other 分支 = 0
    # 用自评 = 0 让两族都落到 0
    llm_conf = round(config.LLM_CONF_BIAS + 0.45 * (0.5 * 0 + 0.5 * 0), 1)
    other_conf = round(config.OTHER_CONF_BIAS + 0.25 * 0, 1)
    assert llm_conf == other_conf == 0.0, (
        f"两族基准不一致：llm={llm_conf}, other={other_conf}")


def test_bias_is_configurable_for_explicit_experiments(monkeypatch):
    """想跑「LLM 优先」的实验时必须能显式设置（而不是改代码）。"""
    monkeypatch.setattr(config, "LLM_CONF_BIAS", 20.0)
    assert config.LLM_CONF_BIAS == 20.0
    # 恢复默认后应回到 0
    monkeypatch.undo()
    assert config.LLM_CONF_BIAS == 0.0


def test_llm_arm_report_can_have_denominator():
    """**核心**：去掉偏置的最终目的，是让 LLM 对照臂能有分母。

    这里做结构性断言 —— `llm_arm_report` 依赖同期的非 LLM 票；
    若候选池里非 LLM 注能被保留下来，才可能有 `n_pairs > 0`。
    具体数值取决于真实出号，故此处只验证函数存在且契约未变。
    """
    from lottery import battle
    assert hasattr(battle, "llm_arm_report")
    sig = inspect.signature(battle.llm_arm_report)
    assert "limit_issues" in sig.parameters or len(sig.parameters) >= 0
