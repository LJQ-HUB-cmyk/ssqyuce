# R3 评审报告：AI 部分（LLM 决策 / 可验证评测）的可落地性审查

> 评审对象：`docs/V10_IMPROVEMENT_PLAN.md` 中与「AI / LLM」相关的部分（P0-1、P0-2、P0-4、P1-4、P1-5，以及 §3.1 的 V10-01/02/04/09/11）
> 评审立场：**AI 工程 / LLM 应用 / 可验证评测**（只评 AI 相关；数学统计口径（P0-3/P1-1/P1-2/P1-3）不在本文范围）
> 评审方式：只读代码 + 只读运行时数据，**未发起任何真实 LLM 调用**
> 代码基准：`APP_VERSION 2.5.0 / 2026-10-V9`，`data/` 现状实测于 2026-10-17

---

## 1. 一句话结论

**需修改后认可。**

方案对「AI 为什么没用」的定性判断是对的，`LOTT_LLM_ONLY_OUTPUT=1` 这个默认值确实是把最没信息量的通道设成了主力，P0-4 第 1 条应当立即执行。但**它用来替换「找规律」的两块新岗位（P0-1 决策排序、P1-4 因果归因）目前都不是"可验证任务"**：一个测的是"LLM 能否猜出我们自己的确定性排序函数"，另一个在 4 期 / 30 注的样本上只能得到 `none`。更严重的是，P0-4 第 2 条（LLM 候选必须 `experiments.status==passed` 才能进候选池）与方案自己 §1.1 的结论（规律在数学上不存在）**直接构成死锁**，且这条死锁与「AI 闭环今天为什么空转」是同一个错误的第二次犯——只是把空转点从"规律库"搬到了"候选池"。**在 P0-4 第 2 条改成"LLM 出号走独立对照臂、永不进规律库/候选池"之前，本方案不应进入 V10-01/V10-04 的排期。**

---

## 2. 评分表

| 维度 | 分数 | 理由（一句话） |
|---|---|---|
| 决策任务有效性（P0-1） | **2 / 5** | 候选票单由后端确定性规则生成，"正确答案"就是那条规则本身 → τ 测的是"猜规则"，不是"预测开奖"；且每期仅 1 个样本，按方案自己的 N≥30 门槛必然落入"不显著"，统计上几乎必然给出"AI 无用"的结论（见 §3.P0-1） |
| abstain 契约（P0-1） | **1 / 5** | 三项数字**本来就在 LLM 的 prompt 里**（`llm_lab.py:147-153`），"抄一行数字"即可通过；这是纯格式校验，测不出任何能力，反而制造"AI 很诚实"的假象（见 §3.P1-1） |
| 默认值改动（P0-4） | **2 / 5** | 第 1 条（默认 0）正确且应做；第 2 条对未变动的 LLM 通道**零影响**、对"LLM 提供候选"模式**近乎死锁**，与现有 `LOTT_RULE_APPLY` 默认 0 叠加后 LLM 永不可能影响出号；第 3 条 `LOTT_PREDICT_CHANNELS` 在代码里并不存在，会与 `LOTT_METHOD_MODE` 形成第二套开关（见 §3.P0-4、§4） |
| 因果归因（P1-4） | **2 / 5** | 真实样本只有 5 期 / 30 注（`eval_details` 实测），任何归因都只能得 `none`；`evidence_rows` 校验防不住"把结果抄回去"，只防"抄错行"（见 §3.P1-4） |
| 成本与自动关停（P1-5） | **2 / 5** | `llm_client` 现有的是**输出预算**不是**钱**；`usage_snapshot()` 全项目只有一个消费者，`llm_cost_log.jsonl` 需要新接线程安全钩子；"自动关停"与 `_scheduler_step` 的 `predict_next(use_llm=True)` 无任何耦合，写在 `llm_client` 里会变成"预测报 503 而不是关掉通道"（见 §3.P1-5） |

加权：**不通过**（AI 相关三块 P0（P0-1/P0-4/abstain 子项）全部低于 3 分）。

---

## 3. 问题清单

### P0-1 · 「决策技能度量」不是一个好的可验证任务

**证据（候选方案由我们自己生成）**
- `engine.py:1452-1466`：候选票单来自 `blend`（stat/ML 概率）采样 + `uniform` 对照；`engine.py:1660` 之后按 `ensemble_mass` 算 `confidence`，再经 `patterns_runtime` 规则重排（`engine.py:1704-1711`）。也就是说**"哪个方案更好"完全由 `_ticket_objective` / `portfolio_expected_value` / `blue_coverage_plan` 这些我们自己的确定性函数算出来**。
- `engine.py:1412-1416` 的 `coverage_optimize(objective=config.COVERAGE_OBJECTIVE)`，而 `config.py:210` 的取值域是 `{"coverage", "expected_value"}` —— 方案 P0-1 第 2 条说"按用户声明的偏好权重（见 P0-4）算出真实排名"，但 **P0-4 里根本没有偏好权重**（偏好权重是 P2-3 才有的东西）。方案在这里**引用了一个不存在的上游定义**，导致"真实排名"的 ground truth 在规格层就是空缺的。

