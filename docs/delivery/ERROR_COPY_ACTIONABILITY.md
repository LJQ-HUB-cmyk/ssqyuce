<!-- 生成时间: 2026-10-21T15:58:50Z | HEAD: 77d8a54 | 生成人: 李工 -->

# 错误文案可行动性清单（D-20 输入）

扫描 `lottery/routes_*.py`：**71 条** error 文案。

判据：**用户看到这句话，知道下一步做什么吗？**

| 类别 | 条数 | 判定 |
|---|---|---|
| OK 已可行动 | 6 | 达标，无需改 |
| **C 完全不可行动** | 35 | **最高优先** |
| B 空数据无指引 | 24 | 中优先 |
| A 参数错误 | 5 | 低优先（用户能自行改） |
| D 其他 | 1 | 需人工判断 |

## C 类｜完全不可行动（最高优先） —— 35 条

| 文件:行 | 原文 | 附 detail |
|---|---|---|
| `lottery/routes_audit.py:58` | AI 核对模块尚未就绪 | 是 |
| `lottery/routes_audit.py:68` | 读取审计记录失败 | 是 |
| `lottery/routes_audit.py:94` | AI 核对模块尚未就绪 | 是 |
| `lottery/routes_audit.py:128` | 构造核对输入失败 | 是 |
| `lottery/routes_audit.py:259` | 读取开奖数据失败 | 是 |
| `lottery/routes_audit.py:275` | 自证实验执行失败 | 是 |
| `lottery/routes_audit.py:291` | 读取审计产物失败 | 是 |
| `lottery/routes_portfolio.py:371` | 模型未就绪（拟合未完成或失败），稍候会自动重试 | 否 |
| `lottery/routes_rigor.py:107` | randomness_report() 返回结构异常 | 是 |
| `lottery/routes_rigor.py:181` | random 是基线，不是可检验策略 | 否 |
| `lottery/routes_rigor.py:191` | 样本不足：可对照期数少于窗口长度，无法做显著性检验 | 否 |
| `lottery/routes_rigor.py:40` | 随机性检验模块 lottery.rigor 尚未就绪 | 是 |
| `lottery/routes_rules.py:483` | battle_report() 返回结构异常 | 否 |
| `lottery/routes_rules.py:81` | 阶段时效模块尚未就绪 | 是 |
| `lottery/routes_rules.py:164` | 阶段时效模块尚未就绪 | 是 |
| `lottery/routes_rules.py:172` | 标注失败 | 是 |
| `lottery/routes_rules.py:183` | 维度模块尚未就绪 | 是 |
| `lottery/routes_rules.py:206` | 维度模块尚未就绪 | 是 |
| `lottery/routes_rules.py:216` | 扫描失败 | 是 |
| `lottery/routes_rules.py:235` | LLM 规律工坊模块尚未就绪 | 是 |
| `lottery/routes_rules.py:255` | 进化模块尚未就绪 | 是 |
| `lottery/routes_rules.py:263` | 注册失败 | 是 |
| `lottery/routes_rules.py:279` | 维度模块尚未就绪 | 是 |
| `lottery/routes_rules.py:295` | 回放失败 | 是 |
| `lottery/routes_rules.py:372` | 规律运行时模块尚未就绪 | 是 |
| `lottery/routes_rules.py:395` | 规律运行时模块尚未就绪 | 是 |
| `lottery/routes_rules.py:403` | 刷新失败 | 是 |
| `lottery/routes_rules.py:417` | LLM 规律工坊模块尚未就绪 | 是 |
| `lottery/routes_rules.py:436` | LLM 规律工坊模块尚未就绪 | 是 |
| `lottery/routes_rules.py:479` | battle_report() 执行失败 | 是 |
| `lottery/routes_rules.py:504` | 自进化模块 lottery.evolution 尚未就绪 | 是 |
| `lottery/routes_rules.py:510` | evolution_status() 执行失败 | 是 |
| `lottery/routes_rules.py:548` | _evolution_step() 执行失败 | 是 |
| `lottery/routes_rules.py:60` | 战斗回放模块 lottery.battle 尚未就绪 | 是 |
| `lottery/routes_tasks.py:56` | task not found | 否 |

## B 类｜知道没数据，但不知道「怎么才有」（中优先） —— 24 条

