# MU 实施基线：用户确认后的最终决策与第一周任务

> 日期：2026-10-19
> 状态：**用户已确认 4 项决策，本文件为实施基线（开发照此执行）**
> 上游：`MU_LEAD_VERDICT.md`（总裁决）、`U1_IDENTITY.md`、`U2_LLM_ISOLATION.md`、`U3_DATA_MODEL.md`、`U4_ROADMAP.md`
> **本文件只记录决策与派单，不改动任何业务代码。**

---

## 一、用户决策（原文 + 系统落地解读）

| # | 用户原话 | 落地解读 | 与早期方案的差异 |
|---|---|---|---|
| 1 | **每个用户可独立预测** | 采用**方案 C 的强化版**：每个用户生成属于自己的预测，`predictions.user_id` 必填；共享层只保留"事实"（开奖数据/特征/规律库/挖掘产出） | 确认了 U3 的方案 C。**强化点**：不再有"全局参考轨道"作为用户可见结果，避免用户误以为那是自己的预测 |
| 2 | **默认不配置 llm key，需要用户自己配置** | **删除"共用管理员 key"整套逻辑**。`user_llm_config` 初始 `api_key_encrypted = NULL`；未配置的用户走**纯统计通道** | **重大简化**：不需要共享 key 配额池、不需要防刷爆共享 key、不需要 `use_custom_key` 标志位 |
| 3 | **你自己定**（工期与人力） | 采用 **3 人小队**（见 §三） | 我是主动选保守档，理由见 §三 |
| 4 | **你定**（历史数据处置） | 历史 40 期归主 admin；33 条假设标 `legacy_untracked`；**不补注册** | 见 §四 |

### 决策 2 带来的连锁影响（**这是本轮最关键的技术裁决**）

**问题**：`.env` 当前 `LOTT_STRICT_MODE=1`，而严格模式的设计是"**必须 LLM + 规律出号，否则不出号**"：
- `methods.py:606` 在严格模式下**强制** `llm_can_pick = True`（**不检查用户是否配置了 key**）
- `api_app.py:240-247` 门控检查 `llm_can_pick`，为 False 则 **422 拒绝出号**
- `engine.py:2326-2333` 在 `llm_required and not use_llm` 时抛 `LLMChannelError`

**后果**：若沿用现在的严格模式语义，**每个新用户一登录就 422 无法出号**——他的 key 还没配，而系统要求"没有 LLM 就不出号"。**这与"默认不配置 key、用户自备"直接冲突，功能上线即不可用。**

**裁决 2-A：严格模式改为「按用户生效」，且语义从"无 LLM 不出号"改为"无 LLM 则走纯统计并明确告知"。**

具体规则（写进代码 docstring 与测试）：

| 用户状态 | 严格模式行为 |
|---|---|
| 该用户**已配置** key 且 LLM 通道可用 | 保持严格语义：LLM 失败 → **不出号**（422，明确报错） |
| 该用户**未配置** key（新用户默认） | **不触发严格门控**，走纯统计通道正常出号，响应中带 `llm_configured=false` + 提示"配置 key 后可使用 AI 通道" |
| 该用户 key 配置了但**调用失败**（网络/额度/429） | 该用户严格语义生效 → 422，并给出可读原因 |
| 管理员（`role='admin'`） | 可选择"全局强制严格"用于自测，但**不得**影响普通用户 |

**同时废除** `methods.py:606` 的 `if _strict: llm_can_pick = True` 全局强制——改为按用户判定。理由：这条强制是在"单用户、自己配了 key"的前提下写的，多用户后它会**误放行**没配 key 的用户进入严格门控，然后在下游炸掉。

> **预判风险**：`LOTT_STRICT_MODE` 是个**进程级环境变量**，无法表达"按用户"的语义。因此实施时它只能表达"是否允许严格模式"这个总开关，真正的按用户判定必须落到 `resolve_channels(user_id=...)`，由 `user_llm_config` 决定。这是 MU-2 的必做项。

---

## 二、最终技术方案（按用户决策修正后）

### 2.1 LLM 通道策略（按决策 2 简化）

