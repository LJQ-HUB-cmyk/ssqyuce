"""FastAPI 应用：REST API + 静态单页前端。"""
from __future__ import annotations

import json
import hmac
import time
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

class _NoCacheStaticFiles(StaticFiles):
    """静态资源禁用缓存，确保前端更新后浏览器立即拿到新版。"""
    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-store, max-age=0"
        return resp

from . import config, data_fetcher, db
from . import migrations
from . import features as F, diagnose as D, mining as M

app = FastAPI(title="双色球智能预测分析系统", version=config.APP_VERSION)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


@app.middleware("http")
async def optional_api_auth(request: Request, call_next):
    """LOTT_TOKEN 配置后保护 /api/*；静态前端和健康探活保持可加载。"""
    if config.API_TOKEN and request.url.path.startswith("/api/"):
        auth = request.headers.get("authorization", "")
        expected = f"Bearer {config.API_TOKEN}"
        if not hmac.compare_digest(auth, expected):
            return JSONResponse({"ok": False, "error": "需要 Bearer Token"}, status_code=401,
                                headers={"WWW-Authenticate": "Bearer"})
    return await call_next(request)


WEB_DIR = Path(__file__).resolve().parent.parent / "web"
STATIC_DIR = WEB_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/static", _NoCacheStaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------- 数据 ----------

@app.get("/api/data/reconcile")
def data_reconcile():
    from . import data_check
    return data_check.run()


@app.get("/api/data/reconcile/history")
def reconcile_history(limit: int = Query(30, ge=1, le=500)):
    """返回多源对账审计记录，便于追踪告警与恢复。"""
    return {"items": db.load_reconcile_runs(limit)}


@app.get("/api/health")
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


@app.get("/api/info")
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


@app.post("/api/refresh")
def refresh():
    """抓取远程最新开奖并增量入库（返回统计信息）。"""
    try:
        info = data_fetcher.fetch_and_update()
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    return {"ok": True, **info}


@app.get("/api/draws/latest")
def latest_draws(n: int = Query(20, ge=1, le=200)):
    draws = db.load_last_draws(n)
    return [{
        "issue": d["issue"], "date": d["date"], "reds": d["reds"], "blue": d["blue"],
        "sum": sum(d["reds"]),
    } for d in draws]


@app.get("/api/draws/history")
def draw_history(n: int = Query(500, ge=1, le=3490)):
    """返回按序号排列的开奖历史（用于走势图）。"""
    draws = db.load_last_draws(n)
    return {
        "issues": [d["issue"] for d in draws],
        "sums": [sum(d["reds"]) for d in draws],
        "blues": [d["blue"] for d in draws],
        "reds": [d["reds"] for d in draws],
    }


@app.get("/api/features")
def get_features():
    """最新一期的多尺度统计报告（含长/中/短窗口）。"""
    draws = db.load_draws()
    return F.compute_features(draws)


# ---------- 规律 ----------

@app.get("/api/patterns")
def get_patterns():
    return {
        "items": db.load_patterns(),
        "summary": _pattern_summary(db.load_patterns(grade_filter=None)),
    }


def _pattern_summary(patterns: List[Dict]) -> Dict:
    g = {"A": 0, "B": 0, "C": 0}
    for p in patterns:
        g[p.get("grade", "C")] = g.get(p.get("grade", "C"), 0) + 1
    return {"A": g["A"], "B": g["B"], "C": g["C"]}


@app.post("/api/patterns/backtest")
def run_backtests():
    from . import backtest as BT
    draws = db.load_draws()
    results = BT.run_all_backtests(draws, min_start=300)
    return {
        "items": db.load_patterns(),
        "summary": _pattern_summary(results),
        "note": ("walk-forward 样本外回测：只用目标期之前的历史，禁用未来信息。"
                 "A=显著，B=弱信号，C=不通过。"),
    }


# ---------- 预测 ----------

@app.api_route("/api/predict", methods=["GET", "POST"])
def predict(use_llm: Optional[bool] = None, n_tickets: int = 10,
            regenerate: bool = False,
            bet_mode: Optional[str] = None,
            coverage: bool = False,
            coverage_r: Optional[int] = None):
    """生成下一期预测；目标期已有预测且未要求重新生成时复用缓存。

    bet_mode: U2 投注模式 single / blue_compound / dan_tuo（None=跟随 LOTT_BET_MODE）。
    coverage: U4 覆盖优化（True 时做“至少命中 r 个红球”+蓝球覆盖组合优化）。
    coverage_r: coverage 模式目标红球命中数。

    M4.5 快速失败：use_llm=true（前端勾选「使用大模型」）时 LLM 通道
    不可用/超时/输出异常，直接返回 503 + 明确错误信息（预测失败），
    不再静默降级为纯统计模型；use_llm=false 走纯统计，行为不变。
    """
    from . import backtest as BT, engine
    from . import methods as METH
    from .llm_client import LLMChannelError
    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "error": "本地暂无开奖数据，请先刷新"}, status_code=400)
    # V7：期号按真实开奖日历推算（每周二/四/日），并在数据陈旧时明说，
    # 不再用「seq+1」硬猜（旧写法在期号越界时会显示成明年的 4xx 期号）。
    _exp = BT.expected_issue(draws)
    issue = _exp["issue"]
    if not regenerate:
        existing = db.load_predictions(issue)
        if existing:
            # 从持久化快照读取 U2/U3/U4 元数据，确保 from_cache 也返回完整响应头
            meta = {}
            for m in db.load_eval_meta(issue):
                snap = json.loads(m.get("config_snapshot_json") or "{}")
                meta = {k: snap.get(k) for k in
                        ("bet_mode", "coverage_mode", "blue_covered", "blue_coverage_rate",
                         "llm_only_output", "shortfall_reason", "requested_tickets")}
                break
            # 旧快照缺失时兜底（避免前端读到 None）
            bm = meta.get("bet_mode") or config.BET_MODE
            if bm not in ("single", "blue_compound", "dan_tuo"):
                bm = "single"
            _llm_used = any(str(t.get("method", "")).startswith("llm:") for t in existing)
            _gen_ts = max((t.get("created_at") or 0) for t in existing) if existing else None
            return {"issue": issue, "tickets": existing, "from_cache": True,
                    "generated_at": (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_gen_ts))
                                     if _gen_ts else None),
                    "generated_at_ts": _gen_ts,
                    "data_basis": _exp,
                    "actual_tickets": len(existing),
                    "requested_tickets": meta.get("requested_tickets") or len(existing),
                    "shortfall_reason": meta.get("shortfall_reason"),
                    "llm_only_output": bool(meta.get("llm_only_output", False)),
                    "llm_used": _llm_used,
                    "bet_mode": bm,
                    "coverage_mode": bool(meta.get("coverage_mode", False)),
                    "blue_covered": meta.get("blue_covered"),
                    "blue_coverage_rate": meta.get("blue_coverage_rate"),
                    "adaptive": METH.adaptive_status() if config.ADAPTIVE_ENABLED else None}
    try:
        res = engine.predict_next(draws, use_llm=use_llm, n_tickets=n_tickets,
                                  llm_required=(use_llm is True),
                                  bet_mode=bet_mode, coverage=coverage,
                                  coverage_r=coverage_r)
    except LLMChannelError as e:
        return JSONResponse(
            {"ok": False, "llm_required": True,
             "error": (f"预测失败：大模型不可用或超时（{e}）。"
                       "已停止降级为纯统计模式：请在「设置」页检查并修复 LLM 通道，"
                       "或取消勾选「使用大模型」后用纯统计模式重试。")},
            status_code=503)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"预测引擎异常: {e}"}, status_code=500)
    res["from_cache"] = False
    res.setdefault("data_basis", _exp)
    return res


def _sanitize_issue_order(issues: Sequence[str]) -> List[str]:
    """期号排序/过滤：剔除越界期号（史料里 seq 应落在当年 1~160）。

    为什么必须做：本地库里残留一条 `2026401` 的预测（2026-10-15 由已修复的
    `next_issue()` 越界跳号生成）。它**数值上**大于任何 2026 年的合法期号，
    于是无论按文本排还是按数值排，"最新预测"都会命中它，用户刷新后看到的
    永远是 15 日那条旧数据（用户反馈第 2 条，两次修复未竟的根因）。
    """
    out = []
    for it in issues or []:
        s = str(it)
        if not (len(s) == 7 and s.isdigit()):
            continue
        year, seq = int(s[:4]), int(s[4:])
        if not (2003 <= year <= 2100 and 1 <= seq <= 160):
            continue
        out.append(s)
    return out


