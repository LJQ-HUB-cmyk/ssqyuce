# U1 身份与权限架构：多用户认证、角色、管理员增删用户

> **评审对象**：`ssqyuce`（Python 3.11 + FastAPI + SQLite，单容器 `ssq-predictor`，公网 `https://ssq.ff88.de`）
> **评审视角**：身份与访问管理（IAM）架构。**只读评审**，除本文件外未修改任何代码/配置。
> **需求原文**：「改为多用户使用，每个用户的 llm 配置隔离，管理员可增加用户。」
> **本文范围**：认证、会话、角色、权限矩阵、首次引导、管理员 API、迁移、攻击面，以及与第 1 点模块化 / U2（LLM 隔离）/ U3（数据模型）的接口约定。**LLM 配置存储细节见 U2，表归属见 U3，本文只定义身份侧契约。**
>
> **证据口径**：仓库实地核查（`read`/`grep`/`wc`）+ 主机与容器实测（`ss -tlnp`、`docker inspect`、容器内 `/proc/net/tcp`、`curl` 状态码、内存内 FastAPI 复现实验）。所有 `文件:行号` 均取自当前仓库状态。
>
> **硬事实来源**：§1.1–§1.3 的 5 条基线由 Lead 实测提供（零用户概念 / 80 端点 / 79 同步 def / 单连接 SQLite / schema_version=4），本文只做**复核引用**，不重复调研；§1.4–§1.6 是本轮**新增实测**。

---

## 0. 一句话结论

**多用户改造的技术难点不在"加登录"，而在三件事：① 现有 80 个端点里 79 个是同步 `def`，鉴权若按"每个端点加一个 `Depends`"落地就是 79 处不可审计的散弹式改动；② 唯一的鉴权开关（`LOTT_TOKEN`）在生产**根本没有配**（§1.2 实测匿名 200），说明"鉴权已具备、只是没开"是假的；③ 端口 `0.0.0.0:18000` 明文直连公网可用（§1.3 实测 200），任何只做在 nginx 层的鉴权都是纸糊的。**

推荐方案：**服务端 session（DB 表 + `HttpOnly/SameSite=Lax/Secure` Cookie）+ Python 标准库 `hashlib.scrypt` 做密码哈希 + 一个 `Deny-by-default` 的鉴权中间件 + `contextvars` 透传用户上下文**。这样：

- **密码哈希部分新增第三方依赖 0 个**（scrypt 在标准库，`hashlib.scrypt` 实测可用）；**全项目**仅因 U2 的 API Key **可逆加密**新增 `cryptography` 一个依赖——**密码哈希是单向问题、API Key 是可逆问题，不能用同一套解法**，`cryptography` 不能省（Lead 裁决 6，见 §3.6）；
- **80 个 HTTP 端点的函数签名一行不用动**（身份解析外提为模块级 `_resolve_session_user_id`，见 §7.3）；
  **但 `db.py` 的 7 个 user-scoped 函数必须加 `user_id` 显式参数**——租户传递**不用隐式 contextvar**（裸 `threading.Thread` 不继承它，丢失后会静默写错人且测试不红），contextvar 只留给 `llm_client` 这类改不了签名的深层库（§7.2.1）；
- 需要**替换的中间件 1 处**、**新增端点 13 条**、**既有端点里仅 7 条路由需显式取 `user_id`**（明细见 §7.6），加上 2~3 个新文件、1 个迁移；
- 鉴权属于**独立横切层（视图层的门禁）**，不属于"视图层内的每个视图"，这是与第 1 点模块化的接口边界（§11）。

**必须先修的部署前提（否则整套鉴权形同虚设）**：`docker-compose.yml:11` 改 `127.0.0.1:18000:18000`、关闭 `/docs` `/redoc` `/openapi.json`、收紧 CORS。三项都是 1 行级改动，但属于**上线阻塞项**（§1.3、§10.5）。

> **⚠️ 端口绑定修复是不可分割的 P0**（Lead 裁决 5）：`127.0.0.1:18000:18000` 与"限流读 `X-Real-IP`"**必须同一批上线**。若先上鉴权再改端口，攻击者可直连 `18000` 并任意伪造 `X-Real-IP` → **登录限流形同不存在**（§10.6）。这条不是优化项，是功能成立的前提。

---

## 1. 现状：为什么"多用户"不是加个登录框

### 1.1 零用户概念，唯一鉴权是一把可选全局 token（复核）

```python
# api_app.py:34-45
@app.middleware("http")
async def optional_api_auth(request: Request, call_next):
    """LOTT_TOKEN 配置后保护 /api/*；静态前端和健康探活保持可加载。"""
    if config.API_TOKEN and request.url.path.startswith("/api/"):
        auth = request.headers.get("authorization", "")
        expected = f"Bearer {config.API_TOKEN}"
        if not hmac.compare_digest(auth, expected):
            return JSONResponse({"ok": False, "error": "需要 Bearer Token"}, status_code=401,
                                headers={"WWW-Authenticate": "Bearer"})
    return await call_next(request)
```

`config.API_TOKEN` 来自 `config.py:20`（`LOTT_TOKEN` 环境变量，空值 → `None`）。语义是**"配了才校验，不配就全放行"**——fail-open。`/api/info` 还会把开关状态直接告诉调用方（`api_app.py:86` `"api_auth_enabled": bool(config.API_TOKEN)`），等于告诉攻击者"这台机器没设防"。

### 1.2 实测：生产环境的 80 个端点**当前全部匿名可访问**

| 探测 | 结果 | 证据 |
|---|---|---|
| `.env` 是否有 `LOTT_TOKEN` | **0 行**（未配置） | `grep -c "^LOTT_TOKEN" .env` → `0` |
| 容器内实际值 | `LOTT_TOKEN=[]`（空） | `docker exec ssq-predictor sh -c 'echo $LOTT_TOKEN'` |
| `GET /api/config/llm`（读全局 LLM 配置，含 base_url/model） | **200** | `curl -s -o /dev/null -w '%{http_code}'` |
| `GET /docs` | **200** | 同上 |
| `GET /openapi.json` | **200** | 同上 |

这不是"配置疏漏"，而是当前设计的必然结果：**单 token 模型下，一旦开启就必须让每个用户的浏览器都持有同一把 token，那就等于没有用户隔离**。所以"多用户"不是给现有 token 换个壳，是替换认证模型。

### 1.3 新增实测：三条部署层暴露面（多用户后全部升级为高危）

#### (a) `18000` 明文直连公网 —— 已实测可达

```
$ ss -tlnp | grep 18000
LISTEN 0 8192 0.0.0.0:18000 0.0.0.0:* users:(("docker-proxy",pid=486559,fd=4))
LISTEN 0 8192    [::]:18000    [::]:* users:(("docker-proxy",pid=486565,fd=4))

$ docker ps --format '{{.Names}}\t{{.Ports}}'
ssq-predictor  0.0.0.0:18000->18000/tcp, :::18000->18000/tcp

$ curl -s -o /dev/null -w '%{http_code}' http://89.106.64.40:18000/api/health
200
```

**根因**：`docker-compose.yml:11` 是 `- "18000:18000"`（双向绑定所有网卡），而 nginx 的上游是 `127.0.0.1:18000`（`/etc/nginx/sites-available/ssq` 的 `upstream ssq_app`）。宿主上其余容器（`outline` `pentagi` `pocket-id` `scraper` `pgvector` `hbbs` …）**普遍已经是 `127.0.0.1:<port>:<port>` 绑定**，只有 `ssq-predictor`（和 `hindsight-app`）例外——这是本机部署规范的**已存在惯例**，改回来不引入新范式。

**这条在多用户下的准确定性**（不要夸大成"Cookie 鉴权失效"，要精确）：

| 风险 | 是否成立 | 说明 |
|---|---|---|
| 会话鉴权被绕过 | **否** | 直连 :18000 走同一份应用代码，缺 Cookie 一样 401。**真正的问题是下面三条。** |
| 明文传输登录口令 | **是，高危** | 攻击者在链路上抓 `POST /api/auth/login` 即得明文口令。`Secure` Cookie 只能阻止浏览器通过 http 发 Cookie，**阻止不了人在 http 上提交密码**。 |
| 限流按 IP 失效 | **是** | §10.6 详述：容器内看到的源 IP 恒为 `172.18.0.1`，只能依赖 `X-Real-IP`；直连时该头可任意伪造 → 每次换一个假 IP 即可绕过登录限流。 |
| 未来任何 nginx 层鉴权（Basic/IP 白名单/WAF）失效 | **是** | 旁路不经过 nginx，规则一律不生效。 |

**缓解（部署层修复，与鉴权中间件双保险）**：

```yaml
# docker-compose.yml:10-11
    ports:
      - "127.0.0.1:18000:18000"      # 只允许宿主 nginx 访问；外网无法直连
```

- 改完 `docker compose up -d` 重建容器（**注意**：`ports` 变更必须重建，不能 `restart`）。
- 双保险：应用侧仍强制要求会话（不因为"反正在内网"而放宽）；nginx 侧可选加 `limit_req` 与 `IP 白名单`。
- 若将来需要保留真实客户端 IP 到容器内，正确做法不是开放公网端口，而是 `network_mode: host` 或 docker daemon 设 `"userland-proxy": false`（当前 `docker-proxy` 在跑，说明 userland proxy 开启，见 §10.6）。

#### (b) `/docs` `/redoc` `/openapi.json` 不在 `/api/` 前缀下

```python
# api_app.py:28  默认 docs_url="/openapi.json"... 均为默认值，未显式关闭
app = FastAPI(title="双色球智能预测分析系统", version=config.APP_VERSION)
```

实测 `GET /docs`、`GET /openapi.json` 均 **200**。现有中间件的判据是 `request.url.path.startswith("/api/")`（`api_app.py:37`），因此**三个文档端点天然绕过**。多用户后这等于把含管理员端点的完整 schema 免费送给攻击者（`/api/admin/users` 的参数、字段全在 schema 里）。

**缓解**：`FastAPI(docs_url=None, redoc_url=None, openapi_url=None)`（生产），或中间件改为 **deny-by-default + 白名单**（§7.3 推荐的正是后者，顺带解决这条）。

#### (c) CORS `allow_origins=["*"]`

```python
# api_app.py:30-32
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)
```

当前无 Cookie，`*` 的实际危害有限（无凭据可带）。**引入 Cookie 后必须收紧**：`allow_origins` 改为 `["https://ssq.ff88.de"]`，且**不要**设 `allow_credentials=True` + `*`（浏览器会直接拒绝这种组合，Starlette 会返回 `*` 而不发 `Access-Control-Allow-Credentials`，表现为前端莫名 401）。同源部署（前端由同一 FastAPI 的 `/` 与 `/static` 提供，`api_app.py:50`、`:2138`）**根本不需要 CORS**——最省事且最安全的选择是：生产环境直接删掉这个中间件。

### 1.4 新增实测：容器内看到的客户端 IP 恒为 `172.18.0.1`

这是设计限流前必须知道的事实。

```
# 容器内 /proc/net/tcp 的 ESTABLISHED 记录（从宿主 127.0.0.1:18000 发起连接）
020012AC:4650  010012AC:9744
      ↑本地               ↑远端
020012AC = 172.18.0.1 的反序   → 容器看到的对端是 docker 网桥网关
```

`docker inspect ssq-predictor` → `NetworkMode=ssqyuce_default`，`Gateway=172.18.0.1`。

**推论**：
1. `request.client.host` 在容器内**永远是 `172.18.0.1`**（不论请求来自公网、nginx 还是宿主本机）；
2. `cli.py:93` 的 `uvicorn.run("lottery.api_app:app", host=..., port=..., log_level="info")` **没传** `forwarded_allow_ips`，uvicorn 默认 `forwarded_allow_ips="127.0.0.1"`；对端是 `172.18.0.1` → **不被信任 → uvicorn 不会用 `X-Forwarded-For` 覆盖 `client.host`**；
3. 因此**应用层必须自己读 `X-Real-IP`**，且只在"对端 == 可信网关"时才认这个头。

**nginx 侧已具备条件**（`/etc/nginx/sites-available/ssq`，`location /`）：

```nginx
proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
```

- `X-Real-IP` 由 nginx **覆盖写**（`$remote_addr`），不可被客户端伪造 → **这是唯一可信的取值**。
- `X-Forwarded-For` 用的是 `$proxy_add_x_forwarded_for`（**追加**），客户端自带的 XFF 会留在最左侧 → **取最左值是错误做法**（经典可伪造陷阱）。要么取最右、要么干脆只信 `X-Real-IP`。
- ⚠️ 例外：`location /static/` **没有**设置这三个头（只设了 `Host`）。静态资源不需要限流，但若将来把鉴权/限流也覆盖到静态路径，必须补齐。
- 404：nginx 未配置 `real_ip_header` / `set_real_ip_from`，`$remote_addr` 就是真实 TCP 对端（本机即边缘），**正确**；若将来前面再挂 CDN，必须补 `set_real_ip_from`，否则 `X-Real-IP` 会变成 CDN 节点 IP，全员共用一个限流桶。

**HTTPS 终止确认**：nginx 监听 `0.0.0.0:443`（pid 2432678）持有 Let's Encrypt 证书，`/` 与 `/static/` 均 `proxy_pass http://ssq_app`，并下发 HSTS。→ **`Secure` Cookie 属性可以启用**，且 `X-Forwarded-Proto` 已透传。

### 1.5 可复用的既有资产（不要重复造）

| 资产 | 位置 | 对 U1 的价值 |
|---|---|---|
| 迁移框架：批次事务 + 迁移前自动备份 + fail-closed | `migrations.py:296-321`（`apply`）、`:263-294`（`_run_batch`）、`:213-252`（`backup_before_migrate`） | 用户表可直接作为 `version=5` 迁移；备份路径 `<db>.pre-migrate-v<cur>`（`migrations.py:177-182`），实测已有 `ssq.db.pre-migrate-v2`、`-v3` |
| 密钥文件已按 0600 落盘 | `data/llm_config.json` → `-rw-------`（宿主与容器内均如此） | U2 的每用户密钥可沿用该权限口径；U1 的 session/审计表沿用"密钥不进日志"约定 |
| 已有审计表范式（结构化 + 可查询） | `llm_audit` 表（`migrations.py:89-114`）、`reconcile_runs`（`db.py:32`） | `user_audit_log` 照抄其"结构化列 + ts 索引"风格，不发明新格式 |
| 后台任务表 + 轮询 | `db.py:87-96`（`tasks`，`id TEXT PRIMARY KEY`）、`api_app.py:621-628`、前端 `app.js:99` | 后台任务**当前无属主**，见 §10.3 IDOR |
| 前端**唯一**请求出口 | `web/static/app.js:55-56`（`async function api(path, opts){ const r = await fetch(path, opts) ...}`，全文件仅 1 处 `fetch(`） | 401 跳登录页 / CSRF 头注入 **只需改这一个函数**（§7.4） |

### 1.6 不可复用的部分（别指望现成）

| 缺失 | 证据 | 影响 |
|---|---|---|
| 零密码哈希能力 | `.venv` 中 `bcrypt`/`argon2`/`passlib`/`cryptography`/`jwt`/`itsdangerous` **全部 `find_spec` 为 None** | 见 §3 选型 |
| 零限流 | `grep -rn "429\|rate_limit" lottery/*.py` 仅命中 `llm_client.py:538,561-562`（对**上游 LLM** 的 429 退避，不是对客户端限流） | 登录接口必须自建限流 |
| 零会话存储 | 无 Redis、无 session 表、`requirements.txt` 仅 6 个包 | 会话落 SQLite（与业务库同库同事务，见 §4.2） |
| 零"当前用户"概念 | 全仓库无 `user_id` / `current_user` / `request.state.user` | 从零建立，但**只在一个中间件里建立** |
| 调度器无主体 | `api_app.py:2387-2393`（`_scheduler_loop`）、`:2146`（`_scheduler_step`）、`:2214-2222`（调度器直接生成预测） | 需要一个**系统主体**（`SYSTEM_USER_ID = 0` 或 `user_id IS NULL`）来承接调度器产出，否则多用户后"调度器给谁生成预测"无解（与 U3 的 §c 强耦合，本文给出身份侧约定） |
| 无邮箱/无 SMTP | `notify.py` 走的是 webhook/通知渠道，非邮件；`.env.example` 无 SMTP 项 | 密码重置**不能走邮件**，见 §10.4 |

---

## 2. 认证方案选型

### 2.1 候选与取舍

| # | 方案 | 单机适配 | 撤销能力 | 前端改动 | 新依赖 | 关键缺陷 |
|---|---|---|---|---|---|---|
| A | **服务端 session + Cookie**（session 表 + 随机 256bit sid） | ★★★★★ | ★★★★★ 删除行即踢下线 | 极小（Cookie 自动带） | 0 | 需要 CSRF 对策；需建表 |
| B | JWT（无状态） | ★★★ | ★ 无法撤销（只能黑名单/短 TTL） | 中（存 token + 每请求加头） | `pyjwt` 或手写 HMAC | 密钥轮换/登出/禁用用户全部变难；本系统**无多实例需求**，无状态好处为零 |
| C | 每用户 API Token（`Authorization: Bearer`） | ★★★★ | ★★★★ 删行即失效 | 中（前端要存 token） | 0 | 浏览器里 token 只能存 `localStorage`/`sessionStorage` → **XSS 一击即失**；且不适合"多用户共用同一台电脑"场景 |
| D | HTTP Basic | ★★ | ★★ | 小 | 0 | 每个请求都带明文口令（靠 TLS 兜底）；无法登出；无法限流到"登录"这个动作；浏览器弹原生框体验差 |
| E | 复用邻居 `pocket-id`（OIDC IdP，实测在线） | ★★ | ★★★★ | **大**（重定向流 + 回调页） | 需 OIDC client | 引入**跨容器故障耦合**：pocket-id 挂 → 本系统无人能登录，直接违背第 1 点"一个崩溃不拖死全站"；且需在 IdP 侧建 client、管 group 映射 |

**关于 E 的实测**：`curl http://127.0.0.1:1411/.well-known/openid-configuration` 返回正常，issuer `https://id.ff88.de`，`claims_supported` 含 `groups`（可直接映射 role）。所以它**技术上可行**，且能省掉密码管理全部工作量（哈希、重置、暴力破解）。

**但本轮仍不推荐 E**，三条理由：
1. **故障耦合方向错误**。本需求是"管理员可增加用户"，规模量级是 **~10 人**（Lead 实测成本外推：10 用户 × 每期 2 次 ≈ 4.42 元/周）。为 10 个账号引入一个跨容器强依赖，把一个**娱乐软件**的可用性绑在另一个容器的可用性上，性价比为负。
2. **第 1 点的立场一致性**。M1 已经论证本系统要走"进程内隔离 + 分层降级 + 启动自愈"，OIDC 反向引入了一个**外部单点登录依赖**。
3. **"管理员增加用户"的语义会分裂**：用户到底在 pocket-id 里加还是在本系统里加？两处都要改 → 运维成本翻倍。

