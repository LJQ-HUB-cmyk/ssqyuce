# S5 设计：AI 判断规律衰减 → 退居二线（monitor）

> 任务：`task-16`（S5）　作者：r3-ai-engineer　日期：2026-10-18
> 用户诉求：「用 AI 来判断哪些规律可能掉下去，适时把这些规律预测退居二线，只观察不使用了。」
> 状态：**设计待 Lead 确认，确认后才动代码**。本轮只写本文档，未修改任何 `.py`。
> 相关：`docs/reviews/R3_AI_REVIEW.md`（AI 岗位边界）、`lottery/multiscale.py`（Lead 新写）、`lottery/patterns_runtime.py`（tier 分级）、`lottery/regime.py`（阶段机）

---

## 0. 一句话结论

**「AI 判断衰减」不能是 AI 的自由心证，只能是"对已算好的、可检验的统计事实做陈述与核对"。**
因此本设计的核心是一个**三层结构**，AI 只占第三层，且**任何一层都能被关掉而系统照常运行**：

```
① 统计层（金标准，纯规则、无 LLM）      → 每期的衰减证据表（可复现、可审计）
② 生命周期层（确定性状态机 + 滞后）      → active / monitor / retired 的实际切换
③ AI 层（只能"读表说话"，不产生动作）    → 归因、预警、解释；每个断言被后端逐条核对
```

**关键决定**：`verdict ∈ {keep, demote, upgrade}` 中的 **`demote`/`upgrade` 不改变任何状态**；状态只由 ② 的确定性状态机决定。AI 的输出用于**归因准确率度量**与**人读的解释**。
理由见 §2.3：如果把动作权交给 LLM，就必须有一个"AI 判定通过才 demote"的门，那正是 V10 踩过的死锁（约束 2），而且 R3 评审已证明 LLM 判断无法被验证到"可以改变行为"的强度。

---

## 1. 先回答一个前置问题：这里的「AI」指什么？

用户说的是「用 AI 来判断」。本系统里有两种"AI"：

| | 能否判定"这条规律在衰减" | 依据 |
|---|---|---|
| **统计判据（可检验）** | 能，但**只能在有检出能力的窗口上判** | `multiscale.scale_scan` 每档给 `delta/z/p_raw/mde_abs/credible`；`regime.regime_report` 给逐阶段 delta/p 与近窗加权检验 |
| **LLM** | 不能独立判定；只能**复述/归因已算好的数字** | R3 评审 §3.P1-4：30 注样本上任何归因都只能是 `none`；LLM 在独立随机数据上无信息 |

**所以本设计给出的答案是：用户的"AI 判断"落成"统计判据 + AI 解释/归因"两条腿。**
统计判据负责"该不该退"，AI 负责"为什么退、哪个维度在退、下一期盯哪里"，并且 AI 的每一句断言都被机器核对（§4）。
**这不是把用户的要求打折**——恰恰相反：只有这样做，"退居二线"的决定才是可复核的；否则退不退取决于模型今天心情，用户第二周就会发现状态在乱跳。

同时必须承认一个现状：**当前系统里 `usable` 永远是 0**（`data/patterns_runtime.json` 实测 `counts = {usable_long_term: 0, usable_phase: 0, monitor: 17, rejected: 0}`）。
这意味着：**本期没有"在一线的规律"可退**。本机制现在的实际作用是（a）把 17 条 monitor 明细化、给出"哪几条在往坏的方向走"；（b）**为将来第一次出现 usable 时，退场通道是现成的**。
这一点必须写进 UI，否则用户会以为"AI 判退了 3 条"而实际那 3 条本来就没在用（见 §7 的诚实表述）。

---

## 2. AI 的输入、输出与机器校验

### 2.1 输入（`build_payload()` 产出的结构化指标，全部可复现）

对**每条**规律（`patterns_runtime` 快照里的 `rules[]`，含假说库来源与 `scan:` 来源），算一张指标卡：

