"""闲时挖掘调度器（P4）。

## 它要解决的问题

系统已经有挖掘引擎（`discovery`/`multiscale`/`regime`），但**没有让它在闲时跑起来**。
M2 的说法很准：「闲时自动挖掘」要建的不是新引擎，而是**给已有引擎装一个能真正
跑起来的调度**。

一天全量交叉能轻松产出上万次检验（124 维度 × 5 窗口 × 23 分段 = 14 260）。
没有治理的后果是精确的：在 m=14260 处，Bonferroni 阈值 z=4.63，
把系统的最小可检效应从 +5.9% 抬到 +7.4%，**而且是永久的**（累计口径不回落）。
这就是为什么本模块的判定必须走 e-process（见 `eprocess.py`）。

## 三条硬约束

### 1. 在线请求优先（让路）

挖掘是 CPU 密集的，跑满会把 40 线程池吃光 → healthcheck 超时 → 容器被判不健康。
本模块用**轮次 + 批间让路**实现：每一小批之后 `time.sleep(0)` 让出 GIL，
并在检测到"最近有在线请求"时加大间隔。

### 2. 可中断、可续跑

中段杀进程（部署、OOM）不能丢进度，也不能重头再来。
状态存 `data/idle_mining_state.json`：已完成到哪个维度、哪些候选已判定。

### 3. 总开关

`LOTT_IDLE_MINING=0` 时整个模块不启动。默认为 **0（关闭）**：
这是新机制，先观察再开，而不是默认改变生产行为。

## 它输出什么

**不是**"找到 N 条规律"这类结论，而是**候选的判定记录**：
每条带 `e_value / alpha_spent / n_obs / decision`（L-C 报告层要求）。
新增的"可用规律"条数在真实数据上大概率是 **0** —— 那是诚实的结果。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config, db, eprocess

#: 每批处理的候选数（批间让路，避免长时间占满 CPU）
BATCH_SIZE = 8
#: 批间基础休眠（秒）
BASE_SLEEP = 0.05
#: 检测到在线请求压力时加大的休眠
PRESSURE_SLEEP = 0.5
#: 判定所需最小观测数（低于此不下结论）
MIN_OBS = 30

#: 候选空间版本。2 = 生成式开放空间（单因子网格 + 跨族交叉）。
#: 旧断点（1 = 封闭的 124 维遍历）语义不兼容，读到就重置游标。
SPACE_VERSION = 2
#: 只保留 e_value 最高的若干个 monitor 候选（避免断点膨胀到几十 MB）
MAX_MONITOR_TOP = 50


def _fresh_state() -> Dict[str, Any]:
    """断点的初始结构（新空间语义）。"""
    return {
        "space_version": SPACE_VERSION,
        "cursors": {"single": 0, "cross": 0},
        "by_decision": {},
        "candidates": {},
        "usable": {},
        "monitor_top": {},
        "n_checked": 0,
        "runs": 0,
        "total_candidates": 0,
        "updated_at": None,
    }


def _migrate_state(state: Dict[str, Any]) -> Dict[str, Any]:
    """把旧版断点迁移到当前候选空间语义。

    旧断点用 `completed_dimensions`（124 个「族:参数」字符串）记录进度。
    新空间是**开放**的且层内顺序不同 —— 旧的已完成集合无法映射成游标，
    强行保留会导致「以为挖过、其实没挖」。因此版本不符时重置游标，
    只保留 runs 等无害计数，并留一条 `migrated_from` 说明。
    """
    if int(state.get("space_version") or 0) >= SPACE_VERSION:
        return state
    old_checked = len(state.get("completed_dimensions") or {})
    fresh = _fresh_state()
    fresh["runs"] = int(state.get("runs") or 0)
    fresh["migrated_from"] = {
        "space_version": int(state.get("space_version") or 1),
        "old_completed_dimensions": old_checked,
        "note_zh": "候选空间已改为生成式开放空间，旧进度不兼容，游标重置",
    }
    return fresh


def enabled() -> bool:
    """总开关。**默认关闭** —— 新机制先观察再开。"""
    return (os.environ.get("LOTT_IDLE_MINING") or "0").strip() == "1"


def state_path() -> Path:
    return Path(config.DATA_DIR) / "idle_mining_state.json"


def load_state() -> Dict[str, Any]:
    """读断点。坏文件一律当作"从头开始"，不让它卡死整个调度。"""
    try:
        p = state_path()
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (ValueError, OSError):
        pass
    return _fresh_state()


def save_state(state: Dict[str, Any]) -> None:
    """原子写断点（先写 tmp 再 replace），避免半写坏文件。"""
    try:
        p = state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        state["updated_at"] = time.time()
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(p))
    except OSError as e:
        print(f"[idle] 断点保存失败: {e}")


def _online_pressure() -> bool:
    """是否有在线请求在跑（用于决定让路力度）。

    读共享连接不现实（会与请求抢锁），这里用**进程线程数**做轻量代理：
    uvicorn 的 worker 线程在活跃时会增加。够用且零成本。
    """
    try:
        import threading
        return threading.active_count() > 4
    except Exception:      # noqa: BLE001
        return False


def judge_candidate(z_scores: List[float], *,
                    alpha: float = eprocess.DEFAULT_ALPHA,
                    tau: float = eprocess.DEFAULT_TAU) -> Dict[str, Any]:
    """对单个候选给出 e-process 判定（委托给 `eprocess.verdict`）。

    本函数存在的意义是**固定入口**：所有闲时挖掘的判定都走这里，
    将来换统计方法只需改这一处，不必在调度逻辑里找散落的判断。
    """
    v = eprocess.verdict(z_scores, tau=tau, alpha=alpha, min_obs=MIN_OBS)
    v["source"] = "idle_mining"
    return v


def run_once(*, budget_candidates: int = 200,
             min_start: int = 300) -> Dict[str, Any]:
    """跑一轮闲时挖掘（可中断、可续跑、**只挖没挖过的**）。

    `budget_candidates` 是**本轮上限**（不是总量）：闲时挖掘应当"细水长流"，
    每次只推进一点，避免一次跑几个小时把机器占满。

    ## 与旧实现的关键区别：从「遍历封闭集合」改为「推进开放空间」

    旧实现遍历 `discovery.FAMILY_BY_KEY` 里写死的 124 个组合，跑完即
    `processed=0` 永久空转 —— 那是**重复验证**，不是挖掘。
    现在改为在 `candidate_space` 的**开放空间**上按游标单向推进：
    单因子 692 项 → 跨族交叉 406 368 项，且交叉层是现有 29 族
    **从未检验过**的搜索空间。

    ## 为什么不做「回头复检 monitor」

    单向推进意味着一个候选只判定一次，monitor 不再回头观察。
    这是刻意的取舍：用户的诉求是「去找新的、之前没检验过的规律」，
    而不是反复验证同一批。回头复检属于另一件事（数据增长带来的持续观察），
    若要做应作为独立的复检队列，不该占用挖掘预算。

    返回运行摘要（也写进断点）。
    """
    if not enabled():
        return {"ok": False, "reason": "idle_mining_disabled",
                "note_zh": "闲时挖掘未启用（LOTT_IDLE_MINING=0）"}

    from . import candidate_space as CS, discovery

    draws = db.load_draws()
    if len(draws) < min_start + 50:
        return {"ok": False, "reason": "insufficient_data",
                "note_zh": f"数据不足（需 ≥{min_start + 50} 期，当前 {len(draws)}）"}

    state = _migrate_state(load_state())
    cursors: Dict[str, int] = dict(state.get("cursors") or {})
    by_decision: Dict[str, int] = dict(state.get("by_decision") or {})
    # ``candidates`` = 单候选过 α 阈的（尚未做族级校正）；
    # ``usable`` 由轮末 e-BH 从 candidates 里筛出，不在这里累加。
    candidates: Dict[str, Any] = dict(state.get("candidates") or {})
    monitor_top: Dict[str, Any] = dict(state.get("monitor_top") or {})
    n_checked = int(state.get("n_checked") or 0)

    n_draws = len(draws)
    scan_window = min(500, n_draws - min_start)
    quick_start = max(min_start, n_draws - scan_window)

    processed = 0
    skipped = 0
    interrupted = False
    layers_done: List[str] = []
    max_skip = CS.layer_size(CS.LAYER_SINGLE) + CS.layer_size(CS.LAYER_CROSS) + 8

    while processed < budget_candidates and skipped < max_skip:
        layer = _next_layer(cursors, CS)
        if layer is None:
            layers_done = [CS.LAYER_SINGLE, CS.LAYER_CROSS]
            break

        idx = int(cursors.get(layer) or 0)
        if idx >= CS.layer_size(layer):
            cursors[layer] = CS.layer_size(layer)
            continue

        got = CS.candidate_at(layer, idx)
        cursors[layer] = idx + 1        # 游标无论如何前进（不重复挖同一个索引）
        if not got:
            skipped += 1                # 非法候选：不消耗预算
            continue

        flt, fp = got
        try:
            scan = discovery.asof_scan(draws, flt, start=quick_start, end=n_draws)
            obs = scan.get("obs") or []
            exp = scan.get("exp") or []
            if len(obs) >= MIN_OBS and len(obs) == len(exp):
                # 红球标准差约 0.8678，蓝球方差 p*(1-p)
                sigma = 0.8678 if flt.get("target") == "red" else 0.24
                z_scores = [(float(o) - float(e)) / sigma for o, e in zip(obs, exp)]
                v = judge_candidate(z_scores)
                v["layer"] = layer
                v["index"] = idx
                v["label"] = flt.get("name_zh") or flt.get("key")
                v["target"] = flt.get("target")
                d = v.get("decision") or "unknown"
                by_decision[d] = by_decision.get(d, 0) + 1
                if d == "usable":
                    # **单候选过阈只是「候选」，不是「可用」** ——
                    # e-process 的 anytime-valid 只对单个假设负责，
                    # 族级假发现率必须由 e-BH 在轮末统一治理（见 _recompute_usable）。
                    candidates[fp] = v
                else:
                    _keep_monitor_top(monitor_top, fp, v)
            else:
                d = "underpowered"
                by_decision[d] = by_decision.get(d, 0) + 1
        except Exception as e:  # noqa: BLE001 —— 单个候选失败不中断整轮
            d = "error"
            by_decision[d] = by_decision.get(d, 0) + 1
            monitor_top.pop(fp, None)

        n_checked += 1
        processed += 1
        if processed % BATCH_SIZE == 0:
            time.sleep(PRESSURE_SLEEP if _online_pressure() else BASE_SLEEP)

    if processed >= budget_candidates:
        interrupted = True

    # 轮末统一做族级校正：把「候选」里经 e-BH 仍成立的升级为「可用」。
    usable = _recompute_usable(candidates, n_checked)

    state.update({
        "cursors": cursors,
        "by_decision": by_decision,
        "candidates": candidates,
        "usable": usable,
        "monitor_top": monitor_top,
        "n_checked": n_checked,
    })
    state["runs"] = int(state.get("runs") or 0) + 1
    state["total_candidates"] = n_checked
    save_state(state)

    remain_single = max(0, CS.layer_size(CS.LAYER_SINGLE)
                        - int(cursors.get(CS.LAYER_SINGLE) or 0))
    remain_cross = max(0, CS.layer_size(CS.LAYER_CROSS)
                       - int(cursors.get(CS.LAYER_CROSS) or 0))
    return {
        "ok": True,
        "processed": processed,
        "completed_total": n_checked,
        "usable": len(usable),
        "candidates": len(candidates),
        "interrupted": interrupted,
        "layers_done": layers_done,
        "remaining": {"single": remain_single, "cross": remain_cross},
        "cursors": dict(cursors),
        "note_zh": (f"本轮判定 {processed} 个**此前未检验过**的候选"
                    f"（累计 {n_checked}）；单候选过阈 {len(candidates)} 条，"
                    f"经 e-BH 族级校正后可用 {len(usable)} 条。"
                    f"剩余：单因子 {remain_single} + 跨族交叉 {remain_cross}。"),
    }


def _recompute_usable(candidates: Dict[str, Any],
                      n_checked: int) -> Dict[str, Any]:
    """用 e-BH 对全部「单候选过阈」的候选做族级 FDR 校正。

    m 用**累计检验总数**（`n_checked`），不是候选字典长度 ——
    分母必须是「一共检验过多少个假设」，否则校正形同虚设。
    """
    if not candidates:
        return {}
    fps = list(candidates.keys())
    e_vals = [float((candidates[fp] or {}).get("e_value") or 0.0) for fp in fps]
    sel = eprocess.ebh_selected(e_vals, m_total=max(len(fps), int(n_checked)))
    return {fps[i]: candidates[fps[i]] for i in sel}


def _next_layer(cursors: Dict[str, int], CS) -> Optional[str]:
    """按 single → cross 的顺序找出下一个**尚有余量**的层。"""
    for layer in (CS.LAYER_SINGLE, CS.LAYER_CROSS):
        if int(cursors.get(layer) or 0) < CS.layer_size(layer):
            return layer
    return None


def _keep_monitor_top(top: Dict[str, Any], fp: str, v: Dict[str, Any]) -> None:
    """保留 e_value 最高的若干 monitor 候选，供「最接近发现」展示。

    只留 top-N：monitor 是绝大多数（几十万），全存会让断点膨胀到几十 MB。
    """
    if v.get("decision") != "monitor":
        return
    top[fp] = {k: v.get(k) for k in ("e_value", "n_obs", "label", "layer", "index")}
    if len(top) > MAX_MONITOR_TOP:
        worst = min(top, key=lambda k: float(top[k].get("e_value") or 0.0))
        top.pop(worst, None)



def status() -> Dict[str, Any]:
    """供 /api/health 与运维查看的进度快照。

    关键字段是 **`progress`（开放空间的挖掘进度）** —— 这是「闲时挖掘到哪了、
    还剩多少没挖」的唯一可信来源。旧实现只有一个 `completed_dimensions` 计数，
    看不出空间有多大、是否已经空转。
    """
    from . import candidate_space as CS

    state = _migrate_state(load_state())
    cursors = state.get("cursors") or {}
    space = CS.stats()
    single_done = min(int(cursors.get(CS.LAYER_SINGLE) or 0), space["single_total"])
    cross_done = min(int(cursors.get(CS.LAYER_CROSS) or 0), space["cross_total"])
    checked = int(state.get("n_checked") or 0)
    total_space = space["single_total"] + space["cross_total"]
    return {
        "enabled": enabled(),
        "space_version": SPACE_VERSION,
        # 兼容旧字段名：语义已变为「累计判定过的候选数」
        "completed_dimensions": checked,
        "checked": checked,
        "runs": int(state.get("runs") or 0),
        "total_candidates": checked,
        "by_decision": dict(state.get("by_decision") or {}),
        # **区分「候选」与「可用」**：前者只是单候选过了 α 阈（数万级搜索里
        # 纯随机也会跑出一批），后者才通过 e-BH 族级校正。两者混为一谈就是骗自己。
        "candidate_count": len(state.get("candidates") or {}),
        "usable_count": len(state.get("usable") or {}),
        "progress": {
            "single_done": single_done,
            "single_total": space["single_total"],
            "cross_done": cross_done,
            "cross_total": space["cross_total"],
            "space_total": total_space,
            "remaining": max(0, total_space - single_done - cross_done),
            "pct": round(100.0 * (single_done + cross_done) / total_space, 4)
            if total_space else 0.0,
        },
        "monitor_top": sorted(
            (state.get("monitor_top") or {}).items(),
            key=lambda kv: -float((kv[1] or {}).get("e_value") or 0.0))[:5],
        "updated_at": state.get("updated_at"),
        "migrated_from": state.get("migrated_from"),
    }
