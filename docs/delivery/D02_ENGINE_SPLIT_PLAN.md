# D-02 `engine.py` 拆分方案

> 作者：高架构（架构师）｜建档：2026-10-21｜代码基准 git HEAD `0f5656d`
> 数据：`data/ssq.db` 3517 期（2003001–2026120）
> 状态：**方案待签收，未动生产代码**
> 复现脚本：`docs/delivery/repro/d02_fanout.py`、`docs/delivery/repro/d02_equivalence.py`

---

## 0. 先说结论（含一条反对意见，请先看这条）

### 0.1 我的判断：**拆分值得做，但收益不是「好维护」，而是「改动能被单测锁住」**

拆分的真实收益只有一条，其余都是副产品：

> `engine.py` 现在是一个**无法被独立测试**的单元。`_expand_dan_tuo` 一个纯组合逻辑函数，
> 要测它必须 `import lottery.engine`，连带拉起 `db`/`config`/`llm_client`/`ml_model`。
> 于是测试者只能退而测 `predict_next` 端到端 —— 而端到端测试**分不清是哪一层坏了**。
> 这是 D-08（判定层缺陷 ①②③④）之所以拖到今天的结构原因，不是人们不想修。

除此之外，「文件太长不好读」的理由我**不认**：3107 行对 python 来说不算长，
`hypothesis.py` 1978 行没有任何人抱怨。**如果只是为了好看，我反对拆。**

### 0.2 但我同时要指出：**看板里的 8 个子系统切法是错的，且不是小错**

看板 §2.2 的 8 个区间合计 2880 行，**有 227 行完全没被划进任何子系统**，
其中散落着 **11 个顶层定义**（含 `TICKET_COST` / `PRIZE_CASH_FIXED` / `JACKPOT_ASSUMPTION`
三个被 `_single_ticket_ev` 直接消费的奖金常量）。按那 8 刀切下去，
第一批就会在 `NameError: TICKET_COST` 上炸。详见 §1。

### 0.3 还有一件事必须摆在最前面（§7 的前置结论）

**「拆分前后 `predict_next` 逐字节相同」这条验收标准，在当前代码上是不可执行的** ——
`predict_next` 每次返回的 `generated_at` 都不同，同一份代码连跑两遍 6/6 用例全不一致。
我实测并修好了这个判定方法（剔除时间性字段），但**这个坑如果不在方案阶段写死，
下一轮拆分会被判成「拆分改坏了行为」而白干一天**。见 §5.2。

---

## 1. 问题一：切法（哪几刀切错了）

### 1.1 先给结论：**8 刀 → 10 刀**

源码里作者自己用注释标了 **10 个**分节，看板把它们合并压成了 8 个，合并过程中漏掉了边界。

**实测命令**：

```bash
grep -n '^# ---\|^# ===' lottery/engine.py
```

**输出（当前 3107 行，注意不是 3044）**：

```
32:# ---------- 硬约束 ----------
101:# ---------- 统计采样 ----------
146:# ---------- V4-B：融合权重（walk-forward 样本外 + softmax + 上限 + 收缩） ----------
528:# ---------- 主流程 ----------
592:# ---------- V7：规律 → 出号（运行时快照驱动） ----------
1081:# ---------- U2 蓝球模式与覆盖 ----------
1353:# ---------- V4-C：奖金期望模型（覆盖优化的目标函数） ----------
1484:# ===========================================================================
1486:# ===========================================================================
1492:# ---------- 数学（全部闭式；测试用 C(33,6)=1,107,568 全枚举交叉验证） ----------
1504:# ---------- 三个必须记住的边界 ----------
2004:# ---------- U4 组合覆盖优化（V10-03：可插拔目标 + 蓝球职责边界） ----------
2734:# ---------- V12-11 预算内最优组合求解器（L2-e） ----------
```

### 1.2 正确的 10 域（实测，行数合计恰为 3107）

| # | 域名 | 行段 | 行数 | 符号数 | 首符号 |
|---|---|---|---|---|---|
| — | HEADER（docstring / import / `R_MAX,B_MAX` / `InsufficientDataError`） | 1–31 | 31 | 1 | `InsufficientDataError` |
| S1 | 硬约束 | 32–100 | 69 | 3 | `_last_sums` |
| S2 | 统计采样 | 101–145 | 45 | 3 | `_weighted_sample` |
| S3 | V4-B 融合权重 | 146–527 | 382 | 19 | `sample_stat_ticket` |
| S4 | 主流程 | 528–591 | 64 | 3 | `build_context` |
| S5 | V7 规律→出号 | 592–1080 | 489 | 11 | `_rules_hit` |
| S6 | U2 蓝球模式与覆盖 | 1081–1352 | 272 | 7 | `_min_significant_blue_sample` |
| S7 | V4-C 奖金期望 | 1353–1483 | 131 | 6 | `expected_prize` |
| S8 | V10-03 票单分布（`# ===` 内含「数学」+「三个边界」，共用常量，**不拆**） | 1484–2003 | 520 | 31 | `_popcount_lut` |
| S9 | U4 组合覆盖优化 | 2004–2733 | 730 | 12 | `_normalize_coverage_objective` |
| S10 | V12-11 约束内最优组合求解器 | 2734–3107 | 374 | 10 | `_balanced_min_sum_a` |
| | **合计** | | **3107** | **106** | |

> **注意 S6 的 272 行**：`1081–1352` 与任务书「蓝球 272 行」完全吻合，说明任务书的原始
> 分节数据是对的，看板 §2.2 的 `1083–1335` 才是后来漂移写错的。

### 1.3 看板 8 刀的具体错误（逐条）

| 看板区间 | 实际界符 | 判定 |
|---|---|---|
| 硬约束 `18–116` | 18 是 `class`，非分隔行；116 是空行 | 起点吃到 HEADER，终点吃掉 S2 前 30 行的一半 |
| Brier `117–529` | 117 是 `def sample_stat_ticket` | **不应从 117 起**：S2（101–145）被拦腰切断，`_weighted_sample` 留在「硬约束」里、`sample_stat_ticket` 进了「Brier」 |
| LLM `530–1033` | 530 是 `def build_context` | **把 S4 主流程（528–591）整段吞进 LLM 域** |
| 蓝球 `1083–1335` | 1083 是 `def _min_significant_blue_sample` | 起止各少 2 行；`_pick_legacy_blue`（1336）被甩在外面 |
| 注数EV `1373–1929` | — | `TICKET_COST`/`PRIZE_CASH_FIXED`/`JACKPOT_ASSUMPTION`（1364–1372）被甩在外面 |
| 覆盖优化 `2054–2363` | — | 丢掉 `COVERAGE_OBJECTIVES`/`DEFAULT_CUSTOM_WEIGHTS`/`_PMAX_SEARCH_DRAWS`（2039–2053） |
| 主预测编排 `2364–2753` | 2364 是 `def channels_snapshot` | `PORTFOLIO_OBJECTIVES`/`PORTFOLIO_P_MAX_AT_LEAST_R`（2750–2751）被划进「主预测编排」，实际属于 S10 |
| 组合优化 `2754–3107` | — | 正确 |

**未被 8 刀覆盖的 227 行，逐段全列**：

```
 1–17      17 行   HEADER（docstring / import / R_MAX,B_MAX）
 1034–1082 49 行   _latest_audit_digest, _ml_result_block         ← LLM 通道的尾巴
 1336–1372 37 行   _pick_legacy_blue, TICKET_COST,
                    PRIZE_CASH_FIXED, JACKPOT_ASSUMPTION           ← 蓝球尾巴 + EV 头部常量
 1930–2053 124 行  _expand_blue_compound, _expand_dan_tuo,
                    COVERAGE_OBJECTIVES, DEFAULT_CUSTOM_WEIGHTS,
                    _PMAX_SEARCH_DRAWS                             ← S8 尾巴 + S9 头部常量
```

**其中 11 个顶层定义**（不是注释，是会 `NameError` 的真代码）：