```jsonc
{
  "rule_id": "repeat_confirmed-7c64d3c0ca",
  "dim": {"key": "repeat", "params": {"back": 1}},
  "target": "red",
  "tier_now": "monitor",               // 当前 tier（来自不可变的快照，不重算）
  "usable_now": false,
  "claim": {"delta": 0.121, "p_adjusted": 0.5997},   // 该规律"当初为什么被提出"的原始效应
  "scales": {                          // multiscale.scale_scan（5 档）
    "all":    {"n": 3315, "delta": -0.0073, "z": -0.49, "p_raw": 0.62, "mde_abs": 0.0422, "credible": false},
    "long":   {"n": 150,  "delta": 0.1024,  "z":  1.46, "p_raw": 0.14, "mde_abs": 0.1985, "credible": false},
    "medium": {"n": 300,  "delta": 0.0791,  "z":  1.66, "p_raw": 0.10, "mde_abs": 0.1404, "credible": false},
    "mid":    {"n": 60,   "delta": 0.0758,  "z":  0.71, "p_raw": 0.48, "mde_abs": 0.3139, "credible": false},
    "short":  {"n": 40,   "delta": 0.0591,  "z":  0.45, "p_raw": 0.65, "mde_abs": 0.3844, "credible": false}
  },
  "eras": [                            // regime.regime_of_filter().eras（逐阶段，仅展示 n>=60 的段）
    {"era": "P22 2024-2025", "n": 150, "delta": 0.062, "p_value": 0.341, "p_adjusted": 1.0},
    {"era": "P23 2025-2026", "n": 150, "delta": 0.076, "p_value": 0.266, "p_adjusted": 1.0},
    {"era": "P24 2026",      "n": 65,  "delta": 0.109, "p_value": 0.284, "p_adjusted": 1.0}
  ],
  "recent": {"n": 150, "n_eff": 130.4, "delta": 0.121, "p_value": 0.101, "p_adjusted": 1.0, "mde": 0.208},
  "rolling": [                         // 确定性算出的滚动 60 期 delta 序列（含期号，共 5 点）
    {"issue": "2026060", "delta": 0.183}, ..., {"issue": "2026118", "delta": 0.076}
  ],
  "lifecycle": {"state": "active", "since": "2026101", "pending": {"direction": "demote", "streak": 1}, "history_n": 3}
}
```

**输入的三条硬性质**（缺一不可）：
1. **全部可由同一份数据重算**：给定 `draws` 与 `rule_id`，任何人跑 `build_payload` 得到同一张卡（`multiscale` 与 `regime` 都是确定性代码，无随机种子）。
2. **不含未来信息**：所有窗口都是 `draws[:t]` 尾部窗口，`regime` 本身是 as-of。
3. **指标已带"检出能力"**：每档都带 `mde_abs` 与 `credible`——这是禁止 LLM 把"短窗口 p<0.05"当证据的制度性设计（防的就是 R3 评审里"抄三个数字"式的假诚实）。

### 2.2 输出 schema（LLM 唯一被接受的形状）

```jsonc
{
  "rule_id": "repeat_confirmed-7c64d3c0ca",
  "verdict": "keep",                  // "keep" | "demote" | "upgrade"  —— 注意：不直接改状态
  "confidence": 0.6,                  // 0~1，必须落在其后端算出的 [lo, hi] 区间内（见 §3）
  "reason": "全部 5 档均未达显著（|z|≤1.66），最近 3 段效应为正但幅度低于该段检出下限 0.198；无衰减迹象",
  "evidence": [                       // 每一条必须能在输入里逐字命中
    {"field": "scales.medium.p_raw", "value": 0.0975},
    {"field": "eras.P23.delta", "value": 0.0758},
    {"field": "recent.p_adjusted", "value": 1.0}
  ],
  "watch_next": ["eras.P24"]          // 可选：下期该盯哪个窗口
}
```

批量形式：`{"judgements": [ ... 上述对象 ... ]}`。**每条规律最多一条判断**，重复即整批失败（见 §4）。

### 2.3 为什么 `verdict` 不直接产生动作（这是本设计最关键的一条）