@app.get("/api/predictions/last")
def last_predictions():
    """最近一期预测（含**生成时间**与数据基准，V7）。

    V6 及以前只返回 issue + tickets，前端因此无法显示"什么时候生成的"；
    现在补上 generated_at / data_basis / rules 摘要，让"这注是什么时候、基于哪期数据算的"可见。
    """
    from . import backtest as BT
    issue = None
    # 取候选期号时**先按时间**再按数值：越界/异常期号在两张排序下都可能置顶
    # （例：2026401 > 2026119），因此以"时间戳最新的一批"为准，再在合法期号里取最大。
    rows = db.get_conn().execute(
        "SELECT issue, MAX(created_at) AS ts FROM predictions GROUP BY issue "
        "ORDER BY ts DESC, CAST(issue AS INTEGER) DESC LIMIT 12").fetchall()
    valid = _sanitize_issue_order([r["issue"] for r in rows])
    issue = max(valid, key=lambda x: int(x)) if valid else (
        (rows[0]["issue"] if rows else None))
    tickets = db.load_predictions(issue) if issue else []
    draws = db.load_draws()
    gen_ts = max((t.get("created_at") or 0) for t in tickets) if tickets else None
    meta = {}
    for m in (db.load_eval_meta(issue) if issue else []):
        try:
            meta = json.loads(m.get("config_snapshot_json") or "{}")
        except (TypeError, ValueError):
            meta = {}
        break
    rules_used = sorted({r for t in tickets for r in (t.get("patterns_used") or [])})
    return {
        "issue": issue,
        "tickets": tickets,
        "generated_at": (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(gen_ts))
                         if gen_ts else None),
        "generated_at_ts": gen_ts,
        "data_basis": BT.expected_issue(draws) if draws else None,
        "requested_tickets": meta.get("requested_tickets"),
        "llm_used": any(str(t.get("method", "")).startswith("llm:") for t in tickets),
        "patterns_used": rules_used,
    }


# ---------- 评估 ----------

@app.get("/api/eval")
def eval_view():
    rows = db.load_eval()
    return rows


@app.post("/api/eval/backtest")
def eval_backtest(issues: int = Query(120), n: int = Query(10)):
    from . import evaluate
    draws = db.load_draws()
    res = evaluate.offline_backtest(draws, issues=min(issues, 200), n_tickets=n,
                                    use_llm=False)
    return res


@app.post("/api/eval/coverage")
def eval_coverage(issues: int = Query(60, ge=5, le=200), n: int = Query(10),
                  coverage_r: int = Query(3, ge=1, le=5)):
    """U4 离线对比：coverage 模式 vs 当前 single 模式的 ≥五等奖命中期数 / 蓝球覆盖。

    walk-forward：只用目标期之前数据预测（纯统计+ML，固定种子），对照实际开奖，
    比较两种选注策略的“≥五等奖命中期数占比”与单期蓝球覆盖。诚实口径：双色球独立
    随机，任何模式都不承诺超过数学期望，本对比量化的是“结构覆盖体感”差异。
    """
    import random
    import numpy as np
    from . import engine as E, evaluate as EV
    draws = db.load_draws()
    if len(draws) < issues + 301:
        return JSONResponse({"ok": False, "error": f"数据不足（需 ≥{issues + 301} 期）"},
                            status_code=400)
    rng = random.Random(20260817)
    start = len(draws) - issues
    stats = {"single": {"hit_periods": 0, "blue_cover": 0, "prizes": 0},
             "coverage": {"hit_periods": 0, "blue_cover": 0, "prizes": 0}}
    for i in range(start, len(draws) - 1):
        history = draws[:i]
        target = draws[i]
        for mode, cov in (("single", False), ("coverage", True)):
            res = E.predict_next(history, use_llm=False, use_ml=False, n_tickets=n,
                                 persist=False, rng=rng, coverage=cov, coverage_r=coverage_r)
            tr = EV.tickets_result(res["tickets"], target)
            stats[mode]["hit_periods"] += int(tr["best_level"] >= 5)
            stats[mode]["blue_cover"] += res["blue_covered"]
            stats[mode]["prizes"] += int(sum(tr["levels"]))
    n_periods = len(range(start, len(draws) - 1))
    for mode in stats:
        stats[mode]["hit_period_rate"] = round(stats[mode]["hit_periods"] / max(1, n_periods), 4)
        stats[mode]["avg_blue_cover"] = round(stats[mode]["blue_cover"] / max(1, n_periods), 2)
    return {
        "n_issues": n_periods, "n_tickets": n, "coverage_r": coverage_r,
        "single": stats["single"], "coverage": stats["coverage"],
        "delta_hit_period_rate": round(
            stats["coverage"]["hit_period_rate"] - stats["single"]["hit_period_rate"], 4),
        "delta_avg_blue_cover": round(
            stats["coverage"]["avg_blue_cover"] - stats["single"]["avg_blue_cover"], 2),
        "note": ("coverage 模式通过红球/蓝球覆盖优化，通常显著提升单期蓝球覆盖数，"
                 "并在相同注数下让“≥五等奖命中期数”体感不低于 single 模式；"
                 "双色球独立随机，此为结构覆盖的量化对比，非中奖保证。"),
    }


@app.post("/api/eval/online")
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


@app.get("/api/notify/status")
def notify_status():
    from . import notify
    return notify.status()


@app.get("/api/eval/cumulative")
def eval_cumulative(method: Optional[str] = Query(None), limit: int = Query(120, ge=1, le=1000)):
    """M4.1：按预测方法返回逐注事实与累计统计；U3 附加方法自适应降权状态。"""
    from . import methods as METH
    report = db.cumulative_eval(method=method, limit=limit)
    try:
        report["adaptive"] = METH.adaptive_status()
    except Exception:  # noqa: BLE001
        report["adaptive"] = None
    return report


@app.get("/api/eval/by-method")
def eval_by_method(limit_issues: int = Query(200, ge=1, le=5000)):
    """V10-04b（R1 P1-4）：方法战绩按族**分列**（llm / stat / blend / ml / uniform）。

    与 /api/eval（``eval_results``，组合口径）严格区分：本接口不返回任何跨族
    合计/加权平均，不同方法族的注数口径不同，相加即失去意义。
    """
    from . import evaluate
    return evaluate.method_breakdown(limit_issues=limit_issues)


@app.get("/api/eval/meta")
def eval_meta(issue: Optional[str] = Query(None)):
    return {"items": db.load_eval_meta(issue)}


@app.get("/api/eval/recommendations")
def eval_recommendations(limit: Optional[int] = Query(None, ge=1, le=1000),
                        min_sample: Optional[int] = Query(None, ge=1, le=1000)):
    """M4.2：按期开出的方法与 uniform 基线做 paired 筛查，输出运营建议。"""
    return db.method_recommendations(limit=limit, min_sample=min_sample)


@app.get("/api/eval/export.csv")
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


# ---------- M3.1 LLM 离线评估 ----------

@app.post("/api/eval/llm/run")
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

    threading.Thread(target=_work, daemon=True).start()
    return {"ok": True, "task_id": task_id,
            "note": ("后台任务运行中（60 期 × 三通道，LLM 通道较慢，约 5~30 分钟）。"
                     "轮询 /api/tasks/<task_id> 完成后查看 /api/eval/llm/latest。")}


@app.get("/api/eval/llm/latest")
def llm_eval_latest():
    from . import llm_eval
    report = llm_eval.load_latest_report()
    if report is None:
        return {"status": "none"}
    return {"status": "ready", "report": report}


# ---------- 任务系统 ----------

@app.get("/api/tasks")
def list_tasks(limit: int = Query(20, ge=1, le=100)):
    return db.list_tasks(limit)


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str):
    t = db.load_task(task_id)
    if t is None:
        return JSONResponse({"ok": False, "error": "task not found"}, status_code=404)
    return t


# ---------- 诊断 ----------

@app.get("/api/diagnose")
def diagnose_endpoint(
    reds: str = Query(..., description="逗号分隔的6个红球，如 1,4,7,12,28,31"),
    blue: int = Query(..., ge=1, le=16),
):
    try:
        r_list = [int(x) for x in reds.split(",")]
    except ValueError:
        return JSONResponse({"ok": False, "error": "reds 必须是逗号分隔的整数"}, status_code=400)
    if len(r_list) != 6 or len(set(r_list)) != 6:
        return JSONResponse({"ok": False, "error": "必须恰好 6 个不重复红球"}, status_code=400)
    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "error": "暂无数据"}, status_code=400)
    return D.diagnose(r_list, blue, draws)


# ---------- 挖掘 ----------

@app.post("/api/mining/run")
def run_mining(min_start: int = Query(300, ge=120, le=1000),
               engine: str = Query("rf")):
    from . import backtest as BT
    import uuid
    task_id = f"mine_{uuid.uuid4().hex[:8]}"
    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "error": "暂无数据"}, status_code=400)
    db.create_task(task_id, "mine")
    # 同步执行（挖掘较快，通常 <10s）
    try:
        db.update_task(task_id, "running", 0.1, "正在计算特征...")
        result = M.run_mining(draws, min_start=min_start, save_to_db=True, engine=engine)
        db.update_task(task_id, "completed", 1.0, "完成")
        db.complete_task(task_id, json.dumps(result, ensure_ascii=False))
        # 同时刷新规律列表
        BT.run_all_backtests(draws, min_start=min_start)
        return {"ok": True, "task_id": task_id, "result": result}
    except Exception as e:
        db.fail_task(task_id, str(e))
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/mining/latest")
def get_latest_mining():
    return M.get_latest_mining_result() or {"status": "none"}