> **保留路径**：若未来用户数 >30 或需要 SSO/2FA/Passkey，E 是正确的演进方向。届时本方案可平滑迁移——因为 §4 的 `users` 表把 `password_hash` 设计成**可空**（`password_hash TEXT`，允许 NULL 表示"外部 IdP 托管"），并预留 `external_id TEXT UNIQUE`。这一点是刻意设计，不是巧合。

### 2.2 推荐：A（session Cookie）为主，C（每用户 API Token）为辅

**主认证 = A**：浏览器（`web/index.html` + `web/static/app.js`，纯静态无构建）用 Cookie 会话。
**辅认证 = C**：同一套 `users` 表额外发一把**只读/低权限**的 API Token，供脚本/CLI（`scripts/`、`tests/`）调用，避免"为了跑个脚本去模拟浏览器登录"。

**为什么不给所有端点保留 Bearer 后门**：现有的 `optional_api_auth` 是"有 token 就查、没有就放行"，这是 fail-open。新中间件必须是 **fail-closed**：**未匹配到任何凭据 = 401**，而不是"没配 token 就放行"。旧 `LOTT_TOKEN` 的处理见 §2.5。

### 2.3 Cookie 属性（逐条给出理由，不要抄模板）

```
Set-Cookie: ssq_sid=<43 char base64url, 256-bit CSPRNG>;
            Path=/;
            HttpOnly;
            SameSite=Lax;
            Secure;
            Max-Age=604800            # 7 天；"记住我"=30 天，默认 12 小时
```

| 属性 | 取值 | 理由（针对本系统） |
|---|---|---|
| `HttpOnly` | **必开** | 前端 4203 行 `app.js` 会 `innerHTML` 拼大量服务端文本（如 `ruleBody`、`predList`、LLM 输出）。一旦有 XSS，JS 必须拿不到 sid。 |
| `Secure` | **必开** | nginx 已终止 TLS 并下 HSTS（§1.4），合法路径全 https。开 `Secure` 后浏览器不会把 sid 发到 `http://IP:18000` 旁路——**顺带给 §1.3(a) 加了一层浏览器侧缓冲**。 |
| `SameSite` | `Lax` | 默认 `Lax` 即可挡住跨站 POST 表单；本系统**没有**需要跨站携带 Cookie 的场景（无第三方嵌入）。**不要**用 `None`（那要求 `Secure` 且等于放弃 CSRF 第一道防线）。注意 `Lax` **不防**同站点的子域攻击——本机 15 个邻居容器共享 `*.ff88.de` 父域，若某邻居被攻破并能注入 `ssq.ff88.de` 内容则另说；这不属于 Lax 的职责，靠 §2.4 CSRF token 兜。 |
| `Path` | `/` | 前端 SPA 与 API 同源，`/` 最省事。若想更严可选 `Path=/` + 另一个 `Path=/api` 的双 Cookie，**不值得**。 |
| `__Host-` 前缀 | 建议 | `__Host-ssq_sid` 强制要求 `Secure` + `Path=/` + 无 `Domain`，浏览器层面锁死"子域写 Cookie 覆盖"攻击。**推荐直接用**，代价是改名。 |
| `Domain` | **不设** | 设了就等于允许所有 `*.ff88.de` 子域（含 15 个邻居容器）读到/覆盖。 |
| `Max-Age` | 相对时间 | 服务端另有 `expires_at`，Cookie 只是载体；**以服务端为准**（§2.4 会话固定）。 |

### 2.4 CSRF 对策：**双提交 + 同源校验**，不引入 `itsdangerous`

因为 Cookie 会自动携带，必须防 CSRF。三层（成本从低到高，**第 1、2 层必做，第 3 层按需**）：

**第 1 层：`SameSite=Lax`**（§2.3）——挡掉跨站表单 POST 与跨站 `fetch` 默认不带 Cookie。

**第 2 层：`Origin` / `Referer` 同源校验**（中间件里 5 行，零状态）：

```python
# 所有非安全方法（POST/PUT/PATCH/DELETE）校验
if request.method not in ("GET", "HEAD", "OPTIONS"):
    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    if origin and not origin.startswith(config.PUBLIC_ORIGIN):   # "https://ssq.ff88.de"
        return JSONResponse({"ok": False, "error": "跨站请求被拒绝"}, status_code=403)
    if not origin:
        # 无 Origin/Referer：只允许带自定义头的请求（浏览器跨站表单发不出自定义头）
        if not request.headers.get("x-requested-with"):
            return JSONResponse({"ok": False, "error": "缺少来源标识"}, status_code=403)
```

> 注意：`origin` 为空时**不能一律放行**（老浏览器/隐私模式会剥 Referer），也不能一律拒绝（会误伤 curl 脚本）。用"是否带自定义头"区分"浏览器跨站表单"（发不出）与"脚本"（能发）是标准做法。

**第 3 层（推荐做，因为成本已被前端的单点 `api()` 吸收）：双提交 Token**

- 登录成功时，服务端同时下发一个**非 HttpOnly** 的 `ssq_csrf` Cookie（随机 128bit）；
- 前端在 `api()` 里对所有非 GET 请求加 `X-CSRF-Token: <读 ssq_csrf 的值>`；
- 中间件比对 Header 与 Cookie 是否一致（`hmac.compare_digest`），不一致 403。

为什么双提交够用：攻击者能**让**浏览器发请求，但**读不到** `ssq_csrf`（跨源读响应被 CORS 拦），因此无法伪造 Header。
为什么不用 `itsdangerous`/`flask-wtf` 式"签名 token"：需要新依赖（本方案对 CSRF 的目标是不引入新依赖），而双提交在**同源部署、无子域共享**的场景下等价。

**实现成本**：中间件 ~15 行 + 前端 `api()` 改 3 行（`app.js:55-56` 是唯一 `fetch` 出口，已实测全文件只有 1 处 `fetch(`）。

### 2.5 旧 `LOTT_TOKEN` 的下场（必须明确，否则会留后门）

**结论：迁移到 session 模型后，`LOTT_TOKEN` 一律作废，不再参与任何判断。**

理由：它是"全局一把钥匙"，与"每用户隔离"在语义上直接冲突；保留它 = 保留一个**不可归因、不可审计、不可单独撤销**的万能凭据，且它就是当前"匿名可访问"这一现状的遮羞布。

**兼容期（建议 1 个发布周期）**：允许 `LOTT_TOKEN` 作为**只读**凭据使用，映射到 `admin` 的只读视图（仅 GET），并在审计日志里标记 `auth_method="legacy_token"`，同时在启动日志打印醒目告警：

```
[migrations][WARN] LOTT_TOKEN 已废弃，将在下一版本移除；请改用 /api/auth/login
```

**移除判据**：`grep -c "LOTT_TOKEN" 审计日志` 连续 7 天为 0。

### 2.6 为什么不选 JWT（把话说透，避免后面反复）

本系统对"无状态"**零需求**：单进程、单容器、无水平扩展计划（`mem_limit: 768m` 单实例）。JWT 的收益（免查库）在本场景下等于"省一次 SQLite 主键查询"——而 `db.py:160-174` 本来就是**全局单连接**，查一次 `user_sessions` 的成本是微秒级。

JWT 的代价却是实打实的：**撤销**。本需求明确要求"管理员可增加用户"，自然也要能**禁用**用户；一个被禁用的人手里的 JWT 在过期前照样有效。要么上短 TTL（体验差、每个请求都要刷新）、要么上黑名单表（那就退化成 session 了，还多了一层复杂度）。**没有必要。**

---

## 3. 密码哈希：推荐标准库 `hashlib.scrypt`（**密码部分**零新依赖）

### 3.1 先看依赖现状（这决定了选型）

```
$ cat requirements.txt          # 只有 6 个包
numpy>=1.24
scipy>=1.10
requests>=2.28
fastapi>=0.104
uvicorn>=0.24
scikit-learn>=1.3

$ .venv/bin/python -c "import importlib.util as u; ..."
bcrypt        False
argon2        False
cryptography  False
passlib       False
jwt           False
itsdangerous  False
hashlib.scrypt available → True
```

`Dockerfile` 只有一行安装依赖（`RUN pip install --no-cache-dir -r requirements.txt`），所以**加包 = 改 `requirements.txt` 即可，Dockerfile 本身不用动**；但会带来：镜像变大、`docker compose build` 变慢、多一条供应链（与本系统"依赖极少"的现状背离）。

### 3.2 三种算法对比（含实测）

`.venv/bin/python` 实测本机 scrypt 性能（16 字节 salt，64 字节输出）：

| 参数 | 内存/次 | 单次耗时 | 结果 |
|---|---|---|---|
| `n=2^14, r=8, p=1` | 16.0 MB | **28.2 ms** | ✅ |
| `n=2^15, r=8, p=1` | 32.0 MB | **60.1 ms** | ✅ |
| `n=2^16, r=8, p=1` | 64.0 MB | — | ❌ `ValueError: memory limit exceeded`（OpenSSL 默认 `maxmem` 32MB） |
| `n=2^15` **不传 `maxmem`** | — | — | ❌ 同上失败 |

> ⚠️ **实测陷阱**：`hashlib.scrypt` 的 `maxmem` 默认是 0（= OpenSSL 默认 32MB 上限），所以 **`n=2^15` 也会直接抛异常**。必须显式传 `maxmem=64*1024*1024`。这是一个"照文档抄就会挂"的坑。

| 维度 | `hashlib.scrypt`（**推荐**） | `bcrypt` | `argon2-cffi` |
|---|---|---|---|
| 新依赖 | **0**（标准库） | +1 包（`bcrypt`） | +2~3 包（`argon2-cffi` → `cffi` → `pycparser`） |
| Dockerfile 改动 | **无** | 无（仅 `requirements.txt`） | 无（仅 `requirements.txt`） |
| 是否需要编译 | 否（Python 内置 OpenSSL） | 否（有 manylinux wheel） | 否（有 wheel；无 wheel 时才需 gcc/libffi-dev，`python:3.11-slim` 未装） |
| 抗 GPU/ASIC | 强（内存硬） | 中（4KB 固定内存，GPU 友好） | **最强**（内存硬 + 可调并行度） |
| 72 字节截断坑 | 无 | **有**（`bcrypt` 静默截断 >72 字节口令） | 无 |
| 参数升级/算法轮换 | 自己管 PHC 串 | `$2b$` 前缀 | 原生 PHC 串 |
| OWASP 定位 | 可接受（需足够参数） | 可接受（cost≥10） | **首选** |

### 3.3 **推荐：`hashlib.scrypt`，参数 `n=2^15, r=8, p=1, maxmem=64MB`，32 字节 salt，64 字节输出，存储为 PHC 风格字符串**

**为什么不是 argon2**：argon2 确实更强，但它带来的收益在本场景下很小（10 个账号、单机、无公网撞库压力以外的场景），代价是 3 个新依赖 + 一条 C 扩展供应链。本系统整体的工程取向是"依赖极少、容器轻"（`mem_limit: 768m`，`requirements.txt` 只有 6 个包）。**为了 10 个账号引入 cffi 依赖链，收益/成本不成立。**

**为什么不是 bcrypt**：`bcrypt` 本身很成熟，但它有 72 字节截断这个**静默安全缺陷**（超长口令等价于截断后口令），且内存硬化弱于 scrypt。两者都要新依赖，那就选不需要新依赖的那个。

**为什么 scrypt 在这里够用**：内存硬（32MB/次）→ 单卡 GPU 并行度被内存带宽锁死；60ms 的验证成本对**登录**这种低频动作完全可接受（对比：预测端点单次 LLM 调用 1~2 分钟）。

**存储格式（用 PHC 串，为将来换算法留路）**：

```
scrypt$n=32768,r=8,p=1$<base64(salt)>$<base64(hash)>
```

只要把算法标识放在第 1 段，将来换 argon2 时就能"**验证时按前缀分派、登录成功时顺手重算并升级**"，不需要一次性重置所有人密码。这是**唯一**需要现在做对的向前兼容设计。

**关键片段（只给要点，不给完整实现）**：

```python
# lottery/auth.py（新建）
import base64, hashlib, hmac, os, secrets

_N, _R, _P = 2**15, 8, 1
_MAXMEM = 64 * 1024 * 1024          # 必须显式传，否则 n>=2^15 直接抛 ValueError
_HASH_MAX_CONCURRENCY = 4           # 见下方"768m 内存护栏"
_hash_sem = threading.BoundedSemaphore(_HASH_MAX_CONCURRENCY)

def hash_password(pw: str) -> str:
    salt = os.urandom(32)
    with _hash_sem:                                    # 防并发打爆内存
        dk = hashlib.scrypt(pw.encode("utf-8"), salt=salt,
                            n=_N, r=_R, p=_P, maxmem=_MAXMEM, dklen=64)
    return f"scrypt$n={_N},r={_R},p={_P}$" \
           f"{base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"

def verify_password(pw: str, stored: str) -> tuple[bool, bool]:
    """返回 (是否通过, 是否需要重哈希升级)。"""
    try:
        algo, params, b64salt, b64hash = stored.split("$")
        if algo != "scrypt":
            return False, False
        kv = dict(p.split("=") for p in params.split(","))
        n, r, p = int(kv["n"]), int(kv["r"]), int(kv["p"])
        salt, want = base64.b64decode(b64salt), base64.b64decode(b64hash)
        with _hash_sem:
            got = hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                                 maxmem=_MAXMEM, dklen=len(want))
    except (ValueError, KeyError):
        return False, False
    ok = hmac.compare_digest(got, want)                # 必须常量时间比较
    return ok, bool(ok and (n, r, p) != (_N, _R, _P))
```

### 3.4 768m 容器里的内存护栏（容易漏掉的一条）

`n=2^15` 每次哈希占 **32MB**。端点跑在 40 线程池里，`/api/auth/login` 又是**唯一会被匿名高频调用**的端点。若不限并发：

> 20 个并发登录 × 32MB = **640MB**，叠加 numpy/scipy 的常驻（实测 peak 371MB，见 `docker-compose.yml:6` 注释）→ **OOMKilled**。

所以必须三层同时上：
1. `threading.BoundedSemaphore(4)`（上例）→ 峰值 ≤128MB；
2. **登录限流**（§10.1，按 IP + 按 username 双桶）；
3. 若仍担心，降级到 `n=2^14`（16MB/次，28ms）——**这是唯一"一行改参数"就能降内存的设计**，建议把 `_N` 放环境变量 `LOTT_SCRYPT_N`（默认 32768），出事时无需改代码。

### 3.5 明确的禁止项

- ❌ 明文、`sha256(pw)`、`md5(pw)`、`sha256(pw+salt)` **单轮**——全部禁止；
- ❌ `passlib` 的 `pbkdf2_sha256` 默认 cost（不值得为它引入依赖，参数也偏低）；
- ❌ 用 `hashlib.pbkdf2_hmac` **不加** 内存硬参数——它不是内存硬函数，本场景下 scrypt 免费更好；
- ❌ 自己实现 salt 拼接/比较（必须 `hmac.compare_digest`，否则时序侧信道）；
- ❌ 把 `password_hash` 写进任何 API 响应（`SELECT` 时必须显式列白名单，见 §8.1）。

### 3.6 依赖落地清单（回答"选哪个库、版本号、要不要改 Dockerfile"）

> **适用范围**：本节只回答**密码哈希**的依赖。API Key 的**可逆加密**是另一个密码学问题，归 U2 用 `cryptography`（见本节末尾的"裁决 6"）。

| 问题 | 答案 |
|---|---|
| 选哪个库 | **密码哈希不装新库**：Python 标准库 `hashlib.scrypt`（`hashlib.scrypt` 在 CPython 3.6+ 恒可用，实测本机 `hasattr(hashlib,'scrypt') == True`） |
| 版本号 | **无需声明**——它随解释器走（容器是 `python:3.11-slim`，即 CPython 3.11 自带的 OpenSSL 实现）。附带好处：**没有版本号要管，也就没有版本冲突** |
| 要不要改 `Dockerfile` | **密码部分不用**：`Dockerfile` 只有 `RUN pip install --no-cache-dir -r requirements.txt` 一行与依赖相关，新增 Python 包只需改 `requirements.txt`，`Dockerfile` 自动生效 |
| 若将来改走备选（`bcrypt`） | `requirements.txt` 加 `bcrypt>=4.1`（4.x 起 `bcrypt.checkpw` 支持 `bytes`/`str` 校验；官方提供 `manylinux` wheel，无需 gcc）。**仍不需要改 `Dockerfile`** |
| 本机现状（实测） | `.venv` 里 `bcrypt`/`argon2`/`cryptography`/`passlib`/`jwt`/`itsdangerous` **全部未安装**；`hashlib.scrypt` **可用** |

### 3.7 **Lead 裁决 6**：密码哈希与 API Key 加密是两个问题，分开用

**裁决原文要点**：U1 的 `hashlib.scrypt` 与 U2 的 `cryptography` **不冲突**，按用途分开用。

| 用途 | 方案 | 为什么不能用另一个 |
|---|---|---|
| **用户口令**（本文件 §3） | `hashlib.scrypt`（标准库，零依赖） | 口令只需**验证**，永不需还原 → 单向哈希是**正确**且更安全的做法。用可逆加密存口令是**设计错误**（等于给自己留一把万能钥匙） |
| **LLM API Key**（U2） | `cryptography` 的 `Fernet`（对称可逆） | API key **必须还原成明文**才能发给上游 LLM → 哈希在信息论上不可能胜任。**这是唯一必须新增依赖的地方** |

**对本文件的影响（唯一一处需要撤回的表述）**：§3.6 的"零新依赖"**只对密码成立**，不能推广到"整个多用户改造零新依赖"。

| 落地项 | 结论 |
|---|---|
| `requirements.txt` | **+1 行**：`cryptography>=42.0`（42 起 `Fernet` 稳定；`python:3.11-slim` 有 manylinux wheel，无需 `libssl-dev`/`gcc`） |
| `Dockerfile` | **无需改动**。它只做 `pip install -r requirements.txt`，加包自动生效（`COPY requirements.txt .` 在前、`RUN pip install` 在后，层缓存会自然失效并重装）。**若采用离线镜像/固定哈希锁（`--require-hashes`），才需要在 `Dockerfile` 或锁文件里补一行**——本仓库当前未使用哈希锁 |
| 版本与供应链 | 新增 1 个包、1 条依赖（`cffi` 由 `cryptography` 的 wheel 自带二进制，无需 Python 层新包）。相比"argon2 要引入 `argon2-cffi`+`cffi`+`pycparser` 三包"，**代价小得多**，且换来的是"API key 不以明文落盘" |

