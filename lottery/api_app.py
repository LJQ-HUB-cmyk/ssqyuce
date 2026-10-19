"""FastAPI 应用：REST API + 静态单页前端。"""
from __future__ import annotations

import json
import hmac
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

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

app = FastAPI(
    title="双色球智能预测分析系统",
    version=config.APP_VERSION,
    # W01：文档端点默认关闭。`/docs` `/redoc` `/openapi.json` **不在 `/api/` 前缀下**，
    # 天然绕过 `optional_api_auth`，是当前生产最容易被忽略的暴露面（实测公网 200）。
    # 本地开发需要时用 `LOTT_API_DOCS=1` 打开。
    docs_url="/docs" if config.API_DOCS_ENABLED else None,
    redoc_url="/redoc" if config.API_DOCS_ENABLED else None,
    openapi_url="/openapi.json" if config.API_DOCS_ENABLED else None,
)
# W01：CORS 收敛到显式白名单（原先 `["*"]`）。前端 `app.js` 用相对路径 fetch，
# 属同源请求、本不依赖 CORS；放开 `*` 只会扩大第三方页面的跨域读取面。
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(config.CORS_ORIGINS),
    allow_methods=["*"],
    allow_headers=["*"],
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

def _strict_mode_reject_response(draws, issue):
    """V11 严格模式门控：出号必须由 LLM 分析规律完成。

    返回 None（放行）或 JSONResponse（拒绝出号，422）。
    **必须在缓存检查之前调用**：严格模式下已有预测缓存也必须重新校验，
    不允许用旧缓存绕过门控（2026-10-18 顺序缺陷修复）。

    两条硬性校验（任一不满足即拒绝，绝不静默降级到统计基线）：
      ① 可用规律（counts.usable）必须 > 0；
      ② LLM 出号通道必须可用（llm_can_pick=True）。
    """
    from . import methods as _METH
    _errs = []

    # ① L2 结构约束可用（V12-04 口径修正，裁决 R-2）：
    #    此前要求 counts.usable > 0（显著规律），但 3515 期实测 0 条显著，
    #    会让严格模式永远哑火。V12 起改用 **L2 结构层的组合恒等约束**
    #    （和值/跨度/三区/奇偶/连号）作为门控依据：它与 C(33,6) 理论枚举一致，
    #    可复现、不需要样本量。
    #
    #    口径纪律（V12-04 强制）：这门控只能命名为 **layer=="L2" 且 violation=0**，
    #    **不得**把它叫成「可用规律」——L1 的「可用规律」语义是「检出/未检出」，
    #    与 L2 的「约束满足率」不是同一种计数，混用正是「可用规律 0 vs 5 条」
    #    矛盾的来源。L2 的通过不意味着存在可预测下期的规律。
    #
    #    violation_count 的定义：用当前 ctx 对 C(33,6) 全空间做**抽样**
    #    校验约束本身自洽（全枚举由 tests/test_v12_shape_lock.py 锁定）；
    #    这里只要求 ctx 结构完整且可枚举出合法约束，因此 violation 恒为 0 的
    #    前提是 ctx 齐全 —— 缺字段时显式置 violation>0 并拒绝。
    try:
        _l2_layer = "L2"
        _violations = 0
        from . import shape_rules as _SR
        _sc = _SR.shape_constraint_ctx(draws)
        _required = ("sum_min", "sum_max", "span_min", "span_max",
                     "top_zones", "odd_range", "max_consecutive")
        _missing = [k for k in _required if _sc.get(k) is None]
        if _missing or not _sc.get("top_zones"):
            _violations = max(1, len(_missing))
        _ok_ctx = (_l2_layer == "L2" and _violations == 0)
    except Exception:                                    # noqa: BLE001
        _ok_ctx = False
        _violations = 1
    if not _ok_ctx:
        _errs.append(
            ("strategy",
             (f"严格模式下不出号：**L2 结构约束不可用**（layer={_l2_layer}，"
              f"violation_count={_violations}；组合恒等约束无法生成，"
              f"历史数据不足或异常）。注意：L2 可用只说明形态约束成立，"
              f"**不代表**存在可预测下期的规律。请确认历史数据齐全，"
              f"或关闭严格模式（LOTT_STRICT_MODE=0）。")))

    # ② LLM 通道必须可用
    _ch = _METH.resolve_channels()
    if not bool(_ch.get("llm_can_pick")):
        _errs.append(
            ("llm",
             ("严格模式下不出号：LLM 出号通道不可用（llm_can_pick=False）。"
              "请检查 LOTT_METHODS 是否包含 llm、LLM 网关（data/llm_config.json）"
              "是否可用。")))

    if _errs:
        return JSONResponse(
            {"ok": False, "strict_mode": True,
             "strict_gates": [e[0] for e in _errs],
             "error": "；".join(e[1] for e in _errs)},
            status_code=422)
    return None


