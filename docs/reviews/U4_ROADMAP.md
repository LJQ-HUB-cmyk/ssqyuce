# U4 路线整合与多人协作推进方案：多用户 × V13 四点

> **评审对象**：`ssqyuce`（Python + FastAPI + SQLite，单容器 `ssq-predictor`，公网 `https://ssq.ff88.de`，4 核 11GB 宿主 + 14 邻居容器）
> **本文定位**：只读整合评审。除本文件外未改任何代码/配置。**不重复 U1/U2/U3 已讲透的技术细节**——只回答"顺序、依赖、分工、门禁"。
> **必读输入**：`V13_LEAD_VERDICT.md`（四点判定、P0–P6、31 人日、真实并行 2 路、MVT）、`U1_IDENTITY.md`（1573 行）、`U2_LLM_ISOLATION.md`（716 行）、`U3_DATA_MODEL.md`（1680 行）、`M1–M5`。
> **本轮新需求**：「改为多用户使用，每个用户的 llm 配置隔离，管理员可增加用户。完善方案，多人协作推进开发落地。」
> **Lead 已裁决 7 条定论**（本文直接采用、不重新论证）：① 产品形态 = **方案 C**（事实层共享 + 决策层按用户；否决 B 的理由是"不可观测的装饰功能"而非成本）；② 鉴权 = 服务端 session + deny-by-default 单中间件 + contextvars，口令 `hashlib.scrypt`、API key `cryptography` Fernet，**只改 `requirements.txt` 一行**；③ 禁止假设 `user_id=1`，新增列 `DEFAULT 0`（=`SYSTEM_USER_ID`，哨兵、不入库）；④ `ON DELETE CASCADE` 不得为正确性依据（`PRAGMA foreign_keys=0`），删用户走应用层显式事务 + 不依赖 PRAGMA 的孤儿行测试；⑤ P0 部署前置不可分割（`LOTT_TOKEN`/鉴权就位 + 端口 `127.0.0.1:18000:18000` + 关 `/docs` `/redoc` `/openapi.json` + 收紧 CORS）；⑥ 先修 `register_prereg` 零调用，前置注册簿 = 全局唯一 α 台账 + `by_user` 归因 + 私有草稿/全局发布；⑦ contextvars：async 中间件→同步 `def` 端点 ✅，`threading.Thread`/线程池 ❌，三处（`api_app.py:2387` 调度器、`:604`/`:2848` 后台线程、`engine.py:881` 线程池）必须显式传参/`copy_context`。
> **数字基线**（与已有文档一致）：collect **1057** / 实跑 **1036 passed + 1 failed**（`test_v12_shape_lock.py` 真值锁，P0 必修）/ 19 skipped / 1 xfailed；修完基线应为 **1037 passed / 0 failed**。

---

## 0. 结论摘要（给没时间读全文的人）

| 项 | 结论 |
|---|---|
| 多用户新增工作量 | **23 人日**（区间 21–27）：身份 3.5 ＋ LLM 隔离 3.5 ＋ 数据模型/迁移 13.0 ＋ 前端登录/管理 2.0 ＋ 收尾负担 1.0（见 §3；`register_prereg` 2.0 已计入 V13 P3，不重复计） |
| 修订后总量 | **54 人日**（区间 50–60）= V13 的 31 ＋ 新增 23；V13 P0 的"LOTT_TOKEN 生效"子项被 MU-B（session）替代吸收，不再单列 |
| 真实并行度 | **2 路**（保守取值；与 V13、U3 §10 一致。历史教训：曾把 2 路误报为 4 路——`db.py`/`api_app.py`/`migrations.py` 单任务独占决定了第 3 路只能排零冲突任务） |
| 日历 | 约 **30–36 工作日**（54 ÷ 2 路 ≈ 27 天 ＋ 灰度观察期；关键路径见 §3.4） |
| 阶段序列 | `P0部署前置 → MU-1身份骨架 → MU-0 prereg修复 → P1′精简(重定级版) → MU-2数据租户化+LLM隔离 → P2模块化 → P3分离 → P4闲时挖掘 → P5′LLM岗位 → P6总验收`（完整论证见 §2） |
| 第一周三件事 | ① P0 部署前置 4 项一次关门（含端口 `127.0.0.1`）；② 真值锁修复（基线归 0 failed）；③ 身份骨架可登录（`auth.py` + 迁移 5/6 + 中间件替换）。三件的详细派单见 §6 |

---

## 1. (A) 多用户如何改变 V13 四点：逐点重判

> 方法：V13 的判定（具备/有条件具备/不具备）是"能不能做"的结论，多用户不推翻它们；改变的是**优先级、范围与前置关系**。下表每点给：V13 原判定 → 多用户后的变化 → 具体重定级动作 → 证据。

### 第 1 点（模块化）：判定不变，内部顺序提前两项

- **V13 原判定**：有条件具备（功能级降级边界，V13 §3-C1）。
- **多用户的改变**：**优先级上升，且 P2 内部有两项工作被提为 P0**。原因只有一条，但很硬：鉴权中间件让"每个请求"都变成"要查库的请求"（U1 §4.2），而 `db.py:160-174` 是全局单连接、无锁、无 WAL、无 `busy_timeout`——多用户并发点"生成预测"会直接撞 `database is locked`，**鉴权本身变成性能退化源**（U1 §11.2 耦合点 1）。
- **重定级动作**：
  1. `db.py` 加 `busy_timeout` + 会话 30s TTL 缓存（U1 §4.2 ②）从"P2 优化项"提为 **P0/B0 必做**，与鉴权上线绑定排期；
  2. WAL 从"第 1 点范畴"明确为**独立性能变更、单独回滚点**（U3 §7.3/§11：本轮不动 WAL，把 v5 变更面压最小——与上一条不矛盾：`busy_timeout` 立即做，WAL 单独做）；
  3. 鉴权中间件本身**不等 P2**：它是横切层（U1 §11.1），新端点放 `lottery/auth_api.py`、中间件放 `lottery/auth.py`，即使 `api_app.py` 还没拆也能上（U1 §11.2 耦合点 3 对第 1 点的主动让步）。
- **不变量**：仍否决多进程/sidecar（768MiB 放不下，V13 §5.2）；仍推荐 ①+②+⑤。

### 第 2 点（精简）：唯一可直接开工的地位被打破，三项 B 档必须重定级

- **V13 原判定**：具备（唯一可直接开工，V13 §1）。
- **多用户的改变**：**"可直接开工"只剩 A 档 7 项删除；B 档里凡与"每用户"沾边的项全部冻结重审**。M4 的 B 档入选标准是"前端 0 调用但有测试或安全价值"（M4 §6）——多用户后，"编辑个人 LLM 配置"从"没人点的设置"变成**每用户必需入口**，原分级逻辑失效。
- **重定级动作**（逐项，引用 M4 编号）：

| M4 编号 | 原分级 | 多用户后 | 理由 |
|---|---|---|---|
| B6 `/api/llm/cost` + `decision/{status,restore}` | 降级到设置页折叠区 | **升级为 P0 范围：重做为"每用户配额卡"**（剩余额度/今日消耗/`Retry-After` 语义，U2 §5.2(3)） | 无配额卡 = 用户不知道自己被限流的原因；且 `restore` 是 admin-only（U1 §6.3 #23） |
| `POST /api/config/llm`（全局） | M4 未单列（U1 §6.4 指出它改进程级 `config.LLM_*` + `os.environ`） | **一拆二**：`PUT /api/user/llm-config`（U，本人，U2 §7.1）+ 全局默认改走 A 的新端点 | 这是"用户 A 改 key 影响用户 B"的直接病灶（U2 §1.1-3），不拆 = 多用户白做 |
| B10「🔬 规律研究」tab（`index.html:202-248`） | 移出"更多"抽屉、并入评估报告 | **保留合并方向，但加可见性过滤**（`private` 仅 owner+admin，`passed` 全局，失败全局只读——U3 §5.5-B） | 否则 A 的私有猜想出现在 B 的界面 = 观点污染（U3 §5.4 否决"全库全局无 owner"） |
| B7 `/api/rules/lifecycle` 系 | 降级为调度器内部件 | **维持**（调度器以 `SYSTEM_USER_ID=0` 运行，U1 §7.5） | 无变化，记录以防有人重提"每用户生命周期"（U3 §5.4 已否决分库） |
| A 档 7 项（A1–A7，80→73） | 删除 | **维持，可直接开工** | 与用户维度无关；但 A2（`/api/notify/status` 泄露 `smtp_host`/`email_to`，U1 §6.3 #24）删除前先确认 admin 侧是否需保留脱敏版 |
| B4/B5（portfolio/L2）、B8/B9（shape/arm） | 合并/别名化 | **维持**，但合并后的新端点从第一天起就带 `user_id` 过滤 | 避免"先合并再返工加租户列"（U3 §8 的教训：签名兼容策略先定） |

### 第 3 点（规律库）：判定不变，但"闲时给谁挖"被彻底定死

