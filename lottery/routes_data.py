"""数据域路由（开奖数据 / 健康 / 系统信息 / 对账）。

P2 模块化：从 `api_app.py` 切出。

## 这一域的两个"必须"：

1. **`/api/health` 必须匿名可达** —— docker healthcheck 依赖它。它也是
   最容易被忽略的暴露面：早期版本把 `/docs` 留在公网可访问，而 `/api/health`
   反而因为"看起来无害"被塞进公开白名单后又被加回鉴权（见 auth_api 的注释）。
2. **`/api/health` 不能因为任何内部异常而 500** —— schema 读不到时返回
   `version=None` + error 字段，而不是让容器被判定为不健康。
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from . import config, data_fetcher, db, migrations
from . import features as F

router = APIRouter()

@router.get("/api/data/reconcile")
def data_reconcile():
    from . import data_check
    return data_check.run()


@router.get("/api/data/reconcile/history")
def reconcile_history(limit: int = Query(30, ge=1, le=500)):
    """返回多源对账审计记录，便于追踪告警与恢复。"""
    return {"items": db.load_reconcile_runs(limit)}


@router.get("/api/health")
def health():
    # V10-00d：对外暴露 schema_version，部署/回滚时可直接确认库结构版本
    # （迁移是懒加载自动执行的，没有这个字段只能靠连库 PRAGMA 猜）。
    try:
        schema = migrations.status(db.get_conn())
    except Exception as e:  # noqa: BLE001 - 健康检查不能因此 500
        schema = {"version": None, "latest": migrations.latest_version(),
                  "error": f"{type(e).__name__}: {e}"}
    return {
        "status": "ok",
        "version": config.APP_VERSION,
        "issues": db.count_draws(),
        "max_issue": db.max_issue(),
        "eval_methods": [r["method"] for r in db.cumulative_eval(limit=1).get("methods", [])],
        "app_build": config.APP_BUILD,
        "schema_version": schema["version"],
        "schema_latest": schema["latest"],
        "uptime_seconds": max(0, int(__import__('time').time() - config.STARTED_AT)),
        "api_auth_enabled": bool(config.API_TOKEN),
        "recent_tasks": db.list_tasks(limit=5),
    }


@router.get("/api/info")
def app_info():
    """系统版本与里程碑状态（主页页脚徽标与「关于」展示用）。"""
    from . import methods as METH
    return {
        "name": "双色球智能预测分析系统",
        "version": config.APP_VERSION,
        "build": config.APP_BUILD,
        "milestones": config.APP_MILESTONES,
        "plan": "docs/UPGRADE_PLAN.md + docs/M3_M4_PLAN.md",
        # U2/U3：投注模式与蓝球口径、方法自适应降权状态
        "bet_mode": config.BET_MODE,
        "blue_mode": config.BLUE_MODE,
        "blue_cover": config.BLUE_COVER,
        "adaptive": METH.adaptive_status(),
        # V4：前端样本量门槛的唯一出处（避免前后端各写一个数）
        "method_min_issues": config.METHOD_RECOMMENDATION_MIN_ISSUES,
        # V10-04b：本期出号通道（resolve_channels() 是唯一判据）。前端用它回填
        # 「启用 LLM」复选框的默认值 —— HTML 里**不再**硬编码 checked，
        # 否则前端会吃掉后端默认 off。这里用无副作用的现算，不改全局快照。
        "channels": METH.resolve_channels(),
        "honesty_boundary": "双色球为独立随机事件，本系统不承诺中奖率",
    }


@router.post("/api/refresh")
def refresh():
    """抓取远程最新开奖并增量入库（返回统计信息）。"""
    try:
        info = data_fetcher.fetch_and_update()
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    return {"ok": True, **info}
@router.get("/api/draws/history")
def draw_history(n: int = Query(500, ge=1, le=3490)):
    """返回按序号排列的开奖历史（用于走势图）。"""
    draws = db.load_last_draws(n)
    return {
        "issues": [d["issue"] for d in draws],
        "sums": [sum(d["reds"]) for d in draws],
        "blues": [d["blue"] for d in draws],
        "reds": [d["reds"] for d in draws],
    }


@router.get("/api/features")
def get_features():
    """最新一期的多尺度统计报告（含长/中/短窗口）。"""
    draws = db.load_draws()
    return F.compute_features(draws)
