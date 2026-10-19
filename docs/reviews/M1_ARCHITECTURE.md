# M1 架构评审：模块化与崩溃隔离（R1 · 第 1 点）可行性

> 评审对象：整个 `ssqyuce` 系统（Python 3.11 + FastAPI + SQLite，单进程单容器 `ssq-predictor`）
> 评审视角：**模块化 / 故障隔离改造的可行性**。只读评审，未修改任何业务代码。
> 评审依据：对仓库的实地核查（`grep`/`read`/`wc -l` + 容器实测 `docker inspect`/`docker stats`/`docker logs`）。
> 基线：仓库 `v4.1.0`（`ca23283`，2026-10-19），`tests/` 58 个文件、**920 个 `def test_`，`pytest --collect-only` 展开为 1057 项，实跑 1036 passed / 1 failed / 19 skipped / 1 xfailed**；容器 `ssq-predictor` 运行中、healthy、`restart=0`、`OOMKilled=false`、`ExitCode=0`。
> 该 1 个 failed **不是环境问题，是本轮新发现的真实缺陷**，且它正是本评审第 1 点要解决的问题的**活标本**，见 §3.3。

---

## 0. 一句话结论

**具备改造条件，但"模块化"在这里的正确含义不是"把 api_app.py 拆文件"，而是"把现存的耦合点（共享单连接 SQLite、共享可变全局、进程内长耗时线程、同步阻塞端点）转成有超时与边界的隔离单元"。** 本系统在**进程内**已经能实现绝大多数崩溃隔离，真正的"一个崩溃不拖死全站"在单节点容器内能做到的上限 = **进程内隔离 + 分层降级 + 启动自愈（restart）**，到不了"微服务独立部署"那一步（成本不符）。详见 §7。

---

## 1. 现状盘点（事实证据）

### 1.1 规模与"伪模块化"

- `lottery/` 下 **38 个 `.py`，总计 28,853 行**（`wc -l lottery/*.py | tail`）。
- 最大的三个文件：`api_app.py`（**3381** 行，单进程 FastAPI 入口）、`engine.py`（**2988** 行，预测引擎）、`hypothesis.py`（**1978** 行）。三者占总量 ~29%。
- **38 个文件 ≠ 38 个模块**。`api_app.py` 一个文件同时承担了 **HTTP 路由层（80 个路由装饰器 / 79 个唯一路径）+ 调度线程 + 启动钩子 + 视图/JSON 拼装 + 部分业务逻辑**（`_scheduler_step`/`_evolution_step`/`_bg_run` 全在 api_app.py）。这是"单文件单进程"，不是分层。
  - 端点口径（实测）：79 个 `@app.get/post/put/delete` + 1 个 `@app.api_route("/api/predict", methods=["GET","POST"])`（`api_app.py:256`）= **80 个路由**；`/api/predict` 的 GET/POST 共用同一路径，故唯一路径 **79**。

### 1.2 import 依赖图（文字版）

顶层 import（`grep '^from .' lottery/*.py`）：

```
api_app ──> config, data_fetcher, db, migrations, features, diagnose, mining
engine  ──> backtest, config, db, features, llm_client, methods, ml_model, models   (+ lazy: shape_rules, patterns_runtime, evaluate, discovery, ai_audit)
hypothesis ─> config, features, llm_client
evaluate  ─> db, engine               ← 静态依赖
backtest  ─> db, features, patterns
battle    ─> config, engine, evaluate, models, rigor
replay    ─> battle, db, engine, evaluate, methods, rigor
llm_eval  ─> config, db, engine, evaluate, llm_client
llm_lab   ─> config, discovery, llm_client, regime
mining    ─> backtest, db, features, patterns
patterns_runtime ─> config (+ lazy: discovery, evolution, hypothesis, regime, rule_lifecycle)
discovery ─> regime
db        ─> config, methods (+ lazy: evaluate@:488)
ml_model  ─> config, features
evolution ─> config (+ lazy: engine@:853, ...)
ai_audit  ─> db (+ lazy: evaluate, discovery, evolution, llm_lab, llm_client)
```

**依赖被刻意"懒加载"打散**（`api_app.py` 里有 70+ 处 `from . import X` 写在函数体里；`engine.py:38`、`:537`、`:556`、`:575`、`:635`、`:942`、`:1385`、`:2425` 等）。懒加载是**逃避循环依赖的补丁**，不是分层。

### 1.3 循环依赖（真实存在）

| 环 | 证据 | 性质 |
|---|---|---|
| `engine → evaluate → engine` | `engine.py:12` 顶层 `import evaluate`（只在 `:556` 用）；`evaluate.py:8` 顶层 `import engine as E`，`:89` 又 lazy `import engine` | **顶层真环**：靠 `evaluate.py:89` 的 lazy import 延迟到底避免导入期崩 |
| `engine → patterns_runtime → (discovery/evolution/hypothesis/regime) → engine` | `engine.py:537/:635/:2425` lazy `import patterns_runtime`；`patterns_runtime.py:100` lazy `import discovery, evolution, hypothesis, regime`；`evolution.py:853` lazy `import engine` | 懒加载环 |
| `db → evaluate → engine → db` | `db.py:488` lazy `import evaluate as EV`；`evaluate.py:8` 顶层 `import engine`；`engine.py:12` 顶层 `import db` | 懒加载环 |

**后果**：任何"把懒加载改成顶层 import 以做严格分层"的重构，都会直接触发导入期 `ImportError`/循环——这是迁移的第一道坎，不是第二道。`rigor.py:1385` 的注释 `# 为什么放在 engine.py 而不是 rigor.py：rigor.py 刻意不 import engine（避免循环）` 是代码作者亲口承认的"为避环而塞错文件"的证据。

### 1.4 共享可变全局（模块级单例 / 缓存 dict）