- **V13 原判定**：3a/3b 有条件具备，3c 原话不具备→改写后具备（扩大候选池而非可用集，V13 §3-C2）。
- **多用户的改变**：**第 3 点最大的未决项（"规律库全局还是分用户"）已被 U3 关闭**：`patterns`/`patterns_runtime`/`rule_lifecycle` 全局（世界的事实），`hypotheses.json` 全局台账 + `owner_user_id`/`visibility`（U3 §5.1/§5.5）。连带把 3b 的运营语义钉死：**算力全局，LLM 成本归发起者，产出进全局池**（U3 §5.7）。
- **重定级动作**：
  1. 闲时挖掘 = **全局系统任务**（`mining.py`/`discovery.py` 跑共享 3516 期，给每用户各挖一遍 = N 倍 CPU 换同一个结果，U3 §5.7）；
  2. `register_prereg` **只对 `source=="stat"` 调用**（U3 §5.5-E：`patterns_runtime.py:125-127` 硬编码 `source=="llm"` 永不 usable；20/33 条历史假设来自 LLM，若进台账会稀释全体 α）；
  3. 33 条历史假设标 `legacy_untracked`、**不许补注册**（U3 §5.5-D；补注册 = 伪造前置承诺）；
  4. 实施顺序红利：当前 `usable=0`（实测），A/B/C 三方案出号结果暂时等价——**可在"规则为空"窗口先完成多租户迁移，等闸门修好时隔离逻辑已被生产验证**（U3 §4.9）。
- **对 V13 MVT 的影响**：MVT 第 2 件（prereg 闭环约 3.5 人日）升级为**多用户灰度的 P0 前置**（裁决 6）：无闸门的挖掘 = 噪声生产机，有闸门的多用户 = N 倍注册涌入已死的闸门 = "看起来有闸门、实际全开"（U3 §5.2）。

### 第 4 点（LLM）：成本治理从 P5 尾部提前为 MU-2 前置；去偏置仍在 P5

- **V13 原判定**：有条件具备（岗位矩阵 + 去偏置，V13 §1/§4.8）。
- **多用户的改变**：U2 把"预算隔离"从 V13 P5 的"成本治理 1 人日"拆成**四个必须在多用户灰度前到位的件**：`_budget_cache` 按 `scope` 分键（U2 §5.1，否则 A 的 32000 学习值污染 B 的超时阈值至 180s）、Fernet 加密列（U2 §4.2）、每用户日配额 + 429 语义（U2 §5.2(3)：默认 50,000 tokens/日约 6–8 次预测）、**失败不扣额**（27% 失败率下"发请求就扣"会让额度系统性缩水 25%，U2 §5.2(2)）。
- **重定级动作**：
  1. 上述四件随 MU-2（数据租户化）**一起灰度**，不进 P5；
  2. 留在 P5 的：去 `+20` 偏置（`engine.py:2438`，V13 §4.8）、2–3 个可测岗位、混合层级模式的产品开关（默认共享通道 + `use_custom_key=1` 自备覆盖，U2 §3.2）；
  3. 底线重申：LLM 全挂/未配置/超配额 → 纯统计通道照常出号（U2 §5.3，`engine.py:2326-2348` 已有分支），且**必须显式标注"本次未使用 LLM"**（U3 §4.8，不能像现在这样静默）。
- **成本定性不变**：10 用户 ≈ 230 元/年（U1 §10.7/U2 §3.2/U3 §4.3 三方独立实测一致），配额是**防滥用/防 DoS**，不是省钱（裁决 1 的另一面）。

### 1.5 一句话总览

| V13 点 | V13 判定 | 多用户后被改变的是 | 结论 |
|---|---|---|---|
| 1 模块化 | 有条件具备 | **优先级↑**，`busy_timeout`+会话缓存提前到 B0；WAL 独立 | 判定不变，顺序微调 |
| 2 精简 | 具备（唯一可开工） | **三项 B 档重定级**（LLM 配置/成本卡/规律 tab）；A 档仍可直接开工 | 判定不变，范围收窄且分两批 |
| 3 挖/验/用 | 3a/3b 有条件，3c 改写 | **归属全部关闭**（全局规律 + 归因台账）；prereg 修复升为灰度前置 | 判定不变，语义钉死 |
| 4 LLM | 有条件具备 | **成本治理四件提前到 MU-2**；去偏置仍留 P5 | 判定不变，拆点前移 |

---

## 2. (B) 多用户插在哪：论证 + 完整阶段序列

### 2.1 结论先行

**多用户不是"另起一条线"，而是拆成三段（MU-0 / MU-1 / MU-2），分别挂在 V13 的 P0 之后、P1 之前和 P1 与 P2 之间。** 试图把它塞进任何单一现有阶段，都会违反下面四条硬依赖中的至少一条。

### 2.2 四条硬依赖（为什么不能塞进任何单一现有阶段）

| # | 依赖 | 为什么是"硬"的 | 证据 |
|---|---|---|---|
| D1 | MU 依赖 **P0 部署前置** | 端口 `0.0.0.0:18000` 明文直连可达（实测公网 `curl` 200）⇒ 口令在链路上明文、`X-Real-IP` 可任意伪造 ⇒ **登录限流形同不存在**。鉴权先上、端口后改 = 整套鉴权在限流维度上是漏的 | 裁决 5；U1 §1.3(a)、§10.6 纪律 2、§12.1「B0 未完成前 B1 不得进入灰度」 |
| D2 | MU 依赖 **模块化中的横切层**，但不依赖 `api_app.py` 瘦身 | 鉴权必须是"一个中间件 + 一个 contextvar"，不是 79 处 `Depends`（U1 §7.1）。中间件是横切层，可先落地；P2 的拆分是"把视图层变薄"，两者互不阻塞 | 裁决 2；U1 §11.1（鉴权向下不依赖业务）、§11.2 耦合点 3 |
| D3 | MU 是 **第 2/3/4 点重定级的前提** | 第 2 点：B 档判级依赖"是否每用户必需"；第 3 点：规律归属依赖"谁拥有假设"；第 4 点：预算隔离依赖"知道是谁在花钱"。三者都要 `user_id` 这个概念存在 | §1；U3 §2.1 判据 3（账本判据） |
| D4 | **`register_prereg` 修复必须先于多用户灰度**，但可晚于身份骨架 | 多用户会把 N 倍假设灌进一个当前已死的闸门（`prereg_ok` 恒 False → `usable_rules()` 恒空）。"注册了但不判定"是最坏状态：看起来有闸门、实际全开。修它不依赖 `user_id`（只给台账加 `owner_user_id` 归因字段），所以是 MU-0，可与身份骨架并行 | 裁决 6；V13 §4.2；U3 §5.2/§5.8 |

### 2.3 为什么身份骨架（MU-1）必须**先于 P1（精简）**

这是本文最关键的一条排序判断，四条理由：

1. **鉴权的白名单是"80 条路由的权限分级"，不是权限矩阵本身**（裁决 2）。删端点会改变 `required_role()` 的枚举，而新增端点默认要定级（U1 §7.3 纪律 2：参数化测试遍历 `app.routes`，未定级即失败）。所以顺序是"先建分级机制 → 再删"，删的时候顺手把该条从枚举移除。
2. **若先精简后鉴权，会有一段时间"80→66 端点"的变化在没有权限分级的情况下发生**，而这期间任何一个被"合并/别名化"的端点都可能把数据跨给别人（例如把 `/api/llm/arm` 并入 `/api/battle/report` 时若忘了加租户过滤）。
3. **第 2 点的重定级必须先有"每用户"概念**（§1 第 2 点 B6/B10 两项直接依赖它）。
4. **但 P1 的 A 档 7 项可以抢跑**：它与用户维度零交集、零风险、且能让后续 PR 的 diff 变小（M4 §9 第一批）。所以 P1 拆成 **P1a（A 档 80→73，在 MU-1 前）** 与 **P1b（B/C 档重定级版，在 MU-2 后）**。

### 2.4 完整阶段序列（修订后）

```
路 A（平台/视图/身份）              路 B（统计/模型）                    运维
─────────────────────────────────────────────────────────────────────────────
P0  部署前置 4 项 + 真值锁 (1.0d) ──┐
                                    ▼
MU-1 身份骨架 (4.0d) ───────────────┼── MU-0 prereg 修复 (2.0d) ──┤ ← 2 路真并行
                                    ▼                              （V13 P3 子集，
P1a A 档删除 80→73 (1.5d) ──────────┤   此处列出为依赖，不重复计）│
                                    ▼
MU-2 迁移 5–8 + 租户化 + LLM 隔离 (16.5d=③13.0+②3.5，独占 db/migrations/api_app/engine)
                                    ▼
P1b B/C 档重定级版 (3.0d) ──────────┤
                                    ▼
P2  模块化 (5.0d) ──────────────────┤
                                    ▼
P3  分离剩余 (5.5d，MU-0 已交付写入者+E2E) ──┤
                                    ▼
P4  闲时挖掘 (3.5d) ───────  P5′ LLM 岗位 (4.0d) ──→  P6 总验收 (2.0d)
                        （2 路并行：调度器 vs llm_lab/ai_audit）
```

**阶段清单与回滚点**：

