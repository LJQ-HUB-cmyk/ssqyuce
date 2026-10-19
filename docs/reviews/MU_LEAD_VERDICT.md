# MU 总裁决：多用户化 + LLM 配置隔离 + 管理员增删用户 —— 方案与落地分工

> 日期：2026-10-19（第二轮需求）　｜　代码基准：`ca23283`（v4.1.0 / 2026-10-V12）
> 承接：`V13_LEAD_VERDICT.md`（四点改造总裁决）
> 评估方式：**3 位专职架构设计师**（身份 / LLM 隔离 / 数据模型）并行 + **1 位技术总监**做路线整合 + Lead 独立复算与 **9 条裁决**
> 原文：`U1_IDENTITY.md`(1677行) / `U2_LLM_ISOLATION.md`(716行) / `U3_DATA_MODEL.md`(1680行) / `U4_ROADMAP.md`(502行)
> **全程只读，业务代码零改动。**

---

## 零、一句话总裁决

**多用户化具备改造条件，且比预想的便宜：不需要微服务、不需要换数据库、HTTP 端点函数签名改动为 0、只新增 1 个第三方依赖。**
但它有两个**必须先付的代价**：① 安全前置（端口/文档页/鉴权）不修完，一切鉴权都是纸糊的；② 「每期预测」的语义必须从"全站一份"改为"每用户一份"，这是产品形态决定，不是技术细节。

**总工期：54 人日（50–60），真实并行 2 路，日历 30–36 工作日。**

---

## 一、结论速览

| 项 | 结论 |
|---|---|
| 多用户新增工作量 | **23 人日**（身份 3.5 ＋ LLM 隔离 3.5 ＋ 数据/迁移 13.0 ＋ 前端 2.0 ＋ 收尾 1.0） |
| 与 V13 合并总量 | **54 人日**（31 ＋ 23 − 0.5） |
| 真实并行度 | **2 路**（不是"有两个人"，而是"有两片不相交的文件域"） |
| 日历 | 30–36 工作日（约 6–7.5 周） |
| 新增第三方依赖 | **1 个**：`cryptography`（仅 `requirements.txt` 一行，Dockerfile 不动） |
| HTTP 端点签名改动 | **0 处**（80 个端点） |
| 需显式改写的端点 | **7 处**（登录 2 + 用户管理 5 类） |
| 需修改的测试 | **9 处 lambda** + 36 处 TestClient（用 1 个 fixture 统一解决） |

### 三个关键设计决策（都已裁决，不再开放讨论）

**① 产品形态 = 方案 C：事实层共享 / 决策层按用户 / 账本按用户**

| 层 | 内容 | 归属 |
|---|---|---|
| 事实层（世界的事实） | `draws` 3516 期、`features`、`patterns`、挖掘产出、回测 | **全局共享** |
| 决策层（用户的决策） | `predictions`、`eval_*`、`tasks` | **每用户私有** |
| 账本层（谁花的钱） | `llm_audit`、LLM 预算、`llm_cost_log` | **每用户私有** |

**否掉"全局生成一次共享"（方案 B）的理由不是省钱**（实测 10 用户 ≈ 230 元/年），而是**需求自相矛盾**：B 下用户配的 key 可能在唯一一次全局生成里**一次都没被调用**——那他填 key 有什么意义？这等于交付一个假的隔离功能。附带三处硬伤：付费者/受益者分离、先到者决定后来者、配置热更新两难（推翻=破坏隔离，不推翻=配置无用）。

**② 鉴权 = 服务端 session + deny-by-default 单中间件 + 显式租户传参**
- 密码用**标准库 `hashlib.scrypt`**（零依赖；必须显式传 `maxmem`，否则 `n=2^15` 抛 `ValueError`）
- API key 用 **`cryptography` Fernet 可逆加密**（密钥必须能还原才能调 LLM）
- **两者不是一回事**：密码是单向哈希，key 是可逆加密。不能混用。
- **租户传递用显式参数，不用隐式 contextvar**（详见 §3 裁决 9）