# ---------- 回放诊断（M3.5） ----------

@app.post("/api/replay/diagnose")
def replay_diagnose(n: int = Query(20, ge=3, le=60), use_ml: bool = Query(True)):
    """最近 N 期「系统预测 vs 实际开奖」反事实回放（纯统计+ML，固定种子可复现）。"""
    import random
    import numpy as np
    from . import engine as E, evaluate as EV
    draws = db.load_draws()
    if len(draws) < n + 305:
        return JSONResponse({"ok": False, "error": f"数据不足（需 ≥{n + 305} 期）"},
                            status_code=400)
    rng = random.Random(20260817)
    results, rows = [], []
    for i in range(len(draws) - n, len(draws)):
        history = draws[:i]
        target = draws[i]
        res = E.predict_next(history, use_llm=False, n_tickets=10, persist=False,
                             use_ml=use_ml, rng=rng)
        tr = EV.tickets_result(res["tickets"], target)
        results.append(tr)
        rows.append({
            "issue": target["issue"], "date": target["date"],
            "red_hits_mean": round(float(np.mean(tr["red_hits"])), 3),
            "blue_hit": int(sum(tr["blue_hits"])),
            "prize_level": tr["best_level"],
            "reward": round(tr["reward"], 2),
        })
    agg = EV.aggregate(results)
    return {"ok": True, "use_ml": use_ml, "n": len(rows), "rows": rows,
            "aggregate": agg,
            "note": ("最近 N 期反事实回放：每期只用该期之前数据预测（纯统计+ML，固定种子可复现），"
                     "对照实际开奖。LLM 通道不参与（成本与噪声考虑）。")}


@app.get("/api/mining/reports")
def mining_reports(limit: int = Query(20, ge=1, le=100)):
    """挖掘运行历史（M3.3）：engine/候选数/通过率/lift/耗时 等运行记录。"""
    return {"runs": db.list_mining_runs(limit)}


# ---------- 历史预测 ----------

@app.get("/api/predictions/history")
def predictions_history(limit: int = Query(50, ge=1, le=200)):
    issues = db.recent_prediction_issues(limit)
    draws_map = {d["issue"]: d for d in db.load_draws()}
    out = []
    for issue in issues:
        preds = db.load_predictions(issue)
        actual = draws_map.get(issue)
        if actual and preds:
            from . import evaluate
            res = evaluate.tickets_result(preds, actual)
            out.append({
                "issue": issue,
                "date": actual["date"],
                "actual": {"reds": actual["reds"], "blue": actual["blue"]},
                "predictions": preds,
                "result": res,
            })
        elif preds:
            out.append({"issue": issue, "predictions": preds})
    return out



# ---------- LLM 配置管理（前台「设置」页，写入 data/llm_config.json） ----------

@app.get("/api/config/llm")
def get_llm_config():
    from . import config, llm_client
    cur = config.LLM_MODEL or ""
    learned = None
    if config.LLM_BASE_URL and cur:
        learned = llm_client.learned_max_tokens(config.LLM_BASE_URL, cur)
    return {
        "disabled": config.LLM_DISABLED,
        "base_url": config.LLM_BASE_URL,
        "model": config.LLM_MODEL,
        # 多模型轮转列表（设置页保存的模型是唯一事实来源；附加通道见 extra_models）
        "model_list": list(config.LLM_MODEL_LIST),
        "samples": config.LLM_SAMPLES,
        "max_tokens": config.LLM_MAX_TOKENS,
        "learned_max_tokens": learned,
        "total_timeout": config.LLM_TOTAL_TIMEOUT,
        "verify": config.LLM_VERIFY_ENABLED,
        "configured": config.llm_configured(),
    }


