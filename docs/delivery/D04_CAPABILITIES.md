# D-04 能力清单端点 `GET /api/capabilities`

> 交付人：李工（后端）｜日期：2026-10-21｜代码基准：`db634e3` + 本轮新增
> 复现：`bash docs/delivery/repro/verify_d03_d04_d07.sh`
> 测试：`tests/test_d04_capabilities_api.py`（21 条）
> 端点实现：`lottery/routes_capabilities.py`（新建）
> 注册：`lottery/api_app.py`（`include_router(routes_capabilities.router)`）

## 一、实测输出（当次真实运行，非转述）

```
$ .venv/bin/python -c "from lottery import routes_capabilities as C; import json; \
    print(json.dumps(C.capabilities()['constants'], ensure_ascii=False, indent=2))"
{
  "alpha": 0.05,
  "single": 306,
  "cross": 72350,
  "ev_single": 0.531925,
  "ev_sixth": 0.15625
}
```

`sources`（每个常量的出处符号，运行时取自代码）：

| 常量 | 值 | 来源符号 |
|---|---|---|
| `alpha` | 0.05 | `lottery.eprocess.DEFAULT_ALPHA` |
| `single` | 306 | `lottery.candidate_space.total_single()` |
| `cross` | 72350 | `lottery.candidate_space.total_cross()` |
| `ev_single` | **0.531925** | `engine._single_ticket_ev(first=6,840,000, second=205,000, sixth='published')['ev_ratio']` |
| `ev_sixth` | 0.15625 | `blue_coverage.optimize(1)['ev_ratio_sixth']` |

## 二、✅ `ev_single` 双口径并存（**Lead 已终裁**，原「待裁决」结案）

### 终裁结论

两个数**都对**，因为它们量的是**两个不同口径**。此前我和齐交付都把 52.3% 判成
"不可复现"—— 那是**我们漏试了一个组合**，不是文档写错。

Lead 给出的四组实测（李工已独立复现，数值逐位吻合）：

| 奖金假设 | `sixth` 模式 | `ev_ratio` | 含义 |
|---|---|---|---|
| `None`（表 A：500万/15万） | `published` | **45.67%** | 旧代码默认值，已废弃 |
| `None`（表 A） | `strict` | **44.77%** | — |
| 中位 684万/20.5万 | `published` | **53.19%** | ← **端点权威值** `0.531925` |
| 中位 684万/20.5万 | `strict` | **52.29%** | ← **红线表 52.3% 的真实出处** |

关键复现证据：`strict` 口径下 `ev_yuan = 1.0458133`，除以 2 = **0.522907**。
`lottery/popularity.py:42` 那句「单注 EV≈1.0458 元 / 2 元 = 52.3%」
**不是手写估计，就是 strict 口径的算术结果** —— 它在 strict 语义下完全正确。

### 两种口径的语义差别

* **`published`**：奖级按官方公布的**判定规则**分层（一~六等各自独立），
  六等奖按"蓝球命中"计，与其他奖级**可以同时命中**（实际不可能同时，但概率模型上分别计）。
  这是 `blue_coverage.ev_ratio_full` 用的口径，与用户实际兑奖体验一致。
* **`strict`**：奖级**严格互斥**（一注只归入最高奖级，不重复计）。
  这是 `iid` 意义上更保守的算法，数值略低。

### 落地规则（已写入代码与测试）

1. **端点 `constants.ev_single` 取 `0.531925`**（published，与 `ev_ratio_full` 同源）；
2. **红线表 52.3% 保留**（strict，不删除——它是另一个语义下的正确值）；
3. **两者必须各自标注 `sixth_mode` 与 `prize_assumption`，不得裸给数字**；
4. 测试 `test_ev_ratio_map_relates_three_values` 断言三口径关系
   （`0.531925 > 0.523 > 0.5028`，且都 `< 1`），保留全部数值以备核对。

> 结论：**不是"同一个数有两个版本"，而是"两个语义各自的数"。**
> 危险的做法是删掉一个假装统一；正确做法是并存 + 标注 + 可复算。

## 三、返回结构（四段，可增不可减）

```json
{
  "ok": true,
  "degraded": false,
  "degraded_constants": [],
  "can":      [ {id, title, evidence_script, honest_bound} ],   // 5 项
  "cannot":   [ {id, title, why_not, evidence} ],                // 4 项（硬否定）
  "degraded_items": [ {id, reason, how_to_restore} ],            // 当前真实降级
  "constants": {...},                                            // 5 项，实时读取
  "constants_meta": {sources, prize_assumption, derivation_zh, errors, note_zh},
  "summary": {n_can, n_cannot, n_degraded, verdict_zh},
  "disclaimer": "..."
}
```

