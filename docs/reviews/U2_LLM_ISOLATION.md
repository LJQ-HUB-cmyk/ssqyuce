# U2 评审报告：LLM 配置按用户隔离架构设计

> **评审对象**：`ssqyuce`（Python 3.11/3.13 + FastAPI + SQLite，双色球预测系统）  
> **文档定位**：多用户改造之 LLM 配置隔离、密钥安全、预算与配额治理规范（只读评审，零业务代码改动）  
> **文档路径**：`docs/reviews/U2_LLM_ISOLATION.md`  
> **协作约定**：表名 `user_llm_config`，主键/外键严格对齐 U1 规范 `user_id INTEGER PRIMARY KEY`，系统主体哨兵 `SYSTEM_USER_ID = 0`，配合 U3 数据模型共享/私有边界划分。

---

## 0. 核心结论与设计决策摘要

1. **核心矛盾与破解**：现系统所有 LLM 配置、预算记忆和出号通道全部写死为**全进程静态单例**（`config.LLM_CONFIG_FILE`、`config.LLM_BUDGET_FILE`、`_budget_cache`），多用户并发访问必然直接串号、密钥裸露、相互覆盖。通过引入 **数据库表 `user_llm_config` + `contextvars.ContextVar` 请求上下文 + 内部线程池上下文浅拷贝**，实现 **0 处端点函数签名改动** 即可将用户专属配置安全透传至底层 `llm_client`。
2. **产品形态决策（混合层级模式）**：
   - 彻底拒绝"强制所有普通用户自备 API Key"（门槛极高、体验极差）；
   - 拒绝"无条件完全共享管理员 Key"（单用户恶意刷号将耗尽全站预算）；
   - **明确推荐：混合层级覆盖模型（Tiered Fallback Model）**。普通用户注册后默认使用系统公共通道并受严格的每日配额/频次限制；高级用户可在设置页填入自备 API Key，勾选后系统标记 `use_custom_key = 1`，走专属通道并豁免公共日配额。
3. **密钥安全与依赖选型**：
   - **强烈主张引入 `cryptography`（Fernet 对称加密，AES-128-CBC + HMAC-SHA256）**。针对 Lead 提出的零依赖方案，我们深入论证了"绝不自研流加密"的安全底线；`python:3.11-slim` 容器中安装 `cryptography` 有官方 pre-built manylinux wheel（约 3.5MB），**完全不需安装 gcc/rustc**，构建耗时 < 3 秒，镜像增量仅 ~10MB。
   - 主密钥由环境变量 `LOTT_SECRET_KEY`（或数据卷中受保护的 `data/.secret_key`，权限 600）注入；
   - API 响应严格执行 `sk-****abcd` 脱敏规则，绝不在任何响应、日志、异常和报错中泄露明文 Key。
4. **配额治理定位（防滥用而非省钱）**：
   - Lead 实测成本外推：10 用户 × 每期 2 次 ≈ 4.42 元/周 ≈ 230 元/年。**成本本身不是瓶颈，配额设计定位于防滥用/防 DoS 攻击**。
   - **失败请求计入规则**：Lead 实测当前失败率达 27%（ok=false 10/37 次）。因此，网络超时/上游 5xx 等**系统故障不扣除用户额度**（避免让用户为系统不稳定性买单），仅因用户参数非法/Prompt 违规触发的 400 错误计入频次限流。
5. **健壮性底线（未配置 LLM 照常出号）**：
   - 未配置 LLM 或配额超限的用户，调用预测接口直接安全降级至纯统计模型（频率/冷热/遗漏 + ML），出号功能 100% 正常。

---

## 1. 现状盘点与硬事实审计

当前系统最初设计为单机单用户本地工具，所有配置、预算记忆与运行时状态均采用**进程级全局单例**或**全局静态文件**管理。在引入多用户（如 10+ 用户并发或分权使用）场景下，存在直接串号、密钥全站裸露、配置相互踩踏的结构性风险。

### 1.1 全进程共享单例现状（硬事实定位）

1. **全局配置文件单例（全进程唯一）**
   - **代码位置**：`lottery/config.py:480`
   - **事实代码**：
     ```python
     LLM_CONFIG_FILE = DATA_DIR / "llm_config.json"
     ```
   - **审计分析**：该路径为模块级常量。系统启动时调用 `load_runtime_llm_config()`（`config.py:484`）直接修改进程全局变量 `LLM_DISABLED`、`LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`、`LLM_MODEL_LIST` 等。任何用户写入该文件，都会直接污染全进程所有协程与线程的运行态。

2. **全局预算记忆文件与共享缓存（必然串号）**
   - **代码位置**：`lottery/config.py:104`，`lottery/llm_client.py:344-408`
   - **事实代码**：
     ```python
     # config.py:104
     LLM_BUDGET_FILE = DATA_DIR / "llm_budget.json"

     # llm_client.py:344-358
     _budget_lock = threading.RLock()
     _budget_cache: Optional[Dict[str, int]] = None

     def _budget_file():
         return config.DATA_DIR / "llm_budget.json"

     def _budget_key(url: str, model: str) -> str:
         return f"{url}|{model}"
     ```
   - **审计分析**：`_budget_cache` 仅以 `f"{url}|{model}"` 为键。当不同用户配置了相同服务商模型但配额不同，或者用户 A 遭遇输出截断将 max_tokens 抬升至 32000 时，用户 B 调用同模型会自动被 `learned_max_tokens`（`llm_client.py:527`）强行拉升到大预算，导致超时阈值被拉大至 180s（`config.LLM_LONG_TIMEOUT`），并可能迅速耗尽配额。

