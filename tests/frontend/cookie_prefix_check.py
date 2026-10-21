"""`__Host-` 前缀 Cookie 的 host 差异验证（零猜测版）。

背景：同一个 http 实例，在 `127.0.0.1` 上登录保持、在局域网 IP 上「登录成功但立刻变未登录」。
根因不是会话中间件，是浏览器的 Cookie 规则：
  * `127.0.0.1` 被浏览器视为 **secure context**（Secure Cookie 可用）；
  * `http://<局域网IP>` 不是 secure context，而 `__Host-` 前缀**强制要求 Secure**，
    浏览器直接丢弃该 Cookie → 后续请求无 Cookie → `authenticated:false`。

用法：
  LOTT_COOKIE_NAME 默认 `__Host-ssq_sid`。要跨 host 复现，改用
  `LOTT_COOKIE_NAME=ssq_sid LOTT_COOKIE_SECURE=0` 覆盖前缀（config.py:114/116）。

  QA_PASS=... python3 tests/frontend/cookie_prefix_check.py
  # 可选 LAN_IP=172.17.0.1 P=18002

⚠️ 排查顺序（严QA 2026-10-21 的教训）：http 下「登录后立刻失效」，
    先查 `__Host-` 前缀是否被浏览器拒绝，**不要先去怀疑会话中间件**——
    前者是环境问题，后者会让你去改不该改的代码。

实测输出（2026-10-21，Chromium 155）：
  默认前缀：127.0.0.1 → secureContext=true, meAuthed=true
             172.17.0.1 → secureContext=false, meAuthed=false   ← Cookie 被丢弃
  改名前缀：172.17.0.1 → secureContext=false, meAuthed=true    ← 严QA 的解法成立
"""
import json
import os

from playwright.sync_api import sync_playwright

PORT = os.environ.get("P", "18001")
USER = os.environ.get("QA_USER", "qa_csrf")
PASS = os.environ.get("QA_PASS", "")
HOSTS = ["127.0.0.1", os.environ.get("LAN_IP", "172.17.0.1")]

if not PASS:
    raise SystemExit("缺少 QA_PASS（测试账号口令），不猜、不写死")

with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/chromium", headless=True,
                          args=["--no-sandbox"])
    for host in HOSTS:
        ctx = b.new_context()
        page = ctx.new_page()
        page.goto(f"http://{host}:{PORT}/", wait_until="load")
        sec = page.evaluate("() => window.isSecureContext")
        r = page.evaluate(
            """async ([u, p]) => {
                 const res = await fetch('/api/auth/login', {method: 'POST',
                   headers: {'Content-Type': 'application/json'},
                   body: JSON.stringify({username: u, password: p})});
                 const j = await res.json().catch(() => ({}));
                 const me = await (await fetch('/api/auth/me')).json().catch(() => ({}));
                 return {login: res.status, loginOk: !!j.ok, csrf: !!j.csrf_token,
                         meAuthed: !!me.authenticated};
               }""", [USER, PASS])
        print(json.dumps({"host": host, "isSecureContext": sec, **r}, ensure_ascii=False))
        ctx.close()
    b.close()
