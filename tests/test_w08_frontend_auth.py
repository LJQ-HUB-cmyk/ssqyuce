"""W08b-e：前端身份层契约测试。

**为什么前端要有测试**
--------------------
本项目 V13 评审发现过多起"后端有、前端没接"或"前端调了一个后端不存在的端点"
的故障（最典型的是 `register_prereg` 零调用）。前端是零构建的原生 JS，
没有编译期检查，**唯一**能拦住这类错配的就是文本级契约测试。

本文件锁定的契约：
1. 登录/引导/用户管理所需的 DOM 元素与 JS 引用的 id **一一对应**
   （少一个 id 就是运行期 `null.value` 崩溃）；
2. 前端调用的每个 `/api/...` 路径都必须真的存在于后端路由表
   （否则表现为"点了没反应 + 404"）；
3. 普通用户界面**不得**把管理员端点当成自己的入口；
4. CSRF：写操作必须能带上 `X-CSRF-Token`；
5. 静态资源版本令牌必须一致（否则浏览器用旧 CSS 配新 HTML）。
"""
from __future__ import annotations

import re
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
INDEX_HTML = WEB_DIR / "index.html"
APP_JS = WEB_DIR / "static" / "app.js"
STYLE_CSS = WEB_DIR / "static" / "style.css"


def _html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _js() -> str:
    return APP_JS.read_text(encoding="utf-8")


def _css() -> str:
    return STYLE_CSS.read_text(encoding="utf-8")


def _js_code() -> str:
    """返回**剥离注释后**的 JS 源码。

    ⚠️ 为什么必须剥注释（2026-10-21 实测教训）：
    本用例最初用 ``assert "www-authenticate" in js.lower()`` 断言三形态判定存在。
    做变异测试时把代码里的 ``headers.get("www-authenticate")`` 全删掉，
    测试**仍然通过** —— 因为文件顶部的文档注释里写着 ``WWW-Authenticate``，
    转小写后正好满足断言。也就是说那条断言测的是"注释里提过这个词"，
    不是"代码实现了这个判断"。

    一个抓不住回归的断言，比没有断言更危险：它给人"已验证"的错觉。
    所以所有涉及"代码是否存在某行为"的断言，一律用本函数剥离注释后再比。
    """
    js = _js()
    # 去掉 /* ... */ 块注释（含 JSDoc），保留字符串字面量里的内容
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    # 去掉 // 行注释（避开 http:// 这类出现在字符串里的情况：要求 // 前不是 ':'）
    js = re.sub(r"(?<![:\"'])\s*//[^\n]*", "", js)
    return js


def _ids_in_html() -> set:
    return set(re.findall(r'id="([^"]+)"', _html()))


# ---------------------------------------------------------------------------
# 1. DOM 契约
# ---------------------------------------------------------------------------

def test_auth_dom_ids_exist():
    """登录层与用户管理要用的 id 必须都在 HTML 里。"""
    ids = _ids_in_html()
    required = [
        "authOverlay", "authUser", "authPass", "authLoginBtn", "authError",
        "authLoginForm", "authHint", "authBootstrapBox", "bootToken", "bootPass",
        "bootError", "authBootBtn", "authBootstrapForm",
        "userAdminOverlay", "userAdminList", "nuName", "nuPass", "nuRole", "nuStatus",
        "userChip", "navUserAdmin",
        # 设置页：W08d 新增/复用的
        "llmScopeNote", "btnSaveLlm", "btnTestLlm", "cfgApiKey",
        "cfgBaseUrl", "cfgModel", "cfgSamples", "cfgLlmEnabled", "llmStatus",
    ]
    missing = [i for i in required if i not in ids]
    assert not missing, f"HTML 缺少这些 id：{missing}"


def test_auth_overlay_starts_hidden():
    """遮罩**必须**默认隐藏。

    如果默认可见，那么在未启用鉴权（AUTH_MODE=off）的部署上，用户一进站
    就被一个"请登录"挡住 —— 而后端此时根本不要求登录，用户没有任何账号可登。
    """
    html = _html()
    for el_id in ("authOverlay", "userAdminOverlay"):
        m = re.search(r'<div class="auth-overlay" id="' + el_id + r'"[^>]*>', html)
        assert m, f"找不到 {el_id} 的 DOM 声明"
        assert 'style="display:none"' in m.group(0), f"{el_id} 必须默认 display:none"


# ---------------------------------------------------------------------------
# 2. 端点契约：前端调的每个路径后端都得有
# ---------------------------------------------------------------------------

