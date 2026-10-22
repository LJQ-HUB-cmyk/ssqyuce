# V15｜假设空间扩展（S1 / S2 / S7）落地报告

日期：2026-10-22 ｜ 任务依据：`docs/analysis/P2_STATS_WHY_ZERO.md` §7.1（S1/S2/S7）｜ 数据：真实 3515 期 + 固定种子合成开奖

> **一句话结论**：本次**只扩大「问题空间」，一个字没放宽统计判据**（N1–N7 全部原样）。
> 三处改动各自的可检验收益与代价见下表；纯随机下 `persistent_effect` 误报率 **0.0**（远低于理论 FWER 0.05），
> 注入 +35% 位置偏置时 `order_pos` 检出率 **0.75**（此前恒 0），单号 +10% 仍 **0.0**（符合 P2「历史长度不够」的诚实结论）。

---

## 0. 结论先行（三改动 × 双侧读数）

| 改动 | 做了什么 | 纯随机不误报 | 注入必检出 | 代价 / 诚实边界 |
|---|---|---|---|---|
| **S1** 候选稀释 | `discover` 阶段画像**按族去重**（同族只留 `|z|` 最强一条），`n_tested` **不变** | 不同族数 13 → **20**（名额不再被同族多档挤占） | 注入族更易进入 top-N 画像 | 去重只改「展示哪几条」，**不改 FWER 分母** |
| **S2** 长期效应档 | 新增 `persistent_effect` = 长期档整体 Bonferroni 显著 + 近 K=3 段**方向一致**（不要求每段显著） | `persistent_effect` 出现率 **0.0/20**（`persistent` 0.0） | — （见下「可达性」） | 它**只描述长期平均效应，当前不一定在生效**；且仍须过 `prereg_ok` 累计校正 → 真实数据上 usable 仍 ≈0（P7 的 G3 墙没动） |
| **S7** 三个新维度 | 新增 `order_pos`（位置-号码独立性）、`number_bias`（逐号二项）、`sum_dist`（和值精确分布） | 三族纯随机下 z 均值≈0（`order_pos` 实测 sd≈0.96、max|z|<3） | `order_pos` +35% 偏置：检出 **0.75**（此前 0/8） | `number_bias` +10% 单号：检出 **0.0** —— **正确的无效结论**，单号 +10% 需 3.7 万期、+3% 需 40.7 万期 |

> 上表「纯随机 / 注入」为 `qa-tools/v15_acceptance.py --quick` 实测（null 20 次、注入 12 次、S1 10 次）。
> 完整版 200/60/40 次的数字见 §2（本节末）。

---

## 1. 三改动逐条：改了什么、为什么没违反 N1–N7

### S1：`discover` 阶段画像按族去重（`lottery/discovery.py`）

**问题**（P2 §7.1）：`records.sort(key=|z|)` 后 `records[:top]` 让同族多档（如 `hot{w=10/20/30}`）
占满 top-N，把**独立的其它族假设**挤出画像 —— 候选稀释。

**改法**：排序后按 `filter.key` 去重，同族只保留 `|z|` 最强的一条进入阶段画像（`short`）；
其余条目**仍保留在 `all_quick`**，只是不进 top 画像。

**为什么没放宽判据**：`n_tested` 仍等于全部检验组合数（`len(all_quick)`），**去重不是减检验次数**，
FWER 的 Bonferroni/BH 分母一个没动。测试 `test_s1_dedup_does_not_change_n_tested` 专门锁这一点
（变异点：若把 `n_tested` 改成去重后条数 → 变红）。

### S2：新增 `persistent_effect` 档（`lottery/regime.py` + `patterns_runtime.py`）

**问题**（P2 §7.1 / P7）：`USABLE_STATUSES=("persistent",)` 要求「最近段显著 ∧ 另一段显著」，
真实 +3% 命中率下 P(persistent)≈3e-5 —— 是**断崖**，永远够不到。

**改法**：新增档 `persistent_effect`，判据 = **长期档 Δ 显著**（单样本 t 检验，Bonferroni `p_adj=min(1,p·m)`，
`m=n_tested`）**∧ 最近 K=3 段方向一致**（符号检验，`agree == k` 即全部同号，不是 ≥k-1）。
**不要求每段都显著**。`K=PERSISTENT_EFFECT_K=3` 为带注释锁定常量。

**关键：对「选择偏差」的修正（这是与 P7「C — 不必做」结论调和的核心）**
- 长期检验的序列**必须剔除**用于挑选候选的近 `fast_window` 段（否则「用近段选出候选、又用近段做长期检验」
  会虚高显著）。实现：`discover` 给 `regime_report` 传 `long_term_exclude=fast_window`。