| 序 | 阶段 | 位置理由（一句话） | 可回滚点 |
|---|---|---|---|
| **P0** | 部署前置 4 项（裁决 5）+ 真值锁修复 + `busy_timeout` | 一切的前提；真值锁不修则基线不是绿的，后面每个 PR 都在噪声里 review | 改回配置 + 重建容器；代码单提交 revert |
| **MU-1** | 身份骨架：`auth.py` + 迁移 5/6 + 中间件替换 + 5 条 auth 端点 | 见 §2.3；它产出的 `user_id` 是后续一切坐标系 | **L1 代码回滚零风险**（纯增量 DDL，旧代码不认识新表也不报错，U1 §9.4） |
| **MU-0** | `register_prereg` 接线（只 `source=="stat"`）+ 跨期累积批次 + E2E 双测 | 与 MU-1 真并行（文件域：`hypothesis.py` + 调度器函数体 vs `auth.py`/`migrations.py`/中间件）；不依赖 `user_id` | 判定开关 + E2E 反向用例保护 |
| **P1a** | A 档 7 项删除 | 零交集、零风险、缩 diff | 单提交 revert |
| **MU-2** | 迁移 7/8 + `db.py` 租户化 + 线程池 `copy_context` + LLM 隔离（Fernet/配额/`_budget_cache` 分键）+ `config_fp` 缓存键 | **独占三热点**；是 §1 第 3/4 点落地的物理位置 | 三层：L1 代码回滚 / L2 `ssq.db.pre-migrate-v4` 回切 / L3 全量备份（U1 §9.4 + U3 §7.4） |
| **P1b** | B/C 档按 §1 重定级后的合并/降级 + 前端视图 14→9 | 必须在 MU-1（有 `role`）与 MU-2（有租户过滤）之后 | 按批 revert |
| **P2** | 模块化：边界契约、进程内隔离、调度链分段、WAL（独立回滚点） | V13 原顺序保留：**精简后的边界才是模块边界**（V13 §6.1） | 开关回退旧路径 |
| **P3** | 分离剩余部分（Rule Protocol、统一误差预算、usable 接线、replay 主键） | 依赖模块边界；MU-0 已交出"存在性证明"（故从 7.5 降到 5.5） | 判定开关回退 |
| **P4** | 闲时挖掘（判定器 + 三级配额 + 断点续跑），**只挖全局** | 依赖闸门（MU-0）与模块边界（P2） | 总开关 `LOTT_MINING=0` |
| **P5′** | LLM 岗位（去 `+20` 偏置、2–3 个可测岗位） | 消费前面所有契约；**成本治理已在 MU-2 完成，故从 5 降到 4** | 单岗位开关 |
| **P6** | 总验收：全量回归 + 性能护栏 + **按用户灰度** + 文档 | 灰度从"按通道/按期"加一条"按用户"（灰 0→2，见 §2.5） | 开关切回 + 备份回切 |

### 2.5 灰度分档（只列"什么时候可以放谁进来"）

| 档 | 进入条件（全部满足） | 谁能用 |
|---|---|---|
| 灰 0 | P0 完成；端口已收、`docs` 已关；`LOTT_BOOTSTRAP_TOKEN` 或 CLI 引导出首个 admin | 仅 admin |
| 灰 1 | MU-1 + MU-0 完成；全量 0 failed；孤儿行测试绿 | admin + 1 个受控测试账号 |
| 灰 2 | MU-2 完成；`LOTT_MULTIUSER=1`；U3 §4.10 的 P1–P6 与 §5.9 的 R1–R6 全绿 | 3–5 个账号 |
| 全量 | 灰 2 观察满 3 期；`llm_cost_log` 每条带 `user_id`；`usable` 不再恒 0 | 全部（≤10 个账号） |

---

## 3. (C) 修订工期

> 口径：1 人日 = 1 工程师 1 天；**已内置 ×1.2 历史膨胀系数**（V10 估 14→实 18.5）；含测试；不含需求变更。
> 并行度：**真实 2 路**（保守）。**不按 3 路以上排期**——`db.py`/`api_app.py`/`migrations.py`/`engine.py`/`config.py`/`llm_client.py` 六个热点决定了同时能改的地方只有两处互不相交的函数域。

### 3.1 多用户新增工作量（五块）

| 块 | 内容 | 人日 | 区间 | 依据 |
|---|---|---:|---|---|
| **① 身份** | scrypt/PHC + 会话表 + 引导（`auth.py`）+ 中间件替换 + 5 条 auth 端点 + 13 条管理/审计端点 + 自保护规则 + 限流 + 路由分级参数化测试（= T03 1.5 ＋ T04 2.0） | **3.5** | 3.0–4.5 | U1 §7.6「核心 1 人日可跑通，真正成本在 13 条新端点边界 + 迁移 + 测试」；U1 §12.5「3.5–4 人日」 |
| **② LLM 隔离** | `crypto.py`(Fernet) + `user_llm_config` + `_budget_cache` 分键 + 配额/429 + 失败不扣额 + `PUT/GET /api/user/llm-config` + 线程池上下文穿透 + T1–T8 测试（= T08） | **3.5** | 3.0–4.5 | U2 §9.2 Phase1–5 合计 3.5；其 Phase2 的"表创建"由 MU-2 的迁移 7 承担，见 3.3 去重 |
| **③ 数据模型 / 迁移** | 迁移 5–8（含 `predictions`/`eval_*` 整表重建）+ `db.py` 租户化 + `api_app.py` 7 处显式 `uid` + `issue_baseline`/`config_fp` + `hypotheses` owner/visibility + 9 处 lambda 修 + 迁移用例 + 预演（= T06 2.5 ＋ T07 2.0 ＋ T07b 1.0 ＋ T09 3.0 ＋ T10 1.5 ＋ T12 的测试部分 3.0，exact 13.0） | **13.0** | 11.0–15.0 | U3 §10 的 U3-1 2.5＋U3-2 2.0＋U3-3 1.0＋U3-4 3.0＋U3-5 1.5＋U3-6 2.0＋U3-7 1.0 = 13.0（U3-0 的 2.0 不在内，见 3.3） |
| **④ 前端** | 登录遮罩 + `/api/auth/me` 启动探测 + `api()` 单点改造（CSRF/401 跳转）+ 用户管理面板（6 动作）+ 改密入口 + LLM 设置页重构 + 配额卡 + `?v=` 升级（= T11） | **2.0** | 1.5–2.5 | U1 §7.4（唯一 `fetch` 出口）+ U2 §7.2；无构建链、纯静态，故不大 |
| **⑤ 收尾负担** | 回滚演练 0.5（L1/L2 各一次，U1 §9.4）＋ `/api/health` auth 自检 + 上线检查表 + 文档 + 灰 0→2 观测 0.5（分摊在 T01–T12 各任务尾部，不单列任务） | **1.0** | 0.5–1.5 | U3 §7.7 上线检查表；U1 §12.3 验收 9/10 |
| | **合计（新增）** | **23.0** | **21–27** | 3.5＋3.5＋13.0＋2.0＋1.0 |

### 3.2 总量与日历（**唯一口径，以 §3.1 的五个块为准**）

| 项 | 人日 | 说明 |
|---|---:|---|
| V13 原总量 | 31.0 | P0 3.5 + P1 4.5 + P2 5.0 + P3 7.5 + P4 3.5 + P5 5.0 + P6 2.0 = 31.0 |
| ＋多用户新增 | +23.0 | §3.1 的 ①3.5＋②3.5＋③13.0＋④2.0＋⑤1.0 |
| −V13 P0 的"`LOTT_TOKEN` 生效"子项 | −0.5 | 被 MU-1 的 session 鉴权整体替代，不做两次 |
| V13 P3 的 prereg 写入者+E2E → **MU-0** | ±0 | 只换位置：V13 P3 7.5→**5.5**，MU-0 单列 **2.0**（**不含在 23 内**） |
| V13 P5 的成本治理 → **并入 MU-2** | ±0 | 只换位置：P5 5.0→**4.0**（P5′）；成本治理落进 **③**（**不含在 ② 的 3.5 里**，避免重复计） |
| **修订总量** | **≈54.0** | 31 − 0.5 + 23 = **53.5**，取整 54；**区间 50–60** |
| **日历** | **30–36 工作日** | 54 ÷ 2 路 = 27 个"路·日"；＋灰度观察 3 期＋联调缓冲 ⇒ 30–36（约 6–7.5 周） |

**修订后的阶段人日（与上表严格对账，合计 53.5）**：

| 阶段 | 人日 | 对应 §3.1 的块 |
|---|---:|---|
| P0 部署前置 + 真值锁 + 契约冻结/备份链 | 3.5 | V13 原 P0 |
| **MU-1 身份骨架** | 4.0 | ① 3.5 ＋ bootstrap CLI / `health` 自检 0.5 |
| **MU-0 prereg 接线** | 2.0 | V13 P3 前移（不含在 23 内） |
| P1a A 档删除 | 1.5 | V13 P1 拆分（4.5 = 1.5 + 3.0） |
| **MU-2 数据租户化 + LLM 隔离** | **16.5** | ③ 13.0 ＋ ② 3.5 |
| P1b B/C 档重定级版 | 3.0 | V13 P1 剩余 |
| P2 模块化 | 5.0 | V13 P2 |
| P3 分离剩余 | 5.5 | V13 P3（7.5 − 2.0） |
| P4 闲时挖掘 | 3.5 | V13 P4 |
| P5′ LLM 岗位 | 4.0 | V13 P5（5.0 − 1.0） |
| P6 总验收 | 2.0 | V13 P6 |
| ④ 前端（跨阶段） | 2.0 | MU-1/MU-2 之后 |
| ⑤ 收尾负担（跨阶段） | 1.0 | 分摊 |
| **合计** | **53.5** | 取整 **54** |