```
_latest_audit_digest(1034)  _ml_result_block(1062)  _pick_legacy_blue(1336)
TICKET_COST(1364)  PRIZE_CASH_FIXED(1365)  JACKPOT_ASSUMPTION(1366)
_expand_blue_compound(1930)  _expand_dan_tuo(1952)
COVERAGE_OBJECTIVES(2039)  DEFAULT_CUSTOM_WEIGHTS(2042)  _PMAX_SEARCH_DRAWS(2051)
```

> **`TICKET_COST` 是最致命的那个**：`_single_ticket_ev`（1764）在 S8 里直接读它，
> 而 `_single_ticket_ev` 又被 `routes_portfolio.py:250` 用 `engine._single_ticket_ev`
> 调用。按 8 刀切完，这个常量会无处安放。

### 1.4 每刀的理由：**用依赖方向，不用「看起来相关」**

我用 AST 跨域引用分析得出下面这张图（脚本见附录 A，可复跑）：

**出向依赖（本节调用了哪些节）**

| 域 | 依赖 |
|---|---|
| S1 硬约束 | → HEADER(`InsufficientDataError`) |
| S2 统计采样 | → S1 |
| S3 融合权重 | **→ 无（纯自足）** |
| S4 主流程 | → S1 |
| S5 规律出号 | → S2, S4 |
| S6 蓝球 | **→ 无（纯自足）** |
| S7 奖金期望 | **→ 无（纯自足）** |
| S8 票单分布 | → S1, S2, S7 |
| S9 覆盖优化 | → S1, S2, S3, S5, S6, S7, S8 |
| S10 求解器 | → S1, S8 |

**入向依赖（谁调用本节）**

| 域 | 被谁依赖 | 入度 |
|---|---|---|
| S1 | S2, S4, S8, S9, S10 | 5 |
| S2 | S5, S8, S9 | 3 |
| S3 | S9 | 1 |
| S4 | S5 | 1 |
| S5 | S9 | 1 |
| S6 | S9 | 1 |
| S7 | S8, S9 | 2 |
| S8 | S9, S10 | 2 |
| S9 | **无** | **0** |
| S10 | **无** | **0** |

**这张图证明了三件事**：

1. **图是严格 DAG，零环** —— 与任务书「`lottery/` 70 模块零循环依赖」一致，
   `engine` 内部同样零环。拆分不会造出新的环。
2. **S3 / S6 / S7 是纯自足叶子** —— 出向依赖为 0。这三域**可以先切、独立切、互不干扰**，
   是风险最低的头三刀。
3. **S9 是出度最大、入度为零的「汇」** —— 它依赖 8 个域中的 7 个，却没有任何域反过来依赖它。
   这是典型的「编排层」特征：**它必须最后切**，而且在拆分方案里它是唯一需要
   `E.xxx()` 大量穿透调用的地方（16 个跨域调用，见 §1.5）。

### 1.5 你的点名问题：`1441 portfolio_expected_value` 与 `1764 _single_ticket_ev` 怎么处理

**先纠正一个前提**：这两个函数**不在不同子系统里**。

| 函数 | 行段 | 归属 |
|---|---|---|
| `expected_prize` | 1373–1431 | **S7** 奖金期望 |
| `_portfolio_blue_hit_prob` | 1434–1439 | **S7** |
| `portfolio_expected_value` | 1441–1481 | **S7** |
| `_single_ticket_ev` | 1764–1814 | **S8** 票单分布 |

`portfolio_expected_value` 在 **1441**，而 S8 从 **1484** 才开始 —— 它在 S7 里。你的疑问来自
8 刀切法把 1373–1929 整段塞成「注数分布与 EV」，把两个域压成了一坨。

**跨域调用的实际拓扑**（AST 实测）：

```
portfolio_expected_value (S7)  →  S7 内部：expected_prize, _portfolio_blue_hit_prob
                                  跨域：无
_single_ticket_ev        (S8)  →  跨域：TICKET_COST(S7), PRIZE_CASH_FIXED(S7)
```

**处理方式：不特判、不机翻 import，统一走 `E.xxx()` 运行期查属性。**

```python
# _s8_distribution.py
from . import engine as E          # 或用下面的 sys.modules 形式，见 §7

def _single_ticket_ev(jackpot=None, *, sixth="published"):
    ...
    cost = E.TICKET_COST                       # 而不是 from ._s7_prize import TICKET_COST
    fixed = E.PRIZE_CASH_FIXED
```

**为什么必须是 `E.xxx()` 而不是 `from ._s7_prize import TICKET_COST`**：

1. **打桩穿透**。`tests/test_v12_shape_lock.py:260` 等处
   `monkeypatch.setattr(engine, "constraint_ctx", _shrunk)` 打在 `lottery.engine`
   这个模块对象上。若 S9 用 `from ._s1_constraints import constraint_ctx` 早期绑定，
   打桩就**打不到** S9 内部那次调用，测试会「单跑绿、全量红」，依赖测试顺序 ——
   这正是 `rigor.py` 踩过的坑。
2. **避免 S7→S8 与 S8→S7 的潜在环**。S8 依赖 S7（常量），S7 的
   `portfolio_expected_value` 今天不依赖 S8，但**将来可能会**（它已有
   `sixth` 档位口径，与 `_single_ticket_ev` 是同族）。运行期查属性天然免疫这种演化。
3. **零语义改动**。`E.TICKET_COST` 与裸 `TICKET_COST` 求值结果完全相同。

**16 个跨域调用点全清单**（`predict_next` 一个就占了 16 项，它是 S9 的编排主体）：

```
predict_next (S9) → _apply_blue_mode(S6)          _brier_blend(S3)
                    _expand_blue_compound(S8)     _expand_dan_tuo(S8)
                    _latest_audit_digest(S5)      _ml_result_block(S5)
                    _pick_legacy_blue(S6)         _plan_blue_coverage(S6)
                    _portfolio_blue_hit_prob(S7)  apply_rules_to_candidates(S5)
                    constraint_ctx(S1)            ensemble_mass(S2)
                    llm_tickets(S5)               portfolio_expected_value(S7)
                    sample_stat_ticket(S3)        ticket_distribution(S8)
coverage_optimize          (S9)  → blue_coverage_plan(S6)
optimal_portfolio         (S10)  → _validate_tickets(S8) _var_ratio(S8)
                                   pass_constraints(S1)  ticket_distribution(S8)
```

> **`predict_next` 的 16 个跨域调用是本次拆分最大的单点风险**。它意味着
> `_s9_orchestrator.py` 与其余 7 个子模块全部强耦合。这是 S9 必须最后切的原因，
> 也是 §6 风险榜第一名。

---

## 2. 问题二：fanout 差异认领

### 2.1 我的计数口径（脚本落地，可复跑）

**脚本**：`docs/delivery/repro/d02_fanout.py`（已提交，非 `/tmp`，符合看板红线 3）

```bash
cd /root/DSH/应用/ssqyuce && .venv/bin/python docs/delivery/repro/d02_fanout.py
```

**实际输出**：

```
口径 A  仅顶层相对导入 (看板 §2.2 用的口径)
        8 个: ['backtest', 'config', 'db', 'features', 'llm_client',
               'methods', 'ml_model', 'models']

口径 B  全部相对导入 = 顶层 + 函数内延迟导入 (本报告采用)
        13 个: ['ai_audit', 'backtest', 'config', 'db', 'discovery', 'evaluate',
                'features', 'llm_client', 'methods', 'ml_model', 'models',
                'patterns_runtime', 'shape_rules']
        顶部之外的延迟导入:
          line    60  shape_rules
          line   559  patterns_runtime
          line   578  evaluate
          line   597  discovery
          line   657  patterns_runtime
          line  1045  ai_audit
          line  2528  patterns_runtime

口径 C  含标准库与第三方 (最宽口径)
        24 个: 项目内 13 + 标准库 8 + 第三方 3 ['concurrent', 'fractions', 'numpy']

口径 D  按 import 语句数 (不看去重)
        26 条 import 语句
```

### 2.2 差异来源：**不是旧数据，是统计边界之争**

| 口径 | 数值 | 谁在用 |
|---|---|---|
| A：顶层相对导入 | **8** | 看板 §2.2、齐交付 |
| B：全部相对导入 | **13** | `V14_TEAM_VERDICT`、我 |
| C：含外部依赖 | 24 | 无人使用，列出仅供对照 |

