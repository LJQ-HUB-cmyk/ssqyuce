# R3 大模型评审：LLM 在每一层的位置与可测性

> 任务：共享任务板 `task-3`（R3）｜作者：r3-ai-engineer｜日期：2026-10-19
> 代码基准：`/root/DSH/应用/ssqyuce`（评审开始时**已跟踪文件无改动**；本轮未修改任何 `.py`/`.env`/`data/`，唯一写入是本文）
> 数据基准：`data/ssq.db`（3515 期，`2003001`–`2026119`）、`data/llm_cost_log.jsonl`（37 行）、
>   `data/hypotheses.json`（33 条）、`data/llm_claims.json`、`data/rule_lifecycle.json`、`data/llm_budget.json`
> 上游结论（**不重复劳动，直接引用**）：`docs/analysis/LEAD_VERDICT.md` §四、`docs/analysis/P3_AI_ENGINEERING.md`、
>   `docs/reviews/AI_DEMOTION_DESIGN.md`
> 性质：**只读评审**。本文不含任何业务代码改动，所有"应该怎么改"都写成可验收的规格。

---

## 0. 结论（先给判断，再给证据）

**一句话**：用户第 4 点「尽可能发挥大模型在每个位置的作用」在当前系统里的真实障碍**不是模型能力，而是三件事同时成立**：
**(a) 有几个岗位的"金标准"根本不来自确定性代码（出号、衰减复核、因果归因），因此永远测不出来；**
**(b) 有两个岗位的金标准完美（结果叙述、契约检查），但它们既不在调度里、也没有前端入口，`llm_audit` 表 0 行；**
**(c) 剩下能产生"新东西"的岗位（提假设）被验证器用"参数逐字命中维度表档位"误杀，20 条真实产出的 LLM 假说全部停在 `proposed`，且 `source=="llm"` 在快照层被**硬编码否决**。**

三条硬结论：

| # | 结论 | 证据 | 处置 |
|---|---|---|---|
| **H1** | **V11 严格模式是当前最严重的设计退化**：它在 `llm_output_mode="off"` 的情况下，靠 `engine.py:2438` 给 llm 注的**恒定 +20 置信度偏置**，让最近 5 期产出的 25 注中 **24 注来自 LLM 通道**；同时把 `eval_details` 打成"同期只有 llm 行"，使唯一能评估 LLM 的对照臂 `battle.llm_arm_report` 永久停在 `n_pairs=0`。 | §8.2、§3.1 | **建议默认关闭**；若要保留，必须补 shadow 基线臂（§8.4 第 3 条） |
| **H2** | **`patterns_runtime.py:127-142` 的 `source != "llm"` 应被"可回放性"判据替换，而不是删除**。直接删会复活 V10 判定的死锁；改成"LLM 输出必须是确定性代码可重放的规格（spec）"，则死锁解除且有金标准。 | §7 | 给出 R1–R5 判据与 `spec_replay.py` 落点 |
| **H3** | **"可测"与"有价值"目前是错位的**：唯一 0 误差可判分的岗位 A（结果叙述），其**自由文本字段 `notes_zh` 完全不判分**（`ai_audit.py:271` 只校验类型），真正承载产品价值的"叙述"部分反而没有判据。 | §5.1 | 把叙述改成"带数字引用的受核对文本" |

**本轮的显式纠错（对用户第 4 点的直接回应）**：见 §9「不做」清单。用户要求"尽可能发挥"，但**把 LLM 塞进没有金标准的位置不会让它更重要，只会让整个系统的可信度变成不可审计的**。当前系统已经在这条路上走了 5 期（H1）。

---

## 1. 读法与口径

1. 本文区分四件事，**永不混用**：
   - **岗位**：LLM 被要求做什么（§2 清单、§4 矩阵）；
   - **金标准**：谁有资格说"这次做对了"（必须是一段**确定性代码**，同输入同输出）；
   - **判分器**：把 LLM 输出与金标准比对的那段代码（分数范围、阈值、分母口径）；
   - **降级路径**：LLM 不可用时系统是否照常工作（这是硬要求，不是加分项）。
2. 本文所有"可打分"的断言都附 `文件:行号`；所有"当前表现"的断言都附可复算的数据来源。
3. 本文**不主张** LLM 能提高中奖率。凡涉及"效果"的地方一律给出 N / 检出下限 / 区间；给不出的地方写"无法判定"。
4. 本文**未复活** V10 删除台账中的任何一项：无 LLM 自由出号、无 Kendall τ 排序、无选择性推断、无 CUSUM。

---

## 2. 现状：LLM 的全部调用点清单

`grep -rn "llm_client\." lottery/ | grep -v tests` 的完整结果，逐条归类为 **7 个调用点 / 6 个岗位**（另 1 个是设置页探活）：

| # | 岗位 | 调用点（文件:行） | 职责 | 输入 | 输出 schema | 失败降级 | 可打分？ | 谁打分 |
|---|---|---|---|---|---|---|---|---|
| **G1** | **出号候选**（`engine.llm_tickets`） | `engine.py:835`（观察轮）、`:865`（选号轮 × `LLM_SAMPLES`）、`:900`（critique）、`:908`（refine） | 在统计/ML 概率分布 + 结构约束下挑组合；**蓝球最终被代码覆盖**（`engine.py:921-928`） | `compact_stats` + 近 12 期 + 规律 + 红/蓝概率分布 | `{"tickets":[{reds[6],blue,confidence,reasoning,...}]}` | `strict=False` → 空列表（统计兜底）；`strict=True` → `LLMChannelError` → API 503 | **否**（`battle.py:82-84`：llm 不可回放） | 仅在线对照臂 `battle.llm_arm_report`（`battle.py:1097`），当前 `n_pairs=0` |
| **G2** | **规律假设生成（主链路）** | `hypothesis.py:1116`（`_ask_llm`），由 `api_app.py:2281` 每期调度 | 读战绩表 + 12 条统计候选，提 2–5 条可实战假说 | ≤ 5k 字符（战绩 + 统计候选 + 近 12 期；`build_hypothesis_prompt`，`hypothesis.py:1044`） | 5 种动作白名单（`hypothesis.py:1034`）：`boost_method/prefer_numbers/blue_prefer/blue_dedupe/blue_uniform` | 任何异常 → `([], err, [])`，统计假说照常产出 | **半**：`_reject_llm_hypothesis` 是确定性**结构门**，但没有质量分；拒绝原因**不落盘** | 无（拒绝原因丢弃）；晋升靠 `verify_hypothesis_ab`，而 `prefer_numbers` 类**没有维度 key** → `prereg_status="invalidated"` |
| **G3** | **规律假设生成（维度工坊）** | `llm_lab.py:231/250/268`（三轮），手动端点 `api_app.py:2603` | 提假说 → 自我质疑 → 修订 | `build_lab_context`（维度表 + 扫描 + 分年统计 + 检测力提示），`:176` 硬截断 14000 字符 | `{"claims":[{title,filters[{key,params}],mode,...}],"next_dimensions":[],"overall":str}` | 第 1 轮失败即整体 `llm_used=False`；第 2/3 轮失败保留上游结果 | **是**（唯一有确定性判分器的生成岗）：`evaluate_claim`（`llm_lab.py:335`）→ `unmappable/insufficient/noise/regime_only/support/reject` | `discovery.regime_of_filter` + BH；但**判分器被"逐字命中档位"卡死**（`:325` `_params_ok`） |
| **G4** | **结果叙述（result_narrator）** | `ai_audit.py:811`（`run_post`），端点 `api_app.py:3048` | 把已算好的逐注对照表汇总为每 method 一行 | `{window,rows[]}`，行级 `red_hits/blue_hit/level/reward` | `{per_method:[{method,n,red_mean,blue_rate,best_level}],window,notes_zh}` | 任何异常 → `degraded`（**不进分母**） | **是（0 误差）** | `judge_narrator`（`ai_audit.py:215`），金标准 = `evaluate.aggregate`（`:127`） |
| **G5** | **契约合规检查（contract_checker）** | `ai_audit.py:816`，同一端点 | 判定一批 filters 是否落在维度表档位内 | `{items:[{idx,key,params,target}]}` | `{items:[{idx,ok,offending[],reason_zh}],summary{n,n_ok,n_bad}}` | 同 G4 | **是（一致率 ≥0.99）** | `judge_contract`（`:389`），金标准 = `llm_lab._params_ok ∧ evolution.discovery_params_ok ∧ key∈FAMILY_BY_KEY`（`:329`） |
| **G6** | **规律生命周期复核** | `rule_lifecycle.py:1066`，端点 `api_app.py:2713`（默认 `use_llm=False`） | 对确定性状态机给出的 `direction` 出"复核意见"（**不改状态**） | 每条规律的指标卡（`build_llm_payload`；`build_llm_prompt` 在 `:689` 硬截断 16000，真实指标会超） | `{judgements:[{rule_id,verdict,confidence,reason,evidence[]}],…}` | 任何异常 → `used=False`，状态迁移不受影响 | **是**：`skill_stats`（`:793`），基线已改哑判官（`:832`） | 金标准 = `decide_rule_state`；但**调度器从未开启**（`api_app.py:2202` `use_llm=False`）→ `judgements: []` |
| **G7** | **数据异常提名** | `data_audit.py:873`，端点 `api_app.py:3268` | 只"提名"哪段数据/哪个特征看起来不对，**不算任何数** | `build_audit_prompt`（分年统计 vs 全历史基线） | `{claims:[{range,feature,direction,suspicion}]}` | `degraded=True` + `parse_error`，判定层仍可跑（空 claims） | **是（判决层）**：`judge_anomaly` → `real_defect/regime_shift/noise`；但**无跨期累计正确率** | 金标准 = 置换检验 + 两段独立复现（`data_audit.py` 头部定义） |
| — | 设置页探活 | `api_app.py:857-866` | 测通道连通性 | 统计摘要 | `observations` JSON | 返回可读错误 | 不适用 | — |

