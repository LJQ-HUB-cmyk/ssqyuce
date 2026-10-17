# R1 架构与集成评审：V10 改进方案

> 评审对象：`docs/V10_IMPROVEMENT_PLAN.md`（v1.0 / 2026-10-17）
> 评审视角：**仅集成与架构**（接口契约可行性、迁移风险、并行拆解、默认值兼容、内部一致性）。
> 不评统计/LLM 方法本身是否正确（如选择性推断、CUSUM 的数学是否正确，交由 R2/R3）。
> 评审依据：对仓库的实地核查（grep/read + 实跑 pytest），所有结论附 `文件:行号`。
> 基线状态：`pytest -q` 实测 **480 passed / 0 failed（114.7s）**；`schema_version` 实测 `[(1,…),(2,…)]`；
> 容器内 `scipy 1.18.1`，`scipy.stats.truncnorm` 可用（P1-1 的依赖成立）。
>
> ⚠️ 计划书 §3.2-5 写「当前回归基线为 381 passed（2026-10-15 记录）」，**该数字已过期**（实为 480）。
> 这不是文档笔误级别的瑕疵：它会被用来判断「新增测试后是否回归」，基线错了这条护栏就失效。

---

## 1. 一句话结论

**需修改后认可。**

方案的问题诊断方向（§1.3–§1.4）经代码核对**全部属实**（尤其 P0-4 描述的「LLM 自由猜号被设成默认」在库里留下了可复核的痕迹），
P0-2 / P0-3 / P0-5 有明确的代码落点与原语可复用；
但方案在**接口层**犯了本项目历史上反复出现的同一个错误——**新增的一等公民会与已有的同义机器共存，而不是并入它**：
§3.1 的「互不触碰同一文件、可并行」与真实文件分布不符（有 3 名开发者要改 `engine.py` + `api_app.py` + `config.py` 中的同一处），
`LOTT_PREDICT_CHANNELS` 会成为第 **7** 套通道/模式开关，P0-2 的 `experiments` 表会成为第 **4** 本登记簿，
而全部新表只挂 `CREATE ... IF NOT EXISTS`、`schema_version` **不递增**，使 `tests/test_m4_5_engineering.py:11` 的版本契约失效且无可用的回滚点。
以上五点必须先修（见 §4），修完即可开工。

---

## 2. 评分表

| 维度 | 分数 | 理由（附证据） |
|---|---|---|
| **架构契合度** | **2 / 5** | ①`experiments` 表（方案 §P0-2:105-116）与已有的 `hypothesis.register_hypotheses`（`lottery/hypothesis.py:1257`，去重靠累加 `attempts`）语义重叠，项目将出现假说库 / `patterns_runtime` 快照 / `llm_claims.json` / 新 `experiments` 四本登记簿；②`LOTT_PREDICT_CHANNELS`（方案 §P0-4-3:174）叠加在 `LOTT_METHODS`（`lottery/config.py:155`）+ `LOTT_METHOD_MODE`（:156）+ `LOTT_LLM_ONLY_OUTPUT`（:81）+ `LOTT_LLM_DISABLED`（:53）+ `LOTT_ADAPTIVE_ENABLED`（:192）+ `runtime methods_config.json`（:160）之上，形成第 7 套开关；③P0-1 要往 `llm_lab` 加「对票单打分」，但 `llm_lab` 当前只有「提规律」一种输出（`lottery/llm_lab.py:56` SYSTEM_PROMPT、`:585` run_lab），且它的入口是**异步任务**（`lottery/api_app.py:1687`），而票单方案必须在 `predict_next` 内同步产生——没有定义这个时序。 |
| **迁移安全性** | **3 / 5** | 机制本身能承载（`lottery/migrations.py:35-57` 是有序 + `version <= current` 跳过 + 自动建 `schema_version`），但方案**通篇没提**：①新增 migration #3 与版本号递增；②`data/ssq.db` 是唯一副本（`DEPLOY.md:140`、`DEPLOY.md:203-215`），备份只是注释掉的 cron、容器内**没有** sqlite3 CLI，方案 §3.2-7 的 `cp` 其实是拷宿主机文件；③写事务路径 `db.py` 的幂等语义被 §P0-5 破坏（`replay_results` 无主键 → 重复插入）。最轻的一条是 `tests/test_m4_5_engineering.py:11` 断言 schema 必须 ==2，一旦走正确流程这条测试必然变红。 |
| **接口清晰度** | **2 / 5** | 方案大量使用**未定义**的符号：`LOTT_LLM_DECISION`（§P0-1-5:92）、`config.COVERAGE_OBJECTIVE ∈ {ev_conserving, pmax_high, p_any_blue, custom_weight}`（§P0-3-2:155，与现存两值域 `coverage/expected_value` 的兼容关系未写）、`replay_results` 的读写 API 归属未写、`llm_decision_skill` 落在哪张表未写（§P0-1-3:90 只给字段名）。另有**签名级**冲突：`ticket_distribution` 被写成 `rigor.py` 的函数（§P0-3-1:149），但 `rigor.py` 的既定定位是「与票单无关的理论分布」（`lottery/rigor.py:187-246` 全是号码级理论量），而票单层语义已经由 `engine.py:1199 expected_prize` / `:1267 portfolio_expected_value` / `:1260 _portfolio_blue_hit_prob` 建立，且有 `tests/test_v4_coverage.py:172-231` 锁死行为。 |
| **并行拆解可行性** | **2 / 5** | §3.1 明写「V10-01/02/05 互不触碰同一文件」，但：**`api_app.py`** 被 02（登记簿三个路由）、10（前端卡）、11（成本报表 + 自动关停）三方改；**`engine.py`** 被 03（`_ticket_objective`/`coverage_optimize`）与 04（`predict_next` 通道选择）两方改，且两处只隔 335 行、共用同一 `result` 字典（`engine.py:1716-1737` vs `:1788-1832`）；**`config.py`** 被 04 与 12（版本号）两方改。真实并行度：01/02/03/05 四个 ≠ 三人可并行（实际最多 3 人且都要改 `rigor.py` 或 `db.py`）。 |
| **默认值兼容性** | **3 / 5** | `LOTT_LLM_ONLY_OUTPUT` 默认 1→0 的**影响面比方案描述的小**（默认路径本来就出不了 LLM 票，见 §3 P1-6 的实证），但方案没做这层分析，也没处理 `data/methods_config.json` 的运行时覆盖优先级（`config.py:412`）。`LOTT_PREDICT_CHANNELS` 若只是「又一个 allow-list」，则与 `LOTT_METHODS` 的 allow 语义**完全重合**（`lottery/methods.py:6-13`）。历史预测记录不受影响（`predictions`/`eval_meta` 带快照，`db.py:433-483`），这一点方案是对的。 |

