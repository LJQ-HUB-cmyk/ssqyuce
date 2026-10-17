# VPS 部署方案（Docker Compose · 长期运行 · 端口 18000）

> **当前部署形态：单节点。** 只有本机（bero_de）在跑，公网入口 `https://ssq.ff88.de`。
> 历史上曾用过 ivy-ray-EU、R71s.AI、BF.US、ZT.JP.Plus 四个节点，
> **现均已退役或不再部署本应用**（见 `docs/PROJECT_CONTEXT.md` 第 2 节）。
> 下文的 `<VPS>` 是**可选的异地重建**流程，当前没有第二个节点需要维护。

本方案把一个"轻量 + 自动调度"的实例长期跑在 VPS 上：

- **资源需求极低**：1 vCPU / 512MB–1GB 内存 / 1GB 磁盘即可（LLM 推理走远端 API，本地无 GPU 负担）；
- **长期运行**：`restart: unless-stopped` 开机自启 + 崩溃自动拉起；日志自动轮转；
- **自动闭环**：开奖日（周二/四/日）21:35（Asia/Shanghai）自动「抓取 → 在线对照 → 生成下期预测」；无下期预测时启动即自动生成；
- **数据持久化**：`./data` 目录挂载卷，数据库 + 原始快照都在里面；备份见第七节
  （`ssq_backup.sh` 对 `ssq.db` 做 SQLite 在线一致性热备，**不要**直接 `cp` 正在写的库）。

## 一、VPS 准备

```bash
# Ubuntu / Debian：安装 Docker + Compose 插件
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER   # 重新登录后生效（或直接用 sudo）
docker --version && docker compose version
```

若 VPS 无法访问 `get.docker.com`，用发行版源：
`sudo apt update && sudo apt install -y docker.io docker-compose-v2`

## 二、代码上服务器

```bash
# 方式 A：git（推荐，便于日后 docker compose 一键升级）
cd /opt && git clone <你的仓库地址> ssq && cd ssq

# 方式 B：从本机 rsync 同步
rsync -av --exclude=.venv --exclude=.git --exclude=data /root/DSH/应用/ssqyuce/ root@<VPS>:/opt/ssqyuce/
```

## 三、迁移本地数据（推荐，可"热启动"）

把本机已积累的数据（3490 期开奖 + 预测记录）带过去，避免首启重新抓取：

```bash
rsync -av /root/DSH/应用/ssqyuce/data/ root@<VPS>:/opt/ssqyuce/data/
```

> 跳过此步也可以：容器首次启动会自动抓取全量数据并生成首期预测（约 2–3 分钟）。
> 注意：`data/raw` 快照、`data/ssq.db` 都随目录一起同步。

## 四、构建并启动

先配置 LLM 凭据（**仓库不含任何 URL/Key**，全部走 `.env`，该文件已 gitignore）：

```bash
cd /opt/ssqyuce
cp .env.example .env
# 编辑 .env，填入你自己的 LLM 通道（DeepSeek/智谱/通义等任意 OpenAI 兼容服务均可）
#   LOTT_LLM_BASE_URL=...  LOTT_LLM_API_KEY=...  LOTT_LLM_MODEL=...
# 多模型可选：LOTT_LLM_MODEL_LIST=模型A,模型B  或  LOTT_LLM_EXTRA_MODELS=[{...}]
```

```bash
docker compose up -d --build          # 首次构建约 2–5 分钟（拉取 python:3.11-slim + pip 依赖）
docker compose logs -f --tail 50      # 观察启动日志（首次含 数据同步→回测→生成预测）
```

验证：

```bash
curl http://<VPS_IP>:18000/api/health
# {"status":"ok","issues":3490,"max_issue":"2026093"}
```

浏览器打开 `http://<VPS_IP>:18000` 即可使用。