### 2.1 调用点清单的三个事实

1. **只有 G4/G5 两个岗位的金标准是"100% 机器可判"的**（H3 已说明 G4 的叙述字段除外）。其余 5 个岗位的"金标准"要么不存在（G1/G2 的效果、G6 的判定力），要么被验证器的形状卡死（G3）。
2. **可打分的两个岗位没有任何自动调用点**：`ai_audit.run_post` 只被 `api_app.py:3104` 的手动端点调用，**不在 `_scheduler_step` 里**（对比 `api_app.py:2202`、`2235` 每期都跑 G2/G6）。`llm_audit` 表实测 **0 行**。
3. **不可打分的岗位反而每期在跑**：G1（严格模式下每期）、G2（每期）、G6（每期但 `use_llm=False`）。

### 2.1b "LLM 挂了系统照常工作吗"——逐个回答（这是硬要求）

| 岗位 | LLM 不可用时的系统行为 | 判定 |
|---|---|---|
| G1 | 非严格模式：降级为统计出号（**照常工作**）；**严格模式：503 不出号** | ⚠️ 取决于配置 |
| G2 | 统计假说照常产出，`llm_used=false`（**照常工作**） | ✅ |
| G3 | 工坊返回"仅维度扫描"，出号不受影响（**照常工作**） | ✅ |
| G4/G5 | `degraded` → 报表用纯数字版（**照常工作**） | ✅ |
| G6 | `used=false`，状态机独立推进（**照常工作**，且设计上"AI 存在与否不影响状态"有测试守着） | ✅ |
| G7 | `degraded=true`，判定层拿到空 claims（**照常工作**） | ✅ |

> **只有 G1 在严格模式下违反"LLM 挂了系统必须照常工作"这条硬要求** —— 而它恰好是当前唯一在自动调度里大量调用的岗位。这是 §8 裁决的另一个独立理由。

---

## 3. 现状实测：AI 在真实数据上的表现

> 全部可复算；命令见 §11.4。这一节是本轮评审最"硬"的部分，也是 §8 裁决的依据。

### 3.1 严格模式把输出变成 100% LLM，同时把唯一可测通道打成 `n=0`

`data/ssq.db` 的 `predictions` 表按方法族统计实际出号：

| 期号 | llm | stat | ml | uniform | 合计 |
|---|---|---|---|---|---|
| 2026120 | 1 | 3 | 0 | 1 | 5 |
| 2026119 | 4 | 0 | 1 | 0 | 5 |
| 2026118 | **5** | 0 | 0 | 0 | 5 |
| 2026117 | **5** | 0 | 0 | 0 | 5 |
| 2026116 | **5** | 0 | 0 | 0 | 5 |
| 2026115 | **5** | 0 | 0 | 0 | 5 |
| 2026114 | 4 | 5 | 1 | 0 | 10 |

`eval_details`（与开奖核对后的逐注记录）更直接：

```
2026115: llm=5
2026116: llm=5
2026117: llm=5
2026118: llm=5
2026119: llm=4  ml=1
```

**后果**：`battle.llm_arm_report()` 的配对规则要求"同期两臂都有记录且注数相等"（`battle.py:1114-1116`），而 2026115 起同期根本没有 stat/ml 行 → 实测输出 `n_pairs: 0`、`dropped_ticket_mismatch: 1`、`verdict: insufficient_sample`。

> **只需一次实测**：`STRICT_MODE=1` 上线后，系统**唯一**能回答"LLM 到底行不行"的通道就永久失去分母。这不是"样本还没攒够"，是**结构上再也攒不到**。

### 3.2 20 条真实 LLM 假说全部停在 `proposed`，且结构上永远进不了 usable

`data/hypotheses.json` 实测：

```
33 条；source: stat=13, llm=20
status: proposed=32, rejected=1；prereg_status: 全部 None
动作类型: prefer_numbers=14, blue_prefer=10, prefer_structure=9
```

20 条 LLM 假说的真实形态（抽样，原文照录）：

- `最近12期红球 4/14/22/24 共命中17次（每期1.42个）…高出0.69个/期`，`n_samples=12`
- `最近12期蓝球 4/11/14 各出现2次…高出31.25个百分点`，`n_samples=12`
- `最近12期号码 22 与 23 同现2次(16.7%)，单对理论同现率2.84%`，`n_samples=12`

**判读（必须两面说）**：
- 一面：这些是**对最近 12 期的描述性外推**，样本量 12，全部是"热号叙事"。它们不是新维度，是既有 `hot{window=12}` 的自然语言版本 —— 与 P3 的"臂 B 只复述维度表"结论一致。
- 另一面：**它们进不了 usable 的原因与质量无关，是代码 veto**，三道：

| 门 | 位置 | 对 LLM 假说的效果 |
|---|---|---|
| ① `prereg` 需要 `family_key ∈ discovery.FAMILY_BY_KEY` | `hypothesis.py:1900-1903` | `prefer_numbers/blue_prefer/boost_method` **不带维度 key** → `family_key="llm_prefer_numbers"` → `invalidated`, `prereg_ok=False` |
| ② `_long_term_ok` 要求 `prereg_ok is True` | `patterns_runtime.py:130-133` | ①已 False → `usable=False` |
| ③ `_src != "llm"` | `patterns_runtime.py:127/132/142` | **即使 ①② 都过也被否决**（"llm 不可回放 + 规律本不存在，集合恒为空"） |

→ **这解释了"20 条 LLM 假说、0 条 usable、0 条 rejected"**：它们既没被验证通过，也**没被验证失败**，而是卡在一个不产生任何判决的中间态。系统里没有"LLM 假说被证伪"的记录，因此也无法反馈给下一轮（→ §6.3）。

### 3.3 两个可打分岗位：0 行记录、0 个前端入口

- `llm_audit` 表行数：**0**（`sqlite3 data/ssq.db "select count(*) from llm_audit"`）。
- 前端消费的端点清单（`grep -o "/api/…" web/static/app.js`）**不含**：
  - `/api/llm/audit/latest`、`/api/llm/audit/run`（G4/G5 的分数卡）
  - `/api/llm/cost`（成本报表，`api_app.py:2044`）
  - `/api/llm/arm`（LLM 独立对照臂，`api_app.py:1642`）
  - `/api/audit/anomaly/latest|run`（G7 数据审计）
  - `/api/rules/lifecycle`（G6 复核技能分）
- 前端**有**的 AI 入口：`/api/llm_lab/run/async`（工坊）、`/api/rules/closed-loop/async`（闭环）、`/api/eval/llm/run`（三通道评估）。

> **这就是"优先顺序倒置"的 UI 层证据**：用户能点到的三个按钮，恰好是三个"金标准最弱"的岗位；而金标准最强的两个岗位，接口写完了、测试 35 例（`tests/test_v12_ai_audit.py`），**但没人能看见**。

### 3.4 数据审计：LLM 提名确实有效，但没有跨期正确率

`data_audit.py` 的设计是对的（LLM 只提名、代码判 `real_defect/regime_shift/noise`，`p_value` 与 `p_adj` 成对、`scope` 必填）。缺口只有一处，但很关键：

- 产物只写 `data/data_audit_latest.json`（`data_audit.py:960-976`，**覆盖写**），没有 append-only 历史；
- 因此**"LLM 提名准确率"永远只有 1 个数**（最近一次的 `n_real_defect / n_claims`），不能累计、不能给 Wilson 区间、不能做"关停规则"。

对比：`ai_audit` 有 `llm_audit` 表（仅追加）、有 `scorecard()`、有 `dumb_judge_baseline()`。**G7 缺的正是 G4/G5 已经建好的那套东西。**

### 3.5 历史上的"三族混算"bug：已修，但有残留面

- **已修**：`evaluate.method_breakdown()`（`evaluate.py:167`）按族分列，**显式不返回** `all/total` 跨族字段；`online_check()` 的 summary 里把 `avg_red_hits/blue_hit_rate` 移除并写入 `mixed_note`（`evaluate.py:264-266`）；对应反向测试在 `tests/test_v10_channel_wiring.py:332/362`（`test_method_breakdown_does_not_mix_llm_and_stat` / `…has_no_cross_family_average`）。
- **残留 1（P2）**：`/api/predictions/last` 与 `/api/predictions/history`（`api_app.py:391-432`、`727-753`）对**该期全部方法**算 `evaluate.tickets_result(preds, actual)`，即一行"本期命中 X 红"。2026119 的 4 llm + 1 ml 被混成一个数，`llm_used` 只是一个布尔。→ 用户看到的"本期成绩"仍然是混算值。**这不是 bug，是口径债**：需要在返回体里加 `by_family`（可直接复用 `evaluate._family_block`）。
- **残留 2（P2）**：`predictions` 表无 `family` 列，只能靠 `method` 前缀推断。任何 SQL 层面的"平均命中"都会重犯历史错误。建议加一列或加视图，**并在 CI 里加一条"禁止无 family 分组的命中率聚合"的反向测试**（形态参考 `test_v12_layer_isolation.py`）。