| 文件:行 | 原文 | 附 detail |
|---|---|---|
| `lottery/routes_audit.py:311` | 本地暂无开奖数据 | 否 |
| `lottery/routes_audit.py:113` | 本地暂无 eval_details 记录（尚未有核对过的预测） | 否 |
| `lottery/routes_audit.py:123` | 没有可检查的 filters（请求体 items 为空且无 llm_claims 快照） | 否 |
| `lottery/routes_diagnosis.py:177` | 本地暂无开奖数据 | 否 |
| `lottery/routes_llm_eval.py:31` | 本地暂无开奖数据 | 否 |
| `lottery/routes_llm_ops.py:70` | 本地暂无开奖数据 | 否 |
| `lottery/routes_llm_ops.py:82` | 本地暂无开奖数据 | 否 |
| `lottery/routes_llm_ops.py:138` | 本地暂无开奖数据 | 否 |
| `lottery/routes_ml.py:35` | 本地暂无开奖数据 | 否 |
| `lottery/routes_rigor.py:99` | 本地暂无开奖数据 | 否 |
| `lottery/routes_rigor.py:186` | 本地暂无开奖数据 | 否 |
| `lottery/routes_rules.py:88` | 本地暂无开奖数据 | 否 |
| `lottery/routes_rules.py:168` | 本地暂无开奖数据 | 否 |
| `lottery/routes_rules.py:210` | 本地暂无开奖数据 | 否 |
| `lottery/routes_rules.py:259` | 本地暂无开奖数据 | 否 |
| `lottery/routes_rules.py:283` | 本地暂无开奖数据 | 否 |
| `lottery/routes_rules.py:345` | 本地暂无开奖数据 | 否 |
| `lottery/routes_rules.py:399` | 本地暂无开奖数据 | 否 |
| `lottery/routes_rules.py:421` | 本地暂无开奖数据 | 否 |
| `lottery/routes_rules.py:440` | 本地暂无开奖数据 | 否 |
| `lottery/routes_rules.py:466` | 本地暂无开奖数据 | 否 |
| `lottery/routes_rules.py:542` | 本地暂无开奖数据 | 否 |
| `lottery/routes_tasks.py:75` | 暂无数据 | 否 |
| `lottery/routes_tasks.py:90` | 暂无数据 | 否 |

## A 类｜参数错误（用户可自行修正，低优先） —— 5 条

| 文件:行 | 原文 | 附 detail |
|---|---|---|
| `lottery/routes_audit.py:265` | synthetic_bias 只支持 'number' 或留空 | 否 |
| `lottery/routes_methods.py:41` | methods 必须是字符串 | 否 |
| `lottery/routes_portfolio.py:262` | 红球必须是 1~33 内互不相同的 6 个数 | 否 |
| `lottery/routes_portfolio.py:257` | reds 必须是逗号分隔的整数 | 否 |
| `lottery/routes_tasks.py:70` | reds 必须是逗号分隔的整数 | 否 |

## D 类｜其他 —— 1 条

| 文件:行 | 原文 | 附 detail |
|---|---|---|
| `lottery/routes_tasks.py:72` | 必须恰好 6 个不重复红球 | 否 |

---

## 分类判据的局限（务必先读，再引用本清单）

**本清单是启发式分类，不是人工逐条判定。**

判据是关键词匹配：
```
含「必须是 / 只支持 / 不能为空」          → A 参数错误
含「暂无 / 没有可检查」                    → B 空数据
含「尚未就绪 / 失败 / 异常 / not found」   → C 不可行动
含「稍后刷新 / 请检查 / 重启 / 需 ≥N」     → OK 已可行动
```

**已知误差（我实测修正过 3 处）**：
- 首版把 `routes_portfolio.py:326`（「需 ≥300 期」）和 `:360`（「需检查日志或重启服务」）
  误判进 C 类 —— 它们**已经带动作**，是本项目做得对的两条。
- 首版把 `routes_rules.py:403`「刷新失败」误匹配了「刷新」这个动作词。

**所以：引用前请对具体条目做一次人工确认。**
本清单的价值在于**给出"哪一类、多少条、集中在哪"的量级与分布**，
而不是"第 37 条一定错"这种逐条判决。

**如果要用它做验收判据，应当先人工标注一遍**（约 71 条，半小时内可完成）。

---

## 数据来源与可复现性

生成方式（可重跑，结果应与本文件一致）：
```python
# AST 遍历 lottery/routes_*.py，收集 JSONResponse({... "error": <literal> ...})
# 同时记录是否带 "detail" / "how_to_restore" 键
```
**注意**：只统计 `error` 是**字面量**的调用点；f-string 动态拼接的错误文案**不在统计内**。
</note>