**后果（这是指标设计问题，不是实现问题）**
1. **LLM 看到的排序信息没有"可学的开奖信号"**：候选票单之间唯一的差别，是它们在**我们自己的评分函数**下的名次。LLM 要做对，必须反向推断出 `confidence = 10 + 0.25*em`（`engine.py:1697`）、`blue_coverage_plan` 的铺号顺序、`var_ratio` 的惩罚项——这些是**我们的代码，不是开奖结果**。因此 `llm_decision_skill` 的正确命名是 **`llm_rule_guessing_skill`**，它**不能**作为"AI 能否预测开奖"的任何证据，也不能写进 §2.0 的"AI 的每一步输出都能被确定性代码打分"里当卖点。
2. **信号是零，且不是"弱"，是"零"**：候选池里没有一条来自未来开奖的信息（这是正确的、也是必须的），所以任何"真实排名"都**不预测**下一期命中。让 LLM 排序 = 让它复述我们的确定性函数。可验证 ≠ 有意义：这个任务**可验证，但检验的构念（construct）是错的**。
3. **更糟的是它会污染 `LOTT_RULE_APPLY` 的语义**：如果 LLM 排名与"真实排名"一致，我们最终仍然只会用真实排名出号（`engine.py:1712` 的 `llm_only` 分支才用 LLM 的），所以即使 τ=1.0，**对开奖结果的影响恒等于 0**。也就是说这个指标唯一可能产生的下游效果是"证明"或"证伪" LLM——**这正是方案想要的两难**。

**样本量估算（(b) 的答案，实测数值）**

`τ` 在"随机排序"零假设下的标准差（本机 `np.random` 模拟 20 万次，n=options）：

| 每期选项数 n | E[τ] | sd(τ) | P(τ = +1，纯随机) |
|---|---|---|---|
| 2 | 0 | 1.000 | 0.4997 |
| 3 | 0 | 0.638 | 0.1659 |
| 4 | 0 | 0.490 | 0.0415 |
| 5 | 0 | 0.409 | 0.0082 |

→ **n=2 时 τ 就是 ±1 的抛硬币，`kendall_tau` 这一列在 n=2 时没有任何信息量**（方案允许 2–5 个选项，必须在 schema 上禁止 n=2，或对 n=2 不算 τ）。

要检出给定效应所需期数 `T = (z·sd/δ)²`（双侧，功效 0.8）：

| 每期选项数 | 目标 τ | z=2.0（p≈0.05） | z=2.8（p≈0.005） |
|---|---|---|---|
| 4 | 0.2 | 25 期 | 48 期 |
| 4 | 0.3 | 11 期 | 21 期 |
| 5 | 0.2 | 17 期 | 33 期 |
| 5 | 0.3 | 8 期 | 15 期 |
| 4 | 0.1 | **96 期** | **188 期** |
| 5 | 0.1 | **65 期** | **126 期** |

**关键结论：方案写的"采样 N≥30 次"在 τ≈0.1~0.2 这个"幻觉级"效应上功效不足**（30 期只能检出 τ≈0.18（n=5）/0.22（n=4）以上）。而 LLM 在"猜我们规则"这件事上最可能的真实 τ 就落在 0~0.2 区间。因此 30 期后**几乎必然得到"不显著"**，然后触发方案 §P0-1 第 5 条的"关掉通道"。

`correct_top1` 更早饱和：n=5 时随机基线 0.2，要区分 0.2 vs 0.35 需要约 **167 次**（α=0.05、功效 0.8 的双比例检验）。而"每期一次"意味着 **167 期 ≈ 56 周**。30 次时的 Wilson 95% CI 是 **[0.095, 0.373]**，下界已经低于随机基线 —— 也就是说**在 N=30 这个方案自己定的门槛上，停用判据（Wilson 下界 ≤ 随机基线）几乎必然被触发**，不管模型好坏。

**关于 (c) 关停判据的表述**

方案 §P1-5 的规则是"Wilson 下界 ≤ 随机基线时自动关闭 decision 通道并报警"。这条规则本身在统计上没错，但它**在 N=30 时是确定性触发而不是证据**。如果直接写成"AI 决策通道已关闭：技能不显著"，用户会读成"**经实验证明 AI 无用**"。应当改为三段表述（缺一不可）：

> ① `llm_decision_skill` 衡量的是"LLM 能否复现本系统既有的确定性排序规则"，**不是**"能否预测开奖"；
> ② 当前 N=__ 期，`τ=__`，95% CI `[__, __]`，与随机排序不可区分；本检验只能检出 τ ≥ __ 的效应（复用 `rigor._min_detectable_effect` 的口径，`rigor.py:641`，实测 n=30 → 最小可检出相对提升 40.7%，n=120 → 20.3%）；
> ③ 关停是**成本决策**，不是"AI 无能"的结论；如需恢复请在设置页手动打开，并会重新累计样本。

**修改建议（见 §4 M1/M2）**
- 把 P0-1 的 `decision` 任务的 ground truth 从"我们自己的排名函数"换成**一个不依赖我们的、可外部复算的目标**，否则放弃该任务；
- 若坚持保留，指标必须**同时**记录一个"确定性基线排序器"的 τ（例如 `confidence` 排序、`uniform` 排序），LLM 只有在**显著优于该基线**时才算有技能 —— 否则测到的是"谁更接近我们的规则"，没有任何意义；
- N 的门槛改为**按 τ 的目标可检出效应反推**（上表），并在 UI 上直接显示"还需 __ 期"。

---

### P1-1 · abstain 契约是形式校验，测不出能力，还制造"AI 很诚实"的假象

**证据（三项数字本来就在 prompt 里，而且就在 LLM 的原文回复里）**
- `llm_lab.py:147-153`：`build_lab_context` 把 `已测试组合数`（= `scan.n_tested`）、`纯随机预期假阳性数`（= `expected_false_positives`）、`最优BH校正p`（= `best_quick_p_adjusted`）**原样写进了上下文**；`build_user_prompt`（`llm_lab.py:176`）把整个 context 的 JSON 前 14000 字符塞进 prompt。
- 实测证据：`data/llm_claims.json` 最近三次运行的 `overall` 原文就已经把这几个数字抄了一遍："已测试组合数为 91、纯随机预期假阳性 4.55、最优 BH 校正 p=1.0"。→ **这不是"要求"，这是"复读"，LLM 今天已经在做了，不需要任何额外能力。**
- 因此 `abstain` 的三项字段校验（"缺一项即判失败"）能通过的模型 = 会 copy 的模型 = 今天的模型。它**零信息量**。