---

## 4. LLM 岗位矩阵（逐层评估）

评分口径（每格必须同时给出）：**可判定性**（机器能否 100% 判对错 0–5）｜**可验证性**（结论能否被确定性代码复算 0–5）｜**金标准从哪来**｜**结论**。

| # | 岗位 | 可判定 | 可验证 | **金标准（谁是对的）** | 结论 |
|---|---|---|---|---|---|
| 1 | **数据质检 / 异常期提名** | 5 | 5 | 置换检验（固定 seed）+ **两段互不重叠复现**；判决词表封闭（`real_defect/regime_shift/noise`） | ✅ **适合**（已实现，补 §6.5 累计分） |
| 2 | **规律假设生成（维度提案）** | 4 | 5 | ① 能否**编译**成 `discovery.make_filter` 可执行对象（纯函数，可 sha1）；② as-of 回放 + BH；③ H0 合成数据上的误报率 | ⚠️ **需改造**（验证器误杀，§6.2） |
| 3 | **规律解释与命名** | 4 | 5 | 解释里的每个数字必须能在 payload 中**逐字节命中**（复用 `ai_audit` 的 0 误差口径）；命名只需"唯一 + 无禁用词 + 长度" | ✅ **适合**（低价值但零风险） |
| 4 | **验证方案设计（设计对照实验）** | 4 | **5** | **规格在 H0 上的类型 I 误差**（目标 α±0.02，2000 次）**+ 注入偏置上的功效**（≥0.8 @ δ=0.15）。金标准是"这个检验的校准曲线"，完全由代码算出 | ⚠️ **需改造**（目前完全没做，但是**最值得新建的岗位**，§5.2） |
| 5 | **冲突裁决 / 规律衰减复核** | **1** | 3 | `decide_rule_state`（确定性），但金标准分布 **99.19% 是 hold** → 哑判官即可 99.19% | ❌ **不适合**（H1 同类：`AI_DEMOTION_DESIGN` §8.3 已承认 `n_action>=10` 现实中到不了） |
| 6 | **出号候选** | **0** | **0** | 无。开奖是独立随机，"LLM 选的号更好"没有可复算判据；`battle.py:82-84` 已判不可回放 | ❌ **不适合**（且严格模式在放大它，§8） |
| 7 | **票单结构决策**（覆盖计划/注数/胆拖） | 4 | 5 | 覆盖数、`var_ratio`、`portfolio_ev` 全部可**精确枚举**（`optimal_portfolio`、`C(33,6)` 全枚举）；但**这些已由确定性求解器解到最优** | ❌ **不适合让 LLM 决定**；✅ 只允许"选择目标函数"（§5.3） |
| 8 | **报告叙述** | 5 | 5 | 复算值（`evaluate.aggregate`），0 误差 | ✅ **适合**（但叙述字段当前不判分，§5.1） |
| 9 | **异常归因（为什么这条规律掉了）** | **0** | 1 | 无。30 注样本上任何归因都只能是 `none`（V10 P1-4） | ❌ **不适合** |
| 10 | **前端交互问答（意图解析）** | 3 | 4 | 受限意图集（≤12 类）的**标注集**上 exact-match；且必须回显结构化参数供用户确认 | ⚠️ **需改造**（当前未实现；只允许受限分类，**不允许自由问答**） |
| 11 | **代码 / 配置自检** | 5 | 5 | 维度表的 `_params_ok ∧ discovery_params_ok ∧ FAMILY_BY_KEY`（G5）；或任意"白名单校验" | ✅ **适合**（但需信息量门槛，§6.4） |

**矩阵的两个读法**：
1. 「✅ 适合」的 4 个岗位（1/3/8/11）有一个共同点：**LLM 的输出是"对已算好事实的重述或判定"**，不是"产生事实"。
2. 「⚠️ 需改造」的 3 个岗位（2/4/10）有一个共同点：**LLM 产生的是一段"规格/意图"，由代码执行**。这也是本文认为唯一还能"扩大 LLM 作用"的方向 —— 因为规格可以被冻结、重放、打分。
3. 「❌ 不适合」的 4 个（5/6/7/9）**全部**是"让 LLM 产生事实或做价值判断"。用户第 4 点的直觉如果落在这里，就是 §9 的"表演"。

---

## 5. 适合 / 需改造岗位的可测定义

> 每岗五件事：**输入 / 输出 schema / 确定性判分器（谁打、怎么打、范围）/ 验收阈值 / 降级路径**。
> 统一纪律：**判分器必须是纯函数、不调 LLM、同输入同输出**；**任何岗位的 LLM 失败都不得改变系统行为**（只影响解释与分数）。

### 5.0 共用约定

```jsonc
// 9. 每个 LLM 调用的必带信封（新增，写入 llm_cost_log.jsonl 与岗位产物）
{
  "post": "dimension_proposer",          // 岗位名（封闭词表）
  "spec_version": "1",                   // 输出 schema 版本，变了就要重跑基线
  "prompt_sha1": "…",                    // sha1(model|system|user|max_tokens|temperature)
  "model": "agent-router/deepseek-v4-flash",
  "degraded": false,                     // true = 没做（不计入分母）
  "verdict": "pass|fail|invalid",        // 三态计分，degraded 单列（沿用 ai_audit 口径）
  "score": 0.0,                          // 判分器给出，范围见各岗位
  "denominator": "n_items"               // 分母口径必须显式，禁止隐式
}
```
理由：`ai_audit.py:16-28` 的四态判定（`degraded/invalid/fail/pass`）与"`degraded` 单列、不进分母"是本项目已被证明正确的口径，**所有新岗位必须复用**，禁止出现第二个计分口径。

---

### 5.1 岗位 8（现状）：结果叙述 `result_narrator`

**现状（已实现）**
- 输入：`build_narrator_input`（`ai_audit.py:685`），**无行数上限**（只有 `limit_issues`）。
- 输出：`{per_method:[{method,n,red_mean,blue_rate,best_level}], window, notes_zh}`。
- 判分：`judge_narrator`（`:215`）—— 四态；`_round4` 后要求**差值 == 0**；`fail_fields` 精确到 `per_method[1].red_mean`。
- 分数：`score = pass/(pass+fail+invalid)`（`scorecard`，`:492`），范围 `[0,1]`，`degraded` 单列。
- **缺陷 1**：`notes_zh` 只校验"是不是字符串"（`:271`）。**一个把表格数字全对、正文写成胡话的输出照样 pass**。而正文才是产品价值。
- **缺陷 2**：输入行数无上限。`limit_issues=200` × 每期每 method ≤5 注 → 最坏 1000+ 行、约 80–120 KB prompt，与 `max_tokens=1200` 的出参预算严重不对称（输入费用吃满、输出被截断）。

**改造规格（P1，可测）**
```jsonc
// 输出（v2）：把"叙述"变成"带引用的断言集合"，仍然 0 误差可判
{
  "per_method": [{"method":"llm:x","n":5,"red_mean":1.2,"blue_rate":0.2,"best_level":"六等奖"}],
  "window": {"from_issue":2015090,"to_issue":2026119,"n_rows":500},
  "notes_zh": "LLM 通道 5 注，红球均值 1.2000，蓝球命中率 0.2000，最好奖级六等奖。",
  "citations": [                          // 新增：正文里每个数字必须在此登记
    {"text_span":"1.2000","field":"per_method[0].red_mean"},
    {"text_span":"0.2000","field":"per_method[0].blue_rate"}
  ]
}
```
**判分器** `judge_narrator_v2`（同文件新增，纯函数）：
| 检查 | 规则 | 失败码 |
|---|---|---|
| V7 | `notes_zh` 中出现的**每一个小数/整数**都必须能在 `citations` 里找到，且 `field` 解析出的金标准值 `round(...,4)` 后与之相等 | `note_number_unsupported` |
| V8 | `citations` 的 `text_span` 必须在 `notes_zh` 中**逐字出现** | `citation_not_in_text` |
| V9 | 禁用词表（"必中/稳赚/提高中奖率/推荐购买"）零命中 | `forbidden_phrase` |

**分数**：沿用 `pass/(pass+fail+invalid)`；**新增分项分数**（0–1，供人看，不进总分母）：
`arithmetic_score`（per_method 表）、`narrative_score = 1 - 无依据数字数/数字总数`。
**验收阈值**：`arithmetic` 必须 `== 1.00`（0 误差）；`narrative_score ≥ 0.95`；`n ≥ 30` 次真实调用后才允许报数（30 次 Wilson 下界 ≈ 0.83，够区分"能用/不能用"，不够区分"好用/很好用"——这一点必须写进 UI）。
**降级**：`degraded` → 报表回退到 `evaluate.method_breakdown()` 的纯数字版，无任何 LLM 文本（**现状已满足**）。
**输入上限**：`rows ≤ 300` 且 `chars ≤ 12000`；超限时**按 method 分块**多次调用（每块独立判分、独立计数），**不允许**静默截断（截断会让"n"错，而 n 是判分项）。