@app.api_route("/api/predict", methods=["GET", "POST"])
def predict(use_llm: Optional[bool] = None,
            n_tickets: Optional[int] = Query(None),
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

    # V11 严格模式门控（**必须在缓存检查之前**）：无可用规律或 LLM 通道
    # 不可用时拒绝出号；通过校验则强制现算（regenerate=True），不允许
    # 用旧缓存绕过门控。这是 2026-10-18 修复的顺序缺陷。
    _strict_active = bool(getattr(config, "STRICT_MODE", False))
    if _strict_active:
        _rej = _strict_mode_reject_response(draws, issue)
        if _rej is not None:
            return _rej
        regenerate = True            # 严格模式强制现算，不走缓存
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
    # V10-11：技能关停（decision_disabled）只关「LLM 出号决策通道」，**不**关预测。
    # 关键区别（R3 G6 第 2 条）：走 LLM_DISABLED 时 engine 会因 llm_required=True
    # 抛 LLMChannelError → 503「预测失败」；这里改为**主动不请求 LLM 通道**，
    # 预测照常成功，只是不含该通道的注（`llm_used=false`、`decision_channel_disabled=true`）。
    _channels_now = METH.resolve_channels()
    _decision_disabled = bool(_channels_now.get("decision_disabled"))
    if _decision_disabled:
        use_llm = False
    # 出号注数：显式传参数 > 环境配置 > 默认 5（用户要求默认 5 注、可配置）
    _n_req = int(n_tickets) if n_tickets is not None else int(config.N_TICKETS)

    # 严格模式已在前置门控校验并强制 regenerate=True；这里补 use_llm 强制
    _strict_active = bool(getattr(config, "STRICT_MODE", False))
    _strict_notes = []
    if _strict_active:
        use_llm = True                                   # 严格模式强制走 LLM
        _strict_notes.append("严格模式：出号强制依赖 LLM + 规律，本次为现算（未走缓存）")
    try:
        res = engine.predict_next(draws, use_llm=use_llm, n_tickets=_n_req,
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
    # 让前端能明确解释「为什么勾了使用大模型却没有 LLM 注」
    res["decision_channel_disabled"] = _decision_disabled
    res["strict_mode"] = bool(getattr(config, "STRICT_MODE", False))
    if _strict_notes:
        res["strict_notes"] = _strict_notes
    if _decision_disabled:
        res["decision_channel_note_zh"] = (
            "LLM 决策通道已被自动关停（决策技能 Wilson 下界 ≤ 随机基线 0.5），"
            "本次预测**照常成功**，只是不含 LLM 通道的注。"
            "恢复入口：POST /api/llm/decision/restore。")
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


# ==========================================================================
# V10-10：前端三卡的数据接口（票单分布 / 前置注册 / ML 诊断 / LLM 对照臂）
#
# 【序列化纪律】以下所有端点返回体**不得含 numpy 标量/数组**（``np.float64``、
# ``np.int64``、``np.ndarray`` 都不是 JSON 可序列化类型，FastAPI 默认编码器会抛
# ``TypeError: Object of type float64 is not JSON serializable``）。底层统计函数
# （``ticket_distribution`` / ``method_breakdown`` / ``battle`` / ``ml_model``）
# 虽然已尽量返回原生类型，但新增字段随时可能漏 —— 故统一过一遍
# ``_jsonable()`` 做深拷贝+类型归一，**不信任**上游"应该是原生类型"。
# ==========================================================================

def _jsonable(obj: Any, _depth: int = 0) -> Any:
    """把返回体递归归一成 JSON 原生类型（numpy 标量 / dict 键 / None）。

    - ``np.generic``（float64/int64/bool_ …）→ ``.item()`` 转 Python 原生；
    - ``np.ndarray`` → ``.tolist()`` 后继续递归；
    - dict / list / tuple 递归处理，dict 的键统一转 ``str``；
    - 其它不可序列化对象 → ``repr()``（宁可显示成字符串，也不让端点 500）。
    """
    if _depth > 12:                                   # pragma: no cover - 纯防御
        return repr(obj)
    try:
        import numpy as _np
    except Exception:                                 # pragma: no cover - numpy 是硬依赖
        _np = None
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if _np is not None:
        if isinstance(obj, _np.generic):
            return obj.item()
        if isinstance(obj, _np.ndarray):
            return _jsonable(obj.tolist(), _depth + 1)
    if isinstance(obj, dict):
        return {str(k): _jsonable(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v, _depth + 1) for v in obj]
    return repr(obj)


@app.get("/api/portfolio/distribution")
def portfolio_distribution(issues: int = Query(1, ge=1, le=20),
                           exact: bool = Query(True)):
    """V10-10「🧾 票单分布」卡：当前推荐注组合的解析分布统计。

    数据源唯一：``engine.ticket_distribution()``（V10-03）。无预测时用固定示例票
    （2 注不重复号）以便前端永远能渲染一张完整的卡，但会明确标 ``source="sample"``。

    ``exact=True``（默认）：P(≥r红) 走 C(33,6) 全枚举（诚实口径，n=10 约 0.1~0.2s）；
    ``exact=False``：iid 近似（热路径/慢机器），返回体里 ``p_at_least_r_method``
    会变成 ``"iid_approx"``，前端据此提示"近似值不可当结论"。

    **诚实边界**：改变票单分布（冗余度/方差比）只改变命中数的**波动结构**，
    **不改变期望值** —— ``ev_ratio_sixth`` / ``ev_ratio_full`` 与选号无关，恒为常数。
    """
    from . import engine as E
    tickets: List[Dict] = db.load_last_predictions() or []
    issue = None
    source = "prediction"
    if tickets:
        issue = tickets[0].get("issue")
    else:
        # 无预测时的示例票：只为了让卡片有内容，不冒充真实推荐
        source = "sample"
        tickets = [
            {"reds": [1, 2, 3, 4, 5, 6], "blue": 1, "method": "sample"},
            {"reds": [7, 8, 9, 10, 11, 12], "blue": 2, "method": "sample"},
        ]
    try:
        dist = E.ticket_distribution(tickets, with_distribution_exact=bool(exact))
    except (ValueError, TypeError) as exc:
        return JSONResponse({"ok": False, "error": f"票单分布计算失败: {exc}"},
                            status_code=400)
    return _jsonable({
        "ok": True,
        "issue": issue,
        "source": source,
        "source_zh": ("当前推荐注" if source == "prediction" else "示例票（暂无预测）"),
        "exact": bool(exact),
        "distribution": dist,
        # 这三条是"诚实边界"，前端必须原样展示，不得省略：
        "honesty": {
            "distribution_changes_ev": False,
            "note_zh": ("改变票单分布（冗余度 / 方差 / 尾部概率）只改变命中数与奖金的"
                        "**波动结构**，不改变期望值：任何组合的返奖比都恒等于"
                        "ev_ratio_sixth / ev_ratio_full，与选号方法无关。"),
            "prize_assumption": dist.get("prize_assumption"),
        },
        "note_zh": "分布统计是组合结构事实，不是中奖预测；双色球为独立随机事件。",
    })


@app.get("/api/registry/status")
def registry_status(include_scan: bool = Query(False)):
    """V10-10「📋 前置注册」卡：已登记实验的计数 + 每条规则的 prereg 理由 + 累计检验数。

    数据源（只读，**不**跑 ``evaluate_prereg``，判定由调度器唯一写者负责）：
      1. ``hypothesis.load_registry()`` → 每条假说的 ``prereg_*`` 字段 + 库根 counters；
      2. ``patterns_runtime.load_snapshot()`` → 快照里每条规则的 ``prereg`` 子块
         （含 ``reason`` / ``divergence`` / ``n_tested_batch`` / ``n_tested_cumulative``）。
    ``include_scan=True`` 时把快照中来自维度扫描（无 prereg）的规则也列出来并标
    ``prereg_status=None``，默认只列已登记实验，避免把扫描线索误读成"路过注册"。
    """
    from . import hypothesis, patterns_runtime
    reg = hypothesis.load_registry() or {}
    hyps = reg.get("hypotheses") or []
    counters = reg.get("counters") if isinstance(reg.get("counters"), dict) else {}

    items: List[Dict] = []
    for h in hyps:
        if not isinstance(h, dict):
            continue
        status = h.get("prereg_status")
        if status is None and not include_scan:
            continue
        items.append({
            "exp_id": h.get("exp_id") or h.get("id"),
            "hyp_id": h.get("id"),
            "statement": h.get("statement"),
            "source": h.get("source") or "stat",
            "prereg_status": status,
            "prereg_ok": bool(h.get("prereg_ok")),
            "batch_id": h.get("batch_id"),
            "scope": h.get("scope"),
            "p_batch": h.get("p_batch"),
            "p_cumulative": h.get("p_cumulative"),
            "n_tested_batch": h.get("n_tested_batch"),
            "n_tested_cumulative": h.get("n_tested_cumulative"),
            "divergence": bool(h.get("divergence")),
            "reason": h.get("prereg_reason"),
            "evaluate_after_issue": h.get("evaluate_after_issue"),
            "frozen_until_issue": h.get("frozen_until_issue"),
            "hyp_status": h.get("status"),
        })
    items.sort(key=lambda x: (str(x.get("prereg_status") or "zz"), str(x.get("exp_id") or "")))

    counts = {s: 0 for s in hypothesis.PREREG_STATUSES}
    for it in items:
        st = str(it.get("prereg_status") or "")
        if st in counts:
            counts[st] += 1
    judged = sum(counts[s] for s in ("passed", "provisional", "failed", "invalidated"))

    snapshot = patterns_runtime.load_snapshot()
    snap_rules = []
    if snapshot:
        for r in (snapshot.get("rules") or []):
            pre = r.get("prereg") or {}
            if not pre and not include_scan:
                continue
            if not pre and include_scan:
                snap_rules.append({
                    "id": r.get("id"), "source": r.get("source"),
                    "prereg_status": None, "usable": bool(r.get("usable")),
                    "tier": r.get("tier"), "reason": "维度扫描线索，未登记前置实验",
                })
                continue
            snap_rules.append({
                "id": r.get("id"), "source": r.get("source"),
                "prereg_status": pre.get("status"),
                "prereg_ok": bool(pre.get("status") == "passed"),
                "exp_id": pre.get("exp_id"), "scope": pre.get("scope"),
                "p_batch": pre.get("p_batch"), "p_cumulative": pre.get("p_cumulative"),
                "n_tested_batch": pre.get("n_tested_batch"),
                "n_tested_cumulative": pre.get("n_tested_cumulative"),
                "divergence": bool(pre.get("divergence")),
                "reason": pre.get("reason"),
                "usable": bool(r.get("usable")), "tier": r.get("tier"),
            })

    return _jsonable({
        "ok": True,
        "counts": counts,
        "registered": counts.get("registered", 0) + counts.get("running", 0),
        "passed": counts.get("passed", 0),
        "provisional": counts.get("provisional", 0),
        "failed": counts.get("failed", 0),
        "invalidated": counts.get("invalidated", 0),
        "judged": judged,
        "n_total": len(items),
        "n_tested_cumulative": int(counters.get("n_tested_cumulative") or 0),
        "last_issue": counters.get("last_issue"),
        "min_batch_size": getattr(hypothesis, "MIN_BATCH_SIZE", None),
        "max_batch_size": getattr(hypothesis, "MAX_BATCH_SIZE", None),
        "items": items,
        "snapshot": {
            "available": bool(snapshot),
            "base_issue": (snapshot or {}).get("base_issue"),
            "generated_at": (snapshot or {}).get("generated_at"),
            "stale": bool((snapshot or {}).get("stale")),
            "counts": (snapshot or {}).get("counts"),
            "rules": snap_rules,
        },
        "note_zh": ("前置注册把「我找了 N 次」变成「我只检验了事先承诺的 M 条」："
                    "只有 passed（累计口径 Bonferroni 校正后 p<α）才允许影响出号；"
                    "provisional（批内显著但累计不显著）与 failed 一律不得使用。"),
    })


@app.get("/api/ml/diagnosis")
def ml_diagnosis(max_issues: int = Query(800, ge=100, le=3500),
                 max_numbers: int = Query(3, ge=1, le=12),
                 n_null: int = Query(20, ge=5, le=200),
                 colors: Optional[str] = Query(None),
                 force: bool = Query(False)):
    """V10-10「🤖 ML 诊断」卡：ML 通道是否可信（置换重要性 + 联合打乱标签零分布）。

    数据源唯一：``ml_model.diagnose_ml_channels()``（V10-08）。
    默认参数刻意比底层默认**更轻**（2 个通道 × 3 个号码 × 20 次零复制，约 10~30s），
    因为这是页面卡片的同步请求；``force=False`` 且 10 分钟内已有结果时直接返回缓存。

    **诚实边界**：真实彩票数据上 ``trusted=False`` 是**预期结论**（零假设为真，
    没有可学习的信号），不是系统故障 —— 返回体里 ``honesty_zh`` 字段必须原样展示。
    """
    import time as _time
    from . import ml_model

    colors_t = tuple(c.strip() for c in (colors or "red,blue").split(",") if c.strip())
    colors_t = tuple(c for c in colors_t if c in ("red", "blue")) or ("red", "blue")
    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "error": "本地暂无开奖数据"}, status_code=400)
    # 缓存键必须含**数据集指纹**：只按参数缓存的话，开奖数据更新后 10 分钟内
    # 仍会返回上一批数据算出的 trustworthy 结论（缓存把结论"钉死"在旧样本上）。
    dataset_fp = f"{len(draws)}:{db.max_issue()}"
    cache_key = [list(colors_t), int(max_issues), int(max_numbers), int(n_null), dataset_fp]
    now = _time.time()
    # ---- D-3：缓存落盘持久化 + 后台刷新 ----
    # 实测一次诊断：默认参数(3号码/800期/B=20) 39.0s，standard(B=79) 56.1s。
    # 每次零复制都要重训 VotingClassifier(RF+HistGB)，是纯 CPU 重活；
    # 进程内 dict 缓存重启即失效 → 重启后第一次打开页面要空等几十秒，
    # 且同步 HTTP 很可能先被网关超时掐断。改为：
    #   ① 缓存写盘（重启后仍可用）；② 命中未过期 → 直接返回；
    #   ③ 过期 → 先返回旧结论（标 stale）+ 后台线程刷新；
    #   ④ 从未算过 → 立即返回 202 + task_id，前端轮询 /api/tasks/{id}。
    cached = _load_ml_diag_cache()
    if (not force and cached and list(cached.get("key") or []) == cache_key):
        age = now - float(cached.get("at") or 0)
        if age < _ML_DIAG_TTL:
            out = dict(cached["value"])
            out["cached"] = True
            out["cache_age_s"] = round(age, 1)
            return _jsonable(out)
        if not _ML_DIAG_JOB.get("running"):
            _kick_ml_diag_bg(draws, colors_t, int(max_issues), int(max_numbers),
                             int(n_null), cache_key)
        out = dict(cached["value"])
        out["cached"] = True
        out["stale"] = True
        out["cache_age_s"] = round(age, 1)
        out["refreshing"] = bool(_ML_DIAG_JOB.get("running"))
        return _jsonable(out)
    # 轻量参数（实测 ~7s）仍同步返回，让单测/CLI/小规模页面无需改造成轮询
    light = (int(n_null) <= 20 and int(max_numbers) <= 2 and int(max_issues) <= 400)
    if light and not force:
        try:
            diag = ml_model.diagnose_ml_channels(
                draws, colors=colors_t, max_issues=int(max_issues),
                max_numbers=int(max_numbers), n_null=int(n_null))
        except Exception as exc:                      # noqa: BLE001 - 诊断失败不 500
            return JSONResponse({"ok": False, "error": f"ML 诊断失败: {exc}"},
                                status_code=200)
        payload = _ml_diag_payload(diag, draws, cache_key, colors_t,
                                   max_issues, max_numbers, n_null)
        _save_ml_diag_cache({"key": cache_key, "at": now, "value": payload})
        return _jsonable(payload)
    # 从未算过 / force：交给后台任务，立即返回
    if force or not _ML_DIAG_JOB.get("running"):
        tid = _kick_ml_diag_bg(draws, colors_t, int(max_issues), int(max_numbers),
                               int(n_null), cache_key)
        if tid:
            return JSONResponse({"ok": True, "pending": True, "task_id": tid,
                                 "note_zh": "诊断在后台计算（单次约 40~90 秒），"
                                            "请轮询 /api/tasks/{task_id} 获取结果"},
                                status_code=202, background=None)