@app.post("/api/config/llm")
def update_llm_config(payload: dict):
    from . import config
    import os
    if "base_url" in payload:
        config.LLM_BASE_URL = (payload.get("base_url") or "").strip().rstrip("/") or None
        os.environ["LOTT_LLM_BASE_URL"] = config.LLM_BASE_URL or ""
    if "api_key" in payload:
        config.LLM_API_KEY = (payload.get("api_key") or "").strip() or None
        os.environ["LOTT_LLM_API_KEY"] = config.LLM_API_KEY or ""
    if "model" in payload:
        config.LLM_MODEL = (payload.get("model") or "").strip() or "minimax-m3"
        os.environ["LOTT_LLM_MODEL"] = config.LLM_MODEL
        # 设置页保存的模型是唯一事实来源：**替换**（而非前插）模型列表。
        # 前插会把 .env 里遗留的旧模型留在观察轮轮转列表里——新模型一旦失败，
        # 旧模型的 404 就成了最终报错，把真实原因（如推理型模型未产出正文）
        # 彻底掩盖掉。多模型容错请用 LOTT_LLM_EXTRA_MODELS / 附加通道。
        config.LLM_MODEL_LIST = [config.LLM_MODEL]
    if "samples" in payload:
        try:
            config.LLM_SAMPLES = max(1, min(20, int(payload["samples"])))
        except (TypeError, ValueError):
            config.LLM_SAMPLES = 3
        os.environ["LOTT_LLM_SAMPLES"] = str(config.LLM_SAMPLES)
    if "disabled" in payload:
        config.LLM_DISABLED = bool(payload["disabled"])
        os.environ["LOTT_LLM_DISABLED"] = "1" if config.LLM_DISABLED else "0"

    # 持久化到数据目录（挂载卷，容器重启不丢失）；保留文件里已有的其它键
    # （例如手工写入的 extra_body / total_timeout），避免设置页保存时被抹掉。
    config.LLM_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    conf = {}
    try:
        if config.LLM_CONFIG_FILE.exists():
            existing = json.loads(config.LLM_CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                conf.update(existing)
    except (ValueError, OSError):
        pass
    conf.update({
        "disabled": config.LLM_DISABLED,
        "base_url": config.LLM_BASE_URL or "",
        "api_key": config.LLM_API_KEY or "",
        "model": config.LLM_MODEL or "",
        "samples": config.LLM_SAMPLES,
    })
    try:
        config.LLM_CONFIG_FILE.write_text(
            json.dumps(conf, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        return {"ok": False, "error": f"配置写入失败: {e}"}
    return {"ok": True, "configured": config.llm_configured(),
            "model_list": list(config.LLM_MODEL_LIST)}


@app.post("/api/llm/test")
def test_llm_connection():
    """用当前配置做一次**贴合真实预测**的结构化出文探测。

    只回一句「连接成功」的轻量 ping 会掩盖真实故障：推理型模型在小请求下能出文，
    而预测用的长提示会把整个 max_tokens 预算耗在 reasoning_content 上（content
    为空），于是出现「测试成功、生成失败」。这里直接复用观察轮的真实提示与同一套
    预算自适应逻辑，并回报实际使用的输出预算与耗时。
    """
    from . import config, llm_client
    import time
    from .llm_client import LLMChannelError
    if config.LLM_DISABLED:
        return {"ok": False, "error": "LLM 已停用（在设置中启用后重试）"}
    cfgs = config.llm_model_list()
    if not cfgs:
        return {"ok": False, "error": "LLM 未配置（请先填写 API 地址 / Key / 模型并保存）"}
    cfg = cfgs[0]
    # 尽量用真实观察轮提示（数据不足时退化为一个小型 JSON 自检）
    user_prompt = '请只输出 JSON：{"ok": true, "note": "结构化输出自检"}'
    realistic = False
    try:
        from . import engine as E
        draws = db.load_draws()
        if len(draws) >= 60:
            stats = F.compute_features(draws)
            ctx = E.build_context(draws, stats, db.load_patterns())
            user_prompt = llm_client.observations_prompt(
                llm_client.compact_stats(ctx["stats"]), ctx["recent"], ctx["patterns"])
            realistic = True
    except Exception as e:  # noqa: BLE001
        print(f"[llm] 测试探测构建真实提示失败，退化为自检提示: {e}")
    t0 = time.time()
    try:
        res = llm_client.chat_json(
            llm_client.SYSTEM_BASE, user_prompt, max_tokens=1600, temperature=0.0,
            timeout=config.LLM_TIMEOUT, model_cfg=cfg, strict=True,
            deadline=time.time() + max(60.0, config.LLM_TOTAL_TIMEOUT))
    except LLMChannelError as e:
        return {"ok": False, "model": cfg.get("model"), "realistic": realistic,
                "time_ms": int((time.time() - t0) * 1000), "error": str(e),
                "hint": ("若提示「只返回推理内容」，说明该模型把输出预算全花在 reasoning 上："
                         f"可调大 LOTT_LLM_MAX_TOKENS（当前 {config.LLM_MAX_TOKENS}），"
                         "或改用不这样做的模型（例如 discovery-api-intern-ai-org/deepseek-v4-flash-0731）")}
    dt_ms = int((time.time() - t0) * 1000)
    learned = llm_client.learned_max_tokens(cfg["base_url"], cfg["model"])
    if not res:
        return {"ok": False, "model": cfg.get("model"), "realistic": realistic,
                "time_ms": dt_ms,
                "error": "模型有返回但无法解析为 JSON（请检查模型是否遵循 JSON 输出要求）"}
    keys = list(res.keys())[:6] if isinstance(res, dict) else []
    return {"ok": True, "model": cfg.get("model"), "realistic": realistic,
            "time_ms": dt_ms, "max_tokens": learned or 1600,
            "reasoning_model": bool(learned and learned > 1600),
            "json_keys": keys,
            "reply": json.dumps(res, ensure_ascii=False)[:160]}


# ---------- M4.2 方法 A/B 开关（Web 设置页配置，写入 data/methods_config.json） ----------

@app.get("/api/methods/status")
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


@app.post("/api/methods/config")
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


# ---------- M2 ML 概率模型 ----------

@app.get("/api/ml/status")
def ml_status():
    from . import ml_model
    draws = db.load_draws()
    return ml_model.ml_status(draws)


@app.post("/api/ml/eval")
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


# ---------- V4：诚实口径 —— 随机性检验 / 策略显著性 / 组合期望值 ----------
#
# 背景（用户实报「准确性过低」）：双色球是独立随机事件，系统**没有**预测能力。
# 用户看到的「蓝球命中率 10.0% vs 理论随机 6.25%」只是 1 期 10 注的样本波动；
# 「最佳方法」曾被封给一个已废弃的模型。这组端点把「能力是否存在」的判断
# 从「看一个百分比」改成「看统计检验 + 最小可检出效应」。
#
# 依赖 lottery/rigor.py（由队友实现）。该模块可能尚未落地，故全部走
# try/except ImportError 优雅降级：**绝不因为缺模块导致服务启动失败**。

def _rigor_or_503():
    """导入 rigor 模块；不可用时返回 (None, 503 响应)。

    返回 503 而非 500：这是「依赖尚未就绪」的服务端状态，不是请求错误。
    响应体里 available=false，前端据此展示「检验模块未就绪」而不是报错。
    """
    try:
        from . import rigor
    except ImportError as e:  # 模块尚未交付：降级，不影响服务启动与其它端点
        return None, JSONResponse(
            {
                "ok": False,
                "available": False,
                "error": "随机性检验模块 lottery.rigor 尚未就绪",
                "detail": str(e),
                "hint": "该端点需 lottery/rigor.py；其余功能不受影响。",
            },
            status_code=503,
        )
    return rigor, None


def _rigor_call_or_503(fn_name: str, *args, **kwargs):
    """调用 rigor 的某个函数，统一处理「模块缺失」与「调用失败」两种降级。

    · ImportError / AttributeError → 503（能力未就绪）
    · 其它异常 → 503 且带 error 文本（检验失败不能伪装成「无结论」以外的含义）
    """
    rigor, resp = _rigor_or_503()
    if resp is not None:
        return None, resp
    fn = getattr(rigor, fn_name, None)
    if fn is None:
        return None, JSONResponse(
            {
                "ok": False,
                "available": False,
                "error": f"lottery.rigor 未提供 {fn_name}()",
                "hint": "模块接口与契约不一致，请检查版本。",
            },
            status_code=503,
        )
    try:
        return fn(*args, **kwargs), None
    except Exception as e:  # noqa: BLE001 —— 检验失败不得让 API 500
        return None, JSONResponse(
            {
                "ok": False,
                "available": False,
                "error": f"{fn_name}() 执行失败",
                "detail": str(e),
            },
            status_code=503,
        )


@app.get("/api/rigor/randomness")
def rigor_randomness(limit: int = Query(0, ge=0, le=3490)):
    """随机性检验：这批开奖数据与「均匀随机」是否不可区分。

    limit=0 表示用全部历史（默认）。返回 randomness_report 的
    verdict / min_detectable_effect / tests，前端「随机性检验」卡直接消费。

    语义提醒：结论为「不可区分」时，任何「命中率高于理论值」的说法都只是波动，
    不是能力——这正是本端点存在的意义。
    """
    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "available": True,
                             "error": "本地暂无开奖数据"}, status_code=400)
    if limit:
        draws = draws[-int(limit):]
    report, resp = _rigor_call_or_503("randomness_report", draws)
    if resp is not None:
        return resp
    if not isinstance(report, dict):
        return JSONResponse({"ok": False, "available": False,
                             "error": "randomness_report() 返回结构异常",
                             "detail": f"期望 dict，实际 {type(report).__name__}"},
                            status_code=503)
    out = dict(report)
    out.setdefault("ok", True)
    out["available"] = True
    out["n_draws"] = len(draws)          # 样本量：所有结论必须带样本量
    return out


# 策略显著性检验支持的策略名（前端下拉与后端一致，未知策略显式报 400）
RIGOR_STRATEGIES = ("hot", "cold", "blue_hot", "blue_cold", "random")


def _strategy_hit_series(draws: List[Dict], strategy: str, window: int) -> Optional[List[float]]:
    """按窗口滚动计算该策略每期的「命中数」，返回与 draws 对齐的序列（前 window 期为 None 去掉）。

    · hot / cold：红球按窗口内出现频次排序，取前 6（hot）或后 6（cold）作为推荐组，
      命中数 = 与当期 6 个红球的交集大小（0..6）。
    · blue_hot / blue_cold：蓝球按窗口内频次取最高/最低的 1 个，命中为 1 否则 0。
    · random：以「期望命中数」作为基线常数序列（不引入伪随机，保证可复现）。

    这里只做**描述性**统计：返回的是命中数序列，显著性判断交给 rigor 模块。
    """
    if strategy == "random":
        return None  # random 作为基线由端点内部构造，不作为策略
    if window < 1 or len(draws) <= window:
        return None
    n = len(draws)
    rows: List[float] = []
    for i in range(window, n):
        hist = draws[i - window:i]
        cur = draws[i]
        if strategy in ("hot", "cold"):
            cnt: Dict[int, int] = {}
            for d in hist:
                for r in (d.get("reds") or []):
                    cnt[r] = cnt.get(r, 0) + 1
            if len(cnt) < 6:
                return None
            ordered = sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))
            pick = ordered[:6] if strategy == "hot" else ordered[-6:]
            chosen = {int(k) for k, _ in pick}
            hit = len(chosen & {int(r) for r in (cur.get("reds") or [])})
        else:  # blue_hot / blue_cold
            bcnt: Dict[int, int] = {}
            for d in hist:
                b = d.get("blue")
                if b is not None:
                    bcnt[int(b)] = bcnt.get(int(b), 0) + 1
            if not bcnt:
                return None
            ordered_b = sorted(bcnt.items(), key=lambda kv: (-kv[1], kv[0]))
            pick_b = ordered_b[0][0] if strategy == "blue_hot" else ordered_b[-1][0]
            hit = 1.0 if cur.get("blue") is not None and int(cur["blue"]) == pick_b else 0.0
        rows.append(float(hit))
    return rows


@app.get("/api/rigor/signal")
def rigor_signal(strategy: str = Query("hot"), window: int = Query(100, ge=5, le=1000),
                 alpha: float = Query(0.05, gt=0, lt=1)):
    """策略显著性检验：该策略的历史表现能否与随机基线区分（含多重检验校正）。

    返回 adjusted_p / survives_correction —— survives_correction=false 意味着
    「看上去不错」的方法只是多重比较下的幸存者偏差，不构成能力证据。
    """
    strategy = (strategy or "").strip().lower()
    if strategy not in RIGOR_STRATEGIES:
        return JSONResponse(
            {"ok": False, "error": f"未知策略 {strategy!r}",
             "supported": list(RIGOR_STRATEGIES)}, status_code=400)
    if strategy == "random":
        return JSONResponse({"ok": False, "error": "random 是基线，不是可检验策略"},
                            status_code=400)

    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "available": True,
                             "error": "本地暂无开奖数据"}, status_code=400)

    series = _strategy_hit_series(draws, strategy, window)
    if not series:
        return JSONResponse(
            {"ok": False, "available": True, "strategy": strategy, "window": window,
             "n_trials": 0,
             "error": "样本不足：可对照期数少于窗口长度，无法做显著性检验",
             "hint": f"需要至少 window+1 = {window + 1} 期数据"}, status_code=400)

    # 基线：红球策略期望命中 6*6/33；蓝球策略期望命中 1/16
    baseline = (6 * 6 / 33) if strategy in ("hot", "cold") else (1 / 16)
    n_trials = len(series)
    result, resp = _rigor_call_or_503(
        "strategy_significance",
        series, [baseline] * n_trials, n_trials, alpha)
    if resp is not None:
        return resp
    out = dict(result) if isinstance(result, dict) else {"raw": result}
    out.setdefault("ok", True)
    out.update({
        "available": True,
        "strategy": strategy,
        "window": int(window),
        "alpha": float(alpha),
        "n_trials": n_trials,
        "observed_mean": sum(series) / len(series),
        "baseline_mean": baseline,
        "sample_note": (f"基于 {n_trials} 期滚动对照；样本量不足时结论不成立，"
                        f"请以 adjusted_p 为准而非观察均值。"),
    })
    return out


