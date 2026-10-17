# V10 改进方案 v2.1：把「找不存在的规律」彻底换成「可机器判定的工程与口径」

> **版本**：**v2.1（2026-10-17，二次评审 B1–B5 修复后）**
>
> **变更史**：v1.0 初稿 → v1.1 首轮评审事实更正 → **v2.0** 按 4 份评审裁决重做设计 → **v2.1** 按二次评审（S1/S2）修复 B1–B5
>
> **v2.1 修了什么（B1–B5）**：
> - **B1（P0）新增 §3.3.5b「写者规格」**：v2.0 只在读侧用 `prereg_ok`，**从未规定谁写它** ——
>   而 U1–U5 全部 monkeypatch `load_registry`，即使实现者永不置 True 也全绿。
>   现补 `evaluate_prereg()` 完整签名 + 调用点写死为调度器 + 库根计数器 + **E1/E2/E3 三条 E2E 测试**（不 mock 任何内部）。
> - **B2（P0）`var_ratio` 不再是"可为负"**：原文「n=10、Ā=0 → −3.000」三处错（算术应 −1.000；该构造需 60 个号 > 33 不可能；−3.000 对应 n=19）。
>   真实**下界为 0**（n=11 每号 2 次），负值测试用例**不可构造**，已删除该要求。
> - **B3（P0）`source` 层判定**：`config.py:239/267` 把生效值写回 `os.environ`，`.get` 永久分不清三层；
>   改用「查文件优先 + `in os.environ`」+ `config.METHODS_SOURCE` import 期定值。
> - **B4（P1）四处照抄会错**：`LEVEL_NAMES` 不存在（实为 `evaluate.PRIZE_NAME`，一/二等奖带 `(≈*)`）；
>   `discovery_params_ok` 收**单个 dict**；T1 的 `blend` 断言与 allow-list 默认值互斥；`replay_results` 主键必须含 `mode`。
> - **B5（P2）数字漂移**：基线 480 → **489**；字段表 12 → **16**（补 `divergence`/`registered_issue`/`prereg_reason`/`alpha`）；
>   DDL 主键同步；§2.2/2.3/2.4 标记"已落地"。
> **权威口径**：[`docs/reviews/V10_REVIEW_SUMMARY.md`](reviews/V10_REVIEW_SUMMARY.md)。本文件与该裁决冲突时，以裁决为准；本文件内部冲突时，以 `第零部分 删除台账` 与 `附录 B 数字来源台账` 为准。
> **评审原文**：`docs/reviews/R1_ARCHITECTURE_REVIEW.md`（265 行）、`R2_STATISTICS_REVIEW.md`（597 行）、`R3_AI_REVIEW.md`（324 行）、`R4_RELEASE_REVIEW.md`（505 行）
> **评审结论**：4 份评审**无一给出"认可"**（R1 2.4/5、R2 2.0/5、R3 AI 三个 P0 项全部 <3/5、R4 工程骨架可但 4 处必须前置）。
> **本文件性质**：**重新做的设计**，不是勘误表。v1.0/v1.1 中被判定不可用的设计**已从正文移除**，只在 §0.2 删除台账里留审计痕（那不是实现指引）。
> **适用代码库**：`/root/DSH/应用/ssqyuce`（当前 `APP_VERSION=2.5.0` / `APP_BUILD=2026-10-V9`，schema_version=2，全量基线 **489 passed**（V10-00 落地 485 + A1/A2 回归测试 4））
> **读者**：本项目程序员。本文是可直接排期实现的技术规格，**每一节都以"能写出测试"为完成标准**。
> **前置阅读**：`docs/PROJECT_CONTEXT.md`、`docs/V4_ACCURACY_DIAGNOSIS.md`、`docs/V6_REGIME_DIAGNOSIS.md`、`docs/reviews/*.md`

---

## 第零部分　返工说明

### 0.1 本次返工做了什么（v1.1 → v2.0）