**差在 5 个模块：`shape_rules`、`patterns_runtime`、`evaluate`、`discovery`、`ai_audit`。**

它们全部是**函数体内的延迟导入**（7 处 import 语句，5 个去重模块）：

```python
lottery/engine.py:60    from . import shape_rules as _SR      # 在 constraint_ctx 里
lottery/engine.py:559   from . import patterns_runtime        # 在 build_context 里
lottery/engine.py:578   from . import evaluate as EV          # 在 _last_feedback 里
lottery/engine.py:597   from . import discovery               # 在 _rules_hit 里
lottery/engine.py:657   from . import patterns_runtime        # 在 apply_rules_to_candidates 里
lottery/engine.py:1045  from . import ai_audit                # 在 _latest_audit_digest 里
lottery/engine.py:2528  from . import patterns_runtime        # 在 predict_next 里
```

**为什么它们要延迟导入**：因为 `patterns_runtime`、`evaluate`、`discovery`、`ai_audit`
这几个模块**本身就 import engine**（`evaluate.py:8` 是 `from . import db, engine as E`）。
放到顶层会立刻成环。`shape_rules` 同理（`shape_rules.py:27` 引用 `engine.pass_constraints`）。

### 2.3 我的认领：**fanout = 13（口径 B），且必须按 13 规划**

不认领「以看板为准」，理由：

> 口径 A 统计的是「顶层 import 行」，得到的是**模块加载期**依赖。但 fanout 的工程含义是
> **「我改了 engine，哪些模块可能得跟着改」**——那必须包含运行期才会拉起的依赖。
> `patterns_runtime` 被 `predict_next` 在 2528 行延迟 import，它在运行期**确实是 engine 的下游**。
> 说 fanout=8 会漏掉 5 个模块，其中包括**整个规律运行时快照层**和**离线评估层**。

**这条差异对拆分方案有实质影响**，不是学术争论：那 5 个延迟导入**必须原样保留在函数体内**
（提到顶层会让 `engine` 与 `evaluate`／`patterns_runtime` 立刻成环，`import lottery.engine`
直接失败）。拆分时如果谁「顺手把延迟导入提到文件头整理一下」，`engine` 的导入会当场崩。

**→ 这必须写进拆分的禁止项：延迟导入位置一字不许动。**

### 2.4 关于 fanin：12 还是 13

| 口径 | 数值 | 构成 |
|---|---|---|
| 排除入口 `cli.py` | **12** | api_app, battle, blue_coverage, evaluate, evolution, l2_service, llm_eval, replay, routes_llm, routes_portfolio, routes_predict, routes_tasks |
| 含 `cli.py` | **13** | 上列 + cli |

我的意见：**两个都对，但我建议一律报 13**。理由：`cli.py:53` 是 `engine.predict_next(...)`
的真实调用点，拆分后它同样要受「导入是否仍然可用」的约束。把它排除在 fanin 之外，
会让「拆分后 12 个 fanin 模块零改动」这条验收标准漏检一个真实调用方。

**我没有把 fanin 算成 13 当成「更正」看板** —— 看板的 12 是有意排除入口，我在报告里
声明口径即可（看板 §2.2 已要求「报告里必须声明用的是哪一个」）。**fanout 才是实质分歧。**

---

## 3. 问题三：第二真源防范（薄壳骨架）

### 3.1 先确立原则：**`is` 恒等，不是「值相等」**

`tests/test_v14_stats_package.py:47` 已经把这条写死为断言：

```python
assert getattr(shell, name) is getattr(real, name), (
    f"lottery/{mod}.py.{name} 与 {src_mod}.{name} 不是同一对象 —— "
    f"薄壳里可能被重新实现了一份")
```

`engine` 有 12~13 个 fanin，比 `stats` 薄壳严苛得多。所以：

> **`lottery.engine.X is lottery.engine.<module>.X` 必须恒为 True，且必须逐名断言，
> 不能抽样。**

### 3.2 架构选型：**包 + 代理模块**（不是「薄壳 + 平铺文件」）

两种可行形态，我选前者：

| 方案 | 结构 | `is` 恒等 | 拆分颗粒 | 判定 |
|---|---|---|---|---|
| **① 包** | `lottery/engine/__init__.py` + `_s1.._s10.py` | 由 `_ProxyModule` 的 `__getattr__` 保证 | 每域一文件 | **采用** |
| ② 平铺 + 薄壳 | `lottery/engine.py`（薄壳）+ `lottery/engine_*.py` | 同上 | 同上 | 可行但污染包根目录 |

**选 ① 的关键理由**：`stats/` 已经用目录把「数学 vs 业务」的边界做成了**可见的**结构。
`engine/` 用同样的形态，`lottery/` 根目录不会多出 10 个 `engine_*` 文件。
代价是 §7 的导入路径问题 —— 我在方案阶段就把它解决掉。

### 3.3 `__init__.py` 骨架（**这是 §7 事故的修正版**）

```python
"""集成预测引擎：统计模型 + LLM 通道 → 硬约束过滤 → 置信度评分 → Top-N 输出。

## V14-A（2026-10-21）拆分说明

本文件由原 `lottery/engine.py`（3107 行）拆分而来，现为**组合根 + 代理模块**：

| 子模块 | 域 | 行数 |
|---|---|---|
| `_s1_constraints.py`   | 硬约束       | 69  |
| `_s2_sampling.py`      | 统计采样     | 45  |
| `_s3_blend.py`         | V4-B 融合权重 | 382 |
| `_s4_context.py`       | 主流程上下文 | 64  |
| `_s5_rules.py`         | V7 规律→出号 | 489 |
| `_s6_blue.py`          | U2 蓝球      | 272 |
| `_s7_prize.py`         | V4-C 奖金期望 | 131 |
| `_s8_distribution.py`  | V10-03 票单分布 | 520 |
| `_s9_orchestrator.py`  | U4 覆盖优化 + predict_next | 730 |
| `_s10_solver.py`       | V12-11 求解器 | 374 |

**对外契约不变**：`from lottery import engine` + `engine.xxx` 全部照旧。
拆分子模块内部互相调用一律 `E.xxx()`（运行期查属性），保证 monkeypatch 打在
`lottery.engine` 上能穿透到每一层。
"""
from __future__ import annotations

import sys as _sys
import types as _types

# ── 依赖方向由 import 顺序显式表达（沿用原文件的行号顺序）──────────────
# 注意：从包内取上级模块必须用 `from ..`，不是 `from .`（见 §7）
from .. import backtest as BT                                    # noqa: F401
from .. import (config, db, features as F, llm_client,            # noqa: F401
                methods as METH, ml_model, models as M)
from ..llm_client import LLMChannelError                          # noqa: F401

R_MAX, B_MAX = 33, 16


class InsufficientDataError(ValueError):
    """历史数据为空 → 无法估计硬约束上下文。...（原文一字不动）"""


# ── 各域挂载（顺序 = 依赖拓扑序，DAG 见方案 §1.4）──────────────────
from . import _s1_constraints                                    # noqa: E402
from . import _s2_sampling                                       # noqa: E402
from . import _s3_blend                                          # noqa: E402
from . import _s4_context                                        # noqa: E402
from . import _s5_rules                                          # noqa: E402
from . import _s6_blue                                           # noqa: E402
from . import _s7_prize                                          # noqa: E402
from . import _s8_distribution                                   # noqa: E402
from . import _s10_solver        # S10 先于 S9：S9 依赖它之外无环   # noqa: E402
from . import _s9_orchestrator                                   # noqa: E402

_SUBMODULES = (_s1_constraints, _s2_sampling, _s3_blend, _s4_context,
               _s5_rules, _s6_blue, _s7_prize, _s8_distribution,
               _s9_orchestrator, _s10_solver)

_OWN = {"__name__", "__doc__", "__file__", "__package__", "__loader__",
        "__spec__", "__builtins__", "__all__", "_SUBMODULES", "_OWN",
        "_ProxyModule", "_sys", "_types"}


class _ProxyModule(_types.ModuleType):
    """把本包未定义的属性读写转发给子模块的代理。

    **为什么不用 `from ._sX import *`**：
    `import *` 不导出下划线开头的名字（Python 语言规范），而本包有
    **27 个私有符号被外部使用**（见方案 §3.5 全清单）。用 `import *`
    回填会让 `engine._single_ticket_ev`、`engine._rebuild_probs` 等全部消失 ——
    这正是 `rigor` 薄壳踩过、造成 16 例 AttributeError 的同一个坑。

    **为什么不用「镜像复制」**：
    复制会产生两份状态。`monkeypatch.setattr(engine, "X", fake)` 只改一份，
    被测代码从另一入口取 `X` 就不一致 —— 单跑绿、全量红，依赖测试顺序，极难排查
    （V14 §10.2 坑 2）。
    """

    def __getattr__(self, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        for mod in _SUBMODULES:
            try:
                return getattr(mod, name)
            except AttributeError:
                continue
        raise AttributeError(
            f"module 'lottery.engine' has no attribute {name!r}")

    def __setattr__(self, name, value):
        if name in _OWN:
            super().__setattr__(name, value)
            return
        # 写双边：先改子模块里的定义处，再改本命名空间 → 打桩两处同时生效
        for mod in _SUBMODULES:
            if hasattr(mod, name):
                setattr(mod, name, value)
        super().__setattr__(name, value)

    def __delattr__(self, name):
        if name in _OWN:
            super().__delattr__(name)
            return
        for mod in _SUBMODULES:
            if hasattr(mod, name):
                delattr(mod, name)
        if name in self.__dict__:
            super().__delattr__(name)


_sys.modules[__name__].__class__ = _ProxyModule

# 组合根只做「把名字搬到自己命名空间」，不做任何计算
for _m in _SUBMODULES:
    for _n in dir(_m):
        if not _n.startswith("__"):
            globals()[_n] = getattr(_m, _n)
del _m, _n

__all__ = [n for n in dir(_sys.modules[__name__]) if not n.startswith("_")]
```