### 3.3 三处数字口径澄清（防止以后核账时对不上）

1. **U3 §10 的 15 人日 vs 本文 ③ 的 13 人日**：差的 2.0 是 U3-0（`register_prereg` 修复）。它不是"多用户新增"，而是 V13 P3 的既有工作，本文把它前移为 **MU-0** 并**不再重复计**。合账后总量 54 与"31+23"自洽。
2. **U2 §9.2 的 3.5 vs 本文 ② 的 3.5**：一致。但 U2 Phase2 写的"batch-5 迁移"**撞号**——迁移 5 已被 U1 的 `users` 占用（U3 §7.1 已核实并冻结 5=users、6=sessions、7=user_llm_config、8=租户化）。落地时 `user_llm_config` 是**迁移 7**，由 MU-2 统一提交，U2 的 Phase2 只剩 DAO 层。
3. **V13 的 31 vs 本文的 54**：V13 的 31 是"四点改造"；多用户是**新增需求**（用户原话第 2 轮），两者不是同一口径，不能说"V13 低估了"——正确的说法是"V13 的路线没有被推翻，只是中间插入了 23 人日"。

### 3.4 关键路径（决定日历的那条链）

```
P0(3.5) → MU-1(4.0) → MU-2(16.5) → P2(5.0) → P3(5.5) → P6(2.0)  = 36.5 人日（关键路径）
其余 17.0 人日（MU-0 2.0、P1a 1.5、P1b 3.0、P4 3.5、P5′ 4.0、④ 2.0、⑤ 1.0）= 第二路
36.5 + 17.0 = 53.5 ✓
```
- P0 里只有 1.0 人日（T01 0.5 ＋ T02 0.5）与多用户相关，其余 2.5 是 V13 既有的契约冻结与备份链修复。
- 关键路径以真实 2 路折算 ≈ **18.3 个"路·日"**；但 **MU-2（16.5）内部只有 ~1.3 路**——`db.py`/`migrations.py`/`api_app.py` 单任务独占，第二路在同一时期只能做 `engine.py`/`config.py`/`llm_client.py` 的**不相交函数域**，因此 MU-2 实际日历约 **10–11 天**而非 8.3 天。
- 于是日历 ≈ max(关键路径 ≈ 20 路·日, 总量 53.5 ÷ 2 ≈ 27 路·日) = **27 路·日**，＋3 期灰度观察（约 1 周）＋联调缓冲 ⇒ **30–36 工作日**。

**MU-2 是整条路线唯一的"日历炸弹"**，必须按 §4.3 的排号规则独占，**不允许被 P1b/P2 抢文件**；P1b/P2 只能在 MU-2 完成后开窗口。剩余 17.0 人日（53.5 − 36.5）由第二路吸收（P1a、P1b、P4、P5′、T05、T10 等），这也是"2 路"这个结论的来源：**不是"有两个人"，而是"有两片互不相交的文件域"**。

---

## 4. (D) 多人协作推进方案

### 4.1 工作分解：12 个可并行独立任务

> 标注：**文件范围 / 依赖前置 / 人日 / 验收标准 / 可回滚点**。`[独占]` = 该任务执行期间对应文件不接受其他 PR。
> MU-0 的 2.0 人日已计入 V13 P3，此处列出只为排依赖，不重复计入 §3 总量。

| ID | 任务名 | 文件范围 | 依赖 | 人日 | 验收标准 | 可回滚点 |
|---|---|---|---|---:|---|---|
| T01 | **P0 部署前置 4 项**：端口 `127.0.0.1:18000:18000` + 关 `docs/redoc/openapi` + 收紧/删除 CORS + `busy_timeout` + LOTT_TOKEN 或鉴权就位 | `docker-compose.yml:11`、`api_app.py:28-32`、`db.py:167`、`.env` | 无 | 0.5 | 公网 `curl http://IP:18000/` 连接被拒；`/docs` 等 404；无 `allow-origin: *` | 改回配置 + `up -d` 重建 |
| T02 | **真值锁修复**：守卫改执行期求值 + `constraint_ctx` 空输入抛类型化异常 | `engine.py:41` 附近、`tests/test_v12_shape_lock.py` | 无 | 0.5 | 全量 **0 failed**（基线 1037/0）；空输入单测绿 | 单提交 revert |
| T03 | **身份骨架**：`auth.py`（scrypt/PHC/会话/引导）+ 迁移 5/6 + 中间件替换 + 登录/登出/me/bootstrap | 新 `lottery/auth.py`、`migrations.py`（占 5/6 号）、`api_app.py:34-45` | T01 | 1.5 | U1 §12.3 验收 1–7、10–11（匿名 401、同步端点能取 user、明文旁路已关） | L1 代码回滚（新表对旧代码无影响） |
| T04 | **管理与审计**：13 条管理端点 + 自保护规则 + 限流三桶 + `tasks` IDOR 修复 + 审计查询端点 | 新 `lottery/auth_api.py`、`api_app.py:621-631`、`db.py:728-743` | T03 | 2.0 | U1 §12.3 验收 2、6、8、12–14（最后一 admin 409、限流 429、无 `DEFAULT 1`、无孤儿行且测试不依赖 PRAGMA） | admin 路径临时降为 U（一行配置） |
| T05 | **`register_prereg` 接线**（MU-0，V13 P3 子集） | `api_app.py` 调度器（`:2146/:2190` 区）、`hypothesis.py` | 无（可与 T03 并行） | 2.0* | E2E 全程无 mock（注册→判定→快照→`usable` 非空）+ 删调用点必红反向用例 + `/api/registry/status` 暴露 M/k/j/p | 判定开关回退 |
| T06 | **数据迁移 5–8**：`users`/sessions/`user_llm_config` + `predictions`/`eval_*` 整表重建 + `tasks` 加列 + `issue_baseline` + 预演 | `migrations.py`（占 5–8 号）、`db.py` SCHEMA、`cli.py`、`entrypoint.sh` | T03（users DDL 冻结）、T08（config DDL） | 2.5 | 预演行数守恒（predictions 40 / eval_details 35 / eval_meta 28 / eval_results 6 / tasks 6）；`pre-migrate-v4` 存在且 `integrity_check` ok | L2 备份回切 |
| T07 | **`db.py` 租户化**：predictions 6 函数 / eval 4 / tasks 6 加 `user_id` + `load_task` 归属校验 + 新任务 id 16 hex | `db.py`（[独占]）、`evaluate.py`（`online_check`） | T06 | 2.0 | U3 §4.10 P1–P6（A 改 model→B 票不变；A 重生成不删 B 票）；`task_id` 越界 404 | `LOTT_MULTIUSER=0` 行为开关 |
| T07b | **`api_app.py` 显式取 uid**：7 条路由（`/api/predict` GET+POST、`/api/predictions/last`、`/history`、`/api/tasks`、`/api/tasks/{id}`、`/api/config/llm`×2、`/api/llm/cost`）显式传 `user_id`；`/api/predictions/last` 的内联裸 SQL（`api_app.py:398-404`）同步改造 | `api_app.py`（[独占]，与 T03/T04 的窗口串行） | T07 | 1.0 | 7 条路由的响应随会话变化；`grep -n 'FROM predictions' lottery/api_app.py` 每条带 `user_id`；匿名/他人数据不出现 | 单提交 revert |
| T08 | **LLM 隔离**：`crypto.py` + Fernet 列 + `_budget_cache` 分键 + 配额/429 + `PUT/GET /api/user/llm-config` + `engine.py:881` 的 `copy_context` 2 行 | 新 `lottery/crypto.py`、`requirements.txt`（+1 行）、`llm_client.py:344-408`（[独占]）、`config.py:480` 区、`engine.py:881` | T03（中间件）、T06（迁移 7） | 3.5 | U2 §8 T1–T8（A 改配置不影响 B、key 零泄露、失败不扣额、未配置纯统计出号） | 按 `user_id` 精确失效缓存；行为开关 |
| T09 | **缓存与基线**：`config_fp` 三元组 `(user_id, issue, config_fp)` + `issue_baseline` L0 + 调度器只算 L0 | `engine.py`（[独占]，与 T08 错开函数域）、`db.py`（读 T07 之后） | T07、T08 | 3.0 | U3 §4.10 P2–P4（10 注请求不拿 5 注缓存；两用户同一 L0；key 不进 `config_fp`）；`grep config_fp` 逐条有 `user_id=?` | 开关回退旧缓存判据 |
| T10 | **假设归因**：`hypotheses` owner/visibility + 33 条 `legacy_untracked` + API 可见性过滤 + 负面全局可见 | `hypothesis.py`（[独占]，与 T05 错开时段）、`api_app.py` 读侧 | T03 | 1.5 | U3 §5.9 R1–R6（A 的 private 不出现在 B 列表；A 的 passed 在 B 可用；M 单调递增且等于 ledger 行数） | JSON 备份回切（L3） |
| T11 | **前端三件**：登录遮罩 + 管理面板 + 设置页/配额卡 | `web/index.html`、`web/static/app.js`（`api()` :55-56 单点 + `loadLlmConfig:3168`/`saveLlmConfig:3192`/`testLlmConnection:3233`） | T03、T04、T08 | 2.0 | 401 统一跳登录；`has_api_key` 只显示布尔 + 脱敏；`node --check` 过；公网 `?v=` MISS | `?v=` 回退即回退 |
| T12 | **总测试与灰度**：9 处 lambda 修 + 迁移用例 + T1–T8/P1–P6/R1–R6 落盘 + 灰 0→2 演练 + 回滚演练 | `tests/`、`scripts/`、运维手册 | 全部 | 3.0 | 全量 **≥1037 passed / 0 failed**；`git status data/` 为空；L1/L2 回滚各演练一次 | —（本身就是回滚） |