加权印象：**2.6 / 5** —— 方向认可，接口层不认可。

---

## 3. 问题清单

### P0（不开工前必须解决）

#### P0-1　`LOTT_PREDICT_CHANNELS` 是第 7 套并行开关，「LLM 不出号」这件事已经能用一行配置做到

**证据**
- 既有通道控制：`LOTT_METHODS`（`lottery/config.py:155`，语义见 `lottery/methods.py:6-13`：空=全开 / `-llm`=拒绝列表 / `stat,ml`=允许列表 / 支持族名与全名）、`LOTT_METHOD_MODE`（`config.py:156`，research 时**忽略**开关 `methods.py:124-125`）、运行时覆盖 `data/methods_config.json`（`config.py:160,412`，代码注释明写「优先于 .env，重启不丢失」）。
- 引擎实际取用链：`engine.py:1572 methods_spec = METH.effective_spec(...)` → `:1576 use_llm = use_llm and METH.is_enabled("llm", methods_spec)` → `:1678 METH.filter_candidates(candidates, methods_spec)`。也就是说 **`LOTT_METHODS=-llm` 已经是「LLM 不参与出号」的完整体现**，且它已经能被设置页写、被 `eval_meta.config_snapshot_json` 快照留痕（`db.py:456-470`）。
- 方案 §P0-4-3（:174）新增 `LOTT_PREDICT_CHANNELS`（默认 `stat,ml,uniform`），语义与 `LOTT_METHODS` 的 allow-list 判然相同；同时 §P0-4-1（:172）还要把 `LOTT_LLM_ONLY_OUTPUT` 改成 0。三条开关描述的是**同一个决策**的不同侧面。

**问题**：这直接违反项目自己的「可审计、单一事实来源」原则（`config.py:390-392` U6 修复注释里已经踩过一次「多处配置互相漂移」）。两个开关一旦不一致（如 `.env` 留空而 `methods_config.json` 里存着 `stat,ml`），实际生效集合取决于**哪段代码先读**，而 `_load_runtime_methods_config` 在 import 期执行（`config.py:413`），新开关若在 `predict_next` 里读 `os.environ` 就会出现「设置页改了不生效」的经典 bug。

**建议改法**
1. **不新增** `LOTT_PREDICT_CHANNELS`。把方案要表达的「LLM 默认不出号」表达为 `config.py` 里 `LOTT_METHODS` 的**默认值变更**：`METHODS_RAW` 缺省时返回 `"stat,ml,uniform"`（allow-list），并在 `methods_status()`/`/api/info` 里附 `"default_from": "v10"` 便于审计。
2. 把 `LOTT_LLM_ONLY_OUTPUT` 在**同一处**收编：它表达的是「LLM 允许参与时，注数口径以 LLM 产出为准」，属于 `LOTT_METHODS` 的**子开关**，应改为 `config.py` 中紧随 `METHODS_SPEC` 的三值枚举 `LLM_OUTPUT_MODE ∈ {only, mix, off}`，默认 `off`；`off` 与 `LOTT_METHODS=-llm` 互为幂等（同时设置不报错、以后者为准并打日志）。
3. 若坚持要「出号通道」这个概念，**必须**定义优先级链并写进测试：`runtime json > .env > 代码默认`，且判据函数只有 **一个**（建议放 `lottery/methods.py`，新增 `resolve_channels()`），`engine.predict_next` / `battle` / `llm_eval` 全部改调它。

---

#### P0-2　`experiments` 表是第 4 本登记簿——已有 3 本，且「注册/去重/累加 attempts」原语现成

**证据**
- `lottery/hypothesis.py:1257 register_hypotheses()`：注释（:1263-1265）明确「同 statement 去重：重复注册**不新建记录**，而是累加 `attempts`」——这**已经**是方案 §P0-2-1「同一份规范重复注册返回已存在 id（幂等）」的实现，语义甚至更强（还留了尝试次数，正好回应方案附注里「我到底找了多少次不可审计」）。
- `lottery/hypothesis.py:1443 resolve_registry()` 已经实现了方案 §P0-2 想要的状态机裁决（`proposed → validated / rejected / insufficient`）。
- 现有三本登记簿：`data/hypotheses.json`（`hypothesis.py:1233 _save_registry`）、`data/llm_claims.json`（`llm_lab.py:42,451`）、`data/patterns_runtime.json`（`patterns_runtime.py:250 load_snapshot`）。新增 SQLite `experiments` 后为第四本，且 `patterns_runtime.build_snapshot`（`patterns_runtime.py:78-175`）**只读前三者**——新表里的 `passed` 实验**不会**进入出号链路的 `usable` 判定。

**问题**：方案 §P0-2 的收官目标之一（§3.2-1）是「`usable` 必须同时满足阶段持续 + A/B 通过 + 前置注册通过」。但 `usable` 的唯一计算点是 `patterns_runtime.py:104`：`usable = (status in USABLE_STATUSES) and h.get("status") == "validated"`。方案没有写**如何把第 4 本簿的结论并入这一行**。若不改这一行，P0-2 做完以后 `usable` 仍然是 0 条，用户看到的东西**完全不变**——这个改动等于白做。

**建议改法**
1. 优先方案（推荐）：**不建新表**。在 `hypothesis.py` 的假说记录里加三个字段（`pre_registered_at` / `exp_id` / `checked_at`），`_save_registry` 是纯 JSON 写盘，加字段无迁移成本；`register_hypotheses` 增加 `spec_hash` 幂等键；把 §P0-2-2 的「`checked_at > created_at` 硬校验」实现在 `resolve_registry` 内。这样 `usable` 只需在 `patterns_runtime.py:104` 加一个 `and h.get("preregistered_ok")`。
2. 若坚持建表（可接受的第二选择）：**必须同时**给出 `patterns_runtime.build_snapshot` 读取 `experiments` 的改动点，并把该改动写进 V10-02 的文件清单（当前清单里没有 `patterns_runtime.py`）。
3. 无论哪种，`exp_id = sha1(规范形式)` 要**与 `hypothesis` 的 statement 去重键兼容**，否则同一规律会在两本书里各占一条、`attempts` 分裂。