# ML 诊断结果缓存（进程内，TTL 见下）。诊断一次 10~30s，页面刷新不该反复重算。
_ML_DIAG_TTL = 600.0
_ML_DIAG_CACHE: Dict[str, Any] = {}
# D-3：后台诊断任务的进程内状态（防重复触发）
_ML_DIAG_JOB: Dict[str, Any] = {"running": False, "task_id": None}


def _ml_diag_cache_path():
    """诊断缓存的落盘位置（与其它 runtime 状态同目录）。"""
    return config.DATA_DIR / "ml_diag_cache.json"


def _load_ml_diag_cache() -> Optional[Dict]:
    """读持久化缓存；进程内已有则优先用进程内的（更新更近）。

    为什么落盘（D-3）：一次诊断 40~90 秒，进程内 dict 重启即失效，
    会让"每次重启后第一次打开 ML 诊断卡"都要空等几十秒。
    """
    mem = _ML_DIAG_CACHE.get("result")
    if mem:
        return mem
    try:
        path = _ml_diag_cache_path()
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "key" in data:
            _ML_DIAG_CACHE["result"] = data
            return data
    except (ValueError, OSError):
        return None
    return None


def _save_ml_diag_cache(entry: Dict) -> None:
    """原子写盘（tmp + replace），失败只告警不影响返回。"""
    try:
        _ML_DIAG_CACHE["result"] = entry
        path = _ml_diag_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(entry, ensure_ascii=False, default=str), encoding="utf-8")
        os.replace(str(tmp), str(path))
    except (ValueError, OSError) as exc:  # noqa: BLE001
        print(f"[api] ML 诊断缓存写盘失败（不影响本次结果）: {exc}")


def _kick_ml_diag_bg(draws, colors_t, max_issues, max_numbers, n_null,
                     cache_key) -> Optional[str]:
    """把一次 ML 诊断丢进后台线程并登记 db 任务（D-3）。

    返回 task_id；已有任务在跑时返回 None（避免重复占用 CPU）。
    """
    if _ML_DIAG_JOB.get("running"):
        return None
    from . import ml_model

    def _work(progress):
        return ml_model.diagnose_ml_channels(
            draws, colors=colors_t, max_issues=int(max_issues),
            max_numbers=int(max_numbers), n_null=int(n_null))

    def _on_done(result):
        _ML_DIAG_JOB["running"] = False
        _ML_DIAG_JOB["task_id"] = None
        if isinstance(result, dict) and result.get("ok", True):
            payload = _ml_diag_payload(result, draws, cache_key, colors_t,
                                       max_issues, max_numbers, n_null)
            _save_ml_diag_cache({"key": cache_key, "at": time.time(), "value": payload})

    tid = _bg_run("ml_diagnosis", _work, on_done=_on_done)
    _ML_DIAG_JOB["running"] = True
    _ML_DIAG_JOB["task_id"] = tid
    return tid


def _ml_diag_payload(diag: Dict, draws, cache_key, colors_t,
                     max_issues, max_numbers, n_null) -> Dict:
    """把 diagnose_ml_channels 的结果整理成对外 payload（唯一构造点）。"""
    return {
        "ok": bool(diag.get("ok", True)),
        "trusted": bool(diag.get("trusted")),
        "tier": diag.get("tier"),
        "channel_tiers": diag.get("channel_tiers") or {},
        "channel_trusted": diag.get("channel_trusted") or {},
        "n_channels": diag.get("n_channels"),
        "n_channels_trusted": diag.get("n_channels_trusted"),
        "consistency_zh": diag.get("consistency_zh") or "",
        "advisory_only": bool(diag.get("advisory_only")),
        "advisory_warning_zh": diag.get("advisory_warning_zh") or "",
        "channels": diag.get("channels") or {},
        "params": {"max_issues": int(max_issues), "max_numbers": int(max_numbers),
                   "n_null": int(n_null), "colors": list(colors_t)},
        "n_draws": len(draws),
        "dataset_fingerprint": f"{len(draws)}:{db.max_issue()}",
        "cached": False,
        "honesty_zh": ("ML 通道的可信度用**真实开奖数据**上的置换重要性检验判定。"
                       "在纯随机数据（双色球即是）上 trusted=False 是**诚实且预期**的"
                       "结论：它说明模型没有学到可泛化的信号，不是故障、不是 bug，"
                       "也不代表模型代码有错。样本不足时「不显著」同样不能当作"
                       "「信号不存在」的证据。"),
        "note_zh": ("诊断只在少量号码上做以控制耗时；提高功效需扩大 max_numbers / "
                    "max_issues 与 n_null（standard 档 B>=79），代价是分钟级耗时。"),
    }


@app.get("/api/llm/arm")
def llm_arm(limit_issues: Optional[int] = Query(None, ge=1, le=1000),
            brief: bool = Query(False)):
    """V10-10：LLM 出号**独立对照臂**结论（V10-09 的 ``battle.llm_arm_report``）。

    只用真实已发生的 ``eval_details``（llm 不可回放），不读 experiments / prereg /
    假说库 —— 所以假说库为空或全 failed **不影响**本结论（防死锁的代码层保证）。
    ``brief=True`` 时只返回 ``llm_arm_brief`` 的一行摘要（前端徽标用）。
    """
    from . import battle
    try:
        report = battle.llm_arm_report(limit_issues=limit_issues)
    except Exception as exc:                          # noqa: BLE001 - 报表失败不 500
        return JSONResponse({"ok": False, "error": f"LLM 对照臂报告失败: {exc}"},
                            status_code=200)
    payload = {"ok": True, "brief_mode": bool(brief)}
    if brief:
        payload["brief"] = battle.llm_arm_brief(report)
    else:
        payload["report"] = report
        payload["brief"] = battle.llm_arm_brief(report)
    return _jsonable(payload)


@app.get("/api/shape/rules")
def shape_rules_endpoint():
    """V12：形态规律（组合恒等约束）报告。

    这些不是"预测下一期"的规律，而是**组合数学恒等约束**——任何一期开奖的
    形态（和值/跨度/三区/奇偶/连号）都几乎必然落在这些区间内。
    作为严格模式的"可用规律"，LLM 在此约束下生成注单，形态必然合理。
    """
    from . import shape_rules as SR
    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "error": "本地暂无开奖数据"}, status_code=400)
    rep = SR.shape_rules_report(draws)
    rep["ok"] = True
    return _jsonable(rep)