| 类型 | 数量 | 内容 |
|---|---|---|
| **彻底删除**（不是删除线） | 5 项 | AI 决策排序+技能度量、选择性推断、CUSUM 序贯监控、P0-4 第 2 条死锁、`LOTT_PREDICT_CHANNELS` |
| **删除断言/降级** | 4 项 | `var_ratio` 随 coverage 上升、"覆盖优化无收益"、`p_any_blue` 优化目标、abstain 三项引用契约 |
| **落点变更** | 3 项 | 登记簿改为并入 `hypothesis.py` 加字段、`ticket_distribution` 归 `engine.py` 而非 `rigor.py`、成本关停接 `resolve_channels()` |
| **诊断更正** | 2 项 | SA"白跑 3000 次"→`:1492-1496` 零梯度 + `:1409` 死代码；LLM 出号影响的真实面（5 期并非 4 期、n=20 不能下结论） |
| **新增章节** | 6 节 | §2.0 Gate 清单 10 条、§2.1 V10-00a 契约冻结、§2.2 V10-00b 测试隔离、§2.3 V10-00c 备份、§2.4 V10-00d 迁移加固、§4 任务表重写 + §5 工程规约 |
| **数字更正** | 10 处 | 见 §0.3（F1–F10 全量） |
| **任务表重写** | 全部 | 按裁决 §五 的 V10-00a..V10-12，含依赖/估时/**真实并行度 2 路** |

### 0.2 删除台账（审计留痕，**不是实现指引**）

> **给程序员的明确指令**：本表只用于回答"这个方案为什么不做 X"。表中"处置"为**删除**的项，**不存在**任何后续实现步骤；若在下文任何地方看到与之对应的实现细节，那是文件缺陷，请立刻报给 Lead，不要照着写。

| 原编号 | 原内容 | 处置 | 替代方案（新编号） | 依据 |
|---|---|---|---|---|
| **P0-1** | LLM 给候选票单打分排序；`llm_decision_skill` 表；`Kendall tau`/`correct_top1`；N≥30 关停 | **删除，不实现** | V10-01 两岗位（§3.2） | G2 / R3 §3 / R3 §5 / R1 §5 |
| **P1-1** | 票单级选择性推断：`truncnorm` + 凸约束 + 单期轴条件推断 | **删除，不实现** | holdout / 交叉拟合，并入 V10-03（§3.4.6） | G6 / R2 §3.2 / B3 |
| **P1-2** | CUSUM / E-Detector / ARL0=500 报警与"失效时点 + CI" | **删除，不实现** | regime 三处改进，V10-07（§3.7） | G7 / R2 §3.3 / B4 |
| **P0-4 第 2 条** | LLM 候选必须 `experiments.status=="passed"` 才能进候选池 | **删除**（按设计死锁：该集合恒为空） | LLM 出号走独立 A/B 对照臂、**永不进规律库**、允许长期 `eligible`，V10-09（§3.9） | G1 / R3 P0-4-2 |
| **G5** | 新增 `LOTT_PREDICT_CHANNELS`（第 7 套开关） | **删除，不新增** | `resolve_channels()` 唯一判据，V10-00a（§2.1） | G5 / R1 P0-1 |
| `experiments` 新表 | 第 4 本登记簿（SQLite 新表） | **不建** | 并入 `hypothesis.py` 加字段（G4 选 A） | G4 / R1 P0-2 |
| **P1-4** | LLM 因果归因（`factor: blue_coverage/...`） | **删除原设计** | 降级为 V10-01 岗位 A 的逐行核对（可 100% 判定） | R3 P1-4 / G5 |
| **P2-2** | 联邦/多源数据 | **不做** | —（更多数据只能把 MDE 降到约 0.03，仍远大于 0） | 裁决 §六 |
| abstain 契约 | 必须引用 `n_tested`/`best_quick_p_adjusted`/`expected_false_positives`（三项本来就在 prompt 里） | **删除** | 岗位 B 复用契约判定；UI 在错误率估计出来前**禁止**出现"AI 已核对并声明弃权" | G3 / R3 P1-1 |
| 断言 | "`var_ratio` 必须随 coverage 上升" | **删除该断言** | "随 `Ā` 单调上升" + 三个解析值断言 | F6 / H2 / B5 |
| 断言 | "覆盖优化无实测收益" | **降级为"未检出"** | 零功效对比说明（MDE ≈ +40.7%~+101.9%） | F7 / H3 / B7 |
| 目标函数 | `p_any_blue` 作为优化目标 | **删除**（`blue_coverage_plan` 已闭式最优） | 降级为报告项 | R1 P0-5 |
| 数据 | `v1.0` 文件作用域表（"01–05 互不触碰同一文件"） | **重写** | §2.1.7 真实表 | F3 / R1 P1-2 |
| 数字 | "381 passed" | **更正 480 → 现 489** | §5.5 | F2 / F9 |
| 数字 | "σ² = 1.2254 / σ = 1.107" | **更正 0.753099 / 0.867813** | §1.2 | F1 / H5 |
| 命名 | 裸 `ev_ratio` | **禁用** | `ev_ratio_sixth` / `ev_ratio_full` + 奖金假设表 | F8 / H4 / B6 |

### 0.3 事实性更正台账（F1–F10，逐条对应裁决 §二）

| # | v1.0 原文 | v2.0 正确值 | 证据来源 |
|---|---|---|---|
| F1 | `σ²=1.2254, σ=1.107` | **`Var(K)=0.753099174 = 729/968`，`sd=0.867812868`** | 超几何解析 + pmf 数值 + 源码 `discovery.SD_HITS` 三路一致（R2 §3.4.1） |
| F2 | `381 passed` | **`480 passed / 0 failed`**（评审当时）；V10-00 落地后 **485**，A1/A2 回归测试后 **489** | R4 P0-A 独立实跑 + Lead 复核 |
| F3 | "01–05 可 4 路并行" | **真实并行度 2 路**（`api_app.py` 三方改、`engine.py` 03/04 改同一函数、02/05 争 migration #3） | R1 P1-2；§2.1.7 重写表 |
| F4 | "SA 在常数目标上白跑 3000 次、浪费 CPU" | **前提不成立**（候选池 8~10 注，`engine.py:1444` 已提前返回，3000 trials 实测 0.034s）。真缺陷：**`:1492-1496` 强制改写蓝球 → 目标函数蓝球项零梯度**；**`:1409` 是死代码** | R1 P0-5 第 2/4 条 |
| F5 | "Bonferroni 只按本批注册数校正"；规范形式**含截止期号** | **漏洞**：只批内校正时 `FWER=1−(1−α)^B`（N=91 拆 7 批 → 0.297，拆 91 批 → 0.991）；截止期进 `exp_id` = "换 20 个截止期 = 20 批 = 0.642" | R2 §3.1；由 R1/R3/R6 六条约束封死（§3.3） |
| F6 | "`var_ratio` 随 coverage 上升" | **方向错**。正确式 `var_ratio = 1 + (n−1)·(11/54)·(Ā − 12/11)`，阈值 `Ā = 12/11`；5 注全同 = **5.0000**，5 注不相交 = **0.1111** | Lead 独立复算（偏差 < 1e-15）+ R2 §3.4.3（R2 原文系数 11/27 为 2 倍手误，其边界值与阈值正确） |
| F7 | "探针证明覆盖优化无收益" | **口径有偏**（`cov_batch` 未用 rng，两次调用结果完全相同）→ 修正后该口径 MDE ≈ **+101.9%**，属**零功效对比**，只能说明"未检出" | R2 §3.4.6 + Lead 复现 |
| F8 | `ev_ratio` 恒为 0.15625 与"¥0.9135 / 45.7%"并列表述 | **三处口径混用**。`ev_ratio_sixth = 0.15625`（仅六等奖）；`ev_ratio_full ≈ 0.45675`（整注，V4 假设表 A）；换成 R2 的一/二等奖假设则为 ≈0.49448 —— **必须连假设表一起展示** | R2 §3.4.5 / H4 / B6 |
| F9 | `docs/PROJECT_CONTEXT.md:231` 记 381 | 同 F2，差额正是 V6/V7/V8 三个测试文件（56+24+19=99） | R4 P0-A |
| F10 | "迁移前 `cp data/ssq.db data/ssq.db.bak`" | 只备份 1.4MB 库，**漏 271MB 的 `data/` 其余内容**；且**当前零备份**（`/etc/cron.d/ssq-backup`、`/backup/`、`crontab`、`data/*.bak*` 四路实测均不存在） | R4 P0-C + Lead 复核；由 V10-00c 解决（§2.3） |

---

## 第一部分　诊断：为什么「还是没发现规律、AI 作用不明显」

> **本节四份评审一致确认方向正确**（裁决 §六：诊断"✅ 全保留"），仅数字按 F1–F10 更正。**不要因为返工而软化这里的结论。**

### 1.1 结论先行

**这不是算法不够先进，而是这个目标本身在数学上不可达，而系统把最稀缺的算力（LLM）押在了这个不可达目标上。**

三条独立证据（全部在本机真实数据 3515 期、2003001–2026118 上现场验证）：

| 证据 | 数据 | 含义 |
|---|---|---|
| 数据与均匀随机不可区分 | 红球 χ²=45.18/df=32，p=0.061；蓝球 χ²=8.84/df=15，p=0.886 | 无频次偏置可挖 |
| 维度扫描 91 组 × 严格 as-of 回放 | 最优 BH 校正后 p = **1.000**；未校正 p<0.05 的 3 个 ≈ 随机预期 4.55 个 | 91 次搜索什么都没搜到，且这符合随机（R2 §3.5 独立复现：`n_tested=91`、未校正显著 3 个、最小未校正 p=0.035675、Bonferroni×91=1.0） |
| 规律时效（23 阶段） | 17 条规律全部 `monitor`，**usable = 0**；7 条假说全 `proposed`、supports/refutes 全 0 | 闭环结构在，但没有可闭环的对象 |

因此「未发现任何规律」是**正确的输出**，不是 bug。历史上"改进→仍无规律→再改进"的循环（V4→V5→V6→V7→V8→V9 五个大版本），正是因为每个版本都在同一个不可达目标上换方法。

### 1.2 一个必须先纠正的认知：中心极限定理救了它，但它不是优化空间

对红球命中数 K（单注）：`μ = 6×6/33 = 12/11 = 1.090909091`，**`Var(K) = 729/968 = 0.753099174`，`sd = 0.867812868`**。

> **v2.0 更正（F1/H5）**：v1.0 此处误写 `σ²=1.2254 / σ=1.107`，无法由任何超几何口径复现（已按真值更正）。源码 `discovery.SD_HITS = 0.8678128678198537` 与 `rigor.py:72-73` 的 `_HITS_VAR` 均为正确值 —— **代码没错，文档错了**。
> 该错误不影响 §1.1 的 MDE≈0.041 结论，但会误导所有方差相关推导。

**更强的证据（v2.0 新增，替换原来的"σ 论据"）**：同期两注的协方差有**闭式**

```
Cov(K_i, K_j) = (27/176)·A_ij − 81/484 ,   A_ij = |票 i ∩ 票 j|
```

（R2 §3.4.2 用 C(33,6) 全枚举逐项验证到 1e-15；`A=6` 时回到 `σ₁²=0.753099`，自洽。）

| `A_ij` | 0 | 1 | 2 | 3 | 4 | 5 | 6 |
|---|---|---|---|---|---|---|---|
| Cov | −0.167355 | −0.013946 | +0.139463 | +0.292872 | +0.446281 | +0.599690 | +0.753099 |

**这就是"买 5 注/10 注平均不了"的精确原因**：票越像，方差越大；票越不像，方差越小 —— 与"覆盖"不是同一个轴（见 §3.4.2）。任何声称"用统计律提高命中"的算法都在跟这个极限对抗，而它赢不了。

### 1.3 AI 作用不明显的**结构性**原因（这是可以修的）

V6 诊断出三处硬伤，**至今只修了两处，且第三处仍未闭环**：

| # | 问题 | 现状（2026-10-17 实测） | 影响 |
|---|---|---|---|
| 1 | LLM 输入太少 | 已修：`llm_lab` 喂阶段序列 + 维度表 + as-of 候选 | — |
| 2 | LLM 产出无法验证 | 已修：映射到维度表档位 + as-of 回放，映射不到记 `unmappable` | — |
| 3 | **LLM 结论必然为空 → 闭环永远空转** | `data/llm_claims.json` 中 LLM 原文结论：「在给定 3515 期数据与 91 个维度参数组合的全扫描下，未发现任何经多重比较校正后仍成立的可证伪规律，最终规律清单为空」，**n_claims = 0** | AI 唯一的"正确输出"是"什么都没有"，因此它再怎么努力都不会改变出号结果 |
| 4 | **LLM 通道在"自由猜号"，且被配置成了主力** | `LOTT_LLM_ONLY_OUTPUT=1`（默认）→ LLM 产出几注就出几注。`predictions` 表实测 **2026115–2026119 共 5 期**（v1.0 写"4 期"，R1 P1-6 更正为 5 期）全部只有 5 注 `llm:agent-router/deepseek-v4-flash` | 系统把"最没用的通道"设成了默认出号通道 |

> **v2.0 更正（R3 P2-3）**：v1.0 用"eval_details 的 20 注、红球均值 1.30、蓝球 0/20"作为"AI 没用"的证据 —— **n=20 下 p≈0.29，这个样本什么都证明不了**。正确表述是「**样本不足，看不出任何东西**」。这条更正很重要：它逼我们承认 P0-4 的默认值修正是**按设计意图**做的，不是"因为 AI 已被证明没用"。
> **另注**：LLM 通道**不可回放**（`battle.py:53-54,81` 明确 `llm` 不支持 replay），所以任何"回放验证 LLM"的想法都不成立 —— 这正是原 P0-4 第 2 条死锁的技术根源。

### 1.4 测试床本身也不是"严格样本外"

`tests/` / 回测里大量使用 `draws[:t]`，但真正的开奖来自抽取过程。V6 §3.3 已证明：**事后阈值（用全样本算 P85 分位）会让回测虚高 0.0308 个红球/期**，而系统在 3515 期下能检出的最小效应只比它大一点（MDE ≈ 0.041 红球/期）。含义：

> **任何新算法上线前，只要它的阈值来自全样本，它在样本外就"必然略好"，而这个"略好"几乎全部是泄漏。**

这条必须写进验收规范（§5.2）。

### 1.5 数学上真正还能改进的部分（本方案的立足点）

既然期望值不动，就只剩两个方向：

- **(A) 方差与分布**：在固定成本下，改变「命中≥4红 / ≥5红」这类高额奖级的**触发概率**。这是**方差**的变化，不是期望的变化 —— 说得再直白：**买同一批钱的票，中得多与中得少哪个"更好"，取决于用户的偏好，不是数学的最优**。所以绝不能宣称"提高准确率"。
- **(B) 决策质量**：把 AI 从"猜号码"重新部署到"**做可被确定性代码打分的核对与判定**"（§3.2），而不是"定目标 + 因果归因 + 预算决策"这种说了但测不出来的话。

另外必须诚实指出：**v1.0 用来支撑"覆盖优化无实测收益"的探针口径有偏**（F7），当前正确的说法是"**未检出**"，见 §1.6。

### 1.6 三条必须在全文生效的诚实边界（v2.0 新增/更正）

**(a) "未检出" ≠ "已证明无收益"。** 探针 v1 的 `cov_batch` **完全没有用 rng**（300 期跑同一组固定票），Lead 已复现"两次调用结果完全相同"。修正后重跑实测该口径 MDE ≈ **+101.9%（相对）**（R2 独立估 +40.7%，Lead 用更保守方差口径得 +101.9%）→ **两者都远超合理效应量，属零功效对比**。正确表述：

> 「在 300 期、10 注口径下，本检验只能排除 +40% 以上的红球命中提升；所有实际讨论中的效应量（<5%）都不在检验范围内。因此该对比**不能**作为'覆盖优化无收益'的证据，只能作为'预期收益在噪声以下'的量级估算。」

R2 用**每期重抽**的正确口径复算（200 次重复）：累计红命中 `Δ=+0.0179±0.0234`（t=1.50）→ 不可区分；≥4 红期数 `Δ=+0.06±0.74`（每 300 期）→ 不可区分。**v1.0 的"覆盖式 ≥4 红反而略降"方向性结论不成立，已删除。**

**(b) 所有 p 值必须带 `m_adj` + `scope`。** `V6_REGIME_DIAGNOSIS.md:58` 的"2023 年本规律内部 BH 后 p=0.029"与 `discover()` 的 `best_quick_p_adjusted=1.0` **口径不同、不可并列引用**（前者 m=23 段、后者 m=91 维度）。规格：任何返回 p 的结构体必须同时给出 `p`、`m_adj`、`scope ∈ {within_era, within_dim, cumulative}`；前端不得渲染无 `scope` 的 p 值。

**(c) 六等奖 EV 比恒为 0.15625 与"至少中 1 蓝"的概率提升不矛盾**，但必须分开说：蓝球去重把 `P(≥1蓝)` 从 `1−(15/16)^n` 提到 `min(n,16)/16`（n=10 时 47.55%→62.50%）—— 这是**概率/分布**；而**期望**不变（`ev_ratio_sixth = 0.15625` 与 n 无关）。把这两句混成"EV 提高了"是错的。

---
## 第二部分　V10-00 前置 Gate（必须先做，约 2.5 人日，不含任何功能）

> **裁决 §四**：V10-00 必须先做。它**不改变任何产品行为**，但它是后续所有任务的计价基准。

### 2.0 开工前必须落地的 Gate 清单（裁决 §七，10 条）

**任一条不满足，V10-01 及以后不得开工。** 这张表要贴在任务看板最上面，逐条打勾（勾选须附可复现的证据/命令输出）。

| # | Gate | 判据（可复现） | 归属 |
|---|---|---|---|
| **Gate-1** | 方案文本按 §二 F1–F10 全部更正 | 本文件 §0.3 的 10 行全部落到正文；尤其 F1 方差、F2 基线、F6 `var_ratio`、F8 EV 口径 | 已完成（本文件） |
| **Gate-2** | V10-00a 契约冻结完成，`LOTT_PREDICT_CHANNELS` **未新增/已删除**，`resolve_channels()` 落地 | §2.1 全部小节的规格已实现并有测试；`grep -rn "LOTT_PREDICT_CHANNELS" lottery/ web/ tests/` 返回空 | V10-00a |
| **Gate-3** | V10-00b 完成：`tests/conftest.py` 存在、19 处裸 `DB_PATH` 改造完、**全量 `pytest -q` 仍 ≥ 485 passed**（V10-00 后基线） | 见 §2.2 验收命令 | V10-00b |
| **Gate-4** | V10-00c 完成：备份 cron 已安装且**恢复演练通过**（从备份还原到临时目录能 `count_draws()==3515`） | §2.3 | V10-00c |
| **Gate-5** | V10-00d 完成：迁移有批次事务、有自动备份、`schema_version` 对外可见 | §2.4 | V10-00d |
| **Gate-6** | V6–V9 的 7 个提交已推送 GitHub，**远端与本地 SHA 一致** | `git log --oneline -1 origin/main` == 本地 HEAD 的父提交；GitHub API 看到 `APP_VERSION==2.5.0` | 开工前置（不属 V10 任务） |
| **Gate-7** | 删除项（原 P0-1、P1-1、P1-2、P0-4 第 2 条、`LOTT_PREDICT_CHANNELS`）**已从方案文本移除** | 只允许出现在 §0.2 台账；`附录 C` 自查 grep 全过 | 已完成（本文件） |
| **Gate-8** | 登记簿落点已明确：**并入 `hypothesis.py` 加字段**，且 `patterns_runtime.py:104` 的 `usable` **真正读到**注册状态 | §3.3.5 的 5 条测试全绿，其中"造一条全字段齐备的记录 → `usable==True`"必须为真 | V10-02 |
| **Gate-9** | AI 岗位改为 R3 §5 的 5 条清单之一，且**每条写明机器判定方式** | §3.2：输入 schema、输出 schema、逐字段比对规则、"不做"与"做错"的区分 | V10-01 |
| **Gate-10** | 每项新统计功能必须带「注入偏置→必检出」+「纯随机→不误报」两条测试，且**验收命令一律全量 `pytest -q`** | §5.3/§5.4；禁止用单文件跑作为验收 | 全部任务 |

### 2.1 V10-00a　契约冻结（1 人日）

**文件**：`lottery/methods.py`、`lottery/config.py`、`lottery/migrations.py`（仅冻结不实现表之外的改动）、`docs/`
**产出**：规格 + 测试，**代码行为不变**（只有默认值变更这一处，见 2.1.3）。

#### 2.1.1 开关收敛表（现状 6 层 → 收敛后 1 个判据）

| 层 | 现状 | 收敛后职责 |
|---|---|---|
| `data/methods_config.json` | 设置页写入，import 期覆盖（`config.py:412`） | **优先级最高** |
| `.env` → `LOTT_METHODS` | 空=全开 / `-llm`=拒绝列表 / `stat,ml`=允许列表（`methods.py:6-13`） | 第二优先 |
| `.env` → `LOTT_METHOD_MODE` | production / research（research 时忽略开关，`methods.py:124-125`） | 并入判据返回值 |
| `.env` → `LOTT_LLM_ONLY_OUTPUT` | 1=只出 LLM 注（`config.py:81`，`engine.py:1715-1720`） | **降为该判据的子字段**，默认 `0`（见 2.1.3） |
| `.env` → `LOTT_LLM_DISABLED` | 全局禁用 LLM（strict 时 503） | 不动（**语义不同，不得复用**，见 §3.11） |
| 代码默认 | `METHODS_RAW` 缺省 = ""（全开） | **改为 `stat,ml,uniform`（allow-list）**，并附 `default_from="v10"` 便于审计 |
| ~~`LOTT_PREDICT_CHANNELS`~~ | v1.0 拟新增 | **不新增**（第 7 套开关；"LLM 不出号"用 `LOTT_METHODS=-llm` 早已能做到） |

#### 2.1.2 `resolve_channels()` —— 唯一判据

```python
# lottery/methods.py  （新函数，与 implement_spec/normalize_method/is_enabled/filter_candidates 并列）
def resolve_channels(*, force_production: bool = False) -> Dict:
    """本期出号通道的**唯一**判据。

    优先级链（硬性，写进 docstring 并测试）：
        1) data/methods_config.json （设置页写入，import 期已由 config 加载）
        2) .env: LOTT_METHODS / LOTT_METHOD_MODE / LOTT_LLM_ONLY_OUTPUT
        3) 代码默认: "stat,ml,uniform"，llm_output_mode="off"
    """
```

**⚠️ `source` 层必须显式判定，不能读 `os.environ`（S2 二次评审 B3 修复）**

`config.py:239` 与 `config.py:267` 会把 `methods_config.json` 的生效值**写回 `os.environ`**：

```python
# config.py（现状，勿改行为，但要用它的副作用）
os.environ["LOTT_METHODS"] = METHODS_RAW      # :239 (_apply_runtime_methods 写入时)
os.environ["LOTT_METHODS"] = METHODS_RAW      # :267 (_load_runtime_methods_config 启动时)
```

因此 `os.environ.get("LOTT_METHODS")` **永久无法区分**「来自 json」与「来自 .env」——
`source` 字段若照 `.get` 实现，**三层会被压成两层，T1/T3 的断言写不诚实**。

**正确判定方式（判据必须按这个顺序）**：

```python
def _resolve_layer() -> str:
    """判定生效开关来自哪一层。**不读 os.environ 判断来源**，而是查文件 + 查环境变量是否存在。"""
    if METHODS_CONFIG_FILE.exists():
        try:
            conf = json.loads(METHODS_CONFIG_FILE.read_text(encoding="utf-8"))
            if conf.get("raw") or conf.get("mode"):
                return "methods_config.json"      # 文件存在且有值 → 它就是生效层
        except (ValueError, OSError):
            pass
    # 关键：用 `in os.environ` 而不是 `.get(...) is not None`
    # —— .get 拿到的是被 config 写回的值，`in` 只能告诉"这个键有没有在 .env 里被设置过"，
    #    但由于上面已排除文件层，此处 `in` 为真即说明来源是 .env（进程环境）
    if "LOTT_METHODS" in os.environ or "LOTT_METHOD_MODE" in os.environ:
        return ".env"
    return "default"
```

并同步在 `config.py` 增加**只读来源标记**（不改任何行为）：

```python
METHODS_SOURCE: str = "default"      # "methods_config.json" | ".env" | "default"
# 在 _load_runtime_methods_config() 里 json 命中时置 "methods_config.json"
# 在模块级 METHODS_RAW 初始化后，若原 env 有值且 json 未命中则置 ".env"
```

> **实现者注意**：`METHODS_SOURCE` 必须是 **import 期一次性定值**，不得在 `resolve_channels()` 里现算后回写，
> 否则会重演"写回 env 导致来源污染"的同一类错误。
>
> **T1/T3 的前提**：测试必须用**临时 `LOTT_HOME`**（`monkeypatch.setenv("LOTT_HOME", tmp_path)`）构造干净的
> `methods_config.json`，**不能**假设"生产机器上没有设置页写过的配置"——在有配置的机器上 T1 会必红。

**返回值（冻结 schema，只增不改）**：

```python
{
  "spec_raw":  str,        # 生效的原始开关串
  "source":    "methods_config.json" | ".env" | "default",   # 命中哪一层；取自 config.METHODS_SOURCE（import 期定值），**不得现算**
  "mode":      "production" | "research",
  "families":  {"stat": bool, "blend": bool, "ml": bool, "uniform": bool, "llm": bool},
  "llm_output_mode": "off" | "only" | "mix",
  "llm_can_pick": bool,    # = families["llm"] and llm_output_mode != "off" and mode == "production"
  "research_arm": {"stat": bool, "llm_probe": bool},   # 只写 replay_results(mode='research_arm')，不写 predictions
  "fingerprint": str,      # sha1(canonical_json(上面除 fingerprint 外全部字段))[:16]
}
```

**硬性纪律（每条都要有测试）**：

1. **`engine.predict_next` / `battle` / `evaluate.offline_backtest` / `api_app` 一律不得直接读 `os.environ["LOTT_METHODS"]` 或 `config.LLM_ONLY_OUTPUT`**，只准读 `resolve_channels()`（或其冻结快照 `config.CHANNELS`）。静态闸门：`grep -rn "LOTT_METHODS\|LLM_ONLY_OUTPUT" lottery/ | grep -v "methods.py" | grep -v "^lottery/config.py"` 必须为空（tests 目录除外）。
2. `research` 模式：`families` 全 True（现状语义保持），但 `llm_can_pick` 恒 `False`（研究模式不参与生产出号）。
3. `llm_output_mode` 与 `families["llm"]` 的冲突：**拒绝列表优先**——若 spec 含 `-llm` 而 `LOTT_LLM_ONLY_OUTPUT=1`，则 `llm_output_mode` 强制为 `"off"` 并 `logger.warning` 一次（幂等，不报错）。
4. `fingerprint` 必须写进 `eval_meta.config_snapshot_json`（`db.py:456-470` 已有快照机制），使"这期是哪套管开关出的号"可审计。
5. `force_production=True` 供回放/测试绕过 research 模式使用。

**测试**（`tests/test_v10_channels.py` ≥ 10 例）：

| 例 | 断言 |
|---|---|
| T1 | 什么都不设（**临时 `LOTT_HOME`**，无 `methods_config.json`）→ `source=="default"`、`families=={stat:True, blend:**False**, ml:True, uniform:True, llm:False}`、`llm_output_mode=="off"`、`llm_can_pick is False`。<br>⚠️ **`blend` 必须断言为 False**：代码默认是 allow-list `"stat,ml,uniform"`，`blend` **不在**其中（`methods.py:23` 的 `FAMILIES` 里确实有 `blend` 族，但 allow-list 语义是"只启用列出的"，故为 False）。T1 若断言 `blend:True` 会与默认值互斥、必红。 |
| T2 | `LOTT_METHODS=-llm` → `source==".env"`、`llm:False`、default 与显式设置结果一致（幂等） |
| T3 | `methods_config.json` 写 `stat,ml` + `.env` 写 `stat` → `source=="methods_config.json"`，families 反映 json（优先级链） |
| T4 | `LOTT_LLM_ONLY_OUTPUT=1` + `LOTT_METHODS=-llm` → `llm_output_mode=="off"` + 有 warning 日志 |
| T5 | `LOTT_METHODS=-llm` + `LOTT_LLM_ONLY_OUTPUT=1` 与上一条结果**完全相同**（对称，互不依赖设置顺序） |
| T6 | `LOTT_METHOD_MODE=research` → `llm_can_pick is False`，但 `families["llm"] is True` |
| T7 | 同输入两次调用 `fingerprint` 相同；改任一输入 `fingerprint` 变（防止把 fingerprint 写成常量） |
| T8 | `force_production=True` 时 research 模式被覆盖 |
| T9 | 静态闸门：全仓 grep 无其它模块直读 `LOTT_METHODS`/`LLM_ONLY_OUTPUT`（用 `pathlib` 扫源码 + 白名单） |
| T10 | 默认值审计：`/api/info` 的 methods 状态含 `"default_from": "v10"` |

#### 2.1.3 `LOTT_LLM_ONLY_OUTPUT` 的处置（保留第 1 条，删除第 2 条）

- **保留并执行**：代码默认 `1 → 0`（`config.py:81`）。理由：这是"把最没用的通道设成主力"的直接修复，且有既有回归支持（R3 P0-4-1）。
- **删除**：原第 2 条"LLM 候选必须 `status=="passed"` 才能进候选池"（按设计死锁，该集合恒为空；见 §0.2、§3.9）。
- **兼容**：`LOTT_LLM_ONLY_OUTPUT` 保留为**别名**（`1→only`、`0→off`），不新增开关；`.env.example:44` 的注释行改为 `# LOTT_LLM_ONLY_OUTPUT=0`（默认已 0，保留为紧急回退开关）。
- **必须注意的两个坑（R1 P1-6，v1.0 全漏）**：
  1. `web/index.html:38` 的 `cfgLlm` **默认 checked** + `app.js:724-730` 的 `localStorage.cfgLlm` 会把后端默认值吃掉 → 前端必须改（V10-04b）。
  2. `docker-compose.yml:14-62` 的 `environment:` **没有透传** `LOTT_METHODS`/`LOTT_LLM_ONLY_OUTPUT` → 用户在 `.env` 里改**不生效**（`docker compose` 只透传列出的变量）。V10-12 必须补透传，否则"按文档改 `.env` 不生效"这个既有坑会被放大。

#### 2.1.4 `COVERAGE_OBJECTIVE` 别名表（值域变更的兼容策略）

```python
# lottery/config.py
COVERAGE_OBJECTIVE_ALIASES = {
    "expected_value": "ev_conserving",   # 旧默认名 → 新名（诚实改名，行为=只输出结构指标）
    "coverage":       "ev_conserving",   # U4 旧口径 → 并入（差异只在报告里体现）
    "ev_conserving":  "ev_conserving",
    "pmax_high":      "pmax_high",
    "custom_weight":  "custom_weight",
}
COVERAGE_OBJECTIVE_RAW = (os.environ.get("LOTT_COVERAGE_OBJECTIVE") or "").strip().lower()
COVERAGE_OBJECTIVE = COVERAGE_OBJECTIVE_ALIASES.get(COVERAGE_OBJECTIVE_RAW, "ev_conserving")
if COVERAGE_OBJECTIVE_RAW and COVERAGE_OBJECTIVE_RAW not in COVERAGE_OBJECTIVE_ALIASES:
    logger.warning("未知 COVERAGE_OBJECTIVE=%r，回落 ev_conserving", COVERAGE_OBJECTIVE_RAW)
```

- `p_any_blue` **不作为可选值**（`blue_coverage_plan` 已闭式最优，见 §3.4.3）。
- `COVERAGE_OBJECTIVE_RAW` 保留原值供审计：`eval_meta` 历史快照里的 `coverage_objective` 字段（`engine.py:1804`）因此可比对"当时写的是什么、实际生效是什么"。
- 测试：4 个旧值/新值映射 + 1 个非法值回落 + 1 个 `RAW` 保留断言（并入 `tests/test_v10_tickets.py` 或 channels 测试）。

#### 2.1.5 `migration #3` 编号与全部 DDL 冻结

**编号冻结**：`MIGRATIONS` 追加**第 3 号**，此后 V10 期间**不再新增第 4 号**。`tests/test_m4_5_engineering.py:11` 改为

```python
assert migrations.apply(conn) == migrations.MIGRATIONS[-1][0]   # 不要写 == 3
```

（原因：写死 `==3` 下个批次又得改；现状 `==2` 走正确迁移流程必然变红。）

**DDL 全文（冻结，实现时逐字照抄）**：

```sql
-- lottery/migrations.py: MIGRATIONS += [(3, "v10 replay results + llm audit", _ensure_v10_tables)]

CREATE TABLE IF NOT EXISTS replay_results (
  method        TEXT    NOT NULL,
  issue         INTEGER NOT NULL,
  n_tickets     INTEGER NOT NULL DEFAULT 0,
  red_hits      INTEGER NOT NULL DEFAULT 0,
  best_red_hit  INTEGER NOT NULL DEFAULT 0,
  blue_hit      INTEGER NOT NULL DEFAULT 0,
  prize         INTEGER NOT NULL DEFAULT 0,
  mode          TEXT    NOT NULL DEFAULT 'replay',   -- replay | research_arm
  channels_fp   TEXT    NOT NULL DEFAULT '',         -- resolve_channels().fingerprint
  created_at    REAL    NOT NULL,
  PRIMARY KEY (method, mode, issue)      -- 必须含 mode：replay 与 research_arm 不得互相覆盖
);
CREATE INDEX IF NOT EXISTS idx_replay_issue ON replay_results(issue);

CREATE TABLE IF NOT EXISTS llm_audit (
  audit_id     TEXT PRIMARY KEY,      -- sha1(post|run_id|ts_ns)
  run_id       TEXT NOT NULL,
  post         TEXT NOT NULL,         -- result_narrator | contract_checker
  model        TEXT NOT NULL DEFAULT '',
  input_json   TEXT NOT NULL DEFAULT '',
  output_json  TEXT NOT NULL DEFAULT '',
  verdict      TEXT NOT NULL,         -- pass | fail | degraded | invalid
  fail_fields  TEXT NOT NULL DEFAULT '[]',   -- JSON 数组，如 ["per_method[1].red_mean"]
  ts           REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_audit_post ON llm_audit(post, ts);
```

**写语义冻结（与项目现有习惯一致，`db.py:355,491,646` 均为 DELETE+INSERT 或 REPLACE）**：

| 表 | 写入语句 | 理由 |
|---|---|---|
| `replay_results` | `INSERT OR REPLACE` | 回放可重跑；（**`method`, `mode`, `issue`**）为主键。**必须含 `mode`**（S2 B4）：同一 method 会同时以 `mode="replay"`（离线回放）与 `mode="research_arm"`（LLM 研究对照臂）写入，若主键只有 `(method, issue)`，**两者会互相覆盖**（`INSERT OR REPLACE` 直接毁掉对方那行）。 |
| `llm_audit` | `INSERT OR IGNORE` | **仅追加**；审计判定历史不可被覆盖。`audit_id` 用 sha1 保证重试幂等 |

**forward-only 声明（硬约束）**：第 3 号迁移**只允许** `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX` / `ADD COLUMN`；**禁止** `DROP` / `RENAME` / 改列类型。原因：`db.get_conn()` 在首次连库时自动 `migrations.apply()`（`db.py:164-173`），容器启动前几秒就落地，**没有回滚路径**。

**v2.0 与裁决文本的两处差异（必须知情，不是笔误）**：

1. **`experiments` 表不建**。裁决 G4 选 (A)"并入 `hypothesis.py` 加字段"，因此第 3 号迁移里**没有** `experiments`（原 v1.0 §P0-2 的建表方案作废）。
2. **原 `llm_proposals` 表取消，改为 `llm_audit`**。理由：`llm_proposals` 是为已删除的原 P0-1（decision 排序）服务的；V10-01 的两岗位需要的是**判定记录**，不是"提案"。

若将来仍需独立实验表，只能新增第 4 号迁移，**不得改动第 3 号**。

#### 2.1.6 `predict_next` 的 `result` 新增字段清单（冻结，供 V10-10 前端并行开发）

**规则：只追加，不改语义**。旧 key 一个不动；新增 key 不得为 `None` 以外的缺失（前端可以按 optional 处理但必须有 key）。

```python
# engine.predict_next(...) 的 result 追加（按此顺序，便于 review）
"channels": {
    "source": "methods_config.json" | ".env" | "default",
    "families": {"stat": bool, "blend": bool, "ml": bool, "uniform": bool, "llm": bool},
    "llm_output_mode": "off" | "only" | "mix",
    "fingerprint": str,                       # 16 hex
},
"ticket_distribution": {                       # 仅 coverage 模式 / with_distribution=True 时出现
    "n": int,
    "sum_A": int,
    "A_bar": float,                            # 两两交集均值；中性值 12/11 ≈ 1.0909
    "redundancy": float,                       # == A_bar（必须注明中性值不是 0）
    "union_coverage": int,
    "var_T": float,                            # Var[Σ K_i]（可为 0，但非负）
    "n_sigma1_sq": float,                      # n · 0.753099174
    "var_ratio": float,                        # var_T / n_sigma1_sq；恒 ≥ 0（下界 0）
    "p_ge4_hist": float,                       # P(≥1 注 ≥4 红)
    "p_ge5_hist": float,
    "p_ge4_method": "exact_enum" | "iid_approx",
    "ev_ratio_sixth": 0.15625,                 # 仅六等奖，与 n 无关
    "ev_ratio_full": float,                    # 整注；必须与 prize_assumption 成对出现
    "prize_assumption": "v4_table_A" | "r2_table_B",
},
"prereg": {                                     # 来自 hypothesis 台账的只读汇总
    "usable": int, "preregistered": int, "provisional": int, "cumulative_n_tested": int,
},
"ai_audit": None | {
    "post": "result_narrator" | "contract_checker",
    "verdict": "pass" | "fail" | "degraded" | "invalid",
    "correct": None | bool,                     # degraded → None
    "ts": float,
},
```

**禁止**：`result` 里出现裸 `ev_ratio`（必须带 `_sixth`/`_full` 后缀）；出现无 `scope` 的 p 值；把 `ticket_distribution` 的 `p_ge4_hist` 直接写死成常数。

#### 2.1.7 文件作用域表（**重写版，v1.0 的表不成立**）

> v1.0 声称"01/02/03/04/05 完全独立、互不触碰同一文件、可 2 人并行 4 路"——**错的**（F3）。下表是按实际改动点核出的真实表。

| 文件 | 被哪些任务改 | 冲突性质 | 串行要求（硬） |
|---|---|---|---|
| `lottery/methods.py` | **00a**（新增 `resolve_channels`）、04a、11 | 同文件追加 | **00a → 04a → 11** |
| `lottery/config.py` | **00a**（默认值/别名表）、04a、11（汇率/关停）、**12**（`APP_VERSION:26-27`） | 相距 200+ 行，机械冲突 | 00a → 04a → 11 → 12 最后 |
| `lottery/engine.py` | **03**（`:1386-1497` 目标函数 + `ticket_distribution` + `result` 的 `ticket_distribution`）、**04b**（`:1712-1725` `llm_only` + `result` 的 `channels`） | **同一函数 `predict_next` 的上下游 + 同一个 `result` 字典** | **03 → 04b（不可并行）** |
| `lottery/migrations.py` | **02**（实现第 3 号迁移） | 唯一占号人 | 00a 冻结 DDL → **02 独占实现** |
| `lottery/db.py` | **02**（迁移挂接 + `schema_status` 接 health）、**05**（`replay_results` 读写） | 可 rebase | **02 → 05** |
| `lottery/api_app.py` | **02**（`/api/prereg/*`）、**10**（三卡聚合接口）、**11**（`/api/llm/audit/*` + 成本报表） | 三处独立路由，合并必然冲突一次 | 02 → 10 → 11 |
| `lottery/patterns_runtime.py` | **02**（`:104` `usable` 读注册 + `prereg` 子块 + `source=="llm"` 短路） | 独占 | 02 |
| `lottery/hypothesis.py` | **02**（加字段 + 时序校验 + 双口径 p） | 独占 | 02 |
| `lottery/llm_lab.py` | **01**（两岗位输出 schema / prompt 改造） | 独占 | 01 |
| `lottery/ai_audit.py` | **01**（新） | — | 01 |
| `lottery/llm_client.py` | **11**（解析 `usage` + 统一记账 + 关停开关） | 独占 | 11 |
| `lottery/battle.py` | **09**（独立对照臂） | 独占 | 09（依赖 05） |
| `lottery/evaluate.py` | **04b**（`:146-147` `int(mean)` 口径）、**05**（`offline_backtest(channels=...)`） | 可 rebase | 04b 与 05 之间需一次人工合并 |
| `lottery/regime.py` | **07**（边界锚定化 + 多重性口径） | 独占 | 07 |
| `lottery/ml_model.py` | **08**（`MODEL_SCHEMA_VERSION` + 缓存 key + 清理 265MB） | 独占 | 08 |
| `lottery/replay.py` | **05**（新） | — | 05 |
| `web/index.html` | **04b**（`cfgLlm` 默认）、**10**（三卡 + 三处 `?v=` + `verText`） | 同文件 | 04b → 10 |
| `web/static/app.js` | **04b**（`localStorage` 迁移）、**10**（三卡渲染） | 同文件 | 04b → 10 |
| `web/static/style.css` | **10** | — | 10 |
| `docker-compose.yml` | **12**（新开关透传，P2-4） | — | 12 |
| `tests/conftest.py` | **00b**（新，唯一创建者） | 唯一 | 00b 最先 |
| `tests/*.py`（存量 19 处） | **00b**（`DB_PATH` 改造） | 唯一 | 00b |
| `tests/test_v10_*.py` | 各任务新增，**文件名互不重叠** | 无 | — |
| `/etc/cron.d/ssq-backup`、`DEPLOY.md` | **00c**、12 | — | 00c → 12 |

**由此得到的真实并行度**：见 §4.2（**2 路**，不是 4 路）。

### 2.2 V10-00b　测试隔离 + 慢测试闸门（0.5 人日）— ✅ **已落地**（commit 8955d37；基线 480 → 485）

**背景（这是现状缺陷，不是新需求）**：

- `tests/conftest.py` **不存在**，git 历史里**从未存在**；也没有 `pytest.ini` / `pyproject.toml` / `testpaths` / 随机顺序插件。隔离完全靠每个用例自觉 `monkeypatch`。
- 仍有 **19 处裸赋值** `config.DB_PATH = tmp_path/...`（无 `monkeypatch`、无 `finally` 还原）：`grep -rn "config\.DB_PATH = " tests/*.py | grep -v "old = \|monkeypatch" | wc -l` → **19**。
- **改造前 480 全绿纯属字母序巧合**：泄漏源按字母序都排在 `test_v4_*` 之前，唯一受害用例是 `test_v4_honest_ui.py:239`。R4 复现：`pytest tests/test_m4_5_engineering.py tests/test_v4_honest_ui.py -q` → **1 failed (400 != 503)**；单独跑 → 17 passed。
- **对 V10 的直接威胁**：12 个新测试文件全命名 `test_v10_*`，**字母序排在 `test_v4_*` 之后**，正好落在泄漏源下游。R4 用探针复现：`test_m4_eval.py + test_v10_*` 同会话时 `count_draws()==0` 直接失败，而单独跑通过。**而 v1.0 给的验收命令恰好全是"单独跑单个文件"** → 会形成假绿灯，直到全量回归才炸。

**规格**：

1. 新建 `tests/conftest.py`：

```python
# tests/conftest.py
import os, pytest
from lottery import config

SLOW = os.environ.get("LOTT_SLOW_TESTS") == "1"

@pytest.fixture(autouse=True)
def _db_path_guard(monkeypatch):
    """每个用例前后快照/还原 config.DB_PATH，任何裸赋值都泄漏不到下一个用例。"""
    before = config.DB_PATH
    yield
    config.DB_PATH = before

@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """独立空库 + 迁移就位；新测试**必须**用它，不得读真实 data/ssq.db。"""
    p = tmp_path / "t.db"
    monkeypatch.setattr(config, "DB_PATH", p)
    from lottery import db, migrations
    conn = db.get_conn()
    assert migrations.apply(conn) == migrations.MIGRATIONS[-1][0]
    return p

def slow_scale(default: int, slow: int) -> int:
    """默认小规模；LOTT_SLOW_TESTS=1 才放大。"""
    return slow if SLOW else default

def pytest_configure(config_):
    config_.addinivalue_line("markers", "slow: 默认跳过，LOTT_SLOW_TESTS=1 时运行")
```

2. 19 处裸赋值全部改为 `monkeypatch.setattr(config, "DB_PATH", ...)`（或用上面的 fixture）。
3. 新测试**不得依赖真实 `data/ssq.db`**；需要真实数据的用例必须显式标注并在 `conftest` 白名单里。
4. **慢测试闸门 `LOTT_SLOW_TESTS`**（此前项目完全没有，R4 P1-2 实测 `grep -rn "SLOW\|SKIP_SLOW"` 无匹配）：默认规模 `≤ 2000` 次模拟、跨期端到端用例 `≤ 6 例`；10 万次 MC / 1000 序列只在 `LOTT_SLOW_TESTS=1` 下跑；单例默认耗时 `≤ 2s`，`> 5s` 必须 `@pytest.mark.slow`（现状已超标：`test_v3_u2_u3_u4.py::test_blue_coverage_no_duplicate_across_ui_ticket_counts` 单例 **27.61s**）。

**验收（必须全绿才过 Gate-3）**：

```bash
cd /root/DSH/应用/ssqyuce
.venv/bin/python -m pytest -q                       # ≥ 489 passed, 0 failed（这是真实闸门）
.venv/bin/python -m pytest tests/test_m4_eval.py tests/test_v4_honest_ui.py -q     # 0 failed（现状 1 failed）
.venv/bin/python -m pytest tests/test_m4_eval.py tests/test_v10_* -q               # 0 failed
grep -rn "config\.DB_PATH = " tests/*.py | grep -v monkeypatch | wc -l             # 0
```

### 2.3 V10-00c　备份安装 + 恢复演练（0.5 人日）— ✅ **已落地**（8955d37 + A3/A4 修复 5b7f36c）

**背景（F10）**：`/etc/cron.d/ssq-backup`、`/backup/`、`crontab -l | grep ssq`、`data/*.bak*` **四路实测全部不存在** → 生产库**零备份**。而 `data/` 共 **271MB**（`ml_models/` 265MB + 5 个 runtime json + `raw/`），v1.0 的"`cp data/ssq.db`"只备份 1.4MB。

**规格**：

1. `mkdir -p /backup`；安装 `/etc/cron.d/ssq-backup`（内容取 `DEPLOY.md:209`，`chmod 644`），覆盖 `data/` **全量**（含 `hypotheses.json` / `adaptive_state.json` / `llm_budget.json` / `patterns_runtime.json` / `llm_config.json` / `raw/`），**`ml_models/` 可排除**（可重训）。
2. `db.py` 增加 `backup_to(path)`：`sqlite3.Connection.backup()`（在线安全，无需停容器）+ `PRAGMA integrity_check`；暴露 `POST /api/admin/backup`（受 `config.API_TOKEN` 保护，`api_app.py:32-41` 中间件已覆盖 `/api/*`）。
3. **恢复演练（Gate-4 的判据，必须真跑）**：`tar -tzf` 列出内容应含上列 6 项；解压到 `/tmp/restore_test` 后用 python 读出 `count_draws() == 3515`。

**验收**：`ls /backup/*.tar.gz` 有文件；恢复演练命令输出 3515；`/etc/cron.d/ssq-backup` 存在。

### 2.4 V10-00d　迁移加固（0.5 人日，依赖 00c）— ✅ **已落地**（8955d37 + A1/A2 修复 5b7f36c）

**规格**：

1. `migrations.apply` 改为**整批一个事务**：`BEGIN` 包住 DDL+INSERT，异常 `ROLLBACK`（现状 `:52-53` 先执行 DDL 再 `INSERT` 版本号，**无事务** → 中途崩溃 = "表建了、版本没记"）。
2. 迁移前**自动备份**：若 `current < latest`，先 `conn.backup()` 到 `data/ssq.db.pre-v{latest}.bak`，日志与 `/api/health` 给出路径。
3. `/api/health` 暴露 `schema_version`（`api_app.py:65-77` 已有 `version` 字段位）；`migrations.status()` 已实现但**无人调用**（R1 P2-7），接上即可。
4. 迁移后自检：`PRAGMA integrity_check` + `SELECT COUNT(*) FROM draws` 与备份比对，不一致直接 `SystemExit`（宁可起不来，也不要在坏库上继续写）。
5. `DEPLOY.md` 写入顺序：**停调度（`LOTT_SCHEDULER=0` 或 `docker compose stop`）→ API 备份 → `up -d --build` → `/api/health` 的 `schema_version==3` → 恢复调度**。

**验收**：注入一个故意失败的迁移 → **必须整体回滚**（`schema_version` 不变、表不存在）；`/api/health` 返回 `schema_version`。

---
## 第三部分　改进方案（功能项，全部重写为可实现规格）

### 3.1 目标重述（先改口径，再改代码）

| 原来的说法 | 改为 | 判定标准 |
|---|---|---|
| 提高命中率 / 准确性 | **同注数下的组合结构健康度**：并集覆盖、`Ā`/冗余、奖级分布、EV 守恒 | 全部**解析**计算，不需要"规律" |
| 用 AI 找规律 | **AI 做可被确定性代码打分的核对与判定**（§3.2 两岗位） | 每个输出都有 ground truth、**允许误差 0** |
| 规律库越多越好 | **规律库的可信度**：`registered/provisional/passed/failed` + 前置注册双口径 | 台账必须**在查看结果之前**写入 |
| 覆盖优化 = 更赚钱 | **覆盖优化改变分布，不改变 EV** | `ev_ratio_full` 与票单结构无关（恒定），前端必须并排显示 |

### 3.2 V10-01　AI 的两个"可机器 100% 判定"岗位（2 人日）

> 依据：裁决 G2 + R3 §5。原 P0-1（让 LLM 给候选排序、算 `Kendall tau`、N=30 关停）**已删除**：那个设计测的是"能否猜出我们后端的 confidence 规则"，不是"能否预测开奖"；且 n=4 时零假设 `sd(tau)=0.49`，`correct_top1` 在 30 次时 Wilson CI = [0.095, 0.373] **已含随机基线**，方案自定的 N=30 关停判据几乎必然触发，会把"AI 无用"写成一个从未被正确测量的结论。

**首批只做 2 条岗位**（R3 §5 的 #1 与 #2）；#3–#5 留作 V10 之后。统一判定标准：**ground truth 由确定性代码给出、不需要统计功效、30 行样本即可判定，且"不做"与"做错"可区分。**

**新增文件**：`lottery/ai_audit.py`、`tests/test_v10_ai_audit.py`；**改**：`lottery/llm_lab.py`（只加"岗位输出 schema + 校验"，**不重写** `_normalize_claims`）
**共同铁律**：两岗位的输出**都不产生任何号码**、**都不写 `predictions`**、**都不进候选池/规律库**；只写 `llm_audit` 表 + 一个 `/api/llm/audit/latest` 只读接口。

#### 3.2.1 岗位 A：确定性结果的**自然语言核对员**（`post="result_narrator"`）

**它做什么**：把一张**已经算好的**逐行对照表，翻译成"每个 method 一行的结构化摘要"。这是**算术复述**，不是预测 —— 样本量不影响正确性，只影响表有多长。

**输入（`input_json`，冻结 schema）**：

```json
{"window": {"from_issue": 2026100, "to_issue": 2026118, "n_rows": 30},
 "rows": [{"method": "stat:freq", "issue": 2026100, "seq": 1,
           "red_hits": 2, "blue_hit": 0, "best_level": "无", "reward": 0}, ...]}
```

行来源：`data/ssq.db` 的 `eval_details`（主键 `(issue, method, seq)`，`db.py:131-144`）；**只读**，不重新跑预测。

**输出（`output_json`，冻结 schema）**：

```json
{"per_method": [{"method": "stat:freq", "n": 12, "red_mean": 1.0833,
                 "blue_rate": 0.1667, "best_level": "六等奖"}],
 "window": {"from_issue": 2026100, "to_issue": 2026118, "n_rows": 30},
 "notes_zh": "一句话中文说明，不含任何预测性表述"}
```

**机器判定（`ai_audit.judge_narrator(input, output)`，允许误差 0）**：

| 字段 | 复算来源 | 比对规则 |
|---|---|---|
| `per_method[*].method` 集合 | 金标准 | 集合**完全相等**（多一个/少一个 → invalid） |
| `n` | `len(rows_of_method)` | 整数严格相等 |
| `red_mean` | `round(sum(red_hits)/n, 4)` | `abs(got−exp) <= 0`（先按同一 `round(...,4)` 规整再比） |
| `blue_rate` | `round(sum(blue_hit)/n, 4)` | 同上 |
| `best_level` | 该 method 出现次数最多的奖级（并列取**最高**奖级；无中奖则 `"无"`） | 字符串严格相等（中文奖级名表**实际在 `lottery/evaluate.py:12` 的 `PRIZE_NAME`**，且一/二等奖带 `(≈*)` 后缀，比对时必须原样包含） |
| `window` | 输入原样回填 | 三个整数严格相等 |

复算统一走 `evaluate.aggregate()` 的口径（`evaluate.py:57-`），**不得**在 `ai_audit.py` 里另写一套均值逻辑（否则"金标准"第 2 个来源，判定失去意义）。

**"不做" vs "做错" 的区分（4 态，这是本岗位能成立的关键）**：

| verdict | 触发条件 | 计入正确率分母？ | UI 文案 |
|---|---|---|---|
| `degraded` | LLM 未配置 / 超时 / 返回非 JSON / 通道异常 | **否**（这是"没做"） | "本次未取得 AI 核对结果" |
| `invalid` | JSON 合法但 schema 不合规（缺字段、类型错、method 集合不一致、多余 key） | **是**（它声称做了） | "AI 输出不合规" |
| `fail` | schema 合规但任一字段与复算值不符 | **是** | "AI 核对错误：`per_method[1].red_mean`" |
| `pass` | 全部字段精确一致 | **是** | "AI 核对一致（0 误差）" |

正确率 = `pass / (pass + fail + invalid)`；`degraded` **单列**。**禁止**只报 pass 率（那会把"经常不做"伪装成"很少做错"）。

**测试怎么写**（`tests/test_v10_ai_audit.py`，注入桩，**默认不真调 LLM**）：

1. `golden_table()` 造 30 行固定输入（3 个 method × 10 期）→ 桩返回完美摘要 → `pass`。
2. 桩把某个 `red_mean` 的小数第 4 位 +0.0001 → `fail`，`fail_fields == ["per_method[1].red_mean"]`。
3. 桩漏掉一个 method → `invalid`（集合不一致），`fail_fields` 含 `"per_method"`。
4. 桩返回 `"抱歉我无法..."`（非 JSON）→ `degraded`，且**不进分母**（断言 `denominator == 之前的值`）。
5. 桩把 `n` 返回成字符串 `"12"` → `invalid`（类型严格）。
6. 桩把 `best_level` 写成"二等奖"（实际六等奖）→ `fail`。
7. **误差 0 断言**：把任意数字改 `1e-9` → `fail`（证明不是"近似比对"）。
8. **确定性**：同输入两次判定 → 同一 verdict（不依赖随机）。
9. 写库幂等：同 `(post, run_id, ts_ns)` 重试两次 → `llm_audit` 只有 1 行（`INSERT OR IGNORE`）。
10. `@pytest.mark.slow`（`LOTT_SLOW_TESTS=1` + 有 key 时）：真调 LLM 一次，只断言"不抛异常且 verdict ∈ 4 态"。

#### 3.2.2 岗位 B：**契约合规检查器**（`post="contract_checker"`）

**它做什么**：判定一批 `filters` 是否**全部落在维度表档位内**、参数是否合法。答案由 `discovery.FAMILY_BY_KEY` 唯一确定 —— 它是**纯判定**，不产生任何候选。

**输入**：

```json
{"items": [{"idx": 0, "key": "hot", "params": {"window": 50, "top": 8}, "target": "red"}, ...]}
```

`items` 来源：`llm_lab` 的候选输出（或历史 `llm_claims.json` 里的 filters），**只读快照**。

**输出**：

```json
{"items": [{"idx": 0, "ok": true,  "offending": []},
           {"idx": 1, "ok": false, "offending": ["window"],
            "reason_zh": "window=37 不在档位 {30,50,100}"}],
 "summary": {"n": 2, "n_ok": 1, "n_bad": 1}}
```

**金标准（`golden_ok`，单一确定性函数）**：

```python
# lottery/ai_audit.py
def golden_ok(key: str, params: dict) -> tuple[bool, list[str], str]:
    """金标准：**单一确定性函数**，与 LLM 输出逐条比对。
    """金标准 = 三项既有校验的合取（与生产同源，禁止另写一套档位表）：
         a) llm_lab._params_ok(key, params)               (lottery/llm_lab.py:325-332)
         b) evolution.discovery_params_ok({"key": key, "params": params})   # ← 注意签名：接收**单个 dict**，不是两个位置参数（lottery/evolution.py:545）
         c) key ∈ discovery.FAMILY_BY_KEY
      返回 (ok, offending_keys, reason_zh)。
      若 a/b 结论不一致 → 保守取 False，并返回 reason="gold_disagreement"（计入分歧计数器）。
    """
```

**机器判定**：

- 逐条 `output.items[i].ok == golden_ok(...)[0]`；一致率 `agree = matches / n_items`。
- **闸门**：`agree >= 0.99` → `pass`；否则 `fail`（`fail_fields` 列出不一致的 idx）。
- **可审计性**：凡 `ok == false` 的条目，`offending` **不得为空**（否则即使布尔值碰对也判 `invalid` —— 因为无法审计它错在哪）。
- `n_bad == 0` 且 `n_ok == 0`（空输入）→ `invalid`。
- 未知 key（不在 `FAMILY_BY_KEY`）→ 金标准 False，`reason` 必须是 `"unknown_key"`。

**测试怎么写**：

1. 用 `discovery.FAMILY_BY_KEY` **参数化全表**：每个 family 取 3 组参数（合法 / 越界 / 未知键），桩返回与金标准一致 → `pass`（这一例保证"全档位覆盖"）。
2. 桩对某个越界参数说 `ok=true` → `fail`，`fail_fields` 含该 idx。
3. 桩 `ok=false` 但 `offending=[]` → `invalid`（可审计性断言）。
4. 未知 key → 金标准 False；桩说 true → `fail`；桩说 false 但 reason 缺失 → `invalid`。
5. 10 条里错 1 条 → `agree=0.90 < 0.99` → `fail`（闸门边界）。
6. 10 条里全对 → `agree=1.0` → `pass`。
7. `a`/`b` 两项校验人为造分歧（monkeypatch）→ 金标准 False 且 reason 为 `gold_disagreement`。
8. **零副作用断言**：调用 `judge_contract` 前后 `predictions` / `hypotheses.json` 的 mtime 与行数不变；函数返回体里不含任何号码字段（防止后门）。
9. 空输入 → `invalid`；非列表输入 → `invalid`。
10. `degraded`：LLM 不可用 → `degraded` 且不进分母（与岗位 A 同规则）。

#### 3.2.3 两岗位共同的工程约束

1. **成本**：每岗位每次只发 **1 次** LLM 调用（`max_tokens ≤ 1200`），**不得**复用 `llm_lab` 的三轮流程（三轮实测单次可跑 3×180s，会把成本报表分母算错）。所有调用的 `usage` 必须记账（§3.11）。
2. **异步**：两岗位都跑在后台任务（复用 `_bg_run`，`api_app.py:1627`），`predict_next` **只读**最近一次判定（`result.ai_audit`），**绝不**在预测请求里等 LLM（`LLM_TOTAL_TIMEOUT=75s` 是 Cloudflare 100s 的余量，再加一轮必然突破）。
3. **不许静默降级**：不可用 → `degraded` + 原因；UI 必须显示"未取得"，不得显示成"AI 已核对"。
4. **UI 禁令（G3）**：在错误率（§3.2.1 的正确率 / §3.2.2 的一致率）**估计出来之前**，界面**不得**出现"AI 已核对并声明弃权"这类表述。abstain 原有的三项数字引用契约**已删除**（那三项本来就在 prompt 里，`llm_lab.py:147-153`，实测 LLM 已在复读 91/4.55/1.0 —— 是零信息量的抄写题）。
5. **明确排除**（R3 §5 末段，写进 `ai_audit.py` 的模块 docstring）：① 让 LLM 排序我们的候选；② 让 LLM 做因果归因；③ 让 LLM 出号并指望它"被显著性验证"；④ 让 LLM 在 30 期样本上"证明"自己有用或无用。

**验收**：`tests/test_v10_ai_audit.py` ≥ 16 例（A 10 + B 10 去重后），全绿且**不依赖网络**；`pytest -q` 全量 ≥ 480 + 新增。

---

### 3.3 V10-02　前置注册（**并入 `hypothesis.py` 加字段**，含 R2 六条约束）（2 人日）

> 依据：裁决 G4 选 **(A)**；R1 P0-2 建议 1；R2 §3.1 的 R1–R6。
> **核心命题**：**只建表/只加字段而不改 `usable` 判定 = P0-2 白做** —— 用户看到的仍然是 0 条可用规律。

#### 3.3.1 为什么并入而不是新建第 4 本登记簿

项目已有 3 本：`data/hypotheses.json`（`hypothesis.py:1257 register_hypotheses`，**已有**幂等去重 + 累加 `attempts`）、`:1443 resolve_registry`（**已有**状态机 `proposed→validated/rejected/insufficient`）、`data/llm_claims.json`、`data/patterns_runtime.json`。而 `patterns_runtime.build_snapshot`（`:78-175`）**只读前三者** —— 新建 SQLite 表后 `passed` 实验**不会**进入 `usable` 判定。

`_save_registry` 是纯 JSON 写盘，**加字段零迁移成本**（不需要第 3 号迁移）；且天然被 `usable` 读到。

#### 3.3.2 假说记录新增字段（冻结）

| 字段 | 类型 | 含义 | 约束 |
|---|---|---|---|
| `prereg_at` | float | 注册时刻（**先于**读结果） | 必填；`checked_at > prereg_at` 硬校验 |
| `exp_id` | str | `sha1(canonical(spec))`；spec = `{family_key, params, target, test, direction, min_samples}` —— **不含** `evaluate_after_issue` | 同一 spec 重注册返回同一 id；`exp_id` 与 `_dedup_key` 兼容（同规律不得在两处各占一条、`attempts` 分裂） |
| `evaluate_after_issue` | int | **独立字段**（预约判定的期号） | 改它只是"改预约"，**不产生新 exp_id**（封死"20 个截止期 = 20 批"的洗白路径） |
| `frozen_until_issue` | int | 冻结期结束期号 = `evaluate_after_issue + FROZEN_ISSUES` | `FROZEN_ISSUES = 150`（≈1 年） |
| `batch_id` | str | 批次标识 | `M_min=10`/`M_max=200`/速率 10/期 都按 batch 记账 |
| `n_tested_batch` | int | 批内检验数 `M_b` | 只用于展示与"批内 p 值" |
| `n_tested_cumulative` | int | **只增不减**的累计检验数 | **唯一校正分母** |
| `p_batch` | float | 批内 Bonferroni | 展示用，**不是**判定依据 |
| `p_cumulative` | float | 累计 Bonferroni | **判定依据** |
| `scope` | str | `"batch"` \| `"cumulative"` | 结论分歧时以 `cumulative` 为准并记录 `divergence=True` |
| `prereg_status` | str | `registered/running/passed/provisional/failed/invalidated` | 状态机单向（见 3.3.4） |
| `prereg_ok` | bool | **唯一给 `usable` 读的字段** | `= (prereg_status=="passed") and (scope=="cumulative") and (p_cumulative < alpha) and (checked_at > prereg_at)` |
| `checked_at` | float | 判定时刻 | 可 None |
| `divergence` | bool | **两套结论是否分歧**（`p_batch<α` 而 `p_cumulative≥α`，或反之） | **R1 必需字段**（S2 B5 补漏：v2.0 只在 `prereg` 子块里被读、从无字段定义）；供前端警示"批内看起来有效、累计口径不成立" |
| `registered_issue` | int | 注册时的期号（先于读结果的时间锚） | 与 `evaluate_after_issue` 独立；审计用 |
| `prereg_reason` | str | 判定结论的可读原因 | 写入 `rule.prereg.reason`；前端展示"为什么不可用" |
| `alpha` | float | 该次判定使用的显著性水平（默认 0.05） | 记录当时的 α，防"事后换 α" |

> **字段总数：16 个**（v2.0 原表 12 个 + `divergence` / `registered_issue` / `prereg_reason` / `alpha`）。
> S2 B5 指出原表**缺 `divergence`**——而 R1 约束要求写它，属于"约束引用了不存在的字段"，
> 实现者会卡在这里（要么自造字段名，要么 R1 落空）。

#### 3.3.3 R2 的六条约束（逐条落地，**缺任一条就不算前置注册**）

| # | 约束 | 落地位置 | 测试 |
|---|---|---|---|
| **R1** | **跨批次累计台账是唯一校正分母**：`n_tested_cumulative` 永久累计，**同时展示** `n_tested_batch`；两套校正后 p 都存；两者结论不同 → 以累计为准并 `divergence=True` | `hypothesis._registry_totals()`；`prereg.py` 逻辑放 `hypothesis.py` | 连续注册 10 批 × 每批 10 条**纯噪声** → 累计口径"发现"率 ≤ 5%（±MC 容差）；批内口径的发现率**必须被记录并展示**（用作警示） |
| **R2** | **每批 `M_min ≥ 10`**，凑不满 **并入下一批**，不得单独成批判定 | `register_hypotheses` 校验 `len(batch) >= MIN_BATCH`，否则 status 停在 `registered` 不进入判定 | 批大小 9 → 不产生 passed；并入后 10 → 正常判定。**规格必须写明 10 是"下限"不是"达标值"**（N=100 时：`M_min=1→FWER 0.994`、`10→0.401`、`50→0.098`、`100→0.050`，要让 FWER≤0.10 需 `B≤2` 即 `M_min≥50`） |
| **R3** | **截止期从 `exp_id` 的规范形式中剔除**，改独立字段 `evaluate_after_issue` | `_spec_canonical()` | 改截止期号 → `exp_id` **不变**（幂等返回同一 id） |
| **R4** | **冻结期 ≥ 1 个完整检验窗（≥150 期）**；冻结期内 `status` **不可从 `failed` 回到 `registered`** | `FROZEN_ISSUES=150` + 状态机断言 | 在冻结期内尝试 `failed→registered` → 抛错/拒绝；切断"看数据→改规格→再看"的 optional stopping 循环 |
| **R5** | **一次性批量上限 `M_max ≤ 200`** 且**注册速率 ≤ 10 条/期**，超出必须填 `reason` 字段 | `register_hypotheses` 入参校验 | 201 条 → 拒绝（或强制 reason）；单期第 11 条 → 拒绝 |
| **R6** | **所有 `passed` 必须再跑一遍累计口径校正并并存**；累计口径下不显著 → 状态必须是 **`provisional`** 而非 `passed`，且**禁止**进入 `usable` | `prereg_ok` 计算 | 造 `p_batch<α` 但 `p_cumulative≥α` → `provisional`，`prereg_ok=False` |

**最低可接受版本**：R1 + R3 是**正确性**要求（不写就不是 FWER 控制）；R2 + R4 是**防滥用**；R5 + R6 是**可用性**。

#### 3.3.4 状态机（单向，冻结）

```
registered ──(期号到达 evaluate_after_issue 且 batch 满 10)──> running
running ──(两套 p 都 < α)──> passed
running ──(p_batch < α 但 p_cumulative ≥ α)──> provisional      # R6
running ──(都不显著)──> failed
任何状态 ──(spec 违反档位/家族回放不可用)──> invalidated
failed/provisional/passed ──✗ 不得回退──> registered（冻结期内尤其禁止，R4）
```

`provisional` **可以**在更晚的复查中升级为 `passed`（预算允许时），但**不得**直接跳过 `pass` 进入 `usable`。

#### 3.3.5 **如何让 `patterns_runtime.py:104` 的 `usable` 真正读到注册状态**（本节是 P0-2 的成败点）

**现状**（`lottery/patterns_runtime.py:104`）：

```python
usable = (status in USABLE_STATUSES) and h.get("status") == "validated"
```

**改为**（三处改动，缺一不可）：

```python
# 改动 1：usable 增加注册条件（安全默认：缺失即 False）
usable = (
    (status in USABLE_STATUSES)
    and h.get("status") == "validated"
    and h.get("prereg_ok") is True                  # ← 唯一新增判据
    and h.get("source") != "llm"                    # ← 改动 2：死锁后门永久封死（R3 G1）
)
```

```python
# 改动 2（同上，防死锁复活）：source=="llm" 的假说永不 usable。
#   理由：hypothesis.py:1501-1507 要"有支持"才 validated、patterns_runtime.py:110 要 persistent、
#   battle.py:81 明确 llm 不可回放 → "llm 且 passed" 的集合恒为空。若不禁，将来有人补了
#   llm 的 validated 路径，死锁会从后门复活。
```

```python
# 改动 3：每条 rule 输出 prereg 子块，让前端看得见"为什么不可用"
rules.append({...,
  "prereg": {
      "status": h.get("prereg_status"),
      "exp_id": h.get("exp_id"),
      "scope": h.get("scope"),
      "p_batch": h.get("p_batch"),
      "p_cumulative": h.get("p_cumulative"),
      "n_tested_batch": h.get("n_tested_batch"),
      "n_tested_cumulative": h.get("n_tested_cumulative"),
      "divergence": bool(h.get("divergence")),
  }})
```

**同时**把 `prereg` 汇总写进 `predict_next` 的 `result.prereg`（§2.1.6），使"为什么 0 条 usable"在 API 层可解释。

**证明"没白做"的测试（Gate-8 的判据，必须全绿）** —— `tests/test_v10_registry.py`：

| # | 用例 | 断言 |
|---|---|---|
| U1 | 造一条 `regime.status="persistent"` + `status="validated"` + `prereg_ok=True` 的假说，`monkeypatch` 注入 `hypothesis.load_registry`，调**真实** `patterns_runtime.build_snapshot` | `rules[0]["usable"] is True` —— **这是"usable 首次可能为 True"的唯一反证；不写这例，P0-2 就没有完成证明** |
| U2 | 同上但删掉 `prereg_ok` 字段 | `usable is False`（安全默认） |
| U3 | 同上但 `prereg_status="provisional"` | `usable is False`（R6） |
| U4 | 同上但 `source="llm"`（其余全字段齐备） | `usable is False`（G1 死锁后门） |
| U5 | `build_snapshot` 返回的 rule 含 `prereg` 子块且字段完整 | 6 个 key 全在 |
| P1 | 改截止期号两次注册同一 spec | 返回同一 `exp_id`，`attempts` 累加（不是两条记录） |
| P2 | `checked_at <= prereg_at`（伪造时序） | 拒绝判定 |
| P3 | 冻结期内 `failed→registered` | 拒绝 |
| P4 | 批大小 9 | 不判定（并入下一批） |
| P5 | 201 条批量 / 单期第 11 条 | 拒绝（R5） |
| P6 | 10 批 × 10 条纯噪声 | 累计口径发现率 ≤ 5%（MC 容差内），批内口径发现率被记录 |

> **重要**：U1–U5 必须 mock **数据来源**（`hypothesis.load_registry`），**不得 mock `build_snapshot` 内部**，否则测的是 mock 不是接线。

#### 3.3.5b **写者规格：`evaluate_prereg()` 的签名、调用点与幂等语义**（S2 二次评审 B1 修复）

> **本节是 B1 的修复核心。** v2.0 的致命缺口：全仓 `grep prereg` 只在**读侧**（`patterns_runtime`）出现，
> **没有任何地方规定 `prereg_status` / `prereg_ok` 由谁计算、何时计算**。
> 后果：U1–U5 全部 `monkeypatch` 了 `hypothesis.load_registry`（即直接伪造一个 `prereg_ok=True` 的记录），
> 所以**即使实现者永远不会把 `prereg_ok` 置 True，U1–U5 与 P6 依然全绿** —— 这正是方案自己警告的"白做"。

##### 签名（冻结，实现在 `lottery/hypothesis.py`）

```python
def evaluate_prereg(*, registry_path: Optional[Any] = None,
                    issue: Optional[str] = None,
                    alpha: float = 0.05,
                    dry_run: bool = False) -> Dict:
    """对**已到期**的注册实验做判定，回写 prereg_* 字段并落盘。

    判定对象（三个条件同时满足才进入判定，否则原样跳过）：
      1. ``prereg_status == "registered"``（或 frozen 期后的 "running"）
      2. ``issue >= evaluate_after_issue``（到期）
      3. 所在 ``batch_id`` 的注册数 >= ``MIN_BATCH``（R2，不足则并入下批，不判定）
      4. ``issue >= frozen_until_issue``（R4 冻结期已过）

    对每条进入判定的实验：
      * 用 ``discovery.make_filter(family_key, params)`` + 严格 as-of 回放算效应与 p（**只用到 issue 为止的数据**）
      * ``p_batch``      = Bonferroni(p, m=n_tested_batch)
      * ``p_cumulative`` = Bonferroni(p, m=n_tested_cumulative)     # R1：累计口径是唯一判定依据
      * 两套都 < alpha            → prereg_status="passed"
      * p_batch < alpha 但 p_cumulative >= alpha → prereg_status="provisional"   # R6
      * 都不显著                  → prereg_status="failed"
      * 回放不可用/档位非法       → prereg_status="invalidated"
      * ``checked_at = time.time()``；``prereg_ok = (prereg_status=="passed"
            and scope=="cumulative" and p_cumulative < alpha and checked_at > prereg_at)``

    幂等语义（**必须**）：
      * 同一 ``exp_id`` 重复调用**不新增记录**，只更新 prereg_* 字段（以 ``exp_id`` 为键原地改写）；
      * ``checked_at`` 已存在且 ``prereg_status in {"passed","failed","invalidated"}`` 且
        ``issue < frozen_until_issue`` → **直接返回，不重判**（防 optional stopping，R4）；
      * ``provisional`` **允许**在后续调用中重新判定（可能升级为 passed），其余终态不可回退。

    dry_run=True：照算并返回判定结果，但**不写盘**（供 API 预览与测试）。

    返回：``{checked, skipped, passed, provisional, failed, invalidated, updated_ids, dry_run}``
    """
```

##### 调用点（**二选一，必须写死在代码里**）

| 选项 | 落点 | 触发时机 | 结论 |
|---|---|---|---|
| **(a) 调度器**（推荐） | `api_app._scheduler_step()` 里，紧跟 `_evolution_step()` 之后 | 每期开奖→抓取→在线对照→**evaluate_prereg**→生成预测 | ✅ 采用 (a) |
| (b) 快照刷新 | `patterns_runtime.refresh()` 开头 | 每次刷新快照前 | ❌ 不采用（刷新可由前端触发，频率不可控，会让"判定时刻"被用户操作影响） |

**写死为 (a)**。理由：判定必须是**每期一次**的确定性动作，且必须在 `predict_next` 读快照**之前**完成，
否则会出现"某期已到期但预测仍用旧 prereg_ok"的时序错误。

```python
# api_app._scheduler_step() 内，紧跟 _evolution_step() 之后（伪码，位置必须一致）
try:
    _prereg = hypothesis.evaluate_prereg(issue=issue)      # 到期实验判定 + 回写
except Exception as exc:                                    # noqa: BLE001
    log.warning("evaluate_prereg 失败（不影响当期预测）：%s", exc)
```

> **失败必须降级而非中断**：判定失败只影响 `usable` 条数，绝不阻断当期预测生成（沿用 V6 `_evolution_step` 的既有约定）。

##### 库根累计计数器（R1 的存储位置）

`data/hypotheses.json` 顶层新增（与 `version` / `hypotheses` 平级）：

```json
{
  "version": "v10-1",
  "updated_at": "...",
  "counters": {
    "n_tested_cumulative": 0,      // 只增不减；每次 evaluate_prereg 判定一条就 +1
    "last_issue": null,
    "batches": []                  // [{batch_id, size, issue, reason}]
  },
  "hypotheses": [...]
}
```

**`n_tested_cumulative` 的更新规则（唯一入口）**：只在 `evaluate_prereg` 内、**对每条真实进入判定的实验** `+1`；
`register_hypotheses` 注册时**不加**（注册 ≠ 检验过）。这是 R1 "唯一校正分母"的实现口径。

##### **证明"没白做"的 E2E 测试（替换掉不可靠的 U1 单点证明）**

U1–U5 保留（它们验证**读侧接线**正确），但**不足以**证明写者存在。必须新增两条：

| # | 用例 | 断言 | 为什么它能挡住"白做" |
|---|---|---|---|
| **E1（E2E 正例）** | **不 monkeypatch 任何 `lottery/` 内部**：① 用真实 `register_hypotheses` 注册一条可回放的假说（构造一个真实存在弱信号的合成 draws 序列）② 真实调 `evaluate_prereg(issue=..., dry_run=False)` ③ 真实调 `patterns_runtime.build_snapshot(draws)` | ① `registry["hypotheses"][i]["prereg_ok"] is True` ② `snapshot["rules"][j]["usable"] is True` ③ `snapshot["counts"]["usable"] >= 1` | 只有**写者真的被调用并真的写盘**才会通过。任何人把 `evaluate_prereg` 实现成 pass、或忘了接到调用点，E1 必红 |
| **E2（反向用例）** | 把 `_scheduler_step` 里的 `evaluate_prereg(...)` 调用**注释掉**（用 monkeypatch 替换为 `lambda **kw: {}`），其余完全不变 | E1 的同一条链路上 `usable is False`，且 `prereg_ok` 保持 `None` | 证明 E1 的绿**确实来自那个调用点**，而不是别处顺手写的 |
| **E3（幂等）** | 连续调 `evaluate_prereg` 三次 | `n_tested_cumulative` **只 +1**（不是 +3）；记录条数不变；`checked_at` 不被刷新 | 防"每跑一次就重复计数"——那会让累计分母虚高、校正过度 |

> **构造 E1 的合成信号**：不要依赖真实开奖（它没有信号）。给 `make_draws` 造一个
> 「某号码被系统性加权」的序列（例如 1 号在 60% 的期里出现），保证 as-of 回放能稳定检出 → `p_cumulative < alpha`。
> 这正是项目既有的「注入偏置必须检出」传统（见 `tests/test_v4_rigor.py:269`）。

#### 3.3.6 与既有 `attempts` 的关系

`register_hypotheses` 的 `attempts` 累加**保留**，并在 R1 口径下作为 `n_tested_cumulative` 的**下限**校验（`attempts ≥ 该 exp_id 的累计检验次数`），防止有人绕过台账直接判定。

**验收**：`tests/test_v10_registry.py` ≥ 16 例（U1–U5 + P1–P6 + 幂等/并发/状态机/字段校验）。

---

### 3.4 V10-03　票单分布（`engine.py`，**不是** `rigor.py`）（2 人日，含 holdout 0.5d）

> **落点变更**：`ticket_distribution` 放 **`engine.py`**（与 `expected_prize` / `portfolio_expected_value` 同文件，复用 `PRIZE_CASH_FIXED` / `JACKPOT_ASSUMPTION`，`engine.py:1199-1307`），**不要**放 `rigor.py` —— 否则制造 `battle.py:66 → engine → rigor → engine` 循环 import（`rigor.py:36-43` 刻意不 import engine）。

#### 3.4.1 真实缺陷（F4 更正后的诊断）

| 缺陷 | 证据 | 修法 |
|---|---|---|
| **目标函数蓝球项零梯度** | `engine.py:1492-1496`：选完红球后 `out[i]["blue"] = plan[i]` **强制改写**蓝球 → `_ticket_objective` 里算的 `distinct_blue`（`:1399`）与最终输出**毫无关系** | 蓝球**唯一**由 `blue_coverage_plan` 负责（闭式最优 `P(≥1蓝)=min(n,16)/16`），因此从目标函数里**删除蓝球项**，并显式返回 `gradient_degenerate=True` |
| **红球项是常数** | `portfolio_expected_value()["expected_reward"] = one × n`（`:1286`，`:1303` 也是 ×n 常数） | 同上：`ev_conserving` 模式**只输出结构指标，不做优化** |
| **死代码** | `:1409` `return float(ev) + cover_bonus * n / max(1, n)` —— `cover_bonus*n/max(1,n)` 恒等于 `cover_bonus` | **删除**该表达式（作者意图应是 `*n`；现在同义反复会被"可插拔目标"框架原样继承） |
| **SA 可能一次都不跑** | 候选池仅 8~10 注（`engine.py:1602` build_models 0.028s、`:1634` 每模型 ×2 注），`engine.py:1444` `len(pool) <= n_tickets` **可能恒真** → 提前返回。实测 `pool=400, trials=3000` 仅 **0.034s** | v1.0 的"SA 白跑 3000 次浪费 CPU"**前提不成立，已删除**。若确需提前退出，判据必须是 `_ticket_objective` 的**极差 < eps**（目标退化），不是拍一个迭代数 |

#### 3.4.2 `var_ratio` 的正确定义（F6 / R2 §3.4.3，**照抄，不要自行改系数**）

**定义写死**：`var_ratio = Var[Σ_i K_i] / (n · σ₁²)`，其中 `σ₁² = 729/968 = 0.753099174`（**不是** `σ₁²` 本身，否则 n=5 全同会算成 1.0 而不是 5.0）。

**解析式**（闭式，用这个算，**不要模拟**）：

```
Var[T]      = n·σ₁² + (27/88)·Σ_{i<j} A_ij − (81/242)·C(n,2)
Cov(K_i,K_j)= (27/176)·A_ij − 81/484
var_ratio   = 1 + (n−1)·(11/54)·( Ā − 12/11 ),      Ā = Σ_{i<j} A_ij / C(n,2)
```

> **系数是 11/54**。R2 §3.4.3 把它印成 `11/27`，是 **2 倍手误**（R2 给出的边界值与阈值本身正确）。自行推导可核：
> `(C(n,2)/(n·σ₁²))·(27/88) = (n−1)·(484/729)·(27/88) = (n−1)·(11/54)`；常数项同理得 `(n−1)·(11/54)·(12/11) = (n−1)·2/9`。

**阈值在 `Ā = 12/11 ≈ 1.0909`，不在 0。** 三个解析值当断言用：

| 票组（n=5） | `Σ A` | `Ā` | `Var[T]` | `var_ratio` |
|---|---:|---:|---:|---:|
| 5 注**完全相同** | 60 | 6 | 18.827479 | **5.000000** |
| 5 注**两两不相交** | 0 | 0 | 0.418388 | **0.111111** |
| `Ā = 12/11`（任意 n） | — | 1.0909 | — | **1.000000** |
| 4 注不相交 + 1 注重叠 1 号 | 6 | 0.6 | 2.259298 | 0.600 |

**两个必须写进规格的坑**：

1. **`var_ratio` 恒 ≥ 0，下界恰为 0**（S2 二次评审 B2 更正）。
   > **v2.0 原文错误**：曾写「`var_ratio` 可以为负（n=10、`Ā=0` → −3.000）」。三处错：
   > ① 算术应为 −1.000（`1 + 9·(11/54)·(0−12/11) = 1−9·(2/9) = −1.000`）；
   > ② `n=10、Ā=0` 需要 60 个**互不重复**的号，超过 33 个红球，**不可能构造**；
   > ③ −3.000 实际对应 n=19。
   >
   > **真实下界**：穷举 n=2..33 的可行 `Ā`，`var_ratio` 最小为 **0.0**（n=11、每号恰出现 2 次时 `var_T=0`），**永不小于 0**。
   > 因此**§3.4.7 原要求的"负值测试用例"不可构造，已删除**；改为断言下界 0。

   `var_ratio` 仍是"协方差结构的线性泛函"，所以**不能**被解读成普通方差比。规格要求：返回体**同时**给出 `var_T`、`n_sigma1_sq`（两个正数）与 `var_ratio`（派生量，`0 ≤ r`），UI 显示前两个并注明 `Ā` 的中性值是 12/11。
2. **`redundancy` 定义为 `Ā`**，且**必须注明中性值 = 12/11，不是 0**。
3. **测试必须用构造性票组**（相同/不相交/固定重叠），**禁止**用随机票组比较两个票组的 `var_ratio` —— 随机 10 注的 `Ā` 抽样分布是 `1.0935 ± 0.1306`（5000 次，5–95% 分位 `[0.889,1.333]`），对应 `var_ratio = 1.005 ± 0.239`、`P[ratio>1]=0.447`，**差异会被噪声完全淹没**。

#### 3.4.3 目标函数与蓝球的职责边界（冻结契约）

**契约（写进 `coverage_optimize` 的 docstring 与 `config.COVERAGE_OBJECTIVE` 注释）**：

> **蓝球由 `blue_coverage_plan` 唯一负责（闭式最优，不可被目标函数覆盖）；`objective` 只作用于红球子问题。**

- `objective` 收敛为 **3 个**（§2.1.4）：`ev_conserving`（诚实默认，只输出结构指标，**不做优化**）、`pmax_high`（红球目标：直接最大化 `P(≥1注≥4红)`，须同时报 `var_ratio`）、`custom_weight`（权重来自 P2-3 的用户偏好）。
- **删除 `p_any_blue`**：`plan` 已保证 `p_any_blue = min(n,16)/16` 解析最优，把它设成优化目标等于让 SA 追一个已由闭式解达成的量（SA 找到的一定 ≤ plan）。降级为**报告项**。
- 任何模式都**不允许**把目标写成"提高期望"。
- `blue_coverage_plan` 的**签名与返回值冻结**（`battle.py:180`、`evolution.py:26` 也在用；`tests/test_v4_coverage.py:134-136,200-231` 锁了数值）；任何新语义放新函数。

#### 3.4.4 `P(≥1注≥r红)` 怎么算（**代理目标与真目标方向相反**，必须直接优化真目标）

R2 用 `C(33,6)=1107568` 全枚举（**不是模拟**）实测：单注 `p₄ = 0.004900828`

| n | iid 票组 | 覆盖铺满（贪心最大化并集） | 独立上界 `1−(1−p₄)^n` |
|---:|---:|---:|---:|
| 5 | 0.024268 | 0.024504 | 0.024265 |
| 10 | **0.048150** | **0.046887（−2.6%）** | 0.047941 |

**结论（写进规格）**：`n` 越大，"铺开"越伤 `P(≥1注≥4红)`（两两不相交的票无法同时中奖，而"至少一注中奖"的并集事件**受益于票间正相关**）。因此 **`pmax_high` 必须直接以该概率为目标**，**不得**用"最小化冗余度"或"最大化并集覆盖"当代理。

**实现**：

- 精确值走全枚举（1.1M 项，< 1s，numpy 一次算完）→ **按票组 hash 缓存**；只在 `with_distribution_exact=True`（评估报告卡 / 测试）时启用，**不放进每期预测热路径**（预测路径返回 `p_ge4_method="iid_approx"` 并如实标注）。
- 并集/`Ā`/`var_ratio` 走解析式，O(n²)，可放热路径。

#### 3.4.5 EV 口径（F8，**命名统一，禁止裸 `ev_ratio`**）

| 常量 | 值 | 定义 | 必须带的假设 |
|---|---:|---|---|
| `ev_ratio_sixth` | **0.15625** | 仅六等奖：`(1/16 × ¥5) / ¥2`，**与注数 n 无关** | 无（蓝球均匀 1/16、六等奖 ¥5 固定） |
| `ev_ratio_full` | **≈ 0.45675**（默认口径） | 整注（一至六等奖全计）：单注 EV ≈ ¥0.9135 / 成本 ¥2.00 | **必须附奖金假设表 A**（V4 口径：`V4_ACCURACY_DIAGNOSIS.md:177-187`） |
| `ev_ratio_full`（替代口径） | ≈ 0.49448 | 整注，一等 650 万 / 二等 18 万 | **必须附奖金假设表 B**；`engine.expected_prize` 的实际输出为 ¥0.988960 |

**规格**：

1. 代码里**禁止**出现裸 `ev_ratio` 字段名（必须带 `_sixth` / `_full` 后缀）。静态闸门：`grep -rn "\bev_ratio\b" lottery/ web/static/app.js` 无匹配。
2. 报表/UI 必须**并排**显示 `ev_ratio_full` + `prize_assumption`（假设表名）+ 一句"六等奖只占整注 EV 的约 31.6%"（表 B 口径），**不得**把 0.15625 单独当作"EV 守恒"的唯一数字（会让用户以为整体返奖率只有 15.6%）。
3. **测试断言两个值**，且断言"换假设表必须导致 `ev_ratio_full` 变化"（防止被实现成硬编码常数）。

#### 3.4.6 holdout / 交叉拟合（**替代已删除的选择性推断**，0.5 人日，并入本任务）

**为什么不再做选择性推断**（§0.2 已删，这里给程序员的"不要再想"依据）：`truncnorm` 近似**有界离散**统计量（K∈0..6）的尾部单点相对误差 **26%~98%**（加连续性校正在 k=4→**43.9%**、k=5→**70.9%**），而选择性推断的全部工作**就发生在尾部** → 量级错误，不是精度问题。且方案没写清"选择"发生在单期轴还是候选轴，按字面实现必然算错；验收带「3.5%~6.5%」≈ 1 个 MC 噪声宽度，真 α=6.5% 时通过率正好 **0.500**，且**无 power 项**。

**替换规格（`holdout_eval`，放 `engine.py` 旁的 `evaluate.py` 或新 `lottery/holdout.py`）**：

```
holdout_split(n_periods, n_folds=2, block=1, seed=0)
  → [(select_idx, eval_idx), ...]     # 按**连续块**切（不打散），避免窗口统计泄漏
交叉拟合：
  for (S, E) in splits:
      best = argmax_{c in candidates} selection_stat(c | draws[S])     # 只在 S 上选
      p    = one_shot_bonferroni(pval(best | draws[E]), m=len(candidates))   # 在 E 上一次算完
  报告：所有方向 p 的**最保守者**（取 max）+ 各自 p，禁止只报一个方向
```

- **选择偏差结构性归零**（不是近似修正），无需任何密度近似，非统计背景也能复核。
- 与 V10-02 的"隔期之后才判定"语义一致。
- **必须报 MDE**：`rigor._min_detectable_effect(n)`，让读者知道这个 holdout 能检出多大的效应。
- **验收（Gate-10 传统，两条必须都有）**：(a) 注入效应 → 必须检出（`p < α`）；(b) 纯随机 → 不得误报（α=5% 的假阳性率在 MC 容差内；默认 2000 次，`LOTT_SLOW_TESTS=1` 时 5000 次）。

#### 3.4.7 前端「🧾 票单分布」卡（内容冻结，实现在 V10-10）

必须显示：`n` / 并集覆盖 / `Ā`（注明中性值 12/11）/ `redundancy` / `var_T` / `nσ₁²` / `var_ratio`（带"≥0、下界 0 在 n=11 每号 2 次时取到"的说明）/ `P(≥4红)` / `P(≥5红)` / `ev_ratio_sixth` / `ev_ratio_full` + `prize_assumption`。
**必须同时出现**"改变分布不改变 EV"提示条（与卡片同一视口）。

**验收**：`tests/test_v10_ticket_distribution.py` ≥ 20 例，其中必须有 3.4.2 的三个解析值断言（±1e-9）、`var_ratio` 为负的用例、`ev_ratio` 两值断言、换假设表变化断言、枚举 vs iid 的误差断言（`|exact − iid| / exact < 5%`，n=10 实测差 2.6%）。

---

### 3.5 V10-04　通道默认值修正 + 前端 + 报表口径（1.5 人日：**04a 0.5d / 04b 1.0d**）

> R1 P1-7：v1.0 把 `config.py` + `engine.predict_next` + 前端 + 测试塞进 1 人日并标"无依赖可并行"是错的。拆成 04a（真独立）与 04b（依赖 03）。

#### 3.5.1 V10-04a（0.5d，无 `engine.py` 改动，真正独立）

`lottery/methods.py` 新增 `resolve_channels()`；`lottery/config.py` 落默认值 `stat,ml,uniform`、别名表、`LOTT_LLM_ONLY_OUTPUT` 默认 0；`tests/test_v10_channels.py`（§2.1.2 的 T1–T10）。**验收**：全量 `pytest -q` 绿 + `resolve_channels` 10 例绿。

#### 3.5.2 V10-04b（1.0d，依赖 03 与 04a）

1. **`engine.predict_next` 收敛**（`:1563-1737`，175 行、7 个开关）：`use_llm` 等一律改读 `config.CHANNELS`（= `resolve_channels()` 冻结快照）；删除对 `config.LLM_ONLY_OUTPUT` 的直接判定（`:1715-1720`），改判 `channels["llm_can_pick"]`；`result.channels` 按 §2.1.6 输出。
2. **前端（R1 P2-1/P2-2，v1.0 全漏）**：`web/index.html:38` 的 `cfgLlm` 去掉 `checked`；`web/static/app.js:724-730` 的 `localStorage.cfgLlm` 加版本迁移（读到 `cfgVer < "v10"` → 重置为 `"0"` 并写新 `cfgVer`），否则老用户浏览器里的 `"1"` 会**吃掉**后端默认值（`/api/predict?use_llm=true` → `llm_required=True` → 503 或仍只出 LLM 注）。
3. **报表口径（R1 P1-4，v1.0 归因错）**：「分列」**已经天然成立** —— `eval_details` 本就按 method 逐行存（`db.py:131-144`，主键 `(issue,method,seq)`）。真正**混算**的是 `eval_results`：`evaluate.py:146-147` 把一期所有注的 `red_hits` 取 `int(np.mean(...))` 存一个标量（把 1.5 抹成 1）。修法：`eval_results` **加 `method` 列**（走第 3 号迁移之外？——**不加列**：改为弃用该表的均值聚合，改为按需从 `eval_details` 聚合，代码层修，不动 schema）；`online_check` 的 `int()` 截断改为保留浮点。
4. **研究臂（R1 P1-6，**必须做**，否则方法对比数据源断供）**：默认不再让 LLM 出号后，`eval_details` 里 `llm:*` 与 `stat:*` 的**同期配对行会消失**，`db.method_recommendations()`（要求同期同时存在两种方法）会永远停在 `insufficient_sample`。规格：`production` 出号之外，**每期离线**跑一次 `stat` 池 + 一次 `llm` 探针（配置可用时），结果**只写 `replay_results`（`mode='research_arm'`）**，**不写 `predictions`**。这就是 §3.6 的回放表，两件事串起来做。

**验收**：默认 `resolve_channels().llm_can_pick is False`；显式开启后行为与 04a 前一致（golden 对比）；`eval_details` 与 `replay_results(mode='research_arm')` 同期配对行数 > 0；前端实测勾选框默认未选、老 `localStorage` 被重置。

---

### 3.6 V10-05　离线回放分表（1.5 人日，依赖 00d、02）

**规格**（三处补全，R1 P0-3-4 / P2-3 / P1-5）：

1. `replay_results` 表按 §2.1.5 冻结 DDL 建（**主键 `(method, mode, issue)`** + `INSERT OR REPLACE`；`mode ∈ {"replay","research_arm"}`）。
2. 新文件 `lottery/replay.py`：`run_replay(draws, methods, channels=None, mode="replay")`。
   - **必须显式传通道**：`offline_backtest`（`evaluate.py:99-100`）当前调 `E.predict_next(history, persist=False)` 会**按 production 口径**算 → 加 `channels=` 形参，默认 `force_production=True` 的 `resolve_channels()`，并把 `channels_fp` 写入表。
   - 全部离线回放**必须**用 `draws[:t]`，阈值用**滚动窗口**（V6 已证明全样本阈值虚高 0.0308 红球）。
   - 与真实 `eval_details` **物理分表**，**禁止**相加。
3. 样本量复用 `rigor.min_sample_size`（`:1022`）与 `_min_detectable_effect`（`:641`）—— **不要**新写 `sample_size_needed()`（项目已有 3 个同义函数名，再加第 4 个会加剧混乱）。

**验收**：`tests/test_v10_replay.py` ≥ 10 例：注入偏置→必检出、纯随机→不误报、幂等（同 (method,issue) 重跑不增行）、`channels_fp` 写入、分表隔离（`eval_details` 行数不受影响）、`draws[:t]` 严格性（偷换全样本阈值 → 测试必须失败）。

---

### 3.7 V10-07　regime 阶段边界锚定化（1 人日，**替代已删除的 CUSUM**）

**为什么不做 CUSUM（§0.2 已删，这里是给程序员的"不要再想"依据）**：ARL0=500 的算术没错，但 δ=0.041（=3515 期 MDE）下**中位检出延迟 248 期 ≈ 1.65 年**，90% 分位 642 期；37 个维度同时监控需 `ARL0 = 18500` 期（≈123 年）。真实残差实测报警 109 次 vs 纯噪声期望 245 次（0.44×，因非 iid 自相关）→ **iid 标定的 h 在生产上系统性失效**（即使写出正确代码也会失败）。且与 `regime.py` 的 23 段（`era_block=150`）重复建设。

**三处低成本改进（R2 §3.3.4）**：

**(1) 阶段边界锚定化（修一个现存的静默缺陷，最重要）**

- 现状：`build_adaptive_eras`（`regime.py:139-154`）从**最新一期往过去**切 → **每加一期所有段边界都平移**，历史段的 p 值随之变化。用户在两次登录会看到"同一个 2023 年段"的 p 值不同 —— 这是**当下就存在的不可复现性**，比"没有 CUSUM"严重。
- 改为：`build_anchored_eras(draws, block=150)` 从**第一期**开始每 150 期切一段，**最后一段可短**；每段记录 `anchor_issue`（该段第一期期号）与 `era_schema=2`。
- **硬断言（必须写成测试）**：在 `draws[:N]` 上算出的每一段，与在 `draws[:N+k]`（任意 k>0）上算出的**同一 `anchor_issue` 段**，其 `start/end/effect/p` **完全一致**（字符串或浮点 ==）。这条既是验收，也是"当前缺陷"的反向用例（对旧 `adaptive` 实现跑同一条断言**必须失败**）。
- 兼容：`era_mode="adaptive"` 的历史快照保留只读，其 p 值标记 `era_schema=1`，**不得**与新值并列展示（否则用户会看到两个不同的 p）。

**(2) 23 段 × 20 族的联合多重性口径**：`regime.py:544` 只把"段内 p + 近期窗口 p"一起 BH，**没把"搜了多少个维度"算进去**。改为在 `regime_report` 返回里同时给出 `m_adj_total = 段数 × 维度数`，UI 并列显示。

**(3) UI 表达换掉"失效时点 + 置信区间"**：MDE 量级下无可靠时点，**不要**给这个视觉锚点。改为"最近 k 段的效应量 ± CI"的轨迹 + 明确的无结论声明。

**验收**：`tests/test_v10_regime_anchor.py` ≥ 8 例：锚定可复现（上面那条硬断言）、旧实现同断言失败（反向用例）、`m_adj_total` 正确、`era_schema` 标记、最后一短段处理、段数与 `eras_for` 一致（23 段 / 150 期 / 最老段 215 期）。

---

### 3.8 V10-08　ML 校准 + 置换重要性 + 模型版本（1.5 人日）

- 可靠性图 + Brier 分解（reliability / resolution / uncertainty）；
- **置换重要性 vs 打乱标签的模型**：若真实模型的重要性不显著高于打乱标签模型 → ML 通道降级为 `monitor`；
- **`MODEL_SCHEMA_VERSION`（R1 P2-5，v1.0 漏）**：`ml_model.py:384-387` 的 `_cache_key = f"{len}:{issue}"` 不含特征版本 → 改特征后会**静默复用 265MB 旧 joblib**。`_cache_key` 加入 `MODEL_SCHEMA_VERSION`，落地时升版本号并清空 `data/ml_models/`。
- **磁盘清理写进可执行验收**（不只写文档）：`ls data/ml_models/*.joblib | wc -l` ≤ 2。

**验收**：`tests/test_v10_ml_calibration.py` ≥ 10 例：打乱标签必须让 importance 不显著、可靠性曲线在噪声带内、`_cache_key` 随 `MODEL_SCHEMA_VERSION` 变化。

---

### 3.9 V10-09　LLM 出号的**独立 A/B 对照臂**（1 人日，依赖 05）

> 原 P0-4 第 2 条"LLM 候选须 `passed` 才能进候选池"**已删除**（死锁）。这里是替代设计。

**规格**：

1. **定位**：`method="llm:<model>"` 与 `stat:*`/`ml` 并列出现在候选池与报表里，但它是**研究臂**：`resolve_channels().llm_can_pick` 默认 `False`（默认不出号），开启后也只是"参与对照"，**永不进规律库/候选池的规则层**。
2. **对照口径（不能沿用回放，`battle.py:81` 明确 `llm` 不可回放）**：同期、同 `n_tickets`、**逐注配对** `eval_details`；检验用 `rigor.strategy_significance` 的**配对序列模式** + `n_trials` 校正（注意：`n_trials` 语义是"搜了多少策略"，**不要**把它当成"配对数"）。
3. **结论分级（必须允许"永远 eligible 且默认关"——在真随机世界里这是唯一诚实的稳态）**：
   - `eligible`：默认状态，**允许长期停留**，不设"必须升级"的期限；
   - `not_worse`：配对检验显示不劣于基线（单侧非劣性；margin 由 MDE 决定）；
   - `insufficient`：样本不足（`n<120` 期时**只能**给这个，禁止下结论）；
   - `worse`：显著劣于基线 → 自动关（`llm_output_mode="off"`）。
4. **永不进规律库（防死锁后门）**：`source=="llm"` 的假说 `usable` 恒 `False`（§3.3.5 改动 2），并有测试 U4 守着。

**验收**：`tests/test_v10_llm_arm.py` ≥ 8 例：`experiments/假说` 全 `failed` 时 LLM 通道在 `eligible` 下**仍能出号**（证明无死锁）；`source=="llm"` 不产出 usable 规则；配对样本不足 → `insufficient`；劣于基线 → 自动关且 **预测接口仍成功**（只是不含该通道）。

---

### 3.10 V10-10　前端三卡（2 人日，等 01/02/03/07 契约冻结）

三卡：🧾 票单分布 / 📋 实验登记簿 / 🤖 AI 审计。
**必须**：`web/index.html` **三处 `?v=` 全部统一**（`style.css` 第 7 行、`app.js` 第 586 行、**`vendor/echarts.min.js` 第 10 行**）—— 当前 2 个令牌（`20261018.3` ×2、`20260817.7` ×1），`lottery/cli.py:127-132` 的 `doctor` 已因"令牌数≠1"报 **WARN**；新令牌**严格大于** `20261018.3`；`app.js` 与 `style.css` 必须**字面相等**（`test_v4_honest_ui.py:151` 断言）；`verText` 徽标改 `v3.0.0` 且 footer 去掉"V4~V8"。
**禁止**新增 echarts `import`/`require`（vendored 全局，用 `echartsInit(id)`，`app.js:1325` 内部已处理 `undefined` 降级）。

**验收**：`node --check web/static/app.js` OK；`python -m lottery.cli doctor | grep 缓存号` → 一致；三卡浏览器实测；"改变分布不改变 EV"提示条与分布卡**同时**出现。

---

### 3.11 V10-11　成本口径 + 自动关停（1 人日，依赖 01）

1. **`data["usage"]` 当前完全没读**（`llm_client.py:241-251`）→ 必须解析 `usage.prompt_tokens / completion_tokens`；`chat()` 的 **4 个出口**（成功/重试/超时/异常）**统一记账**。
2. `data/llm_cost_log.jsonl`：`{ts, run_id, purpose, model, prompt_tokens, completion_tokens, retries, timeout, usd, yuan}`。
3. **汇率口径必须写死并展示**：`config.LLM_EVAL_PRICE_PER_1M` 的语义是 **USD/展示用**，新成本日志用"元" → 新增 `config.LLM_CNY_PER_USD`（默认 7.2，常量），报表标注"按 ¥7.2/$ 折算"。
4. **关停落点（R3 G6）**：写 `data/llm_config.json` 的 `audit_disabled` / `decision_disabled` 字段，由 `_evolution_step` 执行；**不得**复用 `LLM_DISABLED`（否则 strict 模式抛错 → Web 预测 **503**）；**必须**接 `resolve_channels()`，**不许**写 `os.environ[...]="0"`（重启即失效，且与 `methods_config.json` 优先级冲突）；提供独立恢复入口 + UI。
5. 报表分母必须含 `llm_lab` 三轮（单次可跑 3×180s，`llm_lab.py:228-234`）与 `_scheduler_step` 的幂等键（`api_app.py:1141-1146` 重复执行成本泄漏）。

**验收**：`tests/test_v10_cost.py` ≥ 8 例：`usage` 被读、4 个出口都记账、`audit_disabled=true` 时**预测仍成功**（不含该通道）、重启后关停状态保持（读 json 而非 env）、幂等键不重复计费。

---

### 3.12 V10-12　文档 + 版本 + 部署 + 推送（1 人日，全部依赖）

**版本**：`APP_VERSION="3.0.0"`、`APP_BUILD="2026-10-V10"`、`APP_MILESTONES` 追加 V10。
**文档（7 处文件逐条打勾，清单见 R4 §5.D）**：`config.py:26-28`、`web/index.html:7/10/586/571`、`README.md:18` + 版本记录、`DEPLOY.md:81/104`、`docs/PROJECT_CONTEXT.md:9-11/40/231/262`、`.env.example:44`、`docker-compose.yml`（新开关透传）。
**必须先补齐 V8/V9 的文档漂移**（`README.md:18` 说 v2.4.0/V8、`DEPLOY.md:81` 说 2.4.0/V8、`PROJECT_CONTEXT.md:9-11` 说 v2.2.0/V6、`:231` 说 381 passed）—— 否则 V10 文档会在已错两版的基线上继续写。
**推送**：V10 前先推 V6–V9 的 **7 个提交**（6525 行），保留 bisect 能力；V10 交付后再推 V10 提交，逐步核验 SHA。
**部署与回滚（R4 §5.E，最薄弱环节）**：
```
部署前：tar -czf /backup/ssq_pre_v10_$(date +%F).tar.gz -C <repo> data
       sqlite3 data/ssq.db "select max(version) from schema_version"   # 期望 2
       docker image tag ssq-predictor:latest ssq-predictor:pre-v10
部署： docker compose up -d --build → 轮询 health（2~2.5 分钟 starting 属正常）→ /api/info 返回 3.0.0
回滚： docker compose down → cp /tmp/ssq_pre_v10.db data/ssq.db
       → docker image tag ssq-predictor:pre-v10 ssq-predictor:latest → docker compose up -d（不 --build）
```
**必须写进任务的事实**：**回滚会一并丢失 V10 期间写入 `replay_results` / `llm_audit` 的数据**（都是新数据，可接受），但执行者不能以为"回滚能保住 V10 数据"。
**容器冒烟**：`docker exec ssq-predictor python -c "import lottery.ai_audit, lottery.replay, lottery.regime; print('ok')"`（本地 venv 是 **3.13.5**，容器是 **3.11.17**，本地绿不证明容器可用）。

---

### 3.13 P2（长期 / 可选，**不进 V10 排期**）

| 编号 | 内容 | 判断 |
|---|---|---|
| P2-1 | DSP / 符号回归自动搜索（PySR） | 搜索空间更大，在独立随机数据上**只会产生更多假阳性**。**只能**在 V10-02 前置注册下使用。作为"证明系统诚实"的展示项，**不作为产品能力**。 |
| ~~P2-2~~ | ~~联邦/多源数据~~ | **不做**（§0.2）。双色球 3515 期已完整；更多数据只能把 MDE 降到约 0.03，仍远大于 0。 |
| P2-3 | 用户个性化目标函数（重 EV / 重小奖 / 重高效奖） | 数学上等价于在 (EV, Var) 平面上选点。**推荐**：正好承接 §3.4.3 的 `custom_weight`。 |
| P2-4 | 反事实模拟器 | 纯可视化、成本低、用户感知明显，**可提前到 P1**（不依赖任何统计功效）。 |

---

## 第四部分　任务表（重写，可直接排期）

### 4.1 任务清单（裁决 §五 重写版）

> 文件列的"互不重叠"是**目标**不是事实；真实冲突见 §2.1.7，排序以 §4.3 为准。

| ID | 标题 | 主文件 | 依赖 | 估时 | 变化 |
|---|---|---|---|---|---|
| **V10-00a** | 契约冻结（`resolve_channels` + 别名表 + migration #3 DDL + `result` 字段 + 作用域表） | `lottery/methods.py`、`lottery/config.py`、`docs/` | — | **1.0d** | **新增** |
| **V10-00b** | 测试隔离（`tests/conftest.py` + 19 处 `DB_PATH`）+ 慢测试闸门 `LOTT_SLOW_TESTS` | `tests/conftest.py`(新)、`tests/*.py` | — | **0.5d** | **新增** |
| **V10-00c** | 备份安装 + 恢复演练 | `/etc/cron.d/ssq-backup`、`DEPLOY.md`、`lottery/db.py`(backup_to) | — | **0.5d** | **新增** |
| **V10-00d** | 迁移加固（批次事务 + 自动备份 + `schema_version` 可见） | `lottery/migrations.py`、`lottery/db.py`、`lottery/api_app.py`(health) | 00c | **0.5d** | **新增** |
| **V10-01** | AI 两岗位：自然语言核对员 + 契约合规检查器 | `lottery/ai_audit.py`(新)、`lottery/llm_lab.py`、`tests/test_v10_ai_audit.py`(新) | 00a | **2.0d** | **重写**（原 P0-1 废弃） |
| **V10-02** | 前置注册（并入 `hypothesis` + R1–R6 六约束 + **改 `usable` 接线**） | `lottery/hypothesis.py`、`lottery/patterns_runtime.py:104`、`lottery/migrations.py`(#3 实现) | 00a/00d | **2.0d** | **改落点 + 加约束** |
| **V10-03** | 票单分布更正版 + holdout（`engine.py`，非 `rigor.py`） | `lottery/engine.py`(`:1409`、`:1492-1496`、`_ticket_objective`)、`tests/test_v10_ticket_distribution.py`(新) | 00a | **2.0d**（含 holdout 0.5d） | **诊断更正** |
| **V10-04** | 通道默认值 + 前端 + 报表口径 | **04a** `lottery/methods.py`+`config.py`+`tests/test_v10_channels.py`；**04b** `lottery/engine.py`(`:1716`)+`evaluate.py:146-147`+`web/index.html:38`+`web/static/app.js` | 04a←00a；04b←03、04a | **1.5d**（0.5+1.0） | **拆 04a/04b** |
| **V10-05** | 离线回放分表（`PRIMARY KEY (method,issue)` + `INSERT OR REPLACE` + 显式传通道 + 研究臂） | `lottery/replay.py`(新)、`lottery/evaluate.py`、`lottery/db.py`、`tests/test_v10_replay.py`(新) | 00d、02 | **1.5d** | **补三处** |
| **V10-06** | ~~选择性推断~~ → **holdout / 交叉拟合**（已并入 V10-03） | —（并入 03） | — | **含于 03** | **删除→替换** |
| **V10-07** | ~~CUSUM~~ → **regime 边界锚定化 + 多重性口径 + UI 去锚点** | `lottery/regime.py`、`web/static/app.js`(轨迹) | 00a | **1.0d** | **删除→替换** |
| **V10-08** | ML 校准 + 置换重要性 + `MODEL_SCHEMA_VERSION` + 清 265MB 旧 joblib | `lottery/ml_model.py`、`data/ml_models/`、`tests/test_v10_ml_calibration.py`(新) | — | **1.5d** | 补 cache key 缺陷 |
| **V10-09** | LLM 出号**独立对照臂**（非规律库） | `lottery/battle.py`、`tests/test_v10_llm_arm.py`(新) | 05 | **1.0d** | **重写**（原 P1-4 降级为 §3.2 岗位 A） |
| **V10-10** | 前端三卡（票单分布 / 实验登记簿 / AI 审计） | `web/*`（**升 `?v=`，当前 20261018.3**） | 01/02/03/07 | **2.0d** | 等契约冻结 |
| **V10-11** | 成本口径（读 `data["usage"]`，当前**完全没读**）+ 关停接 `resolve_channels()` | `lottery/llm_client.py:241-251`、`lottery/config.py`、`tests/test_v10_cost.py`(新) | 01 | **1.0d** | 改落点 |
| **V10-12** | 文档 + 版本 + 部署 + 推送 7 个提交 | `README.md`、`DEPLOY.md`、`docs/PROJECT_CONTEXT.md`、`config.py:26-29`、`docker-compose.yml` | 全部 | **1.0d** | 补推送 + 补 V8/V9 漂移 |

**总量：19.0 人日**

> **与裁决 18.5 人日的差异（+0.5，知情项）**：裁决 §五把 V10-04 记 1.0d，但其自身要求"拆 04a/04b"；按 R1 P1-7 拆开后为 0.5 + 1.0 = **1.5d**。其余各行与裁决一致。
> 对比 v1.0 的约 14 人日：**删掉 3.5 人日的不可用项**（选择性推断、CUSUM、LLM 决策排序）、**新增 2.5 人日前置 Gate**、**2 人日重写**（V10-01/V10-09）、**0.5 人日测试隔离**。

### 4.2 真实并行度：**2 路**（不是 v1.0 声称的 4 路）

| 冲突 | 事实 | 后果 |
|---|---|---|
| `lottery/api_app.py` | 被 **02 / 10 / 11 三方**改（3 个独立路由，都在文件末尾追加，合并必然冲突一次） | 串行合并 |
| `lottery/engine.py` | **03 与 04b 改同一个 `predict_next` 的上下游**（03 改 `:1386-1497` 目标函数与 `result.ticket_distribution`；04b 改 `:1712-1725` `llm_only` 与 `result.channels`）。04b 加的通道过滤会改变 `scored`/`picked` 的内容，而那正是 03 优化的输入 | **硬顺序 03 → 04b** |
| `lottery/{migrations,db}.py` | **02 与 05 争 migration #3**（都新增表/迁移） | 00a 冻结 DDL → **02 独占实现** → 05 只读表 |
| `web/index.html`、`web/static/app.js` | 04b 与 10 同文件 | 04b → 10 |
| `lottery/config.py` | 00a/04a/11/12 | 12 最后 |

**结论：同一时刻最多 2 个人有活干（一条 db/登记簿链 + 一条 engine/前端链），其余任务各自夹在这两条链的空隙里。** 排期按 2 路估：
**19 人日 ÷ 2 ≈ 10 个工作日**（含验证与合并冲突）。

### 4.3 合并顺序（按 R1 P1-2 的骨架 + 本方案展开）

```
(0) 00a 契约冻结（DDL 冻结 + resolve_channels 规格）           ← 单人对，先做完
(1) 00b（独立） / 00c（独立） / 00d（依赖 00c）                  ← 可与 00a 并行，最多 2 路
(2) 04a（config+methods，无 engine）  ‖  01（ai_audit/llm_lab）  ← 2 路
(3) 02（hypothesis 字段 + patterns_runtime:104 + migration#3 实现）  ← 独占 db/migrations
(4) 03（engine 目标函数 + ticket_distribution + holdout）        ← 独占 engine :1386-1497
(5) 04b（engine.predict_next + 前端 cfgLlm + evaluate 口径 + 研究臂）  ← 依赖 03、04a
(6) 05（replay.py + evaluate 回放 + db 读写）                     ← 依赖 02、00d
(7) 07（regime） / 08（ml_model）                                 ← 可与 (5)(6) 并行
(8) 09（battle 对照臂）                                          ← 依赖 05
(9) 10（web 三卡 + 三处 ?v=）  ‖  11（llm_client 成本/关停）      ← 2 路
(10) 12（文档 + 版本 + 部署 + 推送）
```

### 4.4 里程碑

| 里程碑 | 内容 | 交付判据 |
|---|---|---|
| **M0（2.5d）** | 00a / 00b / 00c / 00d | **Gate-1..Gate-5 全绿**；全量 `pytest -q` ≥ 480 且 `test_m4_eval` + 任一 `test_v10_*` 同跑 0 failed；恢复演练 3515；故意失败的迁移整体回滚 |
| **M1（4.5d）** | 04a / 01 / 02 / 03 | ① `resolve_channels()` 10 例绿 ② AI 两岗位 4 态判定 + 正确率/一致率可算 ③ **`usable` 能读到 `prereg_ok`（U1 为真）** ④ 三个 `var_ratio` 解析值 ±1e-9 |
| **M2（5d）** | 04b / 05 / 07 / 08 | ① 默认不由 LLM 出号且前端默认未选 ② 研究臂配对样本不断供 ③ 回放分表隔离 + 幂等 ④ regime 锚定后可复现（旧实现反向用例失败）⑤ ML 打乱标签不显著 + `_cache_key` 随版本变 |
| **M3（3d）** | 09 / 10 / 11 | 对照臂无死锁；三卡实测；关停后预测仍成功；成本含三轮 LLM |
| **M4（1d）** | 12 | 全量 `pytest -q` 全绿（≥480+新增）、容器 healthy、`/api/info` = 3.0.0/2026-10-V10、文档 7 处 + 推送核验 |

---

## 第五部分　统一工程规约（不可违反）

### 5.1 诚实边界（沿用 `docs/PROJECT_CONTEXT.md` §7 并收紧）

1. **不得出现"提高命中率/准确率"的表述**（任何语言、任何前端文案、任何 README）。可以说"改变分布"、"减少冗余"、"EV 守恒"。
2. 任何百分比必须带**样本量**与**置信区间**；未达样本量的结论必须写 `insufficient`。
3. 不得把离线回放与真实交战样本**混算**（两表物理隔离，前端两数并列且不相加）。
4. `usable` 必须同时满足：**阶段持续 + A/B 通过 + 前置注册通过（`prereg_ok`）**，三条缺一不可；且 `source=="llm"` 永不 usable。
5. 无 `scope` 的 p 值不得展示；无 `prize_assumption` 的 `ev_ratio_full` 不得展示。
6. "未检出" 不得写成 "已证明无效"（§1.6a）。

### 5.2 样本外纪律

任何阈值、分位、模型参数，**只能**用 `draws[:t]` 计算；测试里必须有一条"把阈值偷换成全样本，测试必须失败"的**反向用例**。

### 5.3 随机性自证（项目传统，从 V4 起）

每个检验类新功能都必须带两个测试：
- (a) **注入偏置 → 必须检出**；
- (b) **纯随机 → 不得误报**（落在名义 α 的蒙特卡洛容差内）。

默认规模 ≤ 2000 次；10 万次 / 1000 序列只在 `LOTT_SLOW_TESTS=1`。

### 5.4 回归基线与验收命令（**F2/G8 的核心纪律**）

1. **基线**：`python -m pytest -q` 实测 **480 passed / 0 failed**（两次实测 84.04s / 114.29s，R4 P0-A）。v1.0 写的 **381 是 V5 时代遗留**（差额 99 = `test_v6_regime` 56 + `test_v7_rules_closure` 24 + `test_v8_eras_tasks` 19）。本批次后全量必须 **≥ 480 + 新增数**，且 **0 failed**。
2. **验收命令一律用全量 `pytest -q`，禁止单文件跑作为验收。** 理由（R4 P0-B 实证）：单文件跑**恰好规避**了 19 处 `DB_PATH` 泄漏，是**假绿灯**；`test_m4_eval.py + test_v10_*` 同会话时 `count_draws()==0` 会直接失败而单独跑却通过。

```bash
cd /root/DSH/应用/ssqyuce
.venv/bin/python -m pytest -q                                  # 唯一验收闸门：≥480+新增，0 failed
.venv/bin/python -m pytest tests/test_v10_xxx.py -q            # 仅作快速反馈，不作验收
.venv/bin/python -m compileall -q lottery
node --check web/static/app.js                                 # 前端有改动时
git diff --check
```

3. **耗时闸门**：全量默认 **≤ 180s**，硬上限 **240s**（超了必须拆分）；单例默认 **≤ 2s**，`> 5s` 标 `@pytest.mark.slow`（现状已超标：`test_v3_u2_u3_u4.py` 单例 27.61s）。新增测试总数 **≤ 100**（与 480 合并后 ≤ 580）。
4. **慢测试闸门**：`LOTT_SLOW_TESTS=1` 才放大规模（项目此前完全没有该机制）。
5. **新测试不得依赖真实 `data/ssq.db`**，必须用 `isolated_db` fixture 造库。
6. 端到端 `predict_next` 用例 **≤ 6 例**，且 `use_llm=False, use_ml=False`（单次 0.61s）。

### 5.5 不许静默降级

LLM/ML 不可用 → 明确返回 `degraded: true` + 原因；不许把降级伪装成正常结果，也不许把"没做"计入"做对"（§3.2.1）。

### 5.6 部署与数据安全

1. **顺序**：停调度 → 备份（API 或 tar 全量）→ 迁移（自动镜像备份）→ `/api/health` 的 `schema_version==3` → 恢复调度（§2.4）。
2. **数据安全铁律**：本机 `data/` 是**唯一数据副本**（除公网 ssq.ff88.de 镜像外无其他节点）。**任何迁移前必须先完成 V10-00c 的备份安装与恢复演练** —— 当前零备份状态（F10）下不允许做任何 schema 变更。
3. 迁移 **forward-only**（§2.1.5）：只许 `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX` / `ADD COLUMN`。
4. 前端改动必须同步升 **三处** `?v=`（含 echarts）与 `APP_VERSION`/`APP_BUILD`（目标 `3.0.0` / `2026-10-V10`）。
5. 容器 Python 是 **3.11.17**，本地 venv 是 **3.13.5** → 发布前必须容器内 `import` 冒烟。

---

## 第六部分　给用户的一段话（必须写进 UI 与 README）

> **本系统不会、也做不到提高中奖概率。** V10 的改进全部集中在三件事上：
> 1. **让 AI 的每一次输出都有对错** —— 它做的是"核对已算好的结果"和"判定参数是否合规"这两件**答案唯一**的事，而不是猜号码；
> 2. **让"发现了什么"这件事变得可信** —— 前置注册（先登记再检验、双口径校正）+ holdout 验证 + 阶段边界锚定（同数据两次运行 p 值完全一致）；
> 3. **在固定花费下把票单的分布算清楚** —— 并集覆盖、`Ā`/冗余、方差比（中性值 `Ā=12/11`）、奖级分布。
>
> 三项都**不改变**单注 EV：`ev_ratio_full ≈ 0.45675`（整注，按 V4 奖金假设表 A，单注期望回报约 ¥0.9135 / 成本 ¥2.00）；`ev_ratio_sixth = 0.15625` 是**仅六等奖**的返奖率，与注数无关，展示时必须与整注口径分列。

---

## 附录 A　实测复现命令（可复现的证据链）

```bash
cd /root/DSH/应用/ssqyuce

# 1) 随机性与维度扫描（结论：91 次搜索、最优 BH 后 p=1.000、未校正显著 3 个 ≈ 期望 4.55）
PYTHONPATH=. .venv/bin/python -c "
from lottery import db, discovery
d = db.load_draws(); r = discovery.discover(d, top=12)
print(r['n_tested'], r['expected_false_positives'], r['best_quick_p_adjusted'], r['verdict'])"

# 2) 规律库状态（结论：usable=0 / monitor=17）
PYTHONPATH=. .venv/bin/python -c "
import json; d = json.load(open('data/patterns_runtime.json')); print(d['counts'])"

# 3) 基线（结论：480 passed）
.venv/bin/python -m pytest -q

# 4) 测试隔离缺陷（结论：顺序一变就挂）
.venv/bin/python -m pytest tests/test_m4_5_engineering.py tests/test_v4_honest_ui.py -q   # 现状 1 failed

# 5) 备份现状（结论：零备份）
cat /etc/cron.d/ssq-backup; ls /backup/; ls data/*.bak*; crontab -l | grep -c ssq

# 6) 交付状态（结论：远端停在 v2.1.0，本地 ahead 7）
git branch -vv; git log --oneline -1 origin/main

# 7) 前端缓存号（结论：doctor 已 WARN，2 个令牌）
grep -o "v=[0-9]\{8\}\.[0-9]*" web/index.html | sort | uniq -c
.venv/bin/python -m lottery.cli doctor | grep 缓存号
```

**关于 `docs/v10_probe_tickets.py`（F7/B7）**：该探针 v1 的 `cov_batch` **未使用 rng**（300 期跑同一组固定票），"贪心非冗余"的 `score` 里 `Σ c_j ≡ 6n` 是常数（实际只剩 `−2·max c_j`），`metrics()` 的 `max(0,k)` 是无操作。**该脚本必须重写为每期独立重抽并给出配对差 ± CI，否则不得作为任何结论的依据**；在重写完成前，本方案**不引用其任何数字**（v2.0 唯一引用的是"该口径 MDE ≈ +101.9% → 零功效"这个方法论结论）。

---

## 附录 B　数字来源台账（"哪个数字可以信"）

| 数字 | 值 | 来源 |
|---|---|---|
| `Var(K)` / `sd(K)` | 0.753099174 = 729/968 / 0.867812868 | 超几何解析 + pmf 数值 + `discovery.SD_HITS`（R2 §3.4.1、裁决 F1） |
| `Cov(K_i,K_j)` | `(27/176)A − 81/484` | R2 §3.4.2 全枚举验证到 1e-15；Lead 复算 A=6 时 = Var(K) |
| `var_ratio` 闭式 | `1+(n−1)(11/54)(Ā−12/11)` | R2 §3.4.3（其印的 11/27 为 2 倍手误）+ Lead 独立复算偏差 <1e-15 |
| 5 注全同 / 互不相交 | 5.000000 / 0.111111 | R2 §3.4.3 + Lead 逐项直接计算 |
| `p₄`（单注 ≥4 红） | 0.004900828 | R2 §3.4.7 全枚举 |
| `P(≥1注≥4红)` n=10 | iid 0.048150 / 覆盖铺满 0.046887（−2.6%） | R2 §3.4.7 全枚举 |
| `ev_ratio_sixth` / `ev_ratio_full` | 0.15625 / ≈0.45675（表 A）；≈0.49448（表 B） | R2 §3.4.5 + `V4_ACCURACY_DIAGNOSIS.md:177-187` |
| 全量基线 | **480 passed**，84.04s / 114.29s | R4 P0-A 实跑（四份评审一致） |
| 19 处裸 `DB_PATH` / 无 `conftest.py` | 19 / 不存在（git 无历史） | R4 P0-B 实测 + Lead 复核 |
| FWER 分批 | N=91：B=7→0.297、B=13→0.481、B=91→0.991 | R2 §3.1（解析 1−(1−α)^B 与 MC 一致） |
| `M_min` 与 FWER | 1→0.994、10→0.401、50→0.098、100→0.050 | R2 §3.1 R2 条 |
| truncnorm 尾部误差 | 有界离散上 26%~98%（k=4 加校正仍 43.9%） | R2 §3.2 |
| CUSUM 中位延迟 | 248 期 ≈ 1.65 年；37 维需 ARL0=18500（≈123 年） | R2 §3.3 |
| 真实残差报警 | 109 次 vs 期望 245 次（0.44×） | R2 §3.3.3 |
| regime 段数 | 23 段 / `era_block=150` / 最老段 215 | R2 §3.3.4 实测 |
| 探针零功效 MDE | +101.9%（Lead）/ +40.7%（R2） | 裁决 F7 |
| LLM 出号期数 | **5 期（2026115–2026119）** | R1 P1-6（v1.0 写 4 期） |
| `eval_details` 现状 | 5 期 / 30 行 / 9 个 method | R3 §6 + R1 P1-4 |
| 候选池大小 / SA 耗时 | 8~10 注；3000 trials = **0.034s** | R1 P0-5（v1.0"白跑 3000 次"前提不成立） |
| MDE（n=30 / 120 / 300） | 40.7% / 20.3% / 0.1404 红球（相对 +12.9%） | `rigor.py:641-660` |
| `?v=` 令牌 | `20261018.3` ×2 + `20260817.7` ×1（doctor WARN） | R4 P1-4 |
| Python 版本 | venv 3.13.5 / 容器 3.11.17 | R4 P2-1 |
| 磁盘 | `data/` 271MB（`ml_models/` 265MB） | R4 P0-C |

---

## 附录 C　自查清单（本文档的"无残留"证据）

**任何人改动本文件后必须重跑，结果贴进 PR 说明。** 期望：所有命中都只出现在 §0.2 删除台账 / 附录 C 自身，**不得**出现在任何"规格/步骤/代码/测试"段落。

```bash
cd /root/DSH/应用/ssqyuce
F=docs/V10_IMPROVEMENT_PLAN.md

# 1) 已删除的设计：只允许出现在台账与自查节
grep -n "选择性推断\|truncnorm\|凸约束" $F
grep -n "CUSUM\|E-Detector\|ARL0" $F
grep -n "Kendall\|kendall\|llm_decision_skill\|correct_top1" $F
grep -n "LOTT_PREDICT_CHANNELS" $F
grep -n "p_any_blue" $F
grep -n "381 passed" $F
grep -n "σ² = 1.2254\|σ = 1.107\|1.2254" $F
grep -n "随 coverage 增大而上升\|随 coverage 上升" $F
grep -n "experiments.status == \"passed\"\|experiments.status==\"passed\"" $F

# 2) 必须存在的新规格
grep -c "resolve_channels" $F              # ≥ 8
grep -c "prereg_ok" $F                     # ≥ 5
grep -c "patterns_runtime.py:104" $F       # ≥ 2
grep -c "ev_ratio_sixth\|ev_ratio_full" $F # ≥ 6
grep -c "11/54" $F                         # ≥ 3
grep -c "LOTT_SLOW_TESTS" $F               # ≥ 4
grep -c "pytest -q" $F                     # ≥ 8（且不得以单文件跑作为"验收"）

# 3) 诚实边界：不得出现"提高命中率/准确率"
grep -n "提高命中率\|提高准确率\|提升命中率" $F     # 只允许出现在"禁止"语句里，期望 ≤ 2
```

**版本自查记录（v2.0）**：

| 检查 | 结果 |
|---|---|
| 5 个删除项是否还在指导实现 | **无**。只出现在 §0.2 台账（已标注"不是实现指引"）与本附录 |
| "LLM_ONLY_OUTPUT=1 影响"是否仍被当成"AI 没用"的证据 | **无**。§1.3 已按 R3 P2-3 改为"n=20 样本不足，看不出任何东西" |
| 是否有"提高命中率/准确率" | **无**（只有"不得出现……"的禁令句） |
| 数字是否全部可追溯 | **是**，见附录 B（每行带来源） |
| 文件作用域表是否真实 | **是**，§2.1.7 按实际改动点重写，并据此给出真实 2 路并行 |
| 并行度 | **2 路**（§4.2），不是 4 路 |
| 任务表是否覆盖 Gate | **是**，§2.0 的 10 条 Gate 逐条挂到 00a–00d / 各任务 |

---

> **一句话总结**：方向保留（目标不可达、AI 放错岗位、真实缺陷在工程与口径），**方案返工**（删掉 3.5 人日的不可用设计，换成可机器判定的硬规格），**先做 2.5 人日 Gate 再开功能**，总量 **19.0 人日 / 真实 2 路并行 / 约 10 个工作日**。

---

## 附录 D　发现待评估的既有缺陷（V10 实施过程中发现，非本批次引入）

### D-1　`rigor.strategy_significance` 的单侧口径会系统性地"看不见劣绩"

**发现路径**：V10-09（LLM 对照臂）实现者构造反例时发现，Lead 独立复现确认。

**现象**：`strategy_significance` 的 `p_value` / `p_uncorrected` / `adjusted_p` / `bonferroni_p`
都是**单侧**口径，方向由 `delta` 符号决定。当策略**显著更差**（delta < 0）时，单侧 p 会接近 1，
于是任何用 `adjusted_p` 判「方法是否有效」的调用点，都会把"显著劣绩"读成"无显著差异"。

**Lead 实测（含噪、小幅更差，贴近真实场景）**：

| n | delta | `adjusted_p`（单侧） | `p_two_sided` | 单侧判定 |
|---|---|---|---|---|
| 30 | −0.418 | **1.0** | 0.0087 | not-significant |
| 60 | −0.391 | **1.0** | 0.0011 | not-significant |
| 120 | −0.322 | **1.0** | 7.6e-06 | not-significant |

策略的红球命中**每期低 0.32~0.42 个**（双侧 p 已到 1e-6 量级），单侧口径却报 p=1.0。

**影响面**：`lottery/battle.py:613-648`（方法建议与交叉校验）、`:1218` / `:1293`
（逐方法 A/B 对比）等路径都直接取 `p_value` / `adjusted_p`。
含义：**项目此前所有"方法对比"结论，可能都低估了劣绩**——一个真正更差的方法会被判为"无差异"，
从而留在候选池里。

V10-09 已在**对照臂这一处**用 `p_two_sided` 修正（并做了互换对称性验证），
但 **`rigor` 层本身未改**（超出该任务的文件范围）。

**待办（建议 V10-13 或后续批次）**：
1. 明确 `strategy_significance` 的返回契约：单侧值保留（向后兼容），但**新增**显式的
   `p_two_sided` / `adjusted_p_two_sided`，并在 docstring 里警告"判优劣必须用双侧"
2. 审计 `battle.py` 全部调用点，凡"判断某方法是否更差"的地方一律改双侧
3. 补一条回归测试：构造"显著更差"的场景，断言双侧能判出、单侧不能（把这个坑钉死）

### D-2　`ml_model` 的置换检验零复制次数 B 不足，导致 `trusted=True` 假阳性率过高

**发现路径**：V10-10 复核 `/api/ml/diagnosis` 时，Lead 注意到 `ml_blue` 返回
`trusted=True, p=0.0476`，而 `ml_red` 是 `trusted=False`；顶层 `trusted=False` 但
`tier=signal_found`（两者不同层级，但这个组合会误导读者）。进一步做假阳性率实测。

**Lead 实测（真实 3515 期数据，随机打乱期序以破坏任何时间结构，即"纯噪声"）**：

| 试验 | `trusted=True` 次数 | 假阳性率 |
|---|---|---|
| N=8（blue，max_numbers=3） | 2 | 25.0% |
| N=20（blue，max_numbers=3） | 7 | **35.0%** |

理论值：若按单号码 α=0.05 判、检验 3 个号码不做校正，族错误率 ≈ 1−0.95³ ≈ **14.3%**。
实测 35% **明显高于**该值，更远高于名义 α=0.05。

**根因（代码已定位）**：`_combine_signal_test`（`ml_model.py:711`）用**联合零分布**回避了
"逐号码 Bonferroni 永远无法拒绝"的问题（这个设计本身是对的），但它仍用
**B = n_null = 19/20 次零复制**。于是：

- 最小可达 p = `1/(B+1)` = **0.05**，恰好卡在 α=0.05 的判定线上；
- 只要真实统计量超过**任何一个**零复制，p 就被压到 0.05 → 判显著；
- 也就是说 `trusted=True` 的实际门槛近乎"比零分布最大值略高"，而非 5% 水平。

实现者自己的注释（`:723-725`）已写明"B=19 → 最小可达 p 是 0.05"，但**没有把 B 调大**，
也没有对这个边界做校准测试。

**修复建议**：
1. `n_null` 默认从 20 提到 **≥ 200**（至少 ≥ 1/α−1 的若干倍），使最小 p 到 0.005 量级；
   代价是诊断耗时线性上升，需配合 `max_numbers` / `max_issues` 调低或做成异步任务。
2. 或改用**参数化零分布**（拟合零分布尾部）以少量复制获得更细的 p 值，
   但必须做校准测试（打乱标签下假阳性率 ≤ α + MC 容差）。
3. **必须补一条校准测试**：打乱标签 N≥200 次，断言 `trusted=True` 的比例落在
   `α ± 3·sqrt(α(1−α)/N)` 内。现有 17 条测试**没有覆盖假阳性率校准**，
   所以 35% 这个缺陷才能通过所有测试。
4. 顶层 `tier` 在有多个通道且结论不一致时应取**最保守**的那个（当前会被
   `signal_found` 覆盖，与 `trusted=False` 并排展示时自相矛盾）。

**D-2 补充（Lead 于 task-14 体检过程中发现，同一根因的第二个表现）**：

不只是"假阳性率过高"，同一处还有**反向问题**：`significant = p_mean < alpha`，
而 B=19 时 `min_attainable_p = 1/(19+1) = 0.05`，**严格小于 0.05 在数学上不可能**。

实测（`_combine_signal_test` 直接调用，注入极强分散信号）：

```
T_mean        = 3.7085
null_T_mean_95= 1.3091      ← 信号强度是零分布 95 分位的 2.8 倍
p_value       = 0.05        ← 恰为 min_attainable_p
significant   = False       ← 因为 0.05 < 0.05 为假
```

即：**信号强到 19 个零复制全部被碾压，却仍判"不显著"**。

归纳：B 太小时，判据在 α 附近同时呈现
- **假阳性膨胀**（只要略超任一零复制就 p=0.05 → 判显著；实测 35%）
- **真阳性压制**（信号再强也停在 p=0.05，因严格不等号判不出）

两者是同一个 `p = (1 + #{null ≥ real}) / (1 + B)` 在 B 过小时的必然结果。
**修复必须同时解决这两个方向**：单纯把判定改成 `p <= alpha` 会让假阳性更糟；
正解是提高 B（或改用参数化尾部），使 p 的分辨率足够细。

### D-3　`shuffled_label_test` 单次调用 60~165s，超出同步 API 合理范围

**发现路径**：D-2 修复后跑全量测试时 `pytest` 卡在 19% 超时 6m40s，定位到
`tests/test_v10_ml_calibration.py::test_t7`（fixture 跑 5 个种子）实测耗 6m40s+。

**Lead 实测拆解**（本机 4 核，load average 5.8~8.0）：

| 项 | 耗时 |
|---|---|
| `_make_diag_learner` 训练一次（VotingClassifier = RF + HistGB） | **6.76 s** |
| `_pos_proba` 预测一次（200 样本） | 0.39 s |
| 零分布 20 次 × fit | ≈ 135 s |
| 置换重要性 (20 特征 × 2 重复) × 预测 | ≈ 16 s |
| **合计（实测一次完整调用）** | **164.5 s** |

**影响**：
- `tests/test_v10_ml_calibration.py` 的 fixture（5 个种子）单文件 > 6m40s，
  导致**全量测试无法在合理时间内跑完**（这是本轮"全量卡在 19%"的直接原因）；
- `GET /api/ml/diagnosis` 是**同步接口**，D-2 把 standard 档 `n_null` 提到 200 后，
  一次诊断 ≈ 200 × 6.76s ≈ **22 分钟**，远超任何 HTTP 超时。

**修复建议**：
1. **测试侧（必须，阻塞全量）**：`test_v10_ml_calibration.py` 的 fixture 大幅降规模
   （`n=1000→300`、`n_null=20→5`、`n_estimators=15→8`），或整体放进 `LOTT_SLOW_TESTS` 闸门，
   默认档只保留一个轻量冒烟用例。目标是该文件 < 20s。
2. **生产侧**：`/api/ml/diagnosis` 必须改**异步任务**（复用现有 `tasks` 表机制），
   或缓存优先 + 后台刷新，**不得同步跑 200 次零分布**。
3. 诊断学习器可考虑去掉 HistGB（只留 RF）或把 `max_iter` 压到 10 —— 但需重做
   D-2 的假阳性率校准（换学习器会改变零分布）。
