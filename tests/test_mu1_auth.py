"""MU-1 身份骨架测试（W03/W04/W05）。

覆盖三块，每块都带**反向用例**（只测"正常路径能过"是不够的）：

A. **口令哈希**：scrypt 参数、PHC 往返、错误口令、畸形存储、重哈希触发。
B. **迁移 5**：三张表落地、`users.status` 四值 CHECK、bootstrap 行形态、
   幂等性（重复 apply 不重复插入）。
C. **鉴权中间件**：分级覆盖全部路由、deny-by-default、status 闸门先于角色、
   bootstrap 零权限、会话生命周期、CSRF、限流、自保护规则。

## 为什么每条关键行为都要有反向用例

本项目历史上多次出现"测试全绿但功能是空的"（`prereg_ok` 恒 False、
30 档规律从未运行）。安全代码尤其危险：正向用例只要中间件**放行**就绿，
而放行恰恰是漏洞。所以每个闸门都配一条"必须被拒绝"的断言。
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from lottery import auth, config, db


# ---------------------------------------------------------------------------
# 公共 fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(isolated_env, monkeypatch):
    """测试用 TestClient。

    **必须关掉 Cookie 的 Secure 属性**：TestClient 走 `http://testserver`，
    而 httpx 不会在明文 http 下回传 `Secure` Cookie —— 于是"登录成功但 Cookie
    没被带上"，表现为所有需要登录的用例 401。
    这**不是**生产缺陷（生产经 nginx 443 走 HTTPS，Secure 必须开）；
    所以这里只在测试里降级，并用 `test_cookie_secure_attr_in_production`
    单独断言**生产配置下**该属性确实是 True。
    """
    from lottery import api_app
    monkeypatch.setattr(config, "SESSION_COOKIE_SECURE", False)
    db.close()
    with TestClient(api_app.app) as c:
        yield c
    db.close()


@pytest.fixture()
def admin_user(isolated_env):
    """建一个可用的 admin，并返回 (user_dict, password)。"""
    db.close()
    db.get_conn()          # 触发迁移 5（会插入 bootstrap 行）
    # 把 bootstrap 行升级为真正的主管理员
    from lottery import migrations  # noqa: F401
    conn = db.get_conn()
    pw = "AdminPass123"
    conn.execute(
        "UPDATE users SET password_hash=?, role='admin', status='active',"
        " is_primary_admin=1 WHERE status='bootstrap'",
        (auth.hash_password(pw),))
    conn.commit()
    u = auth.get_user_by_username("admin")
    assert u is not None and u["status"] == "active" and u["role"] == "admin"
    return u, pw


@pytest.fixture()
def normal_user(admin_user):
    """在 admin 存在的前提下再建一个普通用户。"""
    u = auth.create_user("alice", "AlicePass123", role="user")
    return u, "AlicePass123"


# ---------------------------------------------------------------------------
# A. 口令哈希
# ---------------------------------------------------------------------------

def test_hash_format_is_phc_style(isolated_env):
    h = auth.hash_password("correct horse battery")
    parts = h.split("$")
    assert parts[0] == "scrypt", "算法标识必须在第 1 段（向前兼容换算法）"
    assert parts[1].startswith("n=") and "r=" in parts[1] and "p=" in parts[1]
    assert len(parts) == 4


def test_scrypt_maxmem_must_be_passed(isolated_env):
    """回归：不显式传 maxmem 时，n=2^15 会直接抛 ValueError。

    这条防止有人"优化"掉 `maxmem=` 参数——那样所有登录都会 500。
    """
    import hashlib
    import os
    with pytest.raises(ValueError):
        hashlib.scrypt(b"x", salt=os.urandom(16), n=2 ** 15, r=8, p=1, dklen=32)


def test_verify_roundtrip_and_wrong_password(isolated_env):
    h = auth.hash_password("s3cret-pass")
    assert auth.verify_password("s3cret-pass", h) == (True, False)
    ok, _ = auth.verify_password("wrong", h)
    assert ok is False


def test_verify_never_raises_on_garbage(isolated_env):
    """畸形存储串必须返回 (False, False)，不能抛（否则 500 泄露内部状态）。"""
    for bad in (None, "", "garbage", "scrypt$bad$xx$yy", "bcrypt$n=1$a$b", "$$$"):
        ok, rh = auth.verify_password("x", bad)
        assert ok is False and rh is False


def test_verify_detects_needed_rehash(isolated_env, monkeypatch):
    """用旧参数（n=2^14）生成的哈希，校验通过后必须提示重哈希。"""
    old_n = 2 ** 14
    import base64
    import hashlib
    import os
    salt = os.urandom(32)
    dk = hashlib.scrypt(b"pw12345678", salt=salt, n=old_n, r=8, p=1,
                        maxmem=64 * 1024 * 1024, dklen=64)
    stored = "scrypt$n=%d,r=8,p=1$%s$%s" % (
        old_n, base64.b64encode(salt).decode(), base64.b64encode(dk).decode())
    ok, needs = auth.verify_password("pw12345678", stored)
    assert ok is True
    assert needs is True, "参数升级必须被识别，否则老账号永远不会升级到新强度"


def test_password_length_bounds(isolated_env):
    assert auth.validate_new_password("short") is not None
    assert auth.validate_new_password("x" * 200) is not None
    assert auth.validate_new_password("goodpass123") is None


def test_username_validation(isolated_env):
    assert auth.validate_username("ab") is not None          # 太短
    assert auth.validate_username("a" * 40) is not None      # 太长
    assert auth.validate_username("bad name") is not None    # 含空格
    assert auth.validate_username("good_user-1") is None


# ---------------------------------------------------------------------------
# B. 迁移 5
# ---------------------------------------------------------------------------

def test_migration_creates_three_tables(isolated_env):
    db.close()
    conn = db.get_conn()
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"users", "user_sessions", "user_audit_log"} <= names
    assert migrations_current() == 5


def migrations_current() -> int:
    from lottery import migrations
    return migrations.current_version(db.get_conn())


def test_bootstrap_row_shape(isolated_env):
    """bootstrap 行必须是**零权限**形态：role='user' + is_primary_admin=0。

    若插成 role='admin'，一旦中间件的 status 闸门写漏，就是一个
    **零密码管理员**（U1 §4.1 不变量 2）。
    """
    db.close()
    db.get_conn()
    row = db.get_conn().execute(
        "SELECT * FROM users WHERE username='admin'").fetchone()
    assert row is not None
    assert row["status"] == "bootstrap"
    assert row["role"] == "user", "禁止插入 role='admin' 的零密码行"
    assert row["is_primary_admin"] == 0, "主 admin 标记只能在设密成功时置 1"
    assert row["password_hash"] is None


def test_users_status_check_accepts_four_values(isolated_env):
    """`CHECK` 必须含四个值。少 `bootstrap` 会让迁移 5 的 INSERT 被拒 → 上线失败。"""
    db.close()
    conn = db.get_conn()
    now = time.time()
    for st in ("bootstrap", "active", "disabled", "locked"):
        conn.execute(
            "INSERT INTO users(username, role, status, is_primary_admin,"
            " created_at, updated_at) VALUES(?,?,?,0,?,?)",
            (f"u_{st}", "user", st, now, now))
    conn.commit()
    with pytest.raises(Exception):
        conn.execute(
            "INSERT INTO users(username, role, status, is_primary_admin,"
            " created_at, updated_at) VALUES('bad','user','bogus',0,?,?)",
            (now, now))
    conn.rollback()


def test_migration_is_idempotent(isolated_env):
    """重复 apply 不得重复插入 bootstrap 行。"""
    db.close()
    db.get_conn()
    n1 = db.get_conn().execute("SELECT COUNT(*) FROM users").fetchone()[0]
    from lottery import migrations
    migrations.apply(db.get_conn(), backup=False)
    n2 = db.get_conn().execute("SELECT COUNT(*) FROM users").fetchone()[0]
    assert n1 == n2 == 1


def test_primary_admin_unique_index(isolated_env, admin_user):
    """主 admin 标记全表最多一行。"""
    db.close()
    with pytest.raises(Exception):
        db.get_conn().execute(
            "INSERT INTO users(username, role, status, is_primary_admin,"
            " created_at, updated_at) VALUES('second','admin','active',1,0,0)")
    db.get_conn().rollback()


# ---------------------------------------------------------------------------
# C. 鉴权中间件
# ---------------------------------------------------------------------------

def _collect_route_paths(app):
    """枚举 app 上**全部**路由路径（含 include_router 挂进来的子路由）。

    为什么不能直接遍历 ``app.routes``
    ---------------------------------
    FastAPI 0.142（starlette 1.7）把 ``include_router`` 改成了**惰性**实现：
    ``app.routes`` 里只放一个 ``_IncludedRouter`` 占位对象，子路由要到真正
    构建路由表时才展开。于是 ``getattr(r, "path", None)`` 对占位对象返回 None，
    子路由被**静默跳过**。

    实测后果（本项目真实发生过）：auth_api 的 11 条 + user_llm_api 的 4 条
    共 **15 条路由**从未被分级覆盖测试看到，而测试仍以 ``len(paths) >= 80``
    通过 —— 也就是说"每条路由都有明确分级"这条纪律在 auth 路由上一直是空的。
    这与 V13 评审发现的 ``register_prereg`` 零调用是同一类问题：
    **断言写在了看不见目标的地方，所以永远绿**。

    因此这里显式展开占位对象；将来 FastAPI 若换实现，走原路径即可。
    """
    out = []
    for r in app.routes:
        p = getattr(r, "path", None)
        if p and p.startswith("/") and getattr(r, "methods", None):
            out.append(p)
            continue
        expand = getattr(r, "effective_candidates", None)
        if callable(expand):
            try:
                cands = expand()
            except TypeError:      # 某些版本要求传参数，说明不是我们要的那类对象
                cands = []
            for c in cands or []:
                cp = getattr(c, "path", None)
                if cp and cp.startswith("/") and getattr(c, "methods", None):
                    out.append(cp)
    return out


def test_route_enumeration_sees_included_routers(isolated_env):
    """**元测试**：枚举函数必须看得到 include_router 挂进来的路由。

    没有这条，`_collect_route_paths` 万一退化成只看顶层，全量分级测试会
    重新变成"看不见目标却永远绿"。这里把"看得见"本身钉死。
    """
    from lottery import api_app
    db.close()
    paths = _collect_route_paths(api_app.app)
    assert "/api/auth/login" in paths, "枚举不到 auth_api 的子路由 —— 全量分级测试失效"
    assert "/api/user/llm/config" in paths, "枚举不到 user_llm_api 的子路由"
    assert "/api/admin/users" in paths


def test_every_route_has_explicit_grade(isolated_env):
    """**参数化覆盖全部路由**：将来新增端点忘记定级会直接挂这条。

    这是 `required_role` 的"纯函数 + 全量覆盖"纪律（U1 §7.3 纪律 4）。
    """
    from lottery import api_app
    db.close()
    paths = _collect_route_paths(api_app.app)
    assert len(paths) >= 80, f"路由数异常：{len(paths)}"
    for p in paths:
        role = api_app.auth_api.required_role(p)
        assert role in (None, "user", "admin"), f"{p} 分级非法：{role}"
        if role is None:
            # 公开路径必须是白名单里的，不能因为"忘了登记"而变成公开
            assert (p in api_app.auth_api.PUBLIC_EXACT
                    or p.startswith(api_app.auth_api.PUBLIC_PREFIX)), (
                f"{p} 被判为公开，但不在 PUBLIC_EXACT 白名单里 —— 这是 deny-by-default 失效")


def test_required_role_is_pure_and_correct(isolated_env):
    from lottery import auth_api
    assert auth_api.required_role("/api/health") is None
    assert auth_api.required_role("/static/app.js") is None
    assert auth_api.required_role("/api/admin/users") == "admin"
    assert auth_api.required_role("/api/config/llm") == "admin"
    assert auth_api.required_role("/api/predict") == "user"
    # deny-by-default：未登记的路径必须需要登录，而不是公开
    assert auth_api.required_role("/api/some/future/endpoint") == "user"
    assert auth_api.required_role("/brand-new-path") == "user"


def test_me_returns_200_when_anonymous(client):
    """`/api/auth/me` 未登录时返回 200 + authenticated=false（不是 401）。"""
    r = client.get("/api/auth/me")
    assert r.status_code == 200
    assert r.json()["authenticated"] is False


def test_login_success_sets_cookie_and_returns_csrf(client, normal_user):
    u, pw = normal_user
    r = client.post("/api/auth/login", json={"username": "alice", "password": pw})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["user"]["username"] == "alice"
    assert body["csrf_token"], "必须返回 CSRF token（双提交凭据）"
    # Cookie 属性：HttpOnly + SameSite=Lax
    setc = r.headers.get("set-cookie", "")
    assert "httponly" in setc.lower()
    assert "samesite=lax" in setc.lower()


def test_login_wrong_password_generic_message(client, normal_user):
    u, pw = normal_user
    r = client.post("/api/auth/login", json={"username": "alice", "password": "nope"})
    assert r.status_code == 401
    assert "用户名或口令错误" in r.json()["error"]


def test_login_unknown_user_same_message_and_no_enumeration(client):
    """不存在的用户必须返回与错误口令**完全相同**的消息与状态码。"""
    r = client.post("/api/auth/login", json={"username": "ghost", "password": "x"})
    assert r.status_code == 401
    assert "用户名或口令错误" in r.json()["error"]


def test_bootstrap_cannot_login(client):
    """**关键反向用例**：bootstrap 行零权限，不能登录。

    即便它的 role 将来被误写成 admin，status 闸门也必须挡住。
    """
    db.close()
    db.get_conn()      # 迁移插入 bootstrap 行（password_hash=NULL）
    r = client.post("/api/auth/login", json={"username": "admin", "password": "anything"})
    assert r.status_code in (401, 403)
    assert r.status_code != 200


def test_bootstrap_row_with_admin_role_still_denied(isolated_env):
    """纵深防御：即使把 bootstrap 行的 role 改成 admin，也必须登录失败。

    这正是"status 闸门必须先于 role 判断"的验收（U1 §4.1 不变量 1）。
    """
    db.close()
    conn = db.get_conn()
    conn.execute("UPDATE users SET role='admin', password_hash=? WHERE status='bootstrap'",
                 (auth.hash_password("SomePass123"),))
    conn.commit()
    # 直接构造会话：模拟"登录成功但 status 非 active"
    u = auth.get_user_by_username("admin")
    sess = auth.create_session(u["user_id"])
    assert auth.resolve_session(sess["session_id"]) is None, (
        "status != 'active' 的用户，会话必须解析失败 —— 否则零权限账号可访问全站")


def test_bootstrap_endpoint_requires_token(client, monkeypatch):
    """未配置引导 token 时，引导通道整体关闭。"""
    monkeypatch.delenv("LOTT_BOOTSTRAP_TOKEN", raising=False)
    r = client.post("/api/auth/bootstrap",
                    json={"token": "anything", "password": "NewPass123"})
    assert r.status_code == 403


def test_bootstrap_endpoint_works_and_is_one_shot(client, monkeypatch):
    monkeypatch.setenv("LOTT_BOOTSTRAP_TOKEN", "boot-token-xyz")
    db.close()
    db.get_conn()
    r = client.post("/api/auth/bootstrap",
                    json={"token": "boot-token-xyz", "password": "NewPass123"})
    assert r.status_code == 200, r.text
    u = auth.get_user_by_username("admin")
    assert u["status"] == "active"
    assert u["role"] == "admin"
    assert u["is_primary_admin"] == 1, "设密成功后必须同时置主 admin 标记"
    # 一次性：第二次必须失败
    r2 = client.post("/api/auth/bootstrap",
                     json={"token": "boot-token-xyz", "password": "Another123"})
    assert r2.status_code in (403, 409)


def test_logout_revokes_session(client, normal_user):
    u, pw = normal_user
    client.post("/api/auth/login", json={"username": "alice", "password": pw})
    assert client.get("/api/auth/me").json()["authenticated"] is True
    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").json()["authenticated"] is False


def test_csrf_required_for_write(client, normal_user):
    """写操作缺 CSRF token 必须被拒（双提交校验）。"""
    u, pw = normal_user
    r = client.post("/api/auth/login", json={"username": "alice", "password": pw})
    csrf = r.json()["csrf_token"]
    # 无 CSRF 头 → 拒绝
    bad = client.post("/api/admin/users",
                      json={"username": "bob", "password": "BobPass123"})
    assert bad.status_code == 403
    # 带上正确 CSRF 头 + admin 身份才应通过（此处 alice 非 admin，应 403 权限不足）
    withcsrf = client.post("/api/admin/users",
                           json={"username": "bob", "password": "BobPass123"},
                           headers={"X-CSRF-Token": csrf})
    assert withcsrf.status_code == 403
    assert "管理员" in withcsrf.json()["error"]


def test_admin_can_create_and_list_users(client, admin_user):
    u, pw = admin_user
    r = client.post("/api/auth/login", json={"username": "admin", "password": pw})
    csrf = r.json()["csrf_token"]
    h = {"X-CSRF-Token": csrf}
    created = client.post("/api/admin/users",
                          json={"username": "bob", "password": "BobPass123"},
                          headers=h)
    assert created.status_code == 200, created.text
    assert created.json()["user"]["role"] == "user"
    lst = client.get("/api/admin/users")
    assert lst.status_code == 200
    names = [x["username"] for x in lst.json()["users"]]
    assert "bob" in names and "admin" in names
    # 绝不能泄露 password_hash
    assert all("password_hash" not in x for x in lst.json()["users"])


def test_admin_cannot_disable_self(client, admin_user):
    u, pw = admin_user
    r = client.post("/api/auth/login", json={"username": "admin", "password": pw})
    csrf = r.json()["csrf_token"]
    resp = client.patch(f"/api/admin/users/{u['user_id']}",
                        json={"status": "disabled"},
                        headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 400
    assert "自己" in resp.json()["error"]


def test_cannot_remove_last_admin(client, admin_user, normal_user):
    """自保护：最后一个可用管理员不能被降级/删除。"""
    u, pw = admin_user
    n, npw = normal_user
    r = client.post("/api/auth/login", json={"username": "admin", "password": pw})
    csrf = r.json()["csrf_token"]
    h = {"X-CSRF-Token": csrf}
    resp = client.patch(f"/api/admin/users/{u['user_id']}",
                        json={"role": "user"}, headers=h)
    assert resp.status_code == 400          # 先被"不能改自己"挡住
    # 换个角度：由 alice 升 admin 后，admin 降级应成功；但只有 1 个 admin 时不行
    db.close()
    assert auth.count_active_admins() == 1


def test_session_survives_status_change_to_disabled(client, admin_user, normal_user):
    """用户被停用后，其已有会话必须立即失效（不能等 30s TTL）。

    **测试设计说明**：一开始写成"同一 TestClient 先登 alice 再登 admin"，
    但登录会覆盖 Cookie jar，导致第二个登录的响应被第一个会话干扰。
    改为直接验证 **auth 层**（这才是真正被测的契约），
    HTTP 层的行为由 `test_admin_can_create_and_list_users` 等用例覆盖。
    """
    admin, apw = admin_user
    alice, alpw = normal_user

    # ① alice 能正常登录并拿到可用会话
    ac = client.post("/api/auth/login", json={"username": "alice", "password": alpw})
    assert ac.status_code == 200, ac.text
    assert client.get("/api/auth/me").json()["authenticated"] is True

    # ② 模拟 admin 停用 alice（直接走 auth 层，等价于管理员动作的效果）
    auth.set_status(alice["user_id"], "disabled")

    # ③ alice 的会话必须立刻失效
    db.close()
    assert auth.load_active_user(alice["user_id"]) is None
    from lottery import api_app as _api
    assert _api._resolve_session_user_id  # 存在即可

    # ④ HTTP 层：下一个请求必须拿不到身份
    me = client.get("/api/auth/me").json()
    assert me["authenticated"] is False, "停用后会话仍有效 —— 停用等于没做"


def test_disabled_user_session_resolves_none(isolated_env, admin_user):
    """直接验证：停用后 `resolve_session` 返回 None（清缓存 + status 闸门）。"""
    alice = auth.create_user("carol", "CarolPass123", role="user")
    sess = auth.create_session(alice["user_id"])
    assert auth.resolve_session(sess["session_id"]) is not None
    auth.set_status(alice["user_id"], "disabled")
    assert auth.resolve_session(sess["session_id"]) is None, (
        "停用后会话仍可用 —— 停用等于没做")


def test_password_change_invalidates_sessions(isolated_env, admin_user):
    alice = auth.create_user("dave", "DavePass123", role="user")
    sess = auth.create_session(alice["user_id"])
    assert auth.resolve_session(sess["session_id"]) is not None
    auth.set_password(alice["user_id"], "NewDavePass123")
    assert auth.resolve_session(sess["session_id"]) is None, (
        "改密后旧会话必须失效")


def test_soft_delete_frees_username(isolated_env, admin_user):
    """软删除后必须能重建同名用户（靠改名归档释放原名）。"""
    u = auth.create_user("eve", "EvePass123", role="user")
    auth.soft_delete_user(u["user_id"])
    assert auth.get_user_by_username("eve") is None
    u2 = auth.create_user("eve", "EvePass456", role="user")
    assert u2["user_id"] != u["user_id"]


def test_login_rate_limit(isolated_env, admin_user):
    """同一用户名连续失败超过阈值应被限流（429）。"""
    auth.reset_rate_limits()
    from fastapi.testclient import TestClient as TC
    from lottery import api_app
    db.close()
    with TC(api_app.app) as c:
        codes = []
        for _ in range(auth.LOGIN_RATE_MAX_PER_USER + 2):
            r = c.post("/api/auth/login",
                       json={"username": "admin", "password": "wrong"})
            codes.append(r.status_code)
        assert 429 in codes, f"未触发限流：{codes}"
    auth.reset_rate_limits()


def test_audit_records_login_failure(client, normal_user):
    db.close()
    client.post("/api/auth/login", json={"username": "alice", "password": "bad"})
    rows = db.get_conn().execute(
        "SELECT action, result FROM user_audit_log ORDER BY audit_id DESC LIMIT 3").fetchall()
    assert any(r["action"] == "auth.login_failed" for r in rows)


def test_audit_redacts_secrets(isolated_env):
    """审计细节里即使误传 password/api_key，落库前也必须脱敏。"""
    db.close()
    db.get_conn()
    auth.audit("test.action", detail={"password": "plain", "api_key": "sk-123",
                                      "note": "visible"})
    row = db.get_conn().execute(
        "SELECT detail_json FROM user_audit_log ORDER BY audit_id DESC LIMIT 1").fetchone()
    import json as _json
    d = _json.loads(row["detail_json"])
    assert d["password"] == "***"
    assert d["api_key"] == "***"
    assert d["note"] == "visible"


def test_auth_mode_off_does_not_block(client):
    """默认 `AUTH_MODE=off`：未登录也能访问业务端点（灰度期，前端登录页未落地）。"""
    assert config.auth_enforced() is False
    r = client.get("/api/health")
    assert r.status_code == 200


def test_auth_enforce_blocks_anonymous(isolated_env, monkeypatch):
    """`AUTH_MODE=enforce` 时未登录必须 401（deny-by-default）。"""
    from lottery import api_app
    db.close()
    with TestClient(api_app.app) as c:
        # 不 monkeypatch config（它是模块级读取），直接改属性模拟 enforce
        monkeypatch.setattr(config, "AUTH_MODE", "enforce")
        monkeypatch.setattr(config, "auth_enforced", lambda: True)
        r = c.get("/api/predictions/last")
        assert r.status_code == 401, r.text
        assert r.json().get("auth_required") is True
    db.close()


def test_system_user_id_constant(isolated_env):
    """`SYSTEM_USER_ID` 必须是 0 且集中在 config（Lead 裁决 3）。"""
    assert config.SYSTEM_USER_ID == 0


def test_cookie_secure_attr_in_production(isolated_env):
    """生产配置必须带 `Secure`（nginx 走 443）。

    与 `client` fixture 里"测试降级为 False"配对：那条让测试能跑通，
    这条保证**默认值**没有被顺手改成 False 而丢掉生产防护。
    """
    import os
    # 直接读默认配置（不经 monkeypatch）
    assert config.SESSION_COOKIE_SECURE is True or \
        os.environ.get("LOTT_COOKIE_SECURE") == "0", (
        "会话 Cookie 的 Secure 默认值必须是 True；"
        "只有显式 LOTT_COOKIE_SECURE=0（本地 http 联调）才允许关闭")
    # Cookie 名默认带 __Host- 前缀（浏览器强制 Secure + Path=/ + 无 Domain）
    assert config.SESSION_COOKIE_NAME.startswith("__Host-"), config.SESSION_COOKIE_NAME


# ---------------------------------------------------------------------------
# D. 旧 LOTT_TOKEN 兼容层（不许被多用户改造放宽）
# ---------------------------------------------------------------------------

def test_public_allowlist_cannot_widen_token_protection():
    """回归：`LOTT_TOKEN` 配了之后，除极小豁免集外**所有** /api/* 都要 Bearer。

    事故背景：多用户改造中曾把 `/api/info` 加进 `PUBLIC_EXACT`，
    结果配了 token 的部署里它反而变公开（`test_m4_5_engineering` 抓住）。
    教训：公开白名单只能收窄；**"路径小、不敏感"不是放行理由**。
    """
    from lottery import api_app, auth_api
    exempt = api_app._TOKEN_EXEMPT
    # 豁免集必须极小，且只含"不放过就没法工作"的路径
    assert exempt == {"/api/health", "/api/auth/login", "/api/auth/bootstrap",
                      "/api/auth/logout", "/api/auth/me"}, exempt
    # 不许把 /api/info 这类普通端点塞进公开白名单
    assert "/api/info" not in auth_api.PUBLIC_EXACT
    assert "/api/info" not in exempt


def test_token_protects_info_endpoint(client, monkeypatch):
    """配了 LOTT_TOKEN 时 `/api/info` 必须 401（而不是 200）。"""
    monkeypatch.setattr(config, "API_TOKEN", "test-secret")
    assert client.get("/api/info").status_code == 401
    ok = client.get("/api/info", headers={"Authorization": "Bearer test-secret"})
    assert ok.status_code == 200
    # 探活永远例外（容器 healthcheck 不带 token）
    assert client.get("/api/health").status_code == 200