@app.get("/api/shape/constraints")
def shape_constraints_endpoint():
    """V12：当前出号使用的 5 维形态约束（和值/跨度/三区/奇偶/连号）。"""
    from . import shape_rules as SR
    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "error": "本地暂无开奖数据"}, status_code=400)
    ctx = SR.shape_constraint_ctx(draws)
    ctx["ok"] = True
    return _jsonable(ctx)


@app.get("/api/multiscale/mde-table")
def multiscale_mde_table():
    """V11：各窗口期数的**检出能力对照表**（MDE = 最小可检出效应）。

    为什么单独给这张表（这是 V11 多尺度卡存在的意义）：
    用户直觉是「窗口越短越容易找到规律」，但数学上是**反的** ——
    窗口越短，能检出的效应必须越大。把这张表摆在明面上，用户看到
    「40 期需 +35% 才算显著」就不会被短窗口的"显著"骗到。
    """
    from . import multiscale as MS

    rows = []
    for n in (30, 40, 60, 80, 100, 150, 300, 500, 1000, 3515):
        m = MS.mde_for(n)
        rows.append({
            "n": m["n"],
            "mde_abs": round(m["abs"], 4),
            "mde_rel": round(m["rel"], 4),
            "mde_rel_pct": f"{m['rel'] * 100:.1f}%",
            "in_use": any(sc["window"] == n for sc in MS.SCALES),
        })
    return {
        "ok": True,
        "baseline_mean": round(MS.BASELINE_MEAN, 4),
        "sigma1": MS.SIGMA1,
        "alpha": 0.05, "power": 0.8,
        "rows": rows,
        "scales": [{"key": s["key"], "label_zh": s["label_zh"],
                    "window": s["window"], "role_zh": s["role_zh"]}
                   for s in MS.SCALES],
        "note_zh": ("MDE = (z_α + z_β)·σ/√n，表示该窗口在 α=0.05、power=0.8 下"
                    "**能检出的最小效应**。窗口越短 MDE 越大：40 期需 +35%，"
                    "全部 3515 期只需 +3.8%。"
                    "短窗口的『显著』若未达到该档 MDE，属样本波动，不可信。"),
    }


@app.get("/api/multiscale/scan")
def multiscale_scan(only_interesting: bool = Query(True),
                    scales: Optional[str] = Query(None)):
    """V11：多尺度规律扫描（长期 / 中长期 / 中期 / 短中期 / 短期 五个尺度）。

    ``scales`` 为逗号分隔的档位 key 子集（如 ``all,long``）；省略=全部五档。
    ``only_interesting=True``（默认）时只返回 recommendation != none 的条目，
    避免界面全是"不显著"。
    """
    from . import multiscale as MS

    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "error": "本地暂无开奖数据"}, status_code=400)
    scale_keys = None
    if scales:
        scale_keys = [x.strip() for x in str(scales).split(",") if x.strip()]
        scale_keys = [k for k in scale_keys if k in MS.SCALE_BY_KEY] or None
    try:
        res = MS.multi_scale_scan(draws, scales=scale_keys,
                                  only_interesting=bool(only_interesting))
    except Exception as exc:                                   # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"多尺度扫描失败: {exc}"}, status_code=200)
    res["ok"] = True
    res["mode"] = "multiscale"
    return _jsonable(res)


@app.get("/api/samples/side-by-side")
def samples_side_by_side(mode: str = Query("replay")):
    """V10-10：真实交战样本 vs 离线回放样本**并排**（``mixable=False``，禁止相加）。

    数据源唯一：``replay.samples_side_by_side()``。返回体刻意不含 ``combined`` /
    ``pooled`` / ``total`` 等跨来源字段 —— 两套样本物理分表、来源与口径都不同。
    本端点再兜一层断言：若底层哪天混进了合计字段，这里直接报错而不是悄悄放行。
    """
    from . import replay
    try:
        data = replay.samples_side_by_side(mode=mode)
    except Exception as exc:                          # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"样本并排失败: {exc}"}, status_code=200)
    for banned in ("combined", "pooled", "total", "merged", "sum"):
        if banned in data:                            # pragma: no cover - 防御性
            return JSONResponse(
                {"ok": False, "error": f"样本并排返回体含跨来源合计字段 {banned!r}，"
                                       "违反 mixable=False 契约"}, status_code=200)
    return _jsonable({"ok": True, **data})


# ==========================================================================
# V10-11：成本口径（读 usage 的账本）+ 「AI 边际价值」+ LLM 决策技能关停
#
# 规格来源：``docs/reviews/R3_AI_REVIEW.md`` **G6**（三个真实问题）
#   1. 成本分母不可信 —— ``llm_client.chat()`` 原先完全不读 ``data["usage"]``；
#      本任务已在 ``llm_client`` 补齐全部出口记账（见 ``llm_client.cost_report``）。
#   2. 关停方式会误伤 —— **不得**复用 ``LLM_DISABLED``：那会让 Web 预测直接 503
#      （``engine.predict_next(llm_required=True)`` 抛 ``LLMChannelError``）。
#   3. 关停必须可持久且走唯一判据 —— 状态写 ``data/llm_config.json``（**不写
#      os.environ**，否则重启即失效、且与 ``methods_config.json`` 优先级链冲突），
#      由 ``methods.resolve_channels()`` 统一读取（``decision_disabled``）。
#
# 「AI 边际价值」的口径（**分母与分子都必须可审计**）：
#   分母 = ``data/llm_cost_log.jsonl`` 里**已计费**调用的人民币估算总额；
#   分子①（可用决策数）= ``data/llm_claims.json`` 里**契约可落地**的 claim 数
#     （维度 key 在 ``discovery.FAMILY_BY_KEY`` 内 → 能注册成可检验实验的决策）；
#   分子②（通过规律数）= 假说库中 ``prereg_status == "passed"`` 的条数（前置注册通过）。
#   **零数据不返回 0**：没有成本数据时两个比值都是 ``None`` 并附 ``reason_zh``；
#   有成本但分子为 0 时返回 ``0.0``（"花了钱什么都没换到"是真实结论，不是缺数据）。
# ==========================================================================

#: 决策技能的随机基线：LLM 臂相对基线臂"胜"的概率在无技能时就是 0.5
DECISION_SKILL_BASELINE = 0.5


def _wilson_lower(hits: int, n: int, z: float = 1.96) -> float:
    """Wilson 95% 区间下界（小样本比例不做正态近似）。

    与 ``hypothesis._wilson_lower`` 同一公式（同一口径只能有一个公式；这里内联
    是为了不跨模块 import 私有函数，数值一致性由测试钉住）。
    """
    n = int(n)
    if n <= 0:
        return 0.0
    p = float(hits) / n
    denom = 1.0 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return float(max(0.0, (centre - margin) / denom))


def decide_from_wilson(wilson_lower: Optional[float],
                       baseline: float = DECISION_SKILL_BASELINE) -> Dict:
    """判据本体（纯函数，便于钉边界）：Wilson 下界 ≤ 基线 → 应关停。

    ``wilson_lower is None``（无决定性配对样本）→ ``should_disable=None``：
    **无证据不等于证否**，此时既不关停也不宣称有技能。
    """
    if wilson_lower is None:
        return {
            "should_disable": None,
            "baseline": float(baseline),
            "wilson_lower": None,
            "reason_zh": "没有可判定的配对样本：下界无从计算，不做关停判断（也不宣称有技能）。",
        }
    w = float(wilson_lower)
    disable = w <= float(baseline)
    return {
        "should_disable": bool(disable),
        "baseline": float(baseline),
        "wilson_lower": w,
        "reason_zh": (
            f"决策技能 Wilson 95% 下界 {w:.4f} "
            + ("≤" if disable else ">")
            + f" 随机基线 {float(baseline):.2f} → "
            + ("应关停该决策通道（保守口径：连下界都追不平随机，不能证明有技能）。"
               if disable else
               "下界高于随机基线，暂不关停（仍受 llm_can_pick 等其他开关约束）。")),
    }