---

#### P0-3　迁移：新表全部走 `CREATE IF NOT EXISTS`、`schema_version` 不递增 → 版本契约失效 + 无可回滚点

**证据**
- `lottery/migrations.py:35-38`：`MIGRATIONS = [(1, …), (2, …)]`；`:47-55` 只执行 `version > current` 的迁移并写 `schema_version`。
- `lottery/db.py:169-172`：`executescript(SCHEMA)`（全是 `CREATE TABLE IF NOT EXISTS`，`:12-158`）→ `migrations.apply()` → `_ensure_columns()`。
  含义：**新表若只写进 `db.SCHEMA`，在已有库上会静默建成，但 `schema_version` 永远停在 2**——「这条库到底做过哪些迁移」不可审计，而这正是方案 §P0-2 想解决的那类问题（「我到底找了几次」）。
- `tests/test_m4_5_engineering.py:11`：`assert conn.execute("select count(*) from schema_version").fetchone()[0] == 2` —— 走正确迁移流程必然变红。
- 迁移机制本身的缺陷（方案没提，但新表会放大）：`:52-53` 是「先执行 DDL，再 INSERT 版本号」，**没有包 `BEGIN`/事务**；每个 `_ensure_*` 都是幂等 DDL 所以今天没炸，但方案 §P0-5 的 `replay_results` 是**数据表**，若某天要在迁移里 `INSERT` 回填数据，中途崩溃 = 「表建了、版本没记」，下次重跑又执行一遍。

**建议改法**
1. 新表**一律**加到 `MIGRATIONS` 作为 `(3, "v10 experiments/replay/llm_proposals", _ensure_v10_tables)`，**不要**只加到 `db.SCHEMA`（可以两边都加，但迁移列表是权威）。同时 `schema_version` 会变成 3，`tests/test_m4_5_engineering.py:11` 改成 `assert migrations.apply(conn) == migrations.MIGRATIONS[-1][0]` 或 `>= 2`（**不要**改成 `== 3`，否则下个批次又要改一次）。
2. `migrations.apply` 改成「整个批次一个事务」：`conn.execute("BEGIN")` 包住 DDL+INSERT，失败 `ROLLBACK`。
3. 新增 `db.schema_status()` 暴露 `migrations.status(conn)` 到 `/api/health`（`api_app.py:65-77` 已有 `version` 字段位），让「库是否已迁移」可在容器健康检查里看到。
4. **主键与幂等**：`experiments(exp_id PK)` 可以；`llm_proposals` 需要 `(run_id, ts)` 或自增 PK；`replay_results` 方案给的是 `(method, issue, n_tickets, …)` **无主键**（§P0-5-1:188）——必须补 `PRIMARY KEY (method, issue)` + `INSERT OR REPLACE`，否则每跑一次回放就多一份数据，而 `db.py` 现有写路径全是「先 DELETE 再 INSERT」或 `INSERT OR REPLACE`（`db.py:355,491,646`），方案的写法与项目习惯不一致。

---

#### P0-4　回滚与备份：唯一副本 + 容器内无 sqlite3 + 方案的备份步骤是宿主机文件拷贝

**证据**
- `DEPLOY.md:140`「唯一数据副本就是本机 `data/`」；`DEPLOY.md:203-215` 备份命令**是注释掉的 cron 示例**，不是已配置的事实。
- `docker-compose.yml:12-13`：`volumes: - ./data:/app/data`（bind mount，宿主目录）。所以 `DEPLOY.md:169-170` 的 `cp data/ssq.db /tmp/...` 是在**宿主机**执行的；容器里既没有 `sqlite3` CLI（`Dockerfile` 只 `pip install -r requirements.txt`），也没有 `BACKUP` API。方案 §3.2-7（:289）原样照抄了这条命令，**没有说明它在哪个 shell 执行**，也没有说明迁移失败后的**回滚动作**。
- 迁移是**不可逆**的（`DEPLOY.md:169` 自己的注释也这么说），而 SQLite 没有 `DROP COLUMN` 之前的廉价回退（3.35+ 才有）。

**问题**：V10 一次性加 3 张表 + 可能改 `predictions` 语义，是本项目历史上最大的一次 schema 变更，却只有「手工 `cp` 一次」这一道防线；且这道防线**不覆盖** `data/` 下的 JSON 运行时状态（`hypotheses.json` / `adaptive_state.json` / `patterns_runtime.json` / `llm_config.json` —— `DEPLOY.md:214-215` 自己也点了这些，方案 §3.2-7 把它们漏了）。

**建议改法（前置）**
1. 给 `db.py` 增加 `backup_to(path)`：用 `sqlite3.Connection.backup()`（在线安全，无需停容器）+ `PRAGMA integrity_check`，暴露为 `POST /api/admin/backup`（受 `config.API_TOKEN` 保护，`api_app.py:32-41` 中间件已覆盖 `/api/*`）。
2. 迁移前自动备份：在 `migrations.apply` 开头，若 `current < latest`，先 `conn.backup()` 到 `data/ssq.db.pre-v{latest}.bak`，并在日志与 `/api/health` 里给出路径。
3. 迁移后**自检**：跑 `PRAGMA integrity_check` + `SELECT COUNT(*) FROM draws` 与备份比对，不一致直接 `SystemExit`（宁可起不来，也不要在坏库上继续写）。
4. 把 §3.2-7 改写为：**顺序 = 停调度（`LOTT_SCHEDULER=0` 环境变量或 `docker compose stop`）→ API 备份 → `docker compose up -d --build` → `/api/health` 的 `schema_version==3` → 恢复调度**。这条顺序必须写进 DEPLOY.md，而不是留在方案里。

---

#### P0-5　`_ticket_objective` 可插拔目标 / `coverage_optimize` 退火 / `blue_coverage_plan` 前置计划：三者共存的语义方案未定义（且有一处事实性误读）