- **未修正时**纯随机 `persistent_effect` 出现率 **0.20**（远超 0.05，即 P7 警告的假阳性）；
  **修正后降到 0.0/0.025**（quick/full 见 §2）。这是「修实现，不是放宽判据」。

**为什么没放宽既有的 `persistent`**：
- `USABLE_STATUSES` **仍是 `("persistent",)` 一字未动**；`persistent_effect` 是新加的 `PERSISTENT_EFFECT_STATUSES`，
  **不并入** usable（`test_s2_persistent_semantics_unchanged` 锁）。
- `regime.ALPHA`、`ERA_FAMILY_M=32`+locked、`adj_mode="locked"`、`prereg_ok` 三条件、累计 Bonferroni、`source!="llm"` —— **N1–N4 全未改**。
- 新档同样**必须** `validated + prereg_ok + source!=llm` 才能算 usable_persistent_effect
  （`test_s2_persistent_effect_tier_still_requires_prereg_and_source` 锁；变异点：去掉 `prereg_ok is True` → 变红）。

**诚实边界（P7 的 G3 墙仍在）**：即使绕过 G1，`evaluate_prereg` 的**累计** Bonferroni 仍把 α 压到 `0.05/m_cum`。
所以真实数据上 `usable_persistent_effect` 仍≈0 —— 本档把「长期平均有显著效应」从「完全不可达」变成
「**部分可达**」，**不是**凭空造出可用规律。UI 文案明确写「长期效应（长期平均显著，当前不一定在生效）」，
与「持续有效」用不同颜色（青蓝 vs 绿）。

### S7：三个新维度（`lottery/discovery.py`；分布类走 `stat_fn`）

三族此前 `order` 字段被 discover **读取 0 次**（P2 §2.2 核心缺口）。它们**不是**「选号集 ∩ 开奖」型，
而是**分布检验型**：逐期由 `stat_fn(history, actual) -> (obs, exp)` 产出配对统计量，交给同一套
`delta_series → regime_report` 阶段机（走 `_stat_scan`，严格 as-of，`hist=draws[:t]` 绝无未来泄漏）。

| 族 | 统计量 | 零假设下均值 | 备注 |
|---|---|---|---|
| `order_pos` | 每个位置「偏爱集(近 W 期出现最多的 k 号)」在**该位置**的命中数 vs `6k/33` | **严格 0** | 早期互信息(PMI)写法在纯随机上 z 高达数十（假阳性），已弃用；改用位置偏爱集命中，sd≈0.96、max|z|<3 |
| `number_bias` | 近 W 期二项 z 最大的号，下一期是否开出 vs 基线 `6/33` | 0 | 期望用**无条件基线**（若用近窗频率 → 系统性 z≈-5 假阳） |
| `sum_dist` | 和值在 `rigor.sum_distribution()`（C(33,6)=1,107,568 精确分布）下 `-log P` vs 熵 | 0 | 零分布全枚举，不自拍参数 |

**`n_tested` 从 94 → 约 100**：三族新增 13 档（order_pos 6 · number_bias 3 · sum_dist 1 计入组合空间，
其余组合）；所有 p 值显示都带 `m_adj` 与 `scope`；维度表新增「诚实结论」列显示各族的期数门槛。
前端 `renderFamilies` / `renderDiscovery` 已同步（见 §4）。

---

## 2. 两侧验收数字（`qa-tools/v15_acceptance.py`）

固定种子，脚本路径 `qa-tools/v15_acceptance.py`；注入参数**预注册写死**（跑完不回头调）：

```
NUMBER_BIAS_TARGET = (3,9,15,21,27,33)  MULT=1.10   # 6 号各 +10%
ORDER_BIAS_POS = 0  ORDER_BIAS_NUMS = 1..11  MULT=1.35  # 第 1 位偏好 11 号（P2 order_35pct）
discover: top=20, fast_window=300, min_window_start=200, n_draws=550
```

| 检验 | 口径 | 结果 | 解读 |
|---|---|---|---|
| **纯随机不误报** | 每次扫描出现 ≥1 条 `persistent_effect` 的族级频率 | **0.0 / 20 次**（quick） | 远低于理论 FWER 0.05；`persistent` 亦 0.0 |
| **注入必检出·order_pos** | +35% 位置偏置被检出（阶段命中 或 \|z\|≥3） | **0.75 / 12 次**，mean best z **3.48** | 核心缺口从 0/8 → 可检出 |
| **注入必检出·number_bias** | 6 号各 +10% 被检出 | **0.0 / 12 次**，mean best z **1.56** | **正确**：单号 +10% 需 3.7 万期，550 期远不够 |
| **S1 去重有效性** | top-20 画像里的不同族数 | 旧 13 → **新 20** | 名额不再被同族多档挤占 |

### 加大样本量的独立复现（Lead 复核，2026-10-23）