**③ 鉴权前置(P0)：四项不可分割**
`LOTT_TOKEN` 生效 + 端口改 `127.0.0.1:18000:18000` + 关闭 `/docs` `/redoc` `/openapi.json` + 收紧 CORS。
**其中"关闭文档页"最容易被忽略**：`/docs` 不在 `/api/` 前缀下，**天然绕过鉴权中间件**，实测公网 `http://89.106.64.40:18000/docs` 返回 200。

---

## 二、现状：为什么"加个登录框"不够

| 问题 | 实测证据 | 影响 |
|---|---|---|
| 零用户概念 | 仅一把全局 `LOTT_TOKEN`（`api_app.py:34-45`），且 **`.env` 未设** ⇒ fail-open | 80 端点全部匿名可访问 |
| 生产实测匿名可达 | `GET /api/config/llm` → **200**、`/docs` → **200**、`/openapi.json` → **200** | 攻击者能直接读全局 LLM 配置 |
| 端口绕过 HTTPS | `docker-compose.yml:11` `0.0.0.0:18000`，实测 `ss -tlnp` 确认 | 明文直连，绕过 nginx 的一切鉴权 |
| 配置全是进程级单例 | `config.py:480` `LLM_CONFIG_FILE`、`:104` `LLM_BUDGET_FILE`、`llm_client.py:344-408` `_budget_cache` | **一人改配置，全站串号** |
| 预测无用户维度 | `predictions` 主键 `(issue, seq)`，40 行 | "本期推荐"全站唯一 |
| 缓存判据只有期号 | `api_app.py:294-390` `load_predictions(issue)` | **请求参数被静默忽略**；多用户后变跨租户错配 |
| `eval_meta` PK `(issue, method)` | 实测 `method` 内嵌模型名 | 多用户下**必然静默覆盖** |
| 线程池丢上下文 | `engine.py:881` `ThreadPoolExecutor`；`api_app.py:2387`/`:2848` 裸 `threading.Thread` | contextvar 丢失 → **静默落到 SYSTEM/admin 写错数据，测试不会红** |

---

## 三、Lead 的 9 条裁决（跨文档冲突全部关闭）

三位设计师各自独立工作，产出的文档**互相存在真实冲突**。以下 9 条是我逐条复核后的终局裁决，已全部落地到对应文档。

| # | 冲突 / 风险 | 裁决 | 理由（实测） |
|---|---|---|---|
| 1 | U2 写 `ON DELETE CASCADE`，但本库 `PRAGMA foreign_keys = 0` | **不得作为正确性依据**；删除走应用层显式事务；加"不依赖 PRAGMA 的孤儿行测试" | SQLite 该 PRAGMA 每连接生效，换连接池/多进程即静默失效。安全性不能建立在"恰好开着"上 |
| 2 | U3 假设 `admin = user_id 1` 并写 `DEFAULT 1` | **全部作废**。admin 用 `username`+`role` 动态定位；新增列一律 `DEFAULT 0` | AUTOINCREMENT **不复用 ID**，实测删光重插得 `user_id=2`。写死 1 会静默越权 |
| 3 | 系统任务归属：U1 用 `NULL`，U2/U3 用 `0` | 采用 **`SYSTEM_USER_ID = 0`** | `NULL` 在三值逻辑下 `WHERE user_id != ?` 会漏掉系统任务，且无法归因 |
| 4 | U3 原"迁移列全带 `DEFAULT 1`"与裁决 2 冲突 | 改 **`DEFAULT 0`**（= 系统/历史语义），迁移脚本再显式 UPDATE 给 admin | 落到"系统主体"比落到"某个具体人"更诚实 |
| 5 | 限流需真实 IP，但端口可绕过 | **端口绑定修复与鉴权同批上线，不可分割** | nginx 已透传 `X-Real-IP`，但 `18000` 直绑 `0.0.0.0` 可被绕过 |
| 6 | U1 主张零依赖，U2 要 `cryptography` | **两者都对，按用途分工**：密码→`scrypt`（单向）；key→`Fernet`（可逆） | 只新增 `cryptography`；`Dockerfile` 不必改，只改 `requirements.txt` 一行 |
| 7 | U1 的 `CHECK(status IN ('active','disabled','locked'))` 不含 U3 要插入的 `'bootstrap'` | 扩为四值 **`('bootstrap','active','disabled','locked')`** | 不扩则迁移 5 的 INSERT 被 CHECK 拒绝 → 整批事务回滚 → **上线失败** |
| 8 | `app.dependency_overrides` 对中间件无效 | 把「解析会话→user_id」提成**模块级 `_resolve_session_user_id(request)`**，中间件只调用它 | 否则 36 处 `TestClient` 无法用 1 个 fixture 统一注入，爆炸半径从 1 处变 36 处 |
| 9 | 租户传递：隐式 contextvar vs 显式传参 | **api_app→db 用显式参数**；contextvar 仅用于 `llm_client` 这类无法改签名的深层库 | 裸 `threading.Thread`（`:2387`/`:2848`）不继承 contextvars，丢失后**静默写错数据且测试不变红** |