def decision_skill(rows: Optional[List[Dict]] = None, *,
                   baseline: float = DECISION_SKILL_BASELINE,
                   max_tickets: int = 20) -> Dict:
    """LLM **决策技能**：同期逐注配对比较 ``llm:*`` 臂 vs 基线族（stat/ml/blend/uniform）。

    为什么用"胜率 vs 0.5"而不是均值差：技能被定义为"决策比随机更好"的**二分类**
    能力，胜率可以直接套 Wilson 区间；均值差需要分布假设，而在 30 注量级上任何
    近似都不可信（``rigor._min_detectable_effect``：n=30 → 40.7%）。

    口径（与 ``battle.llm_arm_report`` 的配对纪律一致，见 V10-09）：
      * **同期**：两臂必须在同一 ``issue`` 都有记录；
      * **同注数**：该期两臂注数必须相等（不同规模的期剔除并计数）；
      * **逐注配对**：按 ``seq`` 一一对应；逐期取**均值**后比较（注间相关，
        不能把每注当独立样本）；
      * **平局剔除**：两臂逐期均值相等时该期不进 Wilson 分母（否则平局会被
        当成"没输"，系统性抬高胜率）——平局数单独给出。
    """
    from . import evaluate as EV
    if rows is None:
        rows = db.load_eval_details()
    rows = [dict(r) for r in (rows or [])]
    by_issue: Dict[str, Dict[str, List[Dict]]] = {}
    for r in rows:
        issue = str(r.get("issue") or "")
        method = str(r.get("method") or "")
        if not issue or not method:
            continue
        by_issue.setdefault(issue, {}).setdefault(method, []).append(r)

    wins = losses = ties = 0
    dropped_mismatch = 0
    used_issues: List[str] = []
    for issue, methods in by_issue.items():
        llm_rows = sorted((r for m, rs in methods.items() if m.startswith("llm:")
                           for r in rs), key=lambda x: int(x.get("seq") or 0))
        base_rows = sorted((r for m, rs in methods.items()
                            if not m.startswith("llm:")
                            and EV.method_family(m) in ("stat", "ml", "blend", "uniform")
                            for r in rs), key=lambda x: int(x.get("seq") or 0))
        if not llm_rows or not base_rows:
            continue
        if len(llm_rows) != len(base_rows):
            dropped_mismatch += 1
            continue
        if max_tickets and len(llm_rows) > int(max_tickets):
            continue
        llm_mean = sum(float(r.get("red_hits") or 0) for r in llm_rows) / len(llm_rows)
        base_mean = sum(float(r.get("red_hits") or 0) for r in base_rows) / len(base_rows)
        if llm_mean > base_mean:
            wins += 1
        elif llm_mean < base_mean:
            losses += 1
        else:
            ties += 1
            continue
        used_issues.append(issue)

    decisive = wins + losses
    win_rate = (wins / decisive) if decisive else None
    wilson = _wilson_lower(wins, decisive) if decisive else None
    return {
        "ok": True,
        "metric": "decision_skill_win_rate",
        "n_issues_compared": decisive + ties,
        "n_decisive": decisive,
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "dropped_ticket_mismatch": dropped_mismatch,
        "win_rate": None if win_rate is None else round(float(win_rate), 6),
        "wilson_lower": None if wilson is None else round(float(wilson), 6),
        **decide_from_wilson(wilson, baseline=baseline),
        "issues": used_issues,
        "note_zh": ("技能 = LLM 决策在**同期配对**中胜过基线（stat/ml/blend/uniform）"
                    "的比例；无技能时该比例应为 0.5。**平局剔除**且平局数单列。"
                    "小样本下 Wilson 下界天然偏低 → 关停是**保守**决定而非「证明无用」；"
                    "反过来，样本不足时也不得宣称有技能。"),
    }


def _usable_decision_count() -> Dict:
    """分子①：LLM 产出的**可落地决策**数（契约可落地 = 维度 key 在维度表内）。"""
    from . import llm_lab
    claims = list((llm_lab.load_claims() or {}).get("claims") or [])
    try:
        from . import discovery
        known = set(discovery.FAMILY_BY_KEY.keys())
    except Exception:                                  # noqa: BLE001
        known = set()
    usable = 0
    for c in claims:
        c = c or {}
        key = str(c.get("key") or c.get("dimension") or "").strip()
        if not key:
            act = c.get("action") if isinstance(c.get("action"), dict) else {}
            filt = act.get("filter") if isinstance(act.get("filter"), dict) else {}
            key = str(filt.get("key") or "")
        if key and key in known:
            usable += 1
    return {"n_claims": len(claims), "n_usable": usable,
            "source": "data/llm_claims.json",
            "definition_zh": ("可用决策 = 维度 key 落在 discovery.FAMILY_BY_KEY 内、"
                              "能注册成可检验实验的 claim（契约可落地）。")}


def _passed_pattern_count() -> Dict:
    """分子②：**通过**规律的条数（前置注册 ``passed``，V10-02 的唯一写者判定）。"""
    from . import hypothesis
    try:
        reg = hypothesis.load_registry() or {}
    except Exception as e:                             # noqa: BLE001
        return {"n_passed": None, "n_hypotheses": None,
                "source": "data/hypotheses.json", "error": f"{type(e).__name__}: {e}"}
    hyps = list(reg.get("hypotheses") or [])
    passed = sum(1 for h in hyps if str((h or {}).get("prereg_status") or "") == "passed")
    validated = sum(1 for h in hyps if str((h or {}).get("status") or "") == "validated")
    return {"n_passed": passed, "n_validated": validated, "n_hypotheses": len(hyps),
            "source": "data/hypotheses.json",
            "definition_zh": ("通过规律 = prereg_status==passed（前置注册按预注册检验通过）。"
                              "validated（A/B 晋升）单列，不与 passed 相加。")}


def llm_marginal_value(cost: Optional[Dict] = None) -> Dict:
    """「AI 边际价值」：每 ¥1 换回的可用决策数 / 通过规律数。

    **零数据不返回 0**：没有已计费成本时两个比值都是 ``None`` 并说明原因；
    有成本但分子为 0 时返回 ``0.0``（"花了钱什么都没换到"是结论，不是缺数据）。
    """
    if cost is None:
        from .llm_client import cost_report
        cost = cost_report()
    yuan = cost.get("yuan")
    decisions = _usable_decision_count()
    patterns = _passed_pattern_count()

    def _ratio(numerator):
        if yuan is None or numerator is None or float(yuan) <= 0:
            return None
        return round(float(numerator) / float(yuan), 6)

    if yuan is None:
        reason = "成本日志里没有已计费调用（yuan=null），比值无法计算 —— 不用 0 假装有值。"
    elif float(yuan) <= 0:
        reason = "已计费成本为 0，比值无意义。"
    else:
        reason = ("比值 = 分子 ÷ 已计费人民币估算额。分子为 0 时给 0.0（真实结论："
                  "花了钱没换到可审计产物），分母缺失时给 null（缺数据）——两者必须区分展示。")
    return {
        "yuan": yuan,
        "per_yuan_usable_decisions": _ratio(decisions.get("n_usable")),
        "per_yuan_passed_patterns": _ratio(patterns.get("n_passed")),
        "numerator_usable_decisions": decisions,
        "numerator_passed_patterns": patterns,
        "reason_zh": reason,
        "honesty_zh": ("这两个数**不是** ROI，也不代表 AI 有价值：LLM 默认不出号"
                       "（llm_can_pick=False），可用决策与通过规律都可能在真随机世界里"
                       "长期为 0。本指标只回答「花掉的每一块钱换回了多少可审计产物」。"),
    }


def _maybe_auto_disable_decision(*, rows: Optional[List[Dict]] = None,
                                 force: bool = False) -> Dict:
    """技能不达标时**自动**写入关停状态（``llm_config.json``，不写 os.environ）。

    判定：Wilson 下界 ≤ 0.5 且**有决定性配对样本**（``n_decisive > 0``）。
    没有样本 → 不写（无证据不等于证否，绝不能凭"没有数据"关掉通道）。
    已处于关停状态时保持原状（不覆盖已有 reason）。
    整体失败只打印，绝不影响调用方（调度/进化链路不能被它拖垮）。
    """
    from . import methods as METH
    try:
        skill = decision_skill(rows=rows)
        state = METH.decision_state()
        changed = False
        should = skill.get("should_disable") is True and int(skill.get("n_decisive") or 0) > 0
        if (force or should) and not state.get("disabled"):
            METH.set_decision_disabled(True, str(skill.get("reason_zh") or "")[:300])
            changed = True
        if changed:
            # 刷新 import 期快照：``config.CHANNELS`` 是引擎 ``channels_snapshot()`` 的
            # 读口，不刷新的话同一进程内引擎仍看到关停前的 llm_can_pick。
            try:
                config.refresh_channels()
            except Exception as exc:                   # noqa: BLE001
                print(f"[cost] 通道快照刷新失败（关停已落盘）: {exc}")
        return {"ok": True, "applied": bool(changed), "skill": skill,
                "decision_state": METH.decision_state()}
    except Exception as e:                             # noqa: BLE001
        print(f"[cost] 决策技能关停判定失败（不影响出号）: {e}")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.get("/api/llm/cost")
def llm_cost(limit: Optional[int] = Query(None, ge=1, le=100000)):
    """成本报表：总调用数 / 总 token / 总花费 / 按 model 与 purpose 分组 + 边际价值。

    数据源唯一：``llm_client.cost_report()``（``data/llm_cost_log.jsonl``）。
    **零调用时金额与 token 一律 null**（不是 0），并给出原因。
    """
    from .llm_client import cost_report
    from . import methods as METH
    try:
        cost = cost_report(limit=limit)
    except Exception as exc:                          # noqa: BLE001 - 报表失败不 500
        return JSONResponse({"ok": False, "error": f"成本报表失败: {exc}"},
                            status_code=200)
    ch = METH.resolve_channels()
    return _jsonable({
        "ok": True,
        "cost": cost,
        "marginal_value": llm_marginal_value(cost),
        "decision": {
            "decision_disabled": bool(ch.get("decision_disabled")),
            "decision_disabled_reason": ch.get("decision_disabled_reason") or "",
            "decision_disabled_at": ch.get("decision_disabled_at"),
            "llm_can_pick": bool(ch.get("llm_can_pick")),
            "channels_fingerprint": ch.get("fingerprint"),
        },
        "note_zh": ("成本来自每次 ``chat()`` 出口一行的 append-only 日志；token 数才是"
                    "账本口径，yuan 是按可配置单价折算的**估算**。决策关停只关"
                    "「LLM 出号决策通道」，**不**影响预测本身能否成功。"),
    })