**证据（三者当前的真实关系）**
1. `engine.py:1442`：`plan = blue_coverage_plan(n_tickets, blue_probs) if blue_probs is not None else None` —— 蓝球计划只在**调用方传了 `blue_probs`** 时才存在；`predict_next` 的 coverage 分支（`engine.py:1731-1735`）**确实传了**，所以「前置计划」在生产路径上**恒生效**。
2. `engine.py:1492-1496`：选完红球后，`out` 里每注的 `blue` 被**强制改写**为 `plan[i]`。所以 `_ticket_objective` 里算出的 `distinct_blue`（`:1399`）在 SA 阶段是**候选池自带的蓝球**的去重数，与最终输出的蓝球**毫无关系**。
3. 由此得到一个比方案更强的结论：在 `predict_next` 路径下，`objective="expected_value"` 的目标函数**对最终输出连 `distinct_blue` 的梯度都没有**——蓝球在目标里是死的（因为随后被 `plan` 覆盖），红球项是常数（`portfolio_expected_value` 的 `expected_reward = one × n`，`:1286`，且 `:1303` 的 `prize_prob_by_level` 也是 `×n` 常数）。而 `:1602` 的 `build_models` 实测只 `0.028s`，`:1634` 每个模型只 `×2` 注 → 真实候选池 **8~10 注**（≥ `n_tickets=10`），于是 `engine.py:1444 len(pool) <= n_tickets` 分支**可能直接恒真**，整个 SA 一次都不跑。方案 §P0-3 症状描述（:137-139, :159）说「SA 在纯常数目标上仍跑 3000 次迭代，是纯浪费 CPU」——**在没有 coverage 调用方传大候选池的前提下，这个前提不成立**（实测 `pool=400, trials=3000` 只要 0.034s，也不是 CPU 问题）。方案把「目标退化」这个真 bug 归因错了位置。
4. `engine.py:1409`：`return float(ev) + cover_bonus * n / max(1, n)` —— `cover_bonus * n / max(1, n)` 恒等于 `cover_bonus`，这一段是**死代码**（作者的意图应是 `* n`）。方案照抄了这行结论但没指出这个 bug，而 P0-3 重构这段时若不删，同义反复会被「可插拔目标」框架原样继承。

**问题（方案未定义的共存语义）**
- 新目标 `pmax_high`（要最大化 `P(≥1注≥4红)`）与 `plan` 的关系：`plan` 是**结构性最优**（`engine.py:1013` 证明 `P(≥1蓝)=min(n,16)/16`）。若 `pmax_high` 也把蓝球写进目标，就会与 `plan` 冲突；若不写，则蓝球覆盖的最优性依赖 `plan` 单独保证——**方案没有一句话说清谁负责蓝球**。
- `p_any_blue` 目标与 `plan` **完全重复**：`plan` 已经保证 `p_any_blue = min(n,16)/16` 是解析最优，把它设成优化目标等于让 SA 去追一个已由闭式解达成的量（纯浪费算力，且 SA 找到的一定 ≤ `plan`）。方案把它列为 4 个目标之一，属于**未做完的穷举**。
- `objective` 的值域变更（`coverage/expected_value` → `ev_conserving/pmax_high/p_any_blue/custom_weight`）没有兼容策略（见 P1-8）。

**建议改法**
1. **先立边界再改函数**：写出 `coverage_optimize` 的新契约——「**蓝球由 `blue_coverage_plan` 唯一负责（闭式最优，不可被目标函数覆盖）；`objective` 只作用于红球子问题**」。这一句写进 docstring（`engine.py:1417-1433`）与 `config.COVERAGE_OBJECTIVE` 注释（`config.py:207-212`）。
2. `objective` 收敛为 3 个：`ev_conserving`（= 现有 `expected_value` 的**诚实改名**，只输出结构指标不做优化）、`pmax_high`（红球目标，须同时报 `var_ratio`）、`custom_weight`（权重来自 §P2-3 的用户偏好）。**删掉** `p_any_blue`，改为「蓝球覆盖报告项」而非优化目标。
3. 值域变更走**别名表**：`ALIASES = {"expected_value": "ev_conserving", "coverage": "ev_conserving"}`（`coverage` 旧口径与 `ev_conserving` 都只输出结构指标，行为差异在报告里体现），非法值回落到 `ev_conserving` 并打日志——与现有 `config.py:211-212` 的容错风格一致。
4. 删 `engine.py:1409` 的死代码；若「提前退出」确有必要，判据应是 `_ticket_objective` 对候选的**极差为 0**（`max-min < eps`）而不是拍一个迭代数；并且**只有当 `len(pool) > n_tickets`** 才有 SA 可跑（当前 `:1444` 已提前返回，方案这条规格在真实路径上是空操作）。
5. 方案 §P0-3-1 的 `ticket_distribution(tickets)` 归属重新分配：**`engine.py`**（与 `expected_prize`/`portfolio_expected_value` 同文件，复用 `PRIZE_CASH_FIXED`/`JACKPOT_ASSUMPTION`，`:1199-1307`），不要放 `rigor.py`。`rigor.py` 只加「EV 比守恒断言」这类与票单无关的判据。理由：`rigor.py:36-43` 的依赖只有 `numpy/scipy/stats`，刻意**不 import engine**（避免循环），反过来从 `rigor` 引 `engine` 会制造 `battle.py:66 → engine → rigor → engine` 的循环。

---

### P1（方案内部矛盾 / 未定义行为，开工前须给答案，但可与开发并行澄清）

#### P1-1　P0-1「LLM 对候选票单打分」与「预测接口的时序」冲突，方案未定义

- 现有 LLM 出口只有两种形态：`llm_lab`（异步任务，`api_app.py:1687 /api/llm_lab/run/async`；同步入口 `:1479`，同步跑 3 轮 LLM）和 `engine.llm_tickets`（在 `predict_next` 内联、受 `LLM_TOTAL_TIMEOUT=75s` 约束，`config.py:97`）。
- 方案 §P0-1-1 要求 LLM 输出 `decision`：对「我们给出的 2–5 个候选票单方案」打分。候选方案只在 `predict_next` 里才存在（`engine.py:1624 candidates` → `:1729 picked`），而 `predict_next` 的 LLM 预算已被 `llm_required` 严格模式占满（`engine.py:1656-1663`，超时即抛 `LLMChannelError` → HTTP 503，`api_app.py:234-240`）。**再加一轮 LLM 调用必然突破 75s**（`config.py:94-97` 注释已说明这是 Cloudflare 100s 的余量）。
- 结果：要么 `decision` 通道默认关闭（那就没有「AI 有可验证输出」的日常路径），要么把预测接口的失败率抬高。