---

### 5.2 岗位 4（新建）：验证方案设计器 `verification_designer`

这是本文认为**最值得新建**的岗位，也是唯一能同时满足"发挥 LLM 作用"与"完全可测"的新增点。

**职责**：给一个数据现象（例如"和值均值偏低 1.02"），输出一个**可执行的检验规格**（不是结论、不是 p 值）。

```jsonc
// 输入
{
  "phenomenon": {"feature":"sum","observed":100.981,"baseline":102.0,"n":3515},
  "allowed_operators": ["one_sample_t","permutation","chi2","hypergeom","bootstrap"],
  "alpha": 0.05, "n_min": 200
}
// 输出
{
  "specs": [{
    "id": "s1",
    "statistic": "mean(sum(draws[:t].reds))",
    "null_model": "permutation",        // 必须取自 allowed_operators
    "alt": "two_sided",
    "n_perm": 2000,
    "exclude": {"eras": []},
    "decision_rule": "p_adj < alpha after bonferroni over m_specs",
    "rationale_zh": "…",                // 不判分，仅展示
    "expected_effect": 1.02             // 可判分：与实际观测比
  }]
}
```
**判分器** `judge_design(input_obj, output_obj)`（纯函数 + 只调用既有确定性代码）：
| 检查 | 规则 | 分数贡献 |
|---|---|---|
| C1 可执行 | `statistic` 能解析为白名单表达式（AST，禁 import/属性访问），`null_model` ∈ `allowed_operators`；否则 `invalid` | 门（不过即 0） |
| C2 **校准**（最关键） | 在 2000 条**合成均匀**数据集上执行该 spec → 类型 I 误差 `fpr`；`0.03 ≤ fpr ≤ 0.07` 得 1 分，否则 `1 - |fpr-0.05|/0.05` 线性衰减到 0 | 0–1 |
| C3 **功效** | 在注入 δ∈{0.05,0.10,0.15} 的合成数据上执行 → `power ≥ 0.8`（每档） | 0–1（三档均值） |
| C4 方向一致 | `expected_effect` 的符号与 `alt` 一致、量级在 `[0.5×obs, 2×obs]` | 0/1 |
| C5 无泄漏 | 表达式中不得出现 `draws[-1]`、`future`、`t+1` 等（正则 + AST 双查） | 门 |

**总分** = `0.4×C2 + 0.4×C3 + 0.2×C4`，范围 `[0,1]`。
**验收阈值**：`≥0.7` 视为"该规格可用"，可进入下一阶段（由人/代码把它加进 `discovery.FAMILIES` 或 `rigor` 的检验族）；`<0.4` 视为"该 spec 不可用"，**记入 rejections**（这是有价值的数据，不是浪费）。
**分母**：每个 spec 一行；`degraded`（LLM 未返回）单列。
**降级**：完全不可用时用"既有检验族 + 既有 BH"作为默认方案 —— **这本来就是当前行为**，所以本岗位是纯增量，零风险。
**成本**：单次输入 ≈2k（现象 + 算子白名单，**不给维度表全文**），输出 ≈1k，2000 次置换 ×3 档 + 2000 条零分布 ≈ 5–20 秒 CPU（离线、闲时跑）。

> 为什么这个岗位特别重要：**它把 LLM 从"产生结论"挪到"产生方法"，而方法的好坏（校准 + 功效）是统计学里唯一有客观答案的东西。** 这正是"发挥 LLM 作用"与"可测"的交集。

---

### 5.3 岗位 7（改造）：票单结构目标选择器 `portfolio_objective_picker`

**不做**：让 LLM 决定号码或注数（那已是确定性最优解，`optimal_portfolio`）。
**只做**：把用户的自然语言诉求映射到**已有的目标函数枚举**。

```jsonc
// 输出
{"objective":"min_var|max_blue_coverage|max_ev|balanced",
 "n_tickets":5,
 "r":3,
 "echo_zh":"你要求『多覆盖几个蓝球、少花钱』→ 目标=覆盖优先，5 注",   // 必须回显
 "confidence":0.8}
```
**金标准**：目标 ∈ 项目已有枚举（`COVERAGE_OBJECTIVE`）且 `n_tickets ∈ [1,20]` 且 `r ∈ [0,6]`；回显文本与所选目标一致。
**判分器**：确定性参数域校验（门）+ **标注集准确率**（20 条人工标注的用户话术，`tests/` 固化为 fixture）。
**阈值**：标注集 exact-match `≥0.90`；参数合法率 `=1.00`（违规即 `invalid`）。
**降级**：解析失败 → 使用当前配置默认值（`n_tickets=config.N_TICKETS`、目标=默认），**不报错、不阻塞**，并在返回体写 `intent_source="default"`。

---

### 5.4 岗位 1（补强）：数据审计的累计分数卡

**现状**：判决正确（`judge_anomaly`），但只存 latest（§3.4）。
**改造**：
1. 新增 `data/data_audit_log.jsonl`（append-only，字段同 §5.0 信封 + `claim/verdict/m_adj/p_adj/scope`）；
2. 新增 `data_audit.scorecard()`：口径完全复用 `ai_audit.scorecard`（`degraded` 单列）；
3. **新增一个真正有信息量的指标**：`nomination_precision = n_real_defect / (n_real_defect + n_regime_shift + n_noise)`，并给 Wilson 区间；`n_claims < 10` 时只报"样本不足"。
4. 关停规则：`n_claims ≥ 10` 且 `nomination_precision` 的 Wilson 下界 < `1/|FEATURES|`（随机瞎猜的命中率）→ 把该岗位标 `display_only`（复用 `methods.set_decision_disabled` 同款机制）。
**阈值**：`n_real_defect ≥ 1/季度`（P3 建议），`n_noise` 占比 **<80%**。
**降级**：见现状（`degraded=True` 照常返回统计层）。

---

### 5.5 岗位 2/3（改造）：维度提案器 + 解释与命名

- **输入**：族名清单（**只给 29 个 `key` + 一句话说明，不给 7k 字符维度表全文**）+ schema + 最近 20 条历史提案及其判决（§6.3）+ 检测力提示（`MDE=0.22`）。
- **输出**：
```jsonc
{"proposals":[{
  "name":"sum_regime_shift",
  "target":"red",
  "selector":{"family":"hot","params":{"window":30,"k":6}},   // 或 {"expr":"mean(sum(t-30:t))"}
  "predicate":{"op":"below","args":{"ref":"theoretical_mean"}},
  "direction":"up",
  "why_not_before":"历史 3 条同族提案（id:…）在 2023 后失效，本次限定近 200 期",
  "rationale_zh":"…"                                           // 不判分
}]}
```
- **判分器**（纯函数）：
  | 检查 | 规则 | 分数 |
  |---|---|---|
  | K1 可编译 | `make_filter`/`combine` 能实例化，且 `spec_hash` 稳定（同输入同 hash） | 门 |
  | K2 不重复 | 与历史 `spec_hash`/语义指纹碰撞率 | 0/1 |
  | K3 引用准确 | `why_not_before` 提到的历史 id 存在且判决描述正确（**0 误差**） | 0/1 |
  | K4 可回放证据 | `regime_of_filter` 跑出 `n ≥ 200`，给出 `delta/p_adj` | 门（否则 `insufficient`） |
  | K5 显著性 | **前置注册**后 BH 校正 `p_adj < α`，且 ≥2 个互不重叠阶段同向 | 0/1 |
- **阈值**：`可编译率 ≥ 0.7`、`重复率 < 0.2`、`引用准确率 ≥ 0.90`、`K5 命中率` **不设阈值**（因为它测的是"世界上有没有规律"，不是"LLM 行不行"——**这一条必须写进 UI**，否则又变成"LLM 交白卷"）。
- **降级**：LLM 全挂 → 只跑 `discovery.discover`（当前行为），`n_proposals=0`，`degraded=true` 单列。

---

### 5.6 岗位 10（新建，受限）：意图解析

只允许**封闭意图集**：`{set_n_tickets, set_objective, set_bet_mode, ask_rules, ask_cost, ask_last_result, unsupported}`。
- 输出：`{intent, args, echo_zh, confidence}`；
- 金标准：20 条标注话术（fixture），对 `intent+args` 做 exact-match；
- 阈值：`≥0.90`；`unsupported` 必须**允许**（不许硬猜）；
- 降级：`unsupported` 或低置信 → 前端展示"没听懂，请用下面的按钮"，**绝不执行副作用**。
- **明确不做**：自由问答（"你觉得下期会开几"）—— 无金标准，见 §9。

---

## 6. 修「设计不让它做」：具体改动清单

> 这一节回应 Lead 基线里"不是模型不够强，是设计不让它做"。每条都给**文件:行 → 改成什么 → 怎么验收**。

### 6.1 P0-1 上下文截断策略（`llm_lab.py:176`）

**现状**：`body = json.dumps(ctx, ensure_ascii=False)[:14000]`。`ctx` 实测 14579 字符，截断把末尾的 **「统计检测力提示」**（正是"教模型别过度自信"的那段）和「可调用的维度族」整段切掉。这是**唯一一处"我们自己把护栏删掉，然后抱怨模型不谨慎"**的代码。