| 位置 | 符号 | 风险 |
|---|---|---|
| `db.py:160` | `_CONN: sqlite3.Connection = None` | **全局唯一 SQLite 连接**，`check_same_thread=False`（`db.py:167`），所有线程共用。见 §1.5 |
| `engine.py:1433-1435` | `_ALL_RED_MASKS`（C(33,6)=1,107,568 项 ≈8.9MB）、`_POPCOUNT_LUT`、`_EXACT_TAIL_CACHE` | 惰性构建的进程级缓存；并发首次访问无锁（`_all_red_masks` `engine.py:1461`） |
| `engine.py:282` | `_MODEL_BUILDERS` | 进程级模型构造表 |
| `ml_model.py:1250-1254` | `_ML_CACHE` + `_CACHE_LOCK` + `_TRAIN_EVENT` + `_TRAIN_LOCK` | 训练缓存 + 训练权单例（有锁，是对的） |
| `api_app.py:1538-1540` | `_ML_DIAG_CACHE` / `_ML_DIAG_JOB` | 端点级缓存，**无锁** |
| `discovery.py:696/:865/:1360` | `_WD_CACHE` / `_FEATURE_TABLES` / `_DISCOVER_CACHE` | 扫描缓存，无锁 |
| `patterns_runtime.py:385` | `_CACHE`（`mtime/data` 快照缓存） | 快照缓存，无锁 |
| `llm_client.py:15/:52/:344` | `_usage` / `_cost_lock` / `_budget_lock` | LLM 计量（有锁，正确） |
| `methods.py:523` | `_WARNED` set | 只告警一次，无害 |

**无锁全局缓存 + `check_same_thread=False` 单连接 = 线程安全只能靠"恰好没人同时写"来维持。** 这不是 bug 清单，而是"崩溃传染"的结构性土壤。

### 1.5 SQLite 单连接与锁

- `get_conn()`（`db.py:163-176`）：`_CONN = sqlite3.connect(..., check_same_thread=False)`，**全进程共享一个连接**。
- **没有** `PRAGMA journal_mode=WAL`、**没有** `busy_timeout`（全库 grep 仅 `PRAGMA table_info`/`database_list` 用于元数据，见 `db.py:179/182`、`migrations.py:164`）。
- 写路径大量 `conn.commit()`（`db.py:185/217/228/291/321/344/369/436/483/507/662/700-724/778/844`），但**没有 `BEGIN IMMEDIATE` 做写锁抢占**（只有 `migrations.py:282` 在 DDL 时显式 `BEGIN IMMEDIATE`）。
- **后果**：SQLite 默认的写锁争用下，两个线程同时写 → `sqlite3.OperationalError: database is locked`；而 `db` 是全系统最低层，**任何模块的写路径都可能抛这个错**，往上传播时若调用点没包 `except Exception` 就会 500/炸线程。实测 `api_app.py` 有 **63 处 `except Exception`**（全库最多），但这恰恰说明"大量异常靠广谱捕获吞掉"，吞掉的后果是**静默坏数据而非安全**。

### 1.6 后台线程（崩溃传播的真正载体）

| 线程 | 起点 | 行为 |
|---|---|---|
| `_scheduler_loop` | `api_app.py:3381-3382`（`config.SCHEDULER_ENABLED` 时启动） | 每 1800s 跑 `_scheduler_step`（`api_app.py:2146`） |
| `_bg_run` | `api_app.py:2816-2848` | 后台任务线程（closed-loop/llm_lab/audit 用） |
| `_boot_refresh_rules_snapshot` | `api_app.py:3348-3349`（import 期启动，sleep 20 后连 SQLite） | 启动补刷规律快照 |
| `_boot_warm_selfcheck_null` | `api_app.py:3374-3375` | 启动预热自证零分布（≈5.6s 计算） |
| `ml_model` 训练线程 | `ml_model.py:1466` | `get_ml_models` 抢训练权 |
| `llm_eval` worker | `llm_eval.py:35` | LLM 离线评估 worker |
| `start_ml_warmup` | `api_app.py:3307` 附近（模块级调用） | 启动时训练 ML 模型 |

**历史段错误修复（commit `727054b`）的痕迹**：api_app 在**导入期**启动 `boot-rules-refresh` 线程，与 joblib 多进程叠加触发 **Segmentation fault**；修复方式是加 `config.BOOT_REFRESH_RULES` 让 pytest 置 0（`api_app.py:334再看看` 的注释块，`:3336-3352`）。**这证明：导入期起线程 + 共享 SQLite 连接 + 多进程后端，三者叠加会真的崩进程。** 这就是"崩溃隔离缺失"的实证——一次已经踩过的坑。

### 1.7 同步阻塞端点 vs async

- **80 个路由中，只有 1 个是 `async def`**（`llm_audit_run`，`api_app.py:3049`）；中间件 `optional_api_auth`（`api_app.py:35`）虽也是 `async`，但不是路由。**其余 79 个全是 `def`（同步）**，FastAPI 会把它们丢进 **默认线程池**（ThreadPoolExecutor）执行。
- **含义**：同步端点里再调同步长耗时逻辑（LLM、回测、扫描、训练），是"线程池线程 + 线程池线程"，**不会阻塞事件循环**——这反而降低了"阻塞 asyncio 事件循环"这条风险。真正的风险是：**同步长耗时端点吃满线程池 → 新请求排队（starving）→ healthcheck 超时**。见 §2.2。
- 例外：`llm_audit_run` 是 `async def` 且内部有 `await`（读 request body），**在事件循环上跑**；其内的重逻辑若没外包给线程/`_bg_run`，会阻塞事件循环。

### 1.8 容器资源现实（决定选型的硬约束）

实测（`docker inspect` + `docker stats --no-stream`，2026-10-19）：