如果 `demote` 直接生效，就必须回答"LLM 说了 demote 要不要照办"。三个可能的答案都不可接受：

| 方案 | 后果 |
|---|---|
| 照办 | 状态由 LLM 自由裁量决定 → 违反 R3 结论「AI 不得自由判断」；且 LLM 不稳定性会让状态抖动（违反约束 4） |
| 必须 LLM 同意才 demote | 死锁：LLM 不可用/判 keep 时，一条确实在衰减的规律**永远退不下来**（违反约束 2） |
| **必须 LLM 同意才 upgrade** | 死锁的另一种形态：一条恢复的规律**永远升不回来**（同样违反约束 2） |

因此本设计：**状态迁移只由 ② 的确定性状态机产生**；`verdict` 是"AI 对确定性判定的复核意见"，用于
(a) 计算 `llm_demotion_skill`（归因准确率，可度量、可关停）；
(b) UI 上给出一条人话说明；
(c) **发现 `llm_demotion_skill` 长期显著低于基线时，把 AI 层自动降级为"只展示、不调用"**（关的是 AI，不是规律）。

---

## 3. 金标准：确定性判据（`decide_rule_state`）

这是整个机制的**唯一权威**，实现在 `lottery/rule_lifecycle.py`，**零 LLM 依赖**。

### 3.1 判据定义

输入：该规律的 `payload`（§2.1）。输出：`{"direction": "upgrade"|"demote"|"hold", "reasons": [...], "evidence": {...}}`。

**三条判据（任一满足即成立）**

| 代号 | 判据 | 为什么能判 |
|---|---|---|
| **D1 长窗转负** | `scales.all.delta < 0` 且 `scales.all.p_raw < 0.05` 且 `|scales.all.delta| >= scales.all.mde_abs` | `all` 是唯一检出下限 0.042（+3.8%）的档；这是**唯一"够大"的负效应**能被可靠检出的地方 |
| **D2 两段连续转负** | 最近 2 个 `n>=60` 的阶段 `delta < 0` 且 `|delta| >= 0.5 × mde_abs(该段)` | 单段波动是常态（实测 24 段里约 40% 为负），连续两段同向才有边际证据；门槛用 0.5×MDE 是"方向一致 + 量级不可忽略"，**不声称显著** |
| **D3 近窗显著为负** | `recent.delta < 0` 且 `recent.p_adjusted < 0.05` | 加权近窗已经过 `ERA_FAMILY_M` 锁定校正（`regime.py:761`），是系统既有的"近窗"口径 |

**升级判据（U1，与 D1 对称）**：`scales.all.delta > 0` 且 `scales.all.p_raw < 0.05` 且 `>= mde_abs`，**并且** 通过既有的三条件（`hyp_status == validated` 且 `prereg_ok is True` 且 `source != "llm"`，见 `patterns_runtime.py:128-133`）。
→ **升级不放宽**：本机制只负责"能不能回一线"，不负责"能不能进一线"。这一条是防止本功能成为"绕过前置注册的后门"。

**Hold（默认）**：三条都不满足 → 不动作。**这是绝大多数规律的归宿**（现实数据里 17 条全部 hold，见 §6 实测）。

### 3.2 为什么不用"更聪明"的判据（例如 CUSUM / 趋势拟合）

- 在独立随机数据上，任何趋势检测器的**误报率**都只能靠 ARL0 标定，而 ARL0=500 期意味着"平均 500 期误报一次"——对 17 条规律 × 每周 3 期，**误报频率取决于规律条数**，不好向用户解释。
- 更根本：P1-2 的 CUSUM 方案尚未实现，本任务不应把它当前置依赖。
- 因此本设计刻意采用**粗粒度、可向用户一句话讲清**的三条判据，并把"检出能力"（MDE）写进每一条里。**宁可漏判，不可乱判**——漏判的代价是"一条没用的规律继续展示"，乱判的代价是"用户不再相信这个功能"。

---

## 4. 滞后（hysteresis）与状态机