### 3.4 子模块骨架（含 §7 的修正）

```python
# lottery/engine/_s7_prize.py
"""V4-C 奖金期望模型（覆盖优化的目标函数）。原文函数体逐字迁移，未改一行。"""
from __future__ import annotations

import sys as _sys
from typing import Dict, List, Optional

import numpy as np

# 上级包（lottery）依赖：从包里往外取，必须 `from ..`
from .. import config                                             # noqa: F401

# 本包（lottery.engine）自身：**不要** `from . import engine`
#   —— `__init__` 执行到一半时包对象尚未完成初始化，会拿到半成品。
#   `sys.modules[__package__]` 拿的是同一个模块对象，且属性访问发生在调用时。
E = _sys.modules[__package__]


def expected_prize(reds: List[int], blue: int, ...):
    ...   # 原 1373–1431 行，逐字
```

**为什么用 `sys.modules[__package__]` 而不是 `from . import engine as E`**：

`from . import engine` 在 `lottery/engine/_s7_prize.py` 里的语义是
「从父包 `lottery.engine` 里取属性 `engine`」—— **父包里没有叫 `engine` 的属性**，
这句要么 `ImportError`，要么（取决于 Python 版本与导入顺序）拿到半初始化的模块。
`sys.modules[__package__]` 无条件取到包模块对象本身，且**不触发新的导入**。

**替代写法（同样正确，更直白）**：在函数体内做 `from .. import engine as E`，
调用时才解析。两者我都验证过；推荐前者，因为只需在文件头写一次。

### 3.5 `is` 恒等的守卫测试（新增，仿 `test_v14_stats_package.py`）

```python
# tests/test_v14_engine_split.py
"""D-02 拆分守卫：薄壳与子模块必须是同一对象，不得有第二真源。"""
import ast
from pathlib import Path

import pytest

LOTTERY = Path(__file__).resolve().parents[1] / "lottery"
SUBS = ["_s1_constraints", "_s2_sampling", "_s3_blend", "_s4_context",
        "_s5_rules", "_s6_blue", "_s7_prize", "_s8_distribution",
        "_s9_orchestrator", "_s10_solver"]


def test_thin_shell_reexports_same_objects():
    """`lottery.engine.X is lottery.engine.<sub>.X` 逐名恒等。"""
    from lottery import engine

    checked = 0
    for sub in SUBS:
        real = __import__(f"lottery.engine.{sub}", fromlist=["x"])
        for name in dir(real):
            if name.startswith("__"):
                continue
            assert hasattr(engine, name), (
                f"lottery.engine 缺少 {name!r}（本应由 {sub} 提供）—— "
                f"调用方会 AttributeError")
            assert getattr(engine, name) is getattr(real, name), (
                f"lottery.engine.{name} 与 {sub}.{name} 不是同一对象 —— "
                f"出现了第二真源")
            checked += 1
    assert checked > 90, f"仅校验了 {checked} 个符号，守卫可能失效"


def test_private_symbols_survive():
    """27 个被外部使用的私有符号必须全部可用（import* 方案的回归防护）。"""
    from lottery import engine

    required = [
        "_single_ticket_ev", "_rebuild_probs", "_sanitize_patterns_used",
        "_parse_tickets_response", "_plan_blue_coverage", "_pick_blue_coverage",
        "_portfolio_blue_hit_prob", "_normalize_coverage_objective",
        "_exact_red_tail", "_variance_stats", "_ticket_objective",
        "_softmax_weights", "_cap_weights", "_walk_forward_brier",
        "_objective_meta", "_custom_objective_weights",
        "_prob_at_least_r_single", "_min_significant_blue_sample",
        "_expand_blue_compound", "_expand_dan_tuo", "_pick_legacy_blue",
        "_rules_hit", "_regime_rules_for_prompt",
    ]
    missing = [n for n in required if not hasattr(engine, n)]
    assert not missing, f"私有符号丢失 {missing} —— 正是 import* 的缺陷"


def test_submodule_size_budget():
    """每个子模块 ≤ 800 行（看板 §2.2 验收 1）。"""
    for f in (LOTTERY / "engine").glob("_s*.py"):
        n = len(f.read_text(encoding="utf-8").splitlines())
        assert n <= 800, f"{f.name} 有 {n} 行，超过 800 行预算"


def test_submodules_do_not_use_relative_engine_import():
    """子模块不得写 `from . import engine`（半初始化陷阱 + 导入路径陷阱）。"""
    for f in (LOTTERY / "engine").glob("_s*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 1:
                names = [a.name for a in node.names]
                assert "engine" not in names, (
                    f"{f.name}:{node.lineno} 写了 `from . import engine` —— "
                    f"在包内 `.` 是 lottery.engine 自身，会拿到半初始化模块；"
                    f"应用 sys.modules[__package__] 或 `from .. import engine`")


def test_no_import_star_in_shell():
    """组合根不得使用 `import *` 回填（会丢私有符号）。"""
    src = (LOTTERY / "engine" / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert all(a.name != "*" for a in node.names), (
                "__init__.py 出现了 `import *` —— 私有符号会全部丢失")
```

**`is` 断言的可执行示例**（给你的命令能直接跑）：

```bash
cd /root/DSH/应用/ssqyuce && .venv/bin/python -c "
from lottery import engine
from lottery.engine import _s7_prize, _s8_distribution
print('expected_prize        ', engine.expected_prize is _s7_prize.expected_prize)
print('_single_ticket_ev     ', engine._single_ticket_ev is _s8_distribution._single_ticket_ev)
print('TICKET_COST           ', engine.TICKET_COST is _s7_prize.TICKET_COST)
print('_rebuild_probs        ', engine._rebuild_probs is __import__('lottery.engine._s3_blend', fromlist=['x'])._rebuild_probs)
assert engine.expected_prize is _s7_prize.expected_prize
print('OK: 无第二真源')
"
```

### 3.6 打桩穿透的自检（这条最容易漏）