- `mem_limit: 805306368`（**768MiB**），`memswap_limit: 1024MiB`；**无 CPU 限额**（`NanoCpus=0`，`CpuQuota=0`）。
- 当前驻留 **392MiB / 768MiB = 51%**，CPU **0.19%**（idle）。`OOMKilled=false`，`RestartCount=0`，`ExitCode=0`，策略 `unless-stopped`。
- 宿主机 **4 核 / 12GiB RAM**（`free -m`），同机还跑着 9 个其它容器（`docker ps`：hindsight、rustdesk、pentagi、pgvector 等）。
- `compose` 里已设 `OMP_NUM_THREADS=1`/`MKL_NUM_THREADS=1`/`OPENBLAS_NUM_THREADS=1`（`docker-compose.yml:69-71`），且有多个 `n_jobs=1` 的防死锁补丁（`mining.py:300-301`、`ml_model.py:471-484`）。
- `data/` 已 **222MiB**（含 222MiB 的库/产物）。容器无 `ps`。

**结论**：内存余量仅 ~376MiB 留给峰值，CPU 是"共享 4 核"。这决定了 **"多进程隔离"和"独立容器"在内存上几乎不可行**（一个额外 Python 进程 + 加载 numpy/ML 模型就要 >200MiB）。详见 §4 选型。

---

## 2. 崩溃传播路径枚举（一条腿断 → 全站拖死怎么发生）

本系统的崩溃不是"模块 A panic 了进程崩"，而是**共享资源被一个模块耗尽/卡死，拖死所有模块**。每条路径给出触发点与影响面：

### 路径 A：同步阻塞端点吃满线程池 → 全站假死（高危、最常见）

- 触发点：任意长耗时同步端点并发触发。长耗时端点清单：`/api/refresh`（`api_app.py:116`）、`/api/predictions/last` 内部 `predict()`（`api_app.py:257`，LLM 出号 1~8 分钟）、`/api/evolution/run`（`api_app.py:2964`）、`/api/llm_lab/run`（`:2603`）、`/api/rules/closed-loop`（`:2790`）、`/api/ml/eval`（`:943`）、`/api/multiscale/scan`（`:1732`）、`/api/discovery/scan`（`:2545`）。
- **传播链**：`predict()`（`api_app.py:283-290`）→ `engine.predict_next`（`engine.py:2279`）→ `llm_tickets`（`engine.py:795`，`ThreadPoolExecutor(max_workers=min(len(calls),4))`）→ `llm_client.chat`（`llm_client.py:558`，`requests.post(timeout=eff_timeout)`）。
- **为什么是崩溃而非慢**：FastAPI 同步 `def` 端点的线程池默认 `max_workers=40`。若 40 个 worker 全被 LLM（`LLM_TOTAL_TIMEOUT=75s` 起，`config.py:108`；单次可到 `LLM_LONG_TIMEOUT=180s`，`config.py:96`）或回测（`evolution_step` 实测 19~22 分钟/轮，`api_app.py:2244` 注释）占满，**第 41 个请求（包括 `/api/health`）排队等待** → healthcheck `timeout=8s`（compose）超时 → `restart` 策略重启整个容器。**健康探活与业务共享同一进程池，这是"模块化缺失"的最典型传染。**
- **证据**：`api_app.py:116 refresh` 同步抓数据；`api_app.py:2964 evolution_run` 同步跑完整进化；文档 `docs/PROJECT_CONTEXT.md:45` 明写"启动预热期 healthcheck 可能短暂 unhealthy，回测占满 CPU 使 8s 探活超时"——**已在生产观察到 healthcheck 因 CPU 被占而失败**。

### 路径 B：全局共享 SQLite 单连接被写锁卡死 → 所有读写模块报 `database is locked`

- 触发点：`_scheduler_loop` 的 `_scheduler_step`（写 `predictions`/`patterns_runtime`）与任一同步端点（写 `eval_results`/`tasks`）并发写同一 `_CONN`。
- **传播链**：SQLite 默认 journal（非 WAL）+ 无 `busy_timeout` → 第二个写者立刻 `OperationalError: database is locked` → 该异常在 `db.py` 任意写函数向上抛 → 上层若没包 `except Exception`（如 `_bg_run` 的写任务路径 `api_app.py:2830` 是包了的，但 `predict` 的 `except Exception` `api_app.py:289` 会把"锁"吞成通用 500）→ **部分静默丢写、部分 500**。而 `db` 是底层，**任何模块的写都可能中招**。
- **severity**：这是数据面级的传染，**不是单模块**。历史段错误 commit `727054b` 已证明多线程并发连同一 `_CONN` 会崩进程。

### 路径 C：内存暴涨 → 撞 768MiB → OOM 杀进程 → restart 全站重建

- 触发点：`_all_red_masks()`（`engine.py:1461`）一次性构建 **1,107,568 项 ≈8.9MB**；`_rebuild_probs`（`engine.py:238`）在 history 上重建；`coverage` 优化（`engine.py:2006-2133` `_ticket_objective`）枚举；`discovery._DISCOVER_CACHE` 无上限累积；`llm_eval` 批量跑。
- **传播链**：进程 RSS 逼近 768MiB（当前已 392MiB，余量 ~376MiB）→ cgroup OOM killer 杀整个容器进程（`OOMKilled`）→ `restart: unless-stopped` 重建 → **启动预热 2~2.5 分钟 healthcheck unhealthy，期间全站不可用**。一次 ML 训练或 coverage 全枚举即可触发。
- **severity**：最硬的隔离缺口——**内存没有进程级边界**，一个模块的 alloc 直接耗全进程预算。

### 路径 D：LLM 网关挂起 → 长同步端点占住 worker → 回到路径 A

