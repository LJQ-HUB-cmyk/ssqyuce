# 项目长期开发上下文

> 本文档是后续开发的工作记忆，项目根目录为 `D:\AI\Deepseek-harness\CP`。

## 1. 项目定位

双色球智能预测分析系统：Python/FastAPI 后端、SQLite 持久化、Docker Compose 部署、静态 HTML/CSS/JavaScript 前端。系统遵守诚实边界：双色球开奖是独立随机事件；任何方法、规律或 LLM/ML 通道都必须通过样本外回测、随机基线和配对检验，多重检验校正后才能讨论是否保留。系统不承诺中奖率，不提供投注建议。

## 2. 当前发布状态

- 发布版本：`v4.0.0`（V12 交付批次）
- 构建标识：`2026-10-V12`
- 当前主分支提交：见 `git log -1`（V6 阶段有效性 + 多维度规律 + LLM 规律工坊提交）
- GitHub：`https://github.com/jiam9069/ssqyuce`
- **部署形态：单节点**（Docker Compose，容器 `ssq-predictor`，端口 `18000`）

  | 节点 | 路径 / 入口 | 状态（2026-10-15 实测） |
  |---|---|---|
  | **本机（bero_de）** | `/root/DSH/应用/ssqyuce` | ✅ **v4.0.0 / 2026-10-V12**，容器 healthy |
  | 公网入口 | `https://ssq.ff88.de` | ✅ 同版本，`/api/info` 已核验 |

  除本机外**没有其他部署节点**。

- **历史节点（均已退役，不要再按它们部署或排查）**：

  | 节点 | 退役情况 |
  |---|---|
  | **ivy-ray-EU** | 2026-09-17 下线删除（容器/目录/镜像清除，18000 释放） |
  | **R71s.AI** | **已不存在**（用户 2026-10-15 确认） |
  | **BF.US** | **已不存在**（用户 2026-10-15 确认）；历史上曾是本机推 GitHub 的中转 |
  | **ZT.JP.Plus** | 机器仍在（`ssh zt_jp_plus` 可登录），但**已不再部署本应用**：无 `/opt/ssqyuce`、无 ssq 容器、无 18000 监听 |

  > 历史记录里「ivy-ray-EU 的数据已迁往 ZT.JP.Plus」这类结论**已失效**——
  > ZT.JP.Plus 上没有 ssq，R71s.AI 与 BF.US 也已不存在。
  > **当前唯一的数据副本就是本机 `/root/DSH/应用/ssqyuce/data/`，因此它的备份是唯一的安全网。**

- V6（2026-10-17）追加：新增 `GET /api/regime/overview`、`POST /api/regime/refresh`、
  `GET /api/discovery/families`、`GET /api/discovery/scan`、`GET/POST /api/llm_lab/{latest,run,register}`、
  `GET /api/backtest/regime`，前端新增「评估报告 → ⏳ 规律时效」工作台；
  `?v=` → `20261017.1`（app.js / style.css 同步）。诊断依据见 `docs/V6_REGIME_DIAGNOSIS.md`。
- 最近验证（2026-10-15，本机）：容器 `healthy`，`/api/info` 返回 v2.1.0 / 2026-10-V5；
  V4 三个端点 + V5 三个端点（`/api/battle/report`、`/api/evolution/status`、`/api/evolution/run`）均 200；
  公网 `https://ssq.ff88.de` 同样返回 v2.1.0，`/static/app.js?v=20261015.2` 返回 `x-cache-status: MISS`（缓存已换新键）。
  工作树干净，本地 HEAD = `origin/main` = GitHub 远端（三方 SHA 一致）。
- 启动预热期 healthcheck 可能短暂 `unhealthy`（回测占满 CPU 使 8s 探活超时），非故障；本机实测预热约 2~2.5 分钟。
- **api.b.ai 网关账户余额已耗尽**（`insufficient_user_quota`，balance=0）：LLM 通道自检返回“模型无返回”，纯统计模式不受影响；需充值或换通道后 LLM 才恢复。

## 3. 已完成能力

### v3.0.0：V10 前置 Gate + 契约冻结 + 前置注册 + 票单分布（2026-10-17）★ 诚实口径再收紧