> **架构上的一句话**：**"不可逆的东西用哈希，必须还原的东西用加密"**。把这两件事混为一谈，是要么把口令做成可逆（安全事故），要么把 API key 做成不可逆（功能不可用）。本文件只负责前者；后者请 U2 用 `Fernet` 落地，**且主密钥必须来自 env、不得入库**（与 `data/llm_config.json` 的 0600 权限口径一致）。

---

## 4. 用户模型（schema 与跨文档约定）

### 4.0 强制约定（Lead 冻结的 3 条 + U1 追加的 4 条，均受裁决 1~4 约束）

```sql
user_id  INTEGER PRIMARY KEY AUTOINCREMENT
username TEXT UNIQUE NOT NULL
role     TEXT CHECK(role IN ('admin','user'))
```

**本文额外追加的接口约定**（U2 / U3 必须遵守，否则三份文档对不上）：

| 约定 | 内容 | 理由 |
|---|---|---|
| 外键列类型 | 一律 `user_id INTEGER NOT NULL`（除 `users` 自身） | 与主键同类型，SQLite 下避免隐式转换 |
| 外键约束 | **不做正确性依据**。可以写 `REFERENCES` 作为**冗余防御**，但删除用户时**必须**在应用层显式事务里删关联行（Lead 裁决 1） | 本库 `PRAGMA foreign_keys` 实测为 **0**（全仓零命中），且它是**每连接**设置：一旦换连接池/多进程就静默失效。**安全逻辑不能建立在"某个 PRAGMA 恰好开着"之上** |
| 系统主体 | `SYSTEM_USER_ID = 0`（**保留 id，不插入 `users` 表**），定义为 `config.SYSTEM_USER_ID` 常量（Lead 裁决 3） | 调度器（`api_app.py:2387`）需要在没有人类用户时也有归属。**否决 `NULL`**：`NULL` 在三值逻辑里与任何比较都不相等，`WHERE user_id != ?` 会漏掉系统任务，且无法归因 |
| 主 admin 识别 | **禁止任何 `DEFAULT 1`**；admin 一律按 `username` / `is_primary_admin` 定位（Lead 裁决 2/4） | `AUTOINCREMENT` 只保证递增，**不保证第一个 admin 拿到 1**（删光重插会得到 2）。把流程假设硬化成 schema 约束是错误 |
| 软删除 | `users.deleted_at REAL`（NULL=存活），**不做物理 DELETE** | 历史数据（predictions/eval/tasks）的 `user_id` 必须始终能解析出"这个人曾经是谁" |
| 角色演进 | `role` 只有 `admin`/`user` 两值；**不引入 `viewer`/`guest`** | 10 个账号的娱乐系统，两角色够了；多角色是无依据的复杂度 |
| 时间字段 | `REAL`（Unix 秒），与全库一致（`tasks.created_at`、`draws` 同风格） | 本库无 `DATETIME` 用法，混用会制造比较 bug |

### 4.1 `users` 表

```sql
CREATE TABLE IF NOT EXISTS users (
  user_id        INTEGER PRIMARY KEY AUTOINCREMENT,
  username       TEXT    NOT NULL UNIQUE,          -- 约定；大小写敏感（见 §10.8）
  password_hash  TEXT,                             -- 可空：NULL = 外部 IdP 托管（§2.1 预留）
  role           TEXT    NOT NULL DEFAULT 'user'
                         CHECK(role IN ('admin','user')),
  -- status 四值语义（Lead 裁决 7，**CHECK 必须四个值都在，少一个会让迁移 5 的
  -- bootstrap INSERT 被拒 → 整批事务回滚 → 上线失败**）：
  --   bootstrap : 零权限。所有鉴权一律拒绝（中间件在"角色判断之前"就拦），
  --               仅允许走一次性 env token 完成"设密 → 转 active"这一个动作。
  --   active    : 正常可用。
  --   disabled  : 软删除/停用；**该用户的所有 session 立即失效**（不进缓存）。
  --   locked    : 因安全事件（如连续登录失败）被锁定；可由 admin 解锁，
  --               与 disabled 的区别是"本人/管理员可恢复"，disabled 需先 enable。
  status         TEXT    NOT NULL DEFAULT 'active'
                         CHECK(status IN ('bootstrap','active','disabled','locked')),
  display_name   TEXT    NOT NULL DEFAULT '',
  -- 主管理员标记（Lead 裁决 2）：引导创建的第一个 admin 置 1，全表只允许一行为 1。
  -- 用途：迁移回填/全局配置归属的锚点。**不使用 "admin 的 user_id = 1" 这种假设**。
  is_primary_admin INTEGER NOT NULL DEFAULT 0,
  -- 登录失败计数与锁定（暴力破解对策，§10.1）
  failed_count   INTEGER NOT NULL DEFAULT 0,
  locked_until   REAL    NOT NULL DEFAULT 0,
  last_login_at  REAL,
  -- 口令/凭据版本：改密或强制登出时 +1，用于批量失效会话
  token_version  INTEGER NOT NULL DEFAULT 1,
  -- U2 接口预留：不存 LLM 配置本体，只存"是否允许自定义"与"用哪套默认"
  llm_profile    TEXT    NOT NULL DEFAULT 'default',
  -- 外部 IdP 预留（§2.1 的演进路径）
  external_id    TEXT,
  -- 软删除
  deleted_at     REAL,
  created_at     REAL    NOT NULL,
  updated_at     REAL    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_users_username_alive
  ON users(username) WHERE deleted_at IS NULL;      -- 允许"删除后重建同名用户"
CREATE INDEX IF NOT EXISTS idx_users_status ON users(status, deleted_at);
-- 主管理员唯一性：全表最多一行 is_primary_admin=1（Lead 裁决 2 的锚点）
CREATE UNIQUE INDEX IF NOT EXISTS uq_users_primary_admin
  ON users(is_primary_admin) WHERE is_primary_admin = 1;
```

**`status` 状态机（Lead 裁决 7，与 U3 的 bootstrap 设计对接）**：

```
                  ┌──────────────── 一次性 env token + 设密成功（同一事务）────────────────┐
                  │                                                                        │
  (U3 迁移 5 插入) │                                                                        ▼
  status='bootstrap' ──────────────────────────────────────────────► status='active'
   零权限，鉴权全拒                                                     role='admin'
   role 可以是 'admin'，但**不授权任何东西**                             is_primary_admin=1
   is_primary_admin 必须仍为 0
                                                    active ⇄ locked  （登录失败锁定 / admin 解锁）
                                                    active → disabled （停用/软删除，session 立即失效）
```

**三条不可协商的 bootstrap 不变量**：

1. **status 是唯一闸门，且必须在角色判断之前生效**：中间件解析会话后，**先**判 `status != 'active'` → 直接 401/403；**再**判 role。否则"`role='admin'` + `status='bootstrap'`"这一行就等于一个无密码的管理员。
2. **`is_primary_admin` 只能在设密成功的同一事务里置 1**：绝不允许"先建 bootstrap 行、顺手把 is_primary_admin=1"——那会造出一个"零密码但有主 admin 标记"的账号，配合任何一处按 `is_primary_admin` 取"主 admin"的逻辑（如全局配置归属、迁移回填）就是**提权漏洞**。
3. **`bootstrap` 行不得出现在任何"用户列表/计数"里当作有效管理员**：`SELECT COUNT(*) FROM users WHERE role='admin' AND status='active'` 才是"有几个可用管理员"的正确口径（§8.4 的 last_admin 判断也用它）。

> **给 U3 的一条建议（不阻塞，只更安全）**：`U3_DATA_MODEL.md:276` 的 INSERT 目前写 `role='admin'`。建议改为 **`role='user'` + `is_primary_admin=0` + `status='bootstrap'`**，设密成功后再在同一事务里升为 `role='admin'` + `is_primary_admin=1` + `status='active'`。这样即使中间件的 status 闸门将来写漏，role 也不会额外授权（**纵深防御**）。若 U3 坚持插入即 `role='admin'`，则**必须**保证 §7.3 的 status 闸门先于 role 判断且不可绕过。

> **为什么 `username` 上同时有 `UNIQUE` 约束和部分唯一索引**：`TEXT UNIQUE`（表内）保证物理唯一；部分唯一索引是给"软删除后允许同名重建"用的。若 U3 认为不该允许重建，删掉部分索引即可。**两者不冲突**——但要注意：表内 `UNIQUE` 会让软删除后无法重建同名用户。**正确做法是表内不加 `UNIQUE`，只保留部分唯一索引**（见下方修订）。

**修订（以此为准）**：

```sql
  username TEXT NOT NULL,          -- 不加表内 UNIQUE
  ...
CREATE UNIQUE INDEX IF NOT EXISTS uq_users_username_alive
  ON users(username) WHERE deleted_at IS NULL;
```

> 但 Lead 冻结的约定是 `username TEXT UNIQUE NOT NULL`。**折中且不违约的做法**：保留 `UNIQUE`（守约定），**删除改为"禁用 + 改名归档"**——`DELETE` 时把 `username` 改写为 `username || '#deleted#' || user_id`，从而释放原名。这样既遵守约定，又满足"重建同名"。**推荐这条**（§8.4 采用）。

### 4.2 `user_sessions` 表（会话落 SQLite 的理由）

```sql
CREATE TABLE IF NOT EXISTS user_sessions (
  session_id   TEXT    PRIMARY KEY,        -- 32 字节 CSPRNG 的 base64url（43 字符）
  user_id      INTEGER NOT NULL,           -- 约定类型
  created_at   REAL    NOT NULL,
  expires_at   REAL    NOT NULL,           -- 绝对过期（服务端权威）
  last_seen_at REAL    NOT NULL,
  ip           TEXT    NOT NULL DEFAULT '',-- 登录时 X-Real-IP，仅审计/告警用，不参与校验
  user_agent   TEXT    NOT NULL DEFAULT '',
  csrf_token   TEXT    NOT NULL DEFAULT '',-- 双提交凭据（§2.4）
  revoked_at   REAL                        -- 非 NULL = 已登出/被管理员踢下线
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON user_sessions(user_id, revoked_at);
CREATE INDEX IF NOT EXISTS idx_sessions_exp  ON user_sessions(expires_at);
```

**为什么落 DB 而不是内存 dict**：
- 进程重启（`entrypoint.sh` 每次部署都会重启）后用户不会被强制重登；
- 管理员"踢下线"只需 `UPDATE ... SET revoked_at`，不需要跨进程通知；
- 与业务库**同一份文件、同一个连接** → 备份（`migrations.py` 的热备）自动包含会话表，不会出现"库恢复了但会话丢了"；
- 代价：每个请求多一次 `SELECT`。**这个代价必须正视**：`db.py:160-174` 是**全局单连接、无锁、无 WAL**（`sqlite3.connect(..., check_same_thread=False)`），40 线程并发下**新增的高频 `SELECT` 会加剧锁竞争**。

**缓解（必须做，否则鉴权会变成性能瓶颈）**：

```
① session 表查询必须走 PRIMARY KEY（O(log n)，命中 page cache）；
② 进程内加一层 TTL 缓存（session_id -> (user_id, role, expires_at)，上限 512 条，TTL 30s），
   把"每请求一次查询"降为"每 30s 每会话一次"；
③ 撤销路径（登出/踢人/改密）必须主动清缓存，不能只靠 TTL；
④ 长期正解是 db.py 开 WAL + busy_timeout（属第 1 点模块化范畴，本文只标注依赖关系）。
```

> ⚠️ **重要耦合**：鉴权中间件把"每个请求"都变成了"要访问 DB 的请求"。当前系统是**同步 def + 全局单连接 + 无 WAL**（`db.py:160-170`），一旦加上"每请求查会话"，原本不碰 DB 的端点（如 `/api/health`）也开始竞争同一个连接。**因此本文明确要求：会话缓存（上述 ②）是 P0 而非优化项，并且 `db.py` 的 WAL/`busy_timeout` 应在鉴权上线前完成**（或至少同时）。这是 U1 对第 1 点的**硬依赖**，请 Lead 在排期时把两者绑定。

### 4.3 `user_audit_log` 表（管理员动作审计）

```sql
CREATE TABLE IF NOT EXISTS user_audit_log (
  audit_id    INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          REAL    NOT NULL,
  actor_id    INTEGER,                 -- 操作者 user_id；系统动作 = 0；匿名（登录失败）= NULL
  actor_name  TEXT    NOT NULL DEFAULT '',
  action      TEXT    NOT NULL,        -- 见下表枚举
  target_id   INTEGER,                 -- 被操作的 user_id
  target_name TEXT    NOT NULL DEFAULT '',
  result      TEXT    NOT NULL,        -- ok / denied / failed
  ip          TEXT    NOT NULL DEFAULT '',
  detail_json TEXT    NOT NULL DEFAULT '{}'   -- 前后差异（**严禁写入 password_hash / api_key**）
);
CREATE INDEX IF NOT EXISTS idx_uaudit_ts   ON user_audit_log(ts DESC);
CREATE INDEX IF NOT EXISTS idx_uaudit_act  ON user_audit_log(action, ts DESC);
CREATE INDEX IF NOT EXISTS idx_uaudit_tgt  ON user_audit_log(target_id, ts DESC);
```

**`action` 枚举（冻结，前端/报表按此聚合）**：

| action | 触发 | 必记字段 |
|---|---|---|
| `auth.login` | 登录成功 | actor_id, ip, ua |
| `auth.login_failed` | 登录失败 | actor_name（提交的用户名）, ip, detail.reason |
| `auth.logout` | 主动登出 | actor_id |
| `auth.bootstrap` | 首次引导创建 admin | target_id, ip |
| `user.create` | 管理员建用户 | actor_id, target_id, detail.role |
| `user.disable` / `user.enable` | 启停 | actor_id, target_id |
| `user.password_reset` | 管理员重置口令 | actor_id, target_id（**不记新口令**） |
| `user.password_change` | 用户自改 | actor_id |
| `user.role_change` | 改角色 | actor_id, target_id, detail.from/to |
| `user.delete` | 软删 | actor_id, target_id |
| `user.llm_reset` | 重置该用户 LLM 配置（**动作属 U2，审计口径归 U1**） | actor_id, target_id |
| `session.revoke` | 踢下线/批量失效 | actor_id, target_id, detail.count |
| `authz.denied` | 越权访问被拒（**关键：这是入侵信号**） | actor_id, detail.path, detail.required_role |

> `authz.denied` 是整张表里**最有安全价值**的一条：单用户被拒 1 次是误操作，1 分钟内被拒 20 次就是在探路。建议接一条轻量告警（写日志即可，本系统无告警通道时**不要**为它引入新依赖）。

### 4.4 与 U2 / U3 的接口契约（明确"本文不管什么"）

| 事项 | 归属 | 本文提供的契约 |
|---|---|---|
| LLM 配置存哪 | **U2** | 本文只保证：`users.user_id` 是稳定主键、软删除后仍可解析、`llm_profile` 字段已预留 |
| `user_llm_config` 外键 | **U2** | 必须 `user_id INTEGER NOT NULL`，与 `users.user_id` 同名同类型；可写 `REFERENCES` 作**冗余防御**，但**不得作为正确性依据**（Lead 裁决 1） |
| API key 加密主密钥 | **U2** | 由 env 注入；**不得**放进 `users` 表或本文任何表 |
| 哪些表加 `user_id` | **U3** | 本文只强制一条：`user_id` 命名与类型统一为 `INTEGER`，系统主体为 `0` |
| `predictions` 主键改造 | **U3** | 身份侧要求：`(user_id, issue, seq)`；**调度器产出归 `user_id=0`**，与"用户各自生成"（U3 §c）解耦 |
| 前端登录页/管理页 | **本文 §8.5 + U4 前端任务** | 本文给端点契约与 `api()` 改造点，不给 HTML |
| 审计日志查询端点 | 本文 | `GET /api/admin/audit`（admin-only），见 §8.6 |

---

## 5. 首次启动引导：创建第一个 admin

### 5.1 先排除所有"能被告警扫到"的做法

| 反模式 | 为什么必须禁止 |
|---|---|
| `admin` / `admin123` / `password` 默认账号 | 公网 IP 上 80 端点已匿名可访问（§1.2），扫描器（Shodan/Censys/自建字典）对 "新出现的 Web 登录页" 的默认口令尝试是**分钟级**的。而本系统一旦进 admin，就等于拿到 LLM API key + 全部数据 + 可执行 `evolution/run`。 |
| 首启自动创建随机密码并 `print` 到容器日志 | 日志通过 `docker logs` / 日志采集外泄面大；且重启后日志还在。 |
| 首启自动创建随机密码并**返回在某个 API 响应里** | 谁先访问谁就是 admin（race），且公网可达 → 必然被抢。 |
| "首次访问可自行注册" | 同上，抢注。 |
| `LOTT_ADMIN_PASSWORD` 环境变量直接设初值 | 可行但**不推荐**：`.env` 里长期存明文口令，且改密后容易忘了删；`.env` 的权限是 `-rw-r--r--`（实测 `ssqyuce/.env` 为 644）。 |

### 5.2 推荐方案：**一次性引导口令（env 注入）+ 单次令牌消费 + 立即失效**

**状态机（两种入口，终态一致）**：

```
入口 A（U3 迁移 5，无 shell/无 HTTP 时也能就位）
  迁移插入 users(username='admin', password_hash=NULL, role=?, status='bootstrap',
                  is_primary_admin=0)          ← 零权限，鉴权全拒

入口 B（HTTP 一次性引导，需 LOTT_BOOTSTRAP_TOKEN 已设置）
  POST /api/auth/bootstrap { token, username, password }
        │
        ▼
  ┌──────────────────────────────────────────────────────────┐
  │ 同一事务：                                                  │
  │   password_hash = scrypt(password)                        │
  │   status        = 'active'                                │
  │   role          = 'admin'                                 │
  │   is_primary_admin = 1        ← 只能在设密成功的同一事务里   │
  │   INSERT user_audit_log(action='auth.bootstrap')          │
  └──────────────────────────────────────────────────────────┘
        │
        ▼
  [已有 active admin] → /api/auth/bootstrap 永远 410 Gone（不管 token 对不对）
                       → 启动日志提示：请删除 LOTT_BOOTSTRAP_TOKEN
```

**可落地的 6 条硬规则**：