**它能防住的**（很有限）：模型输出幻觉数字（比如自己编一个 `n_tested=30`）。**防不住的**：模型根本没读扫描结果，只是把 prompt 里那三个 token 抄回来；以及模型每次都 abstain（永远弃权，永远"诚实"）。

**会导致的产品事故**：方案 §P0-1 第 4 条要求 abstain 时 UI 显示"AI 已核对扫描结果并声明弃权"。**这是一句不成立的话** —— 我们无法区分"核对过"与"抄了三个数字"。UI 这么写就构成了新的不诚实表述，直接违反方案自己 §3.2 第 1 条的诚实边界。

**替代设计（能真正区分能力的 abstain 契约）**

不要校验"引用了什么"，要校验"**没被喂给模型、但可以后端独立复算**的东西"。三档递进，全部可机器判定：

1. **留出（hold-out）引用校验**：把当次扫描的 `n_tested`/`best_quick_p_adjusted`/`expected_false_positives` **从 prompt 里删掉**，改为要求 LLM **自己从维度表 + 原始分年序列算出**这几个量（维度表在 `llm_lab.py:145` 是给的，但扫描结果不给），后端核对误差。这测的是"能否执行多重比较校正"——一个真能力。
2. **反事实弃权（abstain discrimination）**：每 k 轮注入一次**伪造的强信号**（后端人为把某个维度做成显著，或用纯随机数据中的"看起来显著"的候选），LLM 若仍然 abstain 则记 `abstain_false_negative`；另外注入一次**真实但极弱的信号**（τ 级别的边缘 p），LLM 若提出规律则记 `abstain_false_positive`。**只有这两种错误率都有估计值的 abstain 才算"已核对"**。这复用项目已有的"注入偏置→必须检出 / 纯随机→不得误报"传统（方案 §3.2 第 3 条），成本为零（不需要真 LLM 也能先跑通逻辑）。
3. **弃权契约必须带"下一步"且被判分**：`abstain` 必须同时给出 `next_tests: [...]`，每条必须落在维度表档位内（`llm_lab._params_ok`，`llm_lab.py:325`，已有此机制），后端按"是否可落地 / 是否重复已测组合 / 下次运行后该组合是否真的进了 top-k"打分。这测的是"建议的质量"，比"抄三个数字"难得多。

UI 文案相应改为："AI 声明弃权；本次弃权通过/未通过留出校验"——**"通过"才有资格显示为有效输出**。

---

### P0-4 · 修默认值：影响面被低估，且第 2 条是按设计死锁（方案最严重的自我矛盾）

#### P0-4-1「`LOTT_LLM_ONLY_OUTPUT=1` → 0」：**影响面被低估，且有既有回归说明第 1 条是必须做的**

**实际生效路径**（`engine.py:1715-1728`）：
```
llm_only = bool(config.LLM_ONLY_OUTPUT and llm_cands and not coverage and bet_mode == "single")
```
只有"有 LLM 候选 + 非 coverage + single"三者同时成立才生效。改默认值的**真实影响面**：

| 路径 | 默认 1 时 | 改成 0 后 |
|---|---|---|
| Web 预测（`use_llm=true`，`api_app.py:1137` 起） | **出号 = LLM 产出**（4 期实测 5 注/期，`eval_details` 中 `llm:agent-router/deepseek-v4-flash` 全部来自这里） | 回到"候选池混选 + 补齐到 10"（`engine.py:1745-1756`） |
| 调度器 `_scheduler_step`（`api_app.py:1160` `engine.predict_next(draws, use_llm=True)`） | 同上，**每期自动**把 LLM 出号入库 | 同上，改后统计/ML 主导 |
| `coverage=True` / `blue_compound` / `dan_tuo` | 不生效（`engine.py:1721`） | 不变 |
| `llm_required=False` 的评估/回放路径 | 只影响注数口径 | 不变 |
| `api_app.py:196-224` 读 `llm_only_output` 快照、`db.py:478` 写快照 | 前端"注数口径"字段 | 字段仍在，值变 False；`shortfall_reason` 从 `llm_only_actual_count` 变为 `all_methods_disabled`/`candidate_pool_exhausted`，**前端有分支依赖这两个字符串**（需一并核对） |
| `tests/test_llm_only_output.py:66-152` | 5 个用例，全部 `monkeypatch.setattr(env,"LLM_ONLY_OUTPUT",True)` | **不受默认值影响**（测试显式设置），但**有一条 `:125` 专门测默认 0 的旧行为** → 改默认值后"默认路径"的测试覆盖率反而变成由这条用例承担，建议加一条"不 monkeypatch 时默认 == False"的断言 |

→ **第 1 条正确，应当做，而且应当单独先做**（1 行改动 + 3 条测试）。方案说它"1d"，实际半天。

#### P0-4-2「LLM 候选必须前置注册 + as-of 回放且 `experiments.status==passed` 才能进候选池」：**按设计死锁**

**死锁链条（每条都有代码证据）**
1. 方案 §1.1 自己证明：所有维度校准后 p = 1.000，**规律不存在**。
2. 现有证据链：`score_hypothesis`（`hypothesis.py:1408-1417`）在提供 `p_adjusted` 时，`p_adj >= alpha → 无显著差异 → 维持 proposed`；`resolve_registry`（`hypothesis.py:1501-1507`）**只有"支持"才能升 `validated`**。
3. 方案新增的 `experiments.status` 与之同构（P0-2 第 2 条：Bonferroni 只按本批注册数校正）——**通过率同样是零**。
4. 因此 `status == passed` 的集合**恒为空** ⇒ **任何 LLM 候选永远无法进入候选池**。