### 附加纪律（写进 PR 门禁）

- 代码中**禁止出现 `user_id = 1` 字面量**（用 `username`+`role` 查）
- 代码中**禁止出现 `DEFAULT 1`**（`token_version` 除外）
- 任何新表/新列**必须有 `user_id` 或明确标注"全局共享"**
- `PRAGMA foreign_keys` 的状态**不得**成为任何测试的前提

---

## 四、技术方案要点

### 4.1 身份与权限（U1）

- **认证**：服务端 session（DB 表 + `__Host-` Cookie / `HttpOnly` / `SameSite=Lax` / `Secure`）。不用 JWT——理由是"禁用用户/踢下线"必须能立即撤销，JWT 要么短 TTL 要么上黑名单（那就退化成 session）。
- **密码**：`hashlib.scrypt`，`n=2^15`、显式 `maxmem`（实测 60ms / 32MB）。
- **四态用户**：`bootstrap`（零权限，只能走一次性 env token 初始化）/ `active` / `disabled`（session 立即失效）/ `locked`（安全事件锁定，admin 可解）。
- **主 admin 标识**：`is_primary_admin INTEGER DEFAULT 0` + 部分唯一索引，**设密成功后才置位**。
- **权限分级**（程序化枚举 80 路由）：**P=3 / U=53 / A=24**（管理类 24 个端点 admin-only）。
- **引导**：一次性 env token + CLI 双通道，**无默认密码**（明确排除 admin/admin）。
- **首次启动**：迁移插入 `bootstrap` 行（`user_id` 由 AUTOINCREMENT 分配，不写死）→ 设密 → 转 `active`。

### 4.2 LLM 配置隔离（U2）

- **存储**：DB 表 `user_llm_config`（迁移 7），不用"每用户一文件"——避免路径遍历攻击、备份遗漏、事务不一致。
- **加密**：`cryptography` Fernet，主密钥来自 `LOTT_SECRET_KEY`（或 `data/.secret_key`，权限 600）。响应严格脱敏 `sk-****abcd`；**错误/异常/日志一律不得出现明文**。
- **预算隔离**：`_budget_cache` 按 `(user_id, url, model)` 分键，改造点基于现有 `_budget_lock`。
- **配额定位**：**防滥用，不是省钱**（成本实测 230 元/年）。默认共享管理员 key + 每日配额；用户可自备 key 覆盖并豁免公共配额（混合层级）。
- **失败不扣额**：实测失败率 **27%**，网络故障/上游 5xx **不扣用户额度**，只有 400 类用户错误计入频次。
- **降级**：未配置 LLM 的用户 **100% 走纯统计通道照常出号**（已验证 `engine.py:2311-2360` 路径）。
- **热更新**：改 key 只影响本人，不拖慢他人。

### 4.3 数据模型（U3）

- **迁移编号已冻结**：5=`users`、6=`sessions`、7=`user_llm_config`、8=租户化（U2 原写的"batch-5"撞号已修正）。
- **三张表必须改主键**（`eval_meta` 的"只改查询层"方案不可行）：
  - `predictions` → `(user_id, issue, seq)`
  - `eval_meta` → `(user_id, issue, method)`
  - `eval_details` → `(user_id, issue, method, seq)`
  - `eval_results` → `(user_id, issue)`
  - 理由：PK 是**唯一性约束**，加 `WHERE user_id=?` 只能过滤读，**挡不住写**——两个用户同 `(issue,method)` 时 `INSERT OR REPLACE` 仍会覆盖。