| 项 | 决策 |
|---|---|
| 新用户默认状态 | `user_llm_config` 无记录 / `api_key_encrypted IS NULL` → **纯统计通道** |
| 是否保留"共享管理员 key" | **不保留**。管理员不代付（除管理员本人账号） |
| 配额体系 | **简化**：不再需要"共享池配额"。仅保留**防滥用**的每用户 QPS 限制（如 2 req/s）与单次 token 上限，防止**单个用户的 key 被系统误刷**（保护用户自己的钱） |
| 失败计费 | 网络故障/5xx **不计入用户可见的用量统计**（实测失败率 27%，让用户为自己无法控制的事买单不合理） |
| 降级 | 未配置/调用失败 → **纯统计照常出号**（除已配置用户遇到严格模式） |
| 密钥加密 | 仍用 `cryptography` Fernet（key 必须可逆，要拿去调 LLM）；密码仍用标准库 `hashlib.scrypt` |

**这比原方案少做的**：共享配额池、`use_custom_key` 标志、管理员代付与分配额、预算池重置逻辑。**约省 0.5–1 人日。**

### 2.2 数据归属（按决策 1 强化）

- `predictions(user_id, issue, seq)` —— **每用户独立**，用户只能看自己的
- `eval_*` 全部加 `user_id`
- **共享层只剩**：`draws` / `features` / `patterns` / 挖掘产出 / 规律库
- **规律库的 α 台账仍全局**（理由不变：α 的分母属于共享数据，不属于按按钮的人）；用户个人假设进"私有草稿"，通过验证后才进全局库

### 2.3 安全前置（不变，仍是 P0）

`LOTT_TOKEN` 与新 session 鉴权二者取一（新鉴权就位后 `LOTT_TOKEN` 降级为 API 兼容层）+ 端口 `127.0.0.1:18000:18000` + 关闭 `/docs` `/redoc` `/openapi.json` + 收紧 CORS。

---

## 三、决策 3：团队配置 —— 选 **3 人小队**（我定）

**理由**：
1. 6 个热点文件（`db.py`/`migrations.py`/`api_app.py`/`engine.py`/`config.py`/`llm_client.py`）决定**真实并行度就是 2 路**；满编 5–6 人在此约束下第 3 人起只能做只读/文档/测试，边际收益低而协调成本上升。
2. 已有惨痛教训：V10 曾把 2 路误报为 4 路，实际因热点文件争抢并行度跌回 1。
3. 决策 2 简化了配额体系，**工作量比原估少约 1 人日**，3 人足够。

**分工**：

| 角色 | 负责 | 独占文件 |
|---|---|---|
| **A · 平台/身份** | `auth.py`（scrypt/session/中间件）、迁移 5/6、登录页、用户管理面板、前端 `api()` | `auth.py`、`web/*` |
| **B · 数据/统计** | 迁移 7/8、`db.py` 租户化、`engine.py` 的 `copy_context`、规律链、`register_prereg`（MU-0） | `db.py`、`migrations.py`、`engine.py` |
| **C · LLM/测试/发布** | `user_llm_config`、`crypto.py`、`llm_client.py` 预算分键、全量回归、部署与回滚演练 | `llm_client.py`、`config.py` |

**串行点**：`api_app.py` 由 A 独占；B/C 需改它时**排队并走 A 的 PR 评审**。
**工期**：54 − 1（配额简化）≈ **53 人日**，3 人实际并行 2 路 → 日历约 **32–38 工作日**。

---

## 四、决策 4：历史数据处置（我定）

| 数据 | 处置 | 理由 |
|---|---|---|
| `predictions` 40 行（7 期） | **回填给主 admin**（`is_primary_admin=1`，用 `username`+`role` 定位，**不写死 user_id**） | 这些是迁移前唯一用户的历史决策，归属他本人最诚实 |
| `eval_meta` 28 / `eval_details` 35 / `eval_results` 6 / `tasks` 6 | 同上，回填主 admin | 一致的归属原则 |
| `data/hypotheses.json` 33 条 | 标 `legacy_untracked`，**不补注册**，`owner_user_id=0`（SYSTEM） | 补注册 = 伪造前置注册时间戳，是**学术不端**；标为"既有未纳管"是唯一诚实做法 |
| `draws` 3516 / `features` / `patterns` 47 | 不迁移（全局共享，无 user 概念） | 世界的事实，不属于任何人 |
| `llm_audit` / `replay_results` / `mining_runs` / `llm_eval_results` | 均 0 行，无需回填 | — |

**额外纪律**：迁移脚本必须在**同一个事务**内完成"建表→回填→校验行数→建索引"，任一步失败整体回滚；迁移前自动备份（现有 `migrations.py` 已具备，已验证对重建表类迁移同样生效）。

---

## 五、第一周派单（3 人，可直接执行）