@app.get("/api/llm/decision/status")
def llm_decision_status(limit_issues: Optional[int] = Query(None, ge=1, le=2000)):
    """LLM 决策技能与关停状态（判据 + 当前状态 + 通道快照）。"""
    from . import methods as METH
    rows = db.load_eval_details(limit_issues=limit_issues) if limit_issues else None
    try:
        skill = decision_skill(rows=rows)
    except Exception as exc:                          # noqa: BLE001
        skill = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    ch = METH.resolve_channels()
    return _jsonable({
        "ok": True,
        "skill": skill,
        "decision_state": METH.decision_state(),
        "channels": {
            "decision_disabled": bool(ch.get("decision_disabled")),
            "llm_can_pick": bool(ch.get("llm_can_pick")),
            "families": ch.get("families"),
            "llm_output_mode": ch.get("llm_output_mode"),
            "fingerprint": ch.get("fingerprint"),
        },
        "criterion_zh": ("判据：决策技能 Wilson 95% 下界 ≤ 随机基线 0.5 → 标记该决策通道"
                         "应关停。关停状态写在 data/llm_config.json（重启仍生效），由"
                         "resolve_channels() 读取；**不使用** LLM_DISABLED，因此预测不会 503。"),
    })


@app.post("/api/llm/decision/restore")
def llm_decision_restore(reason: str = Query("", max_length=300)):
    """人工恢复 LLM 决策通道（前端恢复按钮由后续任务接）。

    **只清关停标志**，不动 ``disabled`` / 模型 / 密钥等其它配置键
    （``set_decision_disabled`` 保留 ``llm_config.json`` 的其余键）。
    恢复后调用 ``config.refresh_channels()`` 刷新 import 期快照。
    """
    from . import methods as METH
    try:
        state = METH.set_decision_disabled(False, reason or "人工恢复")
        config.refresh_channels()
    except Exception as exc:                          # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"恢复失败: {exc}"}, status_code=200)
    ch = METH.resolve_channels()
    return _jsonable({
        "ok": True,
        "restored": True,
        "decision_state": state,
        "channels": {
            "decision_disabled": bool(ch.get("decision_disabled")),
            "llm_can_pick": bool(ch.get("llm_can_pick")),
            "fingerprint": ch.get("fingerprint"),
        },
        "note_zh": ("已清除决策关停标志（data/llm_config.json）。注意：LLM 是否真的参与"
                    "出号仍由 families.llm / llm_output_mode（默认 off）决定，"
                    "恢复关停不等于启用 LLM 出号。"),
    })


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

    # V11：每期自动推进规律生命周期（退居二线判定）。
    # 顺序：在快照刷新**之后**、生成预测**之前** —— 这样本期被判定为
    # 退居二线（monitor）的规律，会通过下一轮 build_snapshot 注入的
    # lifecycle 状态立即从 usable_rules() 里消失，本期出号就不再使用它。
    # 失败只降级（沿用既定约定），绝不影响当期预测生成。
    try:
        from . import rule_lifecycle as _RL
        _cycle = _RL.run_cycle(db.load_draws(), use_llm=False, persist=True)
        _mig = _cycle.get("migrated") or []
        print("[scheduler] 生命周期:", "迁移", len(_mig),
              "条" if _mig else "无需迁移", "|",
              {k: v for k, v in (_cycle.get("counts") or {}).items()
               if k in ("active", "monitor", "unmanaged")})
    except Exception as e:  # noqa: BLE001
        print("[scheduler] 生命周期推进失败（不影响预测）:", e)

    draws = db.load_draws()
    exp = BT.expected_issue(draws)
    if exp.get("expected_draws_missing"):
        print("[scheduler] 警告：数据源落后于开奖日历，缺", exp.get("missed_draws"), "期")
    issue = exp["issue"]
    if not db.load_predictions(issue):
        # V10-11：决策通道被关停时不再请求 LLM（省钱且不产出被关停通道的注），
        # 但预测**照常生成**（这就是与 LLM_DISABLED 的区别：后者会 503）。
        from . import methods as METH
        _llm_on = not bool(METH.resolve_channels().get("decision_disabled"))
        engine.predict_next(draws, use_llm=_llm_on)
        print("[scheduler] 已生成", issue, "预测",
              "（LLM 决策通道已关停，本次为纯统计/ML）" if not _llm_on else "")


def _evolution_step(limit: int = 0, use_llm: bool = True,
                    force: bool = False) -> dict:
    """每期开奖后自动跑的一轮自进化：挖掘 → 注册 → A/B 实战验证 → 裁决。

    为什么需要它：``evolve_once`` 只做「挖掘+注册+应用」，假说会永远停在
    ``proposed``（实测 9 条全是 proposed），因为**没有任何环节把它们送去做 A/B 验证**。
    本函数补上这一步——对每条 proposed 假说跑配对回放，把结果交给
    ``hypothesis.resolve_registry`` 裁决，假说才可能晋升 ``validated``（进而影响出号）
    或被 ``rejected``（退场）。

    成本控制：
    - ``limit`` 限制参与验证的历史期数（0=全部）；A/B 内部另有 600 期上限
    - ``use_llm`` 控制是否调用 LLM 提炼新假说（单次 1~2 分钟）
    - **幂等（2026-10-19 CPU 治理）**：同一期数据只自动进化一次。见下方说明。
    - 整体失败只记录不抛出，绝不影响当期预测生成

    ⚠️ 为什么要幂等（实测根因）
    -------------------------
    ``_scheduler_step`` 由 ``_scheduler_loop`` 每 1800s 调用一次；在**开奖日**
    （周二/四/日）过了 21:35 之后，每轮都会走到这里。而这一天剩下的时间里数据
    并不会再变（``inserted_new: 0``），于是**同一份数据被反复跑完整进化**：
    日志实测 2026-10-18（周日）14:13 / 15:06 / 15:56 三轮，每轮 19~22 分钟。
    这就是「容器 CPU 长期 100%」的第二层成因（第一层是 ``_rebuild_probs`` 的
    冗余模型构建，已单独修复）。

    修法：把「已进化到的数据指纹（期数 + 末期号）」记在 ``data/evolution_state.json``；
    指纹没变就**跳过**（返回 ``skipped=True``）。数据一更新指纹就变 ⇒ 立刻恢复进化。
    手动触发（``/api/evolution/run``）传 ``force=True``，永不被跳过。

    返回本轮摘要，便于调度日志与 ``/api/evolution/status`` 观测。
    """
    from . import battle, evolution, hypothesis

    draws = db.load_draws()
    if not draws:
        return {"ok": False, "reason": "本地暂无开奖数据"}

    # ---- 幂等闸门：同一份数据只自动进化一次 ----
    fingerprint = f"{len(draws)}:{draws[-1].get('issue')}"
    if not force:
        state = _evolution_state_load()
        if state.get("last_fingerprint") == fingerprint:
            return {"ok": True, "skipped": True,
                    "skipped_reason": "该期数据已完成进化（幂等跳过）",
                    "fingerprint": fingerprint,
                    "last_run_at": state.get("last_run_at"),
                    "note_zh": ("本期数据未变化，进化已跑过 → 跳过（避免每 30 分钟"
                                "重复跑完整 A/B 验证；手动触发请用 /api/evolution/run）")}

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

    # 5) V10-11：决策技能达标性判定（Wilson 下界 ≤ 0.5 → 自动关停决策通道）。
    #    写 data/llm_config.json（不写 os.environ）；无决定性样本时**不**写。
    #    失败只降级、不影响预测（沿用本函数的既有约定）。
    try:
        summary["decision_skill"] = _maybe_auto_disable_decision()
    except Exception as e:  # noqa: BLE001
        summary["decision_skill"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    print("[evolution] 本轮完成:", summary["resolution"], "| 库:", summary["by_status"])
    # 记录本次进化对应的数据指纹（幂等闸门的数据源）。写失败只记录、不影响结果。
    try:
        _evolution_state_store(fingerprint, summary)
    except Exception as e:  # noqa: BLE001
        print("[evolution] 状态记录失败（不影响进化结果）:", e)
    summary["fingerprint"] = fingerprint
    return summary


def _evolution_state_path():
    """进化幂等状态文件路径（与 ``adaptive_state.json`` 同目录，属运行时状态）。"""
    return config.DATA_DIR / "evolution_state.json"


def _evolution_state_load() -> dict:
    """读幂等状态；文件缺失/损坏一律返回空 dict（当作「从未进化」）。"""
    import json as _json
    try:
        p = _evolution_state_path()
        if not p.exists():
            return {}
        raw = _json.loads(p.read_text(encoding="utf-8") or "{}")
        return raw if isinstance(raw, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _evolution_state_store(fingerprint: str, summary: dict) -> None:
    """记录「已进化到的数据指纹」（原子替换）。"""
    import json as _json
    import os as _os
    import time as _time
    p = _evolution_state_path()
    payload = {
        "last_fingerprint": str(fingerprint),
        "last_run_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        "last_resolution": summary.get("resolution"),
        "schema": 1,
    }
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(_json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _os.replace(tmp, p)


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


@app.get("/api/rules/lifecycle")
def rules_lifecycle_status():
    """V11：规律生命周期状态（谁在一线 / 谁退居二线 / 谁被滞后暂缓）。

    来源唯一：``rule_lifecycle.load_states()``。返回体里
    ``demoted`` 是已退居二线（monitor）的规律 id 及其理由。
    """
    from . import rule_lifecycle as RL

    states = RL.load_states()
    recs = (states or {}).get("rules") or {}
    demoted = {rid: r for rid, r in recs.items()
               if (r or {}).get("state") == "monitor"}
    active = {rid: r for rid, r in recs.items()
              if (r or {}).get("state") == "active"}
    return {
        "ok": True,
        "thresholds": (states or {}).get("thresholds") or RL.thresholds(),
        "version": (states or {}).get("version"),
        "n_managed": len(recs),
        "n_active": len(active),
        "n_demoted": len(demoted),
        "demoted": demoted,
        "active": active,
        "state_file": str(RL.STATE_FILE),
        "note_zh": ("退居二线（monitor）= 衰减判据连续成立，不再参与出号，"
                    "但仍留在列表里继续观察 —— 不是被证伪。"),
    }


@app.post("/api/rules/lifecycle/run")
def rules_lifecycle_run(use_llm: bool = Query(False)):
    """V11：手动推进一次规律生命周期。

    ``use_llm=False``（默认）：纯确定性（不调 LLM）；LLM 的复核意见
    永远不会影响状态迁移（纪律 1），所以手动触发默认不依赖它。

    返回统一走 ``rule_lifecycle.run_cycle`` 的结构化结果（含 migrated /
    counts / rules / note_zh）。
    """
    from . import patterns_runtime, rule_lifecycle as RL

    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "error": "本地暂无开奖数据"}, status_code=400)
    snap = patterns_runtime.build_snapshot(draws)
    try:
        out = RL.run_cycle(draws, snapshot=snap, use_llm=bool(use_llm), persist=True)
    except Exception as exc:                              # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"生命周期推进失败: {exc}"},
                            status_code=200)
    out.setdefault("note_zh", _lifecycle_note(out))
    return _jsonable(out)