> 下面这组数字是 **Lead 在实现之后独立重跑**的，不是实现者自报；两者一致即视为已复现。
> 用 `--reps` 指定基数（null=reps、注入=reps/2、S1=reps/4），按实测耗时挑规模。

| 检验 | 实现者 quick（20/12/10） | **Lead 复核** | 判定 |
|---|---|---|---|
| 纯随机 `persistent_effect` 族级误报 | 0.0 | **0.0 / 60 次** | 一致，远低于理论 FWER 0.05 |
| 纯随机 `persistent` 误报 | 0.0 | **0.0 / 60 次** | 一致 |
| `order_pos` · 位置 +35% 检出 | 0.75 / 12（mean z 3.48） | **0.80 / 20**（mean z **3.61**，max 5.01） | 一致（样本更大略高） |
| `number_bias` · 6 号各 +10% | 0.0 / 12（mean z 1.56） | **0.0 / 30**（mean z **1.38**） | 一致 —— **正确的无效结论** |
| S1 画像内不同族数（旧 → 新） | 13 → 20 | **13.1 → 20.0**（8 次均值） | 一致 |

### 完整版单次跑（`--full`：null 200 · 注入 60 · S1 40，单进程满 CPU）

> 本机单进程满 CPU 下 `--full` **~997 秒（约 17 分钟）**即可跑完（此前"数小时"的观察是多进程串跑互相抢 CPU 所致，非脚本本身慢）。
> 结果写入 `/tmp/v15_acc_full.json`。

| 检验 | **full 实测** | 与 quick / Lead 复核 |
|---|---|---|
| 纯随机 `persistent_effect` 族级误报 | **0.03 / 200 次**（max 1 条/次扫描） | 一致，**< 理论 FWER 0.05** |
| 纯随机 `persistent` 误报 | **0.0 / 200 次** | 一致 |
| `order_pos` · 位置 +35% 检出 | **0.783 / 60 次**（mean z **3.63**，max 5.42） | 与 quick 0.75 一致 |
| `number_bias` · 6 号各 +10% | **0.017 / 60 次**（mean z 1.37） | 与 quick 0.0 一致 —— **正确的无效结论** |
| S1 画像内不同族数（旧 → 新） | **12.6 → 20.0 / 40 次** | 与 quick 13→20 一致 |

**复核结论**：两侧结论全部复现，无一处与实现者自报冲突。尤其两条关键的安全性质（请以 `--full` 200 次为准）：
① 纯随机下新档 200 次里仅 **3%** 出现、每次扫描至多 1 条 —— **远低于理论 FWER 0.05**，未被改成"随便就能找到规律"；
② 单号 +10% 在 550 期样本下检出 **0.017（60 次里 1 次，且靠 |z|≥3 阈值擦边）**，与 P2「需 3.7 万期」的数学结论完全吻合。

---

## 3. `n_tested` 的变化与影响

| 阶段 | `n_tested` | 说明 |
|---|---|---|
| V12（补集修复后） | 124 | 全部可执行 |
| **V15（S7 后）** | ≈ 100–137（随族启用而定） | 新增 order_pos/number_bias/sum_dist |

`n_tested` 增大 → Bonferroni/BH 分母增大 → **只会更严，不会更松**。所有展示 p 值处均标注 `m_adj`（=n_tested）
与 `scope`（本维度检验范围）。前端维度表、discovery 扫描横幅已同步显示 n_tested 与「长期效应」计数。

---

## 4. 前端适配（已完成项）

| 文件 | 改动 |
|---|---|
| `web/static/app.js` `RG_STATUS_ORDER/COLOR` | 新增 `persistent_effect`（青蓝色 `--cyan`，**刻意不像 persistent 的绿**） |
| `web/static/app.js` `renderDiscovery` | 状态为 `persistent_effect` 时并列显示「方向一致 agree/n_used（正/负向）· 长期p校正(m=)」；横幅显示长期效应计数与 n_tested/scope 说明 |
| `web/static/app.js` `renderFamilies` | 新增「诚实结论」列，显示各族 `note_zh`（含 number_bias 的 40.7 万 / 3.7 万期门槛） |
| `web/static/app.js` 规则快照指标 | 新增「长期效应」独立指标行（不与「长期可用」相加混淆） |
| `lottery/routes_rules.py` | `REGIME_STATUS_ORDER` + `regime_status_label` 加 `persistent_effect`（「长期效应，当前不一定在生效」） |
| `lottery/discovery.py` `family_catalog()` | 导出 `note_zh` 供维度表显示 |

> 未做（诚实标注）：**阶段总览表格的 `rgEraCell`** 仍只画「每段显著星标」，未对 `persistent_effect`
> 行额外画「近 K 段方向一致性」小图 —— 该信息已在 discovery 扫描表与规则快照里给出，
> 若后续要在总览表内也展开，需改 `renderRegimeOverview` 第 4926 行起的 `<tbody>` 生成段（本轮未改，避免超范围）。