# 单注 2 元（双色球官方单价，固定事实）
TICKET_PRICE = 2.0


def _hypergeom(k: int, K: int, n: int, N: int) -> float:
    """超几何分布概率：N 个球中 K 个命中类，抽 n 个恰有 k 个命中类。"""
    from math import comb
    if k < 0 or k > K or (n - k) < 0 or (n - k) > (N - K):
        return 0.0
    return comb(K, k) * comb(N - K, n - k) / comb(N, n)


def ticket_expected_prize() -> Dict:
    """单注期望奖金（元）与各奖级贡献拆解。

    按官方奖级规则与均匀随机假设精确计算：
      · 红中 6 + 蓝中 1 → 一等；红 6 蓝 0 → 二等；红 5 蓝 1 → 三等；
        红 5 蓝 0 或 红 4 蓝 1 → 四等；红 4 蓝 0 或 红 3 蓝 1 → 五等；蓝 1 → 六等。
      · 一等奖为浮动奖金，这里取 evaluate.PRIZE_CASH 的近似值并明确标注为估算。
    该值与「系统选什么号」**无关**——这正是要传达给用户的结论。
    """
    from . import evaluate
    N, K_red, n_red = 33, 6, 6
    p_red = {k: _hypergeom(k, K_red, n_red, N) for k in range(7)}
    p_blue_hit, p_blue_miss = 1 / 16, 15 / 16

    # 奖级概率（P(红=k) × P(蓝)）
    levels = {
        1: p_red[6] * p_blue_hit,
        2: p_red[6] * p_blue_miss,
        3: p_red[5] * p_blue_hit,
        4: p_red[5] * p_blue_miss + p_red[4] * p_blue_hit,
        5: p_red[4] * p_blue_miss + p_red[3] * p_blue_hit,
        6: (p_red[2] * p_blue_hit + p_red[1] * p_blue_hit + p_red[0] * p_blue_hit),
    }
    ev = 0.0
    breakdown = []
    for lvl in sorted(levels):
        p = levels[lvl]
        cash = float(evaluate.PRIZE_CASH.get(lvl, 0.0))
        contrib = p * cash
        ev += contrib
        breakdown.append({
            "level": lvl,
            "name": evaluate.PRIZE_NAME.get(lvl, f"{lvl} 等奖"),
            "probability": p,
            "prize_cash": cash,
            "expected_contribution": contrib,
            "prize_is_estimate": lvl in (1, 2),   # 一/二等奖浮动，取近似值
        })
    return {
        "expected_prize_per_ticket": ev,
        "hit_any_prize_probability": sum(levels.values()),
        "breakdown": breakdown,
    }


@app.get("/api/portfolio/expected-value")
def portfolio_expected_value(limit: int = Query(1, ge=1, le=20)):
    """当前推荐注组合的期望奖金分析（期望值为负是彩票的数学事实）。

    返回每注期望奖金、总投入、期望回报与 ROI；用于把一个「看起来能中奖」的
    组合摆回数学现实：长期期望回报率显著小于 1，且与选号方法无关。
    """
    tickets: List[Dict] = db.load_last_predictions() or []
    issue = None
    if tickets:
        issue = tickets[0].get("issue")
    elif hasattr(db, "recent_prediction_issues"):
        for iss in db.recent_prediction_issues(limit):
            got = db.load_predictions(iss)
            if got:
                tickets, issue = got, iss
                break

    price = TICKET_PRICE
    n_tickets = len(tickets)
    cost = price * n_tickets

    ev = ticket_expected_prize()
    ev_per_ticket = float(ev["expected_prize_per_ticket"])

    return {
        "ok": True,
        "available": True,
        "issue": issue,
        "n_tickets": n_tickets,
        "has_tickets": bool(n_tickets),
        "ticket_price": price,
        "total_cost": round(cost, 2),
        "expected_return_per_ticket": round(ev_per_ticket, 4),
        "expected_return_total": round(ev_per_ticket * n_tickets, 4),
        "expected_roi": round((ev_per_ticket - price) / price, 4),
        "hit_any_prize_probability": ev["hit_any_prize_probability"],
        "breakdown": ev["breakdown"],
        "sample_note": ("期望值按固定奖级与官方概率精确计算，与本系统历史命中**无关**；"
                        "彩票是负期望游戏，长期必亏是数学结论而非系统缺陷。"),
        "honesty": "双色球为独立随机事件，本系统不承诺中奖率，也不改变期望值。",
    }


# ---------- 页面 ----------



@app.get("/")
def index():
    resp = FileResponse(WEB_DIR / "index.html")
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


# ---------- 开奖日自动调度（可选，LOTT_SCHEDULER=1） ----------

def _scheduler_step():
    """开奖日（二/四/日）21:35 后：抓取 → 在线对照 → **跑一轮进化** → 生成下期预测。

    V5 起在生成预测之前先跑进化（``_evolution_step``），这样「找规律 → 实战验证 →
    用已验证规律预测」是同一个调度周期内完成的，而不是靠人工手动触发。
    """
    from . import backtest as BT, engine, evaluate
    now = __import__("datetime").datetime.now()
    if now.weekday() not in config.DRAW_WEEKDAYS:
        return
    if now.strftime("%H:%M") < config.DRAW_TIME:
        return
    try:
        info = data_fetcher.fetch_and_update()
        print("[scheduler] 数据已更新:", info.get("inserted_new"), "期")
    except Exception as e:  # noqa: BLE001
        print("[scheduler] 抓取失败:", e)
        return
    check_result = evaluate.online_check()
    from . import notify
    print("[scheduler] 通知结果:", notify.notify_after_check(check_result))

    # V5：每期开奖后自动跑一轮进化（失败不影响预测生成）
    try:
        _evolution_step()
    except Exception as e:  # noqa: BLE001
        print("[scheduler] 进化失败（不影响预测）:", e)

    # V10-02：前置注册判定（**唯一写者**的调用点，见 docs/V10_IMPROVEMENT_PLAN.md §3.3.5b）
    # 必须在快照刷新**之前**跑，否则本期已到期的实验要等下一期才生效（时序错误）。
    # 失败只降级、不中断（沿用 _evolution_step 的既有约定：判定失败只影响 usable 条数）。
    try:
        from . import hypothesis
        # issue=None → 由 evaluate_prereg 从库里读最近一期作为"当前期号"
        _prereg = hypothesis.evaluate_prereg()
        print("[scheduler] 前置注册判定:", {k: _prereg.get(k) for k in
                                            ("checked", "passed", "provisional", "failed",
                                             "invalidated", "n_tested_cumulative")})
    except Exception as e:  # noqa: BLE001
        print("[scheduler] 前置注册判定失败（不影响预测）:", e)

    # V7：生成预测前先重建规律快照，让「刚验证出来的规律」本期就能进入出号链路。
    # 失败只记录、不影响预测（快照是可选加速层，缺失时引擎照常出号）。
    try:
        from . import patterns_runtime
        print("[scheduler] 规律快照:", patterns_runtime.refresh(db.load_draws()))
    except Exception as e:  # noqa: BLE001
        print("[scheduler] 规律快照刷新失败（不影响预测）:", e)

    draws = db.load_draws()
    exp = BT.expected_issue(draws)
    if exp.get("expected_draws_missing"):
        print("[scheduler] 警告：数据源落后于开奖日历，缺", exp.get("missed_draws"), "期")
    issue = exp["issue"]
    if not db.load_predictions(issue):
        engine.predict_next(draws, use_llm=True)
        print("[scheduler] 已生成", issue, "预测")


