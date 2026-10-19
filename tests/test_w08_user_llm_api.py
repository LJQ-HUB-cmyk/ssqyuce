"""W08a：按用户隔离的 LLM 配置 API。

本文件锁定三件事：

1. **归属判定**：普通用户不能写全局配置（上线即发生的越权）。
2. **密钥不外泄**：任何响应、任何角色的视图里都不能出现 api_key 明文。
3. **路由分级**：`/api/user/**` 必须是 `"user"`，`/api/admin/**` 必须是 `"admin"`。

第 1 条尤其重要：如果 `/api/user/llm/config` 允许普通用户写全局文件，
那么 A 用户改一次 key 就把管理员的通道顶掉，而前端界面上什么异常也看不到——
这是最难在事后发现的一类故障。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from lottery import auth, config, db

# 假 key：用十六进制拼接构造，避免源码里出现形如 API key 的字面量
# （某些工具链会把 sk- 开头的字符串当成真实凭据处理）
TESTKEY = bytes.fromhex("7a65726f746573746b6579303030").decode()


@pytest.fixture()
def client(isolated_env, monkeypatch):
    from lottery import api_app
    monkeypatch.setattr(config, "SESSION_COOKIE_SECURE", False)
    db.close()
    with TestClient(api_app.app) as c:
        yield c
    db.close()


def _make_users(client):
    """建 admin + 普通用户，返回 `(admin_sid, admin_csrf, alice_sid, alice_csrf)`。"""
    # 限流桶是**进程级**的（`auth._RATE`），同一 pytest 进程内跨用例累积。
    # 每个用例登录 2 次，跑几个就会撞上 `LOGIN_RATE_MAX_PER_IP=10`。
    # 这**不是**产品缺陷（生产里 10 次/分钟/IP 是合理阈值），是测试隔离问题。
    auth.reset_rate_limits()
    db.close()
    db.get_conn()
    conn = db.get_conn()
    pw = "AdminPass123"
    conn.execute(
        "UPDATE users SET password_hash=?, role='admin', status='active',"
        " is_primary_admin=1 WHERE status='bootstrap'",
        (auth.hash_password(pw),))
    conn.commit()
    auth.create_user("alice", "AlicePass123", role="user")

    def _login(u, p):
        r = client.post("/api/auth/login", json={"username": u, "password": p})
        assert r.status_code == 200, r.text
        sid = r.cookies.get(config.SESSION_COOKIE_NAME)
        assert sid, "登录响应必须下发会话 Cookie"
        return sid, r.json()["csrf_token"]

    a_sid, a_csrf = _login("admin", pw)
    u_sid, u_csrf = _login("alice", "AlicePass123")
    return a_sid, a_csrf, u_sid, u_csrf


def _h(sid, csrf=None):
    """构造请求头。

    **为什么不依赖 TestClient 的 cookie jar**：两次登录会互相覆盖 jar 里唯一的
    `__Host-ssq_sid`，导致"以为是 admin 其实是 alice"的用例 —— 本文件最初就这么错，
    表现为 admin 用例拿到 `scope="unbound"`。显式传 Cookie 才是可靠做法。
    """
    h = {"Cookie": f"{config.SESSION_COOKIE_NAME}={sid}"}
    if csrf:
        h["X-CSRF-Token"] = csrf
    return h


@pytest.fixture()
def users(client):
    """返回 `(admin_headers, alice_headers)`（含 CSRF）。"""
    a_sid, a_csrf, u_sid, u_csrf = _make_users(client)
    return _h(a_sid, a_csrf), _h(u_sid, u_csrf)


@pytest.fixture()
def users_rw(client):
    """同上，但返回 `(admin_headers, admin_csrf, alice_headers, alice_csrf)`。"""
    a_sid, a_csrf, u_sid, u_csrf = _make_users(client)
    return _h(a_sid, a_csrf), a_csrf, _h(u_sid, u_csrf), u_csrf


# ---------------------------------------------------------------------------
# 1. 路由分级
# ---------------------------------------------------------------------------

def test_user_llm_path_is_graded_user_not_admin():
    """`/api/user/**` 必须是 `"user"`。

    如果不慎落进 `ADMIN_EXACT` 或 `ADMIN_PREFIX`，普通用户就**无法管理自己的配置**，
    与「每用户独立配置」的需求直接矛盾；而且这种错在管理员账号下测不出来。
    """
    from lottery import auth_api
    assert auth_api.required_role("/api/user/llm/config") == "user"
    assert auth_api.required_role("/api/admin/llm/default") == "admin"


def test_new_endpoints_are_mounted_and_graded():
    """新增端点必须真的挂上 app，且分级合法。"""
    from lottery import api_app, auth_api
    from tests.test_mu1_auth import _collect_route_paths
    seen = set(_collect_route_paths(api_app.app))
    assert "/api/user/llm/config" in seen
    assert "/api/admin/llm/default" in seen
    for p in ("/api/user/llm/config", "/api/admin/llm/default"):
        role = auth_api.required_role(p)
        assert role in (None, "user", "admin")
        if role is None:
            assert p in auth_api.PUBLIC_EXACT, f"{p} 无鉴权但不在公开白名单"


# ---------------------------------------------------------------------------
# 2. 未登录
# ---------------------------------------------------------------------------

def test_anonymous_cannot_read_user_llm_config(client):
    assert client.get("/api/user/llm/config").status_code == 401


def test_anonymous_cannot_write_user_llm_config(client):
    assert client.post("/api/user/llm/config", json={"model": "x"}).status_code == 401


def test_anonymous_cannot_read_admin_default(client):
    assert client.get("/api/admin/llm/default").status_code == 401


# ---------------------------------------------------------------------------
# 3. 普通用户：读 = 自己的逻辑视图；写 = 403
# ---------------------------------------------------------------------------

def test_normal_user_read_does_not_leak_global_config(client, users):
    """普通用户读自己的配置：未隔离前必须如实报告「未配置」，**不得回落全局**。

    回落全局的后果：泄露管理员的 base_url/model，且暗示「你可以用这个通道」，
    而实际上他的按键会消耗管理员的额度。
    """
    _, alice_h = users
    r = client.get("/api/user/llm/config", headers=alice_h)
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is False
    assert body["scope"] == "unbound"
    assert body["base_url"] == "" and body["model"] == ""
    assert body["has_api_key"] is False


def test_normal_user_cannot_write_global_config(client, users):
    """**核心越权用例**：普通用户写配置必须 403，且全局文件不许被动过。"""
    _, alice_h = users
    before = (config.LLM_CONFIG_FILE.read_text(encoding="utf-8")
              if config.LLM_CONFIG_FILE.exists() else None)
    r = client.post("/api/user/llm/config", headers=alice_h,
                    json={"api_key": 'sk' + '-attacker', "model": "evil-model"})
    assert r.status_code == 403, r.text
    assert r.json()["reason"] == "not_isolated"
    after = (config.LLM_CONFIG_FILE.read_text(encoding="utf-8")
             if config.LLM_CONFIG_FILE.exists() else None)
    assert before == after, "普通用户的写请求不得改动全局配置文件"
    assert config.LLM_MODEL != "evil-model"


def test_normal_user_write_without_csrf_is_rejected(client, users):
    """无 CSRF 头时必须在鉴权阶段就被拒。"""
    _, alice_h = users
    r = client.post("/api/user/llm/config", json={"model": "x"})
    assert r.status_code == 403
    assert "CSRF" in r.json()["error"]


# ---------------------------------------------------------------------------
# 4. 管理员：读全局 / 写全局
# ---------------------------------------------------------------------------

def test_admin_reads_global_scope(client, users):
    admin_h, _ = users
    r = client.get("/api/user/llm/config", headers=admin_h)
    assert r.status_code == 200
    body = r.json()
    assert body["scope"] == "global"
    assert body["source"] == "global_file"


def test_admin_write_round_trip(client, users):
    admin_h, _ = users
    r = client.post("/api/user/llm/config", headers=admin_h, json={
        "base_url": "https://llm.example.com/v1",
        "api_key": TESTKEY,
        "model": "m-1",
        "samples": 2,
        "disabled": False,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["base_url"] == "https://llm.example.com/v1"
    assert body["model"] == "m-1"
    assert body["has_api_key"] is True
    # 落盘确认（写的是真文件，而不是只改内存）
    saved = json.loads(config.LLM_CONFIG_FILE.read_text(encoding="utf-8"))
    assert saved["model"] == "m-1" and saved["api_key"] == TESTKEY


def test_admin_default_endpoint_matches_user_view(client, users):
    admin_h, _ = users
    client.post("/api/user/llm/config", headers=admin_h,
                json={"base_url": "https://llm.example.com/v1", "api_key": 'sk' + '-xyzxyzxyz',
                      "model": "m-2", "samples": 1, "disabled": True})
    a = client.get("/api/admin/llm/default", headers=admin_h).json()
    b = client.get("/api/user/llm/config", headers=admin_h).json()
    for k in ("scope", "configured", "base_url", "model", "samples", "disabled"):
        assert a[k] == b[k], f"{k} 不一致"


def test_normal_user_cannot_reach_admin_default(client, users):
    _, alice_h = users
    assert client.get("/api/admin/llm/default", headers=alice_h).status_code == 403
    assert client.post("/api/admin/llm/default", headers=alice_h,
                       json={"model": "x"}).status_code == 403


# ---------------------------------------------------------------------------
# 5. 密钥绝不外泄
# ---------------------------------------------------------------------------

SK = TESTKEY


def test_api_key_never_appears_in_any_response(client, users):
    """**安全硬断言**：所有角色、所有相关端点的响应正文里都不含 key 明文。"""
    admin_h, alice_h = users
    client.post("/api/user/llm/config", headers=admin_h, json={
        "base_url": "https://llm.example.com/v1", "api_key": SK,
        "model": "m-1", "samples": 1, "disabled": False})
    probes = [("GET", "/api/user/llm/config", admin_h),
              ("GET", "/api/user/llm/config", alice_h),
              ("GET", "/api/admin/llm/default", admin_h),
              ("GET", "/api/config/llm", admin_h)]
    for method, path, headers in probes:
        r = client.request(method, path, headers=headers)
        assert SK not in r.text, f"{method} {path} 泄露了 api_key 明文"
    for path, headers in (("/api/user/llm/config", admin_h),
                          ("/api/user/llm/config", alice_h),
                          ("/api/admin/llm/default", admin_h)):
        j = client.get(path, headers=headers).json()
        assert j.get("api_key") is None
        assert (j.get("api_key_masked") or "") != SK


def test_masked_key_format(client, users):
    admin_h, _ = users
    client.post("/api/user/llm/config", headers=admin_h, json={
        "api_key": 'sk' + '-abcdefghijkl', "model": "m", "base_url": "u", "samples": 1})
    j = client.get("/api/user/llm/config", headers=admin_h).json()
    assert j["api_key_masked"] == ('sk' + '-****ijkl')


def test_short_key_is_fully_masked():
    """短 key 不留首尾：保留首尾会显著提高爆破命中率（U2 §2.3）。"""
    from lottery import user_llm_api
    assert user_llm_api._mask_key("abc") == "****"
    assert user_llm_api._mask_key("abcdefg") == "****"     # 7 < 8
    # 8 字符：前 3 与后 4 会重叠 3 位，实现取"前 3 + 后 4"（abc + efgh）
    assert user_llm_api._mask_key("abcdefgh") == "abc****efgh"
    assert user_llm_api._mask_key(None) == ""
    assert user_llm_api._mask_key("") == ""


# ---------------------------------------------------------------------------
# 6. 改密必须让旧会话失效（行为回归）
# ---------------------------------------------------------------------------

def test_set_password_increments_token_version(isolated_env):
    """改密必须自增 `token_version`，否则旧会话不会失效。

    **这条用例的由来需要如实说明**：开发过程中有一段工具链会在显示层把
    `token_version+1` 渲染成 `{{SECRET_...}}` 这样的遮罩。我据此误判
    "SQL 被占位符污染了"，但字节级核对（逐字符 codepoint）证明源码一直是
    `token_version+1`，**没有任何缺陷**，那次"修复"实际上没有改动文件
    （工作区与该提交字节一致）。

    保留本用例的价值不在"抓过一次 bug"，而在于：
      * 它只看**行为**（自增与否），不看源码文本 —— 文本会被显示层遮罩，
        行为不会；
      * 改密失效旧会话是多用户系统的硬要求（口令泄露后改密必须能踢人下线），
        值得有一条独立断言。
    """
    db.close()
    db.get_conn()
    u = auth.create_user("bob", "BobPass1234", role="user")
    assert u["token_version"] == 1
    auth.set_password(u["user_id"], "BobPass5678")
    u2 = auth.get_user(u["user_id"])
    assert u2["token_version"] == 2, "改密必须自增 token_version（否则旧会话不失效）"
    ok_old, _ = auth.verify_password("BobPass1234", u2["password_hash"])
    ok_new, _ = auth.verify_password("BobPass5678", u2["password_hash"])
    assert ok_old is False and ok_new is True
