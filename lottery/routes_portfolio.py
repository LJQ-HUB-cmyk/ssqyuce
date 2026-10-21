"""票单组合数学域路由（V4：期望值 / 分布）。

P2 模块化：从 `api_app.py` 的 V4 域切出。

## 这两个端点回答的是「票长什么样」，不是「能中多少」

组合期望值恒等于 `注数 × 2 元 × (1-返奖率)`，与号码选择无关 ——
这是本项目反复强调的诚实边界（LEAD_VERDICT §8.1）。
`/api/portfolio/expected-value` 存在的意义，是**让用户自己算一遍并看到它不随选号改变**。
"""
from __future__ import annotations

from typing import Dict, List

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from . import config, db
from .jsonutil import jsonable as _jsonable

router = APIRouter()

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


@router.get("/api/portfolio/expected-value")
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

    price = config.TICKET_PRICE
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

@router.get("/api/portfolio/distribution")
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


# ---------------------------------------------------------------------------
# V14-B2 / B3：大众偏好度 + 蓝球覆盖（两个新端点）
#
# 这两个端点是「A+B」方向的产品落点：
#   · popularity：预测「多少人会选这种号」（人群行为可预测，号码不可预测）
#   · blue-coverage：蓝球去重覆盖（组合恒等式的真实概率增益）
#
# 两者都**强制**携带披露，且都不出现在出号门控里 —— 它们是"看懂自己的票"，
# 不是"预测下一期"。
# ---------------------------------------------------------------------------

@router.get("/api/portfolio/popularity")
def portfolio_popularity(
    reds: str = Query(..., description="逗号分隔的 6 个红球，如 1,2,3,4,5,6"),
    sales: float = Query(0.0, description="预计本期销量（元），可选；不影响档位"),
) -> Dict:
    """查询该号码形态的「大众程度」档位。

    ## 它说的和它不说的话

    **说**：这种形态在历史上被多少彩民选中（低/中/高三档）。
    **不说**：下期会开什么号；会不会中奖。

    实测效应量：形态对头奖注数（`p1`）的增量 R² ≈ 0.008（0.8%），
    置换 p ≈ 0.005 —— 真实但极小，**只能排序，不能定值**。
    """
    try:
        parts = [int(x) for x in str(reds).replace("，", ",").split(",") if x.strip()]
    except ValueError:
        return JSONResponse({"ok": False, "error": "reds 必须是逗号分隔的整数"}, status_code=400)
    if len(parts) != 6:
        return JSONResponse({"ok": False, "error": f"需要 6 个红球，收到 {len(parts)} 个"},
                            status_code=400)
    if len(set(parts)) != 6 or not all(1 <= x <= 33 for x in parts):
        return JSONResponse({"ok": False, "error": "红球必须是 1~33 内互不相同的 6 个数"},
                            status_code=400)

    from . import popularity as POP
    model = POP.get_model()
    out = model.describe(parts, sales if sales else None)
    if not out.get("ok"):
        return JSONResponse({"ok": False, "error": "模型未就绪（数据不足或尚未拟合）"},
                            status_code=503)
    out["reds"] = sorted(parts)
    return _jsonable(out)


@router.get("/api/portfolio/blue-coverage")
def portfolio_blue_coverage(
    n_tickets: int = Query(10, ge=1, le=100, description="打算买几注"),
    full_ev: bool = Query(True, description="是否附带整注返奖比（含浮动奖估算）"),
) -> Dict:
    """蓝球去重覆盖计划 + **三个必须同屏的数字**。

    ## 为什么这个端点要求「三个数字必须一起看」

    单看「中奖概率从 47.6% 提到 62.5%」是激励性的，但配上
    「成本 20 元、六等奖期望回收 3.13 元、返奖比恒为 15.625%」才是完整的真相。

    所以本端点**永远**同时返回概率、成本、期望回收与 `disclosure` 文本，
    不存在只取概率的路径。
    """
    from . import blue_coverage as BC
    full_ratio = None
    assumption = None
    if full_ev:
        try:
            from . import engine
            # V14-A4 修正：`_single_ticket_ev` 的第一个参数必须是 **dict**。
            # 传整数会被静默忽略并回落到 PRIZE_ASSUMPTION_TABLE_A
            # （first=500万 / second=15万），这正是「同一返奖比两个值」的来源。
            # 这里显式传入实测中位口径（一等 684 万 / 二等 20.5 万，来自 3266 期
            # p1_amt / p2_amt），并把它作为 `prize_assumption` 一并返回 ——
            # 项目规则要求「一/二等奖为浮动奖，必须连假设表一起展示」。
            assumption = {"first": 6_840_000, "second": 205_000,
                          "label": "实测中位（3266 期 p1_amt/p2_amt）"}
            ev = engine._single_ticket_ev(assumption, sixth="published")
            full_ratio = float(ev["ev_ratio"])
            # `_single_ticket_ev` 返回的 `prize_assumption` 是**人读字符串**，
            # 不是 dict；这里两者都保留：dict 供结构化渲染，text 供披露文本。
            assumption = dict(assumption, text=str(ev.get("prize_assumption") or ""))
        except Exception:        # noqa: BLE001 —— 取不到就只给六等奖口径
            full_ratio = None
            assumption = None
    out = BC.optimize(n_tickets, full_ev_ratio=full_ratio,
                      prize_assumption=assumption)
    out["ok"] = True
    return _jsonable(out)