def _evolution_step(limit: int = 0, use_llm: bool = True) -> dict:
    """每期开奖后自动跑的一轮自进化：挖掘 → 注册 → A/B 实战验证 → 裁决。

    为什么需要它：``evolve_once`` 只做「挖掘+注册+应用」，假说会永远停在
    ``proposed``（实测 9 条全是 proposed），因为**没有任何环节把它们送去做 A/B 验证**。
    本函数补上这一步——对每条 proposed 假说跑配对回放，把结果交给
    ``hypothesis.resolve_registry`` 裁决，假说才可能晋升 ``validated``（进而影响出号）
    或被 ``rejected``（退场）。

    成本控制：
    - ``limit`` 限制参与验证的历史期数（0=全部）；A/B 内部另有 600 期上限
    - ``use_llm`` 控制是否调用 LLM 提炼新假说（单次 1~2 分钟）
    - 整体失败只记录不抛出，绝不影响当期预测生成

    返回本轮摘要，便于调度日志与 ``/api/evolution/status`` 观测。
    """
    from . import battle, evolution, hypothesis

    draws = db.load_draws()
    if not draws:
        return {"ok": False, "reason": "本地暂无开奖数据"}
    if limit:
        draws = draws[-int(limit):]

    summary: dict = {"ok": True}

    # 1) 挖掘 + 注册新假说（LLM 或纯统计）
    mined = hypothesis.mine_hypotheses({}, draws, use_llm=use_llm)
    reg = hypothesis.register_hypotheses(mined.get("hypotheses") or [])
    summary["mined"] = {"stat": mined.get("stat_count"), "llm": mined.get("llm_count"),
                        "llm_used": mined.get("llm_used"), "llm_error": mined.get("llm_error")}
    summary["registered"] = reg

    # 1b) V6：把「规律工坊」里已验证过阶段的 LLM 规律也注册进假说库
    #     （只收 support/reject/regime_only，unmappable/noise 一律不入库）
    try:
        from . import llm_lab
        if llm_lab.load_claims().get("claims"):
            summary["llm_lab_registered"] = evolution.register_llm_lab_claims(draws)
    except Exception as e:  # noqa: BLE001
        summary["llm_lab_registered"] = {"error": f"{type(e).__name__}: {e}"}

    # 2) 对**全部** proposed 假说做实战验证 —— 这是它们唯一的晋升通道。
    #    V6：按假说类型分派验证口径。维度化的走 as-of 筛选条件回放（任何假说都有
    #    非平凡数字），其余走结构/方法权重的 A/B 配对回放。
    hyps = evolution._as_hyp_list(hypothesis.load_registry())
    proposed = [h for h in hyps if h.get("status") == "proposed"]
    results = []
    for h in proposed:
        act = h.get("action") if isinstance(h.get("action"), dict) else {}
        try:
            if act.get("type") in ("prefer_red_filter", "prefer_blue_filter"):
                results.append(evolution.verify_discovery_hypothesis(h, draws, start=200))
            else:
                results.append(evolution.verify_hypothesis_ab(
                    h, draws, n_tickets=5, start=max(300, len(draws) - 600)))
        except Exception as e:  # noqa: BLE001
            print(f"[evolution] 假说 {h.get('id')} 验证失败: {e}")
    summary["verified"] = len(results)

    # 3) 裁决：validated / rejected / 样本不足
    resolution = hypothesis.resolve_registry(results) if results else {}
    summary["resolution"] = {
        "validated": resolution.get("validated"),
        "rejected": resolution.get("rejected"),
        "insufficient": len(resolution.get("insufficient") or []),
        "unchanged": resolution.get("unchanged"),
    }

    # 3b) V6：阶段时效标注（每条假说带「现在是否还有效」）
    try:
        summary["regime_annotated"] = evolution.annotate_hypotheses(draws)
    except Exception as e:  # noqa: BLE001
        summary["regime_annotated"] = {"error": f"{type(e).__name__}: {e}"}

    # 4) 报告当前生效的出号策略覆盖项
    st = evolution.evolution_status()
    summary["by_status"] = st.get("by_status")
    summary["active_overrides"] = st.get("active_overrides")
    print("[evolution] 本轮完成:", summary["resolution"], "| 库:", summary["by_status"])
    return summary


def _scheduler_loop():
    import time as _time
    while True:
        try:
            _scheduler_step()
        except Exception as e:  # noqa: BLE001
            print("[scheduler] 异常:", e)
        _time.sleep(1800)


# ---------- V5：战斗回放 + 自进化（历史规律 → 实战逐期验证 → 自我改进） ----------
#
# 用户口径：准确性 = 基于历史数据找规律 → 用于实战 → 用实战结果对比。
# 这三个端点把该口径落地为可查询的实战成绩单，而不是理论推演：
#   · /api/battle/report   全方法在 N 期真实开奖上的逐期战绩 + 两两显著性
#   · /api/battle/compare  只要对比结论（前端卡片用）
#   · /api/evolution/status 假说库与进化进度
#
# battle / hypothesis / evolution 三模块并行开发，任一未就绪都必须降级而非 500。

def _battle_or_503():
    """导入 battle 模块；不可用时返回 (None, 503 响应)。"""
    try:
        from . import battle
    except ImportError as e:
        return None, JSONResponse(
            {"ok": False, "available": False,
             "error": "战斗回放模块 lottery.battle 尚未就绪",
             "detail": str(e), "hint": "该端点需 lottery/battle.py；其余功能不受影响。"},
            status_code=503)
    return battle, None


# ---------- V6：阶段有效性（regime）+ 多维度规律发现 + LLM 规律工坊 ----------

@app.get("/api/regime/overview")
def regime_overview(limit: int = Query(0, ge=0, le=3490),
                    era_mode: Optional[str] = Query(None),
                    era_block: int = Query(0, ge=0, le=600)):
    """阶段时效总览：各阶段边界 + 全部规律的阶段画像 + 状态机分布。

    回答用户最关心的两个问题：**规律在哪段时间有效**、**现在还灵不灵**。
    """
    try:
        from . import evolution, hypothesis, regime
    except ImportError as e:
        return JSONResponse({"ok": False, "available": False,
                             "error": "阶段时效模块尚未就绪", "detail": str(e)},
                            status_code=503)
    draws = db.load_draws()
    if limit:
        draws = draws[-int(limit):]
    if not draws:
        return JSONResponse({"ok": False, "error": "本地暂无开奖数据"}, status_code=400)
    hyps = evolution._as_hyp_list(hypothesis.load_registry())
    items = []
    for h in hyps:
        rep = h.get("regime") or {}
        items.append({
            "id": h.get("id"), "family": h.get("family"),
            "statement": h.get("statement"), "status": h.get("status"),
            "action_type": (h.get("action") or {}).get("type"),
            "regime_status": rep.get("status"),
            "regime_label": rep.get("label_zh"),
            "regime_reason": rep.get("reason_zh"),
            "recent_effect": rep.get("effect"),
            "p_adjusted_recent": rep.get("p_adjusted_recent"),
            "n_sig_eras": rep.get("n_sig_eras"),
            "last_significant_era": rep.get("last_significant_era"),
        })
    counts: Dict[str, int] = {}
    for it in items:
        k = it.get("regime_status") or "unannotated"
        counts[k] = counts.get(k, 0) + 1
    return {
        "ok": True,
        "eras": regime.eras_for(draws, mode=(era_mode or regime.ERA_MODE),
                                block=(era_block or None)),
        "era_mode": (era_mode or regime.ERA_MODE),
        "era_block": regime.ERA_BLOCK if (era_mode or regime.ERA_MODE) == "adaptive" else None,
        "era_modes": {
            "adaptive": {"label": "自适应（按固定期数）",
                         "block": regime.ERA_BLOCK,
                         "note": "按 150 期一段切分：单段样本更大、检测力更高；近年与早年同等对待"},
            "calendar": {"label": "按自然年（9 段）",
                         "note": "2003-2008/…/2026；近年单段仅 118~151 期，检测力低，仅供对照"},
        },
        "era_defs": [{"name": n, "start_year": lo, "end_year": hi}
                     for n, lo, hi in regime.ERA_DEFS],
        "recent_window": regime.RECENT_WINDOW,
        "stale_after_draws": regime.STALE_AFTER_DRAWS,
        "min_era_n": regime.MIN_ERA_N,
        "status_counts": counts,
        "status_legend": {k: regime_status_label(k) for k in
                          list(counts.keys()) + list(REGIME_STATUS_ORDER)},
        "items": items,
        "n_draws": len(draws),
        "note_zh": ("状态机口径：每个规律被切成 9 个阶段（近年更细），每阶段做显著性与"
                    "BH 校正；『陈旧』= 最近一次显著距今超过 "
                    f"{regime.STALE_AFTER_DRAWS} 期，正是「规律不再生效」的量化形态。"),
    }


REGIME_STATUS_ORDER = ["persistent", "emerging", "fading", "stale", "flipped",
                       "reversed", "noise", "no_dimension", "insufficient", "error"]


def regime_status_label(status: str) -> str:
    return {
        "persistent": "持续有效（最近阶段仍显著）",
        "emerging": "新兴线索（只有最近阶段显著，未确认）",
        "fading": "正在衰减（上一阶段有效，最近失效）",
        "stale": "陈旧（曾显著，已被新数据推翻）",
        "flipped": "方向反转（疑似伪规律）",
        "reversed": "已失效（仅早期阶段显著）",
        "noise": "未检出（各阶段均不显著）",
        "no_dimension": "无可回放维度（结构类/权重类）",
        "insufficient": "样本不足",
        "error": "阶段分析失败",
        "unannotated": "尚未标注（点「重算阶段时效」）",
    }.get(status or "", status or "")


@app.post("/api/regime/refresh")
def regime_refresh(start: int = Query(200, ge=60, le=3000)):
    """重算全部假说的阶段时效标注（写回 data/hypotheses.json）。"""
    try:
        from . import evolution
    except ImportError as e:
        return JSONResponse({"ok": False, "error": "阶段时效模块尚未就绪",
                             "detail": str(e)}, status_code=503)
    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "error": "本地暂无开奖数据"}, status_code=400)
    try:
        r = evolution.annotate_hypotheses(draws, start=int(start))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "标注失败",
                             "detail": f"{type(e).__name__}: {e}"}, status_code=503)
    return {"ok": True, **r}


