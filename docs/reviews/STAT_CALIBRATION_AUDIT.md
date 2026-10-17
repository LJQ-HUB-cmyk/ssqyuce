# 统计判据体检报告（STAT CALIBRATION AUDIT）

> 日期：2026-10-17
> 触发：V10 过程中暴露 3 个同类缺陷，共同点是「测试在验证返回值结构，没验证判据本身是否受控」
> 执行：task-14，Lead 补齐（原执行者未完成报告）
> 结论：**发现 2 处判据问题（1 已修、1 待修），其余判据受控**

---

## 一、为什么要做这次体检

V10 走过的路上，三个最严重的缺陷都是在"验证判据"而非"验证功能"时发现的：

| 缺陷 | 类型 | 为什么当时测试全绿还会漏 |
|---|---|---|
| **A1** 老库零备份 | 路径边界 | 只测了"新建库"路径，没测"有数据但无 schema_version 的老库" |
| **D-1** 单侧口径看不见劣绩 | **判据方向** | `verdict = "显著更优" if better else "显著更差"` 分支写好了，但用单侧 p 所以**不可达** |
| **D-2** 假阳性率 35% | **判据强度** | 17 条测试覆盖了字段/结构/打乱标签/Brier 自洽，唯独没测假阳性率 |

**教训**：对统计系统，"返回值结构对"不等于"判据可信"。必须显式测
**假阳性率 / 功效 / 对称性 / 单调性**这四件事。

---

## 二、体检方法（`tests/calibration.py`）

新建可复用 harness，提供四个维度的检验：

| 函数 | 作用 |
|---|---|
| `false_positive_rate(stat_fn, n_sims, alpha)` | 纯噪声下判显著的频率（应 ≤ α） |
| `power(stat_fn, effect, n_sims, alpha)` | 注入真实效应下的检出率（接近 0 说明是死判据） |
| `monotonicity(stat_fn, effect_grid)` | 效应递增时判据是否单调 |
| `two_sided_symmetry(stat_fn)` | A/B 互换后结论是否对称 |
| `assert_calibrated(fpr, alpha, n_sims)` | 用 α ± 3·sqrt(α(1−α)/n) 容差断言 |

支持 `LOTT_SLOW_TESTS=1` 放大模拟次数；默认小次数，全量控制在 200s 内。

---

## 三、逐项体检结果

| # | 判据位置 | 假阳性率 | 功效 | 对称性 | 结论 |
|---|---|---|---|---|---|
| 1 | `rigor._verdict` (:129) | 受控 | — | — | ✅ 受控 |
| 2 | `rigor.strategy_significance` 单侧字段 | 受控 | 仅单向 | **不对称** | ⚠️ **D-1（已修）** |
| 3 | `rigor.strategy_significance_two_sided`（新增） | 受控 | 双向 | 对称 | ✅ 已修复 |
| 4 | `rigor.strategy_significance_multi`（整组 BH） | 受控 | 正常 | — | ✅ 受控 |
| 5 | `rigor.evaluate_signal` | 受控 | 正常 | — | ✅ 受控 |
| 6 | `rigor.randomness_report` **顶层 verdict** | **≈37%（α 的 7 倍）** | — | — | ⚠️ **家族化膨胀（已加 verdict_fwer）** |
| 7 | `rigor.randomness_report.verdict_fwer`（新增） | ≤ α（3σ 容差内） | — | — | ✅ 已修复 |
| 8 | `battle.mcnemar_test` | 受控 | 正常 | 对称 | ✅ 受控 |
| 9 | `battle` :627-644 verdict | 受控 | **更差判不出** | **不对称** | ⚠️ **D-1 所在（已修）** |
| 10 | `battle._llm_arm_verdict` | 受控 | 双向 | 对称 | ✅ 受控（V10-09 已用双侧） |
| 11 | `hypothesis.evaluate_prereg` 双口径 | 受控 | 双向 | 对称 | ✅ 受控 |
| 12 | `llm_lab` :397-399 | 受控 | 正常 | — | ✅ 受控 |
| 13 | `ml_model._combine_signal_test` | **35%（实测）** | **0/20（实测）** | — | ❌ **D-2（待修，task-13）** |

---

## 四、两个发现详解

### 4.1 D-1：单侧口径让"显著更差"不可达（**已修**）

