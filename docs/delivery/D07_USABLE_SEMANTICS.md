# D-07 `usable` 一词两义 · 语义标注与禁止并列展示

> 交付人：李工（后端）｜日期：2026-10-21
> 测试：`tests/test_d07_usable_semantics.py`（8 条）
> 改动：`lottery/idle_mining.py` —— `status()` 新增 `usable_semantics` 段（**只加，不删**）

## 一、缺陷原文（用户视角）

```
data/idle_mining_state.json : by_decision.usable = 20   ← 单候选过阈（未校正）
data/patterns_runtime.json  : usable = 0                 ← 生产快照（可回放）
```

用户看到两个 "usable" 数值不同，**直接认定系统造假** —— 尽管每个数字单独看都对。

**两者都对**，因为它们回答的是不同问题：

| 口径 | 字段 | 含义 | 是否影响出号 |
|---|---|---|---|
| 挖掘层 | `idle_mining.rule_evidence_count` | 单候选过 α 阈 + 过 e-BH 族级校正的**统计证据项** | 否，不进快照、不影响出号 |
| 生产层 | `patterns_runtime.counts.usable` | 可回放且被认定可用于出号的**规律** | 是，出号门控依据 |

## 二、本轮范围（刻意克制，遵齐交付指令）

| 动作 | 做没做 | 说明 |
|---|---|---|
| 加 `usable_semantics` 标注 | 已做 | 机器可读，前端/报表可断言 |
| 加测试禁止两者并列展示 | 已做 | AST 扫描 + 前端同行扫描 |
| 删除 `idle_mining` 的 `usable` 弃用别名 | 不做 | 按指令留给下一版本 |
| 改名 `e_reject_candidates` | 不做 | 下一版本执行（已写进 `rename_plan` 字段） |
| 改 `_group_checked` 的 m 记账 | 不做 | 属 D-08；修完 `usable` 仍为 0（+6.3% 到 +5.9%，仅 0.4 个百分点），混入会污染验收 |

## 三、标注实测输出

```
$ .venv/bin/python -c "from lottery import idle_mining as I; import json; \
    print(json.dumps(I.status()['usable_semantics'], ensure_ascii=False, indent=1))"
{
 "this_module_authoritative_field": "rule_evidence_count",
 "deprecated_aliases": ["usable_count_deprecated", "by_decision.usable"],
 "meaning_zh": "本模块的「证据项」= 单候选过 α 阈后再经 e-BH 族级校正的统计结论；它不进生产快照、不影响出号。",
 "collides_with": {
  "field": "patterns_runtime.counts.usable",
  "meaning_zh": "生产快照里的「可用规律」= 可回放且被认定可用于出号的规律，是出号门控依据。与本节字段同名不同义。"
 },
 "never_show_together": true,
 "reason_zh": "两者数值不同是正确的（一个是「有证据」，一个是「能出号」）。...",
 "rename_plan": "下一版本将 `usable` 弃用别名改名为 `e_reject_candidates`。"
}
```

## 四、grep 证明：不存在并列输出路径

### 4.1 后端（AST 扫描，不是 grep，避开注释与字符串误报）

扫描 `lottery/routes_*.py` + `lottery/api_app.py` 的**所有 dict 字面量**，
若某 dict 同时含两侧口径键则报违规。

实测输出：

```
offenders: NONE
```

### 4.2 前端（文本扫描：同一行同时引用两侧）

```
$ awk '(/usable_count|by_decision\.usable|rule_evidence_count/) && /usable_replayable/ {print FILENAME":"FNR": "$0}' web/static/app.js
（无输出）
```

### 4.3 这些检查已测试化，不是一次性命令

| 测试 | 作用 |
|---|---|
| `test_no_route_returns_both_usable_fields` | 后端 AST 扫描，变异已验证能捕获 |
| `test_no_frontend_reads_both_usable_fields` | 前端同行扫描 |
| `test_annotation_keys_do_not_drift` | 防标注被顺手重命名导致守卫静默失效 |

**变异实验（实跑）**：

```
# 在 capabilities 响应 dict 里强行加 "rule_evidence_count": 0, "usable_replayable": 0
$ pytest tests/test_d07_usable_semantics.py::test_no_route_returns_both_usable_fields -q
FAILED  ...  AssertionError        # 变异被捕获

$ # 恢复后
1 passed
```

## 五、语义标注进入 /api/capabilities 的降级段

`GET /api/capabilities` 的 `degraded_items` 只使用**权威口径** `usable_replayable`
（且已断言与 `rule_evidence_count` 不同时出现在同一 dict 中）。当前输出：

```
[idle_mining]       设置 LOTT_IDLE_MINING=1 ... 或 POST /api/mining/run
[usable_replayable] POST /api/rules/refresh 重建快照并回放；若仍为 0，说明确实没有通过检验的规律（不应为此降门槛）
```

两条都是真实降级项，且各自说明口径不同，不构成「两个 usable 打架」。

## 六、三态结论

| 事项 | 状态 | 依据 |
|---|---|---|
| `usable_semantics` 标注存在且两侧含义都写明 | 已验证 | 用例 1 到 6 |
| 权威字段为 `rule_evidence_count` | 已验证 | `test_authoritative_field_present_and_preferred` |
| 弃用别名保留 + 改名计划已记录 | 已验证 | `test_semantics_declares_deprecation_and_rename_plan` |
| 后端无并列输出路径 | 已验证 | AST 扫描 NONE，变异可捕获 |
| 前端无并列引用 | 有风险 | awk 扫描无输出，但未做渲染实测（见下） |
| `usable` 别名未删除 | 已验证 | 按指令保留，`rename_plan` 指向下一版本 |
| `_group_checked` m 记账未改动 | 已验证 | `test_group_checked_still_present`（仅存在性守卫） |

## 七、未做到的部分

1. **前端渲染未实测**：我没有在浏览器里打开页面确认「两个 usable 不再并排出现」。
   前端扫描只能证明代码里没有同一行同时引用，不能证明渲染结果 —— 若某处
   分两行读取再拼到同一张卡上，扫描会漏。建议由严QA 用页面实测补。
2. **`e_reject_candidates` 改名未做**：按指令留给下一版本。
3. **`docs/` 与 `web/` 里可能存在的「可用 XX 条」文案未逐一审计**：
   许产品的 `V14_PRODUCT_COPY_AUDIT` 覆盖了部分，但我未逐条核对是否还有其它
   把两个 usable 混着说的表述。属 D-05/D-06 文案项。
