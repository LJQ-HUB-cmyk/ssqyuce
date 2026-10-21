"""task-4 浏览器实跑取证：403 CSRF 失败时用户看到什么。

前置：隔离实例运行在 127.0.0.1:18001（临时库，不碰生产），测试账号 qa_csrf。
本脚本用真实 Chromium（Playwright CDP）打开页面，执行真实写操作，采集
  - Network：真实发出的请求条数与状态码
  - UI：弹层/提示文本
并与「修复前」的代码路径做对照。

⚠️ 不写死任何口令：从环境变量 QA_PASS 读；缺失则直接报错退出（不猜）。
"""
import json
import os
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get("QA_BASE", "http://127.0.0.1:18001")
USER = os.environ.get("QA_USER", "qa_csrf")
PASS = os.environ.get("QA_PASS", "")
CHROME = "/usr/bin/chromium"

if not PASS:
    print("缺少环境变量 QA_PASS（测试账号口令），不猜、不写死。", file=sys.stderr)
    sys.exit(2)

out = []


def log(**kw):
    out.append(kw)
    print(json.dumps(kw, ensure_ascii=False))


with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=CHROME, headless=True,
                                args=["--no-sandbox", "--disable-dev-shm-usage"])
    ctx = browser.new_context()
    page = ctx.new_page()

    net = []
    page.on("request", lambda r: net.append({"phase": "req", "url": r.url.split("?")[0],
                                             "method": r.method,
                                             "csrf": r.headers.get("x-csrf-token", "")}))
    page.on("response", lambda r: net.append({"phase": "resp", "url": r.url.split("?")[0],
                                              "status": r.status}))

    # 探针在页面上下文里**内联定义**，不注入任何产品文件（早期版本改 index.html 注入
    # _probe_403.js，虽已还原，但那种做法会污染产品文件、也可能被误提交 —— 弃用）。
    page.goto(BASE + "/", wait_until="load")
    page.wait_for_function("typeof api === 'function' && typeof AUTH !== 'undefined'", timeout=15000)
    page.evaluate(
        """() => {
          // 计数钩子：证明「并发只提示/只跳转一次」靠的是**观测到的次数**，
          // 不是读代码推断。toast / showAuthOverlay 都是顶层函数声明（全局属性），
          // 内部调用经全局作用域解析，覆盖后仍能被捕获。
          window.__toasts = [];
          window.__overlayCalls = [];
          const _toast = window.toast;
          window.toast = function (m) { window.__toasts.push(String(m)); return _toast.apply(null, arguments); };
          const _overlay = window.showAuthOverlay;
          window.showAuthOverlay = function (show, hint) {
            if (show) window.__overlayCalls.push(String(hint || ''));
            return _overlay.apply(null, arguments);
          };
          window.__QA = {
            call: async (path, opts) => {
              try { return { ok: true, data: await api(path, opts || {}) }; }
              catch (e) { return { ok: false, status: e.status, msg: e.message, payload: e.payload }; }
            },
            state: () => ({
              authenticated: AUTH.authenticated,
              enforced: AUTH.enforced,
              csrfLen: (AUTH.csrf || '').length,
              overlayShown: ((($('#authOverlay') || {}).style || {}).display) === 'flex',
              overlayHint: (($('#authHint') || {}).textContent || ''),
            }),
            resetCsrf: () => { AUTH.csrf = ''; },
            setCsrf: (v) => { AUTH.csrf = v; },
          };
        }"""
    )

    # ---------- 场景 1：真实登录（拿到真 csrf） ----------
    login = page.evaluate(
        "async ([u,p]) => await window.__QA.call('/api/auth/login',"
        " {method:'POST', headers:{'Content-Type':'application/json'},"
        "  body: JSON.stringify({username:u, password:p})})",
        [USER, PASS],
    )
    log(case="1_login", ok=login.get("ok"), status=login.get("status"),
        has_csrf=bool((login.get("data") or {}).get("csrf_token")),
        csrf_len=len((login.get("data") or {}).get("csrf_token") or ""))
    # 登录成功后把 csrf 灌进 AUTH（模拟 doLogin 的真实赋值）
    page.evaluate("(t) => window.__QA.setCsrf(t)", (login.get("data") or {}).get("csrf_token"))

    # ---------- 场景 1b：csrf 有效时的写操作（证明新分支不误伤正常路径） ----------
    ok_write = page.evaluate(
        "async () => await window.__QA.call('/api/auth/password',"
        " {method:'POST', headers:{'Content-Type':'application/json'},"
        "  body: JSON.stringify({old_password:'definitely-wrong', new_password:'x'})})"
    )
    log(case="1b_valid_csrf_write", status=ok_write.get("status"),
        msg=ok_write.get("msg"), note="期望 400（原口令错），不是 403/401")
    log(case="1b_ui", **page.evaluate("() => window.__QA.state()"))

    # ---------- 场景 2：页面刷新后 csrf 丢失 + 写操作（真实触发 403） ----------
    page.evaluate("() => window.__QA.resetCsrf()")
    before = len(net)
    res = page.evaluate(
        "async () => await window.__QA.call('/api/auth/password',"
        " {method:'POST', headers:{'Content-Type':'application/json'},"
        "  body: JSON.stringify({old_password:'x', new_password:'y'})})"
    )
    log(case="2_after_refresh_write", ok=res.get("ok"), status=res.get("status"),
        msg=res.get("msg"), payload=res.get("payload"))

    # ---------- 场景 3：并发放大——5 个写操作同时打，确认只提示/只跳一次 ----------
    net_http = [n for n in net[before:] if n["phase"] == "resp"]
    page.evaluate("() => window.__QA.setCsrf('')")
    conc = page.evaluate(
        "async () => { const ps=[]; for (let i=0;i<5;i++) ps.push(window.__QA.call('/api/auth/password',"
        " {method:'POST', headers:{'Content-Type':'application/json'},"
        "  body: JSON.stringify({old_password:'x', new_password:'y'})}));"
        " const rs = await Promise.all(ps); return rs.map(r=>({s:r.status,m:r.msg})); }"
    )
    log(case="3_concurrent_5", results=conc,
        all_403=all(r.get("s") == 403 for r in conc))

    # ---------- 场景 3b：并发去重的量化证据（数真实网络请求，不靠 hook 内部函数） ----------
    # 去重是否生效，最硬的证据是「恢复动作发起了几次」→ 数场景 3 期间 /api/auth/me 的响应条数。
    # 未去重会 5 次（每个 403 各拉一遍）；去重后应为 1 次。
    me_calls = [n for n in net[before:] if n["phase"] == "resp" and n["url"].endswith("/api/auth/me")]
    log(case="3b_dedupe", concurrent_403=len(conc),
        me_requests_during_concurrent=len(me_calls),
        expect="1（去重生效）；若为 5 说明每个 403 各自拉了一遍",
        toasts_dom=page.evaluate("() => ((document.querySelector('#toast')||{}).textContent||'').trim()"),
        overlay_calls=page.evaluate("() => (window.__overlayCalls || []).slice()"))

    # ---------- 场景 3c：恢复也失败（契约违背：已登录但 me 不给 csrf）→ 必须跳登录 ----------
    # 造法：拦截 /api/auth/me，把 csrf_token 改成 null（模拟「me 未按 v1.1 下发」的实现缺陷）。
    # 这是纯前端验证，不改后端代码。
    page.evaluate("() => { window.__overlayCalls = []; }")
    page.evaluate("() => window.__QA.setCsrf('')")
    page.route("**/api/auth/me*", lambda route: route.fulfill(
        status=200,
        content_type="application/json",
        body='{"ok":true,"authenticated":true,"user":{"user_id":13,"username":"SAMPLE",'
             '"role":"admin","status":"active","display_name":"","is_primary_admin":false,'
             '"last_login_at":null},"auth_enforced":true,"csrf_token":null}',
    ))
    err = page.evaluate(
        "async () => await window.__QA.call('/api/auth/password',"
        " {method:'POST', headers:{'Content-Type':'application/json'},"
        "  body: JSON.stringify({old_password:'x', new_password:'y'})})"
    )
    page.wait_for_timeout(600)
    log(case="3c_recover_fails", write_status=err.get("status"),
        overlay_calls=page.evaluate("() => (window.__overlayCalls || []).slice()"),
        ui=page.evaluate("() => window.__QA.state()"),
        note="期望：跳登录层恰好 1 次（契约 §3.1 违背 → §1.5 兜底）")
    page.unroute("**/api/auth/me*")
    # 复位成正常状态，供后续场景用
    page.evaluate("() => window.__QA.setCsrf('')")

    # ---------- 场景 4：UI 状态（用户看到什么） ----------
    page.wait_for_timeout(400)
    log(case="4_ui_state", **page.evaluate("() => window.__QA.state()"))

    # ---------- 真实请求条数（从 Network 采，不是从代码推） ----------
    writes = [n for n in net if n["phase"] == "req" and n["method"] == "POST"
              and "/api/auth/password" in n["url"]]
    resp403 = [n for n in net if n["phase"] == "resp" and n["status"] == 403]
    log(case="5_network", write_requests=len(writes),
        csrf_headers=[w["csrf"] for w in writes], resp_403=len(resp403))

    browser.close()

json.dump(out, open("/tmp/task4_evidence.json", "w"), ensure_ascii=False, indent=2)
print("\n证据已写入 /tmp/task4_evidence.json")