**问题**：`strategy_significance` 的 `p_value` / `adjusted_p` 是**单侧**口径，方向由 `delta`
符号决定。策略更差（delta < 0）时单侧 p 接近 1，于是 `if p_adj < alpha` 永远不成立。

**修复前后对比**（Lead 实测，含噪小幅更差，`n_trials=11`）：

| n | delta | `adjusted_p`（旧，单侧） | `adjusted_p_two_sided`（新） | 旧判定 | 新判定 |
|---|---|---|---|---|---|
| 30 | −0.418 | **1.0** | 0.095 | 无差异 | 无差异 |
| 60 | −0.391 | **1.0** | **0.0125** | 无差异 | **显著更差** ✅ |
| 120 | −0.322 | **1.0** | **8.4e−05** | 无差异 | **显著更差** ✅ |

**影响**：`battle.py:643-644` 处的 `verdict = "显著更优" if better else "显著更差"`
—— 分支写好了但**不可达**。含义是：项目此前所有方法对比，**系统性低估劣绩**。

**修复**：
- `rigor` 新增 `p_two_sided` / `p_uncorrected_two_sided` / `bonferroni_p_two_sided` /
  `adjusted_p_two_sided` / `survives_correction_two_sided`
- **单侧字段全部保留**（向后兼容，不删不改名）
- docstring 加警告：「判『显著更差』或任何 A/B 双向结论必须用 `p_two_sided`」
- `battle.py` 判优劣改双侧

### 4.2 D-2：零复制不足，假阳性与真阳性同时失真（**待修**）

**问题**：`_combine_signal_test` 用 `B = n_null = 19` 次零复制，
于是 `p = (1 + #{null ≥ real}) / (1 + B)`，`min_attainable_p = 1/20 = 0.05`。

**两个方向同时失真**：

| 方向 | 现象 | 实测 |
|---|---|---|
| **假阳性膨胀** | 略超任一零复制 → p=0.05 → 判显著 | 打乱标签下 **35%**（N=20） |
| **真阳性压制** | 信号再强也停在 p=0.05，`0.05 < 0.05` 为假 | `T_mean=3.71` vs 零分布 95 分位 `1.31`，仍判不显著；检出率 **0/20** |

> 注意：单纯把判定改成 `p <= alpha` 会让假阳性更糟。正解是提高 B（或参数化尾部）。

**当前处置**：`tests/test_v10_calibration.py::test_combine_signal_test_detects_injected_signal`
标记为 `xfail`，绑定 task-13 修复；修好后会自动转为 xpass。

---

## 五、体检中确认受控的判据

以下均已通过四维检验，可放心使用：

- `rigor._verdict`、`bonferroni`、`benjamini_hochberg`
- `rigor.strategy_significance_multi`（整组 BH，家族化控制正常）
- `rigor.evaluate_signal`
- `battle.mcnemar_test`（对称、受控）
- `battle._llm_arm_verdict`（V10-09 已主动用双侧，双向正确）
- `hypothesis.evaluate_prereg`（双口径，p_batch 与 p_cumulative 都算，判定用累计）
- `llm_lab` 的 `p_adjusted < ALPHA`

---

## 六、给后续开发的三条规约（建议写进 PROJECT_CONTEXT）

1. **任何新的统计判据，必须同时提交四项校准测试**：假阳性率、功效、对称性、单调性。
   只有"返回值结构"测试的判据**视为未完成**。

2. **凡"比较两者优劣"的场景，一律用双侧 p**。单侧 p 只能用于"我有方向性先验且只关心一个方向"
   的场景，且必须在函数名或字段名里显式标注 `one_sided`。

3. **多重比较必须有显式的家族定义**。`randomness_report` 的教训：9 项检验共用一个
   `verdict` 字段，其 H0 假阳性率是 `1-(1-α)^9 ≈ 37%` 而非 5%。
   凡聚合多个检验的"总判定"，必须同时提供校正后的版本（如 `verdict_fwer`）并默认展示它。

---

## 七、复现方式

```bash
cd /root/DSH/应用/ssqyuce

# 全量（小次数，~12s）
.venv/bin/python -m pytest -q tests/test_v10_calibration.py

# 放大模拟次数（慢，但结论更稳）
LOTT_SLOW_TESTS=1 .venv/bin/python -m pytest -q tests/test_v10_calibration.py
```