1. **开关**：`LOTT_BOOTSTRAP_TOKEN`（env，≥32 随机字符）。**不设 = 引导接口不存在（404）**。
2. **只能成功一次**：判据是 `SELECT COUNT(*) FROM users WHERE role='admin' AND status='active' AND deleted_at IS NULL`。≥1 → 直接 `410 Gone`，**先判存在性再判 token**，避免"用引导接口当密码预言机"。**注意口径**：这里必须带 `status='active'`——U3 迁移插入的 `status='bootstrap'` 行不算"已引导"，否则会把引导入口永久锁死（§4.1 不变量 3）。
3. **token 必须常量时间比较**：`hmac.compare_digest`（照抄 `api_app.py:40` 的既有做法）。
4. **成功后必须提示删除 env**：响应与启动日志都打印 `请立即从 .env 删除 LOTT_BOOTSTRAP_TOKEN 并 docker compose up -d`。
5. **`is_primary_admin = 1` 只能在设密成功的同一事务里置位**（Lead 裁决 2 + 7）：**绝对不允许**"建行时就顺手写 `is_primary_admin=1`"——那会造出一个"零密码却带主 admin 标记"的账号（§4.1 不变量 2）。这是本文件里唯一一个"顺序即安全"的点。它是"主 admin"的唯一权威标记：迁移回填、全局配置归属都以它为准，**任何地方都不许假设"admin 的 user_id 是 1"**（`AUTOINCREMENT` 不保证，实测删光重插会得到 2）。
6. **`status='bootstrap'` 的行在设密成功前不得拥有任何权限**（Lead 裁决 7）：中间件必须在**角色判断之前**先拒绝所有非 `active` 状态（§7.3）。**这一行既不能登录、也不能调用任何 `/api/*`，即使它的 `role` 字段写着 `'admin'`。**

**契约**：

```
POST /api/auth/bootstrap
Body: { "token": "<LOTT_BOOTSTRAP_TOKEN>", "username": "alice",
        "password": "<≥12 字符，受 §10.9 强度校验>" }
200: { "ok": true, "user_id": <实际 id，勿写死 1>, "username": "alice", "role": "admin",
       "next": "请立即删除 LOTT_BOOTSTRAP_TOKEN 环境变量并重启服务" }
404: 未设置 LOTT_BOOTSTRAP_TOKEN（接口不暴露）
410: 已存在 active admin（无论 token 是否正确）
403: token 不匹配（同时计入 §10.1 的限流桶，key = bootstrap:<ip>）
```

**为什么这条比 env 直设口令好**：口令**不在磁盘上持久化**（`token` 是一次性闸门，不是密码）；口令由**人**在浏览器里输入（可被 §10.9 强度校验、可被审计）；引导后可**立即删除** token 变量，攻击窗口 = 部署完成到首次登录之间的几分钟，而非永久。

### 5.3 备选方案：CLI 引导（**推荐与上条同时提供**）

```bash
# 宿主上执行（不进 HTTP，无网络暴露风险）
docker exec -it ssq-predictor python -m lottery.cli create-admin --username alice
# 交互式读两次口令（getpass，不回显、不进 shell history、不进日志）
```

**优点**：零网络暴露面，是最安全的通道；**缺点**：需要 shell 权限。

> **推荐组合**：**CLI 为主（文档首选），HTTP 引导为备（无 shell 时用）**。两者共用同一个 `auth.create_user()` 实现，避免两条路径逻辑漂移——这正是本系统在 `evolution_run` 上已经踩过的教训（`api_app.py` 的 `evolution_run` docstring 明确写着"与调度器每期自动跑的 `_evolution_step` 走同一条路径，避免两处逻辑漂移"）。**照此惯例执行。**

### 5.4 引导完成后的自检（写进 `/api/health`）

```json
{
  "status": "ok",
  "auth": {
    "mode": "session",
    "users": 3,
    "admins": 1,
    "bootstrap_pending": false,        // true = 还没有 admin，说明引导未完成
    "legacy_token_enabled": false,
    "insecure_transport": true        // 检测到 18000 仍在 0.0.0.0 绑定 或 X-Forwarded-Proto=http
  }
}
```

- `bootstrap_pending` 让"忘了引导"这件事**可见**，而不是靠"登录不进去"猜。
- `insecure_transport`：中间件里判断 `request.headers.get("x-forwarded-proto") != "https"` 且请求非本机 → 记一条 `authz.insecure_transport` 审计并计数。**这条能把 §1.3(a) 的明文旁路从"没人知道"变成"有人知道"**。
- ⚠️ 注意：`/api/health` 当前已返回 `api_auth_enabled`（`api_app.py:86`）。多用户后该字段含义失效，**必须删除**，否则会暴露"鉴权是否生效"（对攻击者是免费侦察）。

---

## 6. 权限矩阵（80 端点逐条分级）

### 6.1 分级定义与判定原则

| 级别 | 含义 | 判定规则 |
|---|---|---|
| **P**（public） | 无需凭据 | 仅限登录页、健康探活、登录/登出/引导。**必须逐条列白名单**，不在白名单的一律按 U 处理 |
| **U**（user） | 任意已登录用户 | 只读全局事实，或**只影响自己**且成本受自己配额约束的写操作 |
| **A**（admin） | 仅 `role='admin'` | ① 改变**全局共享事实**（抓数、规律库、模型评估）；② 全局配置；③ 高成本/可拖垮全站的任务；④ 用户管理 |
| **I**（internal） | 无 HTTP 入口 | 调度器（`api_app.py:2387`）、后台线程（`:604`、`:2848`）——**不经 HTTP，因此由中间件管不到**，必须走 §7.5 的服务端主体约定 |

**判定原则（写成一句话，供评审时打回）**：> **"这个操作的结果，会不会出现在别人的页面上？"** 会 → A；不会 → U。

举例：`POST /api/predict` 生成的是**我自己的**票，别人看不到 → U（但要吃**我自己的** LLM 配额，U2 负责）。`POST /api/mining/run` 写的是 `patterns`/`mining_runs`，**所有人**的规律库都会变 → A。`POST /api/refresh` 写 `draws`，是全世界共用的事实 → A。

### 6.2 实测分级结果（程序化校验覆盖全部 80 条，无遗漏）

| 级别 | 数量 | 占 80 端点 |
|---|---|---|
| **P** public | 3 | 3.8% |
| **U** user | **53** | 66.3% |
| **A** admin | **24** | 30.0% |

> 校验方法：用内存脚本枚举 `app.routes` 的 80 个 `APIRoute`，逐条按上表判定并计数（不依赖人工数数，避免漏项）。

### 6.3 admin-only 清单（24 条，附理由）

| # | 方法 | 路径 | 归 A 的理由 |
|---|---|---|---|
| 1 | GET | `/api/data/reconcile` | 触发 `data_check.run()`：**有网络副作用 + 写 `reconcile_runs`**（`api_app.py:55-58`）。注意它虽是 GET 但**不幂等**，本就不该用 GET（见 §6.5） |
| 2 | POST | `/api/refresh` | 抓远端写 `draws`（全局事实，`api_app.py:116` 的 `data_fetcher.fetch_and_update()`） |
| 3 | POST | `/api/mining/run` | 写全局规律库 `patterns` / `mining_runs` |
| 4 | POST | `/api/patterns/backtest` | 写全局 `patterns` 回测结论 |
| 5 | POST | `/api/eval/backtest` | 写全局 `eval_*` |
| 6 | POST | `/api/eval/coverage` | 写全局 `eval_*` |
| 7 | POST | `/api/eval/online` | 写全局 `eval_*` |
| 8 | POST | `/api/eval/llm/run` | 后台任务 + **LLM 全量花费**（`accepts issues≤200`），单次可烧掉数元 |
| 9 | POST | `/api/evolution/run` | **危险操作**：挖掘→注册→A/B→应用已验证规律，直接改变出号通道（`api_app.py` 有该端点完整 docstring）。**task 明确点名 admin-only** |
| 10 | POST | `/api/rules/closed-loop` | 同上，同步版 |
| 11 | POST | `/api/rules/closed-loop/async` | 同上，异步版 |
| 12 | POST | `/api/rules/lifecycle/run` | 推进全局规律生命周期状态机 |
| 13 | POST | `/api/rules/refresh` | 重建全局 `patterns_runtime.json`，**直接决定所有人的出号** |
| 14 | POST | `/api/regime/refresh` | 全局阶段切分 |
| 15 | POST | `/api/methods/config` | **全局方法开关**，会改变所有人的预测（`api_app.py` docstring："立即生效并持久化，影响之后的预测"） |
| 16 | POST | `/api/llm_lab/run` | 全局假说库 + LLM 花费 |
| 17 | POST | `/api/llm_lab/run/async` | 同上 |
| 18 | POST | `/api/llm_lab/register` | 写**全局前置注册簿**（`hypothesis.py`）——污染它等于污染全站统计卫生 |
| 19 | POST | `/api/audit/anomaly/run` | 全局异常归因 |
| 20 | POST | `/api/experiments/randomness-selfcheck` | 全局实验（耗时计算） |
| 21 | POST | `/api/ml/eval` | 全局模型评估（重计算） |
| 22 | POST | `/api/llm/audit/run` | 写全局 `llm_audit` + LLM 花费 |
| 23 | POST | `/api/llm/decision/restore` | 改全局 LLM 裁决状态 |
| 24 | GET | `/api/notify/status` | **实测泄露 PII**：`notify.status()` 返回 `smtp_host` 与 `email_to`（真实收件地址，`notify.py:82-87`）。多用户后必须 A，并顺带**脱敏**（`email_to` → `a***@example.com`） |

### 6.4 P 清单（3 条）与"必须在多用户改造中重新定级"的 4 条

**P（保持公开，但要去敏）**：

| 方法 | 路径 | 处理 |
|---|---|---|
| GET | `/` | 返回 `index.html`（`api_app.py:2138`）→ 未登录时前端渲染**登录页**（同一个 HTML，JS 判断 `/api/auth/me` 401 后切视图；无需服务端重定向） |
| GET | `/static/*` | 静态资源（`api_app.py:50` 的 mount）。⚠️ nginx 已对它做 `proxy_cache` + 7d 缓存，**登录页 JS 必须走带 `?v=` 的版本号**，否则改完登录逻辑用户拿到旧 JS（该文件已有 no-store，但 nginx 用 `proxy_ignore_headers Cache-Control` 强行缓存，见 `/etc/nginx/sites-available/ssq` 的 `/static/` 段） |
| GET | `/api/health` | 保留公开（容器 healthcheck 依赖），但**删除 `api_auth_enabled`**，只留 `status`/`version`/`schema`，见 §5.4 |

**必须重新定级（当前是 U，改造后要动）**：

| 路径 | 现状 | 改造后 | 理由 |
|---|---|---|---|
| `GET /api/config/llm` | 返回**全局** base_url/model/disabled（`api_app.py:753-773`） | **U（仅本人）** | 归 U2：每用户一份，admin 另看全局默认。**注意**：该响应虽不含 `api_key`，但 `base_url` 指向 `https://magpie.ivyxy.com/v1`（内网 LLM 网关），对普通用户也属配置泄露 → 建议普通用户只回 `configured: true/false` + 模型名 |
| `POST /api/config/llm` | 改**进程级** `config.LLM_*` 并写全局 `llm_config.json`（`api_app.py:775-828`） | **U（仅本人，归 U2）**；全局默认改走 `A` 的新端点 | 当前实现会 `os.environ[...] = ...` 改**全局可变状态**，多用户下这是"用户 A 改 key 影响用户 B"的直接原因 |
| `GET /api/info` | 返回完整 `milestones` 长文本 + `"plan": "docs/UPGRADE_PLAN.md + docs/M3_M4_PLAN.md"`（内部文档路径） | **P（去敏）** 或 **U** | 不至于泄露密钥，但把内部路线图与文档路径送给匿名访客属无必要暴露 |
| `GET /api/tasks`、`GET /api/tasks/{task_id}` | 返回**全站所有**任务（`db.list_tasks(limit)` 无过滤）+ 任意 `task_id` 可查（`api_app.py:621-628`） | **U（但必须按 owner 过滤 → 见 §10.3 IDOR）** | 这两个是**多用户后最典型的 IDOR**：`task_id` 是 `uuid4` 不易猜，但 `GET /api/tasks` 直接列出别人的 id 和 `result_json` |

### 6.5 顺带记录的两个"设计异味"（不在本次范围，但鉴权会让它们变危险）

1. **`GET /api/data/reconcile` 是写操作**（`api_app.py:55-58` 调 `data_check.run()`）。GET + 副作用 = 可被 `<img src>`、搜索引擎预取、浏览器预连接触发。调成 admin-only 后风险降低（攻击者得先是 admin），但**仍建议改成 POST**。
2. **`GET /api/predict` 与 `POST /api/predict` 共用同一处理函数**（`@app.api_route(..., methods=["GET","POST"])`）。GET 会**生成预测并消耗 LLM 配额** → 违反 HTTP 语义，且让 CSRF 防御变复杂（GET 被 CSRF 中间件豁免，但它其实是个写操作）。**建议拆开**：GET 只读缓存（无则 404 提示"点生成"），POST 才生成。这条直接影响 §2.4 的 CSRF 豁免名单——**豁免名单里不能有会写数据的 GET**。

---

## 7. 鉴权如何"只改一处"：中间件 + contextvars

### 7.1 先排除"给 79 个端点加 `Depends`"

| 方案 | 改动点数 | 问题 |
|---|---|---|
| 每个端点加 `user: User = Depends(require_user)` | **79 处**（79 个同步 def，实测） | ① 漏加一处 = 一个洞，而 79 处靠人肉保证不可审计；② 未来新增端点默认**无保护**（默认放行，方向错）；③ 每个 PR 都要 diff 79 行噪声；④ 无法在"端点执行前"统一做 IP 限流/审计 |
| 每个端点加装饰器 | 79 处 | 同上，且装饰器顺序与 FastAPI 的 `@app.get` 顺序耦合，易错 |
| **一个 HTTP 中间件（推荐）** | **1 处替换** | ① 默认拒绝（不在白名单即 401）；② 新端点自动受保护；③ 审计/限流在同一处 |

**关键结论**：鉴权是**横切关注点**，必须放在"所有端点之前"的那一层。本系统已有这个层（`api_app.py:34` 的 `@app.middleware("http")`），**改造 = 把 fail-open 的那 12 行换成 fail-closed 的实现**，而不是新增 79 处。

### 7.2 用户上下文如何到达端点：`contextvars` **实测成立**（这是本方案的前提）

**核心疑问**：中间件是 `async def`，而 79/80 端点（实测）是**同步 `def`**，由 AnyIO 丢进线程池执行。`contextvars` 在线程池里会丢吗？

**实测（在 `.venv/bin/python` 里构造最小 FastAPI app，走真实 `TestClient` → 真实 anyio 线程池）：

```python
cv = contextvars.ContextVar("user", default=None)

@app.middleware("http")
async def mw(request, call_next):
    cv.set("alice")
    return await call_next(request)

@app.get("/sync")
def sync_ep():
    return {"ctx": cv.get(), "thread": threading.current_thread().name}

@app.get("/async")
async def async_ep():
    return {"ctx": cv.get(), "thread": threading.current_thread().name}
```

```
sync  def : {'ctx': 'alice', 'thread': 'AnyIO worker thread'}          ← ✅ 保留
async def : {'ctx': 'alice', 'thread': 'asyncio-portal-...'}          ← ✅ 保留
```

**结论：在 async 中间件里 `cv.set()`，同步 `def` 端点里 `cv.get()` 能拿到值。** 原因是 AnyIO 的 `to_thread.run_sync` 会复制当前 context 到工作线程。

**但必须知道它在哪里会丢**（同样实测，四组对照）：

```
threading.Thread            -> None    ❌
concurrent.futures.Executor -> None    ❌
loop.run_in_executor(None,fn)-> None   ❌
run_in_executor(None, copy_ctx().run, fn) -> 'alice'  ✅
```

**这直接命中本系统的两处真实代码**：

| 位置 | 代码 | 后果 | 处置 |
|---|---|---|---|
| `api_app.py:604` | `threading.Thread(target=_work, daemon=True).start()` | 后台工作的线程拿不到当前用户 → 若它要写"我的"预测，会写错人/写不进去 | **在 `start()` 前把 `user_id` 显式捕获为闭包参数**（`threading.Thread(target=_work, args=(user_id,))`），不要依赖 contextvar |
| `api_app.py:2848` | `threading.Thread(target=_run, daemon=True, name=f"bg-{tid}").start()` | 同上（这是通用后台任务 runner） | 同上；并把 `user_id` 写进 `tasks` 表的列（U3） |
| `api_app.py:2387` | `_scheduler_loop()` 的调度线程 | 无人类主体 | 显式注入 `SYSTEM_USER_ID = 0`（§4.0） |

> **给 U2 的强提示**：U2 要在不改端点签名的情况下把 user 上下文送进 `llm_client`。上面四条对照就是它的全部答案：**async 中间件 → contextvars 可用；一旦跨 `threading.Thread` 就断**，所以 `llm_client` 若在后台线程里被调用，必须显式传 `user_id`。**不要在这件事上赌。**

### 7.2.1 **总裁决：租户传递用显式参数，contextvar 只用于深层库**（覆盖本文早前的"全隐式"倾向）

U3 实测发现的一条**静默数据错误**风险，Lead 已采纳为定稿口径，本节优先级高于 §7.2 的任何暗示：

| 通道 | 结论 | 理由 |
|---|---|---|
| **`api_app` → `db` 层的租户传递** | **必须显式参数**：`load_predictions(issue, user_id=...)`、`list_tasks(limit, user_id=...)` 等 | `_bg_run`（`api_app.py:2848`）与调度器（`:2387`）是**裸 `threading.Thread`，不继承 contextvars**（§7.2 实测第四节）。上下文一丢，`cv.get()` 返回 `None` → 若代码写成 `user_id = cv.get() or SYSTEM_USER_ID`，就**静默落到 SYSTEM/admin 写入**。**这类 bug 不会让测试变红，只会错数据**——比崩溃更危险 |
| **`api_app` / `db` → `llm_client` 等深层库** | **允许 contextvar** | 这些函数的签名无法轻易改（`llm_client.py` 789 行、调用点分散），显式传参的成本高于收益；且它们**在端点线程内同步调用**，contextvar 可用（§7.2 实测第一节） |
| **跨线程（`threading.Thread`）** | **必须显式传参** | 无例外。contextvar 一定断（实测四组对照） |

**落地的两个具体后果（实施方必须知道，否则会低估工作量）**：

1. **`db.py` 里 user-scoped 的函数要新增 `user_id` 关键字参数**。按当前代码，涉及 **7 个函数**：`save_predictions`（`db.py:353`）、`load_predictions`（`:372`）、`load_last_predictions`（`:407`）、`_recent_prediction_issues`（`:387`）、`create_task`（`:694`）/`load_task`（`:727`）/`list_tasks`（`:740`）。全部用 `def f(..., user_id: int = SYSTEM_USER_ID)` 形式，**默认值给系统主体而不是必填**——这样旧调用点不会立刻崩，但**必须逐个改成显式传**（默认值只是过渡，不是终点）。
2. **测试里 13 处 `lambda i:` 需要改成 `lambda i, **_kw:`**（实测 `grep -rn "lambda i:" tests/*.py` = 13 处）。这是"显式传参"相对"隐式 contextvar"的**唯一实质代价**，可接受；换来的收益是"后台线程写错人"这一类 bug 从"可能发生且测不出"变成"不可能编译通过"。