- `llm_client.py` 的 `chat` 已有**单次硬上限**：`if time.time()-t_start > 600: break`（`llm_client.py:552`）、重试 ≤3 次、429/5xx 退避 ≤3 次（`:571-591`）、`deadline` 墙钟（`llm_client.py:556`）、`eff_timeout = min(timeout, remaining)`（`:556`）。**所以单次 LLM 不会无限挂死**。
- **但**：`predict()` 同步端点（`api_app.py:257`）不设 `deadline` 时走 `config.LLM_TOTAL_TIMEOUT=75s`（`config.py:108`），而 `engine.llm_tickets` 用 `ThreadPoolExecutor` 并发 4 个采样 + 观察轮 + 校验轮，**总墙钟可能远超 75s**（`deadline=None` 时不强制）。多个这样的请求并发 → 线程池占满 → 路径 A。
- **severity**：LLM 是"最长阻塞源"，是线程池饥饿的头号贡献者。

### 路径 E：未捕获异常 → 后台线程静默死亡或端点 500

- 调度线程 `_scheduler_loop`（`api_app.py:2387-239ayo`）外层有 `except Exception` 兜住（`:2392`），**不会崩主进程**，但**一次异常后线程继续 `time.sleep(1800)`，不会自愈**。
- 导入期起的 `_boot_refresh_rules_snapshot` 线程若在 `sleep(20)` 期间进程被 kill/重启，`daemon=True` 直接丢。**无自愈、无重试**。
- **severity**：中等。不会崩全站，但"静默失效"（调度漏跑、快照陈旧）会被当成"系统正常"。

### 路径 F：递归/枚举爆炸（理论存在，实测受控）

- `engine.py` 无 `sys.setrecursionlimit`，无递归函数；重枚举集中在 `itertools.combinations`（`engine.py:1464/2886-2887`），均有 `islice`/上限截断（`engine.py:2898`）。**递归爆炸这条当前不成立**，列为"将来加规则时须设防"。

**优先级排序**：**A（线程池饥饿）> B（SQLite 锁传染）> C（OOM 内存）> D（LLM 挂起）> E（静默失效）**。F 暂不成立。

---

## 3. 崩溃隔离：为什么"拆文件"不是答案，**边界 + 超时 + 降级**才是

用户问"整个系统模块化，防止一个崩溃影响系统"。关键纠偏：

- **拆文件 ≠ 隔离**。把 `api_app.py` 拆成 `views/`/`router/` 只是可读性，**进程内照样共享同一 `_CONN`、同一内存、同一线程池**——A/B/C 三种传染原样存在。拆文件解决的是"第 4 点大模型位置"的组织问题，不是"崩溃隔离"。
- **真正的隔离维度只有三个**：① **时间隔离**（每段工作有 deadline/超时，超时即放弃）；② **资源隔离**（内存/CPU 有配额，超出即停止）；③ **数据隔离**（慢的、易错的重算与快的、必须活的读路径分开，重算挂了不影响读）。三者都能在**进程内**做，多数已做了一半（见 §3.1）。

### 3.1 现状里"已经做对"的隔离（改造的免费起点）

| 隔离措施 | 位置 | 说明 |
|---|---|---|
| LLM 单次硬超时 | `llm_client.py:552`（600s 上限）、`:556`（deadline 墙钟）、`:558`（requests timeout） | 已防 LLM 无限挂死 |
| LLM 多模型容错 | `engine.py:801-831`（观察轮多模型轮转） | 单模型挂不废通道 |
| 失败降级不抛 | `engine.py:2385-2396`（LLM 通道异常降级纯统计）、调度各 try/except | 已把"LLM 挂"与"出号"解耦 |
| 调度外层兜底 | `api_app.py:2387-2394`（`_scheduler_loop` 的 `except Exception`） | 调度异常不崩主进程 |
| ML 训练单例锁 | `ml_model.py:1250-1254`（`_TRAIN_LOCK`/`_TRAIN_EVENT`） | 防并发重复训练 |
| CPU 防死锁补丁 | `mining.py:300`、`ml_model.py:471`（`n_jobs=1`）、compose `OMP_NUM_THREADS=1` | 已防 joblib 死锁 |

**结论**：这个系统**已经在本能地做进程内隔离**（降级、超时、容错全都有），缺的是"系统化 + 把隔离边界画清楚 + 把致命缺口（线程池、单连接、内存）补上"。

### 3.2 四个致命缺口（改造重点，精确到代码位置）

1. **健康探活与重计算共享同一线程池** → `/api/health`（`api_app.py:67`，同步 `def`）与 `/api/predict`（`api_app.py:256` `@app.api_route`）/`/api/evolution/run`（`api_app.py:2964`）/`/api/llm_lab/run`（`:2603`）/`/api/rules/closed-loop`（`:2790`）/`/api/ml/eval`（`:943`）/`/api/multiscale/scan`（`:1732`）/`/api/discovery/scan`（`:2545`）**共用一个 40-worker 线程池**。慢端点占满即饿死 health，`healthcheck timeout=8s`（`docker-compose.yml`）失败 → `restart: unless-stopped` 重启。
2. **SQLite 单连接、无 WAL、无 busy_timeout** → `db.py:160` `_CONN`、`:167` `connect(..., check_same_thread=False)`；全库**零** `PRAGMA journal_mode=WAL` / 零 `busy_timeout`（仅 `db.py:179/182`、`migrations.py:164` 用 `PRAGMA table_info/database_list` 读元数据；唯一显式 `BEGIN IMMEDIATE` 在 `migrations.py:282` 的 DDL 路径）。写锁异常从 `db` 层向所有模块传染。
3. **进程内无内存配额意识** → `engine.py:1459-1467` `_all_red_masks()` 一次构建 `C(33,6)=1,107,568` 项 `int64`（注释自承 ≈8.9MB，`engine.py:1460`）；`engine.py:1435` `_EXACT_TAIL_CACHE` 上限 64（`:1436`，有界，尚可）；`discovery.py:1360` `_DISCOVER_CACHE` **无上限**、`discovery.py:696` `_WD_CACHE` **无上限**；`engine.py:2006-2133` coverage 枚举 `_ticket_objective`。当前 RSS 392MiB/768MiB（余量 ~376MiB），一次 ML 训练 + 全枚举即可能撞顶。
4. **无锁共享全局缓存** → `api_app.py:1538` `_ML_DIAG_CACHE` / `:1540` `_ML_DIAG_JOB`（无锁）；`patterns_runtime.py:385` `_CACHE`（无锁）；`discovery.py:696/:1360`（无锁）；对照 `ml_model.py:1250-1254` 与 `llm_client.py:15/52/344` **是加了锁的**——同一系统里两套标准，正是"模块化缺失"的表现。