**触发**：用户反馈「生成下一期预测时仍发现不了规律、AI 作用不明显」。经 4 名独立评审员（架构/统计/AI/发布工程）
评审 + 二次返工评审，结论为：**目标仍不可达（不变），但系统在工程与口径上有多处真实缺陷**。

**交付（全部经全量回归 609 passed）**：
- **V10-00 前置 Gate**：`tests/conftest.py` 统一 DB 隔离（修掉 19 处裸 `config.DB_PATH` 导致
  480 全绿是字母序巧合的真实缺陷）；备份系统真实安装（此前**生产库零备份**）并做恢复演练；
  `migrations.apply` 改批次事务 + 迁移前自动备份 + fail-closed，`/api/health` 暴露 `schema_version`。
- **V10-00a 契约冻结**：`methods.resolve_channels()` 成为出号通道唯一判据（收敛 6 层开关，
  不再新增第 7 套）；`LOTT_LLM_ONLY_OUTPUT` 默认 **1 → 0**（不再让无信息量的 LLM 当出号主力）。
- **V10-02 前置注册**：`hypothesis.evaluate_prereg()` 是 `prereg_ok` 的**唯一写者**，
  接线到 `patterns_runtime` 的 `usable` 判定（阶段持续 + validated + prereg_ok 三条件缺一不可）；
  强制 R1–R6 六条约束（累计口径唯一分母、每批≥10、exp_id 剔除截止期、冻结期 150 期等）。
- **V10-03 票单分布**：`engine.ticket_distribution()` 解析计算 E/var/`var_ratio`/P(≥r红)/EV 比，
  目标函数可插拔（`ev_conserving` 为诚实默认）；修掉 `:1409` 死代码与蓝球零梯度。
- **V10-04 通道 + 报表**：出号统一走 `channels_snapshot()`；前端 `cfgLlm` 不再硬编码 checked
  （localStorage 版本迁移）；**报表按族分列**——旧 `eval_results` 把 LLM 0 与 stat 3 平均成 1.5
  再 `int()` 截断成 1，这是"LLM 到底行不行"永远测不出的根因。
- **V10-05 离线回放**：`replay_results` 表（migration #3，主键含 `mode`）+ 严格 as-of 回放 +
  两套样本物理隔离（`mixable=False`），MDE 与 V6 诊断交叉验证一致（3515 期 → 4.10%）。
- **V10-08 ML 诊断**：Brier 精确分解 + 置换重要性 + **打乱标签对照**；真实数据 `trusted=False`
  （随机彩票的诚实结论），注入条件依赖信号可检出（双向验证）。

**仍不变的诚实边界**：单注红球命中期望恒为 6×6/33=1.0909，蓝球 1/16；六等奖 EV 比恒为 0.15625；
本批次**没有也不可能**提高中奖概率，改进的是找规律的可信度、AI 输出的可验证性、票单分布的可见度。

### v2.1.0：V5 自进化系统（2026-10）★ 首次具备自我完善能力

**用户口径**：准确性 = 基于历史数据查找规律特点 → 用于实战 → **通过实战结果对比**。
（用户已明确表示充分明白彩票随机独立性、不需要反复提醒；故一律用实战对比口径表述。）

**四段闭环**：历史数据 → 战斗回放逐期验证 → LLM 提炼规律 → A/B 实战验证 → 裁决 → 用已验证规律预测。

- **`lottery/battle.py`（49 测试）**：方法级 walk-forward 战斗回放。`replay_method`/`replay_all`/
  `compare_methods`/`battle_report`；红球 Wilcoxon + 蓝球 McNemar + Fisher 合并 +
  Bonferroni/BH 校正；严格只用 `draws[:t]`（双向泄漏测试）；`ml`/`llm` 标 `replayable: False`。
- **`lottery/hypothesis.py`（46 测试）**：确定性统计挖掘 + LLM 提炼可验证假说；
  LLM 失败必降级；假说库生命周期 proposed→validated/rejected/retired，statement 去重累加 attempts；
  落盘 `data/hypotheses.json`（**运行时状态，已 gitignore**）。
- **`lottery/evolution.py`（32 测试）**：`apply_hypotheses`（**只消费 validated**）、
  `apply_structure_rules`、`verify_hypothesis_ab`（配对 A/B）、`predict_with_evolution`、`evolution_status`。