3. **API 接口直接修改进程全局变量与环境配置**
   - **代码位置**：`lottery/api_app.py:751-826`
   - **事实代码**：
     ```python
     @app.post("/api/config/llm")
     def update_llm_config(payload: dict):
         from . import config
         import os
         if "base_url" in payload:
             config.LLM_BASE_URL = (payload.get("base_url") or "").strip().rstrip("/") or None
             os.environ["LOTT_LLM_BASE_URL"] = config.LLM_BASE_URL or ""
         if "api_key" in payload:
             config.LLM_API_KEY = (payload.get("api_key") or "").strip() or None
             os.environ["LOTT_LLM_API_KEY"] = config.LLM_API_KEY or ""
         ...
         config.LLM_CONFIG_FILE.write_text(json.dumps(conf, ...))
     ```
   - **审计分析**：前台「设置」页（`web/static/app.js:3213`）通过 POST `/api/config/llm` 提交的数据会直接覆盖操作系统的 `os.environ` 和 `config` 模块变量。用户 A 填写自己的 API Key，全站所有用户（包括定时调度器）即刻开始消耗用户 A 的 Key；用户 A 停用 LLM，用户 B 的预测也直接报错中断。

4. **全局可用性判定**
   - **代码位置**：`lottery/config.py:379-386`
   - **事实代码**：
     ```python
     def llm_configured() -> bool:
         if LLM_DISABLED:
             return False
         if LLM_BASE_URL and LLM_API_KEY and LLM_MODEL_LIST:
             return True
         return bool(LLM_EXTRA_MODELS)
     ```
   - **审计分析**：该判定未绑定任何调用主体。在 `api_app.py:587`、`engine.py:2329`、`rule_lifecycle.py:1057` 中均被无条件调用，导致业务逻辑无法针对特定用户判断其是否具备可用的 LLM 通道。

### 1.2 出号与调用链路现状

- **核心预测端点**：`lottery/api_app.py:257` `def predict(...)` 为同步路由函数，接收 `use_llm` 等查询参数。
- **线程池宿主**：系统共 80 个端点，其中 79 个是同步 `def`，全部运行在 Starlette / AnyIO 默认维护的线程池（40 workers）中。
- **调用树分支**：
  `api_app.predict` (工作线程)  
  └── `engine.predict_next`  
      └── `engine._llm_channel` (`engine.py:785`)  
          ├── `obs = llm_client.chat_json(...)` (当前线程)  
          ├── `ThreadPoolExecutor.submit(_ticket_call, ...)` (`engine.py:881`，**派生子线程池！**)  
          │    └── `llm_client.chat_json(...)`  
          └── `critique = llm_client.chat_json(...)` (当前线程)

---

## 2. 核心技术难点攻关：零侵入式上下文透传

### 2.1 难点挑战定义
多用户改造面临的核心工程约束：  
**在不逐个修改 79 个同步端点函数签名（以及 `engine.predict_next` 等深层嵌套工具函数入参）的前提下，如何将经过鉴权的当前用户身份及专属配置安全、可靠地下沉至最底层的 `llm_client`？**

### 2.2 `contextvars.ContextVar` 在 FastAPI 架构中的传播实证

针对 `ContextVar` 能否跨越 FastAPI 线程池边界的问题，我们深入审计了运行环境（Python 3.13 + AnyIO 4.x + Starlette）的源码实现：

#### (1) 正向传播机制（HTTP 异步循环 → 路由工作线程）
- **Starlette 调度入口**（`starlette.routing.run_in_threadpool`）：
  ```python
  async def run_in_threadpool(func: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
      func = functools.partial(func, *args, **kwargs)
      return await anyio.to_thread.run_sync(func)
  ```
- **AnyIO 线程池实现**（`anyio._backends._asyncio.AsyncIOBackend.run_sync_in_worker_thread`）：
  ```python
  # 关键源码实证：
  context = copy_context()
  context.run(set_current_async_library, None)
  worker.queue.put_nowait((context, func, args, future, worker_scope))
  ```
- **AnyIO Worker 消费**（`anyio._backends._asyncio.WorkerThread.run`）：
  ```python
  context, func, args, future, cancel_scope = item
  ...
  result = context.run(func, *args)
  ```
- **实证结论**：FastAPI 的 ASGI 异步事件循环在将同步端点任务委派给工作线程时，**显式调用了 `copy_context()` 并在工作线程中以 `context.run(...)` 运行**。因此，由 FastAPI 鉴权中间件或路由依赖设置的 `ContextVar`，能够 **100% 自动且安全地浅拷贝进同步端点所在的线程中**，无需任何额外包装。

#### (2) 致命陷阱：同步端点内部的二次线程池
虽然 AnyIO 保证了主工作线程的上下文继承，但我们在审计出号链路时发现了**致命断点**：
- **代码位置**：`lottery/engine.py:881`
  ```python
  # engine._llm_channel 内部并发选号
  with ThreadPoolExecutor(max_workers=min(len(calls), 4)) as ex:
      futures = [ex.submit(_ticket_call, cfg) for cfg in calls]
  ```
- **Python 标准库行为实测**（Python 3.13 验证）：
  在 Python 标准库中，`threading.Thread` 与 `concurrent.futures.ThreadPoolExecutor` 创建新工作线程时，**默认不传递也不复制上下文变量（值为 `None`）**。
- **后果**：如果不做处理，`engine._llm_channel` 中的选号子线程在调用 `llm_client.chat_json` 时，通过 `ContextVar` 读取到的用户上下文将直接退化为 `None`，导致采样轮无法读取用户专用配置！

#### (3) 陷阱 2：后台长任务与定时调度线程
- `api_app.py:604, 2848`（`threading.Thread(target=_work, daemon=True).start()` 规律闭环与异步任务）
- `api_app.py:3382`（`_scheduler_loop` 自动开奖调度线程）
- 这些任务脱离了 HTTP 请求生命周期，原生不存在 Request 上下文。

### 2.3 零侵入透传方案选型与推荐架构

#### 隔离模型对比矩阵