### 3.3 活标本（Lead 实测，本轮新发现）：跨模块生命周期脱钩 → 下游零防御 → 崩溃

**失败用例**：`tests/test_v12_shape_lock.py::test_production_pass_rate_is_locked_on_real_data`
**栈**：`lottery/engine.py:41` `np.percentile(s, 5)` → `IndexError: index -1 is out of bounds for axis 0 with size 0`

**根因链（三个不同生命周期的模块，各自答同一个问题，答案不一致）**：

1. `tests/conftest.py:119` 的 `autouse` fixture 把**所有**用例的 `config.DB_PATH` 重定向到临时空库（`:98` `mp.setattr(config, "DB_PATH", env.db_path)`）——这是 V10-00b 为修"字母序巧合全绿"加的正确隔离。
2. 但用例守卫 `_has_real_db()`（`tests/test_v12_shape_lock.py:63-70`，内部 `db.count_draws()`）在**模块导入/收集期**求值（`requires_real_db = pytest.mark.skipif(not _has_real_db(), ...)`，`:73-75`）——那一刻 conftest 尚未 patch，读到真实库 3516 期 → 判定 True → **不 skip**。
3. 用例执行时 conftest 已生效，`db.load_draws()` 读到**空**临时库 → `draws = []`。
4. `engine.constraint_ctx([])`（`engine.py:24`）→ `_last_sums([])`（`:20-21`）→ `F.sums([])` 返回空数组 → `np.percentile(s, 5)`（`engine.py:41`）对 `size 0` 抛 `IndexError`。

**为什么这是"第 1 点"的完美标本**：同一个"是否有真实库"的问题被 **conftest（执行期正确）** 与 **test guard（收集期过期答案）** 两套时序回答；而下游 `constraint_ctx` 对空输入**零防御**，把 numpy 的 `IndexError` 直接抛穿到调用方——调用方无法从 `IndexError` 推断"数据不足"。风险在于：**同样的空/短输入在运行时也可能出现**（抓取失败后首启动、临时库、测试环境、DB 被重建），而 `predict()`（`api_app.py:283`）虽有 `if not draws: return 400` 挡住了"完全空库"，却**没有挡住"非空但不满足 N 期下界"的短输入**。

**两条必须落地的结论**：

- **隔离边界必须是"数据类型级契约"，不能靠"调用时序巧合"。** `constraint_ctx(draws)` 应显式声明前置条件 `len(draws) >= N`（N 建议 = 快照层 `patterns_runtime.MIN_DRAWS_FOR_SNAPSHOT = 500`，与 `shape_rules.shape_constraint_ctx` 的 500 期口径一致），并：
  - 对空/短输入返回**明确的降级值**（如 `shape_rules.shape_constraint_ctx` 在 `len(arr) < 100` 时已经做的那样，`shape_rules.py:197-204` 回退理论带），**或**
  - 抛出**类型化异常** `InsufficientDataError(min_required, actual)`，由 API 层转成 422/503 + 可读原因；**绝不允许** numpy 的 `IndexError` 穿透。
  - 现成的正例：`shape_rules.shape_constraint_ctx`（`shape_rules.py:196-204`）已有 `if len(arr) < 100:` 理论兜底；`features.compute_features`（`features.py:239-256`）已对每个 `np.mean/np.std/percentile` 加 `if len(x)` 守卫。**`engine.constraint_ctx` 是唯一漏网的那一个**。
- **测试守卫必须在执行期求值**（fixture 内 / 测试函数内），不得在收集期缓存：`_has_real_db()` 应改为 `@pytest.fixture` 在测试体内调用，或直接 `@pytest.mark.skipif` 改为函数内 `pytest.skip(...)`。

### 3.4 "空 / 短 / 脏输入"未设前置检查清单（功能级降级最便宜的落点）

对 `engine.py` / `features.py` / `shape_rules.py` 逐一 grep `np.percentile` / `np.mean` / `np.std` / `[-n:]`，结果：

| 文件:行 | 代码 | 是否有空/短输入守卫 | 判定 |
|---|---|---|---|
| `engine.py:41-42` | `np.percentile(s, 5/95)` | ❌ **无**（`_last_sums` 对空 draws 返回空数组） | **必须修**（本次崩溃点） |
| `engine.py:24-31` | `draws[-500:] if len>=500 else draws` | ⚠️ 只是切片技巧，空列表仍得空 | 随 :41 一并修 |
| `engine.py:36` | `v / max(1, len(oc))` | ✅ 有 `max(1,...)` | 安全 |
| `engine.py:113` | `np.mean(red_blend[np.array(reds)-1])` | ⚠️ 依赖 `reds` 非空、`red_blend` 长度 ≥33 | 建议断言 |
| `engine.py:308` | `if len(hist) < 2: ...` | ✅ 有 | 安全 |
| `engine.py:325/329` | `np.mean((rebuilt-tr)**2)` | ✅ 由 :308 守卫间接保护 | 安全 |
| `engine.py:546` | `if len(draws) < 2:` | ✅ 有 | 安全 |
| `features.py:239-241` | `np.mean/std/percentile(s) if len(s) else 0` | ✅ **逐项守卫** | 安全（正例） |
| `features.py:243-256` | `if len(x)` 逐项 | ✅ 有 | 安全 |
| `features.py:249/251/274/275` | `np.argsort(freq[1:])[-6:]` | ⚠️ `freq` 恒为 34 维，空 draws 也是 0 数组 → argsort 正常 | 输出无意义但**不崩** |
| `shape_rules.py:68-73` `_stats_of` | `np.percentile(sums)` | ❌ 无守卫（但仅内部对抽样数组调用） | 中 |
| `shape_rules.py:196-204` | `if len(arr) < 100:` 回退理论带 | ✅ **正例** | 安全 |
| `shape_rules.py:219` | `np.percentile(consec,99) if len(consec) else 2` | ✅ 有 | 安全 |
| `shape_rules.py:222/227` | `np.percentile(sums/spans)` | ✅ 被 :197 的 100 期门槛保护 | 安全 |