### `cannot` 四条硬否定（验收要求，缺一不可）

| id | 标题 | 证据 |
|---|---|---|
| `predict_numbers` | 预测下一期开奖号码 | χ² p=0.0654 / 条件熵降 0.0076 bit / max-stat p=0.2995 |
| `positive_expectation` | 获得正期望收益（长期不亏） | `constants.ev_single` 实时值；长期期望为负 |
| `raise_sixth_ratio` | 让六等奖返奖比高于 5/32 | 恒等式 `(n/16×5)/(2n)=5/32` |
| `coverage_changes_expectation` | 用蓝球覆盖改变期望回报率 | 47.55% → 62.50%，成本 20 元，返奖比仍 15.625% |

### `can` 五项（每项都带存在的证据脚本）

`blinded_audit` / `replayable_rules` / `popularity_rank` / `blue_coverage` / `ticket_distribution`
—— 测试 `test_every_can_item_has_evidence_script` 会**实际检查路径存在**。

### `degraded_items` 当前实测内容

| id | 原因 |
|---|---|
| `idle_mining` | 挖掘关闭（`LOTT_IDLE_MINING` 未开启），已判定 21800 候选 |
| `usable_replayable` | 生产快照可用可回放规律 = 0（审计结论，非加载失败） |

## 四、核心守卫：常量必须实时读取（变异验证）

**这是本端点存在的唯一理由** —— 写死字面量的清单在口径漂移时仍显示"正确"，那正是
本项目历史上反复踩的坑。

守卫用例 `test_constants_are_read_live_not_hardcoded`：篡改
`eprocess.DEFAULT_ALPHA = 0.123`、`candidate_space.total_single = lambda: 999`，
端点输出必须立即变为 `0.123` / `999`。

**变异实验（实跑，非声称）**：

```
$ # 变异：把 alpha 改成写死字面量
$ pytest tests/test_d04_capabilities_api.py -q
FAILED tests/test_d04_capabilities_api.py::test_constants_are_read_live_not_hardcoded
1 failed, 20 passed          ← 变异被捕获 ✅

$ # 变异：常量取不到时静默回落到 0（而不是 None）
$ pytest tests/test_d04_capabilities_api.py -q
FAILED tests/test_d04_capabilities_api.py::test_constants_failure_is_reported_not_silently_wrong
1 failed, 20 passed          ← 变异被捕获 ✅

$ # 恢复后
$ pytest tests/test_d04_capabilities_api.py -q
21 passed                    ← 全部通过 ✅
```

## 五、与 `/api/portfolio/capabilities` 的分工（不是重复实现）

| 端点 | 受众 | 内容 |
|---|---|---|
| `/api/portfolio/capabilities` | 人 | 四条结论 + 长文披露（票怎么选） |
| `/api/capabilities`（本项） | 机器/CI/审计 | `can/cannot/degraded/constants` 四段结构化，常量实时读取 |

两者**不并列展示同一口径的冲突数字**；`constants` 只在本端点出现。

## 六、三态结论

| 事项 | 状态 | 依据 |
|---|---|---|
| 端点存在且注册到 FastAPI | **已验证** | `app.openapi()` 含 `/api/capabilities`，`test_route_is_registered_on_app` |
| `can` 5 项，每项证据脚本**存在** | **已验证** | `test_every_can_item_has_evidence_script` |
| `cannot` 四条硬否定齐全 | **已验证** | `test_cannot_covers_all_four_red_lines` |
| `constants` 实时读取（非字面量） | **已验证** | 变异实验 2 例均被捕获 |
| α/306/72350/15.625% 与红线表一致 | **已验证** | `test_constants_match_redline_table` |
| **`ev_single` 与红线表 52.3% 一致** | **有风险** | 实时值 53.19%，见 §二，待 Lead 裁决 |
| 端点无禁用词 | **已验证** | 4 个禁用词参数化用例 |
| 降级项只报真实状态 | **已验证** | 正反两向用例（开/关挖掘） |

## 七、未做到的部分

1. **前端接入未做** —— 属刘工（D-01/D-04 联动）。当前只是后端可用，首屏看不到。
2. **`test_v14_naming_discipline.py` 未覆盖本端点** —— 验收要求第 5 条提到
   「该端点必须被 naming_discipline 覆盖（配合 D-12）」。我**没有**动该文件
   （D-12 是测试债，排除规则收宽问题需整体改造，改一半会让守卫静默失效）。
   本端点自带禁用词用例作为过渡，但**不替代** D-12。
3. **`ev_single` 红线冲突未解决** —— 我不擅自改红表数字，见 §二。