| 评估维度 | 方案 A：每用户独立文件 (`data/users/{uid}/llm_config.json`) | 方案 B：DB 关系表 `user_llm_config` (**明确推荐**) | 方案 C：全链路函数参数显式透传 |
| :--- | :--- | :--- | :--- |
| **数据一致性** | 差。磁盘多文件，崩溃可能产生半写入、空文件 | **极佳**。SQLite 事务（ACID），与用户表外键级联 | 良好。内存传递，但无持久化保证 |
| **备份与迁移** | 差。分散在子目录，现有 `migrations.py` 难以统一快照 | **极佳**。纳入 `db.sqlite3`，单文件热备/回滚天然一致 | 需配合 A 或 B |
| **安全风险** | **高危**。易遭受路径遍历注入（`../` 逃逸）、目录权限漂移 | **极高安全**。参数化 SQL，无路径注入风险 | 安全，但开发容易漏传 |
| **工程改动量** | 中等。需重写文件读写逻辑 | **低**。仅改造配置存取层与 llm_client 读取点 | **极高**。需重构 79 个端点与十余个内部模块签名 |
| **并发性能** | 差。大量小文件 open/read/write，无连接池与缓存 | **良好**。内存配合 LRU 缓存，写时行级更新 | 良好 |

#### 明确推荐：方案 B（DB 表持久化）+ ContextVar 请求上下文 + 线程池穿透包装器

**落地实施架构（三道保障）**：

1. **定义请求级用户上下文**（`lottery/context.py`）：
   ```python
   # lottery/context.py
   import contextvars
   from typing import Optional, Dict, Any

   # 核心设计：默认值为 None（绝对禁止默认指向 admin，防未鉴权绕过）
   current_user_ctx: contextvars.ContextVar[Optional[Dict[str, Any]]] = \
       contextvars.ContextVar("current_user_ctx", default=None)

   def get_current_user_id() -> Optional[int]:
       ctx = current_user_ctx.get()
       return ctx.get("user_id") if ctx else None

   def get_current_user_config() -> Optional[Dict[str, Any]]:
       ctx = current_user_ctx.get()
       return ctx.get("llm_config") if ctx else None
   ```

2. **FastAPI 中间件统一生命周期管理**：
   在请求进入时解析认证凭证（Token / Session），从 DB 加载（或从缓存取）用户及其 LLM 配置，写入 `current_user_ctx.set(...)`。请求结束后通过 token 自动重置。
   > ⚠️ **鉴权旁路与安全默认值（Lead 提醒）**：针对容器 18000 端口直绑公网（绕过 nginx），中间件在无法解析用户或匿名访问时，`current_user_ctx` **严格保持为 `None`**。绝不允许降级到 `admin` 或任何共享凭证。如果匿名请求调用预测端点且未提供可用配置，严格降级为纯统计出号，绝不触发 LLM。

3. **解决 `ThreadPoolExecutor` 丢失上下文的防线（双保险）**：
   - **防线 A（上下文穿透执行器）**：在 `engine.py:881` 使用标准库 `contextvars.copy_context().run` 包装（仅修改 2 行代码即可修复）：
     ```python
     ctx = contextvars.copy_context()
     futures = [ex.submit(ctx.run, _ticket_call, cfg) for cfg in calls]
     ```
   - **防线 B（显式参数优先兜底）**：在 `llm_client.chat` 与 `engine._llm_channel` 中允许接收可选的 `user_cfg: Optional[Dict] = None`。若调用方显式传参则用显式参，未显式传则自动回退读取 `get_current_user_config()`。

4. **后台任务与调度器保障**：
   定时任务调度器（`_scheduler_loop`）统一显式以系统主体身份（`SYSTEM_USER_ID = 0`）设置上下文或直接调用专用配置，避免上下文为空触发断言。

---

## 3. 协作契约、产品决策与数据模型设计

### 3.1 协作约定对齐（U1 / U2 / U3 接口基线）

依据 Lead 冻结的多模块协作约定：
- 用户表基础字段：`user_id INTEGER PRIMARY KEY AUTOINCREMENT`、`username TEXT UNIQUE NOT NULL`、`role TEXT CHECK(role IN ('admin','user'))`。
- 本模块表命名：`user_llm_config(user_id INTEGER PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE, ...)`。
- 系统主体哨兵：`SYSTEM_USER_ID = 0`（调度器任务归属）。
- 物理外键：不建物理 `FOREIGN KEY`（SQLite 默认未开启外键约束，以逻辑事务维护级联删除），字段类型严格保持 `INTEGER`。

### 3.2 产品形态决策：自备 Key vs 共享 Key vs 混合层级模式

针对 Lead 提出的核心产品问题（多用户下用户是自备 Key 还是共用管理员 Key），我们从业务现实、成本模型与用户体验三个维度进行深度权衡：

#### (1) 现实数据与成本外推
- **Lead 实测成本**：37 次调用共 2.725 元，单次均值 0.0736 元。
- **10 用户成本推演**：10 用户 × 每周 3 期 × 2 次预测 ≈ 60 次/周 ≈ 4.42 元/周 ≈ **230 元/年**。
- **关键定性结论**：**成本本身绝对不是系统的瓶颈**！对于年消耗仅 200~300 元的系统，配额治理的本质是**防滥用（Anti-Abuse）、防并发打爆线程池（DoS 防御），而非省钱**。

#### (2) 模式优缺点对比
1. **纯自备 Key 模式**：
   - 劣势：普通用户根本不知道如何注册 DeepSeek / OpenAI API、充值并获取 API Key，产品使用门槛呈断崖式上升，绝大多数用户将直接放弃使用。
2. **纯共用管理员 Key 模式**：
   - 劣势：若没有严格隔离和惩罚机制，单个恶意或高频用户可以通过脚本频繁调用预测接口，在几分钟内耗尽管理员账户的余额或触发服务商全局限流（429）。
3. **混合层级模式（Tiered Fallback Model）——【明确推荐】**：
   - **基础层（公共共享）**：新注册用户默认使用系统级共享 Key（由环境变量 `LOTT_LLM_*` 或管理员系统配置注入）。该通道施加严格的**每用户日配额**（如每日最多 5 次预测，上限 40,000 tokens）以及全局并发防刷锁。
   - **高级层（自备覆盖）**：高级用户可在前端「设置」页勾选「使用自备 API Key」，填入个人 `base_url`、`api_key`、`model`。一旦配置自备 Key，系统将其标记为 `use_custom_key = 1`，其预测调用**完全走个人通道，并豁免公共日配额**（仅保留每秒并发防爆保护）。