> **一句话**：**contextvar 是"能不传就不传"的便利，不是"责任边界"。"谁的数据"这种身份信息必须出现在函数签名上。** 本文件此前 §7.6 的"80 端点签名改动 0"**只针对 HTTP 端点**（它们仍无需改动），**不覆盖 db 层函数**——请勿据此认为整条链路都无需改签名。


### 7.3 中间件实现要点（关键片段，非完整实现）

```python
# lottery/auth.py（新建）
_PUBLIC_EXACT = {"/", "/api/health", "/api/auth/login", "/api/auth/logout",
                 "/api/auth/me", "/api/auth/bootstrap"}
_PUBLIC_PREFIX = ("/static/",)
_ADMIN_PREFIX = ("/api/admin/",)          # 新增的管理端点整段由前缀兜住
# "全局写操作"必须显式列举（因为它们在 /api/ 下，不共享前缀）
_ADMIN_EXACT = { ... §6.3 的 24 条 ... }

def required_role(path: str) -> str | None:
    if path in _PUBLIC_EXACT or path.startswith(_PUBLIC_PREFIX):
        return None                        # 公开
    if path.startswith(_ADMIN_PREFIX):
        return "admin"
    if path in _ADMIN_EXACT:
        return "admin"
    return "user"                         # ← 默认：任何未列举路径都需要登录（deny-by-default）

# ── Lead 裁决 8：解析逻辑必须是 api_app 的模块级函数，中间件只调用它 ──
def _resolve_session_user_id(request: Request) -> Optional[int]:
    """解析 Cookie/Header → user_id；无有效会话返回 None（**绝不回退 admin/SYSTEM**）。

    这是全站唯一的身份解析入口。它必须能在测试里被
    ``monkeypatch.setattr(api_app, "_resolve_session_user_id", lambda r: ADMIN_ID)`` 替换，
    因此：① 模块级（不是中间件内部嵌套函数）；② 同步签名（不起协程）；
    ③ 不读 contextvar、不依赖中间件状态（纯函数式：request → user_id | None）。
    """
    ...

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    need = required_role(path)
    if need is None:
        return await call_next(request)

    uid = _resolve_session_user_id(request)        # ← 唯一解析入口（可在测试里被 monkeypatch）
    if uid is None:
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401,
                            headers={"WWW-Authenticate": "Cookie"})

    user = load_active_user(uid)                   # 带 30s 缓存；只取 status='active' 的行
    # 【裁决 7】status 闸门必须在角色判断**之前**：bootstrap/locked/disabled 一律拒绝
    if user is None:
        audit("authz.denied", actor_id=uid, path=path, reason="inactive_status")
        return JSONResponse({"ok": False, "error": "账户不可用"}, status_code=403)

    if need == "admin" and user["role"] != "admin":
        audit("authz.denied", actor=user, path=path, required="admin")
        return JSONResponse({"ok": False, "error": "需要管理员权限"}, status_code=403)

    _current_user.set(user)                        # ← 仅供 llm_client 等深层库读取（见 §7.2）
    try:
        return await call_next(request)
    finally:
        _current_user.set(None)                    # 防泄漏到复用的线程
```

**五条实现纪律**：
1. **解析逻辑必须外提为 `_resolve_session_user_id`（裁决 8）**：`app.dependency_overrides` **对 HTTP 中间件无效**，解析若内联，**36 处 `TestClient` 测试**就无法用一个 fixture 统一注入身份，只能逐个改成"注入真实 session cookie"——**爆炸半径从 1 处 fixture 变成 36 处构造点**。外提后：conftest 一行 `monkeypatch.setattr(api_app, "_resolve_session_user_id", lambda r: ADMIN_ID)` 即全局生效；多用户场景再配 `login_as(client, user_id)` 切换。
2. **`status` 闸门先于 `role` 判断（裁决 7）**：bootstrap 行可能写着 `role='admin'`，但 `status='bootstrap'` 必须被这里挡死。**顺序错了就是一个零密码管理员**（§4.1 不变量 1）。
3. **`finally` 必须清理 contextvar**：AnyIO 工作线程会被复用，残留值会把 A 的身份带给 B 的请求（真实事故模式）。
4. **`required_role` 必须是纯函数 + 单元测试覆盖 80 条路由**：一条参数化测试遍历 `app.routes`，断言"每条路由都有明确分级"——**将来新增端点忘记定级会直接挂测试**，而不是静默变成 user 可访问。
5. **不要在中间件里同步阻塞**：`load_active_user` 若查 DB，而 `db.py` 是同步单连接，`async def` 里直接调用会**阻塞事件循环**。要么用缓存（§4.2 ②），要么 `await run_in_threadpool(...)`。**这是最容易写错的一行。**

### 7.4 前端改造点：**1 个函数**（已实测全文件只有 1 处 `fetch`）

```javascript
// web/static/app.js:55-56  ← 全站唯一网络出口（grep -c 'fetch(' 结果 = 1）
async function api(path, opts = {}) {
  const r = await fetch(path, opts);
```

改为：

```javascript
async function api(path, opts = {}) {
  opts = { ...opts, credentials: "same-origin" };      // Cookie 自动带（同源默认即如此，显式写更稳）
  const m = (opts.method || "GET").toUpperCase();
  if (m !== "GET" && m !== "HEAD") {
    opts.headers = { ...(opts.headers || {}), "X-CSRF-Token": readCookie("ssq_csrf") || "" };
  }
  const r = await fetch(path, opts);
  if (r.status === 401) { showLogin(); throw new Error("未登录"); }   // ← 统一跳登录
  ...
}
```

**改动量**：`api()` 内 6 行 + 新增 `showLogin()`/`login()`/`logout()` 三个函数 + 管理页一个 section。
**不需要改**：4203 行里其余所有业务调用（它们全走 `api()`）。**这是本系统对多用户改造最友好的事实**——如果前端有 200 处裸 `fetch`，这一节的工作量会翻 10 倍。

### 7.5 服务端主体（中间件管不到的地方）

中间件只能管 HTTP。以下三处必须**代码里显式指定主体**，否则多用户后行为不可预测：

| 位置 | 现状 | 要求 |
|---|---|---|
| `api_app.py:2387` `_scheduler_loop` | 每 1800s 跑 `_scheduler_step`，`:2214-2222` **直接生成并落库预测** | 以 `user_id = config.SYSTEM_USER_ID`（= **0**，Lead 裁决 3）写入；**不消耗任何人的 LLM 配额**，只走统计通道 |
| `api_app.py:604` `_work` 线程 | 无主体 | 由调用端点显式传 `user_id`（contextvars 在此已断，见 §7.2） |
| `api_app.py:2848` `bg-{tid}` 线程 | 无主体 | 同上 + 写 `tasks.user_id`（**`NOT NULL DEFAULT 0`，禁止 `NULL`**） |

> **常量落点（裁决 3 要求）**：`SYSTEM_USER_ID = 0` 必须定义在 `lottery/config.py`（与 `APP_VERSION`、`LLM_*` 同处），**不要在多个文件里各写一个字面量 0**——否则将来若要改哨兵值，会漏改。调度器、`tasks` 默认值、`predictions` 回填兜底三处共用同一个常量。

### 7.6 改造点数量估算（可直接用于排期）

| 类别 | 数量 | 说明 |
|---|---|---|
| **新建文件** | 2~3 | `lottery/auth.py`（哈希/会话/引导）、`lottery/authz.py`（分级表；也可并入 auth.py）、`lottery/auth_api.py`（登录+管理路由；也可并入 api_app.py） |
| **替换中间件** | **1 处** | `api_app.py:34-45`（12 行 → ~50 行），并抽出一个模块级 `_resolve_session_user_id`（**裁决 8**） |
| **新增端点** | **13 条** | 登录/登出/me/引导/自改密（5）+ 用户管理 CRUD（6：list/create/patch/reset-password/delete + 详情）+ 审计查询（1）+ LLM 配置重置（1，归 U2） |
| **需显式取 `user_id` 的既有端点** | **7 条路由 / 6 个函数** | `/api/predict`（GET+POST 同函数）、`/api/predictions/last`、`/api/predictions/history`、`/api/tasks`、`/api/tasks/{task_id}`、`/api/config/llm`（GET+POST 两函数）、`/api/llm/cost`。**注**：若 U3 选择"全站共享生成、各自只读"，此项可降为 **3 条**（只剩 tasks 与 llm cost） |
| **HTTP 端点函数签名改动** | **0** | 其余 72 条路由**一行不动**（靠中间件 + `auth_api` 提供的 helper） |
| **`db.py` 函数签名改动（显式传参）** | **7 个函数** | 见 §7.2.1：`save_predictions`(`:353`)、`load_predictions`(`:372`)、`load_last_predictions`(`:407`)、`_recent_prediction_issues`(`:387`)、`create_task`(`:694`)、`load_task`(`:727`)、`list_tasks`(`:740`) 各加 `user_id: int = SYSTEM_USER_ID` |
| **测试适配** | **13 处** | `tests/` 里 `lambda i:` → `lambda i, **_kw:`（实测 13 处）；外加 conftest 一处 `monkeypatch.setattr`（裁决 8） |
| **前端** | 1 函数 + 3 视图 | §7.4 |
| **迁移** | 1 个（v5） | §9 |
| **测试新增** | 参数化 1 组覆盖 80 路由 + 用例 ~28 条 | 见 §12.3 |

**一句话给排期**：**核心鉴权 1 人日左右可跑通，真正的成本在"把 13 条新端点的边界情况写对"与"迁移 + `db` 层显式传参改造 + 测试适配"，而不是"改 79 个端点"。**

---

## 8. 管理员 API 契约

### 8.1 统一约定

```
前缀：/api/admin/*          ← 中间件按前缀整体兜成 admin-only（第一条防线）
响应：成功 = {"ok": true, ...}；失败 = {"ok": false, "error": "<人话>", "code": "<机器码>"}
错误码：400 参数错 / 401 未登录 / 403 非 admin / 404 不存在 / 409 冲突（重名/自锁）
       /410 引导已完成 / 422 校验失败 / 429 限流
幂等：所有写操作接受可选 `Idempotency-Key` 头；同一 key + 同一 actor 在 10 分钟内
      重复提交 → 返回首次结果（不重复创建、不重复审计）
脱敏：任何返回 users 的响应**禁止**出现 password_hash；统一用白名单序列化
```

**用户对象（唯一输出形状，前后端共用）**：

```json
{ "user_id": 2, "username": "bob", "role": "user", "status": "active",
  "display_name": "Bob", "llm_profile": "default",
  "has_llm_key": true, "last_login_at": 1760000000.0,
  "created_at": 1759000000.0, "failed_count": 0, "locked_until": 0 }
```

> `has_llm_key`（布尔）而不是 `api_key`（字符串）——**这是 U2 的脱敏约定在 U1 的落点**：管理员也只能看到"有没有配"，看不到 key 本身。

### 8.2 端点清单

| # | 方法 | 路径 | 用途 | 幂等 | 审计 action |
|---|---|---|---|---|---|
| 1 | POST | `/api/auth/login` | 登录建会话 | 是（同凭据重复登录 = 新会话，旧会话保留至过期） | `auth.login` |
| 2 | POST | `/api/auth/logout` | 登出（撤销当前会话） | 是 | `auth.logout` |
| 3 | GET | `/api/auth/me` | 当前用户信息（前端启动探测） | 是 | — |
| 4 | POST | `/api/auth/password` | 自改密码（需旧密码） | 否 | `user.password_change` |
| 5 | POST | `/api/auth/bootstrap` | 首启引导 | 一次性（§5.2） | `auth.bootstrap` |
| 6 | GET | `/api/admin/users` | 列表（分页/过滤） | 是 | — |
| 7 | POST | `/api/admin/users` | 创建用户 | **是**（`Idempotency-Key` 或 username 冲突 → 409） | `user.create` |
| 8 | GET | `/api/admin/users/{user_id}` | 详情 | 是 | — |
| 9 | PATCH | `/api/admin/users/{user_id}` | 改 role / status / display_name | 是（PATCH 语义天然幂等） | `user.role_change` / `user.disable` / `user.enable` |
| 10 | POST | `/api/admin/users/{user_id}/password` | 重置口令（返回**一次性**新口令） | 否（每次生成新口令） | `user.password_reset` |
| 11 | DELETE | `/api/admin/users/{user_id}` | 软删除 + 归档改名 | 是（重复删 → 200 且 `already_deleted: true`） | `user.delete` |
| 12 | POST | `/api/admin/users/{user_id}/llm/reset` | 重置该用户 LLM 配置（**实现归 U2**） | 是 | `user.llm_reset` |
| 13 | POST | `/api/admin/users/{user_id}/sessions/revoke` | 踢下线 | 是 | `session.revoke` |
| 14 | GET | `/api/admin/audit` | 审计查询（`?action=&actor_id=&since=&limit=`） | 是 | — |

### 8.3 关键请求/响应（只给易错处）

```
POST /api/admin/users
Body: { "username": "bob", "password": "<≥12>", "role": "user", "display_name": "Bob" }
201:  { "ok": true, "user": { ...用户对象... } }
409:  { "ok": false, "code": "username_taken", "error": "用户名已存在" }
400:  { "ok": false, "code": "weak_password", "error": "密码至少 12 位且需包含字母与数字" }

POST /api/admin/users/{id}/password
200:  { "ok": true, "username": "bob", "new_password": "<一次性随机 16 字符>",
       "note": "此口令仅显示一次，请通过安全渠道转交" }
       ← 同时：token_version+1（该用户所有会话立即失效）、failed_count 清零、locked_until=0
       ← 审计只记 actor/target，**绝不记 new_password**

DELETE /api/admin/users/{id}
→ 软删：deleted_at=now, status='disabled', username = username || '#deleted#' || user_id
→ **必须在同一个显式事务里清理关联行**（Lead 裁决 1）：
     DELETE FROM user_sessions   WHERE user_id = ?;   -- 会话（安全相关，必须删）
     DELETE FROM user_llm_config WHERE user_id = ?;   -- U2 的表；**不依赖 FK 级联**
→ 保留其 predictions / tasks / 审计（软删除不销毁业务事实，数据可追溯）
→ 该用户若已登录：下一个请求 401（会话行已删）
```

> **⚠️ 裁决 1 的落地纪律**：`user_llm_config` 上的 `REFERENCES ... ON DELETE CASCADE` **只能当冗余防御**，**绝不能作为"已经删干净"的依据**——本库 `PRAGMA foreign_keys` 实测为 **0**，级联根本不触发；即使将来打开，它也是**每连接**设置，换连接池/多进程即静默失效。**显式事务是唯一正确性来源，级联只是保险丝。**

**必须有一条"不依赖 PRAGMA"的测试**（对应 §12.3 验收项 13）：

```
建用户 A → 给 A 建会话 + LLM 配置 → 删除 A
→ 断言 user_sessions   WHERE user_id=A 的行数 == 0
→ 断言 user_llm_config WHERE user_id=A 的行数 == 0
→ 断言 predictions     WHERE user_id=A 的历史行仍存在（软删不销毁业务数据）
前置条件：该测试**不得**执行 "PRAGMA foreign_keys=ON"，以保证验证的是应用层逻辑
```

### 8.4 自保护规则（**必须有，否则一次误操作锁死系统**）

| 场景 | 行为 | 原因 |
|---|---|---|
| admin 删除自己 | **409 `cannot_delete_self`** | 自断后路 |
| 删除/禁用**最后一个** admin | **409 `last_admin`** | 系统失去管理能力，且 `/api/auth/bootstrap` 已 410 → **永久锁死**，只能改库 |
| 把**最后一个** admin 降级为 user | **409 `last_admin`** | 同上 |
| 禁用自己 | **409 `cannot_disable_self`** | 会把自己踢下线 |
| 管理员改自己密码 | 允许，但**强制撤销其他会话**（保留当前会话） | 改密后应把"别人用旧密码登的会话"清掉 |
| 用户名 = 已软删用户的用户名 | 允许（软删时已改名归档，原名已释放） | 见 §4.1 |

**实现要点（防 TOCTOU）**：判"最后一个 admin"必须与写操作在**同一事务**内——`SELECT COUNT(*)` 后再 `UPDATE` 的两步写法在并发下会同时通过。本系统 SQLite 单连接、无 WAL，实际并发风险低，但**写法上仍应放在一个 `BEGIN IMMEDIATE` 事务里**（可参照 `migrations.py:263-294` 的 `_run_batch` 显式事务范式，别自创）。

### 8.5 前端改动点（管理页最小形态）

| 页面/控件 | 位置 | 说明 |
|---|---|---|
| 登录遮罩 | `index.html` 新增 `<div id="loginView">` | 全屏遮罩，用户名 + 密码；不新开路由（纯静态无构建，不引 router） |
| 启动探测 | `app.js` 启动处 | `api('/api/auth/me').catch(showLogin)` |
| 用户管理入口 | 仅 `role==='admin'` 时显示 | 放在现有"更多"抽屉（`index.html:53` 的 `openMore()`）里，不加新导航层 |
| 用户管理面板 | 新 section | 表格（用户名/角色/状态/最后登录）+ 新建/禁用/改角色/重置密码/删除 |
| 改密入口 | 页脚或"更多" | 所有人都要能改自己密码 |

> **与第 2 点（精简功能）的协同**：管理页**不要**做成"又一个复杂后台"。M4 的目标是减少页面数，所以这张表格只放 6 个动作，**不引入权限细粒度编辑、不引入用户组**。

### 8.6 审计查询端点

```
GET /api/admin/audit?action=authz.denied&actor_id=2&since=1759000000&limit=100
200: { "ok": true, "items": [ { "ts":..., "actor_name":"bob", "action":"auth.login_failed",
                                "target_name":"", "result":"failed", "ip":"203.0.113.7",
                                "detail_json":"{\"reason\":\"bad_password\"}" } ], "total": 42 }
```

**注意**：`detail_json` 里**永远不能有** `password`/`api_key`/`session_id` 的明文。落地时在 `audit()` 函数内部做**兜底脱敏**（正则替换 `sk-[A-Za-z0-9]{8,}`、`password`/`token`/`key` 字段名置 `***`），而不是靠每个调用点自觉——**审计日志本身不能变成新的泄露源**。

---

## 9. 迁移策略（schema_version 4 → 5）

### 9.1 结论先行：**U1 的迁移完全符合现有 `forward-only` 契约；冲突发生在 U3，不在 U1**

`migrations.py:100-101`（以及 `:67-68`）明确写着迁移的契约：

```
forward-only：只有 CREATE TABLE IF NOT EXISTS / CREATE INDEX，
无 DROP / RENAME / 改类型（db.get_conn() 首次连库即自动落地，无回滚路径）。
```