**但这还不是全部，还有第二个死锁（方案没提到，实际更根本）**
5. 即便某条 LLM 假说侥幸 `validated`，`patterns_runtime.build_snapshot`（`patterns_runtime.py:110`）要求 **`regime.status ∈ {"persistent"}`** 才 `usable=True`；而候选池只消费 `usable` 的维度规则（`engine.py:598` 的 `patterns_runtime.usable_rules`）。
6. 而"persistent"要求**多阶段持续显著**——在 91 组扫描全灭的数据上，**这是永不可能事件**。
7. 于是 `patterns_runtime.json` 的 `counts` 永远是 `{"usable": 0, "monitor": 17, "rejected": 0}`（方案 §附注实测），engine 每期都走 `note_zh = "本期没有可用规律（未通过阶段验证）"`（`engine.py:595`）。

**结论：方案 P0-4 第 2 条不是"提高门槛"，是"在原本已经空转的闭环上再加一道永不开的门"。它与方案 §1.3 的诊断（LLM 结论必然为空 → 闭环永远空转）是同一个错误，只是搬了个地方。**

**第三个问题：`research` 模式并不能救它**
- 方案说"否则只能作为对照臂（`research` 模式）"。但 `LOTT_METHOD_MODE=research` 的语义是**忽略方法开关、全部启用**（`methods.py:117-126`），它并不产生一个"LLM 对照臂"，也不产生 `replay_results`。`battle.replay_method` 明确 **`llm` 不可回放**（`battle.py:53-54, 81`："需要对 3215 期各调一次外部模型（不可行且不可复现）"）。
- 所以"LLM 以 research 模式出号、与基线配对比较"这条路**在现有代码里不存在**，需要新建（见 §4 M3）。

**解法（推荐，也是本文最重要的建议）**

> **LLM 出号走"独立 A/B 对照臂"，永不进"规律库"，也永不进"候选池"。**

具体：
1. **规律库与出号策略彻底解耦**（这一点今天的代码已经做对了一半：`apply_hypotheses` 只消费 `validated`，而 engine 根本不调用它 —— 见下方 P1-6 的额外发现）。LLM 想影响出号，只有一条路：**作为一条独立的 method 出现在候选池**（`method="llm:<model>"`，`engine.py:1664` 已经是这个形态）。
2. **A/B 对照臂的定义改成"同成本、同注数、同期"的配对**，而**不是**回放（回放不可行）：每期真实开奖后，把 `llm:*` 的 `eval_details` 与 `stat:*/ml` 的 `eval_details` **按注配对**（`evaluate.py:142-149` 已按 method 分组写入，`db.save_eval_details` 幂等）。检验用 `rigor.strategy_significance`（`rigor.py:690`，支持逐期配对序列 + `n_trials` 校正）或 Wilcoxon，**与 `battle` 的其它配对口径一致**。
3. **门槛从"显著性"降级为"不劣于基线 + 成本可解释"**：这是唯一能在"规律不存在"的世界里自洽的门槛。可选三档状态：`eligible`（未显著劣于基线且未显著优于基线）/ `promoted`（显著优于基线，且 Bonferroni 按本次对照臂数校正）/ `retired`（显著劣于基线或成本超限）。**注意 `promoted` 在一个真随机世界里几乎永远不会发生——这本身就是正确的输出，产品上必须允许"永远停在 eligible 且默认关"**。
4. **把"前沿注册"保留在其真正该在的地方**：注册簿（P0-2）用于**审计"我们找了多少次"**（方案 §附注最后一段那个问题才是真问题），**不用于门禁出号**。注册簿的出口是"报告"，不是"开关"。
5. **契约要求写死**：`experiments.source == "llm"` 的记录**禁止**被 `patterns_runtime.build_snapshot` 消费（现在 `patterns_runtime.py:99-100` 是"假说库全量"，只要库里有就会进快照）。这条是防"死锁通过后门复活"的硬约束，必须有测试。

#### P0-4-3「`LOTT_PREDICT_CHANNELS`（默认 `stat,ml,uniform`）」：会与既有开关冲突

- **代码里不存在 `LOTT_PREDICT_CHANNELS`**（全仓 grep 无匹配）。出号通道现在是 `config.METHODS_SPEC` / `LOTT_METHOD_MODE` / `methods.py is_enabled("llm")` 三件套管的（`engine.py:1571-1578`）。
- 再加一个 `LOTT_PREDICT_CHANNELS` 会形成**两套并行的开关**，且方案 §P0-4 第 3 条说"默认不含 llm"，但 `methods.py` 默认全部启用 ⇒ 用户看不到任何差异而配置已经分裂（`methods.py:129-142` 的 `is_enabled` 只看 spec）。**这是可预期的"改了默认值但行为没变"事故。**
- 建议：**不新增开关**，直接复用 `LOTT_METHODS`（已有 `-llm` 关闭语义，`methods.py:161`）+ P0-4 第 1 条的默认值 0。要"面板显式勾选"就做在设置页 `/api/methods/config`（`api_app.py:769` 已有）。

#### P0-4-4「评估报表 `llm:*` 与 `stat/ml` 分列」：**可行，但要看清楚数据现实**

`eval_details` 实测（`data/ssq.db`，2026-10-17）：

| method | 注数 | 平均红球 | 蓝球命中 |
|---|---|---|---|
| `llm:agent-router/deepseek-v4-flash` | 20 | 1.30 | **0** |
| `llm:group/auto-deepseek-v4-flash-vision` | 4 | 0.50 | 1 |
| `stat:freq` | 2 | 1.50 | 0 |
| `stat:bayes`/`stat:omission`/`stat:markov`/`ml` | 各 1 | 1.0~2.0 | 0 |