- **SQLite 改主键 = 整表重建**：12 步脚本（含 2 次行数守卫、逐表幂等闸、索引重建）；已核实 `sqlite_sequence` 无影响、`legacy_alter_table=0`、无视图/触发器、事务内 DDL 原子（`BEGIN IMMEDIATE`）。
- **不引入外键**（本轮）：爆炸半径 = 79 端点 + 1057 测试，收益仅防手工 SQL 孤儿行。
- **缓存键必须是三元组** `(user_id, issue, config_fp)`——`config_fp` 只用于同用户内判失效，**绝不跨用户比较**。
- **历史数据**：`predictions` 40 行显式回填给主 admin；33 条历史假设标 `legacy_untracked`，**不许补注册**（补注册=伪造前置注册时间戳）。

### 4.4 规律库在多用户下的归属（关键）

**前置注册簿 = 全局唯一 α 台账 + 每用户注册配额 + `by_user` 归因 + 私有草稿/全局发布。**

核心论点：**α 的分母属于共享数据，不属于按按钮的人。** 若按用户各自 α，N 个用户各跑一族检验、结果汇入同一共享规律库，库的 FWER = `1-(1-α)^N`（N=10、α=0.05 → **40%**）——这正是"规律库变噪声库"。

两条纪律：
- **先修 `register_prereg` 零调用，再谈多用户**（否则给空池子灌水）
- **`source=="llm"` 的假设不得进 α 台账**（`patterns_runtime.py:125` 硬编码永不 usable），否则每用户 LLM 产出会稀释全体 α

---

## 五、路线：多用户插在哪

### 5.1 修订后的完整阶段序列

```
P0 部署前置4项 + 真值锁修复 (3.5d)
  ↓
MU-1 身份骨架 (4.0d) ──┐
                       ├─ MU-0 prereg 接线 (2.0d)   ← 2 路真并行（不相交文件域）
P1a A档删除 80→73 (1.5d)┘
  ↓
MU-2 迁移5–8 + 租户化 + LLM隔离 (16.5d)   ← 独占 db/migrations/api_app/engine
  ↓
P1b B/C档重定级版 (3.0d) → P2 模块化 (5.0d) → P3 分离剩余 (5.5d)
  ↓
P4 闲时挖掘 (3.5d) ── P5′ LLM岗位 (4.0d)   ← 2 路并行
  ↓
P6 总验收 (2.0d)   ＋ ④前端 2.0 / ⑤收尾 1.0（跨阶段分摊）
```

### 5.2 为什么是这个顺序（四条硬依赖）

1. **P0 必须最先**：真值锁不修，基线不是绿的，后面每个 PR 都在噪声里 review；端口不修，所有鉴权可被绕过。
2. **MU-1 必须先于 P1（精简）**：**鉴权白名单就是 80 条路由的分级枚举**——先删端点再定权限，必然返工。但 A 档 7 项零交集，可以抢跑。
3. **MU-2 必须先于 P1b**：B/C 档重定级（如"个人 LLM 配置"从可选变必需）需要先有租户过滤与配置隔离。
4. **P2 模块化仍在 P1 之后**：V13 的原始理由不变——**精简后的边界才是模块边界**。

### 5.3 多用户如何改变 V13 四点

| V13 点 | 变化 |
|---|---|
| 第 1 点 模块化 | 判定不变；但"线程池分层"和"WAL"提前，因为多用户放大并发 |
| 第 2 点 精简 | **唯一可直接开工的地位被打破**；三项 B 档必须重定级（"实用工具"类从可降级变必需） |
| 第 3 点 规律库 | 判定不变；但"闲时挖掘给谁挖"被定死为**只挖全局** |
| 第 4 点 LLM | 成本治理从 P5 尾部**提前到 MU-2 前置**（预算隔离是前提）；去 `+20` 偏置仍在 P5 |

---

