# M4 产品精简评审（第 2 点：砍掉哪些功能）

> 评审对象：`/root/DSH/应用/ssqyuce`（双色球预测系统，v4.1.0 / `2026-10-V12`，公网 `https://ssq.ff88.de`）
> 评审视角：产品经理 + 技术产品负责人；目标是「单人娱乐系统」的**减面**，不是「功能升级」
> 评审基线：HEAD = `ca23283`（`chore(v12): 4.0.2 → 4.1.0`），评审日期 2026-10-19
> 评审方式：**只读**。全部数字为本机实测，命令见附录 A；本次评审只写本文件，未改动任何 `.py` / 前端 / 配置 / 数据库，未重启容器。
> 上游基线：`docs/analysis/LEAD_VERDICT.md` §五（P4「产品侧为什么用户永远等不到有规律」）——本文不重复论证，只把它转成可执行的精简动作。

---

## 0. 一句话结论

**建议认可方向，但必须重新定义「精简」的靶子。**

用户提的「精简不必要的功能」，如果按字面理解成「删接口」，收益很小——因为**真正的成本不在接口数量，而在同一屏上有 6 种互相矛盾的「规律」定义**。本机实测：

- 80 个路由注册（79 个唯一路径）中，**25 个前端从未调用**，其中 **9 个连测试和非-api 模块也不引用**——7 个是真·死代码（A 档删除），2 个是多源对账（安全功能，应「修接线」而不是删，B 档）。
- 但即使删掉这 7 个，用户看到的困惑一点没减——因为首屏「本期规律使用情况」、评估页「形态规律」、评估页「规律时效」、更多→「规律研究」四个入口，**同时用「规律」这个词讲了 6 件判据完全不同的事**，其中「可用」一词同时意味着「能被使用」（真）与「有预测价值」（假）。
- 配置侧：`config.py` 有 **96 个顶层常量 / 80 个 `LOTT_*` 环境变量**，其中 **5 个从未被读取**；方法通道历史上堆了 **6 层开关**，代码注释已明确写「不再新增第 7 套」（`lottery/methods.py:343-346`）。
- 生产数据侧：`mining_artifact.json` 自 **2026-10-05** 未更新（14 天）、`adaptive_state.json` 是空字典 `{}`、`reconcile_runs / replay_results / llm_audit / llm_eval_results / mining_runs` **全部 0 行**——对应的 5 个功能在生产上从未真正跑过（挖掘 / LLM 离线评估 / 回放落库 / 契约审计 / 多源对账），**其中 3 个在界面上有按钮**（`⛏️ 自动挖掘`、`🧠 LLM 离线评估`、`运行最近 20 期诊断`）。

因此本文的优先级是：**语义层收敛（§4）＞ 删死代码（§5 A 档）＞ 合并重复入口（§7 C 档）＞ 降级展示（§6 B 档）**。只做 §5 是低风险低收益；只做 §4 是高收益但需要勇气；**建议 §4 + §5 + §7 一起做**。

**评分**

| 维度 | 分数 | 理由 |
|---|---|---|
| 现状可诊断性 | **4 / 5** | 端点/配置/测试/DB 都能被机器枚举，证据链完整；扣分项是没有「端点使用率」这种现成指标，必须自己造 |
| 精简的紧迫性 | **5 / 5** | 6 种「规律」同屏、5 个功能从未跑过、3 个死按钮仍在界面上、6 层开关 |
| 零风险删减空间 | **3 / 5** | 真·零风险只有 7 个端点 + 5 个配置 + 5 个函数（+1 个 129 KB 死数据文件）；其余都要改前端或改 DB |
| 与第 1/3 点的协同风险 | **3 / 5** | 至少 **3 个「看似冗余」的组件是第 3 点要用的积木**（§11），删错会返工 |
| 目标形态可达性 | **4 / 5** | 目标值（80→66 端点、14→9 视图、96→68 配置）均可机检；理想线 62 端点 |

---

## 1. 评审基线与可复现命令

| 口径 | 本次实测 | 说明 |
|---|---|---|
| 路由注册 | **80** | 79 个 `@app.get/post` + 1 个 `@app.api_route`（`/api/predict` 同时 GET+POST） |
| 唯一路径 | **79** | `/api/config/llm` 有 GET + POST 两条注册 |
| 方法分布 | GET **55** / POST **26** | `api_route` 拆成 GET+POST 计入 |
| 前端视图 | **14** | 7 个 tab + 7 个 eval 子页 |
| HTML 按钮 | **54** | `grep -c '<button' web/index.html` |
| JS 函数 | **180** | `web/static/app.js` 4203 行 |
| 配置常量 | **96** | `lottery/config.py` 顶层赋值 |
| `LOTT_*` 变量 | **80**（config.py 内）+ 3（config 外） | 另有 `LOTT_SLOW_TESTS`（测试基建） |
| 测试 | 56 文件 / **1057 collected** / 920 个 `def test_` | 上一次交付记录为「1036 passed」 |
| 后端模块 | **38** / 28853 行 | `api_app.py` 3381 行、`engine.py` 2988 行 |
| DB 表 | **14** | 11 在 `db.py`，2 在 `migrations.py`（`replay_results` / `llm_audit`），1 是 `schema_version` |

> **与 Lead 基线的口径差异**：Lead 记「81 个端点」，本机按装饰器枚举为 **80**。差异来自 `/`（首页）是否计入、以及 `/api/config/llm` 的 GET/POST 是否算两个。本文统一用「80 个注册 / 79 个唯一路径」，两者都标出来，避免后续验收对不上账。

---

## 2. 现状盘点（六张底账）

### 2.1 端点分组

| 组 | 数量 | 组 | 数量 |
|---|---|---|---|
| `/api/eval/*` | **11** | `/api/shape/*`、`/api/multiscale/*`、`/api/regime/*`、`/api/discovery/*`、`/api/evolution/*`、`/api/audit/*` | 各 2 |
| `/api/llm/*` | **7** | `/api/data/*`、`/api/draws/*`、`/api/patterns/*`、`/api/predictions/*`、`/api/tasks/*`、`/api/config/*`、`/api/methods/*`、`/api/rigor/*` | 各 2 |
| `/api/rules/*` | **6** | `/api/health`、`/api/info`、`/api/refresh`、`/api/features`、`/api/predict`、`/api/notify`、`/api/diagnose`、`/api/replay`、`/api/registry`、`/api/samples`、`/api/backtest`、`/api/battle`、`/api/l2`、`/api/experiments`、`/` | 各 1 |
| `/api/llm_lab/*` | **4** | | |
| `/api/mining/*`、`/api/ml/*`、`/api/portfolio/*` | 各 3 | | |

**11 个 `/api/eval/*` 是最大的单组**，而前端把其中 11 个中的 8 个塞进同一个 tab 的 7 个子页——这是「评估」这个功能被反复扩建的直接证据。

### 2.2 前端视图与按钮

侧边栏只有 2 个按钮（`🎯 本期推荐` + `📂 更多`），其余全部收进「更多」抽屉（`web/index.html:50-54`、`630-640`）——说明 **V4-U1 已经做过一轮「极简首页」**，本轮精简是它的延续，不是新命题。

| 视图 | HTML 行 | 子页 | 按钮 | 主要端点 |
|---|---|---|---|---|
| `tab-predict` 本期推荐 | 60-120 | — | 6 | predict / predictions / rigor |
| `tab-analysis` 数据分析 | 121-201 | 3 个窗口 | 0 | features / draws |
| `tab-patterns` 规律研究 | 202-248 | — | 3 | patterns / patterns/backtest / mining |
| `tab-replay` 历史回放 | 249-272 | — | 0 | replay/diagnose |
| `tab-eval` 评估报告 | 273-502 | **7 个** | 20 | eval ×8 / ml ×2 / multiscale ×2 / shape / samples / registry / discovery ×2 / regime ×2 / rules ×2 / llm_lab ×3 / battle / evolution ×3 |
| `tab-data` 数据管理 | 503-517 | — | 3 | refresh / tasks / features |
| `tab-settings` 设置 | 518-628 | 2 卡 | 4 | config/llm ×2 / llm/test / methods ×2 |

`tab-eval` 单页 **230 行 HTML / 20 个按钮 / 7 个子页**，占全部视图 HTML 的 35%，是精简的第一优先级。

### 2.3 配置项（96 个顶层常量，按族分组）

| 族 | 数量 | 默认值要点 | 备注 |
|---|---|---|---|
| `LLM_*` | **20** | `LLM_MODEL=minimax-m3`、`LLM_MAX_TOKENS=32000`、`LLM_TIMEOUT=60`、`LLM_TOTAL_TIMEOUT=75`、`LLM_SAMPLES=3` | 另有 `LLM_EVAL_*` 5 个、`LLM_VERIFY_*` 2 个 |
| 路径/版本 | 11 | `APP_VERSION=4.1.0`、`APP_BUILD=2026-10-V12`、`DB_PATH` | `DATA_URL` 硬编码 |
| `NOTIFY_*` | **8** | webhook + SMTP 7 项全为空 | 生产 `.env` 里 8 项**全部已配置** |
| `ML_*` | 7 | `ML_ENABLED=1`、`ML_MIN_START=300`、`ML_CAL_CV=2` | |
| `ADAPTIVE_*` | 6 | `ADAPTIVE_ENABLED=1`、`ADAPTIVE_K=20` | 生产状态文件是 `{}`（§3.3） |
| `BLUE_*` | 6 | `BLUE_MODE=model`、`BLUE_COVER=1` | |
| 方法开关 5 件套 | 5 | `METHODS_RAW` / `METHOD_MODE` / `METHODS_SPEC` / `METHODS_SOURCE` / `CHANNELS` | 全部由 `_methods` 派生 |
| `COVERAGE_*` | 4 | `COVERAGE_R=3`、`COVERAGE_TRIALS=3000` | `COVERAGE_OBJECTIVE` 有 8 个别名 |
| `LIFECYCLE_*` | 4 | `K_DEMOTE=2`、`K_UPGRADE=2`、`MIN_DWELL=3` | |
| 其余 | 25 | `N_TICKETS=5`、`STRICT_MODE=0`、`SCHEDULER_ENABLED=0`（compose 注入为 1）等 | 含 **5 个从未被读取**的（§5 A8） |

**config.py 之外还有 3 个 `LOTT_*`**（不在库里统一管理，属于配置漂移）：
`LOTT_USD_CNY` / `LOTT_LLM_PRICE_PER_1M_YUAN`（`lottery/llm_client.py:50,66`）、`LOTT_BOOT_PREDICT`（`entrypoint.sh:18`）。

**配置项的「多层开关历史」**（`lottery/methods.py:343-346` 原文）：