**合计与对账**（与 §3.1 逐块对齐，不留"大概"）：

| 归属 | 任务 | 人日 |
|---|---|---:|
| V13 P0（**不属于**多用户新增） | T01 0.5 ＋ T02 0.5 | 1.0 |
| V13 P3（**不属于**多用户新增） | T05 | 2.0* |
| ① 身份 | T03 1.5 ＋ T04 2.0 | 3.5 |
| ② LLM 隔离 | T08 | 3.5 |
| ③ 数据/迁移 | T06 2.5 ＋ T07 2.0 ＋ T07b 1.0 ＋ T09 3.0 ＋ T10 1.5 ＋ T12 3.0 | 13.0 |
| ④ 前端 | T11 | 2.0 |
| ⑤ 收尾负担 | 回滚演练 0.5 ＋ auth 自检/检查表/文档/灰度观测 0.5（分摊在各任务尾部） | 1.0 |
| **多用户新增合计** | ①＋②＋③＋④＋⑤ | **23.0** |
| 表内全部任务之和（含 V13 归属项） | — | **26.0** |

> `*` T05 的 2.0 已计入 V13 P3——V13 的 P3 因它前移而由 7.5 降为 5.5，总量不变。**不要把它再加一次**，这是最容易算重的一笔。

### 4.2 团队角色建议

**铁律**：角色可以一人多角，但**同一时间独占文件的任务只能有一个人在改**（见 §4.3）。下面两种配置都满足"真实 2 路"。

#### 配置一：3 人小队（最低可开工配置）

| 角色 | 负责任务 | 为什么这样切 |
|---|---|---|
| **A. 平台/身份后端** | T01、T02、T03、T04、T06 | T01→T03→T04→T06 是**关键路径上的串行链**，同一个人做省掉交接；`migrations.py` 占号 5/6 由他冻结 |
| **B. 数据/LLM 后端** | T05、T07、T08、T09、T10 | `hypothesis.py`（T05/T10）与 `db.py`/`llm_client.py`/`engine.py`（T07–T09）天然两路；T05 与 A 的 T03 真并行开局 |
| **C. 前端 + 测试 + 发布** | T11、T12、全程门禁执行 | 前端零冲突（`web/` 与 `lottery/` 无交集）全程并行；T12 的测试与 A/B 的实现**交叉写**（实现者不写自己的验收用例，见 §4.4 门禁 7） |

- **真并行**：A(T03) × B(T05)；A(T04) × B(T07/T08)；全程 C(T11) × A/B。
- **必须串行**：T06 → T07 → T09（同一 `db.py`）；T08 的 `engine.py:881` 与 T09 的 `engine.py` **错开函数域、错开周**（见 §4.3）。
- **Lead（或 A 兼）每周 2 小时**：周一合并窗口值班 + 周三冻结点检查（见 §4.3）。

#### 配置二：5–6 人满编（最快配置，日历可压到 28 天下限）

| 角色 | 负责任务 | 并行说明 |
|---|---|---|
| **R0 架构/Lead** | 排号、门禁、合并窗口，不直接写热点文件 | 唯一的"交通警察"；`migrations.py` 占号与 `required_role()` 枚举变更由他终审 |
| **R1 身份后端** | T03、T04 | 独占 `auth.py`/`auth_api.py`（新文件，零冲突）+ `api_app.py:34-45` 中间件窗口 |
| **R2 数据迁移** | T06、T07 | 独占 `migrations.py` + `db.py`；T06 完成前**任何人不得碰 `db.py`** |
| **R3 LLM 集成** | T08、T09 的 `engine.py` 部分 | 独占 `llm_client.py` + `crypto.py`；`engine.py` 与 R2 的 `db.py` 调用点对齐（`config_fp` 函数归属 U3，见 U3 §12-6） |
| **R4 前端** | T11 | 全程零冲突，第 1 周即可按契约（U1 §8.2 + U2 §7.1）mock 开工 |
| **R5 测试/运维** | T01、T02、T12、发布/回滚演练 | T01/T02 第 1 周交付；T12 与 R1–R3 交叉写用例；灰度观测 |

- **真并行**：R1 × R2 × R4（三路中 R4 零冲突，R1 与 R2 在 MU-1/MU-2 边界处交接一次）；R3 与 R2 在 `engine.py`↔`db.py` 接口处**每日对齐 15 分钟**。
- **必须串行**：R2(T06→T07) → R3(T09)；R1(T03) → R4(联调) → R5(灰度)。
- **第 6 人若存在**：做 P1a（A 档删除）与文档/审计脱敏复核——**只排零冲突任务**，不碰热点。

### 4.3 文件级冲突矩阵（**本节最重要**）

#### (1) 热点清单与"改动方"（谁会在什么时候改它）

| 文件 | 行数 | 会改它的任务 | 冲突性质 |
|---|---:|---|---|
| `lottery/api_app.py` | 3381 | T01(T1)、T03(中间件)、T04(IDOR)、T05(调度器)、T07/U3-3(7 处 uid)、T10(读侧过滤)、P1a/P1b(删端点)、P2(调度) | **最高危**：任何两个任务同时改 = 结构性冲突 |
| `lottery/engine.py` | 2988 | T02(`constraint_ctx`)、T08(`:881` 线程池 2 行)、T09(L0/`config_fp`)、P3(`apply_rules`)、P5(去 `:2438` 偏置) | 高危：`_llm_channel` 与 `predict_next` 是两片函数域，**可错开不可交叠** |
| `lottery/db.py` | 857 | T01(`:167`)、T04(`:728-743`)、T06(SCHEMA)、T07(全部租户化)、T09(读) | 高危：T06 完成前冻结 |
| `lottery/config.py` | 527 | T01、T08(`LLM_CONFIG_FILE` 区)、SYSTEM_USER_ID 常量 | 中危：**只允许新增常量/函数，禁止删改既有常量**（U2 §9.3） |
| `lottery/llm_client.py` | 789 | T08(`_budget_cache` 344-408 + 异常脱敏 411 区) | 中危：单任务独占 |
| `lottery/migrations.py` | ~330 | T06（占号 5–8） | **占号冲突**：必须一次性把 5/6/7/8 号在一个 PR 里登记 |
| `lottery/hypothesis.py` | ~1978 | T05、T10 | 中高：两者**同文件不同时段** |
| `web/static/app.js` | 4203 | T11、P1b | 低：前端与后端零交集 |
| `lottery/auth.py` / `auth_api.py` / `crypto.py` | 新 | T03/T04、T08 | **零冲突**：新文件是并行的最大红利 |

#### (2) 排号规则（"同一文件同一时间只能一个任务在改"）

**规则 R1 — 独占窗口制。** 每个热点文件的每次改动都要在 `docs/COORD/OWNERS.md`（新建，一行一条：`文件 | 任务ID | owner | 起 | 止 | PR#`）里登记一个**独占窗口**。窗口未关闭前，其他人的 PR 若触及该文件，**CI 直接失败**（脚本：读 OWNERS.md，比对 `git diff --name-only origin/main`，任一文件有第二个活跃窗口即 exit 1）。

**规则 R2 — 窗口粒度是"函数域"，不是"文件"。** 对 `engine.py` 这种 2988 行的文件，允许**两个窗口共享文件但列出函数白名单**：
```
engine.py | T08 | R3 | 10-12..10-14 | funcs=_llm_channel, _ticket_call
engine.py | T09 | R3 | 10-15..10-19 | funcs=predict_next, build_issue_baseline
```
窗口交叠（同一天两个活跃窗口）时**必须函数白名单不相交**，否则排队。

**规则 R3 — 谁开窗口谁合并谁关窗口。** 合并后立即在 OWNERS.md 划掉；超过 5 个工作日未关的窗口自动升级到 Lead（防止"挂着窗口不干活"变成事实锁）。

**规则 R4 — 热点文件的 PR 上限：一次 PR 只碰一个热点。** 例外：`migrations.py` 的占号 PR（它天然要一次登记 4 个号），必须由 Lead 亲自 review。

**规则 R5 — 迁徙型改动（重命名/搬函数）单独一个 PR，且必须与业务改动分开。** 历史教训（V10 F3：2 路误报 4 路）的根因就是"搬函数 + 改逻辑"混在一个 PR 里，导致 diff 不可 review、冲突不可解。