**建议**：`decision` 只在**离线/异步**路径产生（复用 `_bg_run`，`api_app.py:1627` 已有通用后台任务框架），`predict_next` 只**读最近一次 `decision`** 并展示。方案必须明确写下这条，否则 V10-10 前端卡的「AI 技能」会在预测请求里等 2 分钟。

#### P1-2　§3.1 的文件作用域声明与事实不符（并行度是我能给出的最硬的一条）

| 文件 | 被哪些任务改 | 冲突性质 |
|---|---|---|
| `lottery/api_app.py` | 02（3 个路由）、**10**（前端卡要读的聚合接口）、**11**（成本报表 + 自动关停） | 三处新增独立路由/函数，可 rebase，但**不能算「互不触碰」**；且都在文件末尾 `_boot_refresh_rules_snapshot`（:1828）之前追加，合并时必然冲突一次 |
| `lottery/engine.py` | 03（`:1386-1497`）、**04**（`:1716-1737` + `:1788-1832` 的 `result` 字典） | **同一函数体的上下游**：04 加通道过滤会改变 `scored`/`picked` 的内容，而这正是 03 优化的输入；`result` 字典是 04 加字段、03 加 `ticket_distribution` 报告的同一处 |
| `lottery/config.py` | 04（新开关）、**12**（`APP_VERSION`/`APP_BUILD`，:26-27） | 相距 230 行，机械冲突 |
| `lottery/rigor.py` | 03（`ticket_distribution`）、**06**（`selective_eval`） | 同一文件尾部追加，可 rebase |
| `lottery/db.py` | 02（migrate）、**05**（`replay_results` 读写） | 都新增函数/迁移，可 rebase，但**迁移号必须是同一个**（第 3 号）→ 必须先由一人占号 |

**真实的并行度结论**：01 / 02 / 03 / 05 中，02 与 05 都改 `db.py` 且**都要写 migration #3**（硬顺序，不可并行）；03 与 04 都改 `engine.py` 的 `predict_next` 收敛区（硬顺序）。因此 **V10-01..05 的真实最大并行度 = 2**（一条 02→05 的 db 链 + 一条 03→04 的 engine 链），不是方案说的「四个完全独立」。

**建议合并顺序（同时把 V10-04 拆开，见 P1-7）**：
```
(0) V10-00「契约冻结」：migration #3 占号 + 新表 DDL + config 开关统一（P0-1/P0-2 的收敛方案）
(1) 04a（仅 config.py：通道/口径收敛，无 engine 改动）  ──┐
(2) 02（registry：hypothesis.py + db.py 迁移 + api_app 路由） ─┼─ 可并行 2 人
(3) 03（engine 目标函数 + ticket_distribution）              ─┘
(4) 04b（engine.predict_next 收敛 + result 字段）  ← 依赖 (1)(3)
(5) 05（replay：db.py 表 + evaluate.py + rigor 样本量）  ← 依赖 (2) 的迁移号
(6) 10 / 11（api_app + web），06/07/08 全程可并行，12 最后
```

#### P1-3　`blue_coverage_plan` 在非默认路径已被部分实现，「前置计划」说法需校正

- 已存在三套蓝球分散逻辑：`_plan_blue_coverage`（`engine.py:1073`，single 默认路径，`config.BLUE_COVER=1` 默认，`config.py:168`）、`_pick_blue_coverage`（`:1114`，U2 旧口径）、`_pick_legacy_blue`（`:1162`，`LOTT_BLUE_COVER=0` 时）。方案 §P0-3 只提 `blue_coverage_plan`，没有提这三套的收敛。
- 另外 `blue_coverage_plan` 的调用方不止 engine：`lottery/battle.py:180`、`lottery/evolution.py:26`（docstring）也直接用它。**改它的语义会影响 battle 回放的可比性**（`tests/test_v4_coverage.py:134-136,200-231` 锁了数值）。

**建议**：§P0-3 增加一条「`blue_coverage_plan` 签名与返回值**冻结**」，任何新语义放新函数；并在 `tests/test_v4_coverage.py` 侧补一条「battle 路径与 predict 路径的蓝球计划必须同源」的断言。

#### P1-4　P0-4 的「方法战绩必须把 `llm:*` 与 `stat/ml` 分列」——分列是已有的，真正要修的是「口径」不是「列」

- `eval_details` 本来就是**按 method 逐行**存的（`db.py:131-144`，主键 `(issue, method, seq)`），`cumulative_eval` 也是按 method 聚合（`db.py:562-590`）。所以「分列」已经天然成立，`eval_details` 实测 9 个 method 行。
- 方案说「当前 `eval_details` 的均值就是混算的」——**不准确**。真正混算的地方是 `eval_results`：`evaluate.py:146-147` 把一期所有注的 `red_hits` 取 `int(np.mean(...))` 存一个标量（`db.save_eval`）。而 `eval_results` 是 `/api/eval` 与 `online_check` 的汇总源。
- **建议**：把 §P0-4-4 改成「`eval_results` 弃用（或加 `method` 列）」，并把 `evaluate.online_check()` 的 `np.mean` 口径（`evaluate.py:146`，对整数取均值再 `int()` 截断，会把 1.5 抹成 1）一起修掉。这是**已有实现被复用**的问题，不是新写。

#### P1-5　P0-5「样本量」：`rigor.min_sample_size` 已存在，`sample_size_needed()` 是重复命名

- `lottery/rigor.py:641 _min_detectable_effect()`、`:1022 min_sample_size(effect, sd, alpha, power)` 都在。方案 §P0-5-3 要求「每期自动跑 `sample_size_needed()`」——**已有 `min_sample_size`，应复用而不是新写一个同义词**（项目里已有 3 个同义函数名 `_min_detectable_effect` / `min_sample_size` / `method_recommendations` 的 `min_sample` 参数，再加第 4 个会加剧混乱）。
- 同理 P0-1-5「复用 `rigor.strategy_significance` 的思路」——`strategy_significance`（`rigor.py:690`）签名是 `(strategy_hits, baseline_hits, n_trials, alpha)`，用于**命中数差值**的 t/sign 检验；Kendall τ 与随机排序的 p 值**不能**用它（它的 `n_trials` 语义是「搜索了多少策略」）。方案写「复用思路」含糊，实现时极易被照抄成错用。建议明确：τ 的 p 值用置换检验（scipy 有 `kendalltau` 且自带 asymptotic p），CI 用 Wilson（纯 stdlib 可写 10 行）。