@app.get("/api/discovery/families")
def discovery_families():
    """维度表：可搜索的规律空间（20+ 族、90+ 组参数）。"""
    try:
        from . import discovery
    except ImportError as e:
        return JSONResponse({"ok": False, "error": "维度模块尚未就绪",
                             "detail": str(e)}, status_code=503)
    fams = discovery.family_catalog()
    dims: Dict[str, int] = {}
    for f in fams:
        dims[f["dim_zh"]] = dims.get(f["dim_zh"], 0) + f["n_param_sets"]
    return {"ok": True, "families": fams, "n_families": len(fams),
            "n_combos": discovery.combo_space_size(), "dims": dims}


@app.get("/api/discovery/scan")
def discovery_scan(top: int = Query(12, ge=1, le=60),
                   fast_window: int = Query(600, ge=200, le=3000),
                   families: Optional[str] = Query(None),
                   refresh: int = Query(0, ge=0, le=1)):
    """多维度 as-of 扫描：枚举维度×参数，给每条候选做阶段画像。

    严格 as-of：第 t 期只用 draws[:t] 计算筛选条件；扫描的搜索成本（已测组合数、
    纯随机预期假阳性数）一并返回，避免把「搜出来的」当「发现的」。
    """
    try:
        from . import discovery
    except ImportError as e:
        return JSONResponse({"ok": False, "error": "维度模块尚未就绪",
                             "detail": str(e)}, status_code=503)
    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "error": "本地暂无开奖数据"}, status_code=400)
    flist = [x.strip() for x in families.split(",") if x.strip()] if families else None
    try:
        res = discovery.discover(draws, top=int(top), fast_window=int(fast_window),
                                 families=flist, use_cache=not bool(refresh))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "扫描失败",
                             "detail": f"{type(e).__name__}: {e}"}, status_code=503)
    out = dict(res)
    out["ok"] = True
    out["n_draws"] = len(draws)
    return out


def _llm_lab_modules():
    from . import discovery, llm_lab
    return discovery, llm_lab


@app.get("/api/llm_lab/latest")
def llm_lab_latest(limit: int = Query(40, ge=1, le=200)):
    """最近一次规律工坊的结果（LLM 规律 + 逐条落地验证 + 阶段画像）。"""
    try:
        _, llm_lab = _llm_lab_modules()
    except ImportError as e:
        return JSONResponse({"ok": False, "available": False,
                             "error": "LLM 规律工坊模块尚未就绪", "detail": str(e)},
                            status_code=503)
    data = llm_lab.load_claims()
    claims = (data.get("claims") or [])[:int(limit)]
    counts: Dict[str, int] = {}
    for c in claims:
        k = c.get("verdict") or "unknown"
        counts[k] = counts.get(k, 0) + 1
    return {"ok": True, "available": True, "updated_at": data.get("updated_at"),
            "runs": (data.get("runs") or [])[:5],
            "claims": claims, "verdict_counts": counts,
            "verdict_legend": llm_lab.VERDICTS,
            "n_stored": len(data.get("claims") or [])}


@app.post("/api/llm_lab/run")
def llm_lab_run(rounds: int = Query(3, ge=1, le=3),
                use_llm: int = Query(1, ge=0, le=1),
                persist: int = Query(1, ge=0, le=1)):
    """跑一次规律工坊：维度扫描 → 多轮 LLM 提规律/自我质疑/修订 → 逐条落地验证 → 落盘。

    耗时：LLM 三轮各 1~3 分钟（推理型模型更久），维度扫描约 5~10 秒。
    ``use_llm=0`` 时只跑维度扫描与阶段画像（用于无 LLM 环境自检）。
    """
    try:
        from . import discovery, llm_lab
    except ImportError as e:
        return JSONResponse({"ok": False, "available": False,
                             "error": "LLM 规律工坊模块尚未就绪", "detail": str(e)},
                            status_code=503)
    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "error": "本地暂无开奖数据"}, status_code=400)
    try:
        res = llm_lab.run_lab(draws, rounds=int(rounds), use_llm=bool(use_llm),
                              persist=bool(persist))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "工坊执行失败",
                             "detail": f"{type(e).__name__}: {e}"}, status_code=503)
    res.setdefault("ok", True)
    return res


@app.post("/api/llm_lab/register")
def llm_lab_register():
    """把工坊里 verified 的规律注册进假说库（unmappable/noise 不入库）。"""
    try:
        from . import evolution
    except ImportError as e:
        return JSONResponse({"ok": False, "error": "进化模块尚未就绪",
                             "detail": str(e)}, status_code=503)
    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "error": "本地暂无开奖数据"}, status_code=400)
    try:
        r = evolution.register_llm_lab_claims(draws)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "注册失败",
                             "detail": f"{type(e).__name__}: {e}"}, status_code=503)
    return {"ok": True, **r}


@app.get("/api/backtest/regime")
def backtest_regime(key: str = Query(..., min_length=1),
                    params: Optional[str] = Query(None),
                    start: int = Query(200, ge=60, le=3000)):
    """任意维度组合的**检索式核验**：as-of 回放 + 阶段时效（前端「核验」按钮用）。

    ``params`` 是 JSON 字符串，例如 ``{"window":30,"k":6}``。
    """
    try:
        from . import discovery
    except ImportError as e:
        return JSONResponse({"ok": False, "error": "维度模块尚未就绪",
                             "detail": str(e)}, status_code=503)
    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "error": "本地暂无开奖数据"}, status_code=400)
    try:
        p = json.loads(params) if params else (discovery.FAMILY_BY_KEY.get(key) or {}).get("params", [{}])[0]
    except (json.JSONDecodeError, TypeError):
        return JSONResponse({"ok": False, "error": f"params 不是合法 JSON：{params}"},
                            status_code=400)
    try:
        flt = discovery.make_filter(key, p or {})
        rep = discovery.regime_of_filter(draws, flt, start=int(start))
    except KeyError as e:
        return JSONResponse({"ok": False, "error": f"未知维度：{e}"}, status_code=400)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "回放失败",
                             "detail": f"{type(e).__name__}: {e}"}, status_code=503)
    return {"ok": True, "filter": {"key": key, "params": p, "label": flt["label"],
                                   "target": flt["target"]}, "regime": rep}


@app.get("/api/rules/runtime")
def rules_runtime():
    """规律运行时快照：现在到底有哪些规律「可用」、哪些只监控、哪些已否决。"""
    try:
        from . import patterns_runtime
    except ImportError as e:
        return JSONResponse({"ok": False, "available": False,
                             "error": "规律运行时模块尚未就绪", "detail": str(e)},
                            status_code=503)
    draws = db.load_draws()
    current = draws[-1]["issue"] if draws else None
    snap = patterns_runtime.load_snapshot(current_issue=current)
    if not snap:
        return {"ok": True, "available": False, "stale": True,
                "note_zh": "尚未生成规律快照（点「刷新规律快照」或等调度器刷新）"}
    return {"ok": True, "available": True,
            "apply_enabled": patterns_runtime.apply_enabled(),
            "current_issue": current, **{k: snap.get(k) for k in
                                         ("generated_at_text", "base_issue", "n_draws",
                                          "counts", "scan", "eras", "stale", "age_text")},
            "rules": snap.get("rules") or []}


@app.post("/api/rules/refresh")
def rules_refresh():
    """重算规律运行时快照（阶段时效 + 维度扫描 + 假说库 → 快照文件，约 5~20 秒）。"""
    try:
        from . import patterns_runtime
    except ImportError as e:
        return JSONResponse({"ok": False, "error": "规律运行时模块尚未就绪",
                             "detail": str(e)}, status_code=503)
    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "error": "本地暂无开奖数据"}, status_code=400)
    try:
        r = patterns_runtime.refresh(draws)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "刷新失败",
                             "detail": f"{type(e).__name__}: {e}"}, status_code=503)
    return {"ok": True, **r}


@app.post("/api/rules/closed-loop")
def rules_closed_loop(rounds: int = Query(3, ge=1, le=3),
                      use_llm: int = Query(1, ge=0, le=1)):
    """**AI 规律闭环**：挖掘 → 验证 → 注册 → 刷新规律快照（供出号使用）。

    这是用户诉求「用 AI 挖掘规律、验证规律、使用规律」的一键入口：
    LLM 三轮提规律并自我质疑 → 逐条 as-of 回放 + 阶段画像 → 注册假说库 →
    重建规律快照 → 下一期预测自动消费（LOTT_RULE_APPLY=1 时真正影响号码，
    默认影子模式只记录影响）。耗时 1~8 分钟（含 LLM）。
    """
    try:
        from . import llm_lab
    except ImportError as e:
        return JSONResponse({"ok": False, "error": "LLM 规律工坊模块尚未就绪",
                             "detail": str(e)}, status_code=503)
    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "error": "本地暂无开奖数据"}, status_code=400)
    try:
        res = llm_lab.run_closed_loop(draws, rounds=int(rounds), use_llm=bool(use_llm))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "闭环执行失败",
                             "detail": f"{type(e).__name__}: {e}"}, status_code=503)
    return res


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

    threading.Thread(target=_run, daemon=True, name=f"bg-{tid}").start()
    return tid