按 issue 分布：`2026114` 一期有 6 个方法；`2026115–2026118` **每期只有 1 个方法（llm）**。
→ **"分列"之后 `stat:*/ml` 只剩 6 注、1 个批次。任何"分列对比"都会立刻落入"样本不足"**。方案 §P0-4 第 4 条必须与 P0-5 联署：**分列的同时必须显示每列的 n，并在 n < 60（`config.METHOD_RECOMMENDATION_MIN_ISSUES`，`config.py:138`）时禁止出任何结论句**。当前 `eval_details` 里 LLM 的 20 注 × 红球 1.30 看似"优于 1.0909"，但在 n=20、sd=0.868 下 t≈1.09，p≈0.29 —— **看不出任何东西**，而方案 §1.3 表格用它当"AI 没用"的证据，同样不成立（它只说明"注数不够"）。这是方案里**用证据反过来支持自己结论**的一处需要更正（P2 级，但会误导读者）。

---

### P1-4 · 因果归因在 4 期 / 30 注样本上只能得到 `none` —— 又一个空转闭环

**证据**
- 方案说"输入：近 N 期票单 → 结果（`eval_details` + `replay_results`）"。但 `replay_results` **是 P0-5 才新建的表**，现在不存在；`eval_details` 实测 **5 个 issue / 30 行 / 其中 4 行属于同一期的不同方法**。
- `attribution` 要求判断 `factor ∈ {blue_coverage, red_confidence, llm_pick}` 的 `direction`。这三者在今天的预测里**几乎共线**：`llm_only` 打开时，`blue_coverage` 根本不起作用（`engine.py:1715-1720` 直接跳过 coverage 分支），`red_confidence` 是 `confidence` 的函数，而 `confidence` 对 LLM 票恒 = `20 + 0.45*(0.5*conf+0.5*em)`（`engine.py:1695-1697`）。**要归因，必须有一组"同一期、同样注数、只改一个 factor"的对照票**——系统从来没有生成过这种对照。
- 现实中的 effect size：`rigor._min_detectable_effect(30)` 实测 → **最小可检出相对提升 40.7%（0.4439 红球/注）**；`n=120` 仍要 20.3%。P0-3 实测的三策略差（10.78 vs 10.97 vs 10.90）全部 < 2%。**在这种样本量下，`direction` 的正确答案几乎恒为 `none`**（这是统计上的正确答案）。于是 P1-4 的 "N 次后判断该归因技能是否显著" 也会走到 P0-1 同样的结局：**永远 `none` → 不显著 → 关停**。

**`evidence_rows` 校验能防什么、防不住什么**

| 能防住 | 防不住 |
|---|---|
| 模型引用不存在的期号（`issue` 不在表里） | **模型先看方向再编 evidence**：`direction` 与后端复算的一致率在"随机猜 3 类"下就已经是 33%；且只要 30 行里有 1 行支持某个方向，模型挑它即可 |
| 模型报的红/蓝命中和表里不一致（串行） | **选择的自由度**：模型可自由挑 1~30 行，等于给了它 30 次选择机会 → 假阳性率远高于名义 α（项目里 `rigor.strategy_significance` 的 `n_trials` 参数就是在处理同一件事，`rigor.py:705-707`） |
| 一个"合法性"门槛（可审计） | **共线性**：`llm_pick` 与 `blue_coverage` 在同一期不可分离，任何 `direction` 都不可identified |
| —— | **样本量**：`evidence_rows` 校验与"结论是否正确"完全正交，**校验全过 + 结论全错**是常态 |

**修改建议**
1. **把 P1-4 从"归因"降级为"陈述核对"**：任务改为"给定一张**已算好**的逐行对照表（含方法、注号、红/蓝命中、奖级），输出每个方法一行摘要，后端逐字段核对"。这仍然是"结构化小样本上的可验证任务"，但 ground truth 是**确定的算术**，不需要统计功效，30 行就能 100% 判定。**这是 LLM 在这个系统里唯一能立刻做对的事**（见 §5 第 1 条）。
2. **若坚持做归因**，必须先有**配对对照臂**（同 issue、同 n_tickets、只改一个 factor 的 A/B），并且用 P0-5 的 `replay_results` 提供 ≥ 300 期离线配对 —— 这需要 P1-3/P0-5 先完成，**P1-4 依赖 V10-05 不够，应改为依赖 V10-05 + V10-03 + 一个新建的"配对对照出号"任务**（方案 §3.1 里没有这个任务，估时 1.5d 明显不足）。
3. `evidence_rows` 校验必须**加上"必须引用全部行"或"必须引用后端指定的行集"**，否则选择自由度会把 α 彻底废掉。

---

### P1-5 · 成本记录与自动关停：现有机制是"输出预算"不是"钱"，且关停没有落点和恢复路径

