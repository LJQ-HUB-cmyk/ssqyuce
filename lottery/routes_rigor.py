"""规律显著性检验域路由（V4 诚实口径：随机性检验 / 策略显著性）。

P2 模块化：从 `api_app.py` 的 V4 域切出。

这一块的产品意义：2026-10-15 起项目已放弃「提升命中率」这个不可达目标，
转而诚实回答「这些策略真的有信号吗」。本模块的两个端点就是这个回答的出口 ——
**它们的价值恰恰在于如实报告"没有显著信号"**，而不是给出好看的数字。
"""
from __future__ import annotations

import random
from typing import Dict, List, Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from . import db
from .jsonutil import jsonable as _jsonable

router = APIRouter()

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
                "error": "随机性检验功能在本部署中不可用（该模块未安装或未启用）。",
                "detail": "内部：lottery.rigor 导入失败",
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
                "error": "随机性检验的该项功能在本版本中不可用。其他检验项不受影响。",
                "detail": f"内部：lottery.rigor 未提供 {fn_name}()",
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


@router.get("/api/rigor/randomness")
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
                             "error": "本地还没有开奖数据。请先抓取或导入历史开奖（详见部署文档「数据准备」一节），完成后再试。"}, status_code=400)
    if limit:
        draws = draws[-int(limit):]
    report, resp = _rigor_call_or_503("randomness_report", draws)
    if resp is not None:
        return resp
    if not isinstance(report, dict):
        return JSONResponse({"ok": False, "available": False,
                             "error": "随机性检验数据暂时无法生成。这不是你操作的问题，稍后重试即可。",
                "detail": "内部：randomness_report() 返回结构异常",
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


@router.get("/api/rigor/signal")
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
                             "error": "本地还没有开奖数据。请先抓取或导入历史开奖（详见部署文档「数据准备」一节），完成后再试。"}, status_code=400)

    series = _strategy_hit_series(draws, strategy, window)
    if not series:
        return JSONResponse(
            {"ok": False, "available": True, "strategy": strategy, "window": window,
             "n_trials": 0,
             "error": "样本不足：可对照期数少于窗口长度，无法做显著性检验。请先积累更多已开奖期次再试。",
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



@router.get("/api/rigor/walk_forward")
def rigor_walk_forward(method: str = Query("stat:freq"),
                       color: str = Query("blue"),
                       n_tickets: int = Query(5, ge=1, le=50),
                       start: int = Query(0, ge=0),
                       refresh: int = Query(0, ge=0, le=1)):
    """V23 / L3：**前向回测**的公开读数——「这套系统到底能不能向前预测」。

    ## 为什么要把它放到首屏

    docs/analysis/V19_AI_REDESIGN.md §4 层 3：把「我们试过了，数据说没有」
    变成可复核的事实，而不是让用户对着一个「AI 预测」的空壳猜。

    现有 `/api/rigor/signal` 做的是「策略历史表现能否与随机区分」；
    本端点做的是**更朴素也更硬**的一件事：**在时间上向前预测，命中率是否高于随机**。

    ## 判据（与 V20 / L0 同一套，不另立标准）

    * 多种子（`WF_SEEDS`）各跑一遍，取 z **中位数**；
    * 中位数 > `WF_Z_MIN`（1.0）才叫「过线」；
    * 单次 z 过线但多种子不稳健 → 明确判否并说明原因。

    ## 诚实边界（写进返回体，前端必须显示）

    过线 ≠ 有优势。这只是「不比随机差且有一点正向迹象」的最低诚信线。

    ``start=0`` 表示按数据长度自适应（留出至少 400 期做前向评估）。
    """
    method = (method or "").strip()
    color = (color or "blue").strip().lower()
    if color not in ("blue", "red"):
        return JSONResponse({"ok": False,
                             "error": ("color 参数取值不对，只接受 blue 或 red。"
                                       "请把请求里的 color 改成 blue（蓝球）或 red（红球）后重试。")},
                            status_code=400)
    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False,
                             "error": "本地还没有开奖数据。请先抓取或导入历史开奖，完成后再试。"},
                            status_code=400)
    try:
        from . import walk_forward_gate as WFG
    except ImportError as e:
        return JSONResponse({"ok": False, "available": False,
                             "error": "前向回测模块尚未就绪。该功能在本部署中不可用；其他功能不受影响。",
                             "detail": str(e)}, status_code=503)

    _start = int(start) or max(300, min(2917, len(draws) - 400))
    try:
        res = WFG.walk_forward_check(draws, method, n_tickets=int(n_tickets),
                                     start=_start, color=color)
    except Exception as e:      # noqa: BLE001
        return JSONResponse({"ok": False, "error": "前向回测没能完成。请重试一次；若仍失败，请反馈维护者。",
                             "detail": f"{type(e).__name__}: {e}"}, status_code=503)

    res["n_draws"] = len(draws)
    res["z_min"] = WFG.WF_Z_MIN
    res["seeds"] = list(WFG.WF_SEEDS)
    # 前端必须能直接渲染一句诚实结论，而不是自己从数字里猜。
    if not res.get("replayable", True):
        res["headline_zh"] = "该方法不可历史回放 —— 无法提供前向预测证据。"
    elif res.get("passed"):
        res["headline_zh"] = (
            "前向回测过线（z 中位数 {:.2f}）。注意：这只是最低诚信线，"
            "不代表能提高中奖率。".format(float(res.get("z_median") or 0)))
    else:
        res["headline_zh"] = (
            "未检出可预测性（z 中位数 {}）。本系统提供的是结构分析，不是中奖保证。"
            .format(res.get("z_median")))
    res["honesty_zh"] = (
        "双色球为独立随机事件。前向回测是对「能不能向前预测」的检验；"
        "不过线是**最正常**的结果，它说明系统没有在假装能预测。")
    return res