def _lifecycle_note(out: dict) -> str:
    changed = out.get("changed") or {}
    if not changed:
        return ("本期没有规律需要退居二线（全部 hold）——"
                "这不是功能失效，而是规律本身不存在衰减证据")
    items = [f"{rid}: {v.get('from')}→{v.get('to')}（{'、'.join(v.get('reasons') or [])}）"
             for rid, v in list(changed.items())[:10]]
    return f"本次迁移 {len(changed)} 条：" + "；".join(items)


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
        # 手动触发**必须**绕过幂等闸门（用户的意图就是「现在再跑一轮」）。
        result = _evolution_step(limit=int(limit), use_llm=bool(use_llm), force=True)
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


# ---------------------------------------------------------------------------
# V12-13：AI 两岗位（核对员 / 契约检查器）+ 打分卡
#
# 铁律（V10 §3.2 两岗位共同约束）：这两条路由**不产号、不写 predictions、
# 不进候选池**；只读 ``llm_audit``（仅追加）或把判定扔进后台任务。
# 无记录时 ``available=False`` —— 前端必须显示"本次未取得 AI 核对结果"，
# **不许**渲染成"AI 已核对"。
# ---------------------------------------------------------------------------

@app.get("/api/llm/audit/latest")
def llm_audit_latest(post: Optional[str] = Query(None),
                     limit: int = Query(200, ge=1, le=500)):
    """最近一次 AI 核对判定 + 打分卡（只读）。

    ``post`` 可选 ``result_narrator`` / ``contract_checker``；不传则看全部。
    打分卡规则（V12 §1.3 硬性准入）：``n``（判定次数）、``score = n_correct /
    (n_correct+n_wrong+n_invalid)``，``skipped``（degraded）**单列、不进分母**。
    """
    try:
        from . import ai_audit
    except ImportError as e:
        return JSONResponse({"ok": False, "available": False,
                             "error": "AI 核对模块尚未就绪",
                             "detail": str(e)}, status_code=503)
    if post is not None and post not in ai_audit.POSTS:
        return JSONResponse({"ok": False, "available": False,
                             "error": f"未知岗位：{post}（必须是 {ai_audit.POSTS}）"},
                            status_code=400)
    try:
        out = ai_audit.latest_audit(post=post)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "available": False,
                             "error": "读取审计记录失败",
                             "detail": f"{type(e).__name__}: {e}"}, status_code=503)
    out = dict(out)
    out["ok"] = True
    out["posts"] = list(ai_audit.POSTS)
    return out


@app.post("/api/llm/audit/run")
async def llm_audit_run(request: Request,
                        post: str = Query(...),
                        limit_issues: int = Query(30, ge=1, le=200),
                        use_llm: int = Query(1, ge=0, le=1)):
    """跑一次 AI 核对（**后台任务**，V10 §3.2.3-2：预测请求绝不等 LLM）。

    * ``post=result_narrator``：输入来自真实 ``eval_details`` 最近 ``limit_issues`` 期
      （只读，不重新跑预测）；
    * ``post=contract_checker``：输入来自请求体 ``{"items": [{key, params, target}]}``
      （不传则取最近一次 ``llm_claims`` 快照中的 filters；都没有则 400）。

    立即返回 ``task_id``，前端轮询 ``/api/tasks/{id}``。单次只发 **1 次** LLM 调用。
    """
    try:
        from . import ai_audit
    except ImportError as e:
        return JSONResponse({"ok": False, "error": "AI 核对模块尚未就绪",
                             "detail": str(e)}, status_code=503)
    if post not in ai_audit.POSTS:
        return JSONResponse({"ok": False,
                             "error": f"未知岗位：{post}（必须是 {ai_audit.POSTS}）"},
                            status_code=400)
    body: Dict[str, Any] = {}
    try:
        raw = await request.body()
        if raw:
            parsed = json.loads(raw.decode("utf-8") or "{}")
            if isinstance(parsed, dict):
                body = parsed
    except Exception:  # noqa: BLE001
        body = {}
    try:
        if post == ai_audit.POST_NARRATOR:
            rows = db.load_eval_details(limit_issues=int(limit_issues))
            if not rows:
                return JSONResponse({"ok": False,
                                     "error": "本地暂无 eval_details 记录（尚未有核对过的预测）"},
                                    status_code=400)
            audit_input = ai_audit.build_narrator_input(rows,
                                                        limit_issues=int(limit_issues))
        else:
            items = body.get("items")
            if items is None:
                items = _latest_claim_filters()
            if not items:
                return JSONResponse({"ok": False,
                                     "error": "没有可检查的 filters（请求体 items 为空且无 llm_claims 快照）"},
                                    status_code=400)
            audit_input = ai_audit.build_contract_input(items)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "构造核对输入失败",
                             "detail": f"{type(e).__name__}: {e}"}, status_code=503)

    def _fn(_progress):
        _progress(0.1, "AI 核对运行中（单次调用）…")
        res = ai_audit.run_post(post, audit_input, use_llm=bool(use_llm),
                                persist=True)
        res["ok"] = True
        res["note_zh"] = res.get("note_zh") or "AI 核对完成"
        return res

    tid = _bg_run("llm_audit", _fn)
    return {"ok": True, "task_id": tid, "post": post,
            "note_zh": "已进入后台任务，前端轮询 /api/tasks/{id}"}