### 4.1 三态定义（与既有语义对齐，**不新增 rejected**）

| 状态 | 语义 | 与既有 tier 的关系 |
|---|---|---|
| `active` | 规则的**偏置在出号链路里生效** | 快照里 `usable=True` 且 tier ∈ {usable_long_term, usable_phase} |
| `monitor` | **退居二线**：只观察、只展示、**不进 `usable_rules`** | 强制 `usable=False`，tier 不变（保留 monitor/rejected 的原有细分） |
| `retired` | 人工/规则永久退役（目前只由人工触发，本任务不自动产生） | 与 `rejected` 语义一致（有反向证据或人工否决） |

**约束 3 的落实**：`D1/D2/D3` 触发的迁移一律 `active → monitor`，**绝不写 `rejected`**。
只有既有 `hypothesis.resolve_registry` 的"反对"路径才写 `rejected`（`hypothesis.py:1508-1511`），本模块**不碰**。
`monitor` 与 `rejected` 在 UI 上必须分栏显示，并各带一句定义（§7）。

### 4.2 滞后规则

```text
进入条件（K_DEMOTE=2）：同一 direction 连续 2 次周期判定成立，才真正迁移
离开条件（K_UPGRADE=2）：升级同样需连续 2 次（对称，防抖）
持有期（min_dwell_issues=3）：任何一次迁移后，至少 3 期不再评估该规律
```

- **"连续 2 次"指两次调度周期**（每周二/四/日），不是两次请求 —— 在 `_run_lifecycle_step` 里按 `base_issue` 去重，同一期号重复调用是幂等的。
- `pending.streak` 持久化在状态文件里，**跨进程/重启保留**（否则每次重启都清零，滞后名存实亡）。
- 阈值全部可配置（`config` 新增 3 个常量，默认 `2/2/3`），并**写进状态文件**，这样"当时用的是什么阈值"永远可审计。

**滞后的定量依据**（为什么 2 够）：连续 2 次同向的误触发率 ≈ 单次误触发率²。以 D2 为例，单段为负的概率≈0.5，若"连续两段为负且超过 0.5×MDE"的单次成立率记为 q，则两次连续成立 ≈ q²。**q<0.5 时 q²<0.25**，再叠加 min_dwell 的 3 期冷却，实际每周误切换率 <1%。这个数量级可以用蒙特卡洛测试验收（§8 测试 T5）。

### 4.3 状态文件与快照的关系

- **状态文件**：`data/rule_lifecycle.json`，`{version, updated_at, base_issue, thresholds, rules: {rule_id: {...}}}`，原子写（`.tmp` + `os.replace`，与 `hypothesis._save_registry` / `patterns_runtime.save_snapshot` 同款）。
- **注入点在快照**：`build_snapshot` 末尾对 `rules[]` 应用 `apply_states()`：
  `state=="monitor"` → `usable=False`、`usable_kind=None`、`tier` 保留原值但新增 `lifecycle_state` 字段；`counts` 新增 `lifecycle_monitor` / `lifecycle_active`。
- **为什么注入在快照而不是改 engine**：`engine.apply_rules_to_candidates` 只读 `patterns_runtime.usable_rules()`（`patterns_runtime.py:358-361`），快照一改，**出号链路自动生效，engine 零改动**。这也满足"退居二线=只观察不使用了"的语义：被 demote 的规律不再出现在 `usable_rules`，但仍然出现在 `rules[]` 里被展示与统计。
- **失败即放行**：状态文件缺失/损坏/版本不认识 → `apply_states` 返回原快照（等价"没有任何规律被退居二线"）。**绝不因为生命周期层故障而让规律无法使用**（约束 2）。

---

## 5. AI 不可用时的降级路径（约束 2 的落实）