> ⚠️ **改了前端一定要改 `?v=` 版本号**：入口节点对 `/static/` 有 7 天代理缓存
> （`proxy_cache ssqcache; proxy_cache_valid 200 7d`），浏览器还会按 `max-age=86400` 再缓存一天。
> `web/static/app.js` / `style.css` 的**内容变更不会自动生效**——必须同步修改
> `web/index.html` 里 `/static/style.css?v=<日期>.<序号>` 与 `/static/app.js?v=<日期>.<序号>`
> 的版本号（缓存键含 query，改版本号即换新键），否则用户拿到的仍是旧前端。
> 只有 `index.html` 本身不缓存，所以只改版本号、无需清入口缓存。
>
> 当前版本：`?v=20261018.1`（V8 阶段切分与体验修复，`APP_VERSION=2.4.0` / `APP_BUILD=2026-10-V8`）。
> V6 新增前端工作台「评估报告 → ⏳ 规律时效」（app.js + index.html + style.css 均有实质改动，
> 三处必须同时升 `?v=`）。

## 四·补、V4 诚实口径端点的部署验证

V4 新增三个端点，部署后应逐一确认（缺 `lottery/rigor.py` 时前两个会降级为 503，
**这是预期行为、不是故障**，不影响服务启动与其它功能）：

```bash
curl -s http://127.0.0.1:18000/api/rigor/randomness | head -c 200
# 期望：{"baseline":...,"verdict":"consistent-with-random","n_draws":3514,...}

curl -s "http://127.0.0.1:18000/api/rigor/signal?strategy=hot&window=100"
# 期望：survives_correction=false（热号 W=100 是预期中的假阳性，校正后不显著）

curl -s http://127.0.0.1:18000/api/portfolio/expected-value
# 期望：expected_roi 为负（彩票是负期望游戏）
```

公网入口另需确认缓存已换新键（应返回 `x-cache-status: MISS`）：

```bash
curl -sI "https://ssq.ff88.de/static/app.js?v=20261017.1" | grep -i x-cache-status
```


## 四·补二、V5 自进化端点的部署验证

V5 新增三个端点。**注意其中两个是重操作**，验证时务必带小窗口参数，
避免在部署脚本里触发 5 分钟以上的全量回放：

```bash
# 快：假说库与进化进度（毫秒级）
curl -s http://127.0.0.1:18000/api/evolution/status
# 期望：{"available":true,"total":N,"by_status":{...},"active_overrides":{...}}

# 慢：方法级战斗回放。limit=0 是**全量约 5 分钟**，验证请用 limit=400（约 10~20 秒）
curl -s "http://127.0.0.1:18000/api/battle/report?limit=400&start=300" | head -c 300
# 期望含：methods / comparison{pairs,n_significant_uncorrected,n_significant_corrected} / n_issues / gate_zh
```

`POST /api/evolution/run` 会**写** `data/hypotheses.json` 并调用 LLM，
耗时可能超过 10 分钟，**不要**放进自动验证脚本；需要时手动触发。

另：`data/hypotheses.json` 是运行时状态，已在 `.gitignore` 中，
升级时若要把本机假说库带到别的节点，需手动 `scp`（不要指望 git）。

## 五、防火墙 / 安全组

```bash
# 主机防火墙（若有 ufw）
sudo ufw allow 18000/tcp && sudo ufw enable
```

阿里云/腾讯云/AWS 等需在**安全组**里放行 TCP 18000。

## 六、本机挖掘与升级（当前为单节点部署）

**当前部署形态：只有本机（bero_de）一个节点**，容器 `ssq-predictor`，公网入口 `https://ssq.ff88.de`。
历史上还存在过 ivy-ray-EU、R71s.AI、BF.US、ZT.JP.Plus 四个节点，
**现在它们都已退役或不再部署本应用**（详见 `docs/PROJECT_CONTEXT.md` 第 2 节）。
因此本节不再有「发布到远端节点」的流程。

> ⚠️ **唯一数据副本就是本机 `data/`**。既然没有其他节点，本机数据丢失即全丢，
> **备份是唯一安全网**（见第七节「备份」）。

### 本机挖掘（可选）

挖掘在本机完成后只提交 `data/mining_artifact.json`；数据库、原始快照和密钥不进 Git。