def test_frontend_api_paths_exist_in_backend():
    """前端调用的 `/api/...` 字面量路径，必须在后端路由表里找得到。

    只检查**字面量**（模板拼接的跳过），因为只有字面量能在这里静态比对。
    """
    from lottery import api_app
    from tests.test_mu1_auth import _collect_route_paths

    routes = set(_collect_route_paths(api_app.app))
    # 去掉路径参数，便于做"前缀 + 段数"匹配
    js = _js()
    called = set(re.findall(r'api\(\s*"(/api/[^"?#]+)"', js))
    called |= set(re.findall(r'fetch\(\s*"(/api/[^"?#]+)"', js))
    assert called, "没有解析出任何前端 API 调用 —— 正则失效了，测试会假绿"

    def _matches(path: str) -> bool:
        if path in routes:
            return True
        # 含 {param} 的路由：按段数比对
        p_segs = path.strip("/").split("/")
        for r in routes:
            r_segs = r.strip("/").split("/")
            if len(r_segs) != len(p_segs):
                continue
            if all(rs.startswith("{") or rs == ps for rs, ps in zip(r_segs, p_segs)):
                return True
        return False

    unknown = sorted(p for p in called if not _matches(p))
    assert not unknown, f"前端调用了后端不存在的端点：{unknown}"


def test_login_layer_uses_the_new_user_llm_endpoint():
    """设置页必须走 `/api/user/llm/config`，不能再用管理员端点 `/api/config/llm`。

    用管理员端点做"我的配置"的后果：普通用户每次进设置页都被 403，
    界面上却显示"需要管理员权限"——而他想改的明明是自己的模型配置。
    """
    js = _js()
    assert '"/api/user/llm/config"' in js
    assert 'api("/api/config/llm"' not in js, "设置页不应再用管理员专用端点"


def test_admin_panel_only_calls_admin_endpoints():
    """用户管理面板只能打 `/api/admin/users*`。"""
    js = _js()
    assert '"/api/admin/users"' in js
    assert '"/api/admin/users/" + userId' in js


# ---------------------------------------------------------------------------
# 3. CSRF
# ---------------------------------------------------------------------------

def test_csrf_header_is_attached_to_writes():
    js = _js()
    assert "X-CSRF-Token" in js
    assert "AUTH.csrf" in js


def test_login_does_not_persist_csrf_to_localstorage():
    """CSRF token 不落 localStorage：XSS 能读到的东西不该包括它。"""
    js = _js()
    assert 'localStorage.setItem("csrf' not in js
    assert "localStorage.setItem('csrf" not in js


# ---------------------------------------------------------------------------
# 4. 401 / 403 / 423 鉴权失败处理
# ---------------------------------------------------------------------------
#
# 断言原则（2026-10-21 修订，李工）：**断言行为，不断言实现字面量**。
#
# 背景：本用例原先断言源码里必须出现 `"r.status === 401"`。刘工 task-4 把
# 401/403/423 分支抽成 `handleAuthFailure()` 后，行为完全一致（已用真浏览器
# Playwright 取证：修复后 overlayShown=true、并发 5 个写操作只跳一次），
# 但字面量 `r.status === 401` 不再出现 → 测试假红。
#
# 教训：字符串级断言把「实现写法」当成了「契约」。真正的契约是
# `docs/team/design/API-1-auth-session.md` §1.4 的三形态判定：
#   401 + WWW-Authenticate: Cookie → 跳登录
#   401 + WWW-Authenticate: Bearer → 提示，不跳登录
#   401 无该头                    → 登录接口凭据错，就地显示
#   403 + CSRF                    → 恢复 csrf，不按会话失效处理
#   423                           → 提示，不自动重试
# 所以断言改成：三个形态各自的关键行为标记都在，且不绑定具体写法。

def test_api_handles_401_by_showing_login():
    """401 三分支：只有 `Cookie` 分支会跳登录层，且跳登录只有一个入口。

    断言对象是**剥离注释后的代码**（见 `_js_code`）：注释里提到不算实现。
    """
    js = _js_code()
    low = js.lower()
    # ① 按响应头区分三形态（而不是按路径枚举）
    assert "www-authenticate" in low, (
        "401 必须按 WWW-Authenticate 头区分三形态（契约 API-1 §1.4），"
        "否则 423/403 与真正的会话失效会被混为一谈")
    # ② 三种形态都要有对应处理标记
    assert "cookie" in low and "bearer" in low, (
        "必须同时处理 Cookie 与 Bearer 两种 401 形态（契约 §1.4 前两行）")
    # ③ 跳登录层确实存在（行为结果，不是写法）
    assert "showAuthOverlay(true" in js, "必须存在跳登录层的动作"
    # ④ 401 判定仍然存在于代码（允许 `status === 401` 或 `r.status === 401`，
    #    不为任何单一写法背书）
    assert re.search(r"(?:\bstatus\b|\br\.status\b)\s*===\s*401", js), (
        "必须仍存在 401 判定分支")