| 故障 | 行为 |
|---|---|
| `LOTT_LLM_DISABLED=1` / 未配置 / 决策通道关停 | **跳过 AI 层**，只跑 ①② 两层；`llm_judgements=[]`，`llm_note="AI 未参与（原因：…）。本期的退居二线判定完全由确定性判据给出。"` |
| LLM 超时 / 返回非法 JSON / schema 校验失败 | 同一条原因，**不重试第二遍**（一次周期只试一次，避免烧钱）；状态机照常推进 |
| AI 层长期低分（`llm_demotion_skill` 显著低于基线） | 自动把 AI 层切为"只展示不定时调用"（复用 `methods.set_decision_disabled` 的既有机制，`methods.py:458`），并在报表里说明这是**成本决策** |
| 统计层异常（某条规律算不出指标） | 该条 `decide` 返回 `{"direction":"hold","reasons":["指标不可用，不动作"]}`，**其余规律照常** |

**核心不变式（必须有测试守着）**：
> `run_cycle(...)` 在 LLM 完全不可用时，`states` 的结果与"LLM 可用且全部判 keep"时**逐字节相同**。
> 换言之：**AI 的存在与否不影响状态迁移**，只影响解释文本与技能度量。

---

## 6. 现实数据实测（本设计在真实规则上的预演）

用当前 3515 期数据、`data/patterns_runtime.json` 里的真实规律跑 `multiscale.scale_scan` 与 `regime.regime_filter`：

| 规律 | all 档 delta / p | long(150) | 最近 2 段 delta | D1 | D2 | D3 | 结论 |
|---|---|---|---|---|---|---|---|
| `repeat {back:1}` | −0.0073 / 0.62 | +0.102 | +0.062 / +0.076 | ✗ | ✗ | ✗ | **hold** |
| `neighbor {back:1,offset:1}` | −0.0147 / 0.31 | +0.109 | −0.005 / −0.065 | ✗ | ✗ | ✗ | **hold** |
| `zone_lag {zone:0}` | +0.0474 / 0.010（credible） | +0.060 | −0.027 / +0.120 | ✗ | ✗ | ✗ | **hold** |
| `pair {window:100,top:3}` | +0.0333 / 0.022 | +0.121 | −0.005 / +0.163 | ✗ | ✗ | ✗ | **hold** |

**结论**：在真实数据上，当前 17 条规律**一条都不触发 D1/D2/D3** → 全部 hold → 用户看到的是"本期没有任何规律需要退居二线"。
这是**正确且诚实的输出**（与 V4/V6/V10 的既有结论一致：规律不存在，因此谈不上"衰减"）。
它同时意味着：**本机制真正发挥作用的前提，是将来真的出现了 active 规律**。设计上必须允许"长期 0 动作"，UI 也必须这样写（§7）。

> 这是本设计最重要的一处诚实说明。任何"AI 判退了 N 条"的漂亮数字，在今天的系统里都只能是假的。

---

## 7. 诚实边界（必须写进 UI 与 README）

1. **退居二线 ≠ 规律被证伪**：`monitor` 的定义是"证据不足、继续观察"。UI 文案固定为：
   > 「已退居二线（monitor）：本规则的效应已长期检不出/转向，**不再参与出号**，但仍留在列表里继续观察。它不是被证伪，随时可能因新证据回到一线。」
2. **"AI 判断"的实际含义**：UI 必须写明「判定由确定性统计判据给出；AI 负责解释与归因，其解释的可信度由 `llm_demotion_skill` 实时披露」。
3. **0 动作也是结论**：「本期没有规律需要退居二线」要作为一个正常状态显示，不要留白。
4. **不得出现"提高准确率"字样**（沿用 `docs/PROJECT_CONTEXT.md` §7 与 V10 §3.2 第 1 条）。

---

## 8. 机器校验：AI 的每一条断言如何被核对

### 8.1 校验规则（`validate_judgements()`，纯函数，无 LLM）

对每条 judgement 依次检查，**任何一条失败即该条 `status="invalid"`**（不阻塞其它条、不阻塞状态机）：