**现有机制实测（逐条核对）**
- `llm_client.py:14` `_usage = {"calls","prompt_chars","completion_chars"}`、`llm_client.py:440-449` `usage_reset/usage_snapshot` —— **只有字符数，没有 tokens、没有金额**；且**全仓唯一消费者是 `llm_eval.py:158`**（离线评估）。
- `config.py:93` `LLM_BUDGET_FILE = DATA_DIR / "llm_budget.json"` —— 这是"**输出预算（max_tokens）记忆**"（`llm_client.py:20-92` / `llm_budget.json`），**与成本无关**。方案 P1-5 若把 `llm_cost_log.jsonl` 写在同一个模块里，命名极易混淆，务必在代码注释里写死"budget ≠ cost"。
- 轮转：`llm_client.py:163` **默认模型恒为 `LLM_MODEL_LIST[0]`**；真正的多模型轮转在 `engine.py:779-796`（观察轮）+ `engine.py:836-837`（采样轮 `model_cfgs[i % n_models]`）+ `engine.py:860`（校验轮 `_pick_verify_cfg`）。⇒ **一句话调用（如 `llm_lab` / `hypothesis._ask_llm`）不走轮转**，成本日志必须记录"实际使用的 model"，`chat()` 返回的只有 `content` 字符串，**拿不到 model 名** → 需要在 `chat()` 内部落钩子，而不是在调用方记。
- 超时：`LLM_TIMEOUT=60` / `LLM_LONG_TIMEOUT=180` / `LLM_TOTAL_TIMEOUT=75`（Web 预测硬预算，`config.py:82-97`）+ `deadline` 钳制（`llm_client.py:215-223`）。**唯一带总预算的是 Web 预测路径**；调度器路径（`api_app.py:1160` `predict_next(draws, use_llm=True)`，注意**没有传 `llm_required`**）走 `deadline=None` → 单次 60s、共 3 次重试 → **单期最坏 180s×5 次并发采样 + 校验轮**，没有总时长上限，也没有花费上限。
- `LLM_SAMPLES=3`（`config.py:75`）+ `TICKETS_PER_LLM_CALL=5` ⇒ 每期 LLM 调用 = 1（观察）+ 3（采样）+ 0~2（校验）= **4~6 次**。执行位置：每周二/四/日 21:35 后（`config.py:350-351`），由 `_scheduler_loop` 每 1800s 触发一次（`api_app.py:1273` 附近）——**注意 `_scheduler_step` 没有"今天已跑过"的幂等键**，`now.weekday() in DRAW_WEEKDAYS and now.strftime("%H:%M") >= "21:35"` 在 21:35–23:59 之间每 30 分钟都会再进一次（`api_app.py:1141-1146`），仅靠 `if not db.load_predictions(issue)`（`api_app.py:1159`）兜住预测，但 **`_evolution_step()` 与 `evaluate.online_check()` 会重复执行** → **这也是一个未被方案识别的成本泄漏点**（每天最多 3~5 轮 LLM 假说提炼）。这是 P1 级、可在成本报表里立刻暴露。

**冲突点**
1. `data/llm_cost_log.jsonl` 作为**追加写**：需要 `threading.Lock`（现有 `usage_reset` 已经是锁保护的，`llm_client.py:15`），并且**必须在 `chat()` 内部 4 个出口（成功 / 空 content / 异常 / strict 抛出）都记**，否则最贵的情况（超时重试）恰好不被记。方案没写这一点。
2. "自动关停 decision 通道"**与现有 `config.LLM_DISABLED` 语义重叠**。`chat(strict=True)` 在 `LLM_DISABLED` 时抛 `LLMChannelError`（`llm_client.py:147-150`）→ Web 预测**直接 503**（`api_app.py:1140` 注释与 `LLMChannelError` 分支）。**如果关停实现在 `llm_client` 层，效果是"预测失败"而不是"关掉决策通道"** —— 这是最可能的实现事故。
3. 状态存哪里：`data/llm_config.json` 已经是"设置页持久化 + 运行时加载"（`api_app.py:663-690`，实测启动日志 `[config] 已加载运行时 LLM 配置（llm_config.json）`）。**关停状态必须放在这里**，并遵循"设置页保存是唯一事实来源"的既有约定（`api_app.py:646-653`）。
4. 谁执行：唯一有资格执行关停的是**调度器/进化步骤**（它每期跑一次、有 draws、有 `_evolution_step`），**不是** `llm_client`（它不知道自己被谁调用）。方案写"这条要写进代码，不能只是文档"是好的，但需要明确落点：建议新增 `lottery/decision_skill.py`（方案 §3.1 V10-01 已规划此文件）里的 `evaluate_and_maybe_shutdown()`，由 `_evolution_step` 在每期末尾调用，写 `llm_config.json: decision_disabled=true, reason, ci, n, decided_at`。
5. 用户如何看见/恢复：设置页需要一个独立开关（不是 `LLM_DISABLED`），并在预测卡上显示"AI 决策通道由系统自动停用（原因：N=30 期 τ CI 下界 ≤ 随机基线）；[手动恢复]"。

---

### P1-6（额外发现，方案未提及但直接影响 AI 部分的可信度）

**LLM 假说注册到 `patterns_runtime` 后，`engine` 的规则层默认不生效，且即便生效也不作用于 `prefer_*_filter` 类假说。**

- `patterns_runtime.py:290-292`：`apply_enabled()` 返回 `os.environ.get("LOTT_RULE_APPLY","0") == "1"` —— **默认 0**。
- 因此 `engine.py:1704-1711` 的 `_rule_active` 恒为 False，出号完全是概率模型，`rules` 块只做展示。
- 更关键：`apply_rules_to_candidates`（`engine.py:579-648`）只按 **`r.get("dim")`** 做命中加成，不区分假说 `action.type`；而 `usable` 只来自 `hypothesis` 的 `regime.status == "persistent"`（`patterns_runtime.py:110`）。对 `prefer_red_filter` 这类"筛选条件"假说，**"在候选池内部重排"与"条件筛号"是两个语义**，靠 `confidence +0.4*n` 加成（`config.RULE_CONF_BOOST`）近似表达——这在 `usable=0` 的当下看不出来，一旦将来真有 `usable`，会带来"规律到底做了什么"的口径争议。
- **对 AI 部分的意义**：方案 P0-4 说"LLM 候选进候选池要 passed"—— 但在当前代码里，**即便 passed，LLM 候选也只有两条入口**：(a) 走 `llm_only`（会被改成 0）；(b) 走 `apply_rules_to_candidates` 的 confidence 加成（需 `LOTT_RULE_APPLY=1` 且 `usable>0`，**双重不可能**）。⇒ **"pass" 之后的可执行路径根本不存在**。这进一步证明 P0-4 第 2 条是按设计死的。

---

### P2 级（不影响结论，但会误导后续实现）