```
#   项目已有 6 层开关（LOTT_METHODS / LOTT_METHOD_MODE / LOTT_LLM_ONLY_OUTPUT /
#   LOTT_LLM_DISABLED / methods_config.json / .env），v1.0 方案还想加第 7 套
#   LOTT_PREDICT_CHANNELS —— 必然造成设置漂移。本函数把它们收敛为一个返回体。
```

这条注释本身就是一个产品结论：**开关不需要更多，需要更少。**

### 2.4 测试主题（按版本前缀）

| 前缀 | 文件 | 主题 |
|---|---|---|
| `test_v12_*` | **11** | ai_audit / complement / cpu_governance / data_audit / dimension_exec / l2_exact / layer_isolation / portfolio / selfcheck / shape_lock / shape_rules |
| `test_v10_*` | **12** | calibration / channel_wiring / channels / cost / frontend_cards / llm_arm / migrations / ml_calibration / regime_anchor / registry / replay / ticket_distribution |
| `test_v4_*` | **9** | battle / blend / coverage / evolution / evolution_step / evolution_ui / honest_ui / hypothesis / rigor |
| `test_m4_*` / `test_m4_2/3/4/5` | **8** | api / engine / methods / notify / data_check / db_source / engineering / eval |
| `test_v11_*` | **4** | multiscale / multiscale_api / phase_usable / rule_lifecycle |
| `test_v3_*` | **4** | llm_budget / llm_thinking_fallback / u2_u3_u4 / u9_frontend_track_record |
| 其余 | 8 | v6 regime / v7 rules_closure / v8 eras_tasks / v5? strict / mining / llm ×3 |

最大的三个测试文件是 `test_v6_regime.py`(56)、`test_v4_rigor.py`(53)、`test_v11_rule_lifecycle.py`(46)——**恰好对应「阶段/规律」这条线**。这条线上测试最多、产出最少（0 条可用），是「维护成本高于价值」的典型。

### 2.5 生产 DB 行数（`data/ssq.db`，只读查询）

| 表 | 行数 | 判读 |
|---|---|---|
| `draws` | 3516（至 `2026119` / 2026-10-18） | 健康 |
| `predictions` | 40 | 健康 |
| `patterns` | **47** | **0 条 A / 8 条 B / 39 条 C**；其中 **9 条 `avg_hits=0`、13 条 `avg_fav_size=0`**（退化记录） |
| `eval_meta` | 28 | 健康 |
| `eval_results` | **6** | 偏少 |
| `eval_details` | **35** | 方法分布：`llm:*` 28、`ml` 2、`stat:*` 5、**`uniform` 0 条** |
| `features` | 8 | **写入方存在、读取方为 0**（`db.load_features` 无调用者） |
| `tasks` | 6 | 健康 |
| `llm_eval_results` | **0** | LLM 离线评估从未产出结果 |
| `mining_runs` | **0** | 挖掘管道从未在本库跑过 |
| `replay_results` | **0** | 离线回放从未落库 |
| `llm_audit` | **0** | V12-13 契约审计从未运行 |
| `reconcile_runs` | **0** | 多源对账从未运行 |

### 2.6 数据目录状态文件（时间戳即使用痕迹）

| 文件 | 大小 | 最后修改 | 判读 |
|---|---|---|---|
| `patterns_runtime.json` | 21 KB | 2026-10-19 | 活（每期刷新） |
| `hypotheses.json` | 58 KB | 2026-10-19 | 活（前置注册簿） |
| `llm_cost_log.jsonl` | 10 KB | 2026-10-19 | 活 |
| `rule_lifecycle.json` | 335 B | 2026-10-19 | 活（但只有 335 字节 → 几乎空） |
| `evolution_state.json` | 207 B | 2026-10-19 | 活；`last_resolution` = `{validated: [], rejected: [], insufficient: 0, unchanged: 33}` |
| `adaptive_state.json` | **2 B** | 2026-10-19 | **`{}` 空字典** → 自适应降权从未生效 |
| `mining_latest.json` / `mining_artifact.json` | 0.8 / 129 KB | **2026-10-05** | **死**（14 天未更新） |
| `llm_budget.json` / `llm_config.json` | 73 / 208 B | 2026-10-07 | 陈旧但无害 |
| `ssq.db.pre-migrate-v2` / `-v3` | 1.4 MB × 2 | 2026-10-17 | 迁移安全网（**不能删**） |

---

## 3. 端点的真实使用率：用前端 fetch 做硬证据

> 方法：把 80 条路由的路径字符串去 `web/static/app.js` + `web/index.html` 里做**带边界**的精确匹配（`/api/eval/coverage` 不会被 `/api/eval` 误命中），再与 `tests/*.py` 和非-api 后端模块做同样的匹配。这是「单人娱乐系统」里最硬的「有没有人用」证据——比任何主观判断都强。

### 3.1 全量端点证据矩阵（80 条，四项证据中的三项 + 行号）

> 「非-api 模块」列 = 除 `api_app.py` 外的 37 个模块里是否出现该路径字符串（用于识别「端点被内部代码间接引用」的情况）。

| # | 端点 | 方法 | 行 | 前端 | 测试 | 非-api 模块 |
|---|---|---|---|---|---|---|
| 1 | `/api/data/reconcile` | GET | 55 | — | — | — |
| 2 | `/api/data/reconcile/history` | GET | 61 | — | — | — |
| 3 | `/api/health` | GET | 67 | ✔ | v4_honest_ui, m4_5_engineering, v10_migrations | migrations |
| 4 | `/api/info` | GET | 91 | ✔ | v4_honest_ui, v10_channel_wiring, m4_5_engineering… | methods |
| 5 | `/api/refresh` | POST | 116 | ✔ | — | — |
| 6 | `/api/draws/latest` | GET | 126 | — | — | — |
| 7 | `/api/draws/history` | GET | 135 | ✔ | — | — |
| 8 | `/api/features` | GET | 147 | ✔ | — | — |
| 9 | `/api/patterns` | GET | 156 | ✔ | — | — |
| 10 | `/api/patterns/backtest` | POST | 171 | ✔ | — | — |
| 11 | `/api/predict` | GET+POST | 256 | ✔ | v10_cost, strict_mode, llm_failfast… | — |
| 12 | `/api/predictions/last` | GET | 391 | ✔ | v8_eras_tasks | config |
| 13 | `/api/eval` | GET | 434 | ✔ | — | — |
| 14 | `/api/eval/backtest` | POST | 440 | ✔ | — | — |
| 15 | `/api/eval/coverage` | POST | 449 | — | v3_u2_u3_u4 | — |
| 16 | `/api/eval/online` | POST | 496 | ✔ | — | — |
| 17 | `/api/notify/status` | GET | 515 | — | — | — |
| 18 | `/api/eval/cumulative` | GET | 521 | ✔ | v3_u2_u3_u4 | methods |
| 19 | `/api/eval/by-method` | GET | 533 | ✔ | v10_channel_wiring | — |
| 20 | `/api/eval/meta` | GET | 544 | — | — | — |
| 21 | `/api/eval/recommendations` | GET | 549 | ✔ | m4_2_api | — |
| 22 | `/api/eval/export.csv` | GET | 556 | ✔ | — | — |
| 23 | `/api/eval/llm/run` | POST | 573 | ✔ | — | — |
| 24 | `/api/eval/llm/latest` | GET | 610 | ✔ | — | — |
| 25 | `/api/tasks` | GET | 621 | ✔ | — | — |
| 26 | `/api/tasks/{task_id}` | GET | 626 | ✔ | — | — |
| 27 | `/api/diagnose` | GET | 636 | ✔ | — | — |
| 28 | `/api/mining/run` | POST | 655 | ✔ | — | — |
| 29 | `/api/mining/latest` | GET | 679 | — | — | — |
| 30 | `/api/replay/diagnose` | POST | 686 | ✔ | — | — |
| 31 | `/api/mining/reports` | GET | 719 | ✔ | — | — |
| 32 | `/api/predictions/history` | GET | 727 | ✔ | — | — |
| 33 | `/api/config/llm` | GET | 753 | ✔ | v3_llm_budget | — |
| 34 | `/api/config/llm` | POST | 775 | ✔ | v3_llm_budget | — |
| 35 | `/api/llm/test` | POST | 830 | ✔ | v3_llm_budget | — |
| 36 | `/api/methods/status` | GET | 890 | ✔ | m4_2_api | — |
| 37 | `/api/methods/config` | POST | 903 | ✔ | m4_2_api | — |
| 38 | `/api/ml/status` | GET | 936 | ✔ | — | ml_model |
| 39 | `/api/ml/eval` | POST | 943 | ✔ | — | evaluate |
| 40 | `/api/rigor/randomness` | GET | 1025 | ✔ | v4_honest_ui | config |
| 41 | `/api/rigor/signal` | GET | 1105 | ✔ | v4_honest_ui | config |
| 42 | `/api/portfolio/expected-value` | GET | 1216 | — | v4_honest_ui | config |
| 43 | `/api/portfolio/distribution` | GET | 1299 | ✔ | v10_frontend_cards | — |
| 44 | `/api/registry/status` | GET | 1351 | ✔ | v10_frontend_cards | — |
| 45 | `/api/ml/diagnosis` | GET | 1458 | ✔ | v10_frontend_cards | — |
| 46 | `/api/llm/arm` | GET | 1642 | — | v10_frontend_cards | — |
| 47 | `/api/shape/rules` | GET | 1666 | ✔ | v12_shape_rules | shape_rules |
| 48 | `/api/shape/constraints` | GET | 1683 | — | v12_shape_rules | — |
| 49 | `/api/multiscale/mde-table` | GET | 1695 | ✔ | v11_multiscale_api | — |
| 50 | `/api/multiscale/scan` | GET | 1732 | ✔ | v11_multiscale_api | — |
| 51 | `/api/samples/side-by-side` | GET | 1760 | ✔ | v10_frontend_cards | — |
| 52 | `/api/llm/cost` | GET | 2044 | — | v10_cost | — |
| 53 | `/api/llm/decision/status` | GET | 2076 | — | v10_cost | — |
| 54 | `/api/llm/decision/restore` | POST | 2103 | — | v10_cost | — |
| 55 | `/` | GET | 2137 | ✔ | 60 个测试文件均引用（TestClient 首页） | 全部 |
| 56 | `/api/regime/overview` | GET | 2422 | ✔ | v8_eras_tasks | — |
| 57 | `/api/regime/refresh` | POST | 2510 | ✔ | — | — |
| 58 | `/api/discovery/families` | GET | 2529 | ✔ | — | — |
| 59 | `/api/discovery/scan` | GET | 2545 | ✔ | — | — |
| 60 | `/api/llm_lab/latest` | GET | 2581 | ✔ | — | — |
| 61 | `/api/llm_lab/run` | POST | 2603 | — | — | — |
| 62 | `/api/llm_lab/register` | POST | 2631 | ✔ | — | — |
| 63 | `/api/backtest/regime` | GET | 2650 | ✔ | — | — |
| 64 | `/api/rules/lifecycle` | GET | 2683 | — | v11_rule_lifecycle | — |
| 65 | `/api/rules/lifecycle/run` | POST | 2713 | — | v11_rule_lifecycle | — |
| 66 | `/api/rules/runtime` | GET | 2748 | — | v12_dimension_exec | discovery |
| 67 | `/api/rules/refresh` | POST | 2771 | ✔ | — | — |
| 68 | `/api/rules/closed-loop` | POST | 2790 | — | — | — |
| 69 | `/api/rules/closed-loop/async` | POST | 2852 | ✔ | v8_eras_tasks | config |
| 70 | `/api/llm_lab/run/async` | POST | 2875 | ✔ | v8_eras_tasks | config |
| 71 | `/api/battle/report` | GET | 2894 | ✔ | — | — |
| 72 | `/api/evolution/status` | GET | 2941 | ✔ | — | — |
| 73 | `/api/evolution/run` | POST | 2964 | ✔ | v4_evolution_step, v12_cpu_governance | — |
| 74 | `/api/llm/audit/latest` | GET | 3017 | — | — | ai_audit（仅注释提及） |
| 75 | `/api/llm/audit/run` | POST | 3048 | — | — | — |
| 76 | `/api/l2/distribution` | GET | 3145 | — | v12_l2_exact | — |
| 77 | `/api/portfolio/marginal` | GET | 3172 | — | v12_l2_exact | — |
| 78 | `/api/experiments/randomness-selfcheck` | POST | 3197 | — | v12_selfcheck | selfcheck |
| 79 | `/api/audit/anomaly/latest` | GET | 3251 | — | v12_data_audit | data_audit |
| 80 | `/api/audit/anomaly/run` | POST | 3268 | — | — | data_audit |