#### (3) 每周节奏（周历）

| 时点 | 动作 | 谁 | 硬约束 |
|---|---|---|---|
| **周一 10:00–12:00** | **合并窗口**：所有已 approved 的 PR 按 OWNERS.md 顺序合并到 `main`；跑一次全量 `pytest -q` 打基线 tag | Lead | 其余时间**禁止合并 `main`**（紧急回滚除外） |
| 周一 12:00 | 发放本周独占窗口（更新 OWNERS.md） | Lead | 窗口数 ≤ 活跃文件数；超发即排队 |
| **周二–周四** | 开发；每日 15 分钟站会（只说三件：窗口是否按计划关闭 / 是否要抢别人的文件 / 门禁有无红灯） | 全员 | 抢文件必须当天在 OWNERS.md 上变更并通知被抢方 |
| **周三 18:00** | **冻结点**：`main` 打 tag `w<NN>-freeze`；此时 `main` 必须全绿（0 failed）。此后到周四结束只接受 hotfix | Lead | 冻结失败 ⇒ 周五不发布（见 §4.5） |
| 周四 | 灰度窗口：把 `w<NN>-freeze` 部署到生产，观察 ≥24h | R5/值班 | 只在周四部署，**绝不在周四五点后部署**（没人看日志） |
| 周五 10:00 | **复盘 + 回滚窗口检查**：上一周灰度是否异常；备份是否最新；OWNERS.md 是否归零 | 全员 | 有未关闭窗口 ⇒ 不得发版 |
| 每期开奖前 2 小时 | **静默期**：不合并、不部署、不跑重活（避免和抓奖/评估抢资源与造成"开奖日故障"） | 全员 | 违反视为事故 |

#### (4) 12 个任务 → 文件占用甘特（示意，周为单位，2 路）

```
文件 / 周 →       W1        W2        W3        W4        W5        W6        W7
api_app.py        T01,T03   T04       ---       T07(uid)  T10        P1b       P2
migrations.py     ---       T06(5-8)  ---       ---       ---        ---       ---
db.py             T01       T06       T07       T07,T09   ---        ---       P2
llm_client.py     ---       ---       T08       ---       ---        ---       P5
config.py         W1(sys_id) ---      T08        ---       ---        ---       P2
engine.py         T02       ---       T08(881)  T09       ---        P3        P5
hypothesis.py     ---       T05       ---       ---       T10        P3        P4
web/*             ---       T03契约    T11       T11       P1b        ---       ---
（路数）           2         2         2         2         2          2         2
```

> 注：W1 的 `api_app.py` 由 T01 与 T03 先后占用（T01 是 4 行配置级，T03 是中间件 12→50 行），二者**同一人（A/R1）串行**，不计为两路。

### 4.4 PR 验收门禁（每个 PR 必过，缺一条即拒）

| # | 门禁项 | 判定方式（**可机检，不靠人看**） |
|---|---|---|
| 1 | **全量 pytest 绿** | `pytest -q` 必须 **0 failed**，且 `passed ≥ 1037`（基线只增不减；当前 1036+1failed，P0 修完即 1037/0）。**禁止只跑单文件**（V10 G8 字母序巧合全绿） |
| 2 | **测试不写生产数据** | `git status --porcelain data/` **必须为空**（V13 §八通用基线） |
| 3 | **无新增 `DEFAULT 1`** | `git diff origin/main | grep -E '^\+.*DEFAULT +1\b'` 为空（裁决 3/4）。**豁免**：`token_version ... DEFAULT 1`（版本计数起点） |
| 4 | **无 `user_id = 1` 字面量** | `git diff | grep -E '^\+.*user_id\s*=\s*1\b'` 为空；admin 只用 `is_primary_admin = 1` 或 `username=? AND role='admin'` 定位（裁决 3） |
| 5 | **无 `DEFAULT 1` 之外的诊断**：新增调用 `load_predictions`/`save_predictions`/`list_tasks` 的文件必须传 `user_id` | 新增一个 AST 小检查脚本 `scripts/check_tenant_calls.py`（首次由 T07 提供） |
| 6 | **密钥不落日志/响应** | `git diff | grep -Ei 'api_key|LOTT_SECRET_KEY'` 的每一行必须走 `mask_api_key`/`sanitize_error_message`；新增测试必须断言异常消息内无 `sk-[A-Za-z0-9]{8,}`（U2 §8-T2） |
| 7 | **实现者不写自己的验收用例** | 规则：T03/T04/T06/T07/T08/T09 的验收用例由 **R5/测试角色**独立写（与 U3 §8 的"测试影响清单"配合） |
| 8 | **新增路由必须定级** | 参数化测试遍历 `app.routes`，每条在 `required_role()` 有明确返回值（U1 §7.3 纪律 2、§12.3 验收 1） |
| 9 | **删/合端点必须同步三处** | `required_role()` 枚举、`web/static/app.js` 调用点、`docs/API_OPS_ONLY.md` allowlist（M4 §10.2 断言 1） |
| 10 | **迁移 PR 额外门禁** | ① 占号表一致（5=users/6=sessions/7=config/8=租户化）；② 每张表**逐表幂等闸**（不能整函数 `return`，U3 §7.2 踩坑预告）；③ 行数守恒守卫；④ 预演脚本跑过并贴输出；⑤ 不并行跑任何写库任务 |
| 11 | **孤儿行测试** | 任何触及用户删除的 PR 必须含"删用户后 `user_sessions`/`user_llm_config` 行数=0、`predictions` 历史行仍在，且测试内**不执行** `PRAGMA foreign_keys=ON`"（裁决 4，U1 §12.3 验收 13） |
| 12 | **前端** | `node --check web/static/app.js` 过；`?v=` 已升；公网刷新后 `x-cache-status: MISS`（V13 §八） |
| 13 | **工作日志** | PR 描述必须含：触及的 OWNERS 窗口 ID、回滚方式（一句命令）、影响端点清单 |

### 4.5 分支与发布策略（单节点不中断发布）

#### (1) 分支模型

```
main ──●────●────●────●──（只接受 Monday 合并窗口的 PR；每次合并打 tag w<NN>）
        ╲    ╲    ╲
         task/T03-identity   ← 一格一号，窗口与 OWNERS.md 绑定
         task/T08-llm-isolation
hotfix/<issue>  ← 只允许从最近 tag 拉出，当天发版
release/w<NN>  ← 冻结 tag 的可发布快照（= main 在周三冻结点的 SHA）
```

- **不做 long-lived develop**：单节点、2 路并行，多一层 develop 只会多一次合并。
- **热点文件禁止跨窗口 rebase**：窗口开着时，先 `git merge origin/main` 一次，之后每天早上一 merge，不 rebase（rebase 会让别人本地的 patch 全部失效）。

#### (2) 为什么"不中断"在本系统是可行的（先说清边界）

诚实结论：**单容器单进程，无法做到"零中断"**（V13 §3-C1：进程级重启约 2 分钟窗口）。本方案承诺的是 **"零失败发布 + 秒级回滚"**，而不是零秒中断：

| 机制 | 现状 | 作用 |
|---|---|---|
| `restart: unless-stopped` | 已具备（`docker-compose.yml`） | 崩了自动拉起 |
| `entrypoint.sh` 顺序执行 `fetch`/`backtest`/启动 uvicorn | 已具备 | 迁移在启动早期执行（`db.get_conn()` 懒加载，U1 §9.5） |
| 迁移自动热备 `ssq.db.pre-migrate-v<n>` + 失败 fail-closed | 已具备（`migrations.py:213-252/296-321`） | 回滚有物可回 |
| `ssq-backup` 每日 03:17 + 保留 14 份 | 已具备（`scripts/ssq_backup.sh`） | 全量 `data/` 归档 |
| 发布放在低峰（周四 + 非开奖窗口 2h） | 新增纪律 | 把 2 分钟窗口放在几乎无人访问的时段 |
| 健康探针 `curl /api/health` | 已具备（容器 healthcheck） | 判定发布是否成功 |

#### (3) 发布步骤（8 步，逐条可执行）

```bash
cd /root/DSH/应用/ssqyuce
# 0) 前置：确认不在静默期（非开奖前 2h / 非 03:17 备份窗口），确认 main 全绿 tag
git fetch && git log --oneline -1 origin/main        # 记录发布 SHA
# 1) 手工全量备份（L3，U3 §7.4 明确要求：迁移前必须人工跑一次，因为 L1 只保 DB）
sudo ssq-backup backup && sudo ssq-backup verify
# 2) 记录回滚锚点
echo "$(git rev-parse HEAD)" > /tmp/rollback_sha && docker image tag ssqyuce-app:latest ssqyuce-app:prev
# 3) 构建（不重启）
docker compose build
# 4) 重建（ports 变更才需要重建；普通代码变更用 up -d 即可）
docker compose up -d
# 5) 等健康（最多 120s；迁移 + entrypoint 的 fetch/backtest 也在其中）
for i in $(seq 1 40); do curl -fsS localhost:18000/api/health && break; sleep 3; done
# 6) 五项冒烟（缺一不可）
curl -fsS localhost:18000/api/health | grep -q schema_version
sqlite3 data/ssq.db "select max(version) from schema_version;"      # 期望 5..8 中预期值
ls -l data/ssq.db.pre-migrate-*                                     # 期望新增本版备份
curl -s -o /dev/null -w '%{http_code}\n' localhost:18000/api/predictions/last   # 未登录应 401
curl -s -o /dev/null -w '%{http_code}\n' https://ssq.ff88.de/docs               # 应 404
# 7) 观察 24h：/api/predict p95、429 率、容器 restart count、llm_cost_log 是否带 user_id
docker stats --no-stream ssq-predictor; docker inspect -f '{{.RestartCount}}' ssq-predictor
# 8) 通过 → 关闭本周 OWNERS 窗口；失败 → 执行下方回滚
```