```bash
# 打桩必须穿透到子模块内部调用
cd /root/DSH/应用/ssqyuce && .venv/bin/python -c "
import random
from unittest import mock
from lottery import engine, db
draws = db.load_draws()[:600]
calls = []
orig = engine.ticket_distribution
def spy(*a, **k):
    calls.append(1)
    return orig(*a, **k)
with mock.patch.object(engine, 'ticket_distribution', spy):
    engine.predict_next(draws, use_llm=False, persist=False, n_tickets=5,
                        rng=random.Random(1))
print('predict_next 内部是否用到被打桩的实现:', bool(calls))
assert calls, '打桩未穿透 —— 子模块用了 from-import 早期绑定'
print('OK: 打桩穿透正常')
"
```

> `predict_next` 与 `ticket_distribution` 分属 S9/S8，这个自检直接验证
> 「`E.xxx()` 运行期查属性」是否真的生效。**必须纳入每批提交的验收闸门**。

---

## 4. 问题四：不触碰清单

| 禁止项 | 具体文件 / 约束 | 守卫方式 |
|---|---|---|
| `lottery/stats/` | 整包不得改一行。已被 `test_v14_stats_package.py` 锁死（含「薄壳不得定义函数」等 6 条断言） | 跑该测试文件；`git diff --stat -- lottery/stats/` 必须为空 |
| `lottery/eprocess.py`、`lottery/rigor.py` | 两个模块级代理薄壳，读写双写。**若碰到 `import eprocess` 保持原样**，不改成 `from .stats import` | `git diff` 为空 |
| `predict_next` 对外签名 | 14 个参数（`draws`/`use_llm`/`n_tickets`/`persist`/`use_ml`/`rng`/`llm_samples`/`llm_verify`/`llm_required`/`llm_deadline`/`bet_mode`/`coverage`/`coverage_r`）与默认值一字不许动 | `inspect.signature` 前后比对，写进守卫测试 |
| 12/13 个 fanin 模块 | `api_app, battle, blue_coverage, cli, evaluate, evolution, l2_service, llm_eval, replay, routes_llm, routes_portfolio, routes_predict, routes_tasks` | `git diff --stat -- lottery/<each>.py` 全部为空 |
| 函数体内 7 处延迟导入 | `shape_rules`(60) / `patterns_runtime`(559,657,2528) / `evaluate`(578) / `discovery`(597) / `ai_audit`(1045) —— **位置一字不许动** | 见 §2.3：提到顶层会立刻成环 |
| 27 个私有符号 | 全部保留且 `is` 恒等 | 守卫测试 `test_private_symbols_survive` + `test_thin_shell_reexports_same_objects` |
| 统计判据 / EV 表述 / 概率数字 | 任何 `.5` 键值、任何四舍五入、任何 `round()` 参数都不许变 | §5 等价性脚本 + 函数体 SHA256 逐函数比对 |
| `data/ssq.db` | 只读。`persist=False` 跑等价性验证 | 脚本里固定 `persist=False` |

**额外一条自查命令**（每批提交前跑）：

```bash
cd /root/DSH/应用/ssqyuce && git diff --stat -- lottery/stats/ lottery/eprocess.py \
  lottery/rigor.py lottery/api_app.py lottery/battle.py lottery/blue_coverage.py \
  lottery/cli.py lottery/evaluate.py lottery/evolution.py lottery/l2_service.py \
  lottery/llm_eval.py lottery/replay.py lottery/routes_llm.py \
  lottery/routes_portfolio.py lottery/routes_predict.py lottery/routes_tasks.py
# 期望输出：空（无任何改动）
```

---

## 5. 问题五：等价性验证方法

### 5.1 脚本设计

**脚本**：`docs/delivery/repro/d02_equivalence.py`（已落地并**实测跑通**）

**参数选择及理由**：

| 参数 | 取值 | 为什么 |
|---|---|---|
| `rng` | `random.Random(20261021)` | `predict_next` 内部有加权抽样。不给种子 → 两次结果必然不同，等价性无从谈起 |
| `use_llm` | `False` | LLM 通道有网络/超时，不可复现。**且拆分不碰 LLM 通道**，测它没有意义 |
| `persist` | `False` | 不写库，避免污染 `data/ssq.db` |
| `n_tickets` | 1 / 10 / 16 / 50 | 覆盖「单注 / 默认 / 蓝球覆盖满 16 / 大池」四条分支 |
| `use_ml` | True / False | ML 并入 Brier 融合与否是两条路径 |
| `coverage` | True + `coverage_r=3` | 走 `coverage_optimize` 分支（S9 跨 7 域调用最密的地方） |
| 数据 | `db.load_draws()` 全量 3517 期 | 真实数据；脚本记录 `data_rows` 并在对比时断言一致 |

**比什么**（五层，任一层不同即失败）：

1. `json.dumps(result, sort_keys=True)` 的 **sha256** —— 逐字节
2. **顶层键路径全集**（递归进嵌套 dict，不进 list）—— 结构不得增减字段
3. 票单 **字段集合**（每个 ticket 的 key 的 frozenset）—— 字段不得增减
4. **注数** `len(tickets)`
5. **纯计算入口**的哈希：`ticket_distribution` / `_single_ticket_ev` / `portfolio_expected_value`
   —— 这三条不走随机，是最干净的判定面

### 5.2 ⚠️ 必须先解决的一个坑：`generated_at` 让「逐字节相同」不可执行

**我实测到的**：同一份代码（未拆分）连跑两遍，**6/6 用例 sha256 全不相同**：

```
❌ [default] 输出 sha256 不同
   pre  97e77a426feb53bc...  post 027b2792a9f9488c...
```

`unified diff` 定位到**唯一**差异：

```
- "generated_at": "2026-10-21 12:47:59",
+ "generated_at": "2026-10-21 12:48:09",
```

**结论**：若不剔除时间性字段，「拆分前后逐字节相同」这条验收标准**永远不可能通过**，
而下一轮拆分会被误判为「改坏了行为」，白干一天并可能被回滚。

**修法（已在脚本里实现并验证）**：

```python
VOLATILE = {"generated_at", "created_at", "timestamp", "elapsed_ms",
            "duration_ms", "took_ms", "generated_at_ts"}

def _strip_volatile(obj):
    if isinstance(obj, dict):
        return {k: _strip_volatile(v) for k, v in obj.items() if k not in VOLATILE}
    if isinstance(obj, list):
        return [_strip_volatile(v) for v in obj]
    return obj
```

**自证可复现**（这是判定方法本身的可信度证明，必须先跑）：

```bash
cd /root/DSH/应用/ssqyuce && .venv/bin/python docs/delivery/repro/d02_equivalence.py --twice
```

**实测输出**：

```
✅ 等价性验证通过：9 个用例全部逐字节相同
   数据 3517 期，SEED=20261021
```

> **9 个用例 = 6 个 `predict_next` 分支 + 3 个纯计算入口。**
> 这是「脚本本身可复现」的证明；拆分前后的对比是它的第二个用法。

### 5.3 另一处实测发现：`n_tickets=50` 实际返回 10 注

`CASES` 里 `n50` 传 50，实测返回 **10 注**（`bytes` 与 `default` 接近）。
这不影响等价性判定（两侧都是 10 注就比对 10 注），但**说明 `predict_next` 的
`n_tickets` 并非「要多少给多少」**——候选池上限或覆盖上限在起作用。
方案阶段仅记录，**不作为拆分要改的对象**（改它就是改行为）。

### 5.4 完整执行流程

```bash
# ① 拆分前，在 HEAD 上取基线
cd /root/DSH/应用/ssqyuce
git stash -u
.venv/bin/python docs/delivery/repro/d02_equivalence.py --save /tmp/d02_pre.json
git stash pop

# ② 拆分后
.venv/bin/python docs/delivery/repro/d02_equivalence.py --save /tmp/d02_post.json

# ③ 断言（退出码 0 = 通过）
.venv/bin/python docs/delivery/repro/d02_equivalence.py --compare /tmp/d02_pre.json /tmp/d02_post.json
```

**注意**：两态必须用**同一份 `data/ssq.db`**。若期间 `db.load_draws()` 返回期数不同，
脚本会先报 `数据行数不同` 并中止 —— 这是刻意设计，避免把「数据变了」误判成「代码改了」。

### 5.5 第二条闸门：函数体 SHA256 逐函数比对

等价性脚本测的是**行为**，这条测的是**文本**：

