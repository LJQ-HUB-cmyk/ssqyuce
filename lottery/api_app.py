"""FastAPI 应用：REST API + 静态单页前端。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

class _NoCacheStaticFiles(StaticFiles):
    """静态资源禁用缓存，确保前端更新后浏览器立即拿到新版。"""
    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-store, max-age=0"
        return resp

from . import config, data_fetcher, db
from . import features as F, diagnose as D, mining as M

app = FastAPI(title="双色球智能预测分析系统", version="0.5.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
STATIC_DIR = WEB_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/static", _NoCacheStaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------- 数据 ----------

@app.get("/api/health")
def health():
    return {"status": "ok", "issues": db.count_draws(), "max_issue": db.max_issue()}


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
            regenerate: bool = False):
    """生成下一期预测；目标期已有预测且未要求重新生成时复用缓存。"""
    from . import backtest as BT, engine
    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "error": "本地暂无开奖数据，请先刷新"}, status_code=400)
    issue = BT.next_issue(draws[-1]["issue"])
    if not regenerate:
        existing = db.load_predictions(issue)
        if existing:
            return {"issue": issue, "tickets": existing, "from_cache": True,
                    "llm_used": any(t["method"] == "llm" for t in existing)}
    res = engine.predict_next(draws, use_llm=use_llm, n_tickets=n_tickets)
    res["from_cache"] = False
    return res


@app.get("/api/predictions/last")
def last_predictions():
    issue = None
    rows = db.get_conn().execute(
        "SELECT issue FROM predictions ORDER BY issue DESC LIMIT 1").fetchone()
    if rows:
        issue = rows["issue"]
    return {"issue": issue, "tickets": db.load_predictions(issue) if issue else []}


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


@app.post("/api/eval/online")
def eval_online():
    from . import evaluate
    return evaluate.online_check()


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
def run_mining(min_start: int = Query(300, ge=120, le=1000)):
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
        result = M.run_mining(draws, min_start=min_start, save_to_db=True)
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
    from . import config
    return {
        "disabled": config.LLM_DISABLED,
        "base_url": config.LLM_BASE_URL,
        "model": config.LLM_MODEL,
        "samples": config.LLM_SAMPLES,
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
        if config.LLM_MODEL not in config.LLM_MODEL_LIST:
            config.LLM_MODEL_LIST = [config.LLM_MODEL] + config.LLM_MODEL_LIST
    if "samples" in payload:
        try:
            config.LLM_SAMPLES = max(1, min(20, int(payload["samples"])))
        except (TypeError, ValueError):
            config.LLM_SAMPLES = 3
        os.environ["LOTT_LLM_SAMPLES"] = str(config.LLM_SAMPLES)
    if "disabled" in payload:
        config.LLM_DISABLED = bool(payload["disabled"])
        os.environ["LOTT_LLM_DISABLED"] = "1" if config.LLM_DISABLED else "0"

    # 持久化到数据目录（挂载卷，容器重启不丢失）
    config.LLM_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    conf = {
        "disabled": config.LLM_DISABLED,
        "base_url": config.LLM_BASE_URL or "",
        "api_key": config.LLM_API_KEY or "",
        "model": config.LLM_MODEL or "",
        "samples": config.LLM_SAMPLES,
    }
    try:
        config.LLM_CONFIG_FILE.write_text(
            json.dumps(conf, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        return {"ok": False, "error": f"配置写入失败: {e}"}
    return {"ok": True, "configured": config.llm_configured()}



@app.post("/api/llm/test")
def test_llm_connection():
    """用当前配置发起一次最小对话，验证 LLM 通道连通性。"""
    from . import config, llm_client
    import time
    if config.LLM_DISABLED:
        return {"ok": False, "error": "LLM 已停用（在设置中启用后重试）"}
    cfgs = config.llm_model_list()
    if not cfgs:
        return {"ok": False, "error": "LLM 未配置（请先填写 API 地址 / Key / 模型并保存）"}
    cfg = cfgs[0]
    t0 = time.time()
    try:
        text = llm_client.chat(
            "你是连接测试助手。", "请只回复：连接成功",
            max_tokens=50, temperature=0.0, timeout=25, model_cfg=cfg)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"调用异常: {e}"}
    dt_ms = int((time.time() - t0) * 1000)
    if text:
        return {"ok": True, "time_ms": dt_ms, "reply": text.strip()[:100]}
    return {"ok": False, "error": "模型无返回（请检查 API 地址 / Key / 模型名）"}


# ---------- 页面 ----------



@app.get("/")
def index():
    resp = FileResponse(WEB_DIR / "index.html")
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


# ---------- 开奖日自动调度（可选，LOTT_SCHEDULER=1） ----------

def _scheduler_step():
    """开奖日（二/四/日）21:35 后：抓取 → 在线对照 → 生成下期预测。"""
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
    evaluate.online_check()
    draws = db.load_draws()
    issue = BT.next_issue(draws[-1]["issue"])
    if not db.load_predictions(issue):
        engine.predict_next(draws, use_llm=True)
        print("[scheduler] 已生成", issue, "预测")


def _scheduler_loop():
    import time as _time
    while True:
        try:
            _scheduler_step()
        except Exception as e:  # noqa: BLE001
            print("[scheduler] 异常:", e)
        _time.sleep(1800)


if config.SCHEDULER_ENABLED:
    import threading
    threading.Thread(target=_scheduler_loop, daemon=True).start()