### 3.2 25 个「前端从未调用」的端点

```
/api/data/reconcile            /api/data/reconcile/history   /api/draws/latest
/api/eval/coverage             /api/notify/status            /api/eval/meta
/api/mining/latest             /api/portfolio/expected-value /api/llm/arm
/api/shape/constraints         /api/llm/cost                 /api/llm/decision/status
/api/llm/decision/restore      /api/llm_lab/run（同步）        /api/rules/lifecycle
/api/rules/lifecycle/run       /api/rules/runtime            /api/rules/closed-loop（同步）
/api/llm/audit/latest          /api/llm/audit/run            /api/l2/distribution
/api/portfolio/marginal        /api/experiments/randomness-selfcheck
/api/audit/anomaly/latest      /api/audit/anomaly/run
```

**这 25 个里有 13 个是 V12（4.0.0/4.1.0）新交付的**——即最新一批交付**完全没接前端**。这不必然是错（可以是有意的「后端先行」），但意味着：**V12 的用户可见价值为 0，而维护成本已经产生**。

### 3.3 用 DB 行数与状态文件交叉验证（不是猜，是读数）

| 端点所属功能 | 前端 | DB / 文件证据 | 结论 |
|---|---|---|---|
| `/api/data/reconcile` | — | `reconcile_runs` **0 行**；`LOTT_BACKUP_DATA_URL` **未在 `docker-compose.yml` 注入**（compose 里 env 列表无此项，只有 `.env.example` 有） | 生产上恒定返回 `not_configured`，**功能从未生效** |
| `/api/mining/*` | ✔ 有按钮 | `mining_runs` **0 行**；`mining_artifact.json` 停在 **2026-10-05** | 「⛏️ 自动挖掘」按钮点了也不会在库里留痕 |
| `/api/replay/diagnose` | ✔ 有按钮 | `replay_results` **0 行** | 回放样本不落库（只在请求内算） |
| `/api/llm/audit/*` | — | `llm_audit` **0 行** | V12-13 从未运行 |
| `/api/eval/llm/run` | ✔ 有按钮 | `llm_eval_results` **0 行** | 「🧠 LLM 离线评估」从未产出 |
| `ADAPTIVE_*`（无端点，但每期被 `/api/info` 读取） | — | `adaptive_state.json` = **`{}`**；根因：`refresh_adaptive_weights()` 依赖 `uniform` 基线行，而 `eval_details` 里 **`uniform` 0 条**（35 行只有 `llm/ml/stat`）→ 结果恒为空 → 降权恒不触发（`lottery/methods.py:245-246`） | **默认开启但恒不生效**，纯死重 |
| `patterns` 表（规律研究 tab） | ✔ 有 tab | **0 条 A 级**；13 条 `avg_fav_size=0`、9 条 `avg_hits=0`（退化记录） | 首屏可读的「规律库」里没有一条可用结论 |
| 形态规律 | ✔ 有卡 | 5 条规则的理论通过率 0.799~0.9999，实测 0.799~0.9999 **逐条吻合** → 恒成立 | 见 §4 |

---

## 4. 语义层精简：把 6 种「规律」收敛成 1 套命名（比删代码更值钱）

### 4.1 实测到 **6** 种，不是 5 种

Lead 在 `LEAD_VERDICT.md` §五 已认定 5 种。本次实测发现**第 6 种**，且它恰好是用户点进「🔬 规律研究」时看到的那一个：

| # | 含义 | 数据源（代码位置） | 当前生产值 | 用户在哪看到 | 判据 |
|---|---|---|---|---|---|
| ① | **统计显著**（L1） | `hypothesis` 注册簿 + `regime` 阶段 | **0 条** | 首屏「🧩 本期规律使用情况」的 `usable` | 阶段持续 + `validated` + `prereg_ok` + 非 llm（`patterns_runtime.py:126-132`） |
| ② | **形态恒等约束**（L2） | `lottery/shape_rules.py` | **5 条「on」**，理论率 0.7992~0.9999，实测逐条吻合 | 评估页「🧬 形态规律（组合恒等约束）」 | 组合恒等式，**不需要 p 值**；恒成立 |
| ③ | **阶段有效性** | `regime` / `discovery` 扫描 | **0 条**（已否证） | 评估页「⏳ 阶段有效性总览」+ 更多→「规律时效」 | 阶段 `p_adj < α` |
| ④ | **生命周期** | `rule_lifecycle`（`data/rule_lifecycle.json` 仅 335 B） | **未纳管** | **无前端**（`/api/rules/lifecycle` 前端 0 调用） | `K_DEMOTE=2` / `K_UPGRADE=2` / `MIN_DWELL=3` |
| ⑤ | **LLM 洞察** | `hypothesis` 里 `source=="llm"` | **0 条且被代码永久排除** | 评估页「🤖 AI 规律闭环」按钮（常亮，跑 1~8 分钟必不变） | `patterns_runtime.py:125`：`source=="llm"` 永不 `usable` |
| ⑥ | **规律库 A/B/C 等级** | `patterns` 表（`lottery/backtest.py:272` `run_all_backtests`） | **0 A / 8 B / 39 C** | 「🔬 规律研究」tab（`web/index.html:202`） | BH 校正后的 `grade` |

**关键结构性发现：①③④⑤ 走 `patterns_runtime` 快照，⑥ 走 `patterns` 表，② 走 `shape_rules`——三套互不相识的规则源同时进同一个引擎。**

证据（`lottery/engine.py`）：

```python
2339    stats = F.compute_features(draws)
2340    patterns = db.load_patterns()        # ← ⑥ patterns 表（A/B/C）
2424    # V7：规律 → 出号
2425    from . import patterns_runtime
2426    _rule_snap = patterns_runtime.load_snapshot(current_issue=issue)   # ← ①③④⑤
2593    "patterns_summary": {                # ← ⑥ 的等级计数，直接进返回值给前端
2594        "A": sum(1 for p in patterns if p["grade"] == "A"), ... }
```

而 `②` 通过 `constraint_ctx(draws)` 走另一条路（`engine.py:2341`）。**一个请求里同时加载三个「规律」体系，且没有任何一层知道另外两层的存在。**

### 4.2 「可用」这个词本身就是债务

`grep -o '可用' web/static/app.js` 命中 **38 处**；去重成行后，含「可用规律 / 条可用」的有 **9 行**（`index.html` 2 行 + `app.js` 7 行），其中**用户可见 5 处**，而这 5 处的含义在 ①②③ 之间来回跳，而这 6 处的含义在 ①②③ 之间来回跳：

| 位置 | 原文 | 实际含义 |
|---|---|---|
| `app.js:464` | `metricItem("可用规律", Number(r.usable \|\| 0), "阶段持续有效 + A/B 通过")` | ①（当期为 0） |
| `app.js:448` | `(r.usable ? '（可用 ' + r.usable + ' 条）' : "")` | ① |
| `app.js:468-471` | `"长期可用 / 监控 / 否决"` + `"另：阶段可用 N 条（3515 期未获支持，仅观察）"` | ①+③ 混排 |
| `app.js:2211` | `// 作为严格模式的"可用规律"，LLM 在此约束下生成注单` | ②（恒成立） |
| `index.html:331` | `作为严格模式的「可用规律」，LLM 在此约束下生成的注单，形态必然合理` | ② |
| `index.html:85` | `规律只有在「阶段持续有效 + 通过 A/B 验证」后才被标记为可用` | ①（与 ②⑥ 冲突） |

**同一个词，在同一屏上，指三件判据互斥的事。** 这比「有 6 个功能」严重得多——因为用户会把它读成一个意思。

值得表扬的是 V12-04 已经拆掉了**数值层的跨层求和**（`patterns_runtime.py:220-247`，注释明确写「旧写法 `"usable": _n_long + _n_phase` …… 相加会让首屏出现『5 条可用，其中 0 条有预测力』」），但**文案层没拆**——`app.js:448/464` 仍在用 `usable` 单字段渲染，`index.html:334` 仍把 ② 叫「可用规律」。

### 4.3 建议的统一契约（可直接作为第 3 点 Rule 抽象的输入）

**命名层**

1. 「规律」一词在**用户可见文案里全部停用**，改为 **「证据项」**（EvidenceItem）。理由：它同时被 6 种判据占用，无法通过加定语救活。
2. 每个证据项**强制**携带 `layer ∈ {L1, L2, L3}`，`layer` 缺失视为非法（构造期抛错，不是默认填 L1）。
3. 每一层有**自己的** `verdict` 词表，**跨层不可比**：

| layer | 含义 | `verdict` 词表 | 计数语义 | 禁止 |
|---|---|---|---|---|
| **L1** | 预测型（统计偏离） | `detected` / `not_detected` / `insufficient` | 「检出 / 未检出」 | 不许说「可用/有效」 |
| **L2** | 结构型（组合恒等） | `satisfied` / `violated` | 「满足率 5/5」 | 不许说「显著 / p 值」 |
| **L3** | 决策型（AI 岗位） | `correct` / `wrong` / `not_done` | 「对 / 错 / 没做」分列 | 不许把 `not_done` 并入分母 |