## 六、多人协作推进方案（用户明确要求）

### 6.1 真实并行度 = 2 路

**关键教训**：不是"有两个人"，而是"有**两片不相交的文件域**"。
历史事故：V10 曾把 2 路误报为 4 路，实际因 `api_app.py` / `engine.py` 被多方争抢，真实并行度归 1。

**一句话规则**：`db.py` / `migrations.py` / `api_app.py` / `engine.py` / `config.py` / `llm_client.py` 这 6 个热点文件，**同一时间只允许一个任务在改**。

### 6.2 团队配置（两档）

**3 人小队**（保守，日历 ~36 天）
- A：平台/视图/身份（`api_app.py` 路由 + `auth.py` + 前端）
- B：数据/迁移/统计（`db.py` + `migrations.py` + `engine.py` + 规律链）
- C：LLM/测试/发布（`llm_client.py` + `user_llm_config` + 全量回归 + 部署）
- 串行点：`api_app.py` 在 MU-1 与 MU-2 之间必须排队

**5–6 人满编**（日历 ~30 天，但仍是 2 路真并行 + 3 路"零冲突任务"）
- 1 架构/契约（OWNERS 维护 + PR 门禁）
- 2 后端鉴权（MU-1）
- 3 数据迁移（MU-2 的 ③ 块）
- 4 LLM 集成（MU-2 的 ② 块 + P5′）
- 5 前端（登录遮罩 + 管理面板 + 设置页）
- 6 测试/发布（回归 + 灰度 + 回滚演练）
- **第 3 路起只排"只读/文档/前端静态/测试编写"类零冲突任务**

### 6.3 文件级冲突矩阵（本节最重要）

| 文件 | 独占阶段 | 排队规则 |
|---|---|---|
| `db.py` | MU-2 | 只允许 MU-2 一个任务改；P2 的 WAL 追加排在其后 |
| `migrations.py` | MU-2 | 迁移 5–8 一次性提交，**不允许多个 PR 各加一条迁移** |
| `api_app.py` | MU-1 → MU-2 → P1b → P2 | **严格串行**，四个窗口依次排号 |
| `engine.py` | MU-2（`copy_context` 2 行）→ P5′（去偏置） | 两次改动之间必须完全冻结 |
| `config.py` | MU-1（`SYSTEM_USER_ID` 常量）→ MU-2 | 小改动但被全员 import，改动后需全员 rebase |
| `llm_client.py` | MU-2 的 ② 块（独占） | 与 P5′ 串行 |

**周节奏**：周一合并窗口 → 周三冻结点 → 周四灰度 → **开奖前 2 小时静默**（21:35 是调度器跑批时间，禁止发布）。

### 6.4 PR 门禁（缺一条即拒）

1. 全量 `pytest -q` **≥1037 passed / 0 failed**（P0 修完真值锁后的新基线；当前是 1036+1 failed）
2. `git status data/` 为空（不许提交运行时产物）
3. 无新增 `DEFAULT 1`、无 `user_id = 1` 字面量
4. 无密钥落日志（grep `api_key` / `sk-` 在 diff 里）
5. 新表/新列必须有 `user_id` 或标注"全局共享"
6. 涉及迁移的 PR 必须附"回滚演练"记录
7. 涉及 `api_app.py` 的 PR 必须列出"新增端点的权限分级"

### 6.5 发布与回滚（单节点不中断）

**发布**：备份 → 构建新镜像 → `docker compose up -d`（滚动）→ 健康检查 → 冒烟 → 观察 3 期。
**四层回滚**：
- L1 代码：`git revert` 单提交（零风险，纯增量 DDL 旧代码不认识新表也不报错）
- L2 数据库：`data/ssq.db.pre-migrate-v*` 回切（**先还原 DB 再回滚代码**）
- L3 行为开关：`LOTT_MULTIUSER=0` 退回单用户读写语义
- L4 全量：`ssq-backup` tar 恢复

---

## 七、风险登记册（多用户特有 10 条）

