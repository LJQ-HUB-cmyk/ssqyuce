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
# 4. 401 处理
# ---------------------------------------------------------------------------

def test_api_handles_401_by_showing_login():
    js = _js()
    assert "r.status === 401" in js
    assert "showAuthOverlay(true" in js


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
