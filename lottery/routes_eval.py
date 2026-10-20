"""评估域路由（评估 / 离线评估 / 方法战绩）

P2 模块化：从 `api_app.py` 切出的 eval 域。

切分原则：**域模块不得 import api_app**（否则等于把大文件切成几个大文件）。
需要的应用级能力从 `request_context` 或专门的服务模块取。
"""
from __future__ import annotations

from typing import Optional
from fastapi import APIRouter, Query
from . import db

router = APIRouter()

@router.get("/api/eval")
def eval_view():
    rows = db.load_eval()
    return rows


@router.post("/api/eval/backtest")
def eval_backtest(issues: int = Query(120), n: int = Query(10)):
    from . import evaluate
    draws = db.load_draws()
    res = evaluate.offline_backtest(draws, issues=min(issues, 200), n_tickets=n,
                                    use_llm=False)
    return res
@router.post("/api/eval/online")
def eval_online():
    from . import evaluate, notify
    result = evaluate.online_check()
    # V10-04b（R1 P1-4）：方法战绩按族分列，与组合口径的 rows/summary 并列返回
    try:
        result["by_method"] = evaluate.method_breakdown()
    except Exception:  # noqa: BLE001 - 报表失败不影响对照本身
        result["by_method"] = None
    # U3：开奖对照后刷新方法自适应降权决策
    from . import methods as METH
    try:
        result["adaptive"] = METH.adaptive_status()
    except Exception:  # noqa: BLE001
        result["adaptive"] = None
    result["notification"] = notify.notify_after_check(result)
    return result
@router.get("/api/eval/cumulative")
def eval_cumulative(method: Optional[str] = Query(None), limit: int = Query(120, ge=1, le=1000)):
    """M4.1：按预测方法返回逐注事实与累计统计；U3 附加方法自适应降权状态。"""
    from . import methods as METH
    report = db.cumulative_eval(method=method, limit=limit)
    try:
        report["adaptive"] = METH.adaptive_status()
    except Exception:  # noqa: BLE001
        report["adaptive"] = None
    return report


@router.get("/api/eval/by-method")
def eval_by_method(limit_issues: int = Query(200, ge=1, le=5000)):
    """V10-04b（R1 P1-4）：方法战绩按族**分列**（llm / stat / blend / ml / uniform）。

    与 /api/eval（``eval_results``，组合口径）严格区分：本接口不返回任何跨族
    合计/加权平均，不同方法族的注数口径不同，相加即失去意义。
    """
    from . import evaluate
    return evaluate.method_breakdown(limit_issues=limit_issues)
@router.get("/api/eval/recommendations")
def eval_recommendations(limit: Optional[int] = Query(None, ge=1, le=1000),
                        min_sample: Optional[int] = Query(None, ge=1, le=1000)):
    """M4.2：按期开出的方法与 uniform 基线做 paired 筛查，输出运营建议。"""
    return db.method_recommendations(limit=limit, min_sample=min_sample)


@router.get("/api/eval/export.csv")
def eval_export_csv(method: Optional[str] = Query(None), limit: int = Query(1000, ge=1, le=5000)):
    import csv
    import io
    from fastapi.responses import StreamingResponse
    report = db.cumulative_eval(method=method, limit=limit)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["issue", "method", "seq", "red_hits", "blue_hit", "prize_level", "reward", "ticket_cost", "net_return", "evaluated_at"])
    for group in report["methods"]:
        for row in group.get("rows", []):
            writer.writerow([row.get(k, "") for k in ("issue", "method", "seq", "red_hits", "blue_hit", "prize_level", "reward", "ticket_cost", "net_return", "evaluated_at")])
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=eval_cumulative.csv"})