```bash
cd /root/DSH/应用/ssqyuce
.venv/bin/python -m lottery.cli fetch
.venv/bin/python -m lottery.cli mine --engine rf --min-start 300 --top-k 8
.venv/bin/python -m lottery.cli import_mining data/mining_artifact.json  # 可选：验证可导入

git add lottery data/mining_artifact.json entrypoint.sh DEPLOY.md
git commit -m "chore: publish local mining artifact"
# 推送见「十一、推送到 GitHub」
```

`mine` 会更新本机 SQLite，同时生成 `data/mining_artifact.json`。该文件仅包含挖掘规律、统计显著性和来源期号，不包含开奖数据库或凭据。

### 本机升级

```bash
cd /root/DSH/应用/ssqyuce
# 先做一次全量备份（V10-00c 起是已安装的真实脚本，见第七节；不再用 cp 裸拷正在写的库）
/usr/local/bin/ssq-backup backup
# 附加保险：迁移前 lottery/migrations.py 会自动热备到 data/ssq.db.pre-migrate-v<当前版本>
docker compose up -d --build
docker compose logs --tail 80 lottery
# 预热约 2~2.5 分钟；轮询到 healthy 再验证
until [ "$(docker inspect --format='{{.State.Health.Status}}' ssq-predictor)" = healthy ]; do sleep 10; done
curl -fsS http://127.0.0.1:18000/api/info
# 核对库结构版本（V10-00d 起 /api/health 暴露 schema_version / schema_latest）
curl -fsS http://127.0.0.1:18000/api/health | python3 -m json.tool | grep -E 'schema_|issues'
```

### 运行时状态不在 git 里

以下文件是**运行时状态**、已 gitignore，换节点或重建容器时不会自动跟随，需要时手动 `scp`：

| 文件 | 内容 |
|---|---|
| `data/ssq.db` | 开奖库、预测、评估明细 |
| `data/hypotheses.json` | V5 假说库（每轮进化改写） |
| `data/adaptive_state.json` | 方法自适应降权状态 |
| `data/llm_budget.json` | 推理型模型学到的输出预算 |
| `data/llm_config.json`、`.env` | 密钥与运行时配置 |

容器启动时会自动导入 `/app/data/mining_artifact.json`；重复重建是幂等的。若本次不发布产物，启动会明确跳过挖掘，不会在 VPS 上自动运行 `mine`。

## 七、长期运行与运维

| 事项 | 操作 |
|---|---|
| 查看日志 | `docker compose logs -f --tail 100`（日志已轮转 10MB×3，不会撑爆磁盘） |
| 手动刷数据 | `docker compose exec lottery python -m lottery.cli fetch` |
| 手动生成预测 | `docker compose exec lottery python -m lottery.cli predict` |
| 重启服务 | `docker compose restart` |
| 升级版本 | 见第六节「本机升级」（**先跑 `/usr/local/bin/ssq-backup backup`**，再 `docker compose up -d --build`） |
| 服务器重启 | 无需操作，`restart: unless-stopped` 自动拉起 |
| 备份 | 见下方「备份（已安装并做过恢复演练）」 |

### 备份（已安装并做过恢复演练，V10-00c）

> 历史状态提醒：V10-00c 之前本节只是「待配置示例」，实测 `/etc/cron.d/ssq-backup`、
> `/backup/` **都不存在**，`data/*.bak*` 也没有 —— 生产库曾处于**零备份**状态
> （见 `docs/reviews/R4_RELEASE_REVIEW.md` P0-C）。以下命令均为**已实装**的真实命令。

本机是唯一节点，`data/` 是全项目唯一数据副本。备份由
`scripts/ssq_backup.sh`（符号链接 `/usr/local/bin/ssq-backup`）完成：

* `data/ssq.db`：用 Python `sqlite3.Connection.backup()` 做**在线一致性热备**
  （**不是 `cp`**：`cp` 一个正在写的库会得到撕裂副本，`.backup()` 走 SQLite 在线备份 API）；