| # | 检查 | 失败原因码 |
|---|---|---|
| V1 | `rule_id` 必须出现在本次 payload 里，且不重复 | `unknown_rule` / `duplicate_rule` |
| V2 | `verdict ∈ {keep, demote, upgrade}` | `bad_verdict` |
| V3 | `confidence ∈ [0,1]` 且落在后端算出的 `[lo, hi]` 内（见下） | `confidence_out_of_band` |
| V4 | `evidence` 非空（≤6 条）；每条 `field` 必须**在 payload 中真实存在**（按路径解析）且 `value` 与 payload 数值**相对误差 ≤1e-9** | `evidence_unknown_field` / `evidence_value_mismatch` |
| V5 | `reason` 非空且长度 ≤500 | `reason_missing` |
| V6 | **方向一致**：`gold.direction == "hold"` 时 `verdict` 必须是 `keep`；`gold.direction == "demote"` 时 `verdict ∈ {demote, keep}`（允许保守）；`gold.direction == "upgrade"` 时 `verdict ∈ {upgrade, keep}` | `verdict_contradicts_gold` |

**`confidence` 区间 `[lo, hi]` 的确定性算法**（这是白送它一部分分，也防它乱报）：
```
lo = gold.direction == "demote" and verdict == "demote" ? min(0.8, 0.5 + 0.05*streak) : 0.0
hi = 1.0
```
—— 白送的分只来自"与金标准一致"，**不一致的 confidence 一律落在 [0,1] 全区间内照常计分**，因此分数不会被无脑抬高。

### 8.2 技能度量（`llm_demotion_skill`）

金标准 = `decide_rule_state()` 的 `direction`（hold/demote/upgrade）。按判据类型分组计分：

| 指标 | 定义 | 基线 |
|---|---|---|
| `agreement` | `verdict` 与 gold 一致的条数 / 总条数 | **50%**（二分类 keep vs 动作，随机 = 50%） |
| `wilson_lo` | `agreement` 的 Wilson 95% 下界 | —— |
| `by_rule` | 按 `D1/D2/D3/hold` 分组的混淆矩阵 | 用于定位"是哪种判据没跟上" |
| `n` 与 `n_action` | 总判定数 / 其中 gold 为动作的条数 | `n_action < 10` 时**禁止下结论**，只报"样本不足" |

**关停规则**（与 V10-11 同口径，`api_app._maybe_auto_disable_decision` 同款）：`n_action >= 10` 且 `wilson_lo <= 0.5` → 把**AI 解释层**标为 `display_only`（不再周期调用 LLM），并写 `llm_config.json` 的既有键（不新增第 7 层开关）。
—— 注意：`n_action >= 10` 这个门槛在当前数据下**几乎不可能达到**（全部 hold）。因此本规则的现实意义是"当真有规律开始衰减时，AI 只有证明自己跟得上才继续烧钱"。**这条必须写进文档，否则又是一句空话。**

### 8.3 一个反直觉但重要的决定：**不阻止 LLM 说"误报"**

当 gold=`hold` 而 LLM 说 `demote` 时，这是**错误的**（V6 判 invalid）。但**不因此关停 AI**，因为：
- 这恰恰是用户最想要的能力——"我担心它在掉"；
- 保留错误样本，`llm_demotion_skill` 才有分母；
- 如果长期错，自动降级为 display_only，成本有界。

---

## 9. 实现清单与文件域（等 Lead 确认）

| 文件 | 动作 | 说明 |
|---|---|---|
| `lottery/rule_lifecycle.py` | **新建（本任务主交付）** | `build_payload` / `decide_rule_state` / `apply_states` / `load_states` / `save_states` / `run_cycle` / `validate_judgements` / `skill_stats` |
| `tests/test_v11_rule_lifecycle.py` | **新建** | 见下 |
| `lottery/config.py` | 新增 3 个常量 | `LOTT_LIFECYCLE_K_DEMOTE=2` / `K_UPGRADE=2` / `MIN_DWELL=3`（只增不改） |
| `lottery/patterns_runtime.py` | **小改 2 处** | `build_snapshot(..., lifecycle_states=None)` 末尾注入；`refresh` 读取状态文件 |
| `lottery/api_app.py` | 新增 2 个端点 + 调度钩子 | `/api/rules/lifecycle`（读）、`/api/rules/lifecycle/run`（手动跑一轮）；`_scheduler_step` 在快照刷新**之前**跑 `_run_lifecycle_step()`（失败只降级） |
| `web/*` | 不做 | 前端由 Lead 排期；后端先把 JSON 给全（`lifecycle_state` / `pending` / `history` / `judgement` / `skill`） |