4. **禁止相加**：任何 API 响应里不得出现跨层求和字段；三层计数并列返回。加一条反向测试（现有 `tests/test_v12_layer_isolation.py` 已有基础，扩展即可）。
5. 禁止字面量清单（CI 可 grep）：`可用规律`、`N 条可用`、`有效规律`、`已验证规律`（除 L1 `detected` 外）。

### 4.4 用户可见文案的具体改法

| 位置 | 现在 | 改为 | 理由 |
|---|---|---|---|
| 首屏卡标题 `index.html:84` | `🧩 本期规律使用情况` | `🧩 证据项状态（L1 / L2 / L3 分列）` | 去掉「规律」 |
| 首屏卡指标 `app.js:464` | `可用规律` | `L1 检出` | |
| 首屏卡空态 `app.js:490` | `当前 0 条规律满足「阶段持续有效 + A/B 验证」。"无可用规律"是诚实的结论` | `L1 未检出任何非随机偏离。这不是失败，是当前样本量下证据能给出的唯一结论。` | 把「无」讲成「结论」而不是「欠账」 |
| 形态卡标题 `index.html:326` | `🧬 形态规律（组合恒等约束）` | `🧾 L2 结构约束（不是规律）` | |
| 形态卡正文 `index.html:331` | `作为严格模式的「可用规律」，LLM 在此约束下生成的注单，形态必然合理` | `它改变「票的样子」，不改变「中奖的机会」：对 S 内任一票枚举全部 1,107,568 个开奖结果，命中分布与超几何逐位相同（小数点后 9 位）。` | 引用 Lead 已复算的结论 |
| 规律研究 tab 标题 `index.html:204` | `🔬 规律库 · 样本外回测结果` | `🔬 L1 候选池（未通过验证，不代表预测力）` | 0 条 A 级，不能叫「库」 |
| AI 闭环按钮 `index.html:436` | `🤖 AI 规律闭环（挖掘→验证→使用）` | `🧠 生成新假设（必定进入前置注册，不会直接出号）` | 现有实现里 llm 恒不 usable，叫「使用」是空头承诺 |
| 阶段卡标题 `index.html:407` | `⏳ 阶段有效性总览` | `⏳ 历史分期读数（不构成预测力）` | ③ 为 0 条已否证 |
| 更多抽屉 `index.html:631` | `🔬 规律研究` | `📚 候选池（L1）` | |
| 页脚 `index.html:644` | `（M1-M5 + U1-U9 + V4~V12 已上线 · L2 结构层/L3 决策层）` | 删掉版本清单，只留 `v4.2.0 · 仅供研究参考 · 不承诺中奖率` | 版本号是给开发者看的，用户看到的是噪音 |

> 这套改法**不删任何功能**，但能让用户在 3 秒内明白「系统现在没有预测力，且它知道自己没有」。这是本轮精简里投入产出比最高的一项。

---

## 5. A 档：建议删除（7 项，证据 + 影响面）

> 入选标准：**无前端入口 + 无测试 + 无非-api 模块调用**，或**已被同路径的新版取代**。全部属于「删了没人会发现，但维护成本真实存在」。

### A1 `/api/draws/latest` — `api_app.py:126-133`

- **证据**：前端 0 调用、测试 0 引用、模块 0 引用（§3.1 第 6 行）。功能与 `/api/draws/history`（`api_app.py:135`，前端在用）完全重叠，只是少一个 `date` 字段。
- **影响面**：破坏 **0** 个测试、0 个端点、0 篇文档。`db.load_last_draws` 保持被 `/api/draws/history` 使用，无需动 db。
- **净删**：8 行。

### A2 `/api/notify/status` — `api_app.py:515-519`

- **证据**：前端 0、测试 0、模块 0。`notify.status()`（`notify.py:82-87`）只回 4 个布尔/字符串，纯 introspection。
- **影响面**：**0**。`notify` 模块本身被调度器 `notify_after_check` 使用（`api_app.py:2172`），**不动**。
- **注意**：不要顺手删 `NOTIFY_*` 配置——生产 `.env` 里 8 项全部已配置（§2.3），通知是真在用的安全功能（开奖后自动对照并推送）。

### A3 `/api/eval/meta` — `api_app.py:544-547`

- **证据**：前端 0、测试 0、模块 0。内部两处已直接调用 `db.load_eval_meta()`（`api_app.py:298`、`api_app.py:412`），此端点是不经过任何加工的第三层包装。
- **影响面**：**0**。`eval_meta` 表与 `save/load_eval_meta` 全部保留（被 `engine.py:2611`、`evaluate.py:237` 使用）。

### A4 `/api/mining/latest` — `api_app.py:679-681`

- **证据**：前端 0、测试 0、模块 0；`data/mining_latest.json` 最后修改 **2026-10-05**，`mining_runs` 表 **0 行**（§3.3）。
- **影响面**：**0**。`mining.get_latest_mining_result()`（`mining.py:548`）随之变为无调用者 → 可一并删。
- **配套**：`data/mining_latest.json`（0.8 KB）+ `data/mining_artifact.json`（129 KB）为死文件，可归档删除。

### A5 `/api/llm_lab/run`（同步版）— `api_app.py:2603-2630`

- **证据**：前端只调 `/api/llm_lab/run/async`（第 70 行，`app.js:437` 的 `btnLabRun` 走 async）；同步版前端 0、测试 0、模块 0。
- **影响面**：**0**。同步版会让浏览器干等 1~8 分钟，async 版存在的理由就在注释里（`api_app.py:2816-2821`：「同步 HTTP 请求期间浏览器只能转圈……用户就会觉得『点了没反应』」）。**保留同步版等于保留一个已知的坏体验。**
- **净删**：约 28 行。

### A6 `/api/rules/closed-loop`（同步版）— `api_app.py:2790-2851`

- **证据**：前端只调 `/api/rules/closed-loop/async`（第 69 行，`app.js:436` 的 `btnClosedLoop` → `runClosedLoop()` → async）；同步版前端 0、测试 0、模块 0。
- **影响面**：**0**。理由同 A5，且此处耗时更长（1~8 分钟）。
- **净删**：约 60 行（同步版包含自己的 try/except 与 task 登记逻辑，与 `_bg_run` 重复）。

### A7 `/api/eval/coverage` — `api_app.py:449-495`

- **证据**：前端 0 调用（**注意**：`/api/portfolio/distribution` 与它无关，是另一条路）；测试 **1 处**（`tests/test_v3_u2_u3_u4.py:331`）。
- **为什么它是 A 档而不是 B**：这是一个 **U4 时代的一次性对照实验**，用 3000 次蒙特卡洛比较 `single` vs `coverage` 两种选注策略，输出「≥五等奖命中期数占比」。而 **V12 已经用 `l2_service` 做了精确全枚举**（`/api/l2/distribution`、`/api/portfolio/marginal`，`tests/test_v12_l2_exact.py`），精确值 > 蒙特卡洛近似。更关键的是它的结论句（`api_app.py:489-493`）在说「coverage 模式……显著提升单期蓝球覆盖数」——**这正是 Lead 认定的「外观工具冒充概率工具」**：形态/覆盖改变的是票的样子，不是中奖概率（`docs/analysis/LEAD_VERDICT.md` §8.1：「形态约束不改变任何概率……命中分布与超几何逐位相同到小数点后 9 位」）。
- **影响面**：破坏 **1** 个测试（`test_v3_u2_u3_u4.py:331`，需一并删除该用例）；破坏 **0** 个前端入口；影响 **0** 篇文档。**不删** `engine` 里的 `coverage` 选注能力（`config.COVERAGE_*` 仍被 `engine.py:1916` 的 `coverage_optimize_compare` 使用，那是票单生成逻辑，不是评估逻辑）。
- **净删**：约 47 行。

### A8 附：零风险的**非端点**删除（同批执行）

| 对象 | 位置 | 证据 | 影响 |
|---|---|---|---|
| 5 个从未被读取的配置常量 | `config.py:219` `DAN_TUO_TUOS`、`:243` `PHASE_USABLE_MIN_PAIRS`、`:409` `BACKTEST_MIN_N`、`:410` `BACKTEST_HORIZON`、`:413` `OFFLINE_EVAL_ISSUES` | 全仓库（含 `.md`）grep 均只有定义行本身 | 0 测试、0 端点 |
| `db.load_features` + `features` 表写而不读 | `db.py:294-303`、`engine.py:2608` | `load_features` 全仓库 0 调用者（唯一一个），表里 8 行历史数据 | 0 测试；`features` 表建议**第三批**再处理（涉及 DB） |
| **5 个**死函数 | `data_fetcher.py:67 load_local`、`patterns.py:881 active_pattern_keys`、`features.py:197 window_features`、`discovery.py:1356 expected_counts`、`db.py:294 load_features` | 全仓库（含 tests）0 引用 | 0 测试 |
| **必须保留的 3 个「零引用」函数** | `discovery.py:1480 llm_discovery_candidates`、`ai_audit.py:559 dumb_judge_baseline`、`rule_lifecycle.py:557 rolling_series` | 同样 0 引用，**但它们是第 3 点的积木**（详见 §11.1） | **不得删除** |
| 2 个死数据文件 | `data/mining_latest.json`、`data/mining_artifact.json` | 2026-10-05 起未更新 | 0（建议归档而非直接 rm） |

---

## 6. B 档：建议降级（10 项，保留但移出主界面 / 默认关闭 / 标注 API-only）

> 入选标准：**前端 0 调用但有测试或安全价值**，或**进了主界面但产出恒为 0**。降级 ≠ 删除：不破坏契约、不返工积木。

