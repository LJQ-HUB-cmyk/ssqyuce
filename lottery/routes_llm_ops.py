"""LLM 运维与形态/多尺度诊断域路由（P2 从 api_app 的 V4 域切出）。

## 这里的端点大多是「API-only 运维面」

前端不调用它们（M4 评审 §6 B 档）。保留的理由是它们有测试、且是安全底线：

* `/api/llm/cost` —— 花了多少钱、换到了什么。**零数据时返回 None 而不是 0**
  （"没有数据"与"花了钱没换到东西"是两个结论）。
* `/api/llm/decision/{status,restore}` —— 决策技能不达标时自动关停的
  状态查询与恢复入口。
* `/api/shape/*`、`/api/multiscale/*` —— L2 结构约束与多尺度扫描的核对口。
* `/api/samples/side-by-side` —— 真实样本与回放样本并排，防止"样本偷换"。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from . import config, db
from .decision_skill import (
    DECISION_SKILL_BASELINE,
    _passed_pattern_count,
    _usable_decision_count,
    _wilson_lower,
    decision_skill,
    llm_marginal_value,
)
from .jsonutil import jsonable as _jsonable

router = APIRouter()

@router.get("/api/llm/arm")
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


@router.get("/api/shape/rules")
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


@router.get("/api/shape/constraints")
def shape_constraints_endpoint():
    """V12：当前出号使用的 5 维形态约束（和值/跨度/三区/奇偶/连号）。"""
    from . import shape_rules as SR
    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "error": "本地暂无开奖数据"}, status_code=400)
    ctx = SR.shape_constraint_ctx(draws)
    ctx["ok"] = True
    return _jsonable(ctx)


@router.get("/api/multiscale/mde-table")
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


@router.get("/api/multiscale/scan")
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


@router.get("/api/samples/side-by-side")
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


@router.get("/api/llm/cost")
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


@router.get("/api/llm/decision/status")
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


@router.post("/api/llm/decision/restore")
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
