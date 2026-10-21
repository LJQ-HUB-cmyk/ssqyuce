# tests/frontend · 前端取证脚本（刘工维护）

> 位置：Lead 2026-10-21 裁定 `tests/test_*frontend*.py` 归李工（断言后端契约），
> 前端取证落 `tests/frontend/`，不与后端契约测试混放。

## task4_csrf_403_probe.py · 403 CSRF 失败的用户可见行为（真实浏览器）

前置：
1. 起隔离实例，**绝不连生产库**（用 `scripts/isolated_test.sh` 的同一套约定）：
   ```bash
   scripts/isolated_test.sh --keep -- .venv/bin/python - <<'PY'
   from lottery import auth
   auth.create_user("qa_csrf", "TOKEN_PLACEHOLDER", role="admin")
   PY
   # 记下保留的临时目录，然后：
   PYTHONPATH=$PWD LOTT_HOME=<TMP>/home LOTT_DB=<TMP>/ssq.db LOTT_BOOT_REFRESH_RULES=0 \
     .venv/bin/python -m lottery.cli serve --host 127.0.0.1 --port 18001
   ```
2. 口令只从环境变量传入，**不写死**：
   ```bash
   QA_PASS='<测试账号口令>' python3 tests/frontend/task4_csrf_403_probe.py
   ```
   依赖系统 `playwright`（`python3 -c "import playwright"`）+ `/usr/bin/chromium`；
   项目 `.venv` 里没有 playwright，用系统 python 跑。

取证要求（不满足即视为未取证）：
- 登录走**真实** `/api/auth/login`，不伪造 csrf 值（伪造的 csrf 后端一律拒，测不出真实链路）；
- 写操作用真实端点，观察 Network 里的**真实请求**与状态码，不看封装后的提示；
- 并发场景至少 5 条同时打，确认只提示/只跳转一次。

> 探针注入（`?qa_probe=1` + `_probe_403.js`）只用于取证，用完必须还原 `web/index.html`
> 并删除 `web/static/_probe_403.js`；`git status` 里不得出现在产品文件上。

## ⚠️ 坑：`__Host-` 前缀 Cookie 在 http 非 loopback host 下会被浏览器丢弃

**症状**：登录接口 `200 + 有 csrf_token`，紧接着 `/api/auth/me` 返回 `authenticated:false`，
后续写操作全部 401「未登录」。看起来像「会话中间件坏了」。

**根因（浏览器规则，不是后端 bug）**：
- `http://127.0.0.1` 被浏览器视作 **secure context** → Secure Cookie 可用；
- `http://<局域网 IP>` **不是** secure context，而 `__Host-` 前缀**强制要求 Secure**，
  浏览器直接丢弃该 Cookie → 后续请求不带 Cookie。

**实测（2026-10-21，Chromium 155，同一隔离实例，控制变量：只换 host）：**
```
默认前缀 __Host-ssq_sid
  127.0.0.1   → isSecureContext=true,  login=200, meAuthed=true
  172.17.0.1  → isSecureContext=false, login=200, meAuthed=false   ← Cookie 被丢弃
改名后 LOTT_COOKIE_NAME=ssq_sid LOTT_COOKIE_SECURE=0
  172.17.0.1  → isSecureContext=false, login=200, meAuthed=true    ← 解法成立
```

**排查顺序**：http 下「登录后立刻失效」，**先查 `__Host-` 前缀是否被浏览器拒绝**
（用 `LOTT_COOKIE_NAME` 覆盖，`lottery/config.py:114`），**不要先去怀疑会话中间件**。
前者是环境问题，后者会让你去改不该改的代码。

脚本：`cookie_prefix_check.py`（`QA_PASS=... [LAN_IP=172.17.0.1 P=18002] python3 ...`）

## 变异验证（`mutation_check.py`）

**为什么每个行为要两种破坏方式**（严QA 2026-10-21 的对等补充）：
单次变异只能证明「这一种破坏能被抓」；**换一种等价破坏仍能抓，才证明断言锚在行为上**，
而不是记住了某个具体写法。

```bash
.venv/bin/python tests/frontend/mutation_check.py        # 全部
.venv/bin/python tests/frontend/mutation_check.py -k csrf
```

判据：每个变异必须让**指定测试变红**；跑完必须还原到原字节（校验 sha256）且基线全绿。
还原不彻底 = 污染工作区，比变异本身更危险，所以脚本把这步做成硬校验。

**首次运行就抓到我自己一个缺口（保留记录）**：
「把 `if (msg.indexOf("CSRF") >= 0)` 整句换成 `if (false)`」→ 测试**没红**。
原因：断言只要求「`recoverCsrfAndHint(path);` 调用语句存在」，而条件恒假时调用语句仍在、永不执行。
→ 补了一条「必须真的判定 CSRF 文案」的断言，两种破坏才都能抓住。
**这就是「一种破坏不够」的实证：它抓出的不是代码 bug，是我断言的盲区。**