- **每期自动进化**：`api_app._evolution_step()` 接入调度器，在开奖→抓取→在线对照之后、
  生成预测之前跑一轮（挖掘→注册→A/B 验证→裁决）；失败不影响当期预测生成。
- **端点**：`/api/battle/report`、`/api/evolution/status`、`/api/evolution/run`（缺模块降级 503）。
- **前端**：「评估报告 → 🧬 实战战绩」子页（进化总览/战绩榜/两两对比/规律库/进化操作）。

**实测数字（3515 期全量回放，3215 期可评估 × 10 注）**：

| 方法 | 红球均值 | 蓝球命中率 | ≥五等奖率 |
|---|---|---|---|
| stat:freq | 1.0951 | 6.24% | 6.67% |
| blue_coverage | 1.0951 | 6.32% | 6.76% |
| uniform | 1.0926 | 6.30% | 6.69% |
| stat:bayes | 1.0864 | 6.33% | 6.75% |

- 11 组配对：**未校正显著 3 组 → 校正后仅 1 组**（stat:bayes 显著差于 blue_coverage，BH p=0.0187）。
- `uniform` 的 ROI +203.9% 是**单期离群值**（2022128 期 1 注全中 6 红 = 二等奖 ¥180,000）→
  新增稳健口径 `roi_ex_top`（各方法 −75%~−78%），前端两口径并排。
- 9 条假说各 515 期配对 A/B：Δ红球全部落在 ±0.015 → **0 validated / 0 rejected / 9 unchanged**。
  **目前没有任何规律达到显著**，故预测输出等同基线（系统不因「有假说库」宣称超出基线能力）。

**修掉的三个集成缺陷**（都属「看起来能跑、实际断链」）：
1. 71% 挖掘结果被静默丢弃（挖掘端产 `prefer_structure`，白名单不含）→ 2/7 → 7/7。
2. 假说永远停在 proposed：`evolve_once` 无任何环节送 A/B 验证 → 新增 `_evolution_step`。
3. `evolve_once` 把假说库 dict 当假说；`_registry_path` 传 str 时 `path.parent` 崩。

**测试**：全量 **356 → 380 passed**；`?v=` → 20261015.2；APP_VERSION → 2.1.0 / APP_BUILD → 2026-10-V5。

### v2.0.0：V4 诚实口径重构（2026-10）★ 方向纠正型交付

**触发**：用户实报「准确性过低」，要求分析提升准确性的方法、必要时推倒核心算法重做。

**诊断（先量化再动手，见 `docs/V4_ACCURACY_DIAGNOSIS.md`）**：本机 3514 期数据显示
开奖与均匀随机**不可区分**（红卡方 45.18/p=0.061，蓝卡方 8.84/p=0.886）；
7 类规律策略经多重检验校正后**无一显著**；累计评估样本仅 25 注。
结论：**命中率不可提升**，故本次不追命中率，改为「修真实缺陷 + 做数学上可优化的事」。

- **V4-A `lottery/rigor.py`（新增，严格统计检验层）**：`randomness_report`（9 项检验 +
  综合 verdict + `min_detectable_effect`）、`strategy_significance`（t/符号检验 +
  Bonferroni 与 BH-FDR 校正）、`strategy_significance_multi`（整组 p 值上做真 BH）、
  `min_sample_size`、`evaluate_signal`（walk-forward，`hit_series` 含 issue）。
  检验**双向自证**：注入偏置必判 deviation、真随机不得误报。
  **关键约定**：单策略无法做 BH 时 `bh_p=None` + `not-applicable-single-p`，
  **绝不**用 `min()` 把不可计算值当更优值选中（这曾使校正被静默废掉）。
- **V4-B 修掉 `_brier_blend` 样本内过拟合**：原用最近 50 期**样本内** Brier +
  `1/brier` **无界倒数放大**（brier=0.001 时达均匀模型 148.8×）；现为严格
  **walk-forward 样本外** Brier → softmax（温度 0.05/0.02）→ 上限 `MAX_MODEL_WEIGHT=0.6`
  → 收缩 `WEIGHT_SHRINKAGE=0.5`；不可回放方法标注 `weight_source="fixed-fallback"`。