| # | 对象 | 位置 / 证据 | 降级动作 | 影响面 |
|---|---|---|---|---|
| B1 | `/api/llm/audit/{latest,run}` | `api_app.py:3017-3144`；前端 0、`llm_audit` 表 0 行；测试 0 | 标注为 **API-only 运维端点**（写入 `docs/API_OPS_ONLY.md`），**不补前端**；或在评估页加 1 张「L3 契约审计」卡（若 M3 需要） | 0 测试破坏 |
| B2 | `/api/audit/anomaly/{latest,run}` | `api_app.py:3251-3330`；前端 0；`tests/test_v12_data_audit.py` 有契约测试 | **保留**（数据异常检测是安全底线，见 §8），在评估页加 1 张卡接 `latest`；`run` 保持 API/调度触发 | 不动测试 |
| B3 | `/api/experiments/randomness-selfcheck` | `api_app.py:3197-3250`；前端 0；`tests/test_v12_selfcheck.py` 3 个用例 | 保留为**「自证实验」运维端点**；若要给用户看，做成首屏折叠的「我不信，让我自己验」入口（1 个按钮，不是 1 个子页） | 不动测试 |
| B4 | `/api/portfolio/expected-value` | `api_app.py:1216-1298`；前端 0；被 `tests/test_v4_honest_ui.py:185` 的**诚实契约测试**断言存在 | **不删**（契约测试是资产），改为**别名**：内部转调 `portfolio_distribution` 的 EV 字段，删掉自己那套重复计算（约 80 行） | 需同步改该测试断言（若改为别名仍需保留路径） |
| B5 | `/api/portfolio/marginal` + `/api/l2/distribution` | `api_app.py:3145-3196`；前端 0；`tests/test_v12_l2_exact.py` 8 个用例 | 保留（V12 L2 是**唯一真实数学收益**，`P(≥1注≥4红)` 0.019517→0.024504），但**合并进「🧾 票单分布」卡的一个可展开区**，不新开子页 | 不动测试 |
| B6 | `/api/llm/cost` + `/api/llm/decision/{status,restore}` | `api_app.py:2044-2136`；前端 0；`tests/test_v10_cost.py` 3 个用例 | 降级到设置页 LLM 卡的「成本与关停状态」折叠区；`restore` 保持 API | 不动测试 |
| B7 | `/api/rules/lifecycle` + `/run` + `/api/rules/runtime` | `api_app.py:2683-2770`；前端 0；`tests/test_v11_rule_lifecycle.py` 46 + `test_v12_dimension_exec.py` 引用 | 降级为**调度器内部件**（每期由 `_scheduler_step` 调 `rule_lifecycle.run_cycle`，`api_app.py:2222`）。用户不需要手动推生命周期 | 不动测试（测试直接打端点，端点保留） |
| B8 | `/api/shape/constraints` | `api_app.py:1683-1694`；前端 0；`tests/test_v12_shape_rules.py:198` | 合并进 `/api/shape/rules` 返回体（多一个 `constraints` 字段），删除独立端点 | 破坏 1 个测试（改断言为读 `/api/shape/rules`） |
| B9 | `/api/llm/arm` | `api_app.py:1642-1665`；前端 0；`tests/test_v10_frontend_cards.py:266-289` 3 处 | 合并进 `/api/battle/report`（同为 `battle` 模块产出），或做进「⚔️ 实战战绩榜」卡的一行徽标 | 破坏 2~3 个测试断言 |
| B10 | 「🔬 规律研究」tab | `web/index.html:202-248`；`patterns` 表 **0 A / 8 B / 39 C**；13 条退化记录 | **移出「更多」抽屉**，合并进「📊 评估报告 → L1 候选池」子页（现有 7 子页不加新子页）。理由：这是第 ⑥ 种「规律」的唯一入口，也是唯一一个**用户会误以为「系统找到规律了」**的页面 | 改前端 + 1 处 `switchTab('patterns')` 调用 |
| B11 | `data/rule_lifecycle.json` 的「未纳管」档 | 335 字节；`evolution_state.json` 显示 `unchanged: 33` | 保留机制，但**前端任何地方都不要展示「生命周期」到用户面前**（它当前的价值是防重复判定，不是给用户看的结论） | 0 |
| B12 | `ADAPTIVE_*`（6 个配置，无端点） | `config.py:252-260`；`adaptive_state.json` = `{}`；根因见 §3.3（缺 `uniform` 基线行） | 二选一：**(a)** 修根因（让 `eval_details` 落 `uniform` 行）后再开；**(b)** 默认关闭 `ADAPTIVE_ENABLED=0` 并在代码注释写明「因缺基线恒不生效」。**不要**保持「默认开启但恒空转」 | 0 测试破坏（`test_v3_u2_u3_u4.py:141` 直接构造 mock，不依赖生产数据） |

---

## 7. C 档：建议合并（重复实现，7 组）

> 这是**收益最大、风险中等**的一档。合并的判据是「同一件用户意图被多个入口讲了好几遍，且数字对不上」。

**本档真正减掉的注册数（与 §10.1 的端点账一致）**

| 组 | 动作 | 减注册 |
|---|---|---|
| C1 | `/api/eval/by-method` → 并入 `/api/eval/online` 的 `by_method` 字段 | −1 |
| C5 | `/api/shape/constraints` → 并入 `/api/shape/rules` | −1 |
| C5 | `/api/data/reconcile/history` → 并入 `/api/data/reconcile` | −1 |
| C5 | `/api/l2/distribution` + `/api/portfolio/marginal` → 并入 `/api/portfolio/distribution?mode=l2\|marginal` | −2 |
| C5 | `/api/llm/arm` → 并入 `/api/battle/report` | −1 |
| C5 | `/api/llm/decision/restore` → 并入 `/api/llm/decision/status`（POST） | −1 |
| C2 / C3 / C4 / C6 / C7 | **语义与实现合并，不减端点**（C4 只做改名，路径数不变） | 0 |
| **合计** | | **−7** |

### C1 「评估」入口 11 → 8（A 档 −2、C1 −1）

`/api/eval` 组 11 条（§2.1）+ `/api/ml/eval` + `/api/replay/diagnose` + `/api/backtest/regime` + `/api/battle/report` + `/api/evolution/run` = **16 个端点在做「我们和随机基线比怎么样」这一件事**。

| 保留（用户意图） | 端点 | 合并掉 |
|---|---|---|
| 离线回测 | `/api/eval/backtest` | — |
| 在线对照 | `/api/eval/online` | — |
| 累计报表 | `/api/eval/cumulative` | — |
| 导出 | `/api/eval/export.csv` | — |
| 方法建议 | `/api/eval/recommendations` | `/api/eval/by-method`（并入返回体） |
| 族分列 | （并入 `/api/eval/online`） | — |
| 总览 | `/api/eval` | `/api/eval/meta`（A3 已删） |
| — | — | `/api/eval/coverage`（A7 已删） |
| LLM 三通道 | `/api/eval/llm/run` + `/api/eval/llm/latest` | 保留（是独立的用户意图） |

**数一下**：11 − 2（A 档）− 1（C1）= **8**。

**影响面**：破坏 `tests/test_v10_channel_wiring.py`（引用 `/api/eval/by-method`）1 处断言；`app.js:1938` 的 `loadMethodBreakdown()` 需改路径或改为读合并后的字段。

### C2 六种「规律」→ 一套契约（§4）

- 动作：新增 `layer` 强制字段 + 统一 `verdict` 词表 + 禁止跨层求和（数值层已有 V12-04 的基础，语义层补齐）。
- **影响面**：修改 `patterns_runtime.py`、`shape_rules.py`、`rule_lifecycle.py`、`backtest.py` 四个模块的输出 schema；`app.js` 6 处文案；`tests/test_v12_layer_isolation.py` 扩展；`tests/test_v11_phase_usable.py` 需跟着词表改。
- **不删任何模块**——这是「改名 + 加字段」，不是重写。

### C3 四条「找规律」管道 → 一个 `RuleProvider` 接口下的 4 个 adapter

当前**四条独立管道**在干同一件事：

| 管道 | 模块 | 入口 | 生产状态 |
|---|---|---|---|
| 特征挖掘（RF/lift） | `mining.py` 388 行 | `/api/mining/run`（前端有按钮） | **死**（`mining_runs` 0 行，artifact 停 10-05） |
| 维度扫描（as-of） | `discovery.py` 1600+ 行 | `/api/discovery/scan`（前端在用） | 活 |
| 自进化（假说 + A/B） | `evolution.py` + `hypothesis.py` | `/api/evolution/run` + 调度器 | 活（`unchanged: 33`） |
| LLM 规律工坊 | `llm_lab.py` | `/api/rules/closed-loop/async`（前端在用） | 活（但产出恒不 usable） |

- 动作：定义 `RuleProvider` 协议（`name` / `propose(draws) -> list[Candidate]` / `cost_estimate`），四个模块各实现一次；`mining.py` 的 adapter **保留但默认关闭**（它是唯一「非 as-of」的管道，`mining.py:431` 用 `BT.backtest_pattern`，口径与 discovery 的严格 as-of 不同）。
- **影响面**：破坏 `tests/test_mining_artifact.py`（若要改签名）；`/api/mining/run` 的 `min_start=300&engine=rf` 参数需保留向后兼容。
- **为什么放 C 而不是 A**：`mining.py` 虽然生产上死了，但它实现的是「特征重要性 + RF 打分」这条**与统计检验正交**的路径，第 3 点「扩大规律定义」可能要用（§11）。

### C4 三个「随机性」端点 → 统一命名空间（**不减端点**，只减「用户看到的入口概念」）

| 端点 | 作用 | 前端 | 处理 |
|---|---|---|---|
| `/api/rigor/randomness` | 真实数据 vs 均匀随机 | ✔ 首屏「🎲 随机性检验」卡 | **保留**（诚实底线） |
| `/api/rigor/signal` | 单策略显著性 + 多重校正 | ✔ 首屏 | **保留** |
| `/api/experiments/randomness-selfcheck` | 合成数据自证实验 | — | 见 B3 |

- 动作：不合并实现（语义不同），但**统一到 `/api/rigor/*` 命名空间**，`/api/experiments/randomness-selfcheck` → `/api/rigor/selfcheck`。
- **影响面**：破坏 `tests/test_v12_selfcheck.py:159,175` 2 个用例的 URL。

### C5 同族端点合并（组合 EV / 形态 / 数据对账 / LLM 对照臂）

| 端点 | 行 | 前端 | 是否重复 |
|---|---|---|---|
| `/api/portfolio/expected-value` | 1216 | — | 与 distribution 的 `ev_ratio_*` 重复 |
| `/api/portfolio/distribution` | 1299 | ✔ | **权威**（V10-03 `engine.ticket_distribution`） |
| `/api/l2/distribution` | 3145 | — | V12 精确版，输入是 `n/preset/seed` 而非当前推荐 |
| `/api/portfolio/marginal` | 3172 | — | V12 边际读数 |

- 动作：`expected-value` 变别名（B4）；`l2/distribution` + `marginal` 合并进 `portfolio/distribution` 的 `l2` 字段，保留独立路径作为**可选参数模式**（`?mode=l2`）。
- **影响面**：`tests/test_v12_l2_exact.py` 8 个用例改 URL；`tests/test_v4_honest_ui.py:185` 保持路径存在即可通过。

### C6 `patterns` 表 vs `patterns_runtime.json` 两套规则源 → 单一 registry

