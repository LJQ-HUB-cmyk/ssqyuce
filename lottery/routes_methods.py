"""方法开关域路由（M4.2 A/B 方法通道配置）

P2 模块化：从 `api_app.py` 切出的 methods 域。

切分原则：**域模块不得 import api_app**（否则等于把大文件切成几个大文件）。
需要的应用级能力从 `request_context` 或专门的服务模块取。
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from . import config, db
from . import methods as _methods

router = APIRouter()

@router.get("/api/methods/status")
def methods_status():
    """当前方法开关：模式 / 原始字符串 / 解析规格 / 方法注册表 / 各方法族开关状态。"""
    from . import methods as METH
    st = config.methods_status()
    eff = METH.effective_spec(config.METHOD_MODE, config.METHODS_SPEC)
    st["effective"] = {"mode": eff.get("mode", "all"),
                       "tokens": sorted(eff.get("tokens", set()))}
    st["registry"] = METH.registry()
    st["families"] = {fam: METH.is_enabled(fam, eff) for fam in METH.FAMILIES}
    return st


@router.post("/api/methods/config")
def update_methods_config(payload: dict):
    """更新方法开关：{methods, mode}，立即生效并持久化，影响之后的预测。

    methods: LOTT_METHODS 字符串（逗号/空格分隔；- 前缀 = 关闭；留空 = 全部启用）；
    mode: production（严格按开关过滤）/ research（忽略开关、全部启用）。
    """
    from . import methods as METH
    raw = payload.get("methods")
    mode = payload.get("mode")
    if raw is not None and not isinstance(raw, str):
        return JSONResponse({"ok": False, "error": "methods 必须是字符串"},
                            status_code=400)
    if raw is not None:
        err = METH.validate_raw(raw)
        if err:
            return JSONResponse({"ok": False, "error": err}, status_code=400)
    if mode is not None:
        m = str(mode).strip().lower() if isinstance(mode, str) else None
        if m not in METH.MODES:
            return JSONResponse(
                {"ok": False, "error": f"mode 必须是 {'/'.join(METH.MODES)}，收到 {mode!r}"},
                status_code=400)
        mode = m
    try:
        st = config.set_methods(raw, mode)
    except (ValueError, OSError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return {"ok": True, **st}
