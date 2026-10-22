"""规律研究域路由（阶段有效性 / 挖掘发现 / 生命周期 / LLM 工坊 / 战斗回放 / 自进化）。

P2 模块化：从 `api_app.py` 切出（原 V5 + V6 两段，共 18 个端点）。

## 这是「找规律」的主战场，也是本项目最需要诚实的模块

端点很多，但要分清它们回答的是**不同的问题**（M4 指出全站有 6 种「规律」含义）：

* `/api/regime/*` —— 数据在某些**阶段**是否表现出统计差异（不是可预测性）
* `/api/discovery/*` —— 维度扫描：哪些维度有值得进一步检验的迹象
* `/api/rules/lifecycle/*` —— 规律状态机（persistent/emerging/fading/...）
* `/api/rules/closed-loop/async` —— 挖掘→验证→注册→刷新快照的闭环
* `/api/battle/report` —— 各方法在真实开奖上的**实战**战绩
* `/api/evolution/*` —— 假说库与进化进度

## 三条必须守住的不变量

1. **LLM 假设永不进入 α 台账**：`/api/llm_lab/*` 产出的规律不可回放，
   进 α 台账会污染所有人的检验预算。注册只走 `source="stat"`。
2. **`usable` 恒为 0 是结论不是故障**：3516 期真实数据上 A 档 = 0，
   这是「没有可预测信号」的诚实答案。
3. **battle / hypothesis / evolution 任一模块未就绪都必须降级而非 500**
   （`_battle_or_503` 等）。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from . import config, db
# 不要写 `from .bg_tasks import _bg_run`：那会在本模块产生独立绑定，
# 打桩 `api_app._bg_run` 就管不到这里（测试会静默失效 —— 本项目已踩过两次
# 同类坑，见 tests/test_p2_dependency_direction.py 的守护测试）。
from . import bg_tasks
# 同理走模块属性（不要 `from ... import _evolution_step`），保证打桩能生效。
from . import evolution_service
from .jsonutil import jsonable as _jsonable
from .request_context import current_user_id

router = APIRouter()

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
             "error": "战斗回放功能在本部署中不可用（该模块未安装或未启用）。",
            "detail": "内部：lottery.battle 导入失败",
             "detail": str(e), "hint": "该端点需 lottery/battle.py；其余功能不受影响。"},
            status_code=503)
    return battle, None


# ---------- V6：阶段有效性（regime）+ 多维度规律发现 + LLM 规律工坊 ----------

@router.get("/api/regime/overview")
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
                             "error": "阶段时效模块尚未就绪。该功能在本部署中不可用；其他功能不受影响。若是部署方，请确认相关模块已随包安装。", "detail": str(e)},
                            status_code=503)
    draws = db.load_draws()
    if limit:
        draws = draws[-int(limit):]
    if not draws:
        return JSONResponse({"ok": False, "error": "本地还没有开奖数据。请先抓取或导入历史开奖（详见部署文档「数据准备」一节），完成后再试。"}, status_code=400)
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


REGIME_STATUS_ORDER = ["persistent", "persistent_effect", "emerging", "fading", "stale", "flipped",
                       "reversed", "noise", "no_dimension", "insufficient", "error"]


def regime_status_label(status: str) -> str:
    return {
        "persistent": "持续有效（最近阶段仍显著）",
        # S2：长期效应档。**措辞必须与 persistent 明确区分**——它描述的是「长期平均
        # 有显著效应」，当前这一段不一定在生效；绝不能写成「当前有效」。
        "persistent_effect": "长期效应（长期平均显著，当前不一定在生效）",
        "emerging": "新兴线索（只有最近阶段显著，未确认）",
        "fading": "正在衰减（上一阶段有效，最近失效）",
        "stale": "陈旧（曾显著，已被新数据推翻）",
        "flipped": "方向反转（疑似伪规律）",
        "reversed": "已失效（仅早期阶段显著）",
        "noise": "未检出（各阶段均不显著）",
        "no_dimension": "无可回放维度（结构类/权重类）",
        "insufficient": "样本不足",
        "error": "阶段分析暂时无法完成。稍后重试；若持续出现，请反馈维护者。",
        "unannotated": "尚未标注（点「重算阶段时效」）",
    }.get(status or "", status or "")


@router.post("/api/regime/refresh")
def regime_refresh(start: int = Query(200, ge=60, le=3000)):
    """重算全部假说的阶段时效标注（写回 data/hypotheses.json）。"""
    try:
        from . import evolution
    except ImportError as e:
        return JSONResponse({"ok": False, "error": "阶段时效模块尚未就绪。该功能在本部署中不可用；其他功能不受影响。若是部署方，请确认相关模块已随包安装。",
                             "detail": str(e)}, status_code=503)
    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "error": "本地还没有开奖数据。请先抓取或导入历史开奖（详见部署文档「数据准备」一节），完成后再试。"}, status_code=400)
    try:
        r = evolution.annotate_hypotheses(draws, start=int(start))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "标注没能保存。请重试一次；若仍失败，请确认本地数据文件可写。",
                             "detail": f"{type(e).__name__}: {e}"}, status_code=503)
    return {"ok": True, **r}


@router.get("/api/discovery/families")
def discovery_families():
    """维度表：可搜索的规律空间（20+ 族、90+ 组参数）。"""
    try:
        from . import discovery
    except ImportError as e:
        return JSONResponse({"ok": False, "error": "维度模块尚未就绪。该功能在本部署中不可用；其他功能不受影响。若是部署方，请确认相关模块已随包安装。",
                             "detail": str(e)}, status_code=503)
    fams = discovery.family_catalog()
    dims: Dict[str, int] = {}
    for f in fams:
        dims[f["dim_zh"]] = dims.get(f["dim_zh"], 0) + f["n_param_sets"]
    return {"ok": True, "families": fams, "n_families": len(fams),
            "n_combos": discovery.combo_space_size(), "dims": dims}


@router.get("/api/discovery/scan")
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
        return JSONResponse({"ok": False, "error": "维度模块尚未就绪。该功能在本部署中不可用；其他功能不受影响。若是部署方，请确认相关模块已随包安装。",
                             "detail": str(e)}, status_code=503)
    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "error": "本地还没有开奖数据。请先抓取或导入历史开奖（详见部署文档「数据准备」一节），完成后再试。"}, status_code=400)
    flist = [x.strip() for x in families.split(",") if x.strip()] if families else None
    try:
        res = discovery.discover(draws, top=int(top), fast_window=int(fast_window),
                                 families=flist, use_cache=not bool(refresh))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "扫描没能完成。请重试一次；若仍失败，请确认数据完整后反馈维护者。",
                             "detail": f"{type(e).__name__}: {e}"}, status_code=503)
    out = dict(res)
    out["ok"] = True
    out["n_draws"] = len(draws)
    return out


def _llm_lab_modules():
    from . import discovery, llm_lab
    return discovery, llm_lab


@router.get("/api/llm_lab/latest")
def llm_lab_latest(limit: int = Query(40, ge=1, le=200)):
    """最近一次规律工坊的结果（LLM 规律 + 逐条落地验证 + 阶段画像）。"""
    try:
        _, llm_lab = _llm_lab_modules()
    except ImportError as e:
        return JSONResponse({"ok": False, "available": False,
                             "error": "LLM 规律工坊模块尚未就绪。该功能在本部署中不可用；其他功能不受影响。若是部署方，请确认相关模块已随包安装。", "detail": str(e)},
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
@router.post("/api/llm_lab/register")
def llm_lab_register():
    """把工坊里 verified 的规律注册进假说库（unmappable/noise 不入库）。"""
    try:
        from . import evolution
    except ImportError as e:
        return JSONResponse({"ok": False, "error": "进化模块尚未就绪。该功能在本部署中不可用；其他功能不受影响。若是部署方，请确认相关模块已随包安装。",
                             "detail": str(e)}, status_code=503)
    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "error": "本地还没有开奖数据。请先抓取或导入历史开奖（详见部署文档「数据准备」一节），完成后再试。"}, status_code=400)
    try:
        r = evolution.register_llm_lab_claims(draws)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "注册没能完成。请检查必填项后重试；若仍失败，请把错误时间反馈维护者。",
                             "detail": f"{type(e).__name__}: {e}"}, status_code=503)
    return {"ok": True, **r}


@router.get("/api/backtest/regime")
def backtest_regime(key: str = Query(..., min_length=1),
                    params: Optional[str] = Query(None),
                    start: int = Query(200, ge=60, le=3000)):
    """任意维度组合的**检索式核验**：as-of 回放 + 阶段时效（前端「核验」按钮用）。

    ``params`` 是 JSON 字符串，例如 ``{"window":30,"k":6}``。
    """
    try:
        from . import discovery
    except ImportError as e:
        return JSONResponse({"ok": False, "error": "维度模块尚未就绪。该功能在本部署中不可用；其他功能不受影响。若是部署方，请确认相关模块已随包安装。",
                             "detail": str(e)}, status_code=503)
    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "error": "本地还没有开奖数据。请先抓取或导入历史开奖（详见部署文档「数据准备」一节），完成后再试。"}, status_code=400)
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
        return JSONResponse({"ok": False, "error": "回放没能生成。请重试一次；若仍失败，请确认该期开奖数据已存在。",
                             "detail": f"{type(e).__name__}: {e}"}, status_code=503)
    return {"ok": True, "filter": {"key": key, "params": p, "label": flt["label"],
                                   "target": flt["target"]}, "regime": rep}


@router.get("/api/rules/lifecycle")
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


@router.post("/api/rules/lifecycle/run")
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
        return JSONResponse({"ok": False, "error": "本地还没有开奖数据。请先抓取或导入历史开奖（详见部署文档「数据准备」一节），完成后再试。"}, status_code=400)
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


@router.get("/api/rules/runtime")
def rules_runtime():
    """规律运行时快照：现在到底有哪些规律「可用」、哪些只监控、哪些已否决。"""
    try:
        from . import patterns_runtime
    except ImportError as e:
        return JSONResponse({"ok": False, "available": False,
                             "error": "规律运行时模块尚未就绪。该功能在本部署中不可用；其他功能不受影响。若是部署方，请确认相关模块已随包安装。", "detail": str(e)},
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


@router.post("/api/rules/refresh")
def rules_refresh():
    """重算规律运行时快照（阶段时效 + 维度扫描 + 假说库 → 快照文件，约 5~20 秒）。"""
    try:
        from . import patterns_runtime
    except ImportError as e:
        return JSONResponse({"ok": False, "error": "规律运行时模块尚未就绪。该功能在本部署中不可用；其他功能不受影响。若是部署方，请确认相关模块已随包安装。",
                             "detail": str(e)}, status_code=503)
    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "error": "本地还没有开奖数据。请先抓取或导入历史开奖（详见部署文档「数据准备」一节），完成后再试。"}, status_code=400)
    try:
        r = patterns_runtime.refresh(draws)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "刷新没能完成。请重试一次；若仍失败，请确认数据源可用后反馈维护者。",
                             "detail": f"{type(e).__name__}: {e}"}, status_code=503)
    return {"ok": True, **r}
@router.post("/api/rules/closed-loop/async")
def rules_closed_loop_async(rounds: int = Query(3, ge=1, le=3),
                            use_llm: int = Query(1, ge=0, le=1)):
    """**AI 规律闭环（后台任务版）**：立即返回 task_id，前端轮询进度。

    闭环内容与 ``/api/rules/closed-loop`` 相同（挖掘→验证→注册→刷新快照），
    但不再让浏览器干等：每完成一个阶段就更新任务的 progress/message。
    """
    try:
        from . import llm_lab
    except ImportError as e:
        return JSONResponse({"ok": False, "error": "LLM 规律工坊模块尚未就绪。该功能在本部署中不可用；其他功能不受影响。若是部署方，请确认相关模块已随包安装。",
                             "detail": str(e)}, status_code=503)
    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "error": "本地还没有开奖数据。请先抓取或导入历史开奖（详见部署文档「数据准备」一节），完成后再试。"}, status_code=400)
    tid = bg_tasks._bg_run("closed_loop",
                  lambda p: llm_lab.run_closed_loop(draws, rounds=int(rounds),
                                                    use_llm=bool(use_llm), progress=p))
    return {"ok": True, "task_id": tid,
            "note_zh": "AI 规律闭环已在后台运行（约 1~8 分钟），可离开本页，完成后自动刷新"}


@router.post("/api/llm_lab/run/async")
def llm_lab_run_async(rounds: int = Query(3, ge=1, le=3),
                      use_llm: int = Query(1, ge=0, le=1)):
    """LLM 规律工坊（后台任务版），与 ``/api/llm_lab/run`` 同逻辑。"""
    try:
        from . import llm_lab
    except ImportError as e:
        return JSONResponse({"ok": False, "error": "LLM 规律工坊模块尚未就绪。该功能在本部署中不可用；其他功能不受影响。若是部署方，请确认相关模块已随包安装。",
                             "detail": str(e)}, status_code=503)
    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "error": "本地还没有开奖数据。请先抓取或导入历史开奖（详见部署文档「数据准备」一节），完成后再试。"}, status_code=400)
    tid = bg_tasks._bg_run("llm_lab",
                  lambda p: llm_lab.run_lab(draws, rounds=int(rounds),
                                            use_llm=bool(use_llm), progress=p))
    return {"ok": True, "task_id": tid,
            "note_zh": "LLM 规律工坊已在后台运行（约 1~8 分钟），可离开本页"}


@router.get("/api/battle/report")
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
                             "error": "本地还没有开奖数据。请先抓取或导入历史开奖（详见部署文档「数据准备」一节），完成后再试。"}, status_code=400)
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
                             "error": "战斗回放数据暂时无法生成。这不是你操作的问题，稍后重试即可。",
            "detail": "内部：battle_report() 抛出异常",
                             "detail": f"{type(e).__name__}: {e}"}, status_code=503)
    if not isinstance(report, dict):
        return JSONResponse({"ok": False, "available": False,
                             "error": "战斗回放数据暂时无法生成。这不是你操作的问题，稍后重试即可。",
             "detail": "内部：battle_report() 返回结构异常"}, status_code=503)
    out = dict(report)
    out.setdefault("ok", True)
    out["available"] = True
    out["n_draws"] = len(draws)
    # 全量回放耗时较长；给出可复现的参数快照
    out["params"] = {"limit": limit, "n_tickets": n_tickets,
                     "start": start, "seed": seed, "methods": mlist}
    return out


@router.get("/api/evolution/status")
def evolution_status_endpoint():
    """自进化进度：假说库统计 + 当前生效的出号策略覆盖项。

    只展示 ``validated`` 的假说为「生效」——未经验证的假说不得影响真实出号。
    """
    try:
        from . import evolution
    except ImportError as e:
        return JSONResponse({"ok": False, "available": False,
                             "error": "自进化功能在本部署中不可用（该模块未安装或未启用）。",
            "detail": "内部：lottery.evolution 导入失败",
                             "detail": str(e)}, status_code=503)
    try:
        st = evolution.evolution_status()
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "available": False,
                             "error": "自进化状态暂时无法读取。这不是你操作的问题，稍后重试即可。",
            "detail": "内部：evolution_status() 抛出异常",
                             "detail": f"{type(e).__name__}: {e}"}, status_code=503)
    st = dict(st or {})
    st.setdefault("ok", True)
    return st


@router.post("/api/evolution/run")
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
                             "error": "本地还没有开奖数据。请先抓取或导入历史开奖（详见部署文档「数据准备」一节），完成后再试。"}, status_code=400)
    try:
        # 手动触发**必须**绕过幂等闸门（用户的意图就是「现在再跑一轮」）。
        result = evolution_service._evolution_step(limit=int(limit), use_llm=bool(use_llm), force=True)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "available": False,
                             "error": "自进化单步执行失败。这不是你操作的问题，稍后重试即可。",
            "detail": "内部：_evolution_step() 抛出异常",
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