```bash
# 拆分前生成指纹
.venv/bin/python docs/delivery/repro/d02_fingerprints.py --save /tmp/fp_pre.json
# 拆分后
.venv/bin/python docs/delivery/repro/d02_fingerprints.py --save /tmp/fp_post.json
# 比对（106 个定义符号，逐个 ast.unparse 后 SHA256）
.venv/bin/python docs/delivery/repro/d02_fingerprints.py --compare /tmp/fp_pre.json /tmp/fp_post.json
```

> 该脚本与等价性脚本同目录，**下一轮开工时先产出它**。
> 判据：106 个符号的 `ast.unparse` SHA256 **全部相等**。任何一个不等，
> 即证明「顺手优化」了 —— 这比人眼抽查可靠，也是「禁止顺手优化」的机器化证明。

---

## 6. 问题六：人日与风险

### 6.1 工时估计（含验证，不含写文档）

| 批次 | 内容 | 行数 | 人日 |
|---|---|---|---|
| 0 | 建包骨架 + 全量回填 + 守卫测试 + 三闸门跑通 | — | **0.5** |
| 1 | S7 奖金期望（纯自足叶子） | 131 | 0.25 |
| 2 | S3 融合权重（纯自足叶子） | 382 | 0.5 |
| 3 | S6 蓝球（纯自足叶子） | 272 | 0.25 |
| 4 | S4 主流程 + S1 硬约束 + S2 统计采样（三小域，合计 178 行） | 178 | 0.5 |
| 5 | S8 票单分布（31 符号，常量最多） | 520 | 0.5 |
| 6 | S5 规律出号（含 7 处延迟导入中的 5 处） | 489 | 0.5 |
| 7 | S10 求解器 | 374 | 0.25 |
| 8 | **S9 覆盖优化 + predict_next**（16 个跨域调用） | 730 | **1.0** |
| — | 全量等价性 + 指纹 + 冷启动 + 12 fanin 零改动核查 | — | 0.5 |
| | **合计** | | **≈ 4.25 人日** |

**乐观 3.5 人日 / 现实 4.25 人日 / 悲观 6 人日**（悲观情形 = 第 8 批打桩穿透出问题返工）。

> 如果只看「搬代码」，是 1 人日。**多出来的 3 人日全在验证上** —— 而这次事故已经证明，
> 省掉验证的代价是全队回归网失效。

### 6.2 最大的两个风险

**风险 1：第 8 批 `predict_next` 拆分 —— 概率 60%，影响：全队回归网失效**

`predict_next` 一处调用 16 个跨域函数（§1.5 全清单）。若任何一处写成
`from ._s6_blue import _apply_blue_mode` 这种早期绑定，被打桩的内部调用就失效：

```python
# tests/test_v10_cost.py:438
monkeypatch.setattr(engine, "llm_tickets", _boom, raising=False)
```

`llm_tickets` 属 S5、`predict_next` 属 S9。**若 S9 早期绑定了 `llm_tickets`，
这个打桩就完全失效，`test_v10_cost` 会「单跑绿、全量红」。**

**缓解**：
- §3.6 的打桩穿透自检**作为第 8 批的强制闸门**，不是可选项；
- 第 8 批单独一个 commit，可独立回滚；
- 优先在第 0 批就把打桩自检写进 `tests/test_v14_engine_split.py`，让它在骨架阶段就红。

**风险 2：`import *` 丢私有符号 —— 概率 35%，影响：27 个符号消失**

`import *` 不导出下划线名，而本包有 **27 个私有符号被外部使用**（含
`_single_ticket_ev` 被 `routes_portfolio.py:250` 用、`_rebuild_probs` 被
`battle.py`/`evolution.py`/`replay.py` 共 13 处用、`_min_significant_blue_sample`
被 `config.py` 用）。这正是 `rigor` 薄壳踩过、造成 16 例 AttributeError 的同一个坑。

**缓解**：
- §3.3 骨架**明确不用 `import *`**，改用 `dir()` 镜像循环（读子模块 → 写自身 globals）；
- 守卫测试 `test_private_symbols_survive` 列出 23 个必检名字（另有 4 个在 AST 符号表里
  一并覆盖），少了任何一个立即红；
- **绝不给子模块加 `__all__`**（加了反而要人工维护 27 个私有名，漏一个就炸）。

### 6.3 补充风险（不进「最大两个」，但要知道）

| # | 风险 | 缓解 |
|---|---|---|
| 3 | 模块→包迁移时 `from . import X` 全部失效（**本次已真实踩到**） | 见 §7 完整清单 |
| 4 | 子模块 `from . import engine` 拿到半初始化模块 | §3.4 `sys.modules[__package__]` + 守卫测试 |
| 5 | `.pyc` / `__pycache__` 残留旧 `engine.py` 造成假绿 | 每批 `find . -name __pycache__ -delete` 后冷启动验证 |
| 6 | 常量归属错位（`TICKET_COST` 被切到别处）导致 `NameError` | 10 域切法已把常量定义行全部归位（§1.2 符号数列已核） |

---

## 7. 问题七（新增）：包转换的导入路径问题 —— 本次事故根因

### 7.1 事故记录

我在准备骨架时执行了 `git mv lottery/engine.py lottery/engine/__init__.py`，
随后 `import lottery.engine` 立即失败：

```
ImportError: cannot import name 'backtest' from partially initialized module
'lottery.engine' (most likely due to a circular import)
  at lottery/engine/__init__.py:11
```

全套测试在**收集阶段**崩溃（10 个测试文件），全队回归网当场失效。

**根因**：`engine.py` → `engine/__init__.py` 之后，相对导入的**基准变了**：

| 写法 | 在 `engine.py` 里 | 在 `engine/__init__.py` 里 |
|---|---|---|
| `from . import backtest` | `lottery.backtest` ✅ | **`lottery.engine.backtest`** ❌ 不存在 |
| `from .llm_client import X` | `lottery.llm_client` ✅ | **`lottery.engine.llm_client`** ❌ |

`.py` 文件是 `lottery` 包的成员，`.` = `lottery`；
`lottery/engine/__init__.py` 是 `lottery.engine` 包本身，`.` = `lottery.engine`。**升了一级。**

**处置**：已 `git reset --hard` 回滚，`engine.py` 从 HEAD 逐字节还原（md5 `d8a80a84…` 与 HEAD blob 一致），
`lottery/engine/` 目录已消除，冷启动 `import lottery.engine` 恢复正常（127 个属性名）。
**工作区当前干净，未提交任何东西。**

### 7.2 每一个受影响的导入行（完整清单，不遗漏）

**A. `engine/__init__.py` 的顶层相对导入（3 行，必须从 `.` 改成 `..`）**

| 原行号 | 原文（在 `engine.py` 时代正确） | 改为 |
|---|---|---|
| 11 | `from . import backtest as BT` | `from .. import backtest as BT` |
| 12 | `from . import config, db, features as F, llm_client, methods as METH, ml_model, models as M` | `from .. import config, db, features as F, llm_client, methods as METH, ml_model, models as M` |
| 13 | `from .llm_client import LLMChannelError` | `from ..llm_client import LLMChannelError` |

> 第 12 行是**一个语句带 7 个模块**，任何只改第一个模块的补丁都会留下 6 个地雷。

**B. 迁移到子模块后，原本的顶层导入也要各升一级**

子模块 `lottery/engine/_sX.py` 要引用 `lottery` 下的模块时，`from .` 是 `lottery.engine`，
`from ..` 才是 `lottery`：

| 目标 | 子模块里的正确写法 |
|---|---|
| `lottery.config` / `db` / `features` / `methods` / `models` / `ml_model` / `llm_client` / `backtest` | `from .. import config` 等 |
| `lottery.llm_client.LLMChannelError` | `from ..llm_client import LLMChannelError` |
| `lottery.engine` 自身（为了 `E.xxx()`） | `E = sys.modules[__package__]` **或** `from .. import engine as E`（函数内） |
| 兄弟子模块 | `from . import _s1_constraints` |

**C. 函数体内 7 处延迟导入（不动位置，但要改前缀）**