- **U1 需要建 3 张全新表**（`users` / `user_sessions` / `user_audit_log`）→ 全是 `CREATE TABLE IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS`，**100% 符合契约，不需要改 `migrations.py` 的任何机制**。
- **U3 需要改 `predictions` 的主键**（`PRIMARY KEY (issue, seq)` → `(user_id, issue, seq)`，`db.py:63`）→ SQLite 不支持 `ALTER TABLE ... ALTER COLUMN`，**必须走"建新表 + INSERT SELECT + DROP 旧表 + RENAME"**，这与上述契约**正面冲突**。

> **给 Lead / U3 的明确移交项**：`predictions` 主键迁移**不能**复用现成范式，必须由 U3 决定是 ① 扩展迁移框架显式允许"表重建"（并在 `_run_batch` 的事务里做，SQLite 的 DDL 事务性可保证原子）、还是 ② 新增一张 `predictions_v2` 表双写过渡。**U1 不越界做这个决定，但必须在文档里指出这个冲突存在**，否则 U3 会照抄"forward-only"结论然后卡住。

### 9.2 v5 迁移内容（纯增量）

```python
# lottery/migrations.py 追加（不修改既有 1~4 号迁移）
def _ensure_users(conn: sqlite3.Connection) -> None:
    """U1：多用户身份基线（users / user_sessions / user_audit_log）。"""
    conn.execute("""CREATE TABLE IF NOT EXISTS users ( ... §4.1 DDL ... )""")
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS uq_users_username_alive
                    ON users(username) WHERE deleted_at IS NULL""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_users_status
                    ON users(status, deleted_at)""")
    # Lead 裁决 2：主 admin 唯一性锚点（**不依赖 user_id = 1**）
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS uq_users_primary_admin
                    ON users(is_primary_admin) WHERE is_primary_admin = 1""")
    conn.execute("""CREATE TABLE IF NOT EXISTS user_sessions ( ... §4.2 DDL ... )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON user_sessions(user_id, revoked_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_exp  ON user_sessions(expires_at)")
    conn.execute("""CREATE TABLE IF NOT EXISTS user_audit_log ( ... §4.3 DDL ... )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_uaudit_ts  ON user_audit_log(ts DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_uaudit_act ON user_audit_log(action, ts DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_uaudit_tgt ON user_audit_log(target_id, ts DESC)")

MIGRATIONS = [
    ...,  # 1~4 号保持不变（编号冻结，见 migrations.py:117-124 的注释惯例）
    (5, "u1 identity baseline", _ensure_users),
]
```

> **注意 `db.py` 的 `SCHEMA` 常量（`db.py:12-157`）与新迁移的关系**：`get_conn()` 先 `executescript(SCHEMA)`（`db.py:169`）再 `migrations.apply()`（`:171`）。**新表必须只放在迁移里，不要同时塞进 `SCHEMA`**——否则"全新库"会跳过备份逻辑的差异判断（`has_existing_data`，`migrations.py:185-211`），且两处 DDL 会各自漂移。**唯一例外**：若希望全新库一步到位，放 `SCHEMA` 里更简单——但那样 v5 迁移对全新库就是空操作（`IF NOT EXISTS` 保证幂等）。**推荐：只放迁移，保持 `SCHEMA` 冻结**，理由是 `SCHEMA` 里已有的表都是"业务数据表"，身份表属于新阶段产物，混进去会让 `db.py` 继续膨胀（`db.py` 已 900+ 行）。

### 9.3 历史数据归属

| 数据 | 归属 | 说明 |
|---|---|---|
| `draws`(3516) / `features`(8) / `patterns`(47) / `mining_runs`(0) / `replay_results` / `reconcile_runs` / `eval_*`（35/28/6） | **全局共享，不加 `user_id`** | 是"世界的事实"，不是"某人的决策"。详见 U3 |
| `predictions`(40) | **全部归主 admin**（按 `is_primary_admin = 1` 定位，**不写死 `user_id = 1`**） | 这 40 条都是"单用户时代的系统输出"，唯一合理的归属者是管理员。**回填 SQL 由 U3 写**，U1 只给规则 |
| `tasks`(6) | 归 `admin` | 同上 |
| `llm_audit`(0) / `llm_eval_results`(0) | 空表，无需回填 | 但表结构需 U2/U3 决定是否加 `user_id` |
| 系统后续产出（调度器） | `user_id = 0`（`SYSTEM_USER_ID`） | §4.0 / §7.5 |

**回填的执行时机问题（容易踩坑）**：v5 迁移时**还没有 admin**（首个 admin 由 §5 的引导流程在**运行时**创建，不在迁移里）。所以"把 predictions 归给 admin"**不可能在 v5 里完成**——它必须在 U3 的 v6 迁移里、且此时**仍可能没有 admin**（例如部署后管理员还没登录）。

**解法（按 Lead 裁决 2/4 修订）**：

```sql
-- v6 的回填：**用 is_primary_admin 定位，不用 user_id 数字，不用 MIN(user_id)**
UPDATE predictions
   SET user_id = COALESCE((SELECT user_id FROM users
                            WHERE is_primary_admin = 1 AND deleted_at IS NULL), 0)
 WHERE user_id IS NULL;

-- v5/v6 的列默认值一律 DEFAULT 0（SYSTEM_USER_ID），**禁止 DEFAULT 1**
-- 理由（裁决 4）：老代码不写 user_id 时落到"系统主体"，语义等于"迁移前的历史数据"，
-- 比落到"admin 名下"更诚实——admin 是某个人，历史数据不是这个人的决策。
```

三条不可协商的点：
1. **定位 admin 只认 `is_primary_admin = 1`**（或退一步用 `username` + `role='admin'` 双条件），**绝不写 `user_id = 1`**——`AUTOINCREMENT` 下删光重插会得到 2（Lead 裁决 2 已实测）。
2. **落地值必须是 0 或真实 admin id，绝不留 `NULL`**：`NULL` 会让 `WHERE user_id = ?` 永远查不到 → 40 条历史凭空消失，这是最坏的失败模式（也是裁决 3 否决 NULL 的同一理由）。
3. **所有新列的 `DEFAULT` 一律 `0`**，不写 `1`（裁决 4）。

> **对 U3 的直接约束**：U3 原提"v5 一次把列加齐且全部带 `DEFAULT 1`"**已作废**，改为 `DEFAULT 0`；历史 40 行由迁移脚本**显式** `UPDATE` 给主 admin。这既满足"老代码不写 `user_id` 也照常工作"，又不制造"历史数据是 admin 的决策"这一假象。

### 9.4 回滚（三层，按代价从低到高）

| 层 | 操作 | 代价 | 适用 |
|---|---|---|---|
| **L1 代码回滚，保留新表** | 回退镜像到上一版，**不动数据库** | **零数据损失、秒级** | **首选**。因为 v5 是**纯增量 DDL**，旧代码不认识新表也不会受影响（SQLite 允许库里有旧代码不知道的表）。旧代码继续以"匿名单用户"运行 |
| **L2 数据库回滚** | 停容器 → `cp data/ssq.db.pre-migrate-v4 data/ssq.db` → 起容器 | 丢失 v5 之后产生的**全部**数据（包括新建用户、会话、审计） | 仅当 v5 本身有结构缺陷、L1 无法解决 |
| **L3 重放备份** | 从 `data/ssq.db.pre-migrate-v*`（实测已有 `-v2`、`-v3`）逐级恢复 | 更旧的数据损失 | 灾难恢复 |

**备份已自动生成**（`migrations.py:296-321` 的 `apply(backup=True)`）：真正有迁移待执行且库内**已有数据**时，先做 `Connection.backup()` 一致性热备到 `data/ssq.db.pre-migrate-v4`，**备份失败则中止迁移**（fail-closed）。本次迁移时 `schema_version=4`（实测），所以备份文件名是 **`ssq.db.pre-migrate-v4`**。

**回滚演练（上线前必做一次，10 分钟）**：
```bash
# 1. 记录基线
sqlite3 data/ssq.db "SELECT MAX(version) FROM schema_version; SELECT COUNT(*) FROM users;"
# 2. 触发迁移（重启容器即可，migrations 是懒加载：db.get_conn() 首次连库自动执行）
docker compose up -d --build
# 3. 确认新版本 + 备份文件存在
curl -s localhost:18000/api/health | ...   # schema.version 应为 5
ls -l data/ssq.db.pre-migrate-v4
# 4. 回滚 L1：回退镜像 tag，确认旧代码能正常跑（旧代码不认识 users 表也不该报错）
# 5. 回滚 L2（演练时用副本，不要动生产库）
```

### 9.5 迁移期的"锁"风险（本系统特有的坑）

`db.get_conn()` 是**懒加载**（`db.py:162-174`）：第一个请求进来时才建表 + 跑迁移。而 `entrypoint.sh` 在启动前已经执行了 `python -m lottery.cli fetch`、`backtest`，这些都会触发 `get_conn()` → **迁移实际发生在容器启动的早期**，而不是第一个 HTTP 请求时。

**风险**：迁移期间 `BEGIN IMMEDIATE` 持有写锁，而此时 `entrypoint.sh` 的 `backtest` 可能正在读 → 因无 `busy_timeout`（实测全库无 `PRAGMA busy_timeout`）→ **`database is locked` 立即抛错**。

**缓解**：迁移期间是**单进程串行**的（`entrypoint.sh` 是顺序 shell），不会真并发；但**必须验证**容器的 `restart` 策略能在迁移抛错后不陷入重启风暴。建议在 `migrations.py` 的 `apply()` 开头加 `conn.execute("PRAGMA busy_timeout=10000")`（一行，属于第 1 点范畴，但**与本次迁移直接相关**）。

---

## 10. 攻击面清单与缓解（多用户带来的**新增**面）

**总览**：多用户改造把系统从"无认证"变成"有认证"，**净效果是攻击面显著缩小**（80 个匿名端点 → 3 个公开端点）。但**新增了 8 类此前不存在的攻击面**，下面逐条给缓解与残余风险。

### 10.1 暴力破解与撞库

**新增原因**：出现了"可被猜测的凭据"（用户名+密码）。当前系统**零限流**（实测 `grep -rn "429|rate_limit"` 只命中 `llm_client.py` 对**上游 LLM** 的退避，不是客户端限流）。

**缓解（三层，全部必需）**：

| 层 | 规则 | 实现要点 |
|---|---|---|
| **① 账号维度：失败计数 + 渐进锁定** | 连续 5 次失败 → `locked_until = now + 60s`；之后每多失败一次翻倍（60s→120s→…→上限 15 分钟）。成功登录清零 | 复用 `users.failed_count` / `locked_until`（§4.1 已含）。**不要永久锁定**（会变成 DoS 手段，见下） |
| **② IP 维度：令牌桶** | `login:<ip>` 桶：容量 10，每分钟补 5。超限 → `429` + `Retry-After` | `X-Real-IP`（§10.6）。进程内 `dict` + `threading.Lock`（**必须加锁**：79 个同步端点跑在 40 线程池，无锁 dict 会竞态）。**容量上限 4096 条 + LRU 淘汰**，防内存被刷爆 |
| **③ 全局维度：熔断** | 全站登录失败 > 100 次/分钟 → 所有登录一律 429 并记 `auth.login_throttled` | 兜住分布式撞库 |

**DoS 反噬（必须处理）**：攻击者用"锁定"当武器，故意输错 **admin** 的用户名，把管理员锁在门外。
**对策**：**只对"非存在的用户名"和"已存在用户名"做同样的渐进延迟，但永不因 IP 攻击而永久锁死账号**——具体做法是"锁定按 (ip, username) 二元组，而非只用 username"。这样攻击者只能锁住"他自己那个 IP 上的 admin 登录尝试"，锁不住管理员本人的 IP。**这是一个设计细节，但决定了这个功能是防护还是自伤。**

### 10.2 会话固定（Session Fixation）

**攻击**：攻击者预先给受害者植入一个已知 `sid`，受害者登录后该 `sid` 升级为已认证 → 攻击者拿着同一个 `sid` 直接进入。

**缓解**：
1. **`sid` 只由服务端生成**（`secrets.token_urlsafe(32)`），**永不接受客户端提交的 sid**；
2. **登录成功后必须重新生成 sid**（旧的 `session_id` 直接删除，不是改属性）——升级会话 ID；
3. 会话 Cookie 用 `__Host-` 前缀（§2.3），浏览器层面锁死子域写入；
4. 登出 = 服务端 `revoked_at` + 客户端过期 Cookie（**双端都要做**，只做客户端等于没登出）。

### 10.3 越权（IDOR / 水平越权）

**这是多用户后最容易出、也最容易被忽视的一类。本系统有 3 个明确实例**：

| 实例 | 证据 | 后果 | 缓解 |
|---|---|---|---|
| **`GET /api/tasks` 返回全站任务** | `api_app.py:621-623` → `db.list_tasks(limit)`，`db.py:740-743` 的 SQL **无任何 WHERE** | 用户 A 能看到 B 的 `result_json`（可能含完整预测与 LLM 输出） | 加 `WHERE user_id=?`；`user_id=0` 的系统任务对所有人可见（只读） |
| **`GET /api/tasks/{task_id}` 任意查询** | `api_app.py:626-631` → `db.load_task(task_id)`，**无属主校验** | `task_id` 虽为 uuid 不易猜，但 `GET /api/tasks` 直接把 id 列出来了 → 组合起来就是完整 IDOR | 查出来后判 `task.user_id in (me, 0)`，否则 **404**（不是 403，避免确认"这个 id 存在"） |
| **`GET/POST /api/config/llm` 全局单例** | `api_app.py:751-826` 直接读写 `config.LLM_*` 与全局 `data/llm_config.json` | **垂直越权**：普通用户改一次就等于改全站 | 归 U2：改为每用户配置；全局默认改 `A` |

**通用规则（写进代码评审清单）**：
- 任何"按 id 取单条"的端点，**必须在 SQL 的 WHERE 里带 `user_id`**，而不是取出来再判——后者容易在早期 return 里漏掉；
- 越界的响应统一 **404**（不是 403），避免把"资源存在性"变成探测信号；
- **管理员默认也不能读他人私有数据**，除非显式设计（本系统规模下没必要给管理员后门读权限，`tasks` 除外）。

### 10.4 密码重置：**先纠正一个前提——本系统其实有邮件能力**

任务描述的假设是"本系统无邮箱怎么办"。实测**这个前提不完全成立**：

```
# notify.py:44-56 已有完整 SMTP 发送实现（smtplib + starttls + 15s timeout）
# .env 已配置（值已脱敏）：
LOTT_NOTIFY_EMAIL_SMTP_HOST=***   LOTT_NOTIFY_EMAIL_SMTP_USER=***
LOTT_NOTIFY_EMAIL_SMTP_PASSWORD=***  LOTT_NOTIFY_EMAIL_FROM=***
LOTT_NOTIFY_EMAIL_TO=***          LOTT_NOTIFY_EMAIL_SMTP_TLS=1  PORT=587
# 且每期开奖后都会真实调用（api_app.py:2166 → notify.notify_after_check）
```

**但仍不能直接拿来做自助重置**，三个硬障碍：
1. **没有"每用户邮箱"字段**——`NOTIFY_EMAIL_TO`（`config.py:475`）是**全局单一收件人**，所有通知都发给同一个人；
2. 复用它会引入 PII（用户邮箱）与投递可靠性问题（退信、进垃圾箱、SMTP 凭据是共享的）；
3. 重置令牌本身是一个**新的高价值凭据**（有效期、单次性、不可猜测、不能出现在日志/Referer 里）。

**推荐分层（按投入产出）**：

| 方案 | 优先级 | 说明 |
|---|---|---|
| **A. 管理员重置 + 线下转交（首选，P0）** | ★★★★★ | `POST /api/admin/users/{id}/password` 生成一次性强口令，**只在响应里显示一次**，管理员通过当面/电话/其他渠道转交。10 个用户的规模下，这是**最经济且最安全**的方案，且天然带审计 |
| **B. 管理员代发一次性重置链接（P1 可选）** | ★★★ | 若确实要"自助"：加 `users.email`（可空）+ `password_reset_tokens` 表（`token_hash TEXT PK, user_id, expires_at, used_at`），复用 `notify.py` 的 SMTP 发送。**令牌只存哈希**，有效期 30 分钟，一次性 |
| **C. 完全自助（邮箱验证码）** | ✗ 不推荐 | 为 10 个用户建一套完整账号恢复流程，收益/成本不成立；且 SMTP 是共享凭据，一旦被攻破可重置所有人 |

**明确禁止**：
- ❌ **安全问题**（"你母亲的生日"）——答案空间小、可社工、且会永久降低系统安全基线；
- ❌ 把"重置链接"打印在容器日志里；
- ❌ 重置令牌放进 URL 且不设 Referer 泄露防护（若做 B 方案，落地页必须 `Referrer-Policy: no-referrer`）。

### 10.5 明文旁路与文档暴露（§1.3 的两条，此处给完整处置）

| 攻击面 | 证据 | 缓解 | 验收 |
|---|---|---|---|
| `0.0.0.0:18000` 公网明文 | `docker-compose.yml:11`；实测公网 `curl` → **200** | 改 `127.0.0.1:18000:18000` + 重建容器；应用侧 `Secure` Cookie；中间件记 `authz.insecure_transport` 审计 | 公网 `curl http://IP:18000/api/health` **连接被拒/超时**；`ss -tlnp \| grep 18000` 显示 **`127.0.0.1:18000`** |
| `/docs` `/redoc` `/openapi.json` | `api_app.py:28` 默认开启；实测 **200** | 生产 `FastAPI(docs_url=None, redoc_url=None, openapi_url=None)`；或中间件 deny-by-default 兜住（**推荐后者，双保险**） | 三者均 **404** |
| CORS `*` | `api_app.py:30-32` | 删除该中间件（同源部署不需要）或收紧为具体 origin | 响应头不再有 `access-control-allow-origin: *` |

### 10.6 客户端 IP 获取与伪造（限流成败的关键）

**实测事实**（§1.4）：容器内 `request.client.host` **恒为 `172.18.0.1`**（docker 网桥网关），因为 `docker-proxy` 做了用户态转发；且 `cli.py:93` 未传 `forwarded_allow_ips`，uvicorn 默认只信 `127.0.0.1` → **不会**用 `X-Forwarded-For` 覆盖。

**正确的取法**：

```python
_TRUSTED_PEERS = {"172.18.0.1", "127.0.0.1"}     # docker 网桥网关（实测）+ 本机

def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else ""
    if peer in _TRUSTED_PEERS:
        xri = request.headers.get("x-real-ip")     # nginx 用 $remote_addr **覆盖写**，不可伪造
        if xri:
            return xri.strip()
    return peer
```