#### P1-6　`LOTT_LLM_ONLY_OUTPUT` 默认 1→0：**真实影响面比方案说的小**，但方案自己也漏了两个事实

**实测**（`data/ssq.db` 只读查询）：
- `predictions`：2026114 期 8 注（含 `ml`/`stat:*`/`llm:*`），2026115–2026119 全部 **只有 5 注 `llm:agent-router/deepseek-v4-flash`**。→ 方案 §1.3 的「最近 4 期全部由 LLM 出 5 注」属实，且实际是 **5 期**。
- `eval_details`：只有 2026114–2026118 有数据（20 注 llm + 10 注其它），与方案「7 个方法、30 注」一致。

**关键推论（方案漏了）**：2026115 之后都是 5 注，而 `LOTT_N_TICKETS=10`。说明 `use_llm=True` 时，`predict_next` 走的是 `engine.py:1716-1727` 的 `llm_only` 分支，**最终数组里只剩 LLM 票**。这意味着：
1. **`eval_details` 里 `llm:*` 的 20 注是真实交战中方法对比的唯一来源**；把默认改成 0 之后，`eval_details` 将**不再产生任何 `stat:*` 与 `llm:*` 的同期配对行**，而 `db.method_recommendations()`（`db.py:603-643`，要求「同一期同时存在两种方法」）会永远停在 `insufficient_sample`。**方案 §P0-4 没有为「方法对比样本从哪来」给出替代**，这与 §P0-5「让没效果第一次能被测出来」直接矛盾。
2. 也就是说，默认值变更**不只是兼容性问题，它会掐断既有的方法对比数据源**。

**建议**：`LOTT_METHODS` 的默认值改为 `stat,ml,uniform`（P0-1 建议）的**同时**，为 `eval_details` 保留一个「研究臂」：在 `production` 出号之外，每期离线跑一次 `stat` 池 + 一次 `llm`（若配置可用）并**只写 `eval_details`/`replay_results`，不进 `predictions`**。这正是方案 §P0-5-1「`offline_replay` 与真实交战物理分表」的应然用法，但方案把它写成了两件互不相关的事（P0-4 改默认、P0-5 建回放表），没串起来。

另外两个方案没说的兼容点：
- `.env` 里注释掉的 `# LOTT_LLM_ONLY_OUTPUT=1`（`.env.example:44`）是**注释**，所以本地 `.env` 实际不设该变量 → 改代码默认即生效。这点方案没说清，容易被误以为「已部署环境不受影响」。
- **`data/methods_config.json` 的优先级没写**（`config.py:412` 在 import 期覆盖 `METHODS_RAW`/`METHOD_MODE`）。若用户曾在设置页保存过 method 开关，P0-1 改默认值**对他不生效**；而 `docker-compose.yml:14-62` 的 `environment:` 里**根本没有 `LOTT_METHODS` / `LOTT_LLM_ONLY_OUTPUT`** —— 这不影响正确性（config.py 直接读 `os.environ`），但它解释了**为什么 `.env` 里写这两个变量会被静默忽略**：`docker compose` 只透传列出的变量，`.env` 不会被自动注入容器。方案要动默认值，就必须同时补 `docker-compose.yml` 的透传，否则「用户按文档改 `.env` 却不生效」这个既有坑会被放大。

#### P1-7　V10-04 的估时 1d 与内容不匹配（它其实是两件事）

§3.1 把 `config.py` + `engine.py(predict_next)` + `.env.example` + 测试塞进 1 人日、标为无依赖可并行。但 `predict_next`（`engine.py:1563-1737`）是 175 行、涉及 `use_llm`/`use_ml`/`methods_spec`/`adaptive`/`llm_required`/`bet_mode`/`coverage` 七个开关的收敛函数，改它必然触碰 03 的 `_ticket_objective` 输入（`scored`/`picked`）与 P0-4-4 的报表。**建议拆为 04a（config + 测试，0.5d，真正独立）与 04b（engine 收敛，1d，依赖 03）**，这也正好把 P1-2 的并行度提到 2。

#### P1-8　`config.COVERAGE_OBJECTIVE` 值域变更：没有别名/回落策略

现状 `config.py:210-212` 是白名单 + 静默回落 `expected_value`。方案要换成 4 个新值（其中 2 个与旧值语义重叠，见 P0-5）。若直接替换，用户 `.env`/`methods_config` 里的旧值会被静默回落成第一个新值，**且 `eval_meta` 里历史快照的 `coverage_objective` 字段（`engine.py:1804`）会与新口径不可比**。建议见 P0-5 第 3 条（别名表 + 非法值日志）。

---

### P2（细节，随手修）