- **证据**：`engine.py:2340`（读 `patterns` 表）与 `engine.py:2426`（读 `patterns_runtime` 快照）在同一函数里并存，互不知情（§4.1）。
- 动作：把 `patterns` 表降级为 `patterns_runtime` 快照的**一个来源**（`build_snapshot` 增加 `backtest_results` 输入），引擎只读快照一个入口。
- **影响面**：改 `patterns_runtime.build_snapshot` 签名；`tests/test_v7_rules_closure.py`（24 个用例）需同步；`/api/patterns` 与 `/api/patterns/backtest` 保留（前者是数据查看，后者是重算触发）。
- **这一项必须与第 3 点同批做**——否则会撞车（§11）。

### C7 方法通道 6 层开关 → 2 层

- **证据**：`lottery/methods.py:343-346` 代码注释自述 6 层（`LOTT_METHODS` / `LOTT_METHOD_MODE` / `LOTT_LLM_ONLY_OUTPUT` / `LOTT_LLM_DISABLED` / `methods_config.json` / `.env`）。
- 动作：保留 **`methods_config.json`（设置页写入）+ 代码默认**，其余 `.env` 开关降级为「仅测试/应急覆盖」并在 `.env.example` 里加 `# DEPRECATED` 注释。
- **影响面**：`resolve_channels()` 是 V10-00a「契约冻结，返回 schema 只增不改」的**唯一判据**（`methods.py:341`），**绝对不能改返回值 schema**；`tests/test_v10_channels.py` 12 个用例 + `tests/test_v10_channel_wiring.py` 依赖它。此动作只改**读取来源的优先级**，不改返回体。

---

## 8. 不能删清单（即使看起来冗余，也是安全底线）

| 对象 | 位置 | 为什么不能删 |
|---|---|---|
| **前置注册簿** | `hypothesis.py:1737 register_prereg` / `:1820 evaluate_prereg`；`data/hypotheses.json`（58 KB） | 它是**唯一能证明「我们不是事后挑数据」的机制**。评审第 3 点「扩大规律定义」的前提就是它。删了之后所有「已验证」都退化为 p-hacking。 |
| **备份 / 迁移** | `scripts/ssq_backup.sh`、`migrations.py:213 backup_before_migrate`；`data/ssq.db.pre-migrate-v2/v3` | `data/` 是**唯一数据副本**（绑定挂载，无远端）。`migrations.py:20-21` 明确写「备份失败 → **中止迁移**（fail-closed）」。上一轮评审（`docs/reviews/R4_RELEASE_REVIEW.md` P0）已发现「生产库此前零备份」——**这个洞刚补上，不能又挖开**。 |
| **自检 `selfcheck`** | `selfcheck.py`（约 600 行）+ `/api/experiments/randomness-selfcheck` | 它是「用户可自证」的唯一手段：用合成随机数据重跑同一套检验，证明检验本身没坏。V12-16 交付了 `response_sha256` 可复现性。**检验工具的可信度，是整份系统诚实声明的立足点。** |
| **数据核对 `data_audit`** | `data_audit.py`（约 1250 行）+ `/api/audit/anomaly/*` | L3-3「LLM 只提名、代码来判」，且每条判决强制带 `p_value` + `m_adj` + `scope`（`data_audit.py:38-45` 的项目铁律）。这是**唯一一个 LLM 提出、又被确定性代码复现过的真实发现**（`docs/analysis/LEAD_VERDICT.md` P3 §4）。 |
| **多源对账 `data_check`** | `data_check.py` + `/api/data/reconcile` | 生产上**当前未生效**（`LOTT_BACKUP_DATA_URL` 没进 compose），但它是数据源被篡改/漂移时的唯一交叉验证。**正确动作是修接线（把 URL 加进 `docker-compose.yml`），不是删代码。** |
| **健康检查 `/api/health`** | `api_app.py:67-90` | 暴露 `schema_version` / `issues` / `max_issue` / `uptime_seconds` / `recent_tasks`。`migrations.py` 是懒加载自动执行的，**没有它只能靠连库 PRAGMA 猜版本**。 |
| **后台任务基建 `_bg_run`** | `api_app.py:2816` + `/api/tasks`、`/api/tasks/{id}` | 所有 1 分钟以上的操作（闭环/进化/LLM 评估/数据审计）都走它。删了会退回「浏览器转圈」。 |
| **诚实面 `/api/rigor/*`** | `rigor.py` + `api_app.py:1025,1105` | 首屏「🎲 随机性检验」卡的数据源。这是产品定位（第 6 点「不强调随机性」）的**技术锚点**：可以不说，但不能不知道。 |
| **通知 `notify`** | `notify.py`；`api_app.py:2172` 调度调用 | 生产 `.env` 里 webhook + SMTP 8 项全部配置。开奖后自动对照 + 推送是**真实在跑的功能**（`_scheduler_step` 里 `notify.notify_after_check`）。只删 `/api/notify/status`（A2），不删模块。 |
| **`engine.predict_next`** | `engine.py`（约 1500 行核心） | 它已经是**唯一出号口径**。任何「精简」不得绕过它另起一条出号路径。 |
| **`db.py` 的迁移与 schema 版本** | `db.py:126` 起、`migrations.py` | 14 张表里 5 张 0 行，但**表本身是 forward-only DDL**，删表要写补偿迁移 + 备份 + 回滚演练（见 §9 第三批）。 |

---

## 9. 删除动作顺序（三批，按「零风险 → 需改前端 → 涉及 DB」）

### 第一批：零风险（后端死代码，不动前端、不动 DB、不动契约）

**预计 1 人日。全部动作可用「先删代码 → 跑全量测试 → 期望 1057-collected 变成 1057 - 1」验证。**

1. 删 A1~A6 六组端点（6 条注册：`draws/latest`、`notify/status`、`eval/meta`、`mining/latest`、`llm_lab/run` 同步、`rules/closed-loop` 同步）。**净删约 170 行。**
2. 删 A7 `/api/eval/coverage` + 对应 1 个测试用例。**净删约 47 行 + 1 test。**
3. 删 A8 的 5 个死配置常量 + 5 个死函数 + `mining.get_latest_mining_result`。**净删约 40 行。**（`llm_discovery_candidates` / `dumb_judge_baseline` / `rolling_series` 虽然也是 0 引用，**不动**。）
4. 归档（移到 `data/_archive/`）`mining_latest.json`、`mining_artifact.json`。
5. **不动** `rolling_series` / `llm_discovery_candidates` / `dumb_judge_baseline`（§11）。

**验收**：`pytest -q` 全绿；`grep -cE '^@app\.' lottery/api_app.py` = **80 − 7 = 73**；前端未调用端点 25 → **18**；前端 `node --check web/static/app.js` 通过；页面人工点一遍所有按钮无 404（因为没有任何前端调用被删）。

### 第二批：需要改前端（视图合并 + 文案改造 + 端点合流）

**预计 3~4 人日。必须分两个可回滚的提交序列。**

1. **文案层（§4.4）先做**——它不改任何接口，只改 `web/index.html` + `web/static/app.js` 字符串，可独立发布、可独立回滚。**这一步的收益比第 2~3 步加起来都大。**
2. 「🔬 规律研究」tab 合并进「📊 评估报告」（B10）：`switchTab('patterns')` 调用点 1 处（`index.html:631`），`tab-patterns` section 整体挪进 eval 子页。
3. B8/B9/C1/C4/C5 的端点合流：先加新路径 + 保留旧路径（双写 1 个版本），前端切新路径，下个版本删旧路径。**这是唯一保证不回滚事故的做法。**
4. B4/B5/B6/B7 的降级：把 4 组端点从「独立页/卡」收进折叠区。
5. 加 CI 断言（防回归）：
   - `grep -rn '可用规律\|条可用' web/` 必须为 0（白名单除外）
   - 每个保留端点在 `app.js` 里必须有 `fetch`，或在 `docs/API_OPS_ONLY.md` 白名单里
   - `grep -c 'id="tab-' web/index.html` = 5

**验收**：视图 14 → 9；端点 73 → **≤ 66**；前端未调用端点 18 → **≤ 12**；文案禁词 grep = 0；`pytest -q` 全绿（允许因合流调整的断言数 ≤ 10）。

### 第三批：涉及 DB / 迁移（必须走备份 + 回滚演练）

**预计 1~2 人日。只在第一批第二批稳定运行 ≥ 2 个开奖周期后做。**

1. `features` 表（8 行，写而不读）：先删 `engine.py:2608 save_features` 调用 + `db.py:285 save_features` / `:294 load_features`，**观察 2 期**，确认无报错后再写迁移 `DROP TABLE features`。
2. `reconcile_runs` **不删**——先修接线（把 `LOTT_BACKUP_DATA_URL` 加进 `docker-compose.yml`），让表开始有数据，再决定。
3. `replay_results` **不删**——V12 已冻结 DDL（`migrations.py:63`「§2.1.5 冻结 DDL」），删表等于撤销一个已发布契约。
4. 若最终要删任何表：必须 ① `scripts/ssq_backup.sh backup` 留档；② 写 `migrations.py` 的 forward-only 补偿（本项目**只允许 forward-only**，`migrations.py:67`）；③ 在 `docs/DEPLOY.md` 记回滚路径；④ 至少 1 次 `restore` 演练。

**验收**：`/api/health` 的 `schema_version` 递增且 `schema_latest` 相等；`data/ssq.db.pre-migrate-v<N>` 存在；`pytest -q` 全绿。

---

## 10. 目标形态与可度量验收

### 10.1 目标值

| 指标 | 现状 | 第一批后 | 第二批后（**达标线**） | 第三批后（**理想线**） | 机检命令 |
|---|---|---|---|---|---|---|
| 路由注册 | **80** | **73** | **≤ 66** | ≤ 62 | `grep -cE '^@app\.(get\|post\|put\|delete)' lottery/api_app.py` |
| 唯一路径 | 79 | 72 | ≤ 65 | ≤ 61 | 同上 + 去重 |
| 前端从未调用的端点 | **25** | **18** | **≤ 12** | ≤ 8 | 附录 A 第 3 条脚本 |
| 视图（tab + 子页） | **14**（7+7） | 14 | **9**（5+4） | 9 | `grep -c 'id="tab-' web/index.html`；`grep -c 'data-sub=' ` |
| HTML 按钮 | 54 | 54 | ≤ 44 | ≤ 38 | `grep -c '<button' web/index.html` |
| 配置常量 | **96** | **91** | ≤ 76 | **≤ 68** | `grep -cE '^[A-Z][A-Z0-9_]+\s*=' lottery/config.py` |
| `LOTT_*` 变量 | 80（+3 在 config 外） | 78 | ≤ 66 | **≤ 54** | `grep -oE 'LOTT_[A-Z0-9_]+' lottery/config.py \| sort -u \| wc -l` |
| 「规律」含义数 | **6** | 6 | **3**（L1/L2/L3，各一套判据） | 3 | 人工评审 + `layer` 字段强制 |
| 「可用规律 / 条可用」字样 | **9 行**（用户可见 5） | 9 | **0** | 0 | `grep -rn '可用规律\|条可用' web/` |
| 测试数 | 1057 | ≥ 1040 | **≥ 1000**（允许 -5%） | ≥ 1000 | `pytest --collect-only -q \| tail -1` |
| DB 表 | 14 | 14 | 14 | **13**（`features` 待定） | `PRAGMA` 枚举 |