* `data/` 下其余运行时状态：`hypotheses.json`、`adaptive_state.json`、`llm_budget.json`、
  `llm_config.json`（**含 API Key，归档与文件权限 600**）、`methods_config.json`、
  `llm_claims.json`、`patterns_runtime.json`、`raw/`（以及任何新增的 `*.json`）；
* **明确排除 `data/ml_models/`**（265MB，可由训练重建；`MANIFEST.txt` 里写明）。

已安装的 cron（**每日 03:17**，保留最近 **14** 份）：

```bash
# /etc/cron.d/ssq-backup（已实装，权限 644，owner root）
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
MAILTO=""
17 3 * * * root /usr/local/bin/ssq-backup backup >>/var/log/ssq-backup.cron.log 2>&1
```

日常操作：

```bash
/usr/local/bin/ssq-backup backup      # 手工备份一次（cron 同源命令）
/usr/local/bin/ssq-backup list        # 列出已有备份
/usr/local/bin/ssq-backup verify      # 校验最新归档（库完整性 + 期数 + 全部 json 可解析）
ls -l /backup/ssq-backup-*.tar.gz     # 归档位置（权限 600）
tail -20 /var/log/ssq-backup.log      # 脚本日志（只记路径/大小/期数，绝不写密钥）
```

**恢复步骤（演练与实战同一套命令）**：

```bash
# 1) 先恢复到临时目录校验，绝不直接覆盖生产 data/
/usr/local/bin/ssq-backup restore /backup/ssq-backup-<时间戳>.tar.gz /tmp/restore-test

# 2) 用项目 venv 直接读库，确认期数（本机实测基线 = 3515 期）
cd /root/DSH/应用/ssqyuce
LOTT_DB=/tmp/restore-test/ssq.db .venv/bin/python -c \
  "from lottery import db; print('期数 =', db.count_draws()); print('最新期号 =', db.max_issue())"

# 3) 确认 runtime json 都能 json.load（llm_config.json 含密钥，恢复后保持 600）
.venv/bin/python - <<'PY'
import json, pathlib
d = pathlib.Path("/tmp/restore-test")
for f in ("hypotheses.json", "adaptive_state.json", "llm_budget.json",
          "llm_config.json", "llm_claims.json", "patterns_runtime.json"):
    p = d / f
    print(f, "OK" if not p.exists() else json.loads(p.read_text(encoding="utf-8")) is not None)
PY

# 4) 确认无误后再切换（会短暂停服）
docker compose down
cp -a /tmp/restore-test/. /root/DSH/应用/ssqyuce/data/   # 只覆盖，不动 ml_models/
chmod 600 /root/DSH/应用/ssqyuce/data/llm_config.json
docker compose up -d
curl -fsS http://127.0.0.1:18000/api/health   # 核对 issues=3515 与 schema_version
```

**迁移前自动备份（V10-00d）**：`lottery/migrations.py::apply` 在真正有待执行迁移时，
会先自动热备到 `data/ssq.db.pre-migrate-v<当前版本>`（例如 `data/ssq.db.pre-migrate-v2`）；
任一迁移失败会**整体回滚**（含 DDL），`schema_version` 不前进。回滚到旧 schema 时：

```bash
docker compose down
cp -a data/ssq.db.pre-migrate-v2 data/ssq.db   # 回到迁移前状态
docker compose up -d
curl -fsS http://127.0.0.1:18000/api/health | grep schema_version
```

> ⚠️ `.env`（`.env` 含 LLM 凭据与 `LOTT_TOKEN`）**不在** `data/` 里，本脚本不覆盖它，
> 仍需另行单独备份（例如 `cp -a .env ~/env-backup-$(date +%F) && chmod 600 ~/env-backup-*`）。

## 八、公网访问：反代 + HTTPS（可选，推荐）

主机 18000 端口**不直接暴露公网**，改用反向代理 + 自动 HTTPS：

Caddy（一条命令搞定证书）：

```caddyfile
# /etc/caddy/Caddyfile
ssq.example.com {
    reverse_proxy 127.0.0.1:18000
}
```

```bash
sudo apt install -y caddy && sudo systemctl restart caddy
```

Nginx：