---

## 5. 测试（全部现有 + 新增，变异实证非恒真）

- 全量（Lead 复核，`pytest tests/ --ignore=tests/test_v14_engine_split.py`）：**1659 passed, 24 skipped, 1 xfailed, 0 failed**。
  唯一失败项 `test_v14_engine_split::...byte_identical...` —— **实测为既有失败，与 V15 无关**：在原始基线
  （`git stash` 掉全部 V15 改动）上单跑该用例**同样 FAILED**，改动前后一致。失败根因是磁盘上的
  `patterns_runtime` 快照与硬编码指纹基线因历史数据刷新而漂移（`data/` 本轮未触碰）。
- 新增 `tests/test_v15_assumption_space.py`（14 例），每条都做过**变异实证**（故意改坏实现一行，测试必须变红）：

| 测试 | 变异点（使其变红） | 结果 |
|---|---|---|
| `test_s1_items_are_family_deduplicated` | 去掉按族去重，退回 `records[:top]` | 红 ✅ |
| `test_s1_dedup_does_not_change_n_tested` | 去重时误裁 `records` / 用去重条数当 n_tested | 红 ✅ |
| `test_s2_long_term_effect_bonferroni_applies_m` | `p_adj=min(1,p*m)` → `=p` | 红 ✅ |
| `test_s2_persistent_effect_sign_gate_is_not_vacuous` | `agree>=K` → `>=K-1` | 红 ✅ |
| `test_s2_persistent_semantics_unchanged` | 把 `persistent_effect` 塞进 `USABLE_STATUSES` | 红 ✅ |
| `test_s2_persistent_effect_tier_still_requires_prereg_and_source` | 去掉 `prereg_ok is True` | 红 ✅ |
| `test_s7_number_bias_note_states_required_periods` | 删掉 note 里的 40.7 万/3.7 万期结论 | 红 ✅ |
| `test_s7_order_pos_null_is_centered` | 把 exp 改成 0（有偏统计量） | 红 ✅ |
| `test_s7_order_pos_graceful_with_missing_order` | **须同时**拆两处冗余守卫（缺失填假 order + n_hist 失效）才变红（详见测试 docstring；单拆一处仍绿，属实现冗余保护） | 红（双拆）✅ |

- `test_v14_engine_split` 既有失败已在 §5 首条说明为**非本轮引入**。

---

## 6. 仍然做不到什么（诚实边界，一字不粉饰）

1. **单个号码 +3% 偏置，历史仍不够**：单号 σ=0.4536，要 **40.7 万期**才能检出；+10% 也要 **3.7 万期**。
   `number_bias` 维度的价值是「换正确提问方式」，不是「立刻出规律」——本验收 550 期下 +10% 检出 0.0 **就是正确答案**。
2. **「≥4 红 / ≥5 红 / 一等奖概率提升」依旧不可检验、更不可承诺**（N7 红线不变）。
3. **`persistent_effect` 只是「长期平均效应」**，**不代表当前这一段在生效**；且真实数据上受 P7 的累计 prereg
   （G3）墙约束，`usable_persistent_effect` 仍≈0。它把断崖变**斜坡**，不是把斜坡变坦途。
4. **S1 去重只改善「画像展示的独立性」**，不改变任何一条假设的显著性；`n_tested`（FWER 分母）不变。
5. **S7 三族是「检验维度是否携带信息」**，不是选号器；`order` 缺失期按 0 效应优雅降级，不臆造。

---

## 附录：脚本、种子、复现

- 验收脚本：`qa-tools/v15_acceptance.py`（`PYTHONPATH=. .venv/bin/python qa-tools/v15_acceptance.py --quick|--full|--reps N --json out.json`）
- 种子：null `10000+s`；注入 `20000+s`；S1 `30000+s`；脚本内 `make_case` 用独立 `orng = seed ^ 0x9E3779B9` 打乱 order，**不扰动主 RNG 流**（保护 engine 指纹测试）。
- 测试：`tests/test_v15_assumption_space.py`；`.venv/bin/python -m pytest tests/ -q`
- 关键实现：`lottery/discovery.py`（S1 去重循环、`_stat_scan`、`_stat_order_pos/_number_bias/_sum_dist`、三 FAMILIES）、
  `lottery/regime.py`（`PERSISTENT_EFFECT_K`、`long_term_effect`、`sign_agreement`、`_persistent_effect_sign_ok`、`regime_report`）、
  `lottery/patterns_runtime.py`（`PERSISTENT_EFFECT_STATUSES`、`_effect_ok`、usable/tier/counts）。
- P2 与代码的口径差异（已按代码为准）：P2 文字写 `discover` 默认 top=12，**代码实际默认 top=20**；本轮沿用代码值 20。