| 行号 | 原文 | 改为 | 归属子模块 |
|---|---|---|---|
| 60 | `from . import shape_rules as _SR` | `from .. import shape_rules as _SR` | `_s1_constraints`（`constraint_ctx` 内） |
| 559 | `from . import patterns_runtime` | `from .. import patterns_runtime` | `_s4_context`（`build_context` 内） |
| 578 | `from . import evaluate as EV` | `from .. import evaluate as EV` | `_s4_context`（`_last_feedback` 内） |
| 597 | `from . import discovery` | `from .. import discovery` | `_s5_rules`（`_rules_hit` 内） |
| 657 | `from . import patterns_runtime` | `from .. import patterns_runtime` | `_s5_rules`（`apply_rules_to_candidates` 内） |
| 1045 | `from . import ai_audit` | `from .. import ai_audit` | `_s5_rules`（`_latest_audit_digest` 内） |
| 2528 | `from . import patterns_runtime` | `from .. import patterns_runtime` | `_s9_orchestrator`（`predict_next` 内） |

> **⚠️ 这 7 处只改前缀 `"." → ".."`，位置（仍在函数体内）与顺序一字不动。**
> 提到顶层会立刻成环：`evaluate.py:8` 就是 `from . import db, engine as E`，
> `engine` 顶层导入 `evaluate` = 循环导入，`import lottery.engine` 当场失败（§2.3）。

**D. 反向核对：`lottery/stats/` 不受影响**

`lottery/stats/` 用 `test_v14_stats_package.py:128` 断言「不得含 `from .`」，
它本来就是纯标准库、无项目内相对导入，**包转换与它无关，一行不动**。

### 7.3 定论：**保留 `engine.py` 作模块，不改成包**

我推翻自己上一轮的「包」方案。理由是**风险/收益算不过来**：

| 方案 | 改动面 | 风险 | 判定 |
|---|---|---|---|
| **A. 包 `lottery/engine/`** | 3 处顶层导入 + 7 处延迟导入前缀 + 半初始化陷阱 | 高（已真实踩爆一次） | **否决** |
| **B. 平铺 `lottery/engine_*.py` + `engine.py` 薄壳** | **0 处导入前缀改动** | 低 | **采用** |

**为什么 B 能 0 改动**：`engine.py` 仍是 `lottery` 包的成员，`from . import backtest`
依然解析成 `lottery.backtest`。所有 7 处延迟导入**一个字都不用动**。
新文件 `lottery/engine_blend.py` 等在 `lottery` 包内，同样用 `from . import config`。

**代价**：`lottery/` 根目录多出 10 个 `engine_*.py`。
**我认为这个代价远小于包方案的风险** —— 而且 `hypothesis.py`/`evolution.py` 等
1978 行的模块本来就和 `engine_*` 平铺在一起，目录观感并不因此变差。

**B 方案的薄壳骨架（`engine.py` 全文，约 60 行）**：

```python
"""集成预测引擎：统计模型 + LLM 通道 → 硬约束过滤 → 置信度评分 → Top-N 输出。

## V14-A（2026-10-21）拆分说明

实现已按域迁至同目录下的 `engine_*.py`（10 个子模块），本文件为**组合根 + 代理**。

**为什么是平铺 `engine_*.py` 而不是 `engine/` 包**：
把 `engine.py` 改成 `engine/__init__.py` 会让所有 `from . import X` 的基准
从 `lottery` 变成 `lottery.engine`，3 处顶层导入 + 7 处函数内延迟导入全部失效，
且 `import lottery.engine` 直接抛 ImportError（本轮已真实踩到，全队回归网中断）。
平铺方案**零导入改动**，风险低一个量级。

**为什么用代理而不是 `import *`**：
本模块有 27 个下划线私有符号被外部使用，`import *` 不导出私有名
（`rigor` 薄壳曾因此造成 16 例 AttributeError）。

**为什么不用镜像复制**：会产生第二真源，monkeypatch 只改一份，单跑绿全量红。
"""
from __future__ import annotations

import sys as _sys
import types as _types

# 依赖与原文件同序；`. ` = lottery 包，与原 engine.py 完全一致
from . import backtest as BT                                     # noqa: F401
from . import (config, db, features as F, llm_client,             # noqa: F401
               methods as METH, ml_model, models as M)
from .llm_client import LLMChannelError                           # noqa: F401

R_MAX, B_MAX = 33, 16


class InsufficientDataError(ValueError):
    """（原文 docstring 一字不动）"""


# ── 各域挂载，顺序 = 依赖拓扑序（DAG 见 D02 方案 §1.4）──────────────
from . import engine_constraints                                 # noqa: E402  S1
from . import engine_sampling                                    # noqa: E402  S2
from . import engine_blend                                       # noqa: E402  S3
from . import engine_context                                     # noqa: E402  S4
from . import engine_rules                                       # noqa: E402  S5
from . import engine_blue                                        # noqa: E402  S6
from . import engine_prize                                       # noqa: E402  S7
from . import engine_distribution                                # noqa: E402  S8
from . import engine_solver                                      # noqa: E402  S10（先于 orchestrate）
from . import engine_orchestrate                                 # noqa: E402  S9

_SUBMODULES = (engine_constraints, engine_sampling, engine_blend, engine_context,
               engine_rules, engine_blue, engine_prize, engine_distribution,
               engine_orchestrate, engine_solver)

_OWN = {"__name__", "__doc__", "__file__", "__package__", "__loader__",
        "__spec__", "__builtins__", "__all__", "_SUBMODULES", "_OWN",
        "_ProxyModule", "_sys", "_types"}


class _ProxyModule(_types.ModuleType):
    """未定义的属性转发给子模块；写操作双边同步（保证 is 恒等、打桩穿透）。"""

    def __getattr__(self, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        for mod in _SUBMODULES:
            try:
                return getattr(mod, name)
            except AttributeError:
                continue
        raise AttributeError(
            f"module 'lottery.engine' has no attribute {name!r}")

    def __setattr__(self, name, value):
        if name in _OWN:
            super().__setattr__(name, value)
            return
        for mod in _SUBMODULES:
            if hasattr(mod, name):
                setattr(mod, name, value)
        super().__setattr__(name, value)

    def __delattr__(self, name):
        if name in _OWN:
            super().__delattr__(name)
            return
        for mod in _SUBMODULES:
            if hasattr(mod, name):
                delattr(mod, name)
        if name in self.__dict__:
            super().__delattr__(name)


_sys.modules[__name__].__class__ = _ProxyModule

# 组合根只搬名字，不做计算（dir() 镜像，**不是 import ***）
for _m in _SUBMODULES:
    for _n in dir(_m):
        if not _n.startswith("__"):
            globals()[_n] = getattr(_m, _n)
del _m, _n

__all__ = [n for n in dir(_sys.modules[__name__]) if not n.startswith("_")]
```

**B 方案子模块骨架（`lottery/engine_prize.py`）**：

```python
"""V4-C 奖金期望模型（覆盖优化的目标函数）。原文逐字迁移，未改一行。"""
from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np

from . import config        # `.` = lottery，与拆前的 engine.py 完全相同
from . import engine as E   # 模块在 lottery 包内，`from .` 就是 lottery，
                            # 因此这里拿到的是真正的 lottery.engine 薄壳对象，
                            # **不存在包方案的半初始化陷阱**


def expected_prize(reds: List[int], blue: int, ...):
    ...   # 原 1373–1431 行，逐字
```

> **B 方案顺带消灭了「半初始化陷阱」**：`lottery/engine_prize.py` 里
> `from . import engine` 的 `.` 是 `lottery`，取的是 `lottery.engine` 属性，
> 这个属性在 `engine.py` 模块对象创建时已存在（模块对象先注册进 `sys.modules`）。
> 但为绝对安全，仍推荐 `from . import engine as E` 放在**文件头**并在函数体里
> `E.xxx()` 访问 —— 属性查找发生在调用时，天然避开的只是「导入死锁」而非「半初始化」。

### 7.4 一句话经验（写进交接）

> **模块 → 包 的迁移，必然要审计所有顶层相对导入并升一级（`.` → `..`）；
> 且子模块对父包的引用不能用 `from . import engine`（包内 `.` 指向父包自身，
> 半初始化陷阱）。若拆分的收益不足以覆盖这个改动面，就不要改成包 ——
> 平铺 `xxx_*.py` 是零导入改动的等价方案。**

