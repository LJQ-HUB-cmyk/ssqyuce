"""多用户身份与权限（MU-1 / W03）。

本模块是**身份侧的唯一实现点**，对应 `docs/reviews/U1_IDENTITY.md` 的冻结契约。
它只做四件事，不掺业务逻辑：

1. **口令哈希**：标准库 `hashlib.scrypt`（零新依赖），存 PHC 风格串，为将来换算法留路。
2. **会话**：会话落 SQLite（`user_sessions` 表），进程重启不掉登录；管理员踢人只需
   改 `revoked_at`。查询走主键 + 进程序内 30s TTL 缓存（见 `SESSION_CACHE_TTL`）。
3. **用户 DAO**：建/查/改/软删，以及"可用用户"判定（`status='active'`）。
4. **审计**：`user_audit_log`（管理员动作 + 登录失败 + 越权拒绝）。

## 三条不可协商的纪律（来自 U1 §4.1 / §7.3，违反即安全漏洞）

1. **`status` 是唯一闸门，且必须先于 `role` 判断**。
   `status='bootstrap'` 的行可能写着 `role='admin'`，但它在设密成功前**零权限**；
   如果先判 role 再判 status，这一行就等于一个"零密码管理员"。
2. **`is_primary_admin` 只能在"设密成功"的同一事务里置 1**。
   绝不允许"先建 bootstrap 行顺手置 1"——那会造出零密码但有主 admin 标记的账号。
3. **口令比较必须常量时间**（`hmac.compare_digest`），且不得回显/记录明文。

## 为什么不引入第三方库

* 口令哈希用标准库 `hashlib.scrypt`：内存硬（32MB/次）、无需 cffi 依赖链、
  也没有 bcrypt 的 72 字节静默截断问题。`requirements.txt` 与 `Dockerfile` 均不改。
* API Key 的**可逆**加密是另一个问题（U2 负责，用 `cryptography` Fernet）——
  密码是单向哈希，两者不能混为一谈（Lead 裁决 6）。

## 768MiB 容器的内存护栏

`n=2^15` 每次哈希占 **32MB**。`/api/auth/login` 是唯一会被匿名高频调用的端点，
而容器 `mem_limit=768m`（常驻已 ~392MB）。若不限并发：
`20 并发 × 32MB = 640MB` → OOMKilled。因此：
① 用 `BoundedSemaphore(4)` 把峰值钉在 ~128MB；
② 登录限流（见 `login_rate_limited`）；
③ `LOTT_SCRYPT_N` 可一键降到 2^14（16MB/次）作为应急旋钮，无需改代码。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from . import config

# ---------------------------------------------------------------------------
# 参数（U1 §3.3；改动需同步文档与测试）
# ---------------------------------------------------------------------------

# scrypt 成本参数。`n` 可用环境变量降级（应急内存旋钮），r/p 固定。
SCRYPT_N: int = int(os.environ.get("LOTT_SCRYPT_N") or 2 ** 15)
SCRYPT_R: int = 8
SCRYPT_P: int = 1
# 必须显式传：不传时 CPython 默认 maxmem=0，n>=2^15 会直接抛 ValueError。
SCRYPT_MAXMEM: int = 64 * 1024 * 1024
_SCRYPT_DKLEN = 64
_SALT_BYTES = 32

# 并发上限：32MB/次 × 4 = 128MB 峰值（见模块 docstring 的内存护栏）
_HASH_MAX_CONCURRENCY = 4
_hash_sem = threading.BoundedSemaphore(_HASH_MAX_CONCURRENCY)

# 会话
SESSION_ID_BYTES = 32          # → base64url 43 字符
CSRF_TOKEN_BYTES = 32
SESSION_TTL_SECONDS = int(os.environ.get("LOTT_SESSION_TTL") or 14 * 24 * 3600)
SESSION_CACHE_TTL = 30.0       # 进程内缓存（U1 §4.2 ② 明确要求是 P0 而非优化项）
SESSION_CACHE_MAX = 512

# 登录失败锁定（暴力破解对策）
MAX_FAILED_BEFORE_LOCK = int(os.environ.get("LOTT_LOGIN_MAX_FAILED") or 5)
LOCK_SECONDS = int(os.environ.get("LOTT_LOGIN_LOCK_SECONDS") or 300)

# 登录限流（按 IP + 按 username 双桶）
LOGIN_RATE_WINDOW = 60.0
LOGIN_RATE_MAX_PER_IP = int(os.environ.get("LOTT_LOGIN_RATE_PER_IP") or 10)
LOGIN_RATE_MAX_PER_USER = int(os.environ.get("LOTT_LOGIN_RATE_PER_USER") or 5)

# 用户名/口令的长度边界（避免超长输入打爆 scrypt 或 DB）
USERNAME_MIN, USERNAME_MAX = 3, 32
PASSWORD_MIN, PASSWORD_MAX = 8, 128


# ---------------------------------------------------------------------------
# 口令哈希
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    """返回 PHC 风格串：``scrypt$n=...,r=...,p=...$<b64 salt>$<b64 dk>``。

    PHC 串把算法标识放在第 1 段，将来换 argon2 时可以"验证时按前缀分派、
    登录成功时顺手重算并升级"，不需要一次性重置所有人密码。
    """
    if not isinstance(password, str) or not password:
        raise ValueError("口令不能为空")
    salt = os.urandom(_SALT_BYTES)
    with _hash_sem:
        dk = hashlib.scrypt(
            password.encode("utf-8"), salt=salt,
            n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
            maxmem=SCRYPT_MAXMEM, dklen=_SCRYPT_DKLEN,
        )
    return "scrypt$n={},r={},p={}${}${}".format(
        SCRYPT_N, SCRYPT_R, SCRYPT_P,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(dk).decode("ascii"),
    )


def verify_password(password: str, stored: Optional[str]) -> Tuple[bool, bool]:
    """校验口令。返回 ``(是否通过, 是否需要重哈希升级)``。

    **永不抛异常**：任何畸形存储串都返回 ``(False, False)``，避免把
    "存储损坏"变成 500（那会泄露内部状态）。也绝不在异常信息里带出 stored。
    """
    if not password or not stored:
        return False, False
    try:
        algo, params, b64salt, b64hash = stored.split("$")
        if algo != "scrypt":
            return False, False
        kv = dict(p.split("=", 1) for p in params.split(","))
        n, r, p = int(kv["n"]), int(kv["r"]), int(kv["p"])
        salt = base64.b64decode(b64salt)
        want = base64.b64decode(b64hash)
        if not want:
            return False, False
        # 用存储串自带的参数校验（不是当前参数），否则改参数后老用户全部登不进
        with _hash_sem:
            got = hashlib.scrypt(
                password.encode("utf-8"), salt=salt,
                n=n, r=r, p=p, maxmem=SCRYPT_MAXMEM, dklen=len(want),
            )
    except (ValueError, KeyError, TypeError):
        return False, False
    ok = hmac.compare_digest(got, want)
    needs_rehash = bool(ok and (n, r, p) != (SCRYPT_N, SCRYPT_R, SCRYPT_P))
    return ok, needs_rehash


def validate_new_password(password: str) -> Optional[str]:
    """返回错误说明（None = 通过）。长度边界是防 DoS 与防弱口令的最低要求。"""
    if not isinstance(password, str):
        return "口令类型非法"
    if len(password) < PASSWORD_MIN:
        return f"口令至少 {PASSWORD_MIN} 位"
    if len(password) > PASSWORD_MAX:
        return f"口令不得超过 {PASSWORD_MAX} 位"
    return None


def validate_username(username: str) -> Optional[str]:
    if not isinstance(username, str):
        return "用户名类型非法"
    if not (USERNAME_MIN <= len(username) <= USERNAME_MAX):
        return f"用户名长度需 {USERNAME_MIN}–{USERNAME_MAX} 位"
    # 只允许字母数字下划线点横线：避免空白/控制字符在日志与 UI 里造成混淆
    allowed = set("abcdefghijklmnopqrstuvwxyz"
                  "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                  "0123456789_-.@")
    if not set(username) <= allowed:
        return "用户名只能包含字母、数字与 _ - . @"
    return None


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------

def new_session_id() -> str:
    """32 字节 CSPRNG → base64url（无填充，43 字符）。"""
    return base64.urlsafe_b64encode(os.urandom(SESSION_ID_BYTES)).decode("ascii").rstrip("=")


def new_csrf_token() -> str:
    return base64.urlsafe_b64encode(os.urandom(CSRF_TOKEN_BYTES)).decode("ascii").rstrip("=")


_SESSION_CACHE: Dict[str, Tuple[float, Optional[Dict[str, Any]]]] = {}
_SESSION_CACHE_LOCK = threading.Lock()


def _cache_get(session_id: str) -> Optional[Optional[Dict[str, Any]]]:
    """返回 None = 未命中；返回 ``None`` 包装的 dict = 命中且用户可用/不可用。"""
    with _SESSION_CACHE_LOCK:
        hit = _SESSION_CACHE.get(session_id)
        if hit is None:
            return None
        ts, payload = hit
        if time.time() - ts > SESSION_CACHE_TTL:
            _SESSION_CACHE.pop(session_id, None)
            return None
        return payload


def _cache_put(session_id: str, payload: Optional[Dict[str, Any]]) -> None:
    with _SESSION_CACHE_LOCK:
        if len(_SESSION_CACHE) >= SESSION_CACHE_MAX:
            # 简单淘汰：清一半（会话量小，够用；避免引入 LRU 依赖）
            for k in list(_SESSION_CACHE)[: SESSION_CACHE_MAX // 2]:
                _SESSION_CACHE.pop(k, None)
        _SESSION_CACHE[session_id] = (time.time(), payload)


def invalidate_session_cache(session_id: Optional[str] = None) -> None:
    """撤销路径必须**主动**清缓存，不能只靠 TTL（U1 §4.2 ③）。"""
    with _SESSION_CACHE_LOCK:
        if session_id is None:
            _SESSION_CACHE.clear()
        else:
            _SESSION_CACHE.pop(session_id, None)


# ---------------------------------------------------------------------------
# 用户 DAO
# ---------------------------------------------------------------------------

_USER_COLUMNS = (
    "user_id", "username", "password_hash", "role", "status", "display_name",
    "is_primary_admin", "failed_count", "locked_until", "last_login_at",
    "token_version", "llm_profile", "external_id", "deleted_at",
    "created_at", "updated_at",
)


def _row_to_user(row) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    return {k: row[k] for k in _USER_COLUMNS}


def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    from . import db
    row = db.get_conn().execute(
        "SELECT * FROM users WHERE username=? AND deleted_at IS NULL", (username,)
    ).fetchone()
    return _row_to_user(row)


def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    from . import db
    row = db.get_conn().execute(
        "SELECT * FROM users WHERE user_id=?", (int(user_id),)
    ).fetchone()
    return _row_to_user(row)


def load_active_user(user_id: int) -> Optional[Dict[str, Any]]:
    """只返回 ``status='active'`` 且未软删的用户。

    **这是中间件的 status 闸门**（U1 §7.3 纪律 2）：bootstrap/locked/disabled
    一律返回 None，由调用方转 403。判定顺序（先 status 后 role）在本函数与
    中间件里共同保证。
    """
    u = get_user(user_id)
    if u is None or u.get("deleted_at"):
        return None
    if u.get("status") != "active":
        return None
    return u


def count_active_admins(exclude_user_id: Optional[int] = None) -> int:
    """可用管理员数。口径固定为 `role='admin' AND status='active'`（U1 §4.1 不变量 3）：
    bootstrap / locked / disabled 的行都不算。
    """
    from . import db
    sql = ("SELECT COUNT(*) FROM users WHERE role='admin' AND status='active' "
           "AND deleted_at IS NULL")
    params: Tuple = ()
    if exclude_user_id is not None:
        sql += " AND user_id<>?"
        params = (int(exclude_user_id),)
    return int(db.get_conn().execute(sql, params).fetchone()[0])


def create_user(username: str, password: Optional[str], *, role: str = "user",
                status: str = "active", display_name: str = "",
                is_primary_admin: int = 0,
                actor_id: Optional[int] = None,
                ip: str = "") -> Dict[str, Any]:
    """建用户。**不在这里做"置主 admin"之外的提权判断**，由调用方保证策略。

    注意（U1 §4.1 不变量 2）：`is_primary_admin=1` 只允许和"设置了口令"同时出现。
    本函数强制该约束——`is_primary_admin=1` 且 `password is None` 直接报错。
    """
    from . import db
    if role not in ("admin", "user"):
        raise ValueError("role 必须是 admin 或 user")
    if status not in ("bootstrap", "active", "disabled", "locked"):
        raise ValueError("status 非法")
    err = validate_username(username)
    if err:
        raise ValueError(err)
    if is_primary_admin and not password:
        raise ValueError("主管理员必须同时设置口令（禁止零密码管理员）")

    pw_hash = None
    if password:
        perr = validate_new_password(password)
        if perr:
            raise ValueError(perr)
        pw_hash = hash_password(password)

    now = time.time()
    conn = db.get_conn()
    cur = conn.execute(
        "INSERT INTO users(username, password_hash, role, status, display_name,"
        " is_primary_admin, token_version, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,1,?,?)",
        (username, pw_hash, role, status, display_name, int(bool(is_primary_admin)),
         now, now),
    )
    conn.commit()
    uid = int(cur.lastrowid)
    audit("user.create", actor_id=actor_id, target_id=uid, target_name=username,
          ip=ip, detail={"role": role, "status": status})
    return get_user(uid) or {}


def set_password(user_id: int, password: str, *, actor_id: Optional[int] = None,
                 action: str = "user.password_change", ip: str = "") -> None:
    """改密：同时 `token_version+=1` 让该用户所有旧会话失效（U1 §4.1）。"""
    from . import db
    perr = validate_new_password(password)
    if perr:
        raise ValueError(perr)
    pw_hash = hash_password(password)
    now = time.time()
    conn = db.get_conn()
    conn.execute(
        "UPDATE users SET password_hash=?, token_version=token_version+1,"
        " failed_count=0, locked_until=0, updated_at=? WHERE user_id=?",
        (pw_hash, now, int(user_id)),
    )
    conn.commit()
    revoke_user_sessions(user_id, reason="password_change")
    audit(action, actor_id=actor_id, target_id=user_id, ip=ip)


def set_status(user_id: int, status: str, *, actor_id: Optional[int] = None,
               ip: str = "") -> None:
    """改状态。停用/锁定必须**立即**让该用户所有会话失效。"""
    from . import db
    if status not in ("bootstrap", "active", "disabled", "locked"):
        raise ValueError("status 非法")
    conn = db.get_conn()
    conn.execute("UPDATE users SET status=?, updated_at=? WHERE user_id=?",
                 (status, time.time(), int(user_id)))
    conn.commit()
    if status != "active":
        revoke_user_sessions(user_id, reason=f"status_{status}")
        invalidate_session_cache()
    audit("user.disable" if status != "active" else "user.enable",
          actor_id=actor_id, target_id=user_id, ip=ip, detail={"status": status})


def soft_delete_user(user_id: int, *, actor_id: Optional[int] = None, ip: str = "") -> None:
    """软删除：`deleted_at` 置位 + 用户名改写归档，释放原名（U1 §4.1 推荐做法）。

    为什么改名：表内 `username` 有 UNIQUE 约束，若不改名则"删除后重建同名用户"
    永远失败。改写为 ``name#deleted#<user_id>`` 既守约定又释放原名。
    """
    from . import db
    u = get_user(user_id)
    if u is None:
        raise ValueError("用户不存在")
    now = time.time()
    archived = f"{u['username']}#deleted#{int(user_id)}"
    conn = db.get_conn()
    conn.execute(
        "UPDATE users SET deleted_at=?, status='disabled', username=?, updated_at=?"
        " WHERE user_id=?",
        (now, archived, now, int(user_id)),
    )
    conn.commit()
    revoke_user_sessions(user_id, reason="deleted")
    invalidate_session_cache()
    audit("user.delete", actor_id=actor_id, target_id=user_id,
          target_name=archived, ip=ip)


# ---------------------------------------------------------------------------
# 会话 DAO
# ---------------------------------------------------------------------------

def create_session(user_id: int, *, ip: str = "", user_agent: str = "") -> Dict[str, str]:
    """建会话，返回 ``{"session_id", "csrf_token", "expires_at"}``。

    **只存 session_id 本身**（不是它的哈希）——因为它是 32 字节 CSPRNG，
    已足够不可猜；而落库的哈希会让"踢下线"仍需逐条比对，收益不抵复杂度。
    """
    from . import db
    sid = new_session_id()
    csrf = new_csrf_token()
    now = time.time()
    expires = now + SESSION_TTL_SECONDS
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO user_sessions(session_id, user_id, created_at, expires_at,"
        " last_seen_at, ip, user_agent, csrf_token) VALUES(?,?,?,?,?,?,?,?)",
        (sid, int(user_id), now, expires, now, ip[:128], user_agent[:256], csrf),
    )
    conn.commit()
    return {"session_id": sid, "csrf_token": csrf, "expires_at": expires}


def resolve_session(session_id: str) -> Optional[Dict[str, Any]]:
    """解析会话 → ``{"user": {...}, "session": {...}}``；无效返回 None。

    无效的情形全部返回 None（不区分原因，避免给攻击者枚举信号）：
    会话不存在 / 已撤销 / 已过期 / 用户已软删 / 用户 status != 'active' /
    token_version 与建会话时不一致（改密后旧会话失效）。
    """
    if not session_id:
        return None
    cached = _cache_get(session_id)
    if cached is not None:
        return cached or None

    from . import db
    conn = db.get_conn()
    row = conn.execute(
        "SELECT * FROM user_sessions WHERE session_id=?", (session_id,)
    ).fetchone()
    if row is None:
        _cache_put(session_id, None)
        return None
    if row["revoked_at"]:
        _cache_put(session_id, None)
        return None
    now = time.time()
    if float(row["expires_at"]) <= now:
        _cache_put(session_id, None)
        return None

    user = load_active_user(int(row["user_id"]))
    if user is None:
        # status 闸门：bootstrap/locked/disabled/软删 一律不可用
        _cache_put(session_id, None)
        return None

    payload = {
        "user": user,
        "session": {
            "session_id": session_id,
            "user_id": int(row["user_id"]),
            "csrf_token": row["csrf_token"] or "",
            "expires_at": float(row["expires_at"]),
        },
    }
    _cache_put(session_id, payload)
    # last_seen 只在缓存未命中时更新，避免每请求一次写（写会抢锁）
    try:
        conn.execute("UPDATE user_sessions SET last_seen_at=? WHERE session_id=?",
                     (now, session_id))
        conn.commit()
    except Exception:  # noqa: BLE001 —— 审计字段写失败不应阻断请求
        pass
    return payload


def revoke_session(session_id: str, *, actor_id: Optional[int] = None) -> None:
    from . import db
    conn = db.get_conn()
    conn.execute("UPDATE user_sessions SET revoked_at=? WHERE session_id=? AND revoked_at IS NULL",
                 (time.time(), session_id))
    conn.commit()
    invalidate_session_cache(session_id)
    audit("session.revoke", actor_id=actor_id, detail={"session_id": session_id[:8]})


def revoke_user_sessions(user_id: int, *, reason: str = "",
                         actor_id: Optional[int] = None) -> int:
    """撤销某用户全部会话，返回撤销条数。"""
    from . import db
    conn = db.get_conn()
    cur = conn.execute(
        "UPDATE user_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
        (time.time(), int(user_id)),
    )
    conn.commit()
    invalidate_session_cache()
    n = cur.rowcount or 0
    if n:
        audit("session.revoke", actor_id=actor_id, target_id=user_id,
              detail={"count": n, "reason": reason})
    return n


def require_csrf(session: Dict[str, Any], token: Optional[str]) -> bool:
    """双提交校验：请求头 `X-CSRF-Token` 必须等于会话里的 csrf_token。"""
    want = (session or {}).get("csrf_token") or ""
    if not want or not token:
        return False
    return hmac.compare_digest(str(token), str(want))


# ---------------------------------------------------------------------------
# 登录限流 / 失败锁定
# ---------------------------------------------------------------------------

_RATE_LOCK = threading.Lock()
_RATE: Dict[str, List[float]] = {}


def _rate_hit(key: str, limit: int, window: float = LOGIN_RATE_WINDOW) -> bool:
    """返回 True = 超限。滑动窗口，进程内即可（单容器部署）。"""
    now = time.time()
    with _RATE_LOCK:
        bucket = [t for t in _RATE.get(key, []) if now - t < window]
        if len(bucket) >= limit:
            _RATE[key] = bucket
            return True
        bucket.append(now)
        _RATE[key] = bucket
        return False


def login_rate_limited(ip: str, username: str) -> Optional[str]:
    """双桶限流：按 IP 与按用户名各一个桶（防撞库与防针对某账号爆破）。"""
    if _rate_hit(f"ip:{ip}", LOGIN_RATE_MAX_PER_IP):
        return "登录过于频繁，请稍后再试"
    if _rate_hit(f"user:{username}", LOGIN_RATE_MAX_PER_USER):
        return "该账号登录尝试过多，请稍后再试"
    return None


def reset_rate_limits() -> None:
    """测试用：清空限流桶。"""
    with _RATE_LOCK:
        _RATE.clear()


def is_locked(user: Dict[str, Any]) -> bool:
    return float(user.get("locked_until") or 0) > time.time()


def record_login_failure(user_id: int) -> None:
    """失败计数 + 达到阈值则锁定（`locked_until`）。"""
    from . import db
    conn = db.get_conn()
    row = conn.execute("SELECT failed_count FROM users WHERE user_id=?",
                       (int(user_id),)).fetchone()
    n = int(row[0] if row else 0) + 1
    locked_until = time.time() + LOCK_SECONDS if n >= MAX_FAILED_BEFORE_LOCK else 0
    conn.execute(
        "UPDATE users SET failed_count=?, locked_until=?, status=?"
        " WHERE user_id=? AND status='active'",
        (n, locked_until, "locked" if locked_until else "active", int(user_id)),
    )
    conn.commit()
    if locked_until:
        revoke_user_sessions(user_id, reason="locked")


def record_login_success(user_id: int) -> None:
    from . import db
    conn = db.get_conn()
    conn.execute(
        "UPDATE users SET failed_count=0, locked_until=0, last_login_at=?, updated_at=?"
        " WHERE user_id=?",
        (time.time(), time.time(), int(user_id)),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------

def audit(action: str, *, actor_id: Optional[int] = None, actor_name: str = "",
          target_id: Optional[int] = None, target_name: str = "",
          result: str = "ok", ip: str = "",
          detail: Optional[Dict[str, Any]] = None) -> None:
    """写一条审计。

    **严禁把 password / api_key 放进 detail**（U1 §4.3）。这里做一次兜底脱敏：
    键名命中黑名单的值一律替换为 ``***``，防止调用方疏忽。
    """
    try:
        from . import db
        safe = _redact_detail(detail or {})
        db.get_conn().execute(
            "INSERT INTO user_audit_log(ts, actor_id, actor_name, action, target_id,"
            " target_name, result, ip, detail_json) VALUES(?,?,?,?,?,?,?,?,?)",
            (time.time(), actor_id, actor_name or "", action, target_id,
             target_name or "", result, ip or "", json.dumps(safe, ensure_ascii=False)),
        )
        db.get_conn().commit()
    except Exception:  # noqa: BLE001 —— 审计失败绝不能阻断业务/登录
        pass


_SECRET_KEYS = ("password", "passwd", "pwd", "api_key", "apikey", "secret",
                "token", "authorization", "cookie")


def _redact_detail(detail: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in (detail or {}).items():
        if any(s in str(k).lower() for s in _SECRET_KEYS):
            out[k] = "***"
        elif isinstance(v, dict):
            out[k] = _redact_detail(v)
        else:
            out[k] = v
    return out


def claim_bootstrap_token(provided: str) -> bool:
    """校验一次性引导口令（U1 §5.2）。

    口令来自 ``LOTT_BOOTSTRAP_TOKEN``。**没有默认值**——未配置时引导通道整体关闭
    （返回 False），避免出现"默认口令"这种能被告警扫到的做法。
    用常量时间比较；成功一次后调用方必须立刻清空该 env（见 `consume_bootstrap_token`）。
    """
    expected = (os.environ.get("LOTT_BOOTSTRAP_TOKEN") or "").strip()
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided, expected)


def consume_bootstrap_token() -> None:
    """引导成功后删除 env 里的口令，保证"一次性"。

    注意：env 是进程级的，删除只影响本进程；容器重启会从 compose 重新注入，
    因此**部署侧也必须把 `LOTT_BOOTSTRAP_TOKEN` 从 .env 移除**（文档要求）。
    """
    os.environ.pop("LOTT_BOOTSTRAP_TOKEN", None)