**结论**：**真正未设防的只有 `engine.constraint_ctx`（`engine.py:41-42`）这一处会崩**，其余要么已守卫、要么只产出无意义值。这说明"功能级降级"的改造面**很小、很便宜、收益最高**：① 修 `constraint_ctx` 的前置条件；② 把 `shape_rules` 的 `len(arr) < 100` 兜底口径与 `patterns_runtime.MIN_DRAWS_FOR_SNAPSHOT=500` 统一成一处 `MIN_DRAWS` 常量 + 一个 `InsufficientDataError`；③ 修 test guard 时序。三项合计改动 < 50 行，可阻止同类崩溃复发。

### 3.5 失陷暴露面（与崩溃隔离同源：边界缺失）

- `docker-compose.yml:21` `LOTT_TOKEN=${LOTT_TOKEN:-}`，而 `.env` **未设** `LOTT_TOKEN` ⇒ `config.API_TOKEN = None`（`config.py:20`）⇒ 中间件 `api_app.py:34-45` 在 `config.API_TOKEN` 为空时**直接 `return await call_next(request)` 放行** ⇒ **80 个路由全部匿名可访问**（含 `/api/refresh`、`/api/evolution/run`、`/api/rules/closed-loop`、`/api/llm_lab/run` 等会触发 LLM 计费与重计算的路由）。
- 生产库实测：`predictions`=40、`eval_details`=35；`llm_audit`/`replay_results`/`mining_runs`/`llm_eval_results`/`reconcile_runs` **全 0 行** ⇒ 大量端点当前是"空表上的即时响应"，**性能风险尚未暴露**，一旦启用就会同时撞上线程池与内存两条路径。
- **架构含义**：这和"崩溃隔离"是同一个病根——**没有边界**。认证边界缺失 + 线程池边界缺失 + 内存边界缺失，三者在单进程里叠加。补认证是**零代码风险**（只改 `.env` + compose），应与 P0 同批做。

---

## 4. 模块边界建议（数据面 / 规律挖掘 / 规律验证 / 规律使用 / AI 网关 / 调度 / 视图）+ 接口契约草案

目标不是物理拆进程，而是**画清"谁算慢的、谁读快的、谁写 DB、谁调 LLM"**，让隔离措施有落点。契约以**函数签名级**给出（不是真代码），标注调用方向，避免循环依赖。

### 4.1 七层划分与职责

```
┌─────────────┐    ┌──────────────────────────┐
│ 7.视图/HTTP │    │   api_app.py（瘦成路由）  │  ← 现在 3381 行，须瘦
└──────┬──────┘    └───────────┬──────────────┘
       │                       │ 只读
┌──────▼──────┐    ┌───────────▼──────────────┐
│ 1.数据面(db)│◄───│ 6.调度(scheduler_step)    │  ← 每期跑慢的重算
└──────┬──────┘    └──────────────────────────┘
       │ 共享唯一写源（WAL + busy_timeout）
       ▲
┌──────┴──────┐
│ 2.规律挖掘   │ mining / discovery / multiscale   （慢：扫描 5s~数分钟）
├─────────────┤
│ 3.规律验证   │ hypothesis / rule_lifecycle / battle / backtest / evaluate （慢：回测/进化 分钟级）
├─────────────┤
│ 4.规律使用   │ patterns_runtime / engine.predict_next / ml_model （快读：出号 <1ms 到秒级，除 LLM）
├─────────────┤
│ 5.AI 网关    │ llm_client / llm_lab / ai_audit / llm_eval （慢且不可控：外部 LLM）
└─────────────┘
```

### 4.2 接口契约草案（函数签名级）

每条给"方向 = 谁调谁"，防止逆向依赖：