| # | 风险 | 概率/影响 | 缓解 | 触发信号 |
|---|---|---|---|---|
| R1 | 越权 IDOR（`task_id` 仅 32 bit） | 中/高 | `/api/tasks/{id}` 校验归属；`result_json` 不含他人票据 | 陌生人访问他人 task 返回 200 |
| R2 | API key 泄露 | 中/灾难 | Fernet 加密 + 脱敏 + 日志扫描 + 响应快照测试 | 日志/响应出现 `sk-` 明文 |
| R3 | 预算被刷 | 高/中 | 每用户日配额 + QPS + 失败不扣额 + 429 | 单用户 token 日耗突增；429 率 >5% |
| R4 | 数据串号 | 中/高 | 显式传参 + 缓存键三元组 + "A改配置不影响B"测试 | 出现他人票据；`config_fp` 跨用户命中 |
| R5 | 迁移失败/丢库 | 低/灾难 | 批次事务 + 自动备份 + 行数守卫 + 预演 + 回滚演练 | `pre-migrate-v*` 缺失；迁移 exit≠0 |
| R6 | session 劫持 | 中/高 | `__Host-` + HttpOnly + Secure + SameSite + 轮换 | 异常 IP 复用同一 session |
| R7 | 端口绕过鉴权 | 高/灾难 | P0 改 `127.0.0.1` + 防火墙 | 外部 `:18000` 可达 |
| R8 | contextvar 静默丢失写错数据 | 中/高 | 显式传参为主 + 静态断言禁止从 contextvar 取 uid 写库 | 数据归属异常（SYSTEM 行暴涨） |
| R9 | bootstrap 行滞留（零密码有 admin 标记） | 中/高 | 设密成功才置 `is_primary_admin`；启动自检告警 | `status='bootstrap'` 长期存在 |
| R10 | 并行冲突返工 | 中/中 | 文件独占 + 周节奏 + 开奖前静默 | 同一 PR 触及 2+ 独占文件 |

---

## 八、验收标准

**通用基线**：全量 `pytest -q` **≥1037 passed / 0 failed**；`/api/predict` p95 ≤ 改造前 120%；`/api/health` 含 `schema_version` + `auth_enabled`；前端 `?v=` 已升且公网 `x-cache-status: MISS`。

| 阶段 | 可运行 | 可度量 | 可回滚 |
|---|---|---|---|
| **P0** | 端口仅 nginx 可达；`/docs` 404；未认证 401 | 全量测试 **0 failed**（真值锁已修） | 配置改回 + 单提交 revert |
| **MU-1** | 能登录、能登出、admin 能建用户 | bootstrap 零权限断言；`status='bogus'` 被拒；36 处 TestClient 零改动 | L1 代码回滚（纯增量 DDL） |
| **MU-0** | 跑 1 期调度后 `prereg_status≠None` 条数 > 0 | E2E 无 mock；**删调用点必红** | 判定开关 |
| **MU-2** | 迁移 5–8 通过；A/B 两用户数据互不可见 | "A改配置不影响B"；"A的key不出现在B响应/日志"；孤儿行测试（不依赖 PRAGMA） | L1/L2/L3 三层 |
| **P1b** | 页面 14→9；端点数下降 | 每项删除"破 N 测试"清零 | 按批 revert |
| **P2** | **任杀一模块其余端点仍 200** | 全外部调用有 timeout；health P99<500ms | 开关回退 |
| **P3** | E2E：注册→判定→快照→usable=True | 注入必检出 + 纯随机不误报 | 判定开关 |
| **P4** | 在线请求 5s 内让路；可断点续跑 | 配额 health 可见；7 天 p95 无退化 | `LOTT_MINING=0` |
| **P5′** | **拔掉 LLM key 全系统照常出号** | 岗位 ≥2；pass/fail/invalid 分母；成本单期可查 | 单岗位开关 |
| **P6** | 按用户灰度 0→2（先内部 2 人） | 对照差在预期内；三处版本号一致 | 开关 + 备份回切 |

---

## 九、第一周就能开工（8 条，可直接派单）