**端点账怎么算的（逐条可核对，不留「大概」）**

| 阶段 | 动作 | 删除的注册数 | 路由注册累计 | 前端未调用累计 |
|---|---|---|---|---|
| 起点 | — | — | 80 | 25 |
| 第一批 | A1 `/api/draws/latest`、A2 `/api/notify/status`、A3 `/api/eval/meta`、A4 `/api/mining/latest`、A5 `/api/llm_lab/run`(同步)、A6 `/api/rules/closed-loop`(同步)、A7 `/api/eval/coverage` | **−7** | **73** | 18 |
| 第二批 | C5a `/api/shape/constraints`→并入 `shape/rules`；C5b `/api/data/reconcile/history`→并入 `reconcile`；C5c `/api/l2/distribution` + `/api/portfolio/marginal`→并入 `portfolio/distribution?mode=`；C5d `/api/llm/arm`→并入 `battle/report`；C5e `/api/llm/decision/restore`→并入 `decision/status`；C1 `/api/eval/by-method`→并入 `eval/online` | **−7** | **66** | **12** |
| 第三批（理想） | `/api/eval`(总览)→并入 `cumulative`；`/api/llm/cost`→并入 `llm/decision/status`；`/api/rules/runtime`→并入 `rules/lifecycle`；`/api/audit/anomaly/run`→并入 `audit/anomaly/latest?run=1` | **−4** | **62** | 8 |

> ⚠️ **口径修正（相对于本文件初稿）**：初稿写「80 − 7 − 21 = 52」，那是把「合并」与「别名化」混算了——**别名化不减少路由注册数**（路径还在，只是内部转调）。按「真正删除注册」重算，第二批只能做到 **66**，第三批才到 **62**。**「≤52」不是做不到，而是需要把 B 档 10 组也全部别名化，那会撞上 `test_v4_honest_ui.py` 等契约测试，风险高于收益。因此本文把达标线定在 **≤66**，理想线定在 **≤62**。**
>
> 端点数量本身不是目的：**用户感知的复杂度主要由「视图数 × 每屏的「规律」含义数」决定**（14 → 9 视图、6 → 3 含义），而不是端点 80 还是 66。

### 10.2 可度量验收（不是「感觉精简了」）

必须全部通过，缺一不可：

1. **端点可达性断言**（新增 `tests/test_m4_endpoint_reachability.py`）：
   - 遍历 `app.routes`，对每个 `/api/*` 路径，断言「在 `app.js`+`index.html` 里出现」**或**「在 `docs/API_OPS_ONLY.md` 的 allowlist 里」。任一不满足 → fail。
2. **文案禁词断言**（新增 `tests/test_m4_no_ambiguous_usable.py`）：
   - 读 `web/index.html` + `web/static/app.js`，断言不含 `可用规律` / `条可用` / `有效规律`（allowlist 空）。
3. **三层不相加断言**（扩展现有 `tests/test_v12_layer_isolation.py`）：
   - 构造同时含 L1/L2/L3 证据项的输入，断言响应里**不存在**任何 `count == l1 + l2 + l3` 的字段；断言每个证据项都有 `layer` 且 ∈ {L1,L2,L3}。
4. **视觉验收**：首屏截图人工确认——**「本期推荐」页在 3 秒内不会被误读成「系统找到规律了」**。这条是主观的，但它是本轮的唯一真实目标，必须由人来看。
5. **回归验收**：`pytest -q` 全绿；`node --check web/static/app.js`；公网 `https://ssq.ff88.de` 刷新后 `/api/health` 的 `version` 与 `web/index.html` 页脚一致。

---

## 11. 与第 1 点（模块化）/ 第 3 点（挖·验·用分离）的协同约束

> **这是本文最重要的一节。** 精简最容易犯的错，是把「暂时没人调用的积木」当垃圾删掉——而它恰是下一阶段要用的。

### 11.1 明确「看似冗余，其实是积木」（**禁止删除**）

| 组件 | 位置 | 为什么现在看起来没用 | 为什么不能删 |
|---|---|---|---|
| `discovery.llm_discovery_candidates()` | `discovery.py:1480` | 全仓库 **0 引用**（§5 A8 的死函数扫描会命中它） | 它的 docstring 就是第 3 点的设计要求：「LLM 的任务不是从零猜规律，而是在这些**已经算过的**候选上做三件事：换维度组合、解释机理、判断阶段有效性」。**这是「挖/验/用分离」里「用」的 LLM 入口**。删了 M3 要从零写。 |
| `ai_audit.dumb_judge_baseline()` | `ai_audit.py:559` | 0 引用 | 它是 V12-13 专门为「LLM 判官打分」设计的**反对 0.5 基线**（实测全答一类的哑判官在 `rule_lifecycle` 上准确率 **99.19%**）。M3 给 LLM 打分的分母就是它。 |
| `rule_lifecycle.rolling_series()` | `rule_lifecycle.py:557` | 0 引用 | docstring 明写「给 LLM 与前端展示『最近在往哪走』」——正是 M3「规律解释与命名」岗位的输入。 |
| `hypothesis.register_prereg` / `evaluate_prereg` | `hypothesis.py:1737` / `:1820` | 只在调度器里被调用 1 次，前端无入口 | 前置注册簿是第 3 点「闲时自动挖掘 + 多重检验治理」的**唯一防线**。 |
| `rigor.py` 整模块 | `rigor.py`（约 2000 行） | 首屏只有 2 张卡在用它 | 它是「显著性判据」的唯一实现；M2 要用的 e-value / 在线 FDR 必须与它共用常量。 |
| `methods.resolve_channels()` | `methods.py:534` | 6 层开关看着冗余（C7 要收敛） | 它是 V10-00a **契约冻结**的唯一判据，返回 schema「只增不改」。C7 只能改读取优先级，**不能改返回体**。 |
| `engine.ticket_distribution()` | `engine.py:1728` 起 | 只有 1 张卡在用 | 它是 L2 精确值的唯一实现（Lead 已复算认可），M2「评价口径」要用它的 `p_at_least_r`。 |
| `mining.py` 整模块 | `mining.py` 388 行 | 生产 0 行、artifact 停 14 天 | 它是唯一「特征重要性 + RF 打分」路径（与统计检验正交）。C3 里保留为 adapter 并默认关闭，**不删**。 |
| `l2_service.py` | `l2_service.py` 11 KB | 前端 0 调用 | 承载 Lead 认可的**唯一真实数学收益**（`P(≥1注≥4红)` 0.019517 → 0.024504）。 |

### 11.2 精简与模块化的顺序约束

| 约束 | 原因 |
|---|---|
| **先做 §4（语义层改名），再做第 1 点（模块化）** | 模块化的第一步是切接口契约。如果契约里的字段还叫 `usable`、还允许跨层相加，那切出来的「模块边界」就是把债务分装到 7 个地方。**改名必须在切片之前。** |
| **C6（合并两套规则源）必须与第 3 点同批** | `engine.py:2340` 与 `:2426` 两条规则加载路径若在模块化时被切到不同服务里，之后再合并要跨进程改契约。 |
| **第一批删减可以在模块化之前做** | A 档 7 个端点全是叶子节点，删掉会让 `api_app.py` 少 170 行，**降低模块化的切分成本**。 |
| **第三批（DB）必须在模块化之后** | 模块化会改表访问路径（DB 单连接/锁是第 1 点的重点，见 task-1）。在它之前删表，会把「迁移」和「重构」两种风险叠在同一批。 |
| **精简不得减少崩溃隔离的观察点** | 第 1 点要的是「一个模块崩不拖死全站」。删除 `/api/health`、`/api/tasks`、`_bg_run` 会让崩溃变得**不可观测**——这是反向操作。 |

### 11.3 精简不能砍掉第 3 点要用的「数据面」

第 3 点要做「闲时自动挖掘」，需要三样东西，都在现有代码里，**都不能因为「没人用」而删**：

1. **算力护栏**：`v12_cpu_governance`（`tests/test_v12_cpu_governance.py`）+ `_evolution_step` 的**幂等指纹**（`data/evolution_state.json`，`api_app.py:2258` 起的注释记录了「同一份数据被反复跑完整进化，每轮 19~22 分钟 → 容器 CPU 100%」的真实事故）。删了幂等，闲时挖掘会立刻把容器打满。
2. **as-of 回放**：`discovery.discover()` 的严格 as-of 语义（`index.html:427`「第 t 期只用 t 之前的数据算筛选条件」）+ `replay.py`。这是「验证」的唯一可信实现。
3. **前置注册判定**：`hypothesis.evaluate_prereg()`。这是「多重检验治理」的地基。

---

## 12. 顺手发现的二次缺陷（不属于「精简」范围，但建议同批修）

| # | 缺陷 | 证据 | 为什么该一起修 |
|---|---|---|---|
| 1 | **`.env` 里 8 项 `NOTIFY_*` 全部配了，`docker-compose.yml` 只注入 8 项中的 8 项（有），但 `LOTT_BACKUP_DATA_URL` 没注入** | §3.3 | 多源对账在生产上恒定 `not_configured`。**修 1 行 compose 就能让一个安全功能复活**，比删它划算。 |
| 2 | **`/api/data/reconcile` 在公网无鉴权可达** | `api_app.py:34-39`：只有 `config.API_TOKEN` 非空才校验 Bearer；生产 `.env` **未设 `LOTT_TOKEN`** → `api_auth_enabled=false`，80 个端点全部匿名可达 | 「精简端点」在此处有**安全收益**：每删一个端点就少一个匿名攻击面。建议本轮同时补 `LOTT_TOKEN`（但会影响前端 fetch，需加 header，属第二批）。 |
| 3 | **`ADAPTIVE_*` 默认开启但恒空转** | `adaptive_state.json = {}`；根因 `methods.py:245-246` 依赖 `uniform` 基线行，而 `eval_details` 无 `uniform` 行 | 「默认开启的功能实际上不生效」比「关闭」更危险：用户以为有降权保护，其实没有。B12 必须明确二选一。 |
| 4 | **`patterns` 表 13 条退化记录**（`avg_fav_size=0`、`avg_hits=0`、`p=1.0`） | §2.5 | 「规律库」里 28% 的行是坏数据，且它们的 `grade=C` 让「47 条规律」这个数字虚高。B10 合并时顺手清理。 |
| 5 | **`web/index.html:644` 页脚仍写 `M1-M5 + U1-U9 + V4~V12 已上线`** | 版本清单 | 用户视角的纯噪音；且每次交付都要改它（`R4_RELEASE_REVIEW.md` 记录过「升版涉及 7 处文件」）。建议直接删掉清单，只留版本号。 |
| 6 | **`DAN_TUO_DANS` 用了，`DAN_TUO_TUOS` 没用** | `engine.py:2497-2498` vs `config.py:219` | 一对参数只实现了一半——「胆拖」功能本身是半成品。要么补齐，要么连 `DAN_TUO_DANS` 一起删（需产品确认胆拖还要不要）。 |

