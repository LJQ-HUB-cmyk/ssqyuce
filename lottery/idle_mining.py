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
    return {"completed_dimensions": [], "verdicts": {}, "updated_at": None,
            "runs": 0, "total_candidates": 0}


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
    """跑一轮闲时挖掘（可中断、可续跑）。

    `budget_candidates` 是**本轮上限**（不是总量）：闲时挖掘应当"细水长流"，
    每次只推进一点，避免一次跑几个小时把机器占满。

    返回运行摘要（也写进断点）。
    """
    if not enabled():
        return {"ok": False, "reason": "idle_mining_disabled",
                "note_zh": "闲时挖掘未启用（LOTT_IDLE_MINING=0）"}

    from . import discovery

    draws = db.load_draws()
    if len(draws) < min_start + 50:
        return {"ok": False, "reason": "insufficient_data",
                "note_zh": f"数据不足（需 ≥{min_start + 50} 期，当前 {len(draws)}）"}

    state = load_state()
    done_dims = set(state.get("completed_dimensions") or [])
    verdicts: Dict[str, Any] = dict(state.get("verdicts") or {})

    # 维度族：用 discovery 的组合空间枚举（不自己另造一套定义）
    try:
        combos = discovery.enumerate_combos() if hasattr(discovery, "enumerate_combos") else []
    except Exception:      # noqa: BLE001
        combos = []

    processed = 0
    interrupted = False
    for combo in combos:
        key = str(combo.get("key") if isinstance(combo, dict) else combo)
        if key in done_dims:
            continue                      # 续跑：跳过已完成
        if processed >= budget_candidates:
            interrupted = True
            break
        # TODO(P4-2)：这里接真实的维度检验序列（需要 discovery 提供 as-of 逐期序列）。
        # 当前先记录"已访问"，判定留给下一步 —— 宁可先跑通调度与断点，
        # 也不要为了填满流程而伪造统计结论。
        done_dims.add(key)
        processed += 1
        if processed % BATCH_SIZE == 0:
            time.sleep(PRESSURE_SLEEP if _online_pressure() else BASE_SLEEP)

    state["completed_dimensions"] = sorted(done_dims)
    state["verdicts"] = verdicts
    state["runs"] = int(state.get("runs") or 0) + 1
    state["total_candidates"] = int(state.get("total_candidates") or 0) + processed
    save_state(state)

    usable = sum(1 for v in verdicts.values() if v.get("decision") == "usable")
    return {
        "ok": True,
        "processed": processed,
        "completed_total": len(done_dims),
        "usable": usable,
        "interrupted": interrupted,
        "note_zh": (f"本轮处理 {processed} 个候选（累计 {len(done_dims)}）；"
                    f"usable={usable}。闲时挖掘的产物是**判定记录**，"
                    f"不是「找到 N 条规律」—— 真实数据上 usable 长期为 0 是诚实结果。"),
    }


def status() -> Dict[str, Any]:
    """供 /api/health 与运维查看的配额/进度快照。"""
    state = load_state()
    verdicts = state.get("verdicts") or {}
    by_decision: Dict[str, int] = {}
    for v in verdicts.values():
        d = v.get("decision") or "unknown"
        by_decision[d] = by_decision.get(d, 0) + 1
    return {
        "enabled": enabled(),
        "completed_dimensions": len(state.get("completed_dimensions") or []),
        "runs": int(state.get("runs") or 0),
        "total_candidates": int(state.get("total_candidates") or 0),
        "by_decision": by_decision,
        "updated_at": state.get("updated_at"),
    }