**改成**：分段预算组装，按"信息价值/字符"排序，每段独立上限，**永不在段内截断**（要么整段给，要么整段省）：

```python
# llm_lab.py 新增（纯函数，可单测）
SECTION_BUDGET = [           # (key, max_chars, 缺失时是否致命)
    ("统计检测力提示",        600,  True),    # MDE=0.22：必须完整送达
    ("维度空间大小",          400,  True),    # 已测组合数 / 预期假阳性
    ("可调用的维度族",       1800,  True),    # 只有 29 个 key
    ("扫描统计",              900,  True),
    ("最近开奖",              900,  False),
    ("候选（按|z|排序）",    2500,  False),   # 超预算时只保留前 6 条并标注 truncated
    ("阶段划分",              800,  False),
    ("分阶段统计",           3500,  False),
    ("当前状态快照",         2000,  False),
]   # 合计 ≈ 13.4k，留 0.6k 给 prompt 正文
def assemble_context(ctx, total_budget=14000): ...   # 返回 (body, 每段 used/omitted 报告)
```
**验收（3 条测试）**：
1. 断言 `"统计检测力提示"` 与 `"0.22"` **一定**出现在 `build_user_prompt(ctx,1)` 里；
2. 断言 27 个族 key **全部**出现；
3. 构造超大 ctx（50k 字符）→ 断言 `omitted` 列表非空且 `统计检测力提示` **不在** `omitted` 里。

### 6.2 P0-2 验证器：从「参数逐字命中」到「语义可编译」

**现状**：`llm_lab.py:325-332`
```python
if not fam["params"]: return True
return bool(params) and params in fam["params"]   # 逐字相等
```
后果（P3 实测）：自由臂 5 条带完整操作化定义的假说，**按 LLM 自己的口径 5/5 可实现、2 条 Bonferroni 显著**；按维度表口径 **0/5 可检验**。说"近 25 期"而档位是 `20/30` → 直接 `unmappable`。**这是"验证器在测形状，不是在测内容"。**

**改成三段式**（新增 `lottery/spec_compile.py`，纯函数）：
1. `parse_params(key, params) -> {field: value}`：接受 `{window:25}` 或 `{"window":"近25期"}`（正则抽数）。
2. `snap_to_declared(key, values) -> (tier_params, snaps)`：若该族有声明档位，把每个字段**就近**吸到最近档位，`snaps` 记录 `{field: {from:25, to:30}}`；若字段落在声明**值域**内但不在档位（如 `1..33`），允许保留，标 `free_param=True`。
3. `compile_spec(key, tier_params) -> filter`：`discovery.make_filter`。
**必须同时保留的三条纪律**（否则就是偷开搜索维度、重蹈"扩大规律定义→过拟合剧场"）：
- **D-1**：`snaps` 必须写进 `llm_claims.json`，并在前端显示（"LLM 说 25 期，实际按 30 期检验"）；
- **D-2**：`free_param=True` 的提案，其检验必须**计入 `n_tested`**（BH 分母 +1），不允许白拿一次搜索；
- **D-3**：`unmappable` 的语义收窄为**"无法编译成任何可执行对象"**，不再表示"参数不在档位"。
**验收**：
- `tests/test_v13_spec_compile.py`：把 P3 的臂 A 5 条原文做 fixture → **≥4/5 编译成功**（`h5` 自相关允许失败，因为确实需要新算子）；
- 反向测试：`{"window": 9999}` 必须 `unmappable`（超出值域），不得被"就近吸附"成 30 —— 这是**误杀与放水的分界线**，必须有测试守着。

### 6.3 P0-3 反馈闭环格式：让它知道自己上次错在哪

**现状**（P3 ③ + 本轮复核）：`build_hypothesis_prompt`（`hypothesis.py:1044`）不读 `hypotheses.json`、不读 `llm_claims.json`、不读 `prereg_status`。`_parse_llm_hypotheses` 返回的 `rejected` 被 `api_app.py:2238` 只写进内存 summary，**不入库**（本轮 grep `rejections` → 0 处）。→ 同一份输入、每期都是"初次见到"，模型无法改进。

**改成**（两处，都很小）：
```python
# 1) hypothesis.build_hypothesis_prompt(..., history: Optional[list] = None)
#    history 注入（每条 ≤ 200 字符，最多 20 条，约 4k 字符）：
{"id":"recent_red_hot-1e03a69a","source":"llm","statement_hash":"a1b2",
 "verdict":"rejected|regime_only|noise|proposed","p_adjusted":0.42,"n":12,
 "rejected_reason":"evidence 缺少样本量",     # 新增：以前丢失的那部分
 "not_tried":"可考虑全历史矩、跨维度交互"}
# 2) 输出新增必填字段
"why_not_tried_before": "上一轮 id=… 在 n=12 上被判 noise；本次改用 n=300 的全历史检验"
```
**判分器**（纯函数 `judge_history_use`）：
| 检查 | 规则 |
|---|---|
| H1 引用存在 | `why_not_tried_before` 里提到的 id 必须在注入的 history 里（0 误差） |
| H2 判决正确 | 它对该 id 的判决描述与 history 里的 `verdict` 一致（0 误差） |
| H3 重复率 | 本轮 `spec_hash` 与 history 碰撞率 < 0.2 |

**验收**：`引用准确率 ≥ 0.90`、`重复率 < 0.20`；并在 `data/llm_claims.json` 新增 `rejections[]`（append-only，含 `at/post/item/why`）。
**为什么这是"纯收益"**：它不改变模型能力，只改变"它是否知道上次错了"。当前证据显示它**完全不知道**。

### 6.4 P1-1 契约检查器的信息量门槛（`ai_audit.py:389`）

**缺陷**：`judge_contract` 只要求 `agree ≥ 0.99`（`:63`），**没有要求输入里必须含有非法项**。若 `items` 全部合法（`n_bad=0`），一个"永远答 ok"的哑判官就能拿 100% —— 这次调用是纯表演。

**改成**：
- 计算 `dumb_baseline = n_ok / n`（"永远答 ok"的准确率），与 `ai_audit.dumb_judge_baseline` 同款；
- **`n_bad == 0` 时判 `uninformative`（新增第五态，或复用 `degraded` 但 `reason="no_negative_sample"`），不计入分母，并在 UI 写"本批全部合法，无法检验检查器"**；
- 报告体固定给出 `agree / dumb_baseline / n_bad`，**`agree` 单独展示禁止**（与 `p值无 scope 不得展示` 同一条纪律）。
**验收**：`test_contract_all_valid_is_uninformative`；`test_contract_dumb_judge_baseline_reported`。

### 6.5 P1-2 可观测性三件套（本轮新增发现）

| 缺什么 | 现状 | 改成 |
|---|---|---|
| 拒绝原因 | `rejected` 只进内存 | `data/llm_claims.json: rejections[]`（append-only） |
| prompt 指纹 | 无 | 每次调用记 `prompt_sha1`（§5.0），用于"同一 prompt 重复计费"审计与 A/B |
| 按岗位成本 | `cost_report` 有 `by_purpose`（`engine/lab/evolution/…`），但**无 by_post** | `purpose` 已够用，补 `post` 字段（G4/G5 已传 `purpose="decision"`，会与出号混在一起 → **必须分开**） |

---

## 7. 「可回放」判据：让 LLM 的规律合法进入 `usable`

> 这一节正面回答 Lead 的三个问题：`patterns_runtime.py:125` 那条规则该不该改？改成什么？怎么防止变成后门？

### 7.1 先看它现在为什么存在

`patterns_runtime.py:127-142` 的注释写得很清楚：

> `source=="llm"` 永不 usable：llm 不可回放 + 规律本不存在，"llm 且 passed" 的集合恒为空；放行会让死锁从后门复活（V10 G1）。

这句话在**当时的定义下是对的**，因为当时"LLM 出的东西"= 号码 / 自然语言假说。而 `battle.py:82-84` 的 `NON_REPLAYABLE_METHODS["llm"]` 也说了同一件事：不可能对 3215 期各调一次外部模型。

**所以问题不是"这条规则错了"，而是"它把'LLM 说的任何东西'都归入了不可回放"**。这是一个**过宽的类型判定**：它把"LLM 产生的事实"（不可回放）与"LLM 产生的规格"（可回放）混为一谈。

### 7.2 判据 R1–R5（建议直接写进代码 docstring）

一条由 LLM 提出的规律**可回放**，当且仅当同时满足：

| # | 判据 | 怎么机器检验 | 失败后果 |
|---|---|---|---|
| **R1** | **产物是规格，不是自然语言**：`spec = {kind, family_key|expr, params}`，能被纯函数实例化成执行体 | `spec_replay.compile(spec)` 不抛异常，且返回对象有 `.eval(draws[:t])` | `not_compilable` |
| **R2** | **冻结 + 指纹**：`spec_hash = sha1(canonical(spec))` 落库；此后**只读 spec**，不再问 LLM | 重跑同一请求不会改变 `spec_hash` | `unstable_hash` |
| **R3** | **可重放性**：能在任意历史段逐期出号，且遵守 `battle` 的 5 条回放纪律（`draws[:t]`、按期 seed、零未来信息） | `battle.replay_virtual(spec, draws)` 返回 `per_issue` 且 `test_replay_ignores_future_draws` 通过 | `leaky` |
| **R4** | **可复现**：同一 spec + 同一 draws 跑两次，`eval_details` 逐注**完全相同**（红/蓝/顺序/置信度） | `assert run1 == run2`（bit-level） | `nondeterministic` |
| **R5** | **前置注册**：`exp_id` 在见样本前登记，`n_tested` 计入 BH 分母 | `hypothesis.evaluate_prereg` 返回 `prereg_ok=True` | 不可 `validated` |

