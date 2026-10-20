"""预测域路由（出号 / 最近一期 / 严格模式门控）。

P2 模块化：从 `api_app.py` 切出。

## 这是全系统最核心的域，也是历史上缺陷最集中的地方

三个反复出问题的点，都在本模块里，改动前请先读：

1. **严格模式门控必须在缓存检查之前**（2026-10-18 修过一次）：否则已有缓存
   会绕过门控，表现为"严格模式开着却出了纯统计票"。
2. **严格模式必须按用户判定**（MU-2 / 裁决 2-A）：`LOTT_STRICT_MODE=1`
   是部署级开关，但新用户默认没有 LLM 通道 —— 全局套用会让每个新用户点
   "生成预测"都收到 422，而纯统计通道明明可以出号。
3. **票单写入必须带 user_id**（MU-2）：旧实现 `DELETE FROM predictions
   WHERE issue=?` 不带 user，用户 B 重新生成会删光用户 A 同期的票，
   而票单只存这一份、不可恢复。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Sequence

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from . import config, db
from . import backtest as BT
# 注意：**不要**写成 `from .request_context import current_user_id`。
# 那样会在本模块产生一个独立的名字绑定，打桩 `request_context.current_user_id`
# 就管不到这里 —— 测试会静默失效（用例仍通过，但测的不是生产路径）。
# 本项目已因这类"打桩点与依赖源脱节"踩过两次坑（见
# tests/test_p2_dependency_direction.py 的同名守护测试）。
from . import request_context

router = APIRouter()

def _strict_scope_for_current_user():
    """严格模式对**当前用户**是否生效。返回 ``(active: bool, bypass_reason: str)``。

    ## 为什么严格模式必须按用户判定（Lead 裁决 2-A）

    `LOTT_STRICT_MODE=1` 是**部署级**开关，但它想表达的是"用户要求必须由 LLM
    出号"。多用户下这两者不再等价：

    * 管理员的说法是"我要求 LLM，没有 LLM 就别出号"；
    * 新注册用户**默认没有任何 LLM 配置**（用户已确认："默认不配置 llm key，
      需要用户自己配置"）。

    若把部署级开关直接套到所有人，结局是：**每一个新用户点"生成预测"都收到 422
    "严格模式下不出号"，而他能做的只有去配置一个 API Key** —— 这等于用一个
    全局开关把系统对新用户完全锁死，而"纯统计通道"明明可以照常出号。

    因此作用域收敛为：**只有真正有可用 LLM 通道的用户，严格模式才对他生效**；
    没有通道的用户**绕过**门控，走纯统计出号，并在响应里如实说明原因
    （`strict_bypassed` + 提示文案）—— 既不静默降级，也不算"出号成功"。
    """
    if not bool(getattr(config, "STRICT_MODE", False)):
        return False, ""
    try:
        from . import user_llm_store
        uid = request_context.current_user_id()
        eff = user_llm_store.effective_for_user(uid)
        if eff.get("llm_available"):
            return True, ""
        _why = eff.get("reason") or "未配置 LLM"
        return False, (
            f"严格模式对当前账号**不生效**：没有可用的 LLM 通道（{_why}）。"
            f"本次已按纯统计通道出号。若希望强制 LLM 出号，请在设置页配置自己的"
            f" API Key（或由管理员配置共享通道）后重试。")
    except Exception as e:      # noqa: BLE001
        # 判定失败**不**升级成"拒绝出号"：宁可出一份纯统计票，也不要让用户
        # 因为"读取配置抛了个异常"就拿不到任何结果。
        return False, f"严格模式作用域判定失败（{type(e).__name__}），已按纯统计通道出号。"


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


@router.api_route("/api/predict", methods=["GET", "POST"])
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
    _strict_active, _strict_bypass = _strict_scope_for_current_user()
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

    # 严格模式已在前置门控校验并强制 regenerate=True；这里补 use_llm 强制。
    # 注意 _strict_active 来自 _strict_scope_for_current_user()（按用户判定），
    # **不能**重新读 config.STRICT_MODE —— 那会把刚绕过门控的用户的 use_llm
    # 又强制成 True，于是绕过门控却仍然去调 LLM，必然抛 LLMChannelError → 503。
    _strict_notes = []
    if _strict_active:
        use_llm = True                                   # 严格模式强制走 LLM
        _strict_notes.append("严格模式：出号强制依赖 LLM + 规律，本次为现算（未走缓存）")
    elif _strict_bypass:
        # 绕过严格模式：**必须**如实告诉用户发生了什么，否则他会以为自己的
        # "必须 LLM 出号"设置生效了，而实际拿到的是纯统计票。
        use_llm = False
        _strict_notes.append(_strict_bypass)

    # MU-2：按用户的共享通道配额（U2 §3.4）。**必须在严格模式决策之后判定**，
    # 因为两者会冲突，而冲突的答案取决于严格模式是否对该用户生效：
    #
    #  * 严格模式生效（用户明确要求"必须 LLM 出号"）+ 额度用尽 →
    #    返回明确错误。**不能**降级成纯统计：那正是严格模式禁止的静默降级，
    #    用户会以为拿到的是 LLM 票。这里宁可如实失败。
    #  * 非严格模式 + 额度用尽 → 降级为纯统计 + 提示（U2 §5.3：绝不阻断出号）。
    #
    # 配额只对**系统共享通道**生效：自备 Key 花的是自己的钱，不该被系统卡住。
    _quota_note = ""
    try:
        from . import user_llm_store as _ULS
        _eff = _ULS.effective_for_user(request_context.current_user_id())
        if _eff.get("source") == "system":
            _q = _ULS.quota_exceeded(int(request_context.current_user_id()))
            if _q:
                if _strict_active:
                    return JSONResponse(
                        {"ok": False, "strict_mode": True, "llm_required": True,
                         "quota_exceeded": True,
                         "error": (f"严格模式下不出号：{_q}"
                                   "严格模式要求必须由大模型出号，而共享通道额度已用尽；"
                                   "请配置自己的 API Key，或关闭严格模式（LOTT_STRICT_MODE=0）"
                                   "以改用纯统计通道。")},
                        status_code=429)
                use_llm = False
                _quota_note = _q
    except Exception:      # noqa: BLE001 —— 配额判定失败绝不能阻断出号
        _quota_note = ""

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
    if _quota_note:
        _strict_notes.append(_quota_note)
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


@router.get("/api/predictions/last")
def last_predictions():
    """最近一期预测（含**生成时间**与数据基准，V7）。

    V6 及以前只返回 issue + tickets，前端因此无法显示"什么时候生成的"；
    现在补上 generated_at / data_basis / rules 摘要，让"这注是什么时候、基于哪期数据算的"可见。
    """
    from . import backtest as BT
    issue = None
    # 取候选期号时**先按时间**再按数值：越界/异常期号在两张排序下都可能置顶
    # （例：2026401 > 2026119），因此以"时间戳最新的一批"为准，再在合法期号里取最大。
    # ⚠️ U3 §3.7 缺陷 W2：这条内联 SQL 原先**不带 user_id**。
    # 后果：用户 A 的"最近一期"会取到用户 B 的活动痕迹 → 前端显示别人的期号
    # （信息泄漏 + 体验错乱）。这里必须走 resolve_tenant，与 db 层同一判据。
    _uid = db.resolve_tenant()
    rows = db.get_conn().execute(
        "SELECT issue, MAX(created_at) AS ts FROM predictions WHERE user_id=?"
        " GROUP BY issue ORDER BY ts DESC, CAST(issue AS INTEGER) DESC LIMIT 12",
        (_uid,)).fetchall()
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