### 3.3 数据模型定义（DDL 与字段语义）

```sql
CREATE TABLE IF NOT EXISTS user_llm_config (
    user_id              INTEGER PRIMARY KEY,
    -- 模式开关：0=使用系统共享配置（默认），1=使用用户自备配置
    use_custom_key       INTEGER NOT NULL DEFAULT 0 CHECK(use_custom_key IN (0, 1)),
    -- 通道开关：0=正常启用，1=该用户主动停用 LLM（强制纯统计出号）
    disabled             INTEGER NOT NULL DEFAULT 0 CHECK(disabled IN (0, 1)),
    -- 自备通道连接参数（use_custom_key=1 时生效）
    base_url             TEXT,
    api_key_encrypted    TEXT,                  -- 密文存储（Fernet / AES-128-CBC）
    api_key_masked       TEXT,                  -- 脱敏展示用（sk-****abcd）
    model                TEXT,                  -- 自定义模型名称
    model_list_json      TEXT NOT NULL DEFAULT '[]', -- 轮转模型列表 JSON 数组
    samples              INTEGER NOT NULL DEFAULT 3 CHECK(samples BETWEEN 1 AND 10),
    extra_body_json      TEXT NOT NULL DEFAULT '{}', -- reasoning_effort / 思考开关等
    -- 用户专属配额与用量（防滥用治理）
    daily_token_quota    INTEGER NOT NULL DEFAULT 50000, -- 日 token 上限（仅对共享模式生效）
    today_tokens_used    INTEGER NOT NULL DEFAULT 0,     -- 今日已消耗 token
    today_calls_used     INTEGER NOT NULL DEFAULT 0,     -- 今日已成功调用次数
    last_reset_date      TEXT NOT NULL DEFAULT '',       -- 每日配额重置基准日 (YYYY-MM-DD)
    -- 并发与速率限制
    qps_limit            INTEGER NOT NULL DEFAULT 2,     -- 用户级每秒请求上限
    -- 审计与时间戳
    updated_at           REAL NOT NULL,
    created_at           REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_user_llm_updated ON user_llm_config(updated_at);
```

### 3.4 迁移路径（对接 `lottery/migrations.py`）

现有数据库通过 `lottery/migrations.py` 维护，当前 `schema_version = 4`。
1. **Schema 递增**：在 `migrations.py` 中增加 batch-5 迁移函数 `_migrate_v4_to_v5(conn)`。
2. **初始化系统默认与历史数据回填**：
   - 自动为 `SYSTEM_USER_ID = 0` 及初始 `admin`（`user_id = 1`）插入默认记录；
   - 若磁盘存在历史 `data/llm_config.json`，在迁移时自动读取其明文配置，进行 Fernet 加密后迁移至 `user_id = 1` 的行记录中；
   - 迁移成功后将 `data/llm_config.json` 重命名为 `data/llm_config.json.migrated` 归档保留。
3. **自动备份保障**：复用 `migrations.backup_before_migrate(conn, 5)`，确保迁移前生成 `.db.bak-v4`，出现任何异常整批回滚。

---

## 4. 密钥安全与防泄露多道防线

### 4.1 依赖现状评估：`cryptography` vs 零新依赖方案

当前 `.venv` 实测表明：`bcrypt`、`argon2`、`cryptography`、`passlib`、`jwt` 等安全库**全未安装**，`requirements.txt` 仅有 6 个依赖。在这一约束下，必须对密钥加密方案做严肃的技术评估与取舍。

#### (1) 零新依赖自研加密方案的可行性与致命缺陷
- **设想方案**：利用 Python 标准库 `hashlib.scrypt` 派生密钥，结合 `hmac` 做认证标签，并手工实现基于流密码（如 XOR 伪随机流）的加密。
- **专业安全审计结论**：**坚决反对在生产代码中自研加密算法（"Don't roll your own crypto"）**。
  - **风险 1：IV / Nonce 重用致命性**。自研流加密一旦出现计数器重复，明文将遭遇异或深度攻击（Two-time Pad Attack），直接导致全量明文被完全还原。
  - **风险 2：无法抵御填充预言机与密文延展攻击（Malleability）**。缺少经过形式化验证的 AEAD（如 AES-GCM 或 Fernet 的 Encrypt-then-MAC）实现，极易出现时序侧信道或密文伪造漏洞。
  - **风险 3：代码可维护性差**。任何后续维护人员都可能无意破坏密码学边界。

#### (2) 引入 `cryptography` 的环境与镜像影响评估（推荐）
- **Dockerfile 现状**：
  ```dockerfile
  FROM python:3.11-slim
  ...
  COPY requirements.txt .
  RUN pip install --no-cache-dir -r requirements.txt
  ```
- **构建开销实测与分析**：
  - PyPI 官方为 Python 3.11/3.13 提供了完善的 `manylinux_2_28_x86_64` 及 `manylinux_2_17_aarch64` 预编译 wheel。
  - Wheel 包内已经静态链接了优化后的 OpenSSL 3.x 动态库与编译完成的 C 扩展模块，文件体积仅 **约 3.5 MB**。
  - 在 `python:3.11-slim` 镜像执行 `pip install cryptography` **完全不需要安装 gcc、rustc、cargo 或 libffi-dev**。
  - **实测影响**：`docker compose build` 构建耗时增加 **< 3 秒**，容器镜像最终体积增量 **< 12 MB**（对于 768MB 内存限额的容器没有任何运行时常驻压力）。
- **结论**：**引入 `cryptography` 是唯一合规且最高性价比的选择**。

#### (3) 极端零依赖环境下的兜底方案（权限隔离替代）
若运维强制要求绝对不新增任何第三方包，**禁止自研加密算法**，唯一可接受的替代是**操作系统文件系统级权限隔离**：
- 数据库文件权限锁定为 `chmod 600 data/ssqyuce.db`，由容器内专有非 root 用户持有；
- 环境变量 `LOTT_SECRET_KEY` 仅在运行时作为盐值混淆，但明确在架构文档中声明该模式为"主机边界防护"而非"静态数据加密（At-Rest Encryption）"。