def test_csrf_failure_is_not_treated_as_session_expiry():
    """403 + CSRF 必须走「恢复凭据」而不是「会话失效」。

    契约 API-1 §1.5：CSRF 失败是 403 且 body 无 `auth_required`，
    若按 401 处理会让用户反复被踢出登录。这条正是缺陷 1/2 的根治点，
    刘工 task-4 用 `csrfRecovering` 做了并发去重。
    """
    js = _js_code()
    assert "CSRF" in js, "必须识别 CSRF 失败文案"
    assert re.search(r"csrfRecovering|recoverCsrf", js), (
        "CSRF 恢复必须有并发去重标记（契约 §1.5 + task-4 AC「并发只跳一次」）")
    # ⑤' 必须真的**判定** CSRF 文案，而不只是"调用恢复语句还在"。
    #     2026-10-21 变异验证实测：把 `if (msg.indexOf("CSRF") >= 0)` 整句换成
    #     `if (false)` 后，下面的调用断言仍然通过（调用语句没被删，只是永不执行）——
    #     说明单靠「调用存在」锚不住「条件正确」。等价破坏必须两种都试。
    assert re.search(r'indexOf\(\s*"CSRF"\s*\)', js), (
        "必须按后端文案判定 403 CSRF（契约 §1.5 只给了文案，没有错误码），"
        "否则条件恒假时调用语句仍在、测试假绿")
    # ⑤ 恢复必须被**真正调用**：只定义不调用 = 死代码。
    #    2026-10-21 变异测试实测：把 `recoverCsrfAndHint(path);` 这行删掉，
    #    上面两条断言仍然全绿（函数定义还在，字符串仍在代码里）——典型假绿。
    #    注意必须要求**末尾分号**：函数定义 `async function recoverCsrfAndHint(path) {`
    #    本身就能匹配 `recoverCsrfAndHint(path)`——我第一次就是这么写的，删掉调用仍全绿。
    assert re.search(r"\brecoverCsrfAndHint\s*\(\s*path\s*\)\s*;", js), (
        "403 CSRF 分支必须实际调用 recoverCsrfAndHint(path);（带分号＝调用，定义行无分号），"
        "只定义不调用等于没恢复")
    # ⑥ 恢复函数不能读共享的 AUTH 对象来判断成败（读它 = 读原值 = 什么都没验）。
    #    实测教训：旧写法 `const j = await loadAuthState(); if (j.csrf)` 永远判成功、永不跳登录。
    assert not re.search(r"const\s+j\s*=\s*await\s+loadAuthState\(\)", js), (
        "恢复里不得读 loadAuthState() 的返回值（共享 AUTH 对象），必须重新探测 /api/auth/me")


def test_locked_account_is_not_auto_retried():
    """423 账号锁定：只提示，不自动重试（重试会续锁）。"""
    js = _js_code()
    assert "423" in js, "必须显式处理 423 账号锁定（契约 API-1 §1.4）"


def test_boot_probes_auth_before_loading_data():
    """启动必须先 `loadAuthState()` 再 `loadAll()`。

    反过来的话，未登录时 4 个并发请求全部 401，4 条错误 toast 叠加
    会把登录层盖住，用户看到"一堆报错"而不是"请登录"。
    """
    js = _js()
    boot = js[js.index("(async function boot()"):]
    boot = boot[:boot.index("})();")]
    assert boot.index("loadAuthState") < boot.index("loadAll")


# ---------------------------------------------------------------------------
# 5. 静态资源版本
# ---------------------------------------------------------------------------

def test_static_version_tokens_are_consistent():
    html = _html()
    css_v = re.search(r'style\.css\?v=([0-9.]+)', html)
    js_v = re.search(r'app\.js\?v=([0-9.]+)', html)
    assert css_v and js_v, "静态资源必须带 ?v= 令牌（否则改版后浏览器用旧缓存）"
    assert css_v.group(1) == js_v.group(1), (
        f"CSS({css_v.group(1)}) 与 JS({js_v.group(1)}) 版本令牌不一致")
    assert css_v.group(1) >= "20261020.1", "W08 改了前端，令牌必须升级以击穿缓存"


def test_auth_styles_exist():
    css = _css()
    for sel in (".auth-overlay", ".auth-card", ".auth-user-row", ".user-chip"):
        assert sel in css, f"缺少样式 {sel}"


# ---------------------------------------------------------------------------
# 6. 账号口令不落前端
# ---------------------------------------------------------------------------

def test_password_inputs_are_type_password():
    html = _html()
    for el_id in ("authPass", "bootPass", "nuPass"):
        m = re.search(r'<input[^>]*id="' + el_id + r'"[^>]*>', html)
        assert m, f"找不到输入框 {el_id}"
        assert 'type="password"' in m.group(0), f"{el_id} 必须是 password 类型"


def test_new_password_inputs_are_not_autofilled():
    """新建/重置口令的输入框不应被浏览器自动填充成登录口令。"""
    html = _html()
    m = re.search(r'<input[^>]*id="nuPass"[^>]*>', html)
    assert 'autocomplete="new-password"' in m.group(0)