**三条纪律**：
1. **只信 `X-Real-IP`，不信 `X-Forwarded-For` 的最左值**。nginx 用 `$proxy_add_x_forwarded_for` **追加**，客户端自带的假 XFF 会留在最左边 → 取最左 = 取攻击者的值。
2. **必须先判对端是否可信**。在端口仍是 `0.0.0.0` 时，公网可直接连进来并**任意伪造 `X-Real-IP`** → 每次换假 IP 就能绕过限流。**所以 §10.5 的端口修复是 10.1 限流生效的前提**（这是两条风险之间的真实依赖，不要分开排期）。
3. **不要**为了拿到真实 IP 就开 `--proxy-headers --forwarded-allow-ips=*`——那等于信任任意来源的伪造头，比拿不到 IP 更糟。

### 10.7 其余新增面（精简表）

| # | 攻击面 | 触发 | 影响 | 缓解 | 残余风险 |
|---|---|---|---|---|---|
| 1 | **CSRF** | 用户已登录后访问恶意页 | 以受害者身份生成预测/改配置（消耗其 LLM 配额） | SameSite=Lax + Origin 校验 + 双提交 Token（§2.4） | 同站子域（15 个邻居）被攻破时可绕过 Lax；靠 Token 兜 |
| 2 | **XSS** | LLM 输出/规律名被 `innerHTML` 拼接 | 窃取会话（HttpOnly 挡住 sid，但可代发请求） | Cookie HttpOnly；渲染处改用 `textContent`；加 `Content-Security-Policy: default-src 'self'` | 4203 行 `app.js` 存在大量 `innerHTML`，**需单独一轮排查**（不在 U1 范围，但必须登记） |
| 3 | **用户枚举** | 登录报错"用户不存在" vs "密码错误"；响应时间差 | 拿到有效用户名列表 → 定向暴力破解 | 统一报错"用户名或密码错误"；**存在与不存在的用户都跑一次 scrypt**（用固定的 dummy hash）→ 消除时序差 | 若 scrypt 被限流挡住，时序差仍可测；可接受 |
| 4 | **会话表无限增长** | 每次登录插一行，从不清理 | 库膨胀 + 查询变慢 | 启动时 + 每小时清理 `expires_at < now - 7d`；索引已建（§4.2） | 低 |
| 5 | **审计日志泄露** | `detail_json` 误记口令/key | 审计表变成凭据库 | `audit()` 内置兜底脱敏（§8.6） | 需代码评审卡口 |
| 6 | **备份文件含口令哈希** | `data/ssq.db.pre-migrate-v*` 是完整库副本 | 拿到备份 = 拿到所有 `password_hash` + 用户 LLM key 密文 | 备份与库同目录、容器内不可从 Web 访问；宿主权限维持 600（**实测 `llm_config.json` 已是 600，照此口径**）；**scrypt 参数足够高是最后一道防线** | 宿主被 root 拿下则全失，符合威胁模型 |
| 7 | **LLM 成本滥用** | 登录用户疯狂调 `/api/predict` | 刷爆 API 额度（实测 0.0736 元/次） | 归 U2 的每用户日配额；U1 只负责"知道是谁"。**前置条件**：`llm_cost_log.jsonl` 必须能按 `user_id` 切分（U2） | 中 |
| 8 | **迁移失败导致服务不可用** | v5 抛错 → `get_conn()` 抛出 → 首个请求 500 | 全站不可用 | 迁移在启动早期执行（§9.5）；失败时容器重启；L1 代码回滚（§9.4）零风险 | 低 |
| 9 | **`LOTT_BOOTSTRAP_TOKEN` 残留** | 引导完成后忘删 env | 若 admin 被删光，任何人可重新引导 | 引导有 admin 时直接 410（§5.2）；`/api/health` 暴露 `bootstrap_pending`；上线检查清单包含"删除该 env" | 低 |
| 10 | **Cookie 跨环境污染** | 同域多站点共用浏览器 | 无 | `__Host-` 前缀 + 无 `Domain` 属性 | 无 |

**成本实测口径（供 U2 定配额，U1 只负责"可归因"）**：`data/llm_cost_log.jsonl` 实测 37 次调用共 **2.725 元**，单次均值 **0.0736 元**，**失败率 27%**。外推 10 用户 × 每期 2 次 ≈ **4.42 元/周 / 约 230 元/年** → **成本不是主要矛盾**，因此 U1 的设计取向是"**宁可少一个用户维度，也不要为省钱牺牲可归因性**"。但 27% 的失败率带来一条**身份侧硬要求**：

> **调用成本日志必须记录 `user_id`，且"失败但已计费"的调用同样要落账**。否则配额核算会系统性偏低（用户实际消耗 > 账面消耗），而这正是"预算被刷"（§10.7 #7）的温床。具体地：`llm_client` 在重试路径上必须**只记一次账、但把 `attempts` 与 `failed_attempts` 都记下来**——**这条与 U2 的预算设计强耦合，请 U2 在文档里显式回应**。

### 10.8 两处必须明确的次要决策

1. **用户名大小写**：建议**存储保留原样，比较时大小写不敏感**（`WHERE username = ? COLLATE NOCASE`），否则"Alice"与"alice"是两个账号，管理员会困惑。但 `UNIQUE` 索引要一致（`CREATE UNIQUE INDEX ... ON users(username COLLATE NOCASE) WHERE deleted_at IS NULL`）。**这一点必须写进 U3 的约定**，否则前端/后端各按一半实现。
2. **用户名允许字符**：`^[a-zA-Z0-9_.-]{3,32}$`，禁止空格/中文/emoji（登录框与日志/审计的可读性比"支持昵称"重要；昵称走 `display_name`）。

### 10.9 口令策略（被 §5.2 / §8.3 引用，在此定义）

**规则（三档梯度，不过度加码）**：

| 场景 | 规则 |
|---|---|
| 用户自设口令（引导/自改密） | **长度 ≥12**；必须含字母 + 数字；**不强制**大小写/符号混用（强制符号只会逼出 `Passw0rd!` 这类可预测口令） |
| 管理员重置生成的口令 | `secrets.token_urlsafe(16)`（≈128 bit 熵），**不做复杂度校验**（它已远超要求） |
| 黑名单 | 拒绝 **常见弱口令 top-1000** 与**用户名本身/用户名+数字**（`bob`、`bob123`、`bob2026`）——这是性价比最高的两条 |

**明确不做**（理由）：
- ❌ **不做定期强制改密**（NIST SP 800-63B 已明确反对；对 10 人娱乐系统只会产生 `Xxx2026!` 变体）；
- ❌ **不做口令历史库**（"不能重复最近 5 次"）——需要额外表，收益极低；
- ❌ **不做"安全问题"**（同 §10.4）。

**实现落点**：`validate_password(pw, username) -> list[str]` 纯函数放 `auth.py`，被引导/创建用户/自改密**三处共用**（避免三条路径规则漂移，与 §5.3 的"同一实现"纪律一致）。

---

## 11. 与第 1 点模块化的关系：鉴权属于**独立横切层**，不属于"视图层内"

### 11.1 回答"鉴权是视图层还是独立层"

**都不是。正确的分类是：鉴权是「横切关注点（cross-cutting concern）」，在分层上位于「传输层」与「视图层」之间，是视图层的**门禁**，不是视图的一部分。

沿用第 1 点（M1）切分的数据面 / 规律挖掘 / 规律验证 / 规律使用 / AI 网关 / 调度 / **视图** 七层，鉴权层的位置是：

```
        ┌─────────────────────────────────────────┐
  HTTP  │  nginx (TLS + 限流 + IP)                 │  ← 部署层（1.3/10.5 的修复点）
        ├─────────────────────────────────────────┤
        │  auth 中间件（认证 → 授权 → 审计 → 注入ctx）│  ← **新增独立层**（U1）
        ├─────────────────────────────────────────┤
        │  视图层（80 个端点，79 个同步 def）        │  ← 只负责"取 ctx 里的 user_id"
        ├─────────────────────────────────────────┤
        │  数据面 / 规律 / AI 网关 / 调度            │
        └─────────────────────────────────────────┘
```

**三条设计约束（用来判断有没有做错）**：

| 约束 | 反例（做错了） | 正例 |
|---|---|---|
| **鉴权层不依赖任何业务模块** | 中间件 `from . import engine` 去查"这个用户能不能生成预测" | 中间件只依赖 `auth.py` + `db.py` 的三张身份表 |
| **业务模块不感知鉴权** | `engine.predict_next(..., user=...)` 里判角色 | `engine` 完全不 import `auth`；它只接收 `user_id` 这个**纯数据**参数 |
| **端点不写鉴权逻辑** | 每个端点自己 `if user.role != 'admin': return 403` | 端点从 `_current_user.get()` 取 `user_id`，角色判断在中间件 |

> **一句话**：**鉴权向下不依赖业务，业务向上不感知鉴权**。只要这条成立，第 1 点把 `api_app.py` 拆成多个文件时，鉴权可以原样搬走（甚至可以搬成独立 ASGI middleware 包），不需要重写。

### 11.2 与第 1 点的三个**真实耦合点**（必须联合排期，不能各做各的）

| # | 耦合 | U1 的依赖 | 若不做会怎样 |
|---|---|---|---|
| 1 | **`db.py` 单连接 + 无 WAL**（`db.py:160-174`） | 鉴权把"每个请求"变成"要查库的请求"，新增高频 `SELECT` 会加剧锁竞争 | 多个用户同时点"生成预测"时会撞 `database is locked`；鉴权成了**性能退化源**。缓解：会话 30s 缓存（§4.2）+ WAL/busy_timeout |
| 2 | **79 个同步 def 跑 40 线程池** | 登录取哈希（scrypt 60ms×32MB）在同步端点里会占线程 + 吃内存 | 见 §3.4：必须 `BoundedSemaphore(4)` + 限流。**若第 1 点改成"重活放独立进程/服务"，鉴权的哈希也应挪过去** |
| 3 | **`api_app.py` 是 3381 行单文件** | 新增 ~13 个端点和 1 个中间件会继续往里塞 | 建议**新增端点放 `lottery/auth_api.py`**（与 `api_app.py` 用 `APIRouter` 组合），中间件放 `lottery/auth.py`。这样即使第 1 点还没开工，U1 也不会让 `api_app.py` 继续膨胀。**这是 U1 对第 1 点的主动让步** |

### 11.3 不该做的事（防止过度设计）

- ❌ **不要引入 RBAC 框架**（Casbin / OPA / 权限 DSL）——两个角色、10 个用户，一个 `if role == 'admin'` 就够；
- ❌ **不要做多租户数据隔离的"租户"概念**——本需求是"多用户"，不是"多租户 SaaS"；没有组织/团队/子账号；
- ❌ **不要引入 OAuth2 / OIDC**（除非走 §2.1 的 E 方案远期演进）；
- ❌ **不要把鉴权拆成独立微服务**——单容器、10 用户，独立服务只会多一个故障点（这是第 1 点 M1 已论证过的同一结论）；
- ❌ **不要做"记住我 30 天 + 刷新令牌轮转"**——10 个用户在娱乐软件上，7 天绝对过期足够。

---

## 12. 落地顺序与验收

### 12.1 分批（每批可独立回滚、可独立验收）

| 批次 | 内容 | 前置 | 改动点 | 回滚 |
|---|---|---|---|---|
| **B-1 独立第一刀**（2.0 人日，**不依赖多用户任何东西**） | **U3-0：修 `register_prereg` 零调用**（V13 已确认的断链，前置注册簿形同虚设） | 无 | `hypothesis.py` + 调用点 | 纯代码回滚 |
| **B0 部署前提**（0.5 人日） | 端口改 `127.0.0.1`；关 docs；删/收紧 CORS；`db.py` 加 `busy_timeout`+WAL | 无 | `docker-compose.yml:11`、`api_app.py:28-32`、`db.py:167` | 改回配置 + 重建容器 |
| **B1 身份骨架**（1.5 人日） | v5 迁移（3 表）；`auth.py`（scrypt + 会话 + 引导）；中间件替换；登录/登出/me/bootstrap | B0 | 3 新文件 + `api_app.py:34-45` + `migrations.py` | **L1 代码回滚零风险**（新表对旧代码无影响） |
| **B2 权限落地**（1 人日） | 24 条 admin 端点生效；`tasks` IDOR 修复；`/api/config/llm` 改每用户（联合 U2）；前端 `api()` + 登录页 | B1 | §7.6 的 7 条路由 | 同上；admin 端点若误伤，临时把这些路径降为 `U`（一行配置） |
| **B3 管理与运维**（1 人日） | 用户 CRUD 6 端点；自保护规则；审计查询；限流；`/api/health` 的 auth 自检；管理页 | B2 | §8.2 的 13 端点 | 同上 |

**为什么 B-1 排在最前**：它与多用户**完全解耦**（不碰 users、不碰鉴权），却修的是 V13 已确认的真实断链——`register_prereg` 零调用意味着"前置注册簿"从建起就没被用过。**先做它，可以用最小代价验证"改一处、跑测试、上线"的节奏，也不占用多用户的任何前置。** 它是本序列里**唯一可以立刻开工**的批次。

**关键排序理由（Lead 裁决 5，**P0 不可分割**）**：**B0 必须在 B1 之前，且两者视为同一批**。因为如果先上鉴权但端口还开着，攻击者可直连 `18000` 并伪造 `X-Real-IP` → **登录限流形同不存在**（§10.6）。这不是"优化项"，是"功能是否成立"的问题。**B0 未完成前，B1 不得进入灰度。**

### 12.2 与 U2 / U3 的接口交接（三份文档的接缝，**已按 Lead 裁决 1~6 定稿**）

| 交付物 | U1 给出去 | 从 U2/U3 拿回来 |
|---|---|---|
| `users.user_id` 类型/命名 | ✅ 本文件 §4.0 | — |
| `config.SYSTEM_USER_ID = 0` | ✅ 常量落点 §7.5（裁决 3） | U3 的 `predictions`/`tasks` 回填与查询统一用它 |
| `tasks.user_id` | ✅ **`NOT NULL DEFAULT 0`**（裁决 3，否决 NULL） | U3 的迁移与 `db.list_tasks` 改签名 |
| `is_primary_admin = 1`（主 admin 锚点） | ✅ DDL §4.1 + 唯一索引 + 引导置位 §5.2（裁决 2） | U3 的回填 SQL 只认这个标记，**不认 `user_id = 1`** |
| 删除用户的清理边界 | ✅ **显式事务**为正确性来源，级联只作冗余（裁决 1，§8.3） | U2 的 `user_llm_config` 必须被该事务覆盖；U3 的 `predictions` 保留不删 |
| `user_llm_config.user_id` | ✅ `INTEGER NOT NULL`；可写 `REFERENCES` 但**不得当正确性依据** | U2 的表 DDL + Fernet 加密列 |
| 会话上下文如何进 `llm_client` | ✅ **裁决 9**：`api_app→db` 走显式参数；contextvar **仅**给 `llm_client`（§7.2.1） | U2 据此在 `llm_client` 内读 contextvar，但**跨线程必须显式传** |
| 身份解析的可替换点 | ✅ **裁决 8**：`api_app._resolve_session_user_id` 为唯一入口（§7.3） | 前端/测试用 `login_as(client, user_id)` 或 monkeypatch 单点注入 |
| `users.status` 四值 | ✅ **裁决 7**：`bootstrap/active/disabled/locked`（§4.1） | U3 迁移 5 的 bootstrap INSERT 直接可用；**设密成功后才置 `is_primary_admin=1`** |
| 密码 vs 密钥的密码学分工 | ✅ §3.7（裁决 6）：口令 scrypt / key Fernet | U2 提供 Fernet 与主密钥 env 方案 |
| "最后一个 admin 不可删" | ✅ 规则 §8.4 | U3 确认软删除语义 |
| `predictions` 主键重建 vs `forward-only` | ✅ **已标出冲突** §9.1 | U3 做决定（**唯一未决项**） |

### 12.3 验收标准（可运行、可度量）

| # | 验收项 | 判定方式 |
|---|---|---|
| 1 | **路由分级零遗漏** | 参数化测试遍历 `app.routes`（80 条），断言每条都在 `required_role()` 里有明确返回值；**新增未定级路由会让测试挂** |
| 2 | **匿名访问被拒** | `curl` 不带 Cookie 请求 24 条 admin 端点 → 全部 **401**；带普通用户 Cookie → 全部 **403** |
| 3 | **同步端点能拿到 user** | 集成测试：登录后请求一个**同步 def** 端点（如 `/api/predictions/last`），断言其内部 `_current_user.get()` 非空 |
| 4 | **上下文不串号** | 并发测试：A、B 交替请求 200 次，断言任何一次响应都不含对方数据（重点测 `tasks`） |
| 5 | **scrypt 参数与内存** | 单元测试断言 PHC 串前缀 `scrypt$n=32768`；压测 20 并发登录，`docker stats` 内存不超 `mem_limit` 且不 OOMKilled |
| 6 | **限流生效** | 同一 IP 连打 30 次 `/api/auth/login` → 第 11 次起 **429**（§10.1 ②） |
| 7 | **引导一次性** | 引导成功后再次 POST → **410**，且无论 token 对错 |
| 8 | **自保护** | 最后一 admin 删自己 → **409**；admin 数仍为 1 |
| 9 | **迁移可回滚** | 演练 §9.4 的 L1/L2，L1 后旧镜像能正常读写（不因多出 3 张表报错） |
| 10 | **明文旁路已关闭** | 公网 `curl http://89.106.64.40:18000/api/health` **超时/拒绝**；`ss -tlnp` 显示 `127.0.0.1:18000` |
| 11 | **docs 关闭** | `/docs`、`/redoc`、`/openapi.json` 全部 **404** |
| 12 | **审计完整** | 建用户/改角色/重置口令/踢人 各执行一次，`GET /api/admin/audit` 能查到 4 条，且 `detail_json` 里无任何口令/key 明文 |
| 13 | **删除无孤儿行（不依赖 PRAGMA）** | 删用户 A 后：`user_sessions`/`user_llm_config` 中 `user_id=A` 行数 **== 0**；`predictions` 中 A 的历史行**仍存在**。**测试内不得执行 `PRAGMA foreign_keys=ON`**（Lead 裁决 1，见 §8.3） |
| 14 | **无 `DEFAULT 1` 残留** | `user_id` 相关列**零 `DEFAULT 1`**；迁移与回填 SQL 中不出现字面量 `user_id = 1`（Lead 裁决 2/4）。**注**：`token_version INTEGER NOT NULL DEFAULT 1` 属版本计数起点、与主体 id 无关，**不在此列** |
| 15 | **`status` CHECK 含四值（裁决 9）** | 断言三条：① `INSERT INTO users(username, status, ...) VALUES('t1','bootstrap',...)` **必须成功**（证明 CHECK 列表里真有 `bootstrap`，否则 U3 迁移 5 会整批回滚）；② `status='bogus'` **必须抛 `sqlite3.IntegrityError`**（证明 CHECK 真的在生效，不是被忽略）；③ 该 bootstrap 行即使 `role='admin'`、`is_primary_admin=0`，用它发起任何 `/api/*` 请求一律 **401/403**（证明 §7.3 的 status 闸门先于 role 判断） |
| 16 | **身份解析可单点替换（裁决 8）** | conftest 一行 `monkeypatch.setattr(api_app, "_resolve_session_user_id", lambda r: ADMIN_ID)`，**全部 36 处既有 `TestClient` 用例零改动**即通过；反向断言：`_resolve_session_user_id` 无会话时返回 `None`，**绝不回退 `SYSTEM_USER_ID`/`admin`** |
| 17 | **租户传递是显式参数（裁决 7.2.1）** | 静态断言：`grep -rn "user_id.*get()" lottery/api_app.py lottery/db.py` 中**不得出现**"从 contextvar 取 user_id 后写库"的写法；`db.py` 的 7 个 user-scoped 函数签名均含 `user_id`；并发测试：A/B 交替调用后台任务端点，断言 `tasks.user_id` 恒为调用者而非 0 |
| 18 | **不回归** | 现有 920 个 `def test_`（M1 实测口径）全绿；新增测试 ≥28 条 |