**放行规则**：`_long_term_ok` 的 `_src != "llm"` 改为 `_replay_ok(h)`，其中
```python
def _replay_ok(h) -> bool:
    if (h.get("source") or "stat") != "llm":
        return True                     # 非 LLM 假说走原路径，完全不改行为
    return spec_replay.replayable(h)["ok"]   # R1–R5 全过
```
`source` 字段**保留**（审计溯源），但**不再作为否决条件**。

### 7.3 为什么这能解开 V10 的死锁（而不是复活它）

| | V10 死锁形态 | R1–R5 形态 |
|---|---|---|
| 被验证的对象 | LLM 的一次性号码输出 | LLM 写的 **spec**（冻结、可重跑） |
| 能否回放 | 不能（要重新调模型） | 能（代码执行 spec） |
| 显著性检验的分母 | 无（无法构造对照） | 有（spec vs 基线，逐期配对） |
| LLM 不可用 | 结论依赖它 → 死锁 | 结论不依赖它（spec 已冻结） → 不死锁 |
| "LLM 行不行"怎么答 | 答不了 | **可答**：`spec → support/reject` 的命中率，分母是 spec 数 |

**关键区别一句话**：`"llm 且 passed" 的集合恒为空` 这个论断，对"LLM 的一次性输出"成立，对"LLM 产出并被冻结的 spec"**不成立**——因为后者是 `draws` 的确定性函数。

### 7.4 落点与测试

| 文件 | 动作 |
|---|---|
| `lottery/spec_replay.py` | **新建**：`compile(spec) / replayable(hyp) -> {ok,reasons,spec_hash} / replay_virtual(spec, draws)` |
| `lottery/battle.py` | `NON_REPLAYABLE_METHODS` 保持不动（对 `llm` 通道仍不可回放），**新增**虚拟方法 `llmprop:<hash8>` 走 `replay_virtual` |
| `lottery/patterns_runtime.py` | `_src != "llm"`（2 处）→ `_replay_ok(h)` |
| `lottery/hypothesis.py` | `_spec_canonical` 对 `source=="llm"` 且 `R1` 通过者，`family_key` 取 spec 的族 |
| `tests/test_v13_spec_replay.py` | ≥12 例（见下） |

**测试（必须包含的"注入偏置必须检出 / 纯随机不得误报"传统）**：
1. `R1`：把 20 条真实 LLM 假说做 fixture → 可编译者 ≥ 预期（当前动作类型全部不可编译的现状要**显式**断言出来，否则改了什么都不知道）；
2. `R3`：`test_replay_virtual_ignores_future`（改未来 → 结果不变）、`test_replay_virtual_uses_past`（改历史 → 必变）；
3. `R4`：两次回放逐注相等；
4. `R5`：`n_tested` 因新增 spec 而 +1（防"白拿一次搜索"）；
5. **反向**：构造一个 `spec_hash` 相同但语义被改的假说 → 必须拒绝（`unstable_hash`）。

### 7.5 必须提前说清的预期（否则又是一场空欢喜）

即使 R1–R5 全部落地、20 条 LLM 假说全部可回放：**它们仍然大概率全部 `noise`**。理由（P3 §4 + LEAD_VERDICT §2.1）：94 维全扫描 BH 显著 **0/94**，最强候选 `zone_lag{zone:0}` delta=0.0474、BH p=0.162。所以：

> **本改造的收益不是"马上有规律"，而是"第一次让 LLM 的产出拥有分母"。** 在此之前，系统对"LLM 有没有用"的回答是"无法判定"；之后是"0/20，其中 18 条被证伪、2 条证据不足"——**后者才是一个工程系统该给用户的答案**。这句话必须一字不改地写进 UI。

---

## 8. V11「严格模式：必须 LLM+规律出号否则不出号」——现在表现如何、是否保留

### 8.1 代码事实（先纠正一处名不符实）

`.env:36` `LOTT_STRICT_MODE=1`。门控实现在 `api_app.py:186-253`，实际只有**两条**：

| 门 | 代码 | 说明 |
|---|---|---|
| ① L2 结构约束可用 | `shape_constraint_ctx(draws)` 的 7 个字段齐全 | **不是"规律"**：这是组合恒等式（和值/跨度/三区/奇偶/连号），任何随机开奖都满足。注释（`api_app.py:203-211`）自己强调"不得把它叫成可用规律" |
| ② `llm_can_pick == True` | `methods.resolve_channels()`，严格模式强制为 True（`methods.py:592-604`） | 实际等价于"LLM 网关配好了 + 没被 decision_disabled 关掉" |

通过后强制 `regenerate=True`、`use_llm=True`、`llm_required=True`（`api_app.py:287-289`、`339-341`），`engine.llm_tickets(strict=True)` 任何失败 → `LLMChannelError` → **503，不出号**。

**所以严格模式的真实语义是**：
> "LLM 网关可用 **且** 历史数据够算形态约束" → 出号；否则不出号。
> **它并不检验"规律"**（L2 不是规律），**也不检验"LLM 输出的质量"**（只检验"能不能拿到可解析的候选"）。

### 8.2 实测表现（3 条硬数据）

1. **出号几乎 100% 来自 LLM**：2026115–2026119 共 **25 注，24 注 `llm:*`**（§3.1）。
2. **唯一可测通道被打断**：`battle.llm_arm_report()` → `n_pairs=0`、`insufficient_sample`（§3.1）。
3. **它并没有提高"规律"的可用性**：`patterns_runtime` 的 usable 计数不因严格模式变化（严格模式不碰快照）；`data/patterns_runtime.json` 实测 `usable=0`。

### 8.3 为什么会变成"LLM 独裁"（机制溯源）

关键在 `engine.py:2436-2441`：

```python
if t["method"].startswith("llm:"):
    conf = round(20 + 0.45 * (0.5 * t.get("confidence", 50) + 0.5 * em), 1)   # ≈ 50–51
else:
    conf = round(10 + 0.25 * em, 1)                                            # ≈ 10–35
```

- LLM 注的置信度有一个**恒定 +20 的族偏置**（外加 0.45 vs 0.25 的斜率），于是 LLM 注**必然**排在候选池最前；
- `_plan_blue_coverage`（`engine.py` 内）**纯按 confidence 降序**取注 → LLM 注占满名额；
- `llm_output_mode="off"`（`.env` 未设 `LOTT_LLM_ONLY_OUTPUT`，代码默认 off）只控制"是否用 LLM 的实际注数作为输出注数"，**完全不影响力排序**。

实测印证：2026119 的 LLM 注 confidence **50.1–51.3**，`ml` 注 **30.4**。

> **结论**：所谓"V10 诚实默认（LLM 不做主力）"在当前配置下**已被严格模式 + 恒定置信度偏置实质推翻**，而系统界面与文档仍按旧口径叙事。这是一处**必须修的叙事与代码不一致**。

### 8.4 裁决建议：不保留为默认，但不要简单删掉

| 选项 | 评价 |
|---|---|
| A. 原样保留 | ❌ **不可接受**。它使"LLM 行不行"永久不可测（H1），且 24/25 注由不可回放通道决定，与 V10 的裁决直接冲突 |
| B. 直接删除 | ⚠️ 可行但浪费。用户的诉求本身（"必须 LLM 参与"）是可理解的，删掉会引发"你们又降级了 AI"的重复争论 |
| C. **改造为"可回放规律门控"**（推荐） | ✅ 门控改为 `counts.usable_replayable > 0`（§7 的 R1–R5 通过数）+ LLM 可用。**当前该值为 0** → 系统会**不出号** —— 这正是用户要求的语义（"没有规律就不出号"），而且**理由是真实的** |
| D. C + shadow 基线臂 | ✅✅ **推荐落地形态**。即使票单 100% 来自 LLM，也在同一期静默跑一遍 stat 基线并写入 `eval_details`（`method="shadow:stat"`，**不展示、不计入用户票单**），从而保全可评估性 |

**具体建议（按优先级）**：
1. **P0（今天就能做）**：`.env` 去掉 `LOTT_STRICT_MODE=1`（回默认 0）；`engine.py:2438` 的 `+20` 与 `:2441` 的 `+10` 改为**同一公式**（族偏置设为 0 或可配置且默认 0）。理由是这两处组合在一起会让"任何对照实验"失效，而对照实验是唯一能回答用户问题的工具。
2. **P1**：按 §7 落地 R1–R5，把严格模式的门控从 `llm_can_pick` 换成 `replayable_usable > 0`；UI 文案改为：
   > 「严格模式：只有**通过前置注册且可被确定性代码重放**的规律存在时才出号。当前可重放规律 0 条，因此本期不出号。」