- **V4-C 核心目标改为覆盖优化 + 奖金期望**：`blue_coverage_plan`（n≤16 蓝球互不相同，
  「至少中 1 蓝」n=5: 27.58%→31.25%、n=10: 47.55%→62.50%、n=16: 100%）、
  `portfolio_expected_value`（含 `ev_ratio`/`sixth_ev_ratio`/`jackpot_assumption`）、
  `coverage_optimize(objective="expected_value")`；**修复 `_apply_blue_mode` 在噪声上决策**
  （回退门槛 30 期 → 统计反推的 735 期）。
- **V4-D 诚实口径落到界面**：新增 `/api/rigor/randomness`、`/api/rigor/signal`、
  `/api/portfolio/expected-value`（缺 rigor 时降级 503）；前端比率指标一律带样本量，
  不足时显示「样本不足，不构成结论」；新增「🎲 随机性检验」卡回答「为什么准确率上不去」。
- 测试：新增 4 个 V4 测试文件（rigor 53 / blend 23 / coverage 43 / honest_ui 17），
  全量 **89 → 229 passed**；顺带修掉 2 个测试隔离缺陷（`config.DB_PATH` 裸赋值泄漏空库、
  `builtins.__import__` 打桩拦不住 `from . import rigor`）。
- 前端缓存击穿 `?v=` → `20261015.1`。

**两条必须与功能同时呈现的诚实边界（已写进代码断言）**：
1. 覆盖优化提高**中奖频率**而非期望收益——六等奖 EV 比恒为 `5/32=0.15625`，与注数无关；
   覆盖满 16 蓝「必中」但成本 ¥32 只换回 ¥5，**净亏 ¥27**。
2. 单注完整期望回报约 **¥0.9135**/成本 ¥2.00 → **返还率 45.67%**（每 ¥100 期望亏 ¥54.33）。

### v1.0.2：U9 历史战绩样本口径修正（2026-10）

- 历史战绩卡的期数改为接口实际返回的**去重样本**（跨方法按 issue 求并集），
  不再用字面量 `N`、也不再把各方法 `issues` 相加；`sample_limit` 明确为**查询上限**。
- 样本不足（< 60 期，对齐 `LOTT_METHOD_RECOMMENDATION_MIN_ISSUES`）不给「最佳方法」结论；
  取数失败显式报错，不再伪装成「样本为 0」。
- 「🏆 历史战绩」卡移至「🎫 上期开奖回执」下方。

### v1.0.1：U8 推理型模型适配 + 回执补全（2026-10）

- 设置页保存的模型成为**唯一事实来源**（原为前插，导致旧模型残留在轮转列表产生 404）。
- 推理型模型输出预算自适应（`content` 空而 `reasoning_content` 非空 → 抬到
  `LOTT_LLM_MAX_TOKENS` 重试并持久化到 `data/llm_budget.json`）+ `LOTT_LLM_LONG_TIMEOUT`。
- 观察轮报错聚合全部模型原因；`/api/llm/test` 改为结构化出文探测。
- 上期开奖回执列出上期推荐逐注命中（含红中 X/6、蓝球、奖级）。

### v1.0.0：v3 方案 U1–U7（2026-10）

- **U1 极简预测首页**：7 Tab → 2 层导航（「本期推荐」+「更多」抽屉）；票卡默认折叠 + 匹配度星级；
  复制全部 / 跟我买 / 我的号码诊断 / 开奖倒计时 / 设置浮层。
- **U2 蓝球专项**：`models.blue_specialist` 专用蓝球模型；`_pick_blue_coverage` 蓝球覆盖优先
  （单期覆盖 ~5 → ~10）；`_apply_blue_mode` 运行命中率低于随机基线自动回退均匀 16；
  投注模式 `single / blue_compound / dan_tuo`。
- **U3 自适应降权 + LLM 角色重定位**：`methods.refresh_adaptive_weights` 连续 K 期低于随机基线自动
  降权/移出候选池（production），research 保留全对比；LLM 改为"给定概率分布的组合结构优化"，
  不再自由猜号，蓝球交由 blue_specialist 决定。