| # | 问题 | 证据 |
|---|---|---|
| P2-1 | 方案把"abstain 是 AI 的有效输出"写进 UI，但没有定义"没 abstain 也没给出规律"（JSON 解析失败、通道超时）时怎么显示 —— 会与 §3.2 第 4 条"不许静默降级"冲突 | `llm_lab.py:222-237` 通道失败时 `error` 非空但 `llm_used=False`，`run_lab` 仍 `ok: True`（`llm_lab.py:598`） |
| P2-2 | `llm_lab` 的三轮成本不可忽略（每轮 `max_tokens=2400`、`LLM_LONG_TIMEOUT` 180s、`deadline=now+360`），而 `LLM_TOTAL_TIMEOUT=75` 只作用于预测路径 → **闭环按钮单次可跑 3×180s**，成本报表必须覆盖，否则 P1-5 的"每 ¥1 换回多少决策"分母会漏掉最大一块 | `llm_lab.py:228-234` |
| P2-3 | 方案 §1.3 第 4 条用"20 注、红球 1.30、蓝球 0/20"当"AI 没用"的证据 —— 在 n=20 下 p≈0.29，不能支持该结论；正确表述是"样本不足，看不出任何东西" | `rigor.py:641-660` 实测 |
| P2-4 | 方案 §P0-1 第 3 条记录字段有 `kendall_tau` 但没有 `n_options` 的 **并列处理规则**（`llm_lab` 的既有风格是"缺字段即失败"），τ_b vs τ_c 的选择会改变结论；建议显式写 `tau_b` | — |
| P2-5 | `_scheduler_step` 的重复执行成本泄漏（见 P1-5） | `api_app.py:1141-1146` |

---

## 4. 必须前置解决的事项（M1 之前的 gate）

**G1（阻塞项）把 P0-4 第 2 条改为"独立对照臂"，并删除"passed 才能进候选池"。**
- 明确写入：`experiments.source=="llm"` **永不**产生"进入候选池"的权限；LLM 出号只能以 `method="llm:<model>"` 出现在候选池，与 stat/ml 并列，按 G2 的对照臂口径评估。
- 加测试：`build_snapshot` 遇到 `source=="llm"` 的假说时**不产出 usable 规则**（防止死锁以后门复活）。
- 加测试：`experiments.status` 全为 `failed` 时，**LLM 通道在 `eligible` 状态下仍能出号**（证明无死锁）。

**G2（阻塞项）为 LLM 出号定义可执行的对照臂口径，替换"回放"。**
- 不能沿用 `battle.replay_method`（`battle.py:81` 明确不可回放）。
- 口径：同期、同 n_tickets、逐注配对；`llm:*` 的 `eval_details` 对 `stat:*`/`ml` 的 `eval_details`；检验用 `rigor.strategy_significance` 的配对序列模式 + `n_trials` 校正。
- 结论分级必须允许"永远 `eligible` 且默认关"（在真随机世界里这是唯一诚实的稳态）。

**G3（阻塞项）P0-1 的 `decision` 任务重新定义或删除。**
- 若保留：必须记录**确定性基线排序器**的同口径 τ，LLM 只与基线比；`n_options` 必须 ≥3（n=2 时 τ = ±1 抛硬币，见 §3 表）；N 门槛按 |δτ| 反推而非固定 30。
- 若不保留（**本文推荐**）：把 P0-1 换成 §5 的 5 条岗位里任一可机器 100% 判定的任务，AI 部分的"可验证性"立刻成立。

**G4（阻塞项）abstain 契约改为"留出量校验 + 反事实弃权错误率"，并删除"缺三项即失败"。**
- 三项数字**从 prompt 里移除**，改为要求 LLM 自己算；或改用 `next_tests` 的可落地性打分。
- 在错误率估计出来之前，UI **不得**出现"AI 已核对并声明弃权"。

**G5（阻塞项）P1-4 降级或补依赖。**
- 降级为"逐行陈述核对"（可立刻做，30 行即可 100% 判定）；或补"配对对照出号"任务并明确依赖 V10-05 + V10-03。

**G6（阻塞项）成本与关停的落点。**
- `chat()` 内部 4 个出口统一记账（含重试/超时），带 `model` 与 `usage`（需要解析响应 `usage` 字段，现在**完全没读** `data["usage"]`，`llm_client.py:241-251`）。
- 关停状态写入 `llm_config.json`，由 `_evolution_step` 执行；**不得**与 `LLM_DISABLED` 共用（否则 Web 预测 503）。
- 加一条独立开关与 UI 恢复入口；加测试"decision_disabled=true 时预测仍成功、只是不含 decision 通道"。
- 顺手修 `data["usage"]` 未读取与 `_scheduler_step` 幂等键（否则成本报表分母不可信）。

---

## 5. 「LLM 在这个系统里唯一诚实的岗位」——5 条以内，每条都能被机器验证

> 判定标准（对所有条目统一）：**ground truth 由确定性代码给出、无需统计功效、30 行样本即可判定，且"不做"与"做错"必须可区分。**

