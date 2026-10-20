"""LLM 离线评估域路由

P2 模块化：从 `api_app.py` 切出的 llm_eval 域。

切分原则：**域模块不得 import api_app**（否则等于把大文件切成几个大文件）。
需要的应用级能力从 `request_context` 或专门的服务模块取。
"""
from __future__ import annotations

import contextvars
import json
from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from . import config, db

router = APIRouter()

@router.post("/api/eval/llm/run")
def llm_eval_run(issues: int = Query(60, ge=3, le=200),
                 n: int = Query(5, ge=1, le=20),
                 seed: Optional[int] = Query(None, ge=0)):
    """M3.1 LLM 离线评估：三通道 walk-forward（stat / stat+llm / random）后台任务。"""
    import threading
    import uuid
    from . import llm_eval
    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "error": "本地暂无开奖数据"}, status_code=400)
    if len(draws) < 305:
        return JSONResponse({"ok": False, "error": f"数据不足（需 ≥305 期，当前 {len(draws)}）"},
                            status_code=400)
    if not config.llm_configured():
        return JSONResponse({"ok": False,
                             "error": "LLM 通道未配置，无法运行 stat_llm 对比（请先在设置页配置并保存）"},
                            status_code=400)
    task_id = f"llm_eval_{uuid.uuid4().hex[:8]}"
    db.create_task(task_id, "llm_eval")

    def _work():
        try:
            db.update_task(task_id, "running", 0.01, "三通道 walk-forward 评估准备中...")
            res = llm_eval.run_llm_eval(
                draws, issues=issues, n_tickets=n, seed=seed,
                progress_cb=lambda p, m: db.update_task(task_id, "running", round(p, 4), m))
            db.complete_task(task_id, json.dumps(res, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001
            db.fail_task(task_id, str(e))

    # MU-2：后台线程**不继承** contextvars，必须显式带过去，否则这次评估烧掉的
    # LLM 额度记不到发起人名下（配额统计静默落空）。用 copy_context 而非传参：
    # 深层库 llm_client 是按 contextvar 读用户的（Lead 裁决 9）。
    threading.Thread(target=contextvars.copy_context().run, args=(_work,),
                     daemon=True).start()
    return {"ok": True, "task_id": task_id,
            "note": ("后台任务运行中（60 期 × 三通道，LLM 通道较慢，约 5~30 分钟）。"
                     "轮询 /api/tasks/<task_id> 完成后查看 /api/eval/llm/latest。")}


@router.get("/api/eval/llm/latest")
def llm_eval_latest():
    from . import llm_eval
    report = llm_eval.load_latest_report()
    if report is None:
        return {"status": "none"}
    return {"status": "ready", "report": report}