### 4.2 密钥加密实现规范（Fernet 对称加密）

1. **主密钥（Master Key）来源**：
   - 优先读取环境变量：`LOTT_SECRET_KEY`（32 字节 base64url 编码密钥）；
   - 若未配置且处于首次启动：自动使用 `secrets.token_bytes(32)` 生成高熵密钥，安全持久化至受保护文件 `data/.secret_key`（POSIX 权限锁定为 `0600`），避免容器重启丢失；
   - 严禁将主密钥存入 SQLite 数据库本身。

2. **加解密函数封装（`lottery/crypto.py`）**：
   ```python
   # lottery/crypto.py
   import base64
   import os
   from pathlib import Path
   from typing import Optional
   from cryptography.fernet import Fernet
   from . import config

   _fernet_instance: Optional[Fernet] = None

   def _get_fernet() -> Fernet:
       global _fernet_instance
       if _fernet_instance is not None:
           return _fernet_instance
       raw_key = os.environ.get("LOTT_SECRET_KEY")
       key_file = config.DATA_DIR / ".secret_key"
       if raw_key:
           key_bytes = raw_key.encode("utf-8")
       elif key_file.exists():
           key_bytes = key_file.read_bytes().strip()
       else:
           # 首次自动生成并锁定权限 600
           key_bytes = Fernet.generate_key()
           config.DATA_DIR.mkdir(parents=True, exist_ok=True)
           key_file.write_bytes(key_bytes)
           try:
               os.chmod(str(key_file), 0o600)
           except OSError:
               pass
       _fernet_instance = Fernet(key_bytes)
       return _fernet_instance

   def encrypt_api_key(plain_key: str) -> str:
       if not plain_key:
           return ""
       return _get_fernet().encrypt(plain_key.encode("utf-8")).decode("utf-8")

   def decrypt_api_key(cipher_text: str) -> str:
       if not cipher_text:
           return ""
       return _get_fernet().decrypt(cipher_text.encode("utf-8")).decode("utf-8")

   def mask_api_key(key: str) -> str:
       """脱敏显示规则：sk-****abcd"""
       if not key:
           return ""
       key = key.strip()
       if len(key) <= 8:
           return "******"
       prefix = key[:3] if key.startswith("sk-") else key[:2]
       suffix = key[-4:]
       return f"{prefix}****{suffix}"
   ```

### 4.3 脱敏显示规则与前端防线

- **展示规则**：
  - 前端回显一律采用 `mask_api_key`（例如 `sk-2b89...f9a1` 脱敏为 `sk-****f9a1`）；
  - 查询用户配置接口 `GET /api/user/llm-config` 中，只返回 `api_key_masked` 和 `has_api_key: bool`，**绝对不返回明文与密文字段**；
  - 前端输入框：当 `has_api_key=true` 且用户未修改时，显示脱敏占位符；仅当用户重新输入新 Key 时，才在 `PUT` 请求中上传新明文。

### 4.4 拦截泄露的多道防御纵深

为了确保 API Key 绝不出现在任何日志、异常信息、网络抓包和前端弹窗中，构建四道防御网：

1. **第一道防线：`llm_client` 异常清洗**
   在 `lottery/llm_client.py:411` 的 `LLMChannelError` 抛出前，以及 `requests` 报错捕获处，强制进行正则脱敏：
   ```python
   def sanitize_error_message(msg: str, key_to_hide: Optional[str] = None) -> str:
       if not msg:
           return ""
       # 正则匹配常见 API key 模式 (sk-[a-zA-Z0-9]{20,})
       cleaned = re.sub(r'sk-[a-zA-Z0-9_-]{8,}', 'sk-****[HIDDEN]', msg)
       if key_to_hide and key_to_hide in cleaned:
           cleaned = cleaned.replace(key_to_hide, "******")
       return cleaned
   ```
2. **第二道防线：成本记账日志净化（`llm_cost_log.jsonl`）**
   - 严格继承现有 `llm_client.py:35` 的成本日志纪律：
     > 一行只有 model / tokens / purpose / yuan / ts / ok。不写 base_url、api_key、Authorization 头与请求体。
   - 新增 `user_id` 字段归属，但依然禁止记录任何凭据。
3. **第三道防线：FastAPI 全局未捕获异常过滤器**
   在 `api_app.py` 注册全局异常处理器：
   ```python
   @app.exception_handler(Exception)
   def global_exception_sanitizer(request: Request, exc: Exception):
       # 确保未经清洗的底层 requests 堆栈报错不泄露给客户端
       return JSONResponse({"ok": False, "error": "内部服务异常，详情已记入受控日志"}, status_code=500)
   ```
4. **第四道防线：SQL 查询白名单**
   在 DAO 层查询用户信息时，严禁使用 `SELECT *`，只允许显式声明列白名单，从数据源头杜绝 `api_key_encrypted` 随用户对象扩散。

---

## 5. 预算隔离、配额与限流机制

### 5.1 `_budget_cache`（模型 max_tokens 预算记忆）改造

#### 现存问题与串号机理
- `llm_client.py:345` 中 `_budget_cache: Optional[Dict[str, int]]` 为进程级内存字典；
- 写入文件固定为 `DATA_DIR / "llm_budget.json"`；
- 键名为 `f"{url}|{model}"`。
- **风险暴露**：当用户 A 使用了自定义反向代理或个人模型，其探索出的 32000 预算会直接作用到用户 B 的相同模型名称上，导致用户 B 的单次请求超时阈值被拉伸至 180s，大幅占用线程池资源。

#### 改造方案：分级作用域缓存与锁治理
模型技术特性（如推理模型需要多大 max_tokens 输出）本质上是 `(endpoint, model)` 的属性，而非用户业务配额。但为了实现配置隔离与数据安全：
1. **作用域分键**：
   ```python
   def _budget_key(scope: str, url: str, model: str) -> str:
       # scope: "system" 表示公共通道；"user:{uid}" 表示用户专属通道
       return f"{scope}|{url.rstrip('/')}|{model}"
   ```
