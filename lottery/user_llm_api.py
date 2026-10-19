"""按用户隔离的 LLM 配置 API（W08a / MU-2 的前置切片）。

## 为什么单独一个模块，而不是塞进 auth_api.py

`auth_api.py` 的职责是「谁是谁」（身份与授权），本模块的职责是「你的模型用哪个 key」。
两者混在一起会让 `PUBLIC_EXACT` / `ADMIN_EXACT` 这类安全敏感表与业务配置逻辑纠缠，
后续 MU-2 真正落地 `user_llm_config` 表时改动面也无法收敛。分开后：
* `auth_api.required_role()` 只需登记 2 条路径；
* `user_llm_api` 只关心「当前用户」与「配置读写」。

## 当前实现范围（**诚实标注**）

本次只做 **HTTP 契约 + 归属判定 + 分级**，存储仍落在全局 `data/llm_config.json`，
`GET` 会带 `scope="global"` 明确告知前端「尚未真正隔离」。真正隔离（`user_llm_config`
表 + Fernet 加密 + 预算分键 + `config_fp` 缓存键）属于 MU-2，是需要改
`llm_client` / `engine` 的大块工作，不能靠改个端点的归属来假装完成。

**但归属判定必须现在做对**：如果 `/api/user/llm/config` 允许任何用户写全局文件，
那么普通用户改一次 key 就会把管理员的通道顶掉 —— 这是多用户上线即会发生的越权。
所以当前语义是：
* 普通用户读 → 返回自己的**逻辑视图**（未配置时如实返回「未配置」，不泄露管理员 key）；
* 普通用户写 → **403**，并明说原因（尚未隔离，请联系管理员）；
* 管理员写 → 落全局文件，`scope="global"`。
MU-2 完成后只需替换 `_load_view` / `_save_view` 两个函数，端点契约与前端不用再动。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import auth, config

router = APIRouter()


# ---------------------------------------------------------------------------
# 共享工具
# ---------------------------------------------------------------------------

def _err(msg: str, code: int = 400, **extra) -> JSONResponse:
    body: Dict[str, Any] = {"ok": False, "error": msg}
    body.update(extra)
    return JSONResponse(body, status_code=code)


def _session(request: Request) -> Optional[Dict[str, Any]]:
    from . import auth_api
    return auth_api.session_from_request(request)


def _csrf_ok(request: Request, sess: Dict[str, Any]) -> bool:
    return auth.require_csrf(sess.get("session") or {},
                            request.headers.get("x-csrf-token"))


def _read_global_file() -> Dict[str, Any]:
    """读 `data/llm_config.json`（**只读，不触发任何模型调用**）。"""
    import json
    try:
        if config.LLM_CONFIG_FILE.exists():
            data = json.loads(config.LLM_CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (ValueError, OSError):
        pass
    return {}


def _mask_key(key: Optional[str]) -> str:
    """脱敏显示：`sk-****abcd`。

    规则（U2 §2.3）：长度 >= 8 时保留前 3 与后 4；更短的**全部隐去**，
    因为短 key 保留首尾会显著提高爆破命中率。
    """
    if not key:
        return ""
    if len(key) < 8:
        return "****"
    return f"{key[:3]}****{key[-4:]}"


def _public_view(data: Dict[str, Any], *, configured: bool,
                 scope: str, source: str) -> Dict[str, Any]:
    """对外字段白名单。**绝不含 api_key 明文**。

    这条不是"顺手脱敏"：`GET /api/config/llm` 原本连 `api_key` 都不返回，
    但前端 `loadLlmConfig()` 里写了 `config.api_key` 的读取分支——
    新增 `api_key_masked` 字段而非改 `api_key` 语义，避免既有代码误会。
    """
    return {
        "ok": True,
        "scope": scope,                 # "user" | "global" | "unbound"
        "source": source,               # "user_table" | "global_file" | "none"
        "configured": bool(configured),
        "disabled": bool(data.get("disabled", False)),
        "base_url": str(data.get("base_url") or ""),
        "model": str(data.get("model") or ""),
        "samples": int(data.get("samples") or 3),
        "api_key_masked": _mask_key(data.get("api_key")),
        "has_api_key": bool(data.get("api_key")),
    }


# ---------------------------------------------------------------------------
# 用户自己的配置
# ---------------------------------------------------------------------------

@router.get("/api/user/llm/config")
def get_user_llm_config(request: Request):
    """读**当前用户**的 LLM 配置视图。

    未配置 = 一个 200 + `configured=false`，**不是** 404 ——
    前端需要区分"没配"和"接口坏了"，404 会被统一错误处理当成异常弹窗。
    """
    sess = _session(request)
    if not sess:
        return _err("未登录", code=401)
    user = sess["user"]

    data = _read_global_file()
    # MU-1 阶段的归属规则：只有管理员才有权使用全局通道；
    # 普通用户在 MU-2 之前**没有**可用配置，如实返回未配置。
    if user.get("role") == "admin":
        from . import llm_client
        learned = None
        if config.LLM_BASE_URL and config.LLM_MODEL:
            try:
                learned = llm_client.learned_max_tokens(
                    config.LLM_BASE_URL, config.LLM_MODEL)
            except Exception:  # noqa: BLE001 —— 纯装饰信息，拿不到不影响主流程
                learned = None
        view = _public_view(data, configured=config.llm_configured(),
                            scope="global", source="global_file")
        view["learned_max_tokens"] = learned
        view["user_id"] = user["user_id"]
        return view

    # 普通用户：MU-2 之前无个人配置。**不回落到全局**（那等于让所有人共用一把 key，
    # 且泄露管理员的 base_url/model）。
    return _public_view({}, configured=False, scope="unbound", source="none")


@router.post("/api/user/llm/config")
async def save_user_llm_config(request: Request):
    """保存**当前用户**的 LLM 配置。

    当前仅管理员可写（写入全局文件），普通用户返回 403 并说明原因。
    MU-2 落地后本函数改为写 `user_llm_config` 表，届时普通用户自然获得写权限。
    """
    sess = _session(request)
    if not sess:
        return _err("未登录", code=401)
    user = sess["user"]

    if not _csrf_ok(request, sess):
        return _err("CSRF 校验失败", code=403)

    if user.get("role") != "admin":
        from . import auth_api
        auth.audit("authz.denied", actor_id=user["user_id"],
                   ip=auth_api.client_ip(request), result="denied",
                   detail={"path": "/api/user/llm/config", "reason": "llm_not_isolated_yet"})
        return _err(
            "个人 LLM 配置隔离尚未上线：当前只有管理员可以修改模型通道。"
            "在此之前你可以直接使用统计通道出号（不需要任何 key）。",
            code=403, reason="not_isolated")

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return _err("请求体必须是 JSON")
    if not isinstance(payload, dict):
        return _err("请求体必须是 JSON 对象")

    # 复用既有保存逻辑（它已经处理好"只更新传入的键"与"保留未知键"）。
    # 不在这里重写一遍，避免两处对同一文件的写入规则漂移。
    from . import api_app
    res = api_app.update_llm_config(payload)
    if not isinstance(res, dict) or not res.get("ok"):
        return _err((res or {}).get("error") or "保存失败", code=500)
    return _public_view(_read_global_file(), configured=config.llm_configured(),
                        scope="global", source="global_file")


# ---------------------------------------------------------------------------
# 管理员：全局默认
# ---------------------------------------------------------------------------

def _require_admin(request: Request):
    from . import auth_api
    return auth_api._require_admin(request)


@router.get("/api/admin/llm/default")
def get_global_llm_default(request: Request):
    """全局默认 LLM 配置（**管理员专用**，含 base_url/model，不含 key 明文）。"""
    sess, err = _require_admin(request)
    if err:
        return err
    return _public_view(_read_global_file(), configured=config.llm_configured(),
                        scope="global", source="global_file")


@router.post("/api/admin/llm/default")
async def save_global_llm_default(request: Request):
    """写全局默认配置。管理员专用（分级表里 `/api/admin/` 前缀已兜底，
    这里再自检一次，形成双保险）。"""
    from . import auth_api
    sess, err = _require_admin(request)
    if err:
        return err
    if not _csrf_ok(request, sess):
        return _err("CSRF 校验失败", code=403)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return _err("请求体必须是 JSON")
    if not isinstance(payload, dict):
        return _err("请求体必须是 JSON 对象")
    from . import api_app
    res = api_app.update_llm_config(payload)
    if not isinstance(res, dict) or not res.get("ok"):
        return _err((res or {}).get("error") or "保存失败", code=500)
    auth_api.auth.audit("llm_config.global_save",
                        actor_id=sess["user"]["user_id"],
                        ip=auth_api.client_ip(request),
                        detail={"keys": sorted(k for k in payload if k != "api_key")})
    return _public_view(_read_global_file(), configured=config.llm_configured(),
                        scope="global", source="global_file")