| # | 证据 | 问题 | 建议 |
|---|---|---|---|
| P2-1 | `web/index.html:38` `<input id="cfgLlm" checked>` + `web/static/app.js:282` `use_llm=` + `:287` | 前端「使用大模型」勾选框**默认 checked**，即使用户把 `LOTT_METHODS` 默认改掉，`/api/predict?use_llm=true` 仍会走 `llm_required=True`（`api_app.py:231`）→ 要么 503（LLM 未配），要么照旧只有 LLM 注。**方案 §P0-4 只改后端默认，前端默认勾选会让它失效。** | V10-04 的文件清单必须包含 `web/index.html` + `web/static/app.js`（当前清单只有 `config.py`/`engine.py`/`.env.example`）。 |
| P2-2 | `web/static/app.js:724,728-730` | `localStorage.cfgLlm` 持久化：老用户浏览器里存的是 `"1"`，改默认值**不迁移**。 | 迁移逻辑：读到的值版本号 `< v10` 则重置为 `"0"`。 |
| P2-3 | `lottery/evaluate.py:99-100` | `offline_backtest` 调 `E.predict_next(history, persist=False)`，若 `LOTT_METHODS` 默认含 LLM，**离线回放会按 production 出号口径算**，与「离线回放用独立方法」的意图不符。 | §P0-5 的回放必须在回放里**显式**传通道参数，不依赖全局默认；`offline_backtest` 加 `channels=` 参数。 |
| P2-4 | `docker-compose.yml` `environment:` 列表 | `LOTT_METHODS` / `LOTT_LLM_ONLY_OUTPUT` / `LOTT_COVERAGE_OBJECTIVE` / 任何新开关**都未透传**；`docs/V10` §3.2-6 说要改 `docker-compose.yml` 但只提版本号。 | 新开关若通过 `.env` 配置（方案 §P0-1-5 的 `LOTT_LLM_DECISION`、§P1-5 等），**必须**在这里加行；否则「改了 `.env` 不生效」。 |
| P2-5 | `lottery/ml_model.py:384-387` `_cache_key = f"{len}:{issue}"` | P1-3 改特征后，key 不变 → **旧的 265MB `data/ml_models/*.joblib`（已 gitignore、`data/` 是唯一副本）会被静默复用，模型与代码不一致**。方案 §P1-3 只提「只保留最近 2 个」，没提这个。 | `_cache_key` 加入 `MODEL_SCHEMA_VERSION` 常量；P1-3 落地时升版本号。 |
| P2-6 | `tests/test_v4_honest_ui.py:145-152`、`tests/test_v8_eras_tasks.py::test_js_prediction_fetch_is_cache_busted` | V10-10 必须升 `?v=`（当前 `web/index.html:7,586` 是 `20261018.3`），且 `app.js`/`style.css` 必须同步。方案 §3.1 V10-10 已写「必须升 `?v=`」，但**没写当前值**，执行者会猜。 | 在 V10-10 里写明基线 `20261018.3`。 |
| P2-7 | `lottery/migrations.py:60-63` `status()` | 已实现但**无人调用**（`grep` 无引用）。 | 接进 `/api/health`（`api_app.py:65`），迁移状态从此可观测。 |
| P2-8 | `lottery/discovery.py:1171` `all_quick` | 方案附注（:332）说「扫描结果在入库前被截断，`n_tested=91` 但只留 17 条」——实际 `discover()` **返回了全量 `all_quick`**，截断发生在 `patterns_runtime.py:145`（只遍历 `scan["items"]`，即 `top=12`）。方案归因不准确；`n_tested` 本身是完整枚举数（`discovery.py:1131`）。 | 附注改写为「快照层只取 top12 入簿」，登记簿的 `family_key/params_json` 因此必须从 `all_quick` 取全量，不能从 `items` 取。 |

---

## 4. 必须前置解决的事项（没有这些就不该开工）

> 这 6 条是 **Gate**：任一条没落成文字或代码，V10-01..05 不应开工。

1. **【V10-00 契约冻结任务】**（约 1 人日，必须在任何编码前完成）
   - 产出 `lottery/config.py` 的**开关收敛方案**：删除 `LOTT_PREDICT_CHANNELS`；`LOTT_METHODS` 默认值改为 allow-list `stat,ml,uniform`；`LLM_OUTPUT_MODE ∈ {only,mix,off}` 三值替代 `LOTT_LLM_ONLY_OUTPUT`；`COVERAGE_OBJECTIVE` 别名表（含 `expected_value→ev_conserving`）；一份**优先级链**（`methods_config.json > .env > 代码默认`）与唯一的 `resolve_channels()` 判据函数。
   - 同任务里**冻结**：`migration #3` 的编号与全部 DDL（`experiments`/`replay_results`/`llm_proposals`，含主键与 `INSERT OR REPLACE` 语义）、`blue_coverage_plan` 签名冻结声明、`predict_next` 的 `result` 新增字段清单（供 V10-10 前端并行开发）。
2. **【登记簿二选一】**：`experiments` 要么并入 `hypothesis`（推荐，见 P0-2 建议 1），要么在方案里**写明** `patterns_runtime.py:104` 的 `usable` 判定如何读新表。只写新表不改 `usable` = P0-2 白做。
3. **【迁移可审计 + 可回滚】**：`migrations.apply` 改为批次事务；迁移前自动 `sqlite3.backup()` 到带版本号的文件；`/api/health` 暴露 `schema_version`；`tests/test_m4_5_engineering.py:11` 改为 `>= 2` 或 `== MIGRATIONS[-1][0]`；DEPLOY.md 写入「停调度 → 备份 → 迁移 → 自检 → 恢复调度」的顺序。
4. **【文件作用域表重写】**：把 §3.1 的「文件」列替换为真实表（含 `web/index.html`、`web/static/app.js`、`docker-compose.yml`、`patterns_runtime.py` 的归属），并据此重排并行度（真实 2 路，见 P1-2）与合并顺序。
5. **【方法对比数据源不断供】**：明确「默认不再让 LLM 出号」之后，`eval_details` 的 `stat` vs `llm` 配对样本从哪来（见 P1-6 的研究臂方案）。不解决这一点，§P0-5「让没效果第一次能被测出来」在真实交战场上无法成立。
6. **【基线数字更正】**：§3.2-5 的 `381 passed` → 实测 `480 passed`；验收命令里加 `python -m pytest -q` 的**耗时预算**（实测 114.7s，新增约 65 例后预计 150s+，CI/本地都不会超时，但方案里应写明）。

---

## 5. 对 V10-01..V10-12 的逐条可行性意见