**测试（`tests/test_v11_rule_lifecycle.py`，≥16 例）**

| 编号 | 断言 |
|---|---|
| T1 | `decide_rule_state`：D1/D2/D3 各自单独命中 → `demote`；构造数据注入负向偏置必须检出（"注入偏置必须检出"传统） |
| T2 | **纯随机 / 全 hold 数据必须不动作**（"纯随机不得误报"传统），1000 次蒙特卡洛，误动作率 ≤ 名义阈值 |
| T3 | 滞后：连续 1 次成立 → 不迁移（只写 `pending`）；连续 2 次 → 迁移；`min_dwell` 内不再评估 |
| T4 | 语义：迁移目标**只能是 monitor**；任何路径都不会写 rejected |
| T5 | 抖动：构造"在阈值周围来回抖"的序列 → 无滞后时迁移次数 ≥5，有滞后时 ≤1（直接量化约束 4） |
| T6 | LLM 不可用（`LLM_DISABLED=1` / 未配置）→ `run_cycle` 正常返回，states 与"LLM 全 keep"逐字段相同 |
| T7 | `validate_judgements`：V1–V6 逐条构造违规样例，全部必须 invalid |
| T8 | `apply_states`：monitor 状态把 `usable` 置 False 且 `counts.usable` 下降；状态文件缺失/损坏 → 原样返回 |
| T9 | 幂等：同一 `base_issue` 连跑两次，状态与 `pending.streak` 不变 |
| T10 | `skill_stats`：n_action<10 时不得输出"显著"结论；Wilson 下界计算正确 |

**回归**：`pytest -q` 全量必须 ≥ 724 + 新增数，0 failed；`compileall`；`git diff --check`。本任务不碰前端，故无需 `?v=` 升级。

---

## 10. 需要 Lead 裁决的 3 个问题

1. **`demote` 是否允许直接产生动作？** 我的建议是**不允许**（§2.3）。若 Lead 坚持要 AI 直接 demote，则必须补一条"AI 不可用时用纯规则兜底"的等价路径，否则违反约束 2 —— 而那等价于"AI 的话可以由规则代替"，也就没有理由让 AI 直接改状态了。
2. **降级目标用现有 `monitor` tier 还是新状态文件？** 我的建议是**两者都要**：状态文件记录生命周期历史与滞后计数（快照是派生数据，会被重建），快照只负责"把 monitor 状态翻译成 `usable=False`"。若 Lead 希望完全不引入新状态文件，滞后计数只能塞进 `hypotheses.json`（跨模块写，风险更高，不推荐）。
3. **是否现在就接 `_scheduler_step`？** 我的建议是**接，但排在最后**（先把手动 `/api/rules/lifecycle/run` 跑通并通过测试）。理由：调度钩子是唯一能造成线上状态变化的入口，先让它可手动复核再自动化。

---

## 11. 与 r2 统计评审的接口

`docs/reviews/MULTISCALE_DESIGN.md` 目前**尚未产出**（截至本文写作时不存在）。本文档依赖的统计口径全部直接来自 `lottery/multiscale.py` 与 `lottery/regime.py` 的**已实现代码**，不依赖尚未定稿的评审结论。
若 r2 的结论是「多尺度 delta 不足以支撑衰减判定」，本设计的**降级方案**是：把 D1/D2/D3 换成 `regime` 现有的阶段状态机迁移（`persistent → fading/stale` 即触发 demote，`stale → persistent` 触发 upgrade），**AI 层与状态机/滞后/skill 度量全部不变**——因为本设计的核心是把"动作权"与"解释权"分离，而不是绑定在某一种检验上。
