"""后台任务运行器（P2 从 api_app 中提取的共享基础设施）。

## 为什么独立成模块

`_bg_run()` 被多个域共用（ML 诊断、AI 闭环、LLM 工坊、离线评估）。若留在
`api_app`，每个域都得反向 import 它 —— 切分等于白做。

## 它解决什么

AI 闭环 / LLM 工坊动辄 1~8 分钟。同步 HTTP 请求期间浏览器只能转圈、
没有任何阶段信息，用户会以为「点了没反应」。改成「立即返回 task_id →
前端每 3 秒轮询 `/api/tasks/{id}` → 显示阶段 + 进度」。

## 一个必须保留的细节：contextvars

裸 `threading.Thread` **不继承** contextvars。MU-2 的用量记账正是靠 contextvar
判断"这次调用算谁的"，所以这里必须显式 `copy_context().run(...)`。

漏掉的后果是**静默**的：LLM 调用成功、成本日志齐全，但配额计数永远是 0，
用户以为有额度、实际从未被计数（bug 实测过，见 tests/test_mu2_contextvar.py）。
"""
from __future__ import annotations

import contextvars
import json
import threading
import time
import uuid

from . import db


def _bg_run(kind: str, fn, on_done=None) -> str:
    """把长耗时操作放进后台线程并登记成 db 任务，供前端轮询进度。

    为什么要有它（用户反馈第 2 条）：AI 闭环/LLM 工坊动辄 1~8 分钟，
    同步 HTTP 请求期间浏览器只能转圈、也没有任何阶段信息，用户就会觉得"点了没反应"。
    这里改成「立即返回 task_id → 前端每 3 秒轮询 /api/tasks/{id} → 显示阶段 + 进度」。
    """
    import threading
    import uuid

    tid = f"{kind}_{uuid.uuid4().hex[:8]}"
    db.create_task(tid, kind)

    def _run():
        def _progress(frac: float, msg: str) -> None:
            db.update_task(tid, "running", frac, msg)
        try:
            db.update_task(tid, "running", 0.01, "已启动…")
            result = fn(_progress)
            conn = db.get_conn()
            conn.execute("UPDATE tasks SET status=?, progress=?, message=?, result_json=?, updated_at=? WHERE id=?",
                         ("completed", 1.0, (result or {}).get("note_zh", "完成")[:300],
                          json.dumps(result or {}, ensure_ascii=False, default=str), time.time(), tid))
            conn.commit()
            if on_done:
                try:
                    on_done(result)
                except Exception:  # noqa: BLE001
                    pass
        except Exception as e:  # noqa: BLE001
            db.update_task(tid, "failed", 0.0, f"{type(e).__name__}: {e}")

    # MU-2：同 llm_eval，后台任务里的 LLM 消耗必须记到发起人头上
    threading.Thread(target=contextvars.copy_context().run, args=(_run,),
                     daemon=True, name=f"bg-{tid}").start()
    return tid