#### (4) 回滚步骤（三层，按代价从低到高；**先回滚再排查**）

| 层 | 触发条件 | 命令（逐条） | 代价 |
|---|---|---|---|
| **L1 代码回滚（首选）** | 应用 5xx、接口异常、鉴权误伤 | `git checkout $(cat /tmp/rollback_sha)` → `docker compose up -d --build`；**不动数据库** | 秒级～2 分钟；**零数据损失**（v5–v8 是纯增量 + 应用层显式语义，旧代码不认识新表也不报错，U1 §9.4 L1） |
| **L2 数据库回滚** | 迁移本身有结构缺陷（列错、行数不守恒） | `docker compose stop` → `cp data/ssq.db.pre-migrate-v4 data/ssq.db`（**若已开 WAL，还需删 `-wal`/`-shm`**，U3 §7.3）→ `docker compose up -d` | 丢失 v5 之后的全部新数据（新用户/会话/审计）；历史 3516 期无损 |
| **L3 全量恢复** | 灾难（库损坏、误删表） | `sudo ssq-backup restore --to /tmp/restore` → 校验行数 → 停容器 → 替换 `data/` → 起容器 | 丢失到最后一次 03:17 归档；**必须有演练记录才允许执行**（U3 §7.4） |
| **行为回滚（与 L1 正交）** | 新逻辑有问题但结构正常 | `LOTT_MULTIUSER=0`（单租户语义）/ `LOTT_MINING=0` / 单 LLM 岗位开关 → `docker compose up -d` | 0 数据损失；**这是灰度期的主用手段** |

**回滚演练是发布的前置条件**：每个涉及迁移的版本，发布前必须在 `/tmp/rehearse/` 上做一次 L1 + L2 演练并贴输出（U1 §9.4 演练清单）。

---

## 5. (E) 风险登记册：多用户特有 10 条

> 与 V13 §七（R1–R8，系统级）**不重复**；这里只登记"因多用户而新出现或性质改变"的风险。
> 概率/影响取值：高/中/低 × 灾难/高/中/低。**触发信号都必须是可观测指标**（有命令或端点可查）。

| # | 风险 | 概率/影响 | 缓解（落在哪个任务） | 触发信号（可观测） |
|---|---|---|---|---|
| **MU-R1** | **越权 / IDOR**：`GET /api/tasks` 返回全站任务（`db.py:740-743` 无 WHERE）、`/api/tasks/{id}` 无属主校验、`/api/config/llm` 全局单例（垂直越权） | 高 / **高** | T04：`WHERE user_id=?` 进 SQL（不是取出来再判）、越界统一 **404**（不返 403，避免存在性探测）；T07：`load_task(..., user_id=)`；T03：24 条 admin 端点由中间件前缀兜住 | `user_audit_log` 里 `authz.denied` 1 分钟内 >20 条（U1 §4.3：#1 误操作、#20 在探路）；`GET /api/tasks` 响应里出现非本会话 `user_id` 的任务（灰 2 期每天 grep 一次） |
| **MU-R2** | **API key 泄露**：日志、异常栈、响应、审计 `detail_json`、备份文件 | 中 / **灾难** | T08：Fernet 加密列 + `mask_api_key` 只回 `sk-****abcd` + `sanitize_error_message`（U2 §4.4 四道防线）+ `audit()` 内置兜底脱敏；T12：`grep -r 'sk-[A-Za-z0-9]\{8,\}' data/*.jsonl data/*.json logs/` 纳入门禁 | 全仓 grep 命中任何 `sk-` 明文；`llm_cost_log.jsonl` 出现 base_url/key 字段；异常响应体含 `Authorization` |
| **MU-R3** | **预算被刷 / 单用户耗尽公共配额** | 高 / 中 | T08：每用户日配额（默认 50,000 tokens）、单用户并发=1、2 QPS、429 带 `Retry-After` 与 `can_fallback_to_stat`（U2 §5.2）；公共通道与管理员的 key **物理分离**（`use_custom_key=1` 才走自备） | 单用户单日 `today_tokens_used` 触顶次数 >3；429 率 >5%；`llm_cost_log` 按 user 聚合后 Top1 占比 >60% |
| **MU-R4** | **数据串号**：A 的票/评估/任务出现在 B 的界面；或 A 重新生成时 `DELETE FROM predictions WHERE issue=?`（`db.py:353-369` 现状）把 B 的票删掉 | 中 / **高** | T07：所有 predictions 写路径先按 `user_id` 收敛；T09：缓存键三元组 `(user_id, issue, config_fp)`，**禁止跨用户比较 `config_fp`**（U3 §4.6 纪律 2 + 裁决 5）；T12：U3 §4.10 P6「A 重生成不删 B 的票」 | 对账 SQL 每日跑：`SELECT issue,count(DISTINCT user_id) FROM predictions GROUP BY issue` 与审计不一致；用户投诉"票变了"；两用户同期响应 `target_draw` 不同 |
| **MU-R5** | **迁移失败 / 表重建中途失败**（`predictions`/`eval_*` 需整表重建，SQLite 不支持改主键） | 中 / **灾难** | T06：行数守恒守卫 + **逐表幂等闸**（不能整函数 `return`，U3 §7.2）+ 预演（`/tmp/rehearse`）+ `pre-migrate-v4` 热备 + fail-closed；发布步骤强制先手工 `ssq-backup backup` | 迁移后行数 ≠ 40/35/28/6/6；`pre-migrate-v4` 缺失；`integrity_check` 非 ok；容器 restart count 上涨 |
| **MU-R6** | **session 劫持 / 固定** | 中 / 高 | T03：sid 只由服务端 `secrets.token_urlsafe(32)` 生成、**登录成功换 sid**、`__Host-` 前缀 + `HttpOnly` + `Secure` + `SameSite=Lax` + 无 `Domain`；登出 = 服务端 `revoked_at` + 客户端过期；T04：改密/踢人 `token_version+1` | 同一 sid 短时间出现在 ≥2 个不同 `X-Real-IP`；`session.revoke` 后仍能 200；Cookie 出现在 URL 或 Referer |
| **MU-R7** | **明文旁路 + 限流绕过**（若 T01 未完成就上鉴权） | 中 / **高** | T01 与 T03 **同批上线、不可分割**（裁决 5）：端口 `127.0.0.1` + 中间件记 `authz.insecure_transport` | `ss -tlnp` 出现 `0.0.0.0:18000`；`/api/health` 的 `insecure_transport=true`；同一账号从"多个 IP"高频登录（= 伪造 `X-Real-IP` 的指纹） |
| **MU-R8** | **账号被锁死**（误删最后一个 admin / 禁用自己 / `bootstrap` 已 410 后无人能管） | 中 / **高** | T04 自保护四条（删自己 409、最后一个 admin 409、禁用自己 409；"最后一个 admin"检查与写操作在同一个 `BEGIN IMMEDIATE` 事务内防 TOCTOU，U1 §8.4）；CLI 引导兜底 `python -m lottery.cli create-admin` | admin 数量 = 0；`/api/health` 的 `auth.admins == 0`；`bootstrap` 已 410 但 `users` 里没有 active admin |
| **MU-R9** | **contextvars 串号 / 丢失**：线程池复用残留导致 B 的请求看到 A；后台线程拿不到 user 导致写到系统主体或写错人 | 中 / 高 | T03：中间件 `finally` 必须 `_current_user.set(None)`；T08：`engine.py:881` 用 `copy_context().run` 包装（裁决 7）；T07/U3-3：`api_app.py:604`/`:2848` 显式传 `user_id` 闭包参数；调度器 `:2387` 用 `SYSTEM_USER_ID=0` | 并发 A/B 交替请求 200 次出现交叉响应（U1 §12.3 验收 4）；`predictions` 中出现本该属于某人的票落到 `user_id=0`；日志出现 `[tenant] 无法解析租户` 告警 |
| **MU-R10** | **测试假绿 / 基线漂移**（多用户改造新增大量跨模块测试；`conftest.py` 的 DB 重定向与"真实库守卫"存在**收集期 vs 执行期**时序坑——真值锁就是活标本） | 中 / **高** | 门禁 1/2（全量 + `git status data/` 为空）；门禁 7（实现者不写自己验收用例）；T12 独立写 U1 §12.3 的 15 条与 U3 §4.10/§5.9 | 全量 vs 单文件结果不一致；baseline `passed` 下降且无解释；`data/` 出现新文件（`ssq.db-journal`、测试用户会话） |