3. **P1（D 方案）**：`shadow_arm=True` 时同步记录基线臂，保证 `llm_arm_report` 永远有分母。
4. **P2**：在 `docker-compose.yml` / `.env.example` 里对 `LOTT_STRICT_MODE` 加一行警告：**开启后 LLM 出号占比会接近 100%，请同时开启 shadow 臂，否则本系统将无法评估 LLM**。

---

## 9. 「不做」清单：不可验证的表演

> 用户要求"尽可能发挥大模型作用"。**这一节是本文最重要的纠错**：下面 10 项都能"看起来在发挥大模型作用"，但会让系统可信度下降。判定标准只有一条：**这次调用是否产生一个能被确定性代码判对错的产物？** 不能 → 不做。

| # | 不做的用法 | 为什么它是表演 | 现有痕迹（要清理/改口径的地方） |
|---|---|---|---|
| **N1** | **让 LLM 自由出号 / 排序我们的候选** | 无金标准；`battle.py:82-84` 已判不可回放；测出来的只是"像不像后端规则" | `engine.py:780-940`（保留，但见 §8.4：去偏置） |
| **N2** | **让 LLM 判定规律衰减并影响状态** | 金标准 99.19% 是 hold → 哑判官 99.19%；`n_action≥10` 现实中到不了（`AI_DEMOTION_DESIGN` §8.2 自认） | `rule_lifecycle.py:1052`（已 `use_llm=False`，**保持**） |
| **N3** | **让 LLM 做因果归因 / 机理叙事并作为结论** | 30 注样本上任何归因都只能是 `none`（V10 P1-4）；"机制"不可证伪 | `llm_lab` 的 `mechanism` 字段、`expectation` 字段：**只允许展示，禁止入库、禁止判分、禁止当结论**（当前已如此，需在 UI 加"以下为模型自述，未经检验"） |
| **N4** | **让 LLM 算统计量 / 给 p 值** | LLM 算的数不可复核；一旦让它算，后面所有判定失去锚点 | `data_audit.py` 头部已明令禁止（正确）；`llm_lab` 的 `expectation.delta` 是**它自己的预期**，不得回填为观测值 |
| **N5** | **用 LLM 的自评 `confidence` 当概率** | 实测 LLM 注 `confidence` 集中在 50.1–51.3，是**常量**不是概率；且 `engine.py:2438` 又给它 +20 | `engine.py` 置信度公式；UI "置信度"文案已声明"结构分，不构成中奖概率"（保留） |
| **N6** | **让 LLM 生成"提高中奖率"文案 / 购买建议** | 违反项目 §7 与 V10 §3.2.1 禁令；`test_v12_l2_exact.py` 已有 grep 反向测试 | 继续沿用该测试手法，扩展到所有 AI 报告 |
| **N7** | **让 LLM 直接写 DB / schema / 迁移 / 配置** | 不可回滚、不可审计；一次幻觉就是数据事故 | 无（当前没有，**明确禁止新增**） |
| **N8** | **在 30 注 / 60 期样本上判"LLM 有没有用"** | 300 注时 MDE_red=0.1404；要检出 0.05 增量需 473 期（P3 §1④）→ 测不出来还要下结论 = 假证据 | `llm_eval` 的报告口径：必须**同时**打印 N / MDE / Wilson 区间，`verdict` 只能是 `insufficient_sample`（`battle.llm_arm_report` 已正确，保持） |
| **N9** | **把"LLM 参与率 / 调用次数 / 按钮常亮"当 KPI** | 这是最容易发生的一种：用户看到"AI 已参与"就以为有用 | 前端 3 个 AI 按钮（§3.3）；建议每个按钮旁固定显示该岗位**最近的分数卡**或"尚无判分记录" |
| **N10** | **自由自然语言问答（"下期会开什么"）** | 无金标准、无边界、必然产生幻觉且被当成预测 | 只允许 §5.6 的受限意图集 |

**一句话总结这一节**：LLM 的合法岗位不取决于"它有多聪明"，而取决于"有没有一段代码有资格判它对错"。**没有金标准的调用，不是"多发挥了一点作用"，而是给系统加了一段无法审计的行为。**

---

## 10. 成本与稳定性

### 10.1 现状（config ↔ 实际值）

| config 项（`config.py` 行） | 默认 | 本机实际（`.env`） | 作用 | 风险 |
|---|---|---|---|---|
| `LLM_DISABLED`（54） | `0` | 未设 → `0` | 总开关 | 与 `decision_disabled` 语义不同（前者导致 503，后者照常出号）——已有文档区分，保持 |
| `LLM_MODEL_LIST`（63） | `[LLM_MODEL]` | **单模型** `agent-router/deepseek-v4-flash` | 多模型轮转容错（`engine.py:818-827`） | **容错实际不存在**：轮转只有 1 个元素，主通道挂 = 全挂。建议配 2 个 |
| `LLM_SAMPLES`（76） | `3` | **`1`** | 选号轮并发数 | 省成本正确；但"自洽投票"能力为 0（本来就测不出差别，可接受） |
| `LLM_MAX_TOKENS`（101） | `32000` | `32000` | 输出预算上限 / 推理模型升级目标 | **单次成本主因**：实测 engine 平均 `out≈7024` token/次 |
| `LLM_TIMEOUT`（93） | `60` | `60` | 单次尝试超时 | — |
| `LLM_LONG_TIMEOUT`（96） | `180` | `180` | 大预算/升级后的超时 | 与 `LLM_TOTAL_TIMEOUT` 的关系要看清：`chat()` 用 `deadline` 钳制，见下 |
| `LLM_TOTAL_TIMEOUT`（108） | `75` | **`180`** | 预测阶段墙钟预算 | 180s 已超 Cloudflare 100s 代理超时（注释里 75 的理由）→ **本机配置与设计意图矛盾**，会让长预测变成 524 |
| `LLM_VERIFY_ENABLED`（376） | `1` | **`0`** | 第三轮 critique/refine | 关掉省钱；但也关掉了唯一"自我复核"环节（该环节无判分，见 §4 #5，关掉合理） |
| `LLM_EVAL_ISSUES/TICKETS`（370-371） | 60 / 5 | 60 / 5 | 三通道离线评估 | MDE 0.1404 ≫ 真实效应（P3）→ 该评估结构上只能得出"无差异" |
| `STRICT_MODE`（85） | `0` | **`1`** | 严格模式 | **§8 的 H1** |
| `LLM_ONLY_OUTPUT`（92） | `0` | 未设 | 是否用 LLM 实际注数出号 | 实际被 `confidence` 偏置绕过（§8.3） |

### 10.2 重试 / 超时 / 并发的真实行为（`llm_client.chat`，`:451`）

| 机制 | 实现 | 备注 |
|---|---|---|
| 尝试次数 | `while attempt < 3` | 每次尝试都被 `deadline` 钳制（`eff_timeout = min(timeout, remaining)`） |
| 瞬态重试 | 429/5xx → 退避 `2/4/6s`，**不消耗 attempt**，上限 3 次 | 合计最多 12s 退避 |
| 推理型模型兜底 | `content` 空 + 有 `reasoning_content` → 注入 `thinking:disabled` + 把预算抬到 `LLM_MAX_TOKENS`，**重试 1 次** | `test_v3_llm_budget.py` 锁定；`data/llm_budget.json` 已学到 `32000`，后续直接起步 |
| 读超时兜底 | `ReadTimeout` → 同样抬预算 + 重试 1 次 | — |
| 硬顶 | 单次 `chat()` 总耗时 > 600s 放弃 | 防上游挂起 |
| 并发 | `engine.llm_tickets` 用 `ThreadPoolExecutor(min(n,4))`；本机 `LLM_SAMPLES=1` → 串行 | — |
| **缓存** | **无**（只有 `learned_max_tokens` 的预算缓存） | 同 prompt 重复调用**重复计费** |
| 批处理 | 无（每次调用一个问题） | `contract_checker` 天然支持批量（一次 20 条） |

`tests/test_v3_llm_budget.py`（9 例）锁定了上表最容易回归的三条：① 设置页保存的模型是**唯一事实来源**（`LLM_MODEL_LIST` 被替换而非前插，旧模型不会再混进观察轮抢占报错）；② 观察轮全部模型失败时，报错**聚合所有模型**的原因并以主通道开头；③ "只返回推理内容、无正文"时把输出预算一次抬到 `LOTT_LLM_MAX_TOKENS` 并**记住**该模型可行预算。**新增的 §10.4 建议（缓存、输入上限、模型双配）都必须在这个测试文件里补对应用例**，否则会重演"设置页测试通过、真实预测 404"的故障模式。

### 10.3 实测成本（`data/llm_cost_log.jsonl`，37 行）

| purpose | 次数 | 成功/失败 | in tokens | out tokens | 费用（元） | 均次费用 |
|---|---|---|---|---|---|---|
| `engine` | 31 | 23 / **8** | 127,667 | 217,763 | **2.4871** | ≈0.080 |
| `hypothesis` | 3 | 3 / 0 | 5,859 | 27,109 | 0.2374 | ≈0.079 |
| `probe` | 3 | 1 / 2 | 35 | 23 | 0.0004 | — |
| **合计** | 37 | 27 / 10 | 133,561 | 244,895 | **≈2.725** | — |