| ID | 可行性 | 意见 |
|---|---|---|
| **V10-01** AI 决策闭环 + 技能度量 | ⚠️ **有条件可行** | `llm_lab` 当前只有「提规律」一种输出（`llm_lab.py:56,585`），加 `abstain/decision` 两种需要重写 SYSTEM_PROMPT 与 `_normalize_claims`（:292）。**硬阻塞**是时序（P1-1）：`decision` 依赖候选票单，而候选票单只存在于 `predict_next` 内且预算已满 → 必须降级为异步/离线产出，`predict_next` 只读。另外 τ 的 p 值别误用 `rigor.strategy_significance`（P1-5）。 |
| **V10-02** 前置注册簿 | ⚠️ **语义现成，落点要改** | 幂等注册 + 去重 + `attempts` 累加**已有**（`hypothesis.py:1257`），`register/resolve` 状态机**已有**（:1443）。真正新的只有「注册**先于**读结果」的时序强制与「按批次做 Bonferroni」。建议改为在 `hypothesis.py` 上加字段（无迁移成本），而不是新表；若要新表，必须同时改 `patterns_runtime.py:104`（P0-2）。三个新路由放 `api_app.py` 会与 V10-10/11 撞（P1-2）。 |
| **V10-03** 票单分布 + 目标函数 | ⚠️ **诊断需更正，落点需改** | `ticket_distribution` 放 **`engine.py`**（与 `:1199/:1267` 同源，复用 `PRIZE_CASH_FIXED`），不要放 `rigor.py`（循环 import 风险，见 P0-5-5）。§P0-3 的「SA 在常数目标上白跑 3000 次」在真实路径上不成立（候选池 8~10 注，`engine.py:1444` 已提前返回；实测 3000 trials 仅 0.034s）。目标函数**确实退化**，但根因是 `plan` 覆盖蓝球（`:1492-1496`）+ 红球项常数，比方案说的更彻底。`config.COVERAGE_OBJECTIVE` 值域变更必须带别名表（P1-8）。`engine.py:1409` 的死代码顺手删。24/7 无风险，**唯一风险是与 V10-04b 争 `predict_next`**。 |
| **V10-04** 通道默认值 + 报表分列 | ✅ **可行但要拆、要补前端** | 拆 04a（config+测试，独立）/ 04b（engine，依赖 03）。**`LOTT_PREDICT_CHANNELS` 应删除**（P0-1）。前端 `cfgLlm` 默认 checked（`web/index.html:38`）+ `localStorage` 持久化（`app.js:724-730`）会吃掉后端默认值 → 必须改前端（P2-1/P2-2）。报表「分列」已天然成立（`eval_details` 按 method 存），真正混算的是 `eval_results`（`evaluate.py:146-147`，P1-4）。 |
| **V10-05** 离线回放分表 | ✅ **可行，须补三处** | ①`replay_results` 加 `PRIMARY KEY (method, issue)` + `INSERT OR REPLACE`（P0-3-4）；②`offline_backtest`（`evaluate.py:99`）必须显式传通道，否则会被 production 默认污染（P2-3）；③与 V10-02 共享 migration #3，硬顺序。复用 `rigor.min_sample_size`（:1022）而不是新写 `sample_size_needed()`（P1-5）。 |
| **V10-06** 选择性推断 | ✅ **可行** | `scipy 1.18.1` 已装，`truncnorm` 可用（实测）。`rigor.py:36-43` 无 `engine` 依赖，新增函数不制造环。仅追加，与 03 同文件但可 rebase。 |
| **V10-07** CUSUM 失效时点 | ✅ **可行** | `regime.py` 已有阶段切分与 `family_adjust`/`ALPHA`（`discovery.py:1132,1164` 在用），CUSUM 作为新函数追加。风险是 ARL0 标定的模拟测试耗时（1000 条序列 × 每期一次对数似然比）——建议纯 numpy 向量化，别用 `scipy.stats` 逐个算。 |
| **V10-08** ML 校准 + 置换重要性 | ✅ **可行，有一处隐含破坏** | `_cache_key = f"{len}:{issue}"`（`ml_model.py:384-387`）不含特征版本 → 改特征后会复用 265MB 旧 joblib（P2-5）。同一任务里必须加 `MODEL_SCHEMA_VERSION` 并清空 `data/ml_models/`。 |
| **V10-09** 因果归因 | ⚠️ **可行但依赖未定义** | 依赖 V10-05 的 `replay_results`，而该表的 schema 里**没有 method 级归因所需字段足够吗**——`evidence_rows` 要逐行核对「红/蓝命中」，`replay_results` 有 `red_hits/best_red_hit/blue_hit/prize`，够用；但「factor = blue_coverage / red_confidence / llm_pick」这三个因子**当前没有任何表记录**（`predictions` 只有 `evidence_json` 自由字典，`db.py:366`）。要么在 `eval_details` 加列（=另一个迁移），要么明确「因子只能从可复算的方法名推断」。方案没写。 |
| **V10-10** 前端三卡 | ✅ **可行** | 必须等 01/02/03 的接口契约（V10-00 的 `result` 字段清单）冻结。当前 `?v=20261018.3`（`index.html:7,586`），测试 `test_v4_honest_ui.py:145-152` 会强制 ≥ 20261015.2 且 app.js/style.css 一致。注意 `web/index.html` 与 `web/static/app.js` **不在** V10-04 的清单里却是必需的（P2-1）。 |
| **V10-11** 成本口径 + 自动关停 | ⚠️ **可行但要接已有开关** | `llm_client.chat` 已返回/记录 token（`llm_client.py:134-200`）；`config.LLM_EVAL_PRICE_PER_1M` 已存在（`config.py:297`，但语义是 USD/展示用）——新成本日志用「元」需要汇率口径，方案未定义。自动关停必须**接到 `resolve_channels()`**（P0-1）而不是另写一个 `os.environ["LOTT_LLM_DECISION"]="0"`，否则重启即失效、且与 `methods_config.json` 优先级冲突。 |
| **V10-12** 文档 + 版本 + 部署 | ✅ **可行** | 与 V10-04 同改 `config.py`（版本号 `:26-27`）→ 机械冲突，放最后即可。`APP_VERSION` 升 `3.0.0` 会让 `tests/test_v4_honest_ui.py:167` 与 `test_v4_evolution_step.py:240` 的 `>= (2,0,0)` 断言仍然通过（实测这些断言是向前兼容的，无需改测试）。`docker-compose.yml` 需要补新开关透传（P2-4）。 |

---

### 附：评审未覆盖的内容（交给其他评审）

- 统计正确性：`pmax_high`/`var_ratio` 的推导、泊松二项近似的误差界、选择性推断的截断正态是否适用、CUSUM 的 ARL 标定公式。
- LLM 方法有效性：`decision` 的 Kendall τ 是否真能区分模型能力（随机排序基线 τ≈0，小样本方差很大，n=5 个方案时 τ 只有 10 种离散取值——方案第 5 条说「N≥30 次」是对的，但 30 次 τ 的 CI 宽度需要确认）。
- 产品口径：§第四部分的一句话预期管理是否与 §2.0 的口径表一致（我核对了，一致）。