### 12.4 风险与未决项（诚实登记）

| # | 未决 / 风险 | 状态 | 建议 |
|---|---|---|---|
| 1 | **`predictions` 主键重建与 `forward-only` 冲突** | **未决**（U3 决定） | U3 必须先出方案，否则 B1 之后数据仍无 user 维度 |
| 2 | `db.py` 是否同步上 WAL | **未决**（第 1 点范围） | 建议与 B1 绑定，否则鉴权会放大锁竞争（§11.2） |
| 3 | 前端 4203 行 `app.js` 的 `innerHTML` XSS 面 | **未评估** | 不在 U1 范围，但登记为多用户后的**残余高危**（§10.7 #2） |
| 4 | 15 个邻居容器共享 `*.ff88.de` | 已知 | 靠 `__Host-` Cookie + CSRF Token 兜；不建议为此上独立域名 |
| 5 | 会话缓存的一致性与撤销时效 | 设计已定 | TTL 30s + 撤销主动清缓存；最坏情况：被踢用户还能用 30s |
| 6 | `LOTT_TOKEN` 兼容期 | 设计已定 | 1 个发布周期后移除（§2.5） |
| 7 | **contextvar 跨线程丢失导致静默写错人** | **已用设计消除**（裁决 9） | `api_app→db` 一律显式传 `user_id`；只有 `llm_client` 用 contextvar，且跨线程强制显式传（§7.2.1）。**残余风险**：新写的后台任务若图省事又去 `cv.get()`，会重犯——建议在评审清单里加一条"写库前不得从 contextvar 取 user_id" |
| 8 | **`status='bootstrap'` 行长期滞留** | 设计已定 | 若引导一直没完成，库里会留一条零权限 admin 行。`/api/health` 的 `bootstrap_pending` 已能暴露；**再加一条**：启动日志若发现 `status='bootstrap'` 且已存在超过 24h，打印醒目告警（不自动删，避免误清） |
| 9 | **中间件 status 闸门被后来者误删** | 设计已定 | 验收项 15 ③ 用"bootstrap 行发起请求必须 401/403"把它钉死；任何让该测试变绿又删闸门的改动都会被测试抓住 |

### 12.5 最终结论

**具备改造条件，推荐方案 = 服务端 session（DB 表）+ 标准库 scrypt + deny-by-default 中间件 + contextvars 注入。**

- **密码哈希新增第三方依赖 0 个**；全项目仅因 U2 的 API Key 可逆加密新增 `cryptography` 1 个（`requirements.txt` +1 行，`Dockerfile` 无需改，见 §3.6/§3.7）；
- **80 个 HTTP 端点函数签名改动 0 处**（除 7 条需显式取 `user_id` 的路由）；
- **但 `db.py` 的 7 个 user-scoped 函数需加 `user_id` 显式参数**，`tests/` 13 处 lambda 随之适配（裁决 9，§7.2.1）；
- **真正的成本在 3 张新表 + 13 条新端点 + 一次迁移 + 一轮测试**，约 **3.5~4 人日**（不含 U2/U3 与前端管理页）；
- **最大的风险不在代码，在部署层**：`0.0.0.0:18000` 明文旁路必须先关，否则整套鉴权在限流维度上是漏的（§10.6）。

### 12.6 跨文档冲突 → **Lead 已裁决 1~9，全部按裁决执行**（已定稿，无待决项）

写完后交叉核对了 `U2_LLM_ISOLATION.md` 与 `U3_DATA_MODEL.md`，提出 3 处会**静默失效/静默越权**的不一致。**Lead 已独立复核并全部确认，以下为终局裁决；本节即本文件的最终口径，先前若有与本表冲突的表述，一律以本表为准。**

| # | 议题 | 终局裁决 | 本文件已改到哪 |
|---|---|---|---|
| **1** | `PRAGMA foreign_keys` 与级联删除 | **不依赖级联**。删除用户走**显式事务**（应用层删关联行）；`ON DELETE CASCADE` 只作**冗余防御**，**不得作为正确性依据**。理由：`foreign_keys` 是**每连接**设置，本库当前=0，将来换连接池/多进程会静默失效——安全逻辑不能建立在"某个 PRAGMA 恰好开着"之上。**必须有测试：删用户后 `user_llm_config`/`user_sessions` 无孤儿行，且测试不依赖 PRAGMA** | §4.0（外键约定）、§8.3（DELETE 事务 + 测试）、§12.3 验收 13 |
| **2** | admin 的 `user_id` | **禁止假设 `admin = 1`**（实测删光重插得 2）。迁移/回填**用 `username` 或 `is_primary_admin` 定位**；`users` 表加 `is_primary_admin INTEGER NOT NULL DEFAULT 0`（全表最多一行=1）。**所有 DDL 的 `DEFAULT` 一律不许写死 `1`** | §4.0（主 admin 识别）、§4.1（DDL + 唯一索引）、§9.3（回填 SQL） |
| **3** | 系统任务归属 | **采纳 `SYSTEM_USER_ID = 0`，否决 `NULL`**。`tasks.user_id` 为 **`NOT NULL DEFAULT 0`**；`SYSTEM_USER_ID = 0` 写进 **`config.py`** 作为全局常量 | §4.0、§7.5、§9.3 |
| **4** | U3 的 `DEFAULT 1` | **全部作废**。改为 **`DEFAULT 0`（SYSTEM_USER_ID）**：老代码不写 `user_id` → 落"系统主体"，语义等于"迁移前的历史数据"，比落"admin 名下"更诚实。历史 40 行由迁移脚本**显式 UPDATE** 给主 admin | §9.3（含 SQL 与三点不可协商） |
| **5** | 端口绑定 vs 限流 | **同一批上线，不可分割**。`127.0.0.1:18000:18000` 是**所有鉴权工作的 P0 前置**（nginx 已透传 `X-Real-IP`/`X-Forwarded-For`/`X-Forwarded-Proto`，但容器直绑 `0.0.0.0` 可绕过） | §0（P0 提示框）、§12.1（B0 批次的排序理由） |
| **6** | 依赖：scrypt vs cryptography | **两者不冲突，按用途分开**：口令 → `hashlib.scrypt`（单向，标准库）；API Key → `cryptography` Fernet（**必须可逆**才能调 LLM）。U1 的"零新依赖"**只适用于密码**，不推广到密钥。全项目只新增 `cryptography` 1 个 | §0、§3.6（适用范围声明）、**§3.7（新增，裁决 6 专节）** |
| **7** | `users.status` 合法值 | **扩为四值 `('bootstrap','active','disabled','locked')`**。`bootstrap` 必须进 CHECK（否则 U3 迁移 5 的 INSERT 被拒 → 整批回滚 → 上线失败）。语义：bootstrap 零权限/仅一次 env token；active 正常；disabled 软删且 session 立即失效；locked 安全锁定、admin 可解锁。**bootstrap 期间 `is_primary_admin` 不置位，设密成功后才置位** | §4.1（CHECK + 四值注释 + 状态机 + 三条不变量）、§5.2（规则 5/6 + 两入口状态机）、§7.3（status 闸门先于 role）、§12.3 验收 15 |
| **8** | 身份解析必须可单点替换 | **提成 `api_app` 模块级 `_resolve_session_user_id(request) -> int \| None`**，中间件只调用。理由：`app.dependency_overrides` **对 HTTP 中间件无效**，内联会让 36 处 `TestClient` 用例无法用一个 fixture 注入身份（爆炸半径 1 → 36）。无会话**绝不回退 admin/SYSTEM** | §7.3（代码 + 纪律 1）、§12.3 验收 16 |
| **9** | 租户传递方式 | **显式参数 > 隐式 contextvar**。`api_app → db` 用 `load_predictions(issue, user_id=...)`；contextvar **只**给 `llm_client` 这类改不了签名的深层库。理由：裸 `threading.Thread`（`:2387`/`:2848`）不继承 contextvars，丢失后**静默落到 SYSTEM/admin 写错数据**且测试不红。代价：`db.py` 7 个函数加参数 + `tests/` 13 处 `lambda i:` → `lambda i, **_kw:` | **§7.2.1（新增专节）**、§0、§7.6、§12.2、§12.3 验收 17 |

**五条给实施方的必过项**（建议直接抄进 U3 迁移脚本与代码评审清单）：
1. 迁移里**没有任何** `DEFAULT 1`；`user_id` 相关默认值**只能是 0**；
2. 定位 admin 的 SQL **不出现字面量 `1`**，只用 `is_primary_admin = 1` 或 `username = ? AND role = 'admin'`；
3. 删除用户的路径**有显式事务 + 一条不依赖 PRAGMA 的孤儿行测试**；
4. `users.status` 的 CHECK **含四个值**，且中间件的 status 闸门**先于** role 判断（有可执行断言，验收 15）；
5. **写库路径不得从 contextvar 取 `user_id`**——`api_app→db` 一律显式传参（验收 17）。

> **一处技术澄清（不改变裁决，只是把"改几行"说准）**：裁决 6 的结论我完全执行；但"`requirements.txt` 与 `Dockerfile` 各改一行"里，**`Dockerfile` 实际不需要改**——它只做 `COPY requirements.txt` + `RUN pip install -r requirements.txt`，新增包会自动被装上。当前仓库也未用 `--require-hashes` 锁，所以没有锁文件要同步。**改动只在 `requirements.txt` 一行**。若你们希望"镜像显式声明依赖"以便审计，才需要动 `Dockerfile`（加注释或显式版本），那属于可选动作。

---

## 附录 A：80 端点完整权限分级表（程序化生成，可作测试基线）

> 生成方式：枚举 `app.routes` 中 80 个 `APIRoute`，按 §6.1 规则分类。**此表可直接转成 §12.3 验收项 1 的参数化测试数据**。
> 计数：`P=3` / `U=53` / `A=24` / 合计 `80`。

| 级别 | 方法 | 路径 | 备注 |
|---|---|---|---|
| `P` | GET | `/` | 未登录时渲染登录页（同一 HTML） |
| `U` | GET | `/api/audit/anomaly/latest` |  |
| `A` | POST | `/api/audit/anomaly/run` |  |
| `U` | GET | `/api/backtest/regime` |  |
| `U` | GET | `/api/battle/report` |  |
| `U` | GET | `/api/config/llm` | U2：改造后仅本人；全局默认改 A |
| `U` | POST | `/api/config/llm` | U2：改造后仅本人；全局默认改 A |
| `A` | GET | `/api/data/reconcile` | GET 带副作用，建议改 POST（§6.5） |
| `U` | GET | `/api/data/reconcile/history` |  |
| `U` | GET | `/api/diagnose` |  |
| `U` | GET | `/api/discovery/families` |  |
| `U` | GET | `/api/discovery/scan` |  |
| `U` | GET | `/api/draws/history` |  |
| `U` | GET | `/api/draws/latest` |  |
| `U` | GET | `/api/eval` |  |
| `A` | POST | `/api/eval/backtest` |  |
| `U` | GET | `/api/eval/by-method` |  |
| `A` | POST | `/api/eval/coverage` |  |
| `U` | GET | `/api/eval/cumulative` |  |
| `U` | GET | `/api/eval/export.csv` |  |
| `U` | GET | `/api/eval/llm/latest` |  |
| `A` | POST | `/api/eval/llm/run` |  |
| `U` | GET | `/api/eval/meta` |  |
| `A` | POST | `/api/eval/online` |  |
| `U` | GET | `/api/eval/recommendations` |  |
| `A` | POST | `/api/evolution/run` |  |
| `U` | GET | `/api/evolution/status` |  |
| `A` | POST | `/api/experiments/randomness-selfcheck` |  |
| `U` | GET | `/api/features` |  |
| `P` | GET | `/api/health` | 保留公开（容器 healthcheck），删除 api_auth_enabled |
| `P` | GET | `/api/info` | 去敏：删 milestones 长文本与 docs/ 路径 |
| `U` | GET | `/api/l2/distribution` |  |
| `U` | GET | `/api/llm/arm` |  |
| `U` | GET | `/api/llm/audit/latest` |  |
| `A` | POST | `/api/llm/audit/run` |  |
| `U` | GET | `/api/llm/cost` |  |
| `A` | POST | `/api/llm/decision/restore` |  |
| `U` | GET | `/api/llm/decision/status` |  |
| `U` | POST | `/api/llm/test` |  |
| `U` | GET | `/api/llm_lab/latest` |  |
| `A` | POST | `/api/llm_lab/register` |  |
| `A` | POST | `/api/llm_lab/run` |  |
| `A` | POST | `/api/llm_lab/run/async` |  |
| `A` | POST | `/api/methods/config` |  |
| `U` | GET | `/api/methods/status` |  |
| `U` | GET | `/api/mining/latest` |  |
| `U` | GET | `/api/mining/reports` |  |
| `A` | POST | `/api/mining/run` |  |
| `U` | GET | `/api/ml/diagnosis` |  |
| `A` | POST | `/api/ml/eval` |  |
| `U` | GET | `/api/ml/status` |  |
| `U` | GET | `/api/multiscale/mde-table` |  |
| `U` | GET | `/api/multiscale/scan` |  |
| `A` | GET | `/api/notify/status` | 泄露 smtp_host / email_to（notify.py:82-87） |
| `U` | GET | `/api/patterns` |  |
| `A` | POST | `/api/patterns/backtest` |  |
| `U` | GET | `/api/portfolio/distribution` |  |
| `U` | GET | `/api/portfolio/expected-value` |  |
| `U` | GET | `/api/portfolio/marginal` |  |
| `U` | GET,POST | `/api/predict` | GET 会消耗 LLM 配额，建议拆分（§6.5） |
| `U` | GET | `/api/predictions/history` |  |
| `U` | GET | `/api/predictions/last` |  |
| `A` | POST | `/api/refresh` |  |
| `U` | GET | `/api/regime/overview` |  |
| `A` | POST | `/api/regime/refresh` |  |
| `U` | GET | `/api/registry/status` |  |
| `U` | POST | `/api/replay/diagnose` |  |
| `U` | GET | `/api/rigor/randomness` |  |
| `U` | GET | `/api/rigor/signal` |  |
| `A` | POST | `/api/rules/closed-loop` |  |
| `A` | POST | `/api/rules/closed-loop/async` |  |
| `U` | GET | `/api/rules/lifecycle` |  |
| `A` | POST | `/api/rules/lifecycle/run` |  |
| `A` | POST | `/api/rules/refresh` |  |
| `U` | GET | `/api/rules/runtime` |  |
| `U` | GET | `/api/samples/side-by-side` |  |
| `U` | GET | `/api/shape/constraints` |  |
| `U` | GET | `/api/shape/rules` |  |
| `U` | GET | `/api/tasks` | 必须加 WHERE user_id=?（§10.3） |
| `U` | GET | `/api/tasks/{task_id}` | 必须校验属主，越界返回 404（§10.3） |

---

## 附录 B：本文引用的关键文件:行号索引

| 主题 | 位置 |
|---|---|
| 可选全局 token 中间件（fail-open） | `lottery/api_app.py:34-45` |
| `config.API_TOKEN` 定义 | `lottery/config.py:20` |
| `/api/health` 泄露 `api_auth_enabled` | `lottery/api_app.py:86` |
| FastAPI 默认开启 docs | `lottery/api_app.py:28` |
| CORS `allow_origins=["*"]` | `lottery/api_app.py:30-32` |
| 静态目录挂载 | `lottery/api_app.py:50` |
| `GET /`（index） | `lottery/api_app.py:2138` |
| 全局 LLM 配置读写（多用户污染源） | `lottery/api_app.py:751-826` |
| 任务列表/详情（IDOR） | `lottery/api_app.py:621-631`；`lottery/db.py:728-743` |
| `GET /api/predict` 会消耗 LLM | `lottery/api_app.py` 的 `@app.api_route("/api/predict", methods=["GET","POST"])` |
| `GET /api/data/reconcile` 带副作用 | `lottery/api_app.py:55-58` |
| 后台线程（contextvar 丢失点） | `lottery/api_app.py:604`、`:2848` |
| 调度线程（需要系统主体） | `lottery/api_app.py:2387-2393`、`:2214-2222` |
| uvicorn 启动（未设 forwarded_allow_ips） | `lottery/cli.py:93` |
| 全局单连接 SQLite | `lottery/db.py:160-174` |
| `SCHEMA` 常量 | `lottery/db.py:12-157` |
| `predictions` 主键定义 | `lottery/db.py:63` |
| `tasks` 表 DDL | `lottery/db.py:87-96` |
| 迁移批次事务 + 备份 + fail-closed | `lottery/migrations.py:296-321`、`:263-294`、`:213-252` |
| `forward-only` 契约 | `lottery/migrations.py:100-101`、`:67-68` |
| 备份路径 `<db>.pre-migrate-v<n>` | `lottery/migrations.py:177-182` |
| 已有 `llm_audit` 表（审计范式参考） | `lottery/migrations.py:89-114` |
| SMTP 发送实现（密码重置可复用） | `lottery/notify.py:44-56` |
| `notify.status()` 泄露 smtp_host/email_to | `lottery/notify.py:82-87` |
| 全局单收件人 | `lottery/config.py:475` |
| 全局 LLM 配置文件常量 | `lottery/config.py:480` |
| 前端唯一网络出口 | `web/static/app.js:55-56` |
| 前端任务轮询 | `web/static/app.js:99`、`:542`、`:2414` |
| 端口绑定（明文旁路） | `docker-compose.yml:11` |
| 内存限额 768m | `docker-compose.yml:8-9` |
| 依赖清单（6 包） | `requirements.txt` |
| Dockerfile 安装依赖方式 | `Dockerfile`（`RUN pip install --no-cache-dir -r requirements.txt`） |
| nginx 反代与转发头 | `/etc/nginx/sites-available/ssq`（`location /`） |

---

*本文为只读评审产出，未修改任何业务代码、配置或部署文件。所有"实测"结论均可在当前环境复现（命令见正文）。*