def _latest_claim_filters() -> List[Dict[str, Any]]:
    """从最近一次 ``llm_claims`` 快照里取出 filters（只读；没有则返回空列表）。"""
    try:
        from . import llm_lab
        claims = llm_lab.load_claims() or {}
    except Exception:  # noqa: BLE001
        return []
    items = claims.get("claims") or claims.get("items") or []
    out: List[Dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        flt = it.get("filter") or it.get("params") or {}
        out.append({"key": str(it.get("key") or flt.get("key") or ""),
                    "params": dict(it.get("params") or flt.get("params") or {}),
                    "target": str(it.get("target") or "red")})
    return [x for x in out if x["key"]]


# ==========================================================================
# V12-10 / V12-15：L2 结构层的两个 API（组合数学恒等式，不依赖历史数据）
#
# 共同纪律：
#   * 所有概率都是 C(33,6) **全枚举精确值**（``method="exact_enum"``），
#     与样本量 / p 值无关；
#   * 不改变、不宣称改变期望值（``ev_ratio_full`` 恒为单注口径）；
#   * **不给购买建议**（返回体里不得出现「划算」「推荐」等词，
#     ``tests/test_v12_l2_exact.py`` 用 grep 计数锁死）。
# ==========================================================================

@app.get("/api/l2/distribution")
def l2_distribution(n: int = Query(5, ge=1, le=16),
                    preset: str = Query("spread"),
                    seed: int = Query(0),
                    tickets: Optional[str] = Query(None)):
    """V12-10 L2 精确值服务：n 注票组的分布/尾部/覆盖精确值（中性读数）。

    入参：``n``（1..16）、``preset``（``spread`` 号位均摊 = 低重叠，``overlap``
    高重叠）、``seed``（造票种子，同 seed 同结果）、``tickets``（可选 JSON 字符串
    ``[{"reds":[..6..],"blue":1},...]``，给了就以它为准）。

    出参：``ticket_distribution()`` 原样字段 + ``method="exact_enum"``（断言非精确
    即 500）+ ``methods``（每个字段的方法标签）+ ``n``。
    """
    from . import l2_service
    try:
        payload = l2_service.l2_distribution_payload(
            n=int(n), preset=preset, seed=int(seed), tickets_json=tickets)
    except (ValueError, TypeError) as exc:
        return JSONResponse({"ok": False, "error": f"参数错误: {exc}"},
                            status_code=400)
    except AssertionError as exc:          # exact_enum / prize_assumption 断言失败
        return JSONResponse({"ok": False, "error": f"L2 口径断言失败: {exc}",
                             "method": "unavailable"}, status_code=500)
    return _jsonable(payload)


@app.get("/api/portfolio/marginal")
def portfolio_marginal(n: int = Query(10, ge=1, le=15),
                       preset: str = Query("spread")):
    """V12-15 边际读数（L3-4）：「加到 n+1 注」的中性读数，**只给数字不给建议**。

    返回 ``delta_p_at_least_one_prize_pp``（+X 个百分点，全枚举精确值差分）、
    ``expected_return_yuan = −¥2×(1−ev_ratio_full)``（新增 1 注的期望回报）、
    ``ev_ratio_full``、``prize_assumption``、``n``、``n_next``、``method="exact_enum"``。
    """
    from . import l2_service
    try:
        payload = l2_service.marginal_reading(int(n), preset=preset)
    except (ValueError, TypeError) as exc:
        return JSONResponse({"ok": False, "error": f"参数错误: {exc}"},
                            status_code=400)
    except AssertionError as exc:
        return JSONResponse({"ok": False, "error": f"L2 口径断言失败: {exc}",
                             "method": "unavailable"}, status_code=500)
    return _jsonable(payload)


# ==========================================================================
# V12-16 / V12-14 新增路由（追加在既有路由之后，**不修改**任何既有路由）
# ==========================================================================

@app.post("/api/experiments/randomness-selfcheck")
def experiments_randomness_selfcheck(
        seed: int = Query(42),
        n_issues: Optional[int] = Query(None, ge=0, le=20000),
        n_perm: Optional[int] = Query(None, ge=0, le=5000),
        n_draws: Optional[int] = Query(None, ge=0, le=20000),
        synthetic_bias: Optional[str] = Query(None),
        window: Optional[int] = Query(None, ge=1, le=20000)):
    """**用户可自证实验**（V12-16 / L3-7）：用合成随机数据重跑同一套检验，并排展示。

    真实数据走 ``rigor.randomness_report()`` 原样；合成数据由 ``seed`` 派生的
    ``numpy.random.default_rng`` 新生成（同期数、同蓝球范围）。返回 4 项检查
    （``real_not_detected`` / ``synthetic_uncorrected_rate_in_ci`` /
    ``strongest_p_same_magnitude`` / ``reproducible_same_seed``）、``all_pass``、
    ``seed``、``response_sha256``、``elapsed_ms``。

    可复现（A7）：同 ``seed`` 两次响应的 ``response_sha256`` 必须相同
    （哈希覆盖剔除 ``response_sha256`` 与 ``elapsed_ms`` 后的规范化 JSON）。

    参数
    ----
    seed: 唯一随机性来源（默认 42）。
    n_issues: 合成数据期数；默认 = 真实有效期数（保证同期数，方案上限 3516）。
    n_perm: 零分布重采样次数；默认 200。越小越快，越大分位估计越稳。
    n_draws: 只用最近 n 期真实数据（默认全部）；用于快速演示/自测。
    synthetic_bias: 传 ``"number"`` 时给**合成数据**注入偏置（反向演示：
        假数据一旦带偏，检查 1 会失败）。默认不注入。
    window: 兼容参数，当前未使用（保留签名以免前端改造成本）。
    """
    from . import selfcheck as _sc
    try:
        draws = db.load_draws()
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "读取开奖数据失败",
                             "detail": f"{type(e).__name__}: {e}"}, status_code=503)
    if n_draws:
        draws = draws[-int(n_draws):]
    bias = (synthetic_bias or "").strip() or None
    if bias not in (None, "number"):
        return JSONResponse({"ok": False,
                             "error": "synthetic_bias 只支持 'number' 或留空"},
                            status_code=400)
    try:
        payload = _sc.selfcheck(
            draws, seed=int(seed),
            n_issues=(_sc.DEFAULT_N_ISSUES if n_issues is None else int(n_issues)),
            n_perm=(_sc.DEFAULT_N_PERM if n_perm is None else int(n_perm)),
            synthetic_bias=bias)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "自证实验执行失败",
                             "detail": f"{type(e).__name__}: {e}"}, status_code=503)
    return _jsonable(payload)


@app.get("/api/audit/anomaly/latest")
def audit_anomaly_latest():
    """V12-14（L3-3）：读取**最近一次**数据异常审计结果（只读，不触发计算）。

    无记录时返回 ``available=False``（HTTP 200，前端据此显示「尚未运行」），
    不返回 404 —— 404 会让前端把它当成接口故障。
    """
    from . import data_audit
    try:
        payload = data_audit.latest_audit()
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"available": False, "error": "读取审计产物失败",
                             "detail": f"{type(e).__name__}: {e}"},
                            status_code=503)
    return _jsonable(payload)


@app.post("/api/audit/anomaly/run")
def audit_anomaly_run(use_llm: int = Query(1, ge=0, le=1),
                      window: int = Query(40, ge=5, le=500),
                      w_seg: int = Query(300, ge=30, le=3000),
                      n_perm: int = Query(2000, ge=200, le=20000),
                      seed: int = Query(20261018)):
    """V12-14（L3-3）：**后台**跑一次数据异常审计（立即返回 task_id，不阻塞）。

    审计流程：确定性统计 → LLM 提名（``use_llm=1``；不可用时 ``degraded=True``）
    → 代码逐条判定（``real_defect`` / ``regime_shift`` / ``noise``）→ 落盘。
    完成后 ``GET /api/audit/anomaly/latest`` 可读到结果。
    """
    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "available": True,
                             "error": "本地暂无开奖数据"}, status_code=400)
    from . import data_audit

    def _work(progress):
        return data_audit.run_audit(draws, use_llm=bool(use_llm), window=int(window),
                                    w_seg=int(w_seg), n_perm=int(n_perm),
                                    seed=int(seed), progress=progress)

    tid = _bg_run("data_audit", _work)
    return {"ok": True, "task_id": tid,
            "note_zh": ("数据审计已在后台运行（LLM 提名 + 确定性判定），"
                        "完成后 GET /api/audit/anomaly/latest 可读结果")}


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


# 启动后异步补刷规律快照。**必须可关闭**：
#   ① pytest 会话会反复 import 本模块，若每次都在导入期起线程，
#      多个 boot-rules-refresh 线程会并发连同一个 SQLite 连接（check_same_thread=False），
#      与 joblib 的多进程后端叠加后实测触发 **Segmentation fault**（2026-10-18）；
#   ② 调度器/CLI 等非 Web 场景也不需要这个补刷。
# 因此由 config.BOOT_REFRESH_RULES 控制（默认 1=开，pytest 下由 conftest 置 0）。
if getattr(config, "BOOT_REFRESH_RULES", True) and os.environ.get("LOTT_BOOT_REFRESH_RULES", "1") == "1":
    try:
        import threading as _threading
        _threading.Thread(target=_boot_refresh_rules_snapshot, daemon=True,
                          name="boot-rules-refresh").start()
    except Exception:  # noqa: BLE001
        pass

    # V12-16：预热自证实验的零分布（纯随机数据下「最强校正 p」的 5% 分位）。
    # 这个值是**确定性**的（只由 seed / 期数 / n_perm / 检验数 / alpha 决定），
    # 首次计算约 5.6s（200 次 × randomness_report），之后走磁盘缓存 <0.1s。
    # 把首次计算挪到启动后台，用户点「🧪 验证」时就不会等 5 秒。
    # 预热失败不影响任何功能（自证实验会自己再算一次）。
    def _boot_warm_selfcheck_null():
        import time as _t
        _t.sleep(25)          # 让启动回测/预测先跑完，避免抢 CPU（与上面补刷错开）
        try:
            from . import selfcheck as _sc
            d = db.load_draws()
            if not d:
                return
            res = _sc.warm_null_quantile(seed=_sc.DEFAULT_SEED,
                                         n_issues=len(d))
            print("[selfcheck] 零分布预热:", res)
        except Exception as e:  # noqa: BLE001
            print("[selfcheck] 零分布预热失败（不影响功能）:", e)

    try:
        import threading as _threading2
        _threading2.Thread(target=_boot_warm_selfcheck_null, daemon=True,
                           name="boot-selfcheck-warm").start()
    except Exception:  # noqa: BLE001
        pass

if config.SCHEDULER_ENABLED:
    import threading
    threading.Thread(target=_scheduler_loop, daemon=True).start()