**残余风险（明确不做缓解，登记以免被追问）**：① 同站子域——15 个邻居容器共享 `*.ff88.de`，某邻居被攻破可注入同源内容，靠 `__Host-` Cookie + CSRF Token 兜（U1 §10.7 #1）；② 宿主被 root 拿下 ⇒ 全部数据（含备份里的口令哈希与 key 密文）暴露，符合本机威胁模型（U1 §10.7 #6）；③ `app.js` 4203 行里的 `innerHTML` XSS 面**尚未评估**（U1 §12.4 #3），不在本轮范围，但 P1b 前端改造时必须同步登记。

---

## 6. (F) 第一周开工清单（8 条，每条可直接派一个人）

> 派单原则：第 1 周就把"不可分割的 P0"关门（W01 先行），让后面所有 PR 都跑在绿色基线与安全网络上。验收一律可机检。

| # | 任务 | 派给 | 文件范围 | 人日 | 验收（可机检） |
|---|---|---|---:|---|---|
| **W01** | **P0 部署前置 4 项一次关门**：① `docker-compose.yml:11` → `127.0.0.1:18000:18000`（`up -d` 必须**重建**，不能 `restart`）；② `FastAPI(docs_url=None, redoc_url=None, openapi_url=None)`；③ 删 CORS 中间件（同源部署不需要）或收紧为 `https://ssq.ff88.de`；④ `.env` 设 `LOTT_TOKEN`（临时，MU-1 上线后作废）；⑤ `db.py:167` 加 `PRAGMA busy_timeout=5000` | A / R5 | `docker-compose.yml`、`api_app.py:28-32`、`.env`、`db.py:160-174` | 0.5 | 公网 `curl http://89.106.64.40:18000/api/health` **超时/拒绝**；`ss -tlnp \| grep 18000` 显示 `127.0.0.1`；`/docs`,`/redoc`,`/openapi.json` 全 **404**；响应头无 `access-control-allow-origin: *`；未带 token 请求 `/api/predictions/last` 为 **401** |
| **W02** | **真值锁修复 → 基线归零**：① `tests/conftest.py` 守卫改**执行期求值**（不在收集期）；② `constraint_ctx` 对空/短输入返回明确降级值或抛 `InsufficientDataError`（不让 `IndexError` 抛穿，V13 §4.1） | B | `tests/conftest.py`、`tests/test_v12_shape_lock.py`、`engine.py`（`constraint_ctx` 函数域） | 0.5 | `pytest -q` = **1037 passed / 0 failed**；新增"空输入不抛 `IndexError`"单测；`git status data/` 为空 |
| **W03** | **`auth.py` 骨架**：`hash_password`/`verify_password`（`hashlib.scrypt`，`n=2^15,r=8,p=1`，**必须显式 `maxmem=64MB`**，否则 ≥2^15 直接 `ValueError`）+ `BoundedSemaphore(4)` + PHC 串 + `validate_password` 纯函数 + 会话创建/校验/撤销 + `client_ip()` 只信 `X-Real-IP` | A / R1 | 新 `lottery/auth.py`（+ `tests/test_auth_unit.py`） | 1.0 | PHC 前缀断言 `scrypt$n=32768`；20 并发登录 `docker stats` 不 OOMKilled；比较用 `hmac.compare_digest`；不存在的用户也跑一次 dummy hash（消时序差） |
| **W04** | **迁移 5/6 占号 + 建表**：`users`（含 `is_primary_admin`；`status` CHECK **必须含 `'bootstrap'`**，否则 U3 §12-2 的种入直接违反 CHECK）、`user_sessions`、索引；`MIGRATIONS` **一次登记 5–8 号**（7/8 先留空实现，防撞号） | A / R2 | `migrations.py`（唯一修改者）、`db.py` SCHEMA **保持冻结**（新表只放迁移） | 0.5 | 预演库 `schema_version=6`（最终 8）；`uq_users_primary_admin` 唯一索引存在；迁移 PR 由 Lead 亲审；旧代码（回滚 SHA）能正常读写（L1 演练） |
| **W05** | **中间件替换 + 5 条 auth 端点**：把 `api_app.py:34-45` 的 fail-open 12 行换成 deny-by-default（`_PUBLIC_EXACT={"/","/api/health","/api/auth/*"}`、`/api/admin/` 前缀兜 admin）+ `contextvars` 注入与 `finally` 清理 + 一次性引导（`LOTT_BOOTSTRAP_TOKEN`；**先判"已有 admin"再判 token**，已有则 410） | A / R1 | `api_app.py:34-45`（[独占] 窗口）、新 `lottery/auth_api.py`、`config.py`（+`SYSTEM_USER_ID=0`，**只加不删**） | 1.5 | U1 §12.3 验收 1–7、10–11；同步 `def` 端点内 `_current_user.get()` 非空；匿名打 24 条 admin 端点全 401、普通用户全 403；引导二次调用 410 |
| **W06** | **`register_prereg` 接线（MU-0）**：调度器在 `register_hypotheses` 之后、**读本期结果之前**调用，**只传 `source=="stat"`**；单批 <10 条走跨期累积（`counters.batches` 已有）；33 条历史置 `legacy_untracked` | B | `api_app.py` 调度器函数体（`:2146/:2190` 区，与 W05 的 `:34-45` 不相交）、`hypothesis.py`、`tests/test_v10_registry.py` | 2.0（跨周） | E2E：真实注册→到期判定→`build_snapshot`→`usable` 非空，**全程不 mock**；**删掉调用点该用例必红**；`/api/registry/status` 暴露 `{n_tested_cumulative, n_registered, n_judged, n_passed}`，且"有假设但 M=0"时健康检查 `degraded` |
| **W07** | **P1a：A 档 7 项删除（80→73）**：A1 `/api/draws/latest`、A2 `/api/notify/status`（顺带脱敏 `email_to`）、A3 `/api/eval/meta`、A4 `/api/mining/latest`、A5 `/api/llm_lab/run`(同步)、A6 `/api/rules/closed-loop`(同步)、A7 `/api/eval/coverage` | 第 6 人 / C | `api_app.py`（**窗口与 W05 串行**）、`web/static/app.js`（若有引用） | 1.0 | `grep -cE '^@app\.(get\|post)' lottery/api_app.py` = **73**；"破 N 测试"清零（M4 §9 第一批）；`required_role()` 枚举同步移除；无孤儿路由（grep 零引用） |
| **W08** | **前端契约先行（mock 开工）+ 门禁脚本**：① `api()` 单点改造（`credentials:'same-origin'`、非 GET 加 `X-CSRF-Token`、**401 统一跳登录**），对着 W03/W05 契约先接 mock；② 落地 `scripts/check_tenant_calls.py` 与 OWNERS.md 窗口检查脚本并接入 CI | C / R4 | `web/static/app.js:55-56`、`web/index.html`（登录遮罩骨架）、`scripts/`、`docs/COORD/OWNERS.md` | 1.0 | `node --check web/static/app.js` 过；`?v=` 已升；门禁脚本对"同文件双窗口"能 exit 1（用假 PR 验证）；401 分支有 stub 验证 |

**第一周终态（DoD）**：基线 **1037 / 0 failed**；公网 `:18000` 不可直连、`/docs` 404；`users` 表与 5 条 auth 端点可用、首个 admin 已引导；`register_prereg` 有生产调用者且 E2E 双测绿；端点 80→73；门禁脚本进 CI。**此时才允许进入第二周（MU-2：迁移 5–8 + 租户化 + LLM 隔离，16.5 人日，日历炸弹）**。

---

## 7. 附：本文与其他文档的关系（防止重复劳动）

| 文档 | 本文引用它什么 | 本文**不**重复什么 |
|---|---|---|
| `V13_LEAD_VERDICT.md` | 四点判定、P0–P6、31 人日、真实并行 2 路、MVT 三件、§七 R1–R8 | 不重新论证"是否能做" |
| `U1_IDENTITY.md` | 认证选型、三张表 DDL、80 端点权限分级、引导流程、§12.3 验收 15 条 | 不重复 scrypt 参数推导、中间件代码、攻击面逐条分析 |
| `U2_LLM_ISOLATION.md` | `user_llm_config` DDL、Fernet、`_budget_cache` 分键、配额/429、T1–T8 | 不重复 contextvars 传播实测与线程池陷阱代码 |
| `U3_DATA_MODEL.md` | 共享/私有判据、迁移 5–8 占号与重建脚本、α 台账、`config_fp`、P1–P6/R1–R6 | 不重复 SQLite 重建 12 步、逐表判定论证、ER 图 |
| `M4_SIMPLIFY.md` | A/B/C 三档、目标形态（80→73→66）、§10.2 断言 | 不重复端点证据矩阵 |
| `M5_DELIVERY.md` | 依赖链顺序理由、风险登记册与阶段验收的格式 | 不重复纠偏 C1–C6 |
| **本文增量** | §1 逐点重定级 / §2 插入位置与阶段序列 / §3 修订工期与口径澄清 / §4 分工＋冲突矩阵＋门禁＋发布 / §5 多用户风险 / §6 第一周派单 | — |

---

*本文为只读整合评审产出，未修改任何业务代码、配置或部署文件。所有"实测"数字均来自 V13/U1/U2/U3 的独立实测与 Lead 复算；本文只做口径对齐与算术核对。*