2. **多用户并发读写保护**：
   沿用现有的 `_budget_lock = threading.RLock()`，将内存缓存结构升级为分级字典。
3. **数据持久化升级**：
   放弃单点 JSON 文件，将预算记忆迁移至 SQLite `user_llm_budget` 表或作为 JSON 字段存入 `user_llm_config`。当用户在设置页更换模型或 API 地址时，仅清除其个人 `user:{uid}` 缓存，完全不影响全局和其他用户。

### 5.2 配额与限流策略（基于实测数据驱动）

#### (1) 实测数据输入
- **Lead 提供的生产实测数据**：
  - 37 次真实调用，总支出 2.725 元，均值 0.0736 元/次；
  - **失败率高达 27%**（10 次 ok=false，主要为网络瞬态错误、推理模型格式异常、上游超时）。
- **用途分布**：
  - `engine` 出号：2.487 元；
  - `hypothesis` 假说提炼：0.237 元；
  - `probe` 探活：0 元。

#### (2) 关键裁决：失败请求是否计入配额？
针对高达 27% 的失败率，若简单采取"只要发请求就扣配额"，用户正常进行 4 次预测就会因为系统上游抖动而被无辜扣除 1 次配额，导致可用额度系统性缩水 25%，带来极其恶劣的使用体验。

**配额扣减策略裁决**：
1. **区分故障归属（Fault Attribution）**：
   - **系统/网络层故障（不扣额度）**：HTTP 5xx、DNS 故障、连接超时、上游网关 502/504、模型输出 JSON 损坏无法解析。此类调用在 `_bill_finish` 中记入系统真实支出（账单总账实报实销），但**绝对不累加用户的 `today_tokens_used` 与 `today_calls_used`**。
   - **用户侧/参数违规错误（计入频次限流，防恶意探测）**：HTTP 400（参数超长）、401（自备 Key 无效）、恶意高频注入。此类请求不消耗 Token 额度，但**消耗 QPS 桶与每分钟频次配额**，连续失败 5 次触发短时封禁（10 分钟内禁止调用该接口）。
2. **Token 配额扣减时点**：
   仅在收到 LLM 完整且合规的响应，且通过 `parse_usage(data)` 成功提取 `total_tokens` 时，才原子扣减用户的每日可用配额。

#### (3) 配额标准与 429 响应语义
- **默认配额（管理员可下发全局默认值）**：
  - 公共模式用户：每日上限 50,000 tokens（约支持 6~8 次完整预测），单日最高调用 10 次；
  - 自备 Key 用户：豁免每日 Token 配额限制。
- **并发与速率保护**：
  - 单用户限制并发请求数 = 1（避免单个用户通过多标签页同时提交耗尽 40 线程池）；
  - 单用户速率限制 = 2 QPS。
- **超限 429 语义规范**：
  当用户触发限流时，API 严格返回标准 HTTP 429 响应，并带有时效说明：
  ```json
  // HTTP 429 Too Many Requests
  // Headers: Retry-After: 3600
  {
    "ok": false,
    "error": "今日大模型配额已用尽（已消耗 51,200 / 50,000 tokens）。请于明日 00:00 重试，或在设置页配置个人 API Key 以解除配额限制。",
    "retry_after": 3600,
    "quota_type": "daily_tokens",
    "can_fallback_to_stat": true
  }
  ```

### 5.3 降级与安全底线：未配置 LLM 用户 100% 照常出号

本系统核心价值在于双色球预测计算，LLM 仅作为辅助候选通道（V10 架构判定）。**任何由于未配置 LLM、配置错误、配额用尽或大模型宕机引发的异常，都绝不能阻断用户的核心出号流程**。

#### 代码路径确认与降级走读
1. **入口层**（`api_app.py:257-360`）：
   ```python
   # 当用户未勾选 use_llm，或者未配置 LLM 时：
   res = engine.predict_next(draws, use_llm=use_llm, n_tickets=_n_req,
                             llm_required=(use_llm is True), ...)
   ```
2. **引擎调度层**（`engine.py:2326-2348`）：
   ```python
   # 当 llm_required=False 时，即使 LLM 未配置，系统也绝不抛出异常，
   # 而是直接组装统计模型与机器学习模型：
   bl = {name: m for name, m in M.build_models(draws).items()
         if METH.is_enabled(f"stat:{name}", methods_spec)}
   if not bl:
       bl = {"uniform": M.uniform_model()}
   # 正常结合特征与概率分布生成号码
   ```
3. **双通道保障**：
   - 当用户在前端未配置 LLM，或者点击预测时未勾选「使用大模型」，系统自动以纯统计基线（走势/冷热/遗漏/复式覆盖）极速出号（耗时 < 50ms）；
   - 即便用户勾选了「使用大模型」，但若因公共配额耗尽被拦截，前端可提供一键「改用纯统计模式出号」按钮，无缝完成降级出号。

---

## 6. 配置热更新与失效传播机制

在多用户并发系统中，任何配置变更必须做到**即时生效（无需重启服务）**，同时遵循**故障与性能隔离原则**：用户 A 变更个人配置绝不能导致用户 B 的正常请求遭遇缓存击穿、锁竞争卡顿或配置污染。

### 6.1 局部缓存驱逐模型（按 `user_id` 精确失效）

为了兼顾高并发读取性能与内存占用，配置管理层在内存中维护轻量级线程安全缓存：
```python
# lottery/user_config.py
from typing import Dict, Optional
import threading
import time

_config_cache: Dict[int, Dict] = {}       # user_id -> decrypted_config_dict
_cache_lock = threading.Lock()
_CACHE_TTL = 300                          # 5 分钟 TTL 自动保鲜

def get_user_llm_config_cached(user_id: int) -> Dict:
    now = time.time()
    with _cache_lock:
        entry = _config_cache.get(user_id)
        if entry and (now - entry["_cached_at"] < _CACHE_TTL):
            return entry["config"]
    # 穿透至数据库查询（只读单行）
    cfg = _load_user_config_from_db(user_id)
    with _cache_lock:
        _config_cache[user_id] = {"config": cfg, "_cached_at": now}
    return cfg

def invalidate_user_llm_config(user_id: int) -> None:
    """热更新失效入口：仅驱逐指定用户的缓存，零影响其余用户"""
    with _cache_lock:
        _config_cache.pop(user_id, None)
    # 同步清理预算记忆中该用户的条目
    reset_user_budget_cache(user_id)
```