```nginx
server {
    listen 443 ssl;
    server_name ssq.example.com;
    # ssl_certificate ...（如用 certbot）
    location / { proxy_pass http://127.0.0.1:18000; proxy_set_header Host $host; }
}
```

> 此时主机只需放行 80/443，18000 仅监听 127.0.0.1。

## 九、常见问题

- **端口占用/想换端口**：改 `docker-compose.yml` 中 `"18000:18000"`（左侧），或仅改左侧为 `"新端口:18000"` 保持容器内不变；
- **LLM 未生效/调用失败**：仓库不含任何模型凭据，需先在项目目录创建 `.env`（`cp .env.example .env` 后填写你自己的 `LOTT_LLM_BASE_URL` / `LOTT_LLM_API_KEY` / `LOTT_LLM_MODEL`），然后 `docker compose up -d`。确认 VPS 能访问该 API 域名；可临时设 `LOTT_LLM_DISABLED=1` 降级为纯统计模型排查；
- **多模型**：同一通道多模型用 `LOTT_LLM_MODEL_LIST=模型A,模型B`；完全独立的通道（不同 URL/Key）用 `LOTT_LLM_EXTRA_MODELS=[{...}]`。v0.8.3 起观察轮也会在模型间轮转容错（单模型挂掉不废整个 LLM 通道）；
- **推理型模型输出为空/总是超时（v0.8.3）**：聚合网关的推理模型（glm/qwen 系 flash 等）会把 max_tokens 全部耗在 reasoning_content 上导致 content 为空。用 `LOTT_LLM_EXTRA_BODY`（全模型默认）与 `LOTT_LLM_EXTRA_BODY_MAP`（按模型覆盖）下发思考控制参数，例如 api.b.ai：
  `LOTT_LLM_EXTRA_BODY={"reasoning_effort":"low"}` + `LOTT_LLM_EXTRA_BODY_MAP={"qwen3.8-flash":{"enable_thinking":false,"reasoning_effort":"none"}}`；
- **瞬态 429/5xx（v0.8.3）**：免费共享池常见并发/TPM 限流，chat 内置 2/4/6s 退避重试（最多 3 次，遵守总预算），无需人工干预；
- **预测很慢/超时**：推理型模型（如 deepseek 系列的 reasoner）单期约 2–4 分钟属正常；换轻量模型（如 minimax-m3）较快；
- **LLM 不可用/超时 = 预测失败（v0.8.2 起，有意为之）**：Web 端勾选「使用大模型」时，若通道未配置、余额耗尽、限流或超时，`/api/predict` 直接返回 503 与中文原因（前端显示「❌ 预测失败：大模型不可用或超时(...)」），**不再静默降级**为纯统计；取消勾选「使用大模型」即可用纯统计模式。调度器/离线评估仍自动降级，不影响开奖日闭环；
- **LOTT_LLM_TOTAL_TIMEOUT（默认 75s）**：单次预测 LLM 全阶段（观察+选号+校验）的墙钟预算，超时判定「大模型超时」并快速失败。**若站点在 Cloudflare 等 CDN 后面，该值 + 约 5s 统计开销必须 < CDN 代理超时（CF 为 100s，否则 HTTP 524）**，同时反代 `proxy_read_timeout` 应大于该值；直连部署（无 CDN）可按模型速度调大，如 `LOTT_LLM_TOTAL_TIMEOUT=200`。
- **时区**：已设 `TZ=Asia/Shanghai`，调度以北京时间为准；
- **合规**：页面与输出已含免责声明，勿对外声称"可预测中奖"；建议反代 + 简单鉴权（Caddy `basic_auth`）后再公网开放。

## 十、架构速览（容器内）

```
entrypoint.sh（启动：同步数据→回测→按需生成预测）
  └─ python -m lottery.cli serve  (uvicorn, :18000)
      ├─ 单页前端（web/，ECharts CDN，零构建）
      ├─ REST API（lottery/api_app.py）
      └─ 调度线程（LOTT_SCHEDULER=1：开奖日 21:35 后自动闭环）
数据卷：/app/data（ssq.db + raw 快照 + hypotheses.json 等运行时状态）
```