- **U4 组合覆盖优化**：`coverage_optimize` 贪心 + 退火；`/api/eval/coverage` 离线对照。
- **U5 复盘 + 跟我买 + 中奖回执**：首页战绩卡、一键复制蓝球覆盖最优子集、开奖回执闭环。
- **U6 推理模型兜底**：`reasoning_content` 非空且 content 为空 → 自动注入 `thinking:disabled` 重试。
- **U7 LLM 实际生成注数模式**：开关 `LOTT_LLM_ONLY_OUTPUT`（默认 1）。LLM 推理时注数以大模型真实产出为准，
  **不再用统计/ML 候选补齐到 `LOTT_N_TICKETS`**；响应新增 `llm_only_output` /
  `shortfall_reason=llm_only_actual_count`。纯统计 / coverage / 复式 / 胆拖模式行为不变。
- 测试：新增 `tests/test_v3_u2_u3_u4.py`、`tests/test_v3_llm_thinking_fallback.py`、
  `tests/test_llm_only_output.py`，全量 74 passed。
- 关键修复：`_pick_blue_coverage` 重复注缺陷根治（补齐分支按对象身份跳过已选票）；
  `/api/predict` 缓存路径 `llm_used` 判断修正为 `startswith("llm:")`。

### M5.1 交互升级（v0.9.0，前端为主）

- 历史回放命中可视化：命中球金框 ✓、奖级徽标、期号下拉命中摘要、汇总指标卡、按命中排序（前端本地对照，不依赖后端 result）。
- 数据分析增强：红球热力三模式（频率/遗漏/遗漏比）、蓝球 16 格面板 + 轨迹、和值/三区/跨度/奇偶走势、
  八类分布直方图（结构指标叠超几何理论值）、遗漏散点与区间分组、号码弹窗近 100 期轨迹；数据源
  `/api/features` + `/api/draws/history`（客户端计算，无新后端依赖）。
- 评估报告重组：综合概览/累计对照/方法对比/ML/LLM 五子页；系统 vs 随机 KPI 对比卡；奖级分布图
  （`lottery/evaluate.py` `aggregate` 新增 `levels_dist`）；累计报表 CI 列 + 10 期滚动趋势图（含 CI 带）。
- 号码弹窗全局化；主要操作按钮补齐 busy 状态。

### M1-M3

- 单页 Tab 工作台：预测中心、数据分析、规律研究、历史回放、评估报告、数据管理、设置。
- 统计规律库、walk-forward 样本外回测、Wilson CI、t/sign/Wilcoxon 检验、BH 校正、A/B/C 分级。
- GBDT + RandomForest 概率模型、Brier 加权融合、概率校准、ML walk-forward 评估。
- LLM 三阶段链路、结构化 evidence/counter_evidence/structure_scores、LLM 离线三通道评估及成本估算。
- 规律挖掘、特征重要性、mining_runs、研究台红牌预警/多选对比、历史回放诊断。

### M4.1

- `eval_meta` 不可变预测/配置快照。
- `eval_details` 逐期逐方法逐注明细，幂等在线对照。
- 累计评估 API、10/30/60 滚动指标、近似 95% CI、CSV 导出。

### M4.2

- `LOTT_METHODS` 支持空值/all、allow-list、`-` deny-list、族名/全方法名。
- `LOTT_METHOD_MODE=production/research`；research 忽略开关进行全方法对比。
- 方法配置校验、原子持久化到 `data/methods_config.json`、启动时校验加载。
- 预测结果含 `requested_tickets`、`actual_tickets`、`shortfall_reason`。
- 方法建议 API：paired sign-test 对比 uniform；只提示、不自动关闭。

### M4.3

- `lottery/notify.py` 支持 Webhook/SMTP。
- 在线对照和调度器触发通知；通道隔离、失败静默降级。
- `/api/notify/status`。

### M4.4

- `lottery/data_check.py`：备用源抓取、期号和开奖号码对账，只读不覆盖主库。
- `draws.source` 来源追溯，主源默认 `17500`。
- `reconcile_runs` 对账审计表。
- `/api/data/reconcile` 与 `/api/data/reconcile/history`。
- 当前仍未完成：完整来源适配器、对账告警通知、特征增量缓存。

### M4.5 首个切片

- `lottery/migrations.py`：有序、幂等 SQLite 迁移，`schema_version` 当前版本 2。
- 存量列/表迁移：`draws.source`、`predictions.evidence_json`、`reconcile_runs`。
- `LOTT_TOKEN` 配置后保护全部 `/api/*`，未配置保持开放兼容。
- `/api/health` 提供 `uptime_seconds`、`api_auth_enabled`、最近 5 条任务。
- Docker Compose 和 `.env.example` 已透传配置。

