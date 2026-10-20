"""ML 概率模型域路由（M2）

P2 模块化：从 `api_app.py` 切出的 ml 域。

切分原则：**域模块不得 import api_app**（否则等于把大文件切成几个大文件）。
需要的应用级能力从 `request_context` 或专门的服务模块取。
"""
from __future__ import annotations

from typing import Optional
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from . import db

router = APIRouter()

@router.get("/api/ml/status")
def ml_status():
    from . import ml_model
    draws = db.load_draws()
    return ml_model.ml_status(draws)


@router.post("/api/ml/eval")
def ml_eval(window: int = Query(30, ge=10, le=200),
            refit_every: int = Query(15, ge=1, le=50),
            train_window: int = Query(800, ge=200, le=3490)):
    """ML 概率模型 walk-forward 滚动评估：Brier/log-loss/校准曲线 + paired 检验。

    注意：重训使用最近 train_window 期历史（默认 800），通常耗时 2~5 分钟。
    """
    from . import ml_model
    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "error": "本地暂无开奖数据"}, status_code=400)
    return ml_model.evaluate_ml_walkforward(
        draws, window=window, refit_every=refit_every, train_window=train_window)