### 6.2 零全局锁设计与并发隔离保证

1. **写操作无阻塞**：
   当用户 A 在前端点击「保存配置」时，系统执行流程为：
   - 参数校验与加密；
   - 开启 SQLite 事务执行单行 `UPDATE user_llm_config WHERE user_id = :uid`；
   - 提交事务后调用 `invalidate_user_llm_config(uid)` 清理用户 A 的内存缓存；
   - **完全不修改任何 `config.py` 中的模块全局变量，不修改 `os.environ`**。
2. **读操作零竞争**：
   用户 B、用户 C 读取配置时，命中各自的缓存条目或独立的 DB 单行读取，不会遭遇全局写锁阻塞，更不会因为用户 A 的配置失效而发生群体性缓存雪崩。

---

## 7. 完整 API 契约与前端设置页改动点

### 7.1 后端 API 规范（契约定义）

所有端点均纳入认证中间件保护（继承 U1 会话机制），通过 `current_user_ctx` 获取操作主体。

#### (1) 获取当前用户 LLM 配置与配额状态
- **路由**：`GET /api/user/llm-config`
- **认证**：已登录用户
- **响应示例**：
  ```json
  {
    "ok": true,
    "user_id": 102,
    "use_custom_key": true,
    "disabled": false,
    "base_url": "https://api.deepseek.com/v1",
    "has_api_key": true,
    "api_key_masked": "sk-****8f2a",
    "model": "deepseek-chat",
    "samples": 3,
    "quota": {
      "is_unlimited": true,
      "daily_token_quota": 50000,
      "today_tokens_used": 12800,
      "today_calls_used": 3,
      "reset_time": "2026-10-20 00:00:00"
    },
    "configured": true
  }
  ```

#### (2) 更新当前用户 LLM 配置
- **路由**：`PUT /api/user/llm-config`
- **认证**：已登录用户
- **请求体（JSON）**：
  ```json
  {
    "use_custom_key": true,
    "disabled": false,
    "base_url": "https://api.deepseek.com/v1",
    "api_key": "sk-real-secret-key-123456",  // 若留空或不传，则保持已有密钥不变
    "model": "deepseek-chat",
    "samples": 3,
    "extra_body": {"reasoning_effort": "low"}
  }
  ```
- **响应**：
  ```json
  {
    "ok": true,
    "configured": true,
    "message": "配置已保存并即时生效"
  }
  ```

#### (3) 测试用户自备 LLM 连接与出文
- **路由**：`POST /api/user/llm-config/test`
- **认证**：已登录用户
- **说明**：使用当前用户专属配置（或请求体中待保存的临时配置）发起结构化探测调用，探测耗时与预算自适应情况。
- **响应**：
  ```json
  {
    "ok": true,
    "model": "deepseek-chat",
    "max_tokens": 4096,
    "time_ms": 1250,
    "reasoning_model": false
  }
  ```

#### (4) 管理员重置指定用户 LLM 配置（对齐 U1 §4.4 管理员接口）
- **路由**：`POST /api/admin/users/{user_id}/llm-reset`
- **认证**：Admin 角色
- **响应**：
  ```json
  {
    "ok": true,
    "target_user_id": 102,
    "message": "该用户 LLM 配置已重置为默认共享模式"
  }
  ```

### 7.2 前端改动明细（`web/index.html` + `web/static/app.js`）

#### (1) `web/index.html` 增量表单升级（`#settingsModal` 区域）
1. **模式切换单选框/勾选卡**：
   - 新增「使用自备 API Key」开关（默认关闭即走平台公共通道）；
   - 开启时展开 `API 地址`、`API Key`、`模型名称` 输入框；
   - 关闭时折叠输入框，并展示「当前正在使用平台共享模型，今日剩余配额：XX,XXX / 50,000 tokens」。
2. **Key 保护输入框强化**：
   - `input type="password" id="cfgApiKey"` 增加 `placeholder="保持现有密钥不变（输入新密钥覆盖）"`；
   - 增加脱敏标签 `<span id="cfgApiKeyMasked" class="badge"></span>` 显示 `sk-****8f2a`。
3. **配额用量卡片**：
   - 增加轻量进度条展示 `today_tokens_used / daily_token_quota`。

#### (2) `web/static/app.js` 逻辑升级
- **`loadLlmConfig()`**（`app.js:3168`）：
  - 改造为请求 `GET /api/user/llm-config`；
  - 根据返回的 `use_custom_key` 状态切换表单禁用/展示模式；
  - 将 `config.api_key_masked` 渲染至界面，并将输入框内容置空。
- **`saveLlmConfig()`**（`app.js:3192`）：
  - 改造为请求 `PUT /api/user/llm-config`；
  - 若用户未修改 Key（输入框为空），则请求体中不传递 `api_key` 字段，服务端保持既有密文不变，彻底杜绝星号被反向保存的经典 Bug。
- **`testLlmConnection()`**（`app.js:3233`）：
  - 路由切换至 `/api/user/llm-config/test`。

---

## 8. 全覆盖测试清单（Verification Matrix）

为保证多用户 LLM 隔离在工程实施中 100% 可行且无回退，定义以下自动化测试套件（纳入 `tests/test_user_llm_isolation.py`）：