---

## 8. 哪些子系统**不该拆**

**答：没有「不该拆」的域，但有「不该按某个粒度拆」的三处。**

### 8.1 `# ===` 大标题下的 S8 不能拆成两半

`1484` 的 `# ====` 块里含两个 `# ---` 子节：「数学（1492）」与「三个必须记住的边界（1504）」。
**它们是同一个域，不能拆**，理由（依赖方向，不是观感）：

- `RED_PER_TICKET` / `SIGMA1_SQ` / `E_HITS_PER_TICKET` / `VAR_RATIO_SLOPE` 定义在
  「三个边界」（1514–1521），被「数学」里的 `_var_ratio`（1627）、`_variance_from_abar`（1620）
  直接使用；
- `PRIZE_ASSUMPTION_TABLE_A/B`（1523/1529）被 `expected_prize`（S7）跨域使用；
- `_POPCOUNT_LUT` / `_EXACT_TAIL_CACHE`（1536–1539）被 `_popcount33` / `_exact_red_tail` 使用，
  且是**可变模块状态**（缓存），拆开就真的成了两份缓存。

拆开会让 `_EXACT_TAIL_CACHE` 变成两份，直接制造第二真源。

### 8.2 `S1 硬约束` 不该单独提前切

`InsufficientDataError` 被 6 个域使用（入度 5，是全图最高）。若第 1 批就切 S1，
`engine.py` 薄壳在第一版里就有 6 个子模块依赖它 —— 骨架期就把最高耦合点先搬走，
一旦出问题，回滚面 = 全部。**应放到第 4 批与 S2/S4 一起切**（见 §6.1 批次表）。

### 8.3 `S9` 不该和任何东西合并切

`S9` 出度 7、入度 0，是唯一「调所有人、没人调它」的编排层。把它和 S10 合并
（8 刀法就是把它俩与 S10 分开切、但把 `PORTFOLIO_*` 常量错划进 S9）会让
`PORTFOLIO_OBJECTIVES` 的归属含糊。**S9 独立一批、最后一批。**

---

## 9. 批次计划（每批 1 commit，可单域回滚）

| 批 | commit 主题 | 域 | 行数 | 验收闸门 |
|---|---|---|---|---|
| 0 | `refactor(engine): 建 engine_* 平铺骨架 + 代理薄壳 + 拆分守卫测试` | 无（全量回填） | 0 迁移 | **1358 必须全绿，否则就地停** |
| 1 | `refactor(engine): 切出 S7 奖金期望 engine_prize.py` | S7 | 131 | 五闸门 |
| 2 | `refactor(engine): 切出 S3 融合权重 engine_blend.py` | S3 | 382 | 五闸门 |
| 3 | `refactor(engine): 切出 S6 蓝球 engine_blue.py` | S6 | 272 | 五闸门 |
| 4 | `refactor(engine): 切出 S1/S2/S4 硬约束+采样+上下文` | S1,S2,S4 | 178 | 五闸门 |
| 5 | `refactor(engine): 切出 S8 票单分布 engine_distribution.py` | S8 | 520 | 五闸门 |
| 6 | `refactor(engine): 切出 S5 规律出号 engine_rules.py` | S5 | 489 | 五闸门 |
| 7 | `refactor(engine): 切出 S10 求解器 engine_solver.py` | S10 | 374 | 五闸门 |
| 8 | `refactor(engine): 切出 S9 覆盖优化 engine_orchestrate.py` | S9 | 730 | 五闸门 + **打桩穿透自检** |

**五闸门**（每批必过，缺一不可）：

```bash
# ① 全量测试
.venv/bin/python -m pytest tests/ -q --ignore=tests/frontend
#    期望：1358 passed, 24 skipped, 1 xfailed

# ② 等价性
.venv/bin/python docs/delivery/repro/d02_equivalence.py --compare /tmp/d02_pre.json /tmp/d02_post.json

# ③ 函数体指纹（106 符号 SHA256 全等）
.venv/bin/python docs/delivery/repro/d02_fingerprints.py --compare /tmp/fp_pre.json /tmp/fp_post.json

# ④ 冷启动 + 符号完整性（清缓存后）
find . -name __pycache__ -type d -not -path './.venv/*' -exec rm -rf {} +
.venv/bin/python -c "from lottery import engine as E; print(E._single_ticket_ev, E._rebuild_probs, E._sanitize_patterns_used)"

# ⑤ 禁止改动面核查
git diff --stat -- lottery/stats/ lottery/eprocess.py lottery/rigor.py \
  lottery/api_app.py lottery/battle.py lottery/blue_coverage.py lottery/cli.py \
  lottery/evaluate.py lottery/evolution.py lottery/l2_service.py lottery/llm_eval.py \
  lottery/replay.py lottery/routes_llm.py lottery/routes_portfolio.py \
  lottery/routes_predict.py lottery/routes_tasks.py
#    期望：空
```

**commit message 模板**（满足任务书「写明切出哪些函数、行数变化、下游 import」）：

```
refactor(engine): 切出 S7 奖金期望 → engine_prize.py

切出函数（6 个）：
  expected_prize(1373-1431)          _portfolio_blue_hit_prob(1434-1439)
  portfolio_expected_value(1441-1481) TICKET_COST(1364)
  PRIZE_CASH_FIXED(1365)              JACKPOT_ASSUMPTION(1366-1372)

行数：engine.py 3107 → 2976（-131）；新增 engine_prize.py 148 行（含文件头）
下游：0 个 import 改动。全部 22 个调用点走 engine.xxx，由薄壳再导出，无需改动。
闸门：1358 passed / 24 skipped / 1 xfailed；等价性 9/9；指纹 106/106；冷启动 OK
```

---

## 10. 附：本方案的实测依据与复现命令

| 结论 | 命令 |
|---|---|
| 分节边界是 10 个，合计 3107 行 | `grep -n '^# ---\|^# ===' lottery/engine.py` |
| 符号数：76 函数/类，106 顶层符号 | `docs/delivery/repro/d02_fanout.py`（同目录另有 AST 脚本） |
| fanout：口径 A=8 / B=13 / C=24 | `docs/delivery/repro/d02_fanout.py` |
| 8 刀漏掉 227 行 / 11 个定义 | 见 §1.3，用 AST 逐符号定位 |
| 跨域依赖 DAG 与入/出度 | §1.4 表格（AST 自由度分析） |
| `is` 恒等、打桩穿透可验 | §3.5 / §3.6 命令 |
| `generated_at` 破坏可复现性 | `d02_equivalence.py --twice`（未剔除时 6/6 不一致） |
| 等价性脚本自身可复现 | `d02_equivalence.py --twice` → `9 个用例全部逐字节相同` |
| 无 `from engine import xxx` 直接符号导入 | `grep -rn 'from \.engine import\|from engine import' --include='*.py' .` → 空 |
| 生产代码全部走 `engine as E` 属性访问 | 13 个模块，逐行列出（§4） |

**`docs/delivery/repro/` 下需要的脚本**：

- ✅ `d02_fanout.py` —— 已落地，已跑通
- ✅ `d02_equivalence.py` —— 已落地，已跑通（含 `--twice` 自证）
- ⬜ `d02_fingerprints.py` —— **下一轮开工第一件事就产出**（§5.5）

---

## 11. 结论

1. **值得拆**，但收益是「单测能锁住改动」，不是「文件变短」。若反对这个理由，我建议不拆。
2. **切法必须从 8 刀改成 10 刀**，否则漏掉 227 行 / 11 个定义（含 `TICKET_COST`），第一批就 `NameError`。
3. **采用平铺 `engine_*.py`，否决 `engine/` 包**——包方案有 10 处导入要改且已真实踩爆一次。
4. **薄壳用代理、不用镜像、不用 `import *`**，保证 27 个私有符号存活且 `is` 恒等。
5. **等价性判定必须先剔除 `generated_at`**，否则那条验收标准不可执行。
6. **4.25 人日**，第 8 批（`predict_next`）是最大风险，打桩穿透自检是它的专用闸门。

**方案在此，等签收后开工。生产代码一行未动。**