读法：
- 一次 Web 预测（观察轮 + 1 次选号轮，`LLM_VERIFY=0`）≈ **0.16 元**；若开 `LLM_SAMPLES=3` + 校验轮 ≈ **0.4 元/次**。
- `engine` 失败率 **8/31 = 26%**，全部是"推理内容吃满预算/空正文"类，靠抬预算重试救回一部分 —— **这是 `LLM_MAX_TOKENS=32000` 的根本原因，也是最贵的一项**。
- `hypothesis` 每次 `out≈9000` token（1800 的预算被学到的 32000 覆盖 → 见 `llm_client.py:527-536` 的 `_learned` 逻辑），**"小请求"实际上并不小**。这是成本被低估的地方。

### 10.4 建议（按性价比排序）

1. **P0 加"确定性缓存"**：key = `sha1(model|system|user|max_tokens|temperature)`，只对 `temperature == 0.0` 的调用启用（G4/G5/G7 全是 0.0），TTL 7 天，落 `data/llm_cache/`。**理由**：这三个岗位的输入是"同一份已算好的表"，重复跑必然同结果；缓存后重跑/审计成本 ≈ 0。
2. **P0 给 `narrator` 输入加上限**（§5.1），防止 1000 行 prompt 打满输入预算。
3. **P1 `LLM_TOTAL_TIMEOUT` 回到 75**（或至少 ≤90），与代理超时对齐；`LLM_LONG_TIMEOUT` 留给离线岗位（lab/audit，不受 HTTP 代理约束）。
4. **P1 配第二个模型**（`LLM_MODEL_LIST` 两个），让 `obs_rotation` 真正起到容错作用。
5. **P2 小请求不要继承大预算**：`_learned` 只对 `max_tokens >= 1000` 生效（已有），但 `hypothesis` 传 1800 → 被抬到 32000。建议对一个岗位设**输出硬上限**（`hypothesis` 1200/条、`data_audit` 1200、`narrator` 1200），超过就是设计错误而非"推理需要"。
6. **P2 批处理**：`contract_checker` 一次 20 条（`build_contract_input` 已支持），不要逐条调。

---

## 11. 实施顺序与验收

### 11.1 阶段划分（每阶段都可独立回滚；本轮只出文档，不动代码）

| 阶段 | 内容 | 依赖 | 可回滚点 | 人日（1 人 = 1 天） |
|---|---|---|---|---|
| **S1（0 风险，先做）** | ① 关严格模式；② 去 `confidence` 族偏置；③ `narrator` 输入上限；④ `contract` 信息量门槛；⑤ `.env` timeout 修正 | 无 | 各自一行 config/常量 | **1.5** |
| **S2（可观测性）** | ⑥ `rejections[]` 落盘；⑦ `prompt_sha1`；⑧ `data_audit_log.jsonl` + scorecard；⑨ 给 4 个 AI 岗位加前端分数卡（含"尚无判分记录"） | S1 | 新增文件/接口，后端先给 JSON | **3** |
| **S3（修"不让它做"）** | ⑩ `assemble_context` 分段预算；⑪ `spec_compile` 语义校验 + snaps 落盘；⑫ `history` 注入 + `why_not_tried_before` | S2（要有 history 数据） | 每个都是新增函数 + 开关（`LOTT_LLM_CTX_V2=1` 等） | **4** |
| **S4（可回放）** | ⑬ `spec_replay.py` + `battle` 虚拟方法；⑭ `patterns_runtime` 门控替换；⑮ 严格模式门控改为 `replayable_usable > 0` | S3 | 门控有开关，可一键回 `_src != "llm"` | **5** |
| **S5（新增岗位）** | ⑯ `verification_designer`（校准 + 功效）；⑰ 受限意图解析 | S4（需要 spec 体系） | 新模块，不接主链路 | **4** |

**真实并行度**：S1 与 S2 可并行（2 路）；S3 内部 3 项可并行但**都改 prompt，容易冲突**，实际按 1.5 路估；S4 是串行（⑬→⑭→⑮）；S5 可与 S4 并行（2 路）。合计 ≈ **17.5 人日**，若 2 人并行 ≈ 11 个工作日（**保守按 12–13 天排**，历史上曾把 2 路误报为 4 路）。

### 11.2 必须新增/修改的测试（每项都要"注入偏置必须检出、纯随机不得误报"传统）

| 测试文件 | 例数 | 关键断言 |
|---|---|---|
| `tests/test_v13_ai_ctx_budget.py` | ≥5 | 检测力提示必在 prompt；27 族 key 全在；超大 ctx 不切关键段 |
| `tests/test_v13_spec_compile.py` | ≥8 | 臂 A 5 条 ≥4/5 可编译；`window=9999` 必须 `unmappable`（**不得**被吸附）；snaps 落盘 |
| `tests/test_v13_history_feedback.py` | ≥5 | `why_not_tried_before` 引用准确率（0 误差）；`rejections[]` append-only；重复率统计正确 |
| `tests/test_v13_spec_replay.py` | ≥12 | R1–R5 逐条；`ignores_future` / `uses_past` 双向；两次回放逐注相等 |
| `tests/test_v13_verification_designer.py` | ≥8 | H0 合成数据 2000 次 → 类型 I 误差 ∈[0.03,0.07]；注入 δ=0.15 → power ≥0.8；AST 禁 `draws[-1]` |
| `tests/test_v13_ai_scorecard.py` | ≥6 | `degraded` 单列；`n_bad=0` → `uninformative`；哑判官基线正确 |
| 回归 | — | `pytest -q` 全量 0 failed；`compileall`；`git diff --check` |

### 11.3 验收（用户可感知的三条）

1. 首屏的每个 AI 入口旁都有**一个数**：要么"对 X / 错 Y / 未取得 Z"，要么明确写"尚无判分记录"。**不允许只有按钮没有分数。**
2. 严格模式下必须永远存在一个可评估的基线臂（shadow），`/api/llm/arm` 的 `n_pairs` 每期 +1。
3. `data/llm_claims.json` 里能查到"LLM 提了 N 条、被证伪 M 条、证据不足 K 条"——**有分母的 0 才叫结论**。

### 11.4 本文所有实测的复现命令

```bash
cd /root/DSH/应用/ssqyuce
# 1) 出号族分布 / eval_details 分布
.venv/bin/python - <<'PY'
import sqlite3; c=sqlite3.connect('data/ssq.db'); c.row_factory=sqlite3.Row
for r in c.execute("select issue,method,count(*) n from predictions group by issue,method order by issue desc limit 20"): print(dict(r))
for r in c.execute("select issue,method,count(*) n from eval_details where issue>='2026110' group by issue,method"): print(dict(r))
print("llm_audit:", c.execute("select count(*) from llm_audit").fetchone()[0])
PY
# 2) LLM 对照臂（应输出 n_pairs=0 / insufficient_sample）
.venv/bin/python -c "from lottery import battle; print(battle.llm_arm_brief(battle.llm_arm_report()))"
# 3) 假说库构成
.venv/bin/python -c "
import json,collections; d=json.load(open('data/hypotheses.json'))
print(collections.Counter(h.get('source') for h in d['hypotheses']))
print(collections.Counter((h.get('status'),h.get('prereg_status')) for h in d['hypotheses']))"
# 4) 成本
.venv/bin/python -c "
import json,collections; rows=[json.loads(l) for l in open('data/llm_cost_log.jsonl') if l.strip()]
g=collections.defaultdict(lambda:[0,0,0.0])
for r in rows: g[r.get('purpose')][0]+=1; g[r.get('purpose')][1]+=r.get('tokens_out') or 0; g[r.get('purpose')][2]+=r.get('yuan') or 0
print(dict((k,{'n':v[0],'out':v[1],'yuan':round(v[2],4)}) for k,v in g.items()))"
# 5) 门控与偏置（只看，不改）
grep -n "source\|_src != \"llm\"" lottery/patterns_runtime.py | sed -n '1,10p'
sed -n '2436,2442p' lottery/engine.py
grep -n "STRICT_MODE\|LLM_ONLY_OUTPUT" .env lottery/config.py
```

---

## 12. 诚实边界（全文生效）

1. 本文**不主张** LLM 能提高中奖率。§7 的改造目标是"让 LLM 的产出可被证伪"，不是"让 LLM 变准"。
2. 本文**没有**做新的 LLM 真实调用（成本纪律）；所有关于 LLM 行为的引述来自 P3 的真实调用记录（`/root/DSH/分析/team/p3/exp3_llm_raw.json`）与仓库内的 `data/*.json`。
3. §5.2 的 `verification_designer` 是**提案**，未实现、未跑过真实数据；其阈值（类型 I ∈[0.03,0.07]、power ≥0.8）是统计学的常规标准，不是本项目的实测值。
4. §8 的"24/25 注来自 LLM"是**已发生的事实**（`predictions` 表），但样本只有 5 期，**不能**据此推断"LLM 的号更差或更好"——只能推断"系统当前无法回答这个问题"，而这正是本文的核心论点。
5. 本文**未**修改任何 `.py`、`.env`、`data/` 文件；唯一写入是本文。
6. 仓库内 `data/llm_config.json` 的 `api_key` 是脱敏占位符（`{{API_KEY_…}}`）；若部署时未注入真实值，严格模式会直接 503 —— 这是"严格模式让系统出不了号"的一个现实触发路径，运维文档里应写明。