## 4. 关键文件

- `lottery/config.py`：路径、版本、环境变量、运行时配置。
- `lottery/db.py`：SQLite schema、迁移调用、开奖/预测/评估/任务/审计存取。
- `lottery/migrations.py`：schema migration registry。
- `lottery/engine.py`：预测候选生成、方法开关和 shortfall。
- `lottery/evaluate.py`：离线/在线评估。
- `lottery/api_app.py`：FastAPI 路由、鉴权中间件、健康检查、调度器。
- `lottery/notify.py`：通知通道。
- `lottery/data_check.py`：多源对账。
- `README.md`：用户文档、配置、API、版本记录。
- `docs/M3_M4_PLAN.md`：M3/M4 规划、实施状态、验收与风险。
- `tests/`：回归测试，当前本地全套 **609 passed**（2026-10-17 实测，V10 交付批次）。

## 5. 运行与交付约定

1. 工作目录：本机为 `/root/DSH/应用/ssqyuce`（**不要**把 DSH checkout 当作项目目录）。
   （历史记录里的 `D:\AI\Deepseek-harness\CP` 是早期 Windows 环境，已不适用。）
2. 使用 `read`/`edit`/`write`/`glob`/`grep` 检查和修改项目文件；不要提交运行态密钥和数据：
   `.env`、`data/llm_config.json`、`data/methods_config.json`、`data/hypotheses.json`、
   `data/adaptive_state.json`、`data/llm_budget.json`、SQLite/raw 数据。这些已在 `.gitignore` 中。
3. 每次较大修改必须本地运行 `python -m pytest -q`、compileall、前端 `node --check`、`git diff --check`。
4. **提交后直接推 GitHub**（2026-10-15 实测本机 HTTPS 可直连、PAT 存于 `/root/DSH/githubtoken.txt`）：
   ```
   git -c credential.helper= push "https://jiam9069:${PAT}@github.com/jiam9069/ssqyuce.git" main
   ```
   推送后用 `git update-ref refs/remotes/origin/main <sha>` 同步本地跟踪引用
   （走显式 URL 推送不会更新它），并以 GitHub API 核验落地。
5. **本机部署**：`docker compose up -d --build`，然后轮询 `docker inspect --format='{{.State.Health.Status}}' ssq-predictor`。
   首次启动预热（fetch/backtest/ML）约 **2~2.5 分钟**，期间 `/api/health` 不可用、healthcheck 显示 `starting`——
   这不是故障，不要据此判失败。容器 `mem_limit=768m`，预热期实测约 630MB（82%），偏紧。
6. Token/SMTP/Webhook 只写服务器 `.env`，最终回复不得展示秘密。
7. **前端改动必须同步升 `?v=`**（见 `DEPLOY.md`）：入口节点对 `/static/` 有 7 天代理缓存、
   浏览器 24h 缓存，不升版本号用户刷新也拿不到新前端。
8. 多节点部署状态**以实测为准**，不要沿用旧记录（详见第 2 节的状态表）。

## 6. 下一步优先级

P0：
- 修正长任务状态/清理机制，避免 `tasks` 中永久 running 或历史结果过大；补任务列表/详情 API 的可观测性。
- 补生产级备份与恢复校验，覆盖 SQLite、LLM/runtime config、raw 快照和前端静态资源。
- 增加最近抓取、在线评估、通知、对账状态到 health/运维摘要。

P1：
- 多源对账分级告警、可选通知和对账日报；增加真实备用源适配器。
- 特征按 issue 增量缓存，避免每次全量重算。
- 通知幂等、审计和重试策略。
- M4.2 方法建议完善 BH 校正、决策历史和人工确认记录；禁止自动关闭生产方法。

P2：
- 长任务统一异步化，完善任务取消/超时/重试。
- 更完整的 schema migration 测试和启动自检。
- 反向代理/HTTPS/限流文档与公网安全加固。

## 7. 验收原则

- 不能把随机波动表述为稳定命中率提升。
- 不能宣称 M4.1 真实数据验收完成，除非有开奖后的真实 `online_check` 和累计报告证据。
- 每次代码改动都要同时更新 README/规划文档/版本字段（如影响发布状态）。
- 完成后必须记录本地测试、GitHub 提交，以及本机重建部署后的健康验证结果（当前为单节点）。