## 十一、推送到 GitHub

本机（bero_de）HTTPS 可直连 GitHub，PAT 存于 `/root/DSH/githubtoken.txt`：

```bash
cd /root/DSH/应用/ssqyuce
git add -A && git commit -m "..."           # 先提交
PAT=$(tr -d '\r\n' < /root/DSH/githubtoken.txt)
git -c credential.helper= push "https://jiam9069:${PAT}@github.com/jiam9069/ssqyuce.git" main

# 走显式 URL 推送**不会**更新本地 origin/main 跟踪引用，需手动同步后再看 git status：
git update-ref refs/remotes/origin/main "$(git rev-parse HEAD)"

# 核验确实落地（不要只信 push 的输出）
curl -s https://api.github.com/repos/jiam9069/ssqyuce/commits/main | grep -m1 '"sha"'
```

**推送前检查**（本仓库历史踩过密钥泄露风险）：

```bash
git status --short                      # 确认没把 .env / data/*.json / ssq.db 加进来
git diff --cached --stat                # 过一眼改动面
grep -rIl "LOTT_LLM_API_KEY\|ghp_" $(git ls-files) 2>/dev/null   # 应为空
```

> ⚠️ **凭据现状（待改进）**：`/root/DSH/githubtoken.txt` 里的 PAT 以**明文**存放，
> 且对账号下**所有**仓库有权限。建议换成仅限本仓库、权限为 Contents: Read and write
> 的细粒度令牌，并吊销旧令牌。

升级本机即完成全量部署（当前为单节点，见第六节）。
## 四·补三、V6 阶段有效性端点的部署验证

V6 新增 8 个端点，全部为**只读或短耗时**（`/api/llm_lab/run` 除外，它要调三轮大模型）：

```bash
# 1) 维度表（20 族 91 组参数，毫秒级）
curl -s http://127.0.0.1:18000/api/discovery/families | head -c 160
# 期望：{"ok":true,"families":[...],"n_families":20,"n_combos":91,...}

# 2) 多维度 as-of 扫描（约 5~15 秒；首次冷启动略慢）
curl -s "http://127.0.0.1:18000/api/discovery/scan?top=10" | head -c 260
# 期望：含 "n_tested"、"expected_false_positives"、"verdict"（纯随机下预期 4~5 个假阳性）

# 3) 阶段时效总览（毫秒级；未标注过时 items 里 regime_status 为 null）
curl -s http://127.0.0.1:18000/api/regime/overview | head -c 200

# 4) 重算阶段时效（写 data/hypotheses.json，约 5~20 秒）
curl -s -X POST http://127.0.0.1:18000/api/regime/refresh
# 期望：{"ok":true,"annotated":N,"no_dimension":M,"total":N+M}

# 5) 检索式核验（任意维度 + 档位）
curl -s "http://127.0.0.1:18000/api/backtest/regime?key=hot&params=%7B%22window%22%3A30%2C%22k%22%3A6%7D" | head -c 240

# 6) LLM 规律工坊最近结果 / 注册
curl -s http://127.0.0.1:18000/api/llm_lab/latest | head -c 200
```

⚠️ `/api/llm_lab/run` 会调用大模型三轮（提规律 → 自我质疑 → 修订），
推理型模型单轮 30~60 秒，**整轮 1~8 分钟**。入口节点 `proxy_read_timeout` 为 300 秒，
从公网触发可能 504 —— 建议在容器内网侧触发，或直接等每期调度器跑完再在页面看结果：

```bash
docker exec ssq-predictor curl -s -X POST \
  "http://127.0.0.1:18000/api/llm_lab/run?rounds=3&use_llm=1&persist=1" | head -c 400
```

**已知边界（不是故障）**：若网关上的推理型模型本轮不可用，`llm_lab` 会返回
`{"ok":true,"llm_used":false,"error":"第 1 轮失败: ..."}`，并照常给出维度扫描结果。
这是刻意的降级设计——**绝不因为大模型挂了就把扫描结论伪装成模型结论**。