---

## 附录 A：可复现命令

```bash
cd /root/DSH/应用/ssqyuce

# 1) 端点总数与分组
grep -cE '^@app\.(get|post|put|delete)' lottery/api_app.py        # → 79
grep -nE '^@app\.' lottery/api_app.py | wc -l                      # → 80（含 1 个 api_route）
grep -oE '@app\.(get|post|put|delete|api_route)\("[^"]*"' lottery/api_app.py \
  | sed -E 's/.*\("//; s/"//' | cut -d/ -f3 | sort | uniq -c | sort -rn

# 2) 配置项
grep -cE '^[A-Z][A-Z0-9_]+\s*=' lottery/config.py                 # → 96
grep -oE 'LOTT_[A-Z0-9_]+' lottery/config.py | sort -u | wc -l     # → 80

# 3) 前端使用率（本文 §3 的脚本，带边界匹配）
python3 - <<'EOF'
import re,glob,os
api=open('lottery/api_app.py').read()
eps=[(m.group(2),api[:m.start()].count('\n')+1)
     for m in re.finditer(r'@app\.(get|post|put|delete|api_route)\(\s*[\'"]([^\'"]+)',api)]
front=open('web/static/app.js').read()+open('web/index.html').read()
tests={f:open(f,errors='ignore').read() for f in glob.glob('tests/*.py')}
mods={f:open(f,errors='ignore').read() for f in glob.glob('lottery/*.py') if not f.endswith('api_app.py')}
def p(e): return re.compile(re.escape(e)+r'(?![a-zA-Z0-9_/{.-])')
for e,l in eps:
    fe = '/api/tasks/' in front if e.endswith('{task_id}') else bool(p(e).search(front))
    ts = [os.path.basename(f) for f,t in tests.items() if p(e).search(t)]
    ms = [os.path.basename(f) for f,t in mods.items() if p(e).search(t)]
    if not fe: print(f"L{l:<5} {e:<42} test={len(ts)} mod={len(ms)}")
EOF

# 4) 视图与按钮
grep -c 'id="tab-' web/index.html          # → 7
grep -c 'data-sub=' web/index.html         # → 7
grep -c '<button' web/index.html           # → 54
grep -cE '^\s*(async )?function ' web/static/app.js   # → 180

# 5) 测试基线
.venv/bin/python -m pytest tests/ -q --collect-only | tail -1      # → 1057 tests collected

# 6) 生产使用痕迹（只读）
.venv/bin/python -c "
import sqlite3
c=sqlite3.connect('data/ssq.db')
for t in ['predictions','eval_results','eval_details','llm_eval_results','mining_runs',
          'replay_results','llm_audit','reconcile_runs','patterns']:
    print(t, c.execute(f'select count(*) from {t}').fetchone()[0])
print(c.execute('select grade,count(*) from patterns group by grade').fetchall())
"
cat data/adaptive_state.json      # → {}
ls -l --time-style=long-iso data/mining_artifact.json data/mining_latest.json

# 7) 死函数（含测试，排除装饰器注册的函数）
python3 - <<'EOF'
import re,glob,os
files={f:open(f,errors='ignore').read() for f in glob.glob('lottery/*.py')+glob.glob('tests/*.py')}
for f in glob.glob('lottery/*.py'):
    if f.endswith(('api_app.py','cli.py')): continue
    src=files[f]
    for m in re.finditer(r'^(?:    )?def ([A-Za-z][A-Za-z0-9_]*)',src,re.M):
        n=m.group(1)
        if n.startswith('__'): continue
        pat=re.compile(r'(?<![A-Za-z0-9_])'+re.escape(n)+r'(?![A-Za-z0-9_])')
        if sum(len(pat.findall(t)) for t in files.values())-1 <= 0:
            print(f"{f}:{src[:m.start()].count(chr(10))+1}  {n}")
EOF

# 8) 生产 DB 备份（第三方批前置，必做）
scripts/ssq_backup.sh backup && scripts/ssq_backup.sh verify
```

## 附录 B：96 个配置常量清单（行号 / 默认值）

| # | 行 | 常量 | 默认 | 状态 |
|---|---|---|---|---|
| 1 | 12 | `BASE` | `LOTT_HOME` 或仓库根 | 保留 |
| 2-4 | 13-15 | `DATA_DIR` / `RAW_DIR` / `DB_PATH` | `data/` / `data/raw` / `data/ssq.db` | 保留 |
| 5 | 17 | `DATA_URL` | `e.17500.cn`（硬编码） | 保留（可提为配置） |
| 6 | 18 | `STARTED_AT` | `time.time()` | 保留 |
| 7 | 20 | `API_TOKEN` | 空（→ 无鉴权） | 保留（§12-2） |
| 8 | 22 | `BACKUP_DATA_URL` | 空 | **修接线**（§12-1） |
| 9-11 | 26-28 | `APP_VERSION` / `APP_BUILD` / `APP_MILESTONES` | `4.1.0` / `2026-10-V12` / dict | 保留 |
| 12 | 54 | `LLM_DISABLED` | `0` | 保留（C7 收敛） |
| 13-15 | 57-59 | `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | 空 / 空 / `minimax-m3` | 保留 |
| 16-18 | 76-79 | `LLM_SAMPLES` / `TICKETS_PER_LLM_CALL` / `N_TICKETS` | `3` / `5` / `5` | 保留 |
| 19 | 85 | `STRICT_MODE` | `0` | 保留 |
| 20 | 92 | `LLM_ONLY_OUTPUT` | `0` | 保留（C7 收敛） |
| 21-25 | 93-108 | `LLM_TIMEOUT` / `LLM_LONG_TIMEOUT` / `LLM_MAX_TOKENS` / `LLM_BUDGET_FILE` / `LLM_TOTAL_TIMEOUT` | `60` / `180` / `32000` / 文件 / `75` | 保留 |
| 26-28 | 149-151 | `METHOD_RECOMMENDATION_*` | `60` / `120` / `0.05` | 保留 |
| 29 | 159 | `RULE_CONF_BOOST` | `1.0` | 保留 |
| 30-35 | 166-183 | `METHODS_RAW` / `METHOD_MODE` / `METHODS_SPEC` / `METHODS_CONFIG_FILE` / `METHODS_SOURCE` / `CHANNELS` | 派生 | **C7 收敛** |
| 36-41 | 195-209 | `BLUE_MODE` / `BLUE_COVER` / `BLUE_FALLBACK_*` ×3 / `BLUE_RANDOM_RATE` | `model` / `1` / `30` / `0.045` / `0` / `1/16` | 保留 |
| 42-45 | 212-219 | `BET_MODE` / `BLUE_COMPOUND_K` / `DAN_TUO_DANS` / `DAN_TUO_TUOS` | `single` / `5` / `4` / `5` | **`DAN_TUO_TUOS` 删**（§12-6） |
| 46-48 | 239-243 | `LOTT_PHASE_USABLE` / `PHASE_USABLE_AUTO_OFF_T` / `PHASE_USABLE_MIN_PAIRS` | `0` / `-2.0` / `60` | **`MIN_PAIRS` 删**（A8） |
| 49 | 249 | `BOOT_REFRESH_RULES` | `1` | 保留 |
| 50-55 | 252-260 | `ADAPTIVE_*` ×6 | `1` / `20` / `60` / `0.5` / `red` / 文件 | **B12 二选一** |
| 56-60 | 264-286 | `COVERAGE_R` / `COVERAGE_TRIALS` / `COVERAGE_BLUE_K` / `COVERAGE_OBJECTIVE_ALIASES` / `COVERAGE_OBJECTIVE` | `3` / `3000` / `4` / 8 别名 / `ev_conserving` | 保留 |
| 61-67 | 370-376 | `LLM_EVAL_*` ×5 / `LLM_VERIFY_MODEL` / `LLM_VERIFY_ENABLED` | `60` / `5` / `42` / 空 / `1.0` / 空 / `1` | 保留 |
| 68 | 406 | `WINDOWS` | `{long:0, mid:150, short:30}` | 保留 |
| 69-71 | 409-413 | `BACKTEST_MIN_N` / `BACKTEST_HORIZON` / `OFFLINE_EVAL_ISSUES` | `30` / `1` / `120` | **全删**（A8） |
| 72-78 | 417-423 | `ML_*` ×7 | `1` / `300` / `10` / `60` / `60` / `5` / `2` | 保留 |
| 79 | 438 | `MODEL_SCHEMA_VERSION` | `2` | 保留 |
| 80-84 | 455-460 | `LIFECYCLE_*` ×5 | 文件 / `2` / `2` / `3` / `60` | 保留 |
| 85-87 | 463-465 | `SCHEDULER_ENABLED` / `DRAW_WEEKDAYS` / `DRAW_TIME` | `0`（compose 注入 1）/ `(1,3,6)` / `21:35` | 保留 |
| 88-95 | 468-475 | `NOTIFY_*` ×8 | 全空（生产已配） | 保留（A2 只删 status 端点） |
| 96 | 480 | `LLM_CONFIG_FILE` | `data/llm_config.json` | 保留 |

---

## 附录 C：本文与其它评审文档的边界

| 文档 | 负责 | 本文不涉及 |
|---|---|---|
| `M1_ARCHITECTURE.md`（task-1） | 模块化、崩溃隔离、进程/线程选型 | 本文只在 §11.2 给「顺序约束」 |
| `M2_STATISTICS.md`（task-2） | 规律定义扩容、多重检验、e-value/FDR、闲时挖掘调度 | 本文只在 §11 标出「不能删的积木」 |
| `M3_AI.md`（task-3） | LLM 岗位矩阵、可测性、降级路径 | 本文只在 §11.1 标出 `llm_discovery_candidates` / `dumb_judge_baseline` / `rolling_series` |
| `M5_DELIVERY.md`（task-5） | 工期、风险、验收、MVT | 本文只给「三批删除顺序」与「目标值」 |
| `LEAD_VERDICT.md` §五 | 6 种「规律」的产品侧成因（5 种） | 本文补第 6 种 + 给出文案改法与验收断言 |
| `docs/reviews/R4_RELEASE_REVIEW.md` | V10 的测试/发布工程 | 本文不重复其测试基建结论 |

---

*本文件为只读评审产出，未修改任何业务代码、前端、配置或数据库。所有数字均可用附录 A 的命令复现。*