| # | 任务 | 文件范围 | 验收 | 依赖 |
|---|---|---|---|---|
| W01 | **P0 部署前置一次关门**：端口 `127.0.0.1:18000:18000`、关 `/docs` `/redoc` `/openapi.json`、收紧 CORS、`db.py` 加 `WAL`+`busy_timeout` | `docker-compose.yml`、`api_app.py`、`db.py`、`.env` | 外部 `:18000` 不可达；`/docs` 404；并发写无 `database is locked` | 无 |
| W02 | **修真值锁测试失败**（`_has_real_db()` 改执行期求值 + `constraint_ctx` 加前置条件） | `tests/test_v12_shape_lock.py`、`engine.py` | 全量 **1037 passed / 0 failed** | 无 |
| W03 | **`auth.py` 骨架**：scrypt（`maxmem` 显式）+ PHC 串 + 用户 CRUD DAO | 新 `lottery/auth.py` | 单测：哈希/校验/PHC 往返 | 无 |
| W04 | **迁移 5/6**：`users`（四值 status）+ `sessions` + bootstrap 行 | `migrations.py` | 迁移幂等；`bootstrap` 可插入；`bogus` 被拒 | W03 |
| W05 | **中间件 fail-closed**：`_resolve_session_user_id` 提取 + deny-by-default + contextvar | `api_app.py` | 未登录 401；36 处 TestClient 用 1 个 fixture 通过 | W04 |
| W06 | **MU-0 `register_prereg` 接线**（只 `source=="stat"`） | `api_app.py`（调度器函数体）、`hypothesis.py` | 跑 1 期后 `prereg_status≠None` > 0；删调用点测试变红 | 无（**唯一可立即并行的第二路**） |
| W07 | **A 档 7 项删除**（零交集） | `api_app.py`、前端 | 每项"破 N 测试"清零 | 建议 W05 后 |
| W08 | **前端 `api()` 单点改造** + 登录遮罩骨架 | `web/static/app.js`、`web/index.html` | 401 自动跳登录；CSRF 头注入 | W05 |

**W01–W02 必须先做完**（它们决定后面所有 PR 的评审基线是绿还是红）。
**W03–W05 是 MU-1 骨架**（一个人做）；**W06 是唯一能与它并行的第二路**（文件域不相交）。

---

## 十、需要用户确认的 4 个决策

1. **产品形态**：接受"每用户各自生成预测"（方案 C）吗？意味着每个用户能看到**属于自己配置**的结果，代价是同一期不同用户结果不同。
2. **LLM Key 策略**：默认**共用管理员 key + 每用户日配额**，用户可自备 key 覆盖？
3. **工期与人力**：54 人日 / 30–36 工作日。你要 **3 人小队**还是 **5–6 人满编**？
4. **历史数据**：40 期历史预测归 admin，33 条历史假设标 `legacy_untracked`（不补注册）——同意吗？

---

## 附：文档地图

| 文档 | 行数 | 内容 | 看什么 |
|---|---|---|---|
| `V13_LEAD_VERDICT.md` | 308 | 四点改造总裁决 | 六点判定、5 处纠偏、MVT |
| `M1_ARCHITECTURE.md` | 392 | 模块化与故障隔离 | 8 组循环依赖、隔离选型 |
| `M2_STATISTICS.md` | 679 | 规律三段分离 | Rule Protocol、e-process 标定 |
| `M3_AI.md` | 755 | LLM 岗位矩阵 | 11 行岗位表、可回放判据 |
| `M4_SIMPLIFY.md` | 823 | 功能精简 | 80 行端点证据矩阵、A/B/C 三档 |
| `M5_DELIVERY.md` | 230 | 落地监理 | 风险册、MVT |
| `U1_IDENTITY.md` | 1677 | 身份与权限 | 9 条裁决后的终局版、权限矩阵 P3/U53/A24 |
| `U2_LLM_ISOLATION.md` | 716 | LLM 配置隔离 | Fernet、`_budget_cache` 分键、配额 |
| `U3_DATA_MODEL.md` | 1680 | 数据模型 | 共享/私有边界、迁移脚本、α 台账 |
| `U4_ROADMAP.md` | 502 | 路线与协作 | 12 任务、冲突矩阵、PR 门禁、8 步发布 |
| **本文** | — | **第二轮总裁决** | **先看本文，再按需查细节** |