| # | 责任人 | 任务 | 文件范围 | 验收 |
|---|---|---|---|---|
| **W01** | **全员同步（半天）** | P0 部署前置一次关门：端口 `127.0.0.1:18000:18000`、关 `/docs` `/redoc` `/openapi.json`、收紧 CORS、`db.py` 加 `PRAGMA journal_mode=WAL` + `busy_timeout=5000` | `docker-compose.yml`、`api_app.py`、`db.py`、`.env` | 外部 `:18000` 不可达；`/docs` 返回 404；并发写无 `database is locked` |
| **W02** | **B** | 修真值锁测试：`_has_real_db()` 改执行期求值；`constraint_ctx` 对空/短输入加前置条件（抛 `InsufficientDataError`） | `tests/test_v12_shape_lock.py`、`engine.py` | 全量 **1037 passed / 0 failed** |
| **W03** | **A** | `auth.py` 骨架：scrypt（**显式传 `maxmem`**，否则 `n=2^15` 抛 ValueError）+ PHC 串 + 用户 CRUD DAO | 新 `lottery/auth.py` | 单测：哈希/校验/PHC 往返；`n=2^15` 不抛错 |
| **W04** | **A** | 迁移 5/6：`users`（`status` 四值含 `bootstrap`）+ `sessions` + bootstrap 行（不写死 user_id） | `migrations.py` | 迁移幂等；`status='bootstrap'` 可插入；`status='bogus'` 被 CHECK 拒绝 |
| **W05** | **A** | 中间件 fail-closed：提 `_resolve_session_user_id(request)` 模块级函数 + deny-by-default + status 校验先于 role | `api_app.py` | 未登录 401；36 处 TestClient 用 **1 个 fixture** 通过 |
| **W06** | **B** | MU-0：`register_prereg` 接线（只对 `source=="stat"`）+ 跨期累积批次 | `api_app.py`（调度器函数体）、`hypothesis.py` | 跑 1 期后 `prereg_status≠None` 条数 > 0；**删调用点必须变红** |
| **W07** | **C** | `crypto.py`：Fernet 封装 + 主密钥加载（`LOTT_SECRET_KEY` 或 `data/.secret_key` 600）+ 脱敏函数 | 新 `lottery/crypto.py`、`requirements.txt`（加 `cryptography`） | 加解密往返；日志/响应无明文 key；主密钥缺失时**拒绝启动**而非降级明文 |
| **W08** | **C** | 严格模式按用户改造的**设计定稿 + 测试骨架**（见 §一 裁决 2-A） | `methods.py`、`api_app.py` 的设计文档 + `tests/` 骨架 | 四象限测试用例写出来：已配置/未配置 × 成功/失败 |

**W01–W02 必须先完成**（决定后续所有 PR 的评审基线）。
**W03–W05 是 MU-1 骨架（A 一人串行）**；**W06 是唯一能与之并行的第二路（B）**；**W07–W08（C）文件域不相交，可并行**。

---

## 六、修订后的验收基线

| 项 | 值 |
|---|---|
| 测试基线 | **1037 passed / 0 failed**（W02 修完真值锁后；当前 1036 + 1 failed） |
| 新增测试（MU 全期） | 约 120 条（认证 40 / 隔离 30 / 迁移 20 / 严格模式 15 / 前端 15） |
| 安全验收 | 未认证 401；`/docs` 404；外部 `:18000` 不可达；日志/响应无明文 key |
| 隔离验收 | A 改配置不影响 B；A 的 key 不出现在 B 响应/日志；A 看不到 B 的票单 |
| 降级验收 | **新用户（无 key）登录即可用纯统计出号** ← 决策 2 的核心验收项 |
| 严格模式验收 | 已配置用户失败→422；未配置用户→正常出号且提示配置 |
| 回滚验收 | L1 代码 revert / L2 `pre-migrate-v4` 回切 / L3 `LOTT_MULTIUSER=0` / L4 全量 tar，四层各演练一次 |

---

## 七、需要盯住的三条风险（因决策 2 新增或强化）

| # | 风险 | 说明 |
|---|---|---|
| N1 | **严格模式误伤新用户** | 已裁决为按用户生效；实施时必须加"新用户可正常出号"的 E2E 测试，否则上线即不可用 |
| N2 | **`LOTT_STRICT_MODE` 是进程级 env，无法表达按用户** | 必须下沉到 `resolve_channels(user_id=...)`；这是 MU-2 的必做项，遗漏会导致"一个人开严格模式全站受影响" |
| N3 | **用户自备 key 的体验断层** | 新用户无 key 只能拿纯统计结果，可能觉得"AI 功能坏了"。UI 必须明确显示"未配置 AI 通道 · 去配置"，而不是静默降级 |