| 序号 | 测试用例名称 | 测试目标与前置条件 | 核心验证步骤与断言标准 |
| :--- | :--- | :--- | :--- |
| **T1** | **用户配置强隔离 (A 改配置不影响 B)** | 用户 A（自备 DeepSeek）、用户 B（使用共享通道）同时在线 | 1. 用户 A 调用 `PUT /api/user/llm-config` 将模型改为 `model-A`；<br>2. 验证用户 B 读取配置仍为平台默认模型；<br>3. 用户 A 发起预测，调用抓包确认走 `model-A`；用户 B 预测确认走公共通道；<br>4. 确认无跨用户串号。 |
| **T2** | **Key 防泄露全链路断言 (A 的 Key 不出现在任何响应/日志/异常)** | 用户 A 配置真实 Key `sk-secret-12345678` | 1. 验证 `GET /api/user/llm-config` 返回脱敏串 `sk-****5678`，无明文字段；<br>2. 注入模拟上游 401 报错，捕获 `LLMChannelError`，断言异常消息中不包含完整 key；<br>3. 检查 `llm_cost_log.jsonl`，断言 0 处 key 出现；<br>4. 检查全局 FastAPI 异常响应，断言仅返回标准错误码。 |
| **T3** | **并发预测预算不串号 (Budget Cache Isolation)** | 用户 A 与用户 B 并发触发 `predict` | 1. 用户 A 调用配置了超长 max_tokens 的模型并触发预算自适应至 32000；<br>2. 用户 B 并发调用普通模型；<br>3. 断言用户 B 的单次请求 max_tokens 不被异常拉升，超时时间保持默认 60s；<br>4. 验证 `_budget_cache` 按作用域严格分键。 |
| **T4** | **AnyIO 线程池与内部 ThreadPoolExecutor 穿透** | 用户 A 发起完整出号请求（触发 `engine.py:881` 多模型采样） | 1. 模拟进入同步端点；<br>2. 验证 AnyIO 将 `current_user_ctx` 传入主工作线程；<br>3. 验证 `engine._llm_channel` 内部的 `ThreadPoolExecutor` 子线程中 `current_user_ctx.get()` 仍然能够准确获取用户 A 身份与配置，不为 `None`。 |
| **T5** | **鉴权旁路与未鉴权安全降级** | 模拟公网直接访问 `0.0.0.0:18000`（无 Token / 无 Session） | 1. 验证 `current_user_ctx.get()` 严格返回 `None`；<br>2. 绝对不降级到 `admin` 或任何已存用户的 Key；<br>3. 预测端点在 `use_llm=true` 时返回 401 未登录；在默认模式下自动降级为纯统计出号。 |
| **T6** | **失败率 27% 下网络抖动不扣除配额** | 用户 A 每日配额 50,000 tokens，初始已用 0 | 1. 模拟调用 LLM 遭遇网络超时（HTTP 504）或 JSON 解析失败（ok=false）；<br>2. 验证成本总账 `llm_cost_log.jsonl` 正常记账；<br>3. 验证用户 A 的 `today_tokens_used` 依然为 0，额度未被扣除；<br>4. 连续模拟 3 次成功调用，断言 `today_tokens_used` 准确累加真实 tokens。 |
| **T7** | **未配置 LLM 用户纯统计平滑降级** | 新用户注册，未配置任何 LLM，`use_llm=None` | 1. 调用 `/api/predict`；<br>2. 断言出号成功（返回 5 注号码，`actual_tickets=5`）；<br>3. 断言 `llm_used=False`，耗时 < 100ms；<br>4. 全程无任何异常抛出。 |
| **T8** | **超配额 429 拦截与降级引导** | 用户 A 共享配额已用尽（今日已消耗 55,000 / 50,000 tokens） | 1. 调用 `predict(use_llm=True)` 返回 HTTP 429 与标准重试头；<br>2. 调用 `predict(use_llm=False)` 依然能成功以统计模式出号。 |

---

## 9. 落地风险清单、工作量与协作建议

### 9.1 三个关键权衡（Trade-offs）

1. **依赖权衡：引入 `cryptography`（+1 包） vs 零依赖明文/弱权限**
   - **裁决**：选择引入 `cryptography`。为了避免自研流加密的致命安全缺陷，引入具有官方预编译 wheel 的标准工业级库是极其合算的投资，对 Docker 构建与容器运行环境零冲击。
2. **产品形态权衡：混合层级模式 vs 纯自备 Key**
   - **裁决**：选择混合层级。降低普通用户的认知与使用门槛，保留自备 Key 作为高级通道，既防止公共 Key 被刷爆，又保证开箱即用。
3. **上下文透传权衡：ContextVar + 内部线程池浅拷贝 vs 79 个端点显式修改**
   - **裁决**：选择 ContextVar。将改动范围限制在中间件、上下文工具函数与出号调用点 1 处内部线程池，避免引发全站 79 个端点的大规模接口变动风险。

### 9.2 工作量拆解与人日评估

多用户 LLM 隔离改造预计总工作量为 **3.5 人日**：

| 阶段 | 交付物与任务内容 | 预计人日 | 依赖前置 |
| :--- | :--- | :--- | :--- |
| **Phase 1** | 依赖引入（`cryptography`）、加解密工具模块 `lottery/crypto.py` 与单元测试 | 0.5 人日 | 无 |
| **Phase 2** | `migrations.py` batch-5（`user_llm_config` 表创建与历史迁移）与 DAO 层 | 1.0 人日 | U1 用户表基础 |
| **Phase 3** | `contextvars` 上下文管理、中间件接入、`engine.py` 线程池上下文穿透改造 | 1.0 人日 | U1 鉴权中间件 |
| **Phase 4** | API 契约落地（`/api/user/llm-config` 等 4 个端点）与前端设置页重构 | 0.5 人日 | Phase 2, Phase 3 |
| **Phase 5** | 集成测试、配额限流验证与防泄露渗透验证（T1~T8 用例） | 0.5 人日 | 全部就绪 |

### 9.3 协作与文件排号纪律

为防止多人协作时发生严重的文件代码合并冲突（依据 Lead 规范）：
- **`lottery/config.py`**：本模块仅新增读取函数，不删除既有常量，避免破坏未迁移模块；
- **`lottery/engine.py`**：仅在 `_llm_channel`（`engine.py:881`）进行 2 行级线程池上下文浅拷贝改造，严禁重构出号主逻辑；
- **`lottery/api_app.py`**：新增用户配置端点集中放在 `api_app.py:751` 原配置端点位置替换，避免在文件多处散落修改。