```
# --- 层 1 数据面（唯一写 DB 的地方；其余层只读/经它写）---
# 现状 db.py 已基本符合，只需补 WAL/busy_timeout。
def db.get_conn() -> sqlite3.Connection            # 加 WAL + busy_timeout
def db.upsert_draws(draws) -> int                  # 唯一开奖写入口
def db.write_prediction(issue, seq, rec) -> None   # 唯一预测写入口
def db.load_predictions(issue) -> List[dict]       # 只读
def db.transaction(fn) -> T                        # 新增：带 busy_timeout 的写事务包装

# --- 层 6 调度（唯一"慢重算"触发器；已有 _scheduler_step）---
# 现状在 api_app.py，应迁出成 scheduler 模块。
def scheduler.step(draws, now) -> dict             # 每期：fetch→verify→mine→validate→refresh→predict
def scheduler._schedule_predict(draws) -> dict     # 只负责触发 engine.predict_next
# 关键：调度调用的慢函数全部应经 deadline 钳制。

# --- 层 2 规律挖掘 ---
def mining.run_mining(draws, min_start, engine) -> dict      # 慢
def discovery.asof_scan(draws, ctx) -> list[dict]            # 慢 ~5s
def multiscale.scale_scan(draws, ...) -> list[dict]          # 慢

# --- 层 3 规律验证 ---
def hypothesis.evaluate_prereg(issue=None) -> dict           # 慢，唯一写者
def hypothesis.resolve_registry(results) -> dict
def rule_lifecycle.run_cycle(draws, use_llm=False, persist=True) -> dict  # 慢
def battle.replay( ... ) -> dict                             # 慢
def backtest.run( ... ) -> dict                              # 慢

# --- 层 4 规律使用（出号）---
def patterns_runtime.refresh(draws) -> dict        # 慢，写 data/patterns_runtime.json
def patterns_runtime.load_snapshot(issue) -> dict  # 快 <1ms，只读；engine 每请求用
def engine.predict_next(draws, use_llm, n_tickets, llm_required, llm_deadline, ...) -> dict
def ml_model.get_ml_models(draws) -> Optional[dict]  # 快，读缓存；首次训练在后台

# --- 层 5 AI 网关（唯一对外 HTTP；其余层都不得直连 LLM）---
def llm_client.chat_json(system, user, max_tokens, temperature, model_cfg, deadline, strict) -> Optional[dict]
def llm_client.chat( ..., deadline=None, strict=False ) -> Optional[str]
def llm_client.cost_report(...) -> dict
def llm_lab.run_closed_loop(draws, rounds, use_llm, progress) -> dict
def ai_audit.run_audit( ... ) -> dict

# --- 层 7 视图/HTTP（api_app.py 瘦身目标）---
# 只做：参数解析 → 调用上面各层 → JSON 组装。不做业务计算。
# 快读端点（纯 JSON 读，无 LLM/回测）单独标为"安全路径"，见 §4.3。
```

### 4.3 "安全路径"白名单（必须保证不饿死）