| # | 岗位 | 如何被机器验证 | 为什么它诚实 |
|---|---|---|---|
| **1** | **确定性结果的"自然语言核对员"**：输入一张已算好的逐行对照表（`eval_details`：method / 期号 / 注号 / 红命中 / 蓝命中 / 奖级 / reward），输出每个 method 一行的结构化摘要 `{method, n, red_mean, blue_rate, best_level}` | 逐字段与后端 `eval.aggregate()`（`evaluate.py:57-`）复算值比对，**允许误差 0**；不一致即 `fail`，并记录哪一字段错。30 行样本 100% 可判，不需要 CI、不需要 N≥30 期 | 这是**算术复述**，不是预测。样本量不影响正确性，只影响表有多长。今天 LLM 的 `overall` 已经在做类似的事（`llm_claims.json` 里它准确复述了 91/4.55/1.0），只是没有被打分 |
| **2** | **契约合规（schema 与可落地性）检查器**：给定 LLM 自己或上次运行的输出，判定其 `filters` 是否全部落在维度表档位内、参数是否合法、组合是否已测 | 直接调 `llm_lab._params_ok`（`llm_lab.py:325`）+ `evolution.discovery_params_ok`（`evolution.py:545`）**并与之逐条对齐**：每个 key 的"可落地/不可落地"必须与"金标准"一致率 ≥ 99%，不一致处必须给出可审计的原因 | 这是一个**纯判定任务**，答案由 `discovery.FAMILY_BY_KEY` 唯一确定。它修复的正是方案 §1.3 第 2 条的"产出无法验证"——但验证的是**格式与可落地性**，不是"规律真假" |
| **3** | **反事实弃权（abstain）的二分类判别器**：在"后端人为注入的强信号"与"纯随机边缘 p"两类输入上判断该不该提规律 | 结果矩阵 2×2（注入强信号 / 真随机）× (提出 / 弃权)，计算**假阴率与假阳率**，与名义 α 及蒙特卡洛容差比较（复用方案 §3.2 第 3 条的"注入偏置必须检出 / 纯随机不得误报"传统，也复用 `tests/` 既有风格）。**只有两个错误率都被估计出来的 abstain 才有价值** | 这是**唯一能在"规律不存在"的世界里保住"诚实"的岗位**：模型的正确行为是"该弃权时弃权、该报警时报警"，而这个"该"由我们构造的注入决定，不依赖开奖 |
| **4** | **用户意图 → 结构化偏好参数**（把用户的一句话翻译成 `config.COVERAGE_OBJECTIVE` / `n_tickets` / `var_ratio` 容忍度等既有参数），**不产生任何号码** | 给定一组自然语言输入与对应"可接受参数集"，检查解析结果是否落在允许的枚举/区间内（`config.py:210-212` 已经有白名单）+ 前端表单能否无歧义回填；**错误 = 越界或必填缺失**，可 100% 判定 | 这是**纯映射**。它不改 EV、不碰号码，出错只会让用户重述一句，代价有界；同时它让 LLM 真正参与"定目标定约束"（方案 §1.5(B) 的本意），却把"能否预测"彻底移出 LLM 的职责 |
| **5** | **（可选，需先有 G2）**：**解释"我们自己的决策"**，即对 `coverage_optimize` / `blue_coverage_plan` 的确定输出做可审计的中文解读 | 解读中出现的每个数字都必须能在 `portfolio_expected_value` / `plan` 的结构体里找到同值字段（数字抽取 + 精确匹配），无法匹配即 `invalid`；同时强制出现"EV 不变"的固定声明 | 它把 LLM 放在"人机接口"位置，而不是"预测器"位置。**产品价值高、可验证性 100%、成本极低**，且天然满足方案 §第四部分的诚实边界 |

**明确排除（不要写进 V10 方案的 AI 岗位）**：① 让 LLM 排序我们自己的候选方案（P0-1，测的是猜规则）；② 让 LLM 做因果归因（P1-4，样本不足 + 共线）；③ 让 LLM 出号并期望它被"显著性验证"（P0-4，规律不存在 → 死锁）；④ 让 LLM 在 30 期样本上"证明"自己有用或无用（P0-1/P1-5 的关停判据）。

---

## 6. 附：本文引用的关键代码位置（便于复核）

| 主张 | 位置 |
|---|---|
| `LLM_ONLY_OUTPUT` 默认 1 | `lottery/config.py:81` |
| `llm_only` 生效条件 | `lottery/engine.py:1715-1720` |
| 默认 0 后的补齐路径 | `lottery/engine.py:1745-1756` |
| LLM 置信度公式 | `lottery/engine.py:1694-1697` |
| 蓝球覆盖计划前置 | `lottery/engine.py:1073-1090` |
| 规律应用默认关闭 | `lottery/patterns_runtime.py:290-292` |
| usable 要求 persistent + validated | `lottery/patterns_runtime.py:110`、`:58` |
| 扫描结果进快照（假说库全量） | `lottery/patterns_runtime.py:99-100` |
| 假说晋升仅靠"支持" | `lottery/hypothesis.py:1501-1507` |
| `p_adj >= alpha` → 无显著差异 | `lottery/hypothesis.py:1413-1417` |
| 最小样本门槛 60 | `lottery/config.py:138`、`lottery/hypothesis.py:1406` |
| llm_lab 上下文含三项扫描数字 | `lottery/llm_lab.py:147-153` |
| 维度表档位校验 | `lottery/llm_lab.py:325-332` |
| llm_lab 三轮成本 | `lottery/llm_lab.py:228-234` |
| 单次调用无 model 返回 | `lottery/llm_client.py:134-297`（`return content` 在 `:251`） |
| 响应 `usage` 未读取 | `lottery/llm_client.py:241-251` |
| usage 记账只有字符 | `lottery/llm_client.py:14`、`:440-449` |
| `LLM_BUDGET_FILE` 是输出预算而非成本 | `lottery/config.py:93`、`lottery/llm_client.py:20-92` |
| 调度器每期 `predict_next(use_llm=True)` 无总预算 | `lottery/api_app.py:1141-1146`、`:1158-1160` |
| `_evolution_step` 每期跑 LLM 提炼 | `lottery/api_app.py:1180`、`:1203-1215` |
| `LLM_DISABLED → strict 抛错 → 503` | `lottery/llm_client.py:147-150`、`lottery/api_app.py:234-236` |
| llm 不可回放 | `lottery/battle.py:53-54`、`:81` |
| MDE 实测 | `rigor._min_detectable_effect`，`lottery/rigor.py:641-660`（n=30 → 40.7%；n=120 → 20.3%） |
| `eval_details` 现状 5 期 / 30 行 | `data/ssq.db` 实测（2026-10-17） |
| 假说库 7 条全 proposed | `data/hypotheses.json` 实测 |
| LLM 原文结论 n_claims=0 | `data/llm_claims.json` 实测 |