@app.post("/api/rules/closed-loop/async")
def rules_closed_loop_async(rounds: int = Query(3, ge=1, le=3),
                            use_llm: int = Query(1, ge=0, le=1)):
    """**AI 规律闭环（后台任务版）**：立即返回 task_id，前端轮询进度。

    闭环内容与 ``/api/rules/closed-loop`` 相同（挖掘→验证→注册→刷新快照），
    但不再让浏览器干等：每完成一个阶段就更新任务的 progress/message。
    """
    try:
        from . import llm_lab
    except ImportError as e:
        return JSONResponse({"ok": False, "error": "LLM 规律工坊模块尚未就绪",
                             "detail": str(e)}, status_code=503)
    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "error": "本地暂无开奖数据"}, status_code=400)
    tid = _bg_run("closed_loop",
                  lambda p: llm_lab.run_closed_loop(draws, rounds=int(rounds),
                                                    use_llm=bool(use_llm), progress=p))
    return {"ok": True, "task_id": tid,
            "note_zh": "AI 规律闭环已在后台运行（约 1~8 分钟），可离开本页，完成后自动刷新"}


@app.post("/api/llm_lab/run/async")
def llm_lab_run_async(rounds: int = Query(3, ge=1, le=3),
                      use_llm: int = Query(1, ge=0, le=1)):
    """LLM 规律工坊（后台任务版），与 ``/api/llm_lab/run`` 同逻辑。"""
    try:
        from . import llm_lab
    except ImportError as e:
        return JSONResponse({"ok": False, "error": "LLM 规律工坊模块尚未就绪",
                             "detail": str(e)}, status_code=503)
    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "error": "本地暂无开奖数据"}, status_code=400)
    tid = _bg_run("llm_lab",
                  lambda p: llm_lab.run_lab(draws, rounds=int(rounds),
                                            use_llm=bool(use_llm), progress=p))
    return {"ok": True, "task_id": tid,
            "note_zh": "LLM 规律工坊已在后台运行（约 1~8 分钟），可离开本页"}


@app.get("/api/battle/report")
def battle_report(limit: int = Query(0, ge=0, le=3490),
                  n_tickets: int = Query(10, ge=1, le=20),
                  start: int = Query(300, ge=60, le=3000),
                  methods: Optional[str] = Query(None),
                  seed: int = Query(42)):
    """方法级 walk-forward 实战回放：各出号方法在真实开奖上的逐期战绩。

    limit=0 用全部历史（默认；计算量大，前端可按需传小值）。
    严格 walk-forward：评估第 t 期只用 draws[:t]，不泄漏未来信息。

    methods 逗号分隔可限定方法集（如 `uniform,stat:freq,blue_coverage`）以缩短耗时。
    """
    battle, resp = _battle_or_503()
    if resp is not None:
        return resp
    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "available": True,
                             "error": "本地暂无开奖数据"}, status_code=400)
    if limit:
        draws = draws[-int(limit):]
    if len(draws) <= start:
        return JSONResponse({"ok": False, "available": True,
                             "error": f"样本不足：{len(draws)} 期 ≤ start={start}"},
                            status_code=400)
    mlist = [m.strip() for m in methods.split(",") if m.strip()] if methods else None
    try:
        report = battle.battle_report(draws, n_tickets=int(n_tickets),
                                      start=int(start), seed=int(seed))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "available": False,
                             "error": "battle_report() 执行失败",
                             "detail": f"{type(e).__name__}: {e}"}, status_code=503)
    if not isinstance(report, dict):
        return JSONResponse({"ok": False, "available": False,
                             "error": "battle_report() 返回结构异常"}, status_code=503)
    out = dict(report)
    out.setdefault("ok", True)
    out["available"] = True
    out["n_draws"] = len(draws)
    # 全量回放耗时较长；给出可复现的参数快照
    out["params"] = {"limit": limit, "n_tickets": n_tickets,
                     "start": start, "seed": seed, "methods": mlist}
    return out


@app.get("/api/evolution/status")
def evolution_status_endpoint():
    """自进化进度：假说库统计 + 当前生效的出号策略覆盖项。

    只展示 ``validated`` 的假说为「生效」——未经验证的假说不得影响真实出号。
    """
    try:
        from . import evolution
    except ImportError as e:
        return JSONResponse({"ok": False, "available": False,
                             "error": "自进化模块 lottery.evolution 尚未就绪",
                             "detail": str(e)}, status_code=503)
    try:
        st = evolution.evolution_status()
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "available": False,
                             "error": "evolution_status() 执行失败",
                             "detail": f"{type(e).__name__}: {e}"}, status_code=503)
    st = dict(st or {})
    st.setdefault("ok", True)
    return st


@app.post("/api/evolution/run")
def evolution_run(limit: int = Query(0, ge=0, le=3490),
                  n_tickets: int = Query(10, ge=1, le=20),
                  start: int = Query(300, ge=60, le=3000),
                  use_llm: int = Query(1, ge=0, le=1),
                  seed: int = Query(42)):
    """跑一轮**完整**进化：挖掘 → 注册 → A/B 实战验证 → 裁决 → 应用已验证规律。

    这是「自我完善、自我进化」的手动触发点，与调度器每期自动跑的
    ``_evolution_step`` 走同一条路径（避免两处逻辑漂移）。

    耗时较长：默认含 LLM 提炼（1~2 分钟）+ 对每条 proposed 假说做 600 期配对 A/B 验证。
    返回本轮摘要：挖到几条、注册几条、验证几条、裁决结果（validated/rejected/unchanged）、
    以及当前生效的出号策略覆盖项。
    """
    for mod in ("evolution", "hypothesis", "battle"):
        try:
            __import__(f"lottery.{mod}")
        except ImportError as e:
            return JSONResponse({"ok": False, "available": False,
                                 "error": f"自进化模块 lottery.{mod} 尚未就绪",
                                 "detail": str(e)}, status_code=503)
    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "available": True,
                             "error": "本地暂无开奖数据"}, status_code=400)
    try:
        result = _evolution_step(limit=int(limit), use_llm=bool(use_llm))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "available": False,
                             "error": "_evolution_step() 执行失败",
                             "detail": f"{type(e).__name__}: {e}"}, status_code=503)
    result = dict(result or {})
    result.setdefault("ok", True)
    # 附上当前进化状态，便于前端一次拿到完整画面
    try:
        from . import evolution as _evo
        result["status"] = _evo.evolution_status()
    except Exception:  # noqa: BLE001
        pass
    return result


# M2：后台预热 ML 模型（首个预测请求不阻塞；训练结果按数据版本缓存）
try:
    from . import ml_model
    _boot_draws = db.load_draws()
    ml_model.start_ml_warmup(_boot_draws)
except Exception:  # noqa: BLE001
    pass

# V8：启动后异步刷新一次规律快照，保证「本期规律使用情况」不会因为镜像重建而读到陈旧快照。
# 失败只记录日志，绝不阻塞启动。
def _boot_refresh_rules_snapshot():
    import time as _t
    _t.sleep(20)                     # 让启动回测/预测先跑完，避免抢 IO
    try:
        from . import hypothesis, patterns_runtime
        _d = db.load_draws()
        if not _d:
            return
        # 假说库是运行时状态，重启/误清后会变成空库；空库会让规律链路"看起来
        # 什么都没分析"。启动时补一次统计挖掘（纯确定性、无 LLM、秒级），
        # 把重号/邻号/隔期/奇偶/三区这几类基线规律重新拉起来。
        try:
            _reg = hypothesis.load_registry()
            if not _reg.get("hypotheses"):
                _mined = hypothesis.mine_hypotheses({}, _d, use_llm=False)
                _reg_res = hypothesis.register_hypotheses(_mined.get("hypotheses") or [])
                print("[rules] 假说库为空，已补跑统计挖掘:", _reg_res.get("registered"))
                evolution_annotate = None
                try:
                    from . import evolution as _evo
                    evolution_annotate = _evo.annotate_hypotheses(_d)
                except Exception:  # noqa: BLE001
                    evolution_annotate = None
                if evolution_annotate is not None:
                    print("[rules] 阶段标注:", evolution_annotate.get("annotated"))
        except Exception as e:  # noqa: BLE001
            print("[rules] 补跑统计挖掘失败（不影响出号）:", e)
        r = patterns_runtime.refresh(_d)
        print("[rules] 启动后规律快照刷新:", (r.get("snapshot") or {}).get("counts"))
    except Exception as e:  # noqa: BLE001
        print("[rules] 启动后规律快照刷新失败（不影响出号）:", e)


try:
    import threading as _threading
    _threading.Thread(target=_boot_refresh_rules_snapshot, daemon=True,
                      name="boot-rules-refresh").start()
except Exception:  # noqa: BLE001
    pass

if config.SCHEDULER_ENABLED:
    import threading
    threading.Thread(target=_scheduler_loop, daemon=True).start()