下列端点**必须**在任何隔离下都即时响应（秒级），它们只读 `db`/`patterns_runtime` 快照，不得触发慢重算：`/api/health`（`api_app.py:67`）、`/api/info`（`:91`）、`/api/draws/latest`（`:126`）、`/api/predictions/last`（`从缓存读，`:392`）、`/api/patterns`（`:157`）、`/api/methods/status`（`:890`）、`/api/rules/runtime`（`:2748`）、`/api/regime/overview`（`:2422`）。

**改造手段**：这些端点**不走公共线程池**，或给它们预留 worker 配额（见 §5 方案 2）。

---

## 5. 隔离机制选型比较（结合单节点 + 768MiB 限额）

| 方案 | 隔离粒度 | 抗传染性 | 内存成本 | 工程成本 | 本机(单节点 768MiB/4核)可行性 |
|---|---|---|---|---|---|
| **① 进程内 try/except + 超时（现状强化）** | 逻辑 | 中（只防异常，不防线程池/内存/OOM） | 0 | 低 | ✅ 现有大量基础，先做 |
| **② 进程内线程池分层 + 独立探活线程** | 时间 | 高（防线程池饥饿） | ~0 | 中 | ✅ **推荐**：给健康探活/安全路径独立小线程池或独立 uvicorn worker |
| **③ 多进程（multiprocessing/独立 uvicorn worker）** | 进程 | 高（真隔离，OOM 只杀子进程） | **高**（每个进程 +200~300MiB 加载 numpy/ML） | 高 | ⚠️ **768MiB 放不下 2 个完整进程**，除非 `--workers` 不加载 ML 或砍半；宿主机内存够但容器限额不允许 |
| **④ 独立容器 / sidecar** | 进程+网络 | 最高 | 极高（又一个 python 镜像 ~200MiB+） | 很高 | ❌ **不可行**：单节点、内存 768MiB、同机 9 个容器已挤，且全站目标"防崩溃"不需要网络隔离 |
| **⑤ 系统级（systemd/健康看门狗自愈）** | 进程 | 中（只事后重启） | 0 | 低 | ✅ 已具备：`restart: unless-stopped`，应配合"健康探活不误杀" |

**结论（结合 §1.8 现实）**：
- **推荐 = ① + ② + ⑤ 组合**：进程内强化超时/降级（①，低成本先做），加**线程池分层/独立探活**（②，核心防线程池饥饿），保底 `restart` 自愈（⑤，已有）。
- **明确不推荐 ③④（多进程/独立容器）**：在 768MiB 单容器内，**每多一个 Python 进程 = +200~300MiB 峰值**，当前余量 376MiB 只够约 1 个轻量进程，且"sidecar"的网络隔离对"防 LLM 挂、防线程池饿"毫无帮助，纯增成本。
- **唯一的例外**：如果**必须要内存级隔离**（某个模块确定会 OOM），才考虑"把 `engine.predict_next` 的 LLM 段移到一个 `--workers` 减载的子 uvicorn worker"——但这要把容器限额定到 ≥1.5GiB，与本机"单节点"现实冲突，需 Lead 决策。

---

## 6. 分阶段迁移顺序（每阶段可回滚）

每个阶段**不拆进程、只加隔离**，可回滚 = 回滚点就是"改配置/env/加装饰器"，不动数据。

| 阶段 | 动作 | 可回滚点 | 验证 |
|---|---|---|---|
| **P0（零风险，纯配置）** | ① `db.py:163` `get_conn` 加 `PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000`；② compose 给 `ssq-predictor` 加 `nano_cpus`（如 3 核）限 CPU，防单容器吃满宿主机饿死同机其它容器；③ **`.env` 设 `LOTT_TOKEN`**（当前为空 ⇒ 80 路由匿名，见 §3.5） | 改回原 PRAGMA / 去掉 `nano_cpus` / 清空 token | `pytest` 全量绿；并发写压测无 `database is locked`；未带 token 的 `/api/*` 返回 401 |
| **P0.5（零风险，< 50 行）** | 修 `engine.constraint_ctx`（`engine.py:24-46`）前置条件：`len(draws) < MIN_DRAWS` 时返回理论兜底值或抛 `InsufficientDataError`；统一 `MIN_DRAWS=500` 常量；修 `tests/test_v12_shape_lock.py:63-75` 守卫为执行期求值 | 单文件 revert | 该 failed 用例转绿，实跑 1037 passed |
| **P1（低风险，装饰器级）** | 给长耗时同步端点加统一 `deadline`/超时装饰器（`predict`/`evolution_run`/`llm_lab_run`/`rules_closed_loop` 已各自有 try，缺统一上限）；给安全路径端点打标记 | 摘装饰器 | 压测：并发打 10 个慢端点，`/api/health` 仍在 200ms 内 |
| **P2（中风险，线程池分层）** | 安全路径/健康探活改用独立小线程池（或独立 uvicorn worker 端口只暴露 `/api/health`+读端点）；慢端点限并发信号量（Semaphore） | 切回公共线程池 | 压测：health 在慢端点轰炸下 P99<500ms |
| **P3（中高风险，缓存收敛）** | 无锁全局缓存（`_ML_DIAG_CACHE`/`_CACHE`/`_WD_CACHE`/`_DISCOVER_CACHE`）加锁或改为"版本化重建"；`_ALL_RED_MASKS` 构建加 `lru_cache`+进程锁 | 逐项回滚 | 全量 pytest + 并发访问竞态测试 |
| **P4（结构，仅当确需）** | 把 `_scheduler_step`/`_evolution_step` 从 api_app.py 迁到独立模块（**这一步才碰"拆文件"，放最后**） | git revert | 全量 pytest |

**为什么把"拆文件"放最后**：因为拆文件是 P0-P3 的**前提**（不拆也能加 WAL/超时/线程池），而拆文件本身要解循环依赖（§1.3），风险高收益低，应最后做、且仅在可读性需要时做。

---

## 7. "微服务改造" vs "模块化 + 故障隔离"成本差异（用户真正要什么）

| | 微服务改造 | 模块化 + 故障隔离（用户真正要的） |
|---|---|---|
| 目标 | 独立部署、独立扩缩、独立发布 | "一个崩溃不拖死全站" |
| 手段 | 拆服务、加网关、跨服务调用、分布式事务 | 进程内超时/降级/线程池分层/内存配额/自愈 |
| 代码改动 | 大（每模块独立 `__main__`、API、鉴权） | 小（装饰器、配置、PRAGMA） |
| 数据 | 需选跨服务存储（本系统是单文件 SQLite，**拆服务=重写数据层**） | 仍单 `ssq.db` |
| 内存 | 每服务 +200~300MiB | +0 |
| 运维 | 编排、服务发现 | 一个容器 + restart |
| 单节点 768MiB | **不可行** | **完全可行** |

**结论**：用户要的是"模块化 + 故障隔离"，不是微服务。**最小可行做法（MVT）= P0（WAL/busy_timeout + CPU 限额）+ P1（统一超时/降级）+ P5（已具备的 restart 自愈）**，三件做完，"一个模块崩溃不拖死全站"在进程内就成立。

---

## 8. 哪些地方**不需要**模块化（防过度设计）

1. **`rigor.py`（1353 行）**：纯理论分布、确定性纯函数，无 IO、无全局可变（只有 `_SUM_CACHE`/`_CONSECUTIVE_CACHE` 数值缓存，线程安全风险极低）。**不需要动**。
2. **`features.py`/`parser.py`/`data_check.py`/`data_fetcher.py`**：纯数据转换/抓取，无崩溃传染面。**不需要动**。
3. **`notify.py`（86 行）**：通知外发，慢时最多阻塞调用方，不传染全站。**不需要动**。
4. **`models.py`/`l2_service.py`（247 行）**：轻量构造。**不需要动**。
5. **`config.py` 的常量**：`STARTED_AT` 等是只读启动常量，**不需要动**。
6. **`__init__.py`/`migrations.py`/`cli.py`**：入口/迁移，一次性，**不需要动**。
7. **`data_audit.py`/`selfcheck.py`/`diagnose.py`**：审计/自检，慢但经 `_bg_run` 走后台线程，**已隔离，不需要动**。

**总原则**：**只对"共享资源（_CONN/线程池/内存/LLM）"的传染点做隔离**，不对"纯计算/纯 IO"模块做结构性拆分。拆 `engine.py`/`api_app.py`/`hypothesis.py` 三个大文件**仅为可读性**，不做。

---

## 9. 结论摘要（给 Lead）

**具备改造条件**。方向应是**进程内隔离（超时/降级/线程池分层/内存配额/自愈）**，而非"拆文件"或"微服务"。最关键 3 条风险：**① 健康探活与慢端点共享同一线程池（80 个路由中 79 个同步 `def`），长耗时 LLM/回测端点可饿死 `/api/health` 触发容器 restart；② 全局单连接 SQLite（`db.py:160`）无 WAL/无 busy_timeout，写锁跨模块传染 + 已踩过导入期线程段错误（commit 727054b）；③ 768MiB 内存限额（当前已用 51%）下无内存级隔离，`_ALL_RED_MASKS`(8.9MB)/无锁无上限缓存/coverage 枚举可 OOM 全站**。**本轮新发现的活标本**：`engine.constraint_ctx` 对空输入零防御（`engine.py:41` `IndexError`）+ test guard 在收集期缓存时序答案（`tests/test_v12_shape_lock.py:63-75`），证明"隔离边界必须落在数据类型级契约、不能靠调用时序巧合"。

**建议第一步**：只做零风险 P0——`db.py` `get_conn` 加 `WAL + busy_timeout`，compose 加 `nano_cpus` 限 CPU，**并在 `.env` 设置 `LOTT_TOKEN` 补上认证边界**；随后立刻做 §3.4 的「空输入前置条件」修复（< 50 行，成本最低、直接消灭本轮 failed 用例的同类风险）。验证口径统一为 **`pytest --collect-only` = 1057 项 / 实跑 1036 passed（1 known failed 修复后应为 1037 passed）** + 并发写压测无 `database is locked`。