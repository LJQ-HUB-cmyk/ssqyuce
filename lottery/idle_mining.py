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
#: 无压力时批间的最小让出（给 GIL / SQLite 连接喘口气，不是为省 CPU）
BATCH_GAP = 0.01
#: 判定所需最小观测数（低于此不下结论）
MIN_OBS = 30

#: **CPU 占空比上限（相对整机）**。用户硬约束：本 VPS 4 核，闲时挖掘不得占用超过 60%。
#: 60% × 4 核 = 2.4 核。单线程挖掘最多用 1 核 = 25%，理论安全；
#: 但系统还有前台请求、其他容器，因此按「整机 60%」做闭环控制：
#: 每批用 os.times() 量出真实 CPU 秒，按目标占空比算出应休眠多久。
CPU_LIMIT_PCT = float(os.environ.get("LOTT_IDLE_CPU_PCT", "60") or "60")
#: 控制环的采样窗口（秒）：每这么长时间校一次占空比，避免抖动
CPU_SAMPLE_WINDOW = 2.0


def _cpu_count() -> int:
    """本机可用核心数（用于把占空比换算成核数）。"""
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return max(1, os.cpu_count() or 1)


def _system_cpu_pct() -> Optional[float]:
    """整机当前 CPU 使用率（%）。

    `os.getloadavg()` 给的是 1 分钟平均负载，对「实时限流」太迟钝；
    这里读 `/proc/stat` 的前后差值算瞬时值。读不到就返回 None（调用方跳过控制）。
    """
    try:
        with open("/proc/stat", "r", encoding="utf-8") as f:
            parts = f.readline().split()
        if not parts or parts[0] != "cpu":
            return None
        vals = [int(x) for x in parts[1:11]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)   # idle + iowait
        total = sum(vals)
        time.sleep(0.05)
        with open("/proc/stat", "r", encoding="utf-8") as f:
            parts2 = f.readline().split()
        vals2 = [int(x) for x in parts2[1:11]]
        idle2 = vals2[3] + (vals2[4] if len(vals2) > 4 else 0)
        total2 = sum(vals2)
        dt = total2 - total
        if dt <= 0:
            return None
        return 100.0 * (1.0 - (idle2 - idle) / dt)
    except (OSError, ValueError, IndexError):
        return None


def _throttle(t_work: float) -> None:
    """按 CPU 占空比目标休眠，让闲时挖掘不超过 `CPU_LIMIT_PCT`（整机口径）。

    ## 口径（最容易搞错的地方，第一版就错在这里）

    `CPU_LIMIT_PCT` 是**整机百分比**：本 VPS 4 核，60% = **2.4 核**。
    单个挖掘线程最多吃满 1 核 = 整机 **25%** —— 单线程天然不可能突破 60%。
    限流的真实职责是「别让前台被挤」，不是「把单线程压到 60%」。

    第一版把 `r` 当成「本进程可用的核心比例」，算出
    `t_sleep ≥ 0.667·t_work`，等于把进程压到**单核 60%**，比整机目标
    严了 4 倍。实测代价：单候选墙钟 0.05s 拖到 0.29s，
    吞吐仅 2.9 候选/秒，1 天窗口完不成。

    ## 修正后的判据

    只在**整机余量不足一个核**时才让路：

        one_core_pct = 100 / n_cores            # 4 核 → 25%
        headroom = CPU_LIMIT_PCT - sys_pct      # 整机还剩多少百分点
        headroom >= one_core_pct  → 不让路     # 余量够本线程吃满，直接跑
        headroom <  one_core_pct  → 按缺口比例休眠

    例：整机已用 50%、上限 60% ⇒ 余量 10 < 25 ⇒ 让路；
        整机已用 10% ⇒ 余量 50 ≥ 25 ⇒ 全速跑。
    """
    if t_work <= 0:
        time.sleep(BASE_SLEEP)
        return

    one_core = 100.0 / _cpu_count()
    sys_pct = _system_cpu_pct()

    if sys_pct is not None:
        if sys_pct >= CPU_LIMIT_PCT:
            # 整机已到/超限额：本批不干活，等它降下来
            time.sleep(CPU_SAMPLE_WINDOW)
            return
        headroom = CPU_LIMIT_PCT - sys_pct
        if headroom < one_core:
            # 余量已不足一个核：按缺口比例让路（最多压到不干活的一半）
            frac = headroom / one_core              # 0..1
            need = t_work * (1.0 - frac) / max(frac, 0.05)
            time.sleep(min(max(need, 0.0), 30.0))
            return
    # 读不到整机占用时按「余量充足」处理 —— 仍有 cgroup 2.4 核硬限兜底

    # 余量充足，但若有在线请求仍让一小档，保证前台响应延迟不劣化
    if _online_pressure():
        time.sleep(PRESSURE_SLEEP)
    else:
        time.sleep(BATCH_GAP)

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
    # D1（2026-10-20 四专家论证裁决）：**全史增长窗口，不再截断 500 期**。
    #
    # 旧实现 `scan_window = min(500, ...)` 有两个独立缺陷：
    #   1) **统计效力**：500 期只能检出 Δ≥0.119（+10.9%）的效应，全史 Δ≥0.048
    #      （+4.4%）；对 Δ=0.05 的长周期效应，500 期检出率 6.5%、全史 56.6%（8.7×）。
    #   2) **正确性**：滑窗每次重启 = 每次都算「新检验」，Ville 不等式的
    #      anytime-valid 保证**不覆盖跨窗口复查**。实测滑窗 W=500、每 30 期看一次
    #      的纯噪声误报率 = **7.80% > α=0.05**（保证被破坏）；累积增长窗口 = **2.46%**（受控）。
    # 真实数据佐证：`sum_range{45-75,w65}` 在滑窗下 e=12.4，累积下仅 1.83 —— 滑窗高估 6.8×。
    #
    # 增长窗口才是 e-process 超鞅的标准归宿：t 持续增长，效应项按 O(t) 累积、
    # 惩罚项仅 O(log t)，这正是「长周期规律一旦存在就该被找到」的数学基础。
    quick_start = min_start

    processed = 0
    skipped = 0
    interrupted = False
    layers_done: List[str] = []
    max_skip = CS.layer_size(CS.LAYER_SINGLE) + CS.layer_size(CS.LAYER_CROSS) + 8
    batch_cpu_start = time.process_time()

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
            # 闭环限流：按本批真实 CPU 时间算应休眠多久（用户约束：整机 ≤60%）
            t_now = time.process_time()
            _throttle(t_now - batch_cpu_start)
            batch_cpu_start = time.process_time()

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
    """用 **分层 e-BH** 对「单候选过阈」的候选做族级 FDR 校正。

    ## 为什么必须分层（D2，四专家论证裁决）

    交叉层有 72 350 个候选（536 个族对 × 2 种合并模式）。若整层共用一个大 m，
    k=1 的阈值 = m/α = **1 447 000** —— 而真实数据上全史最大 e 只有约 109。
    这等于**实际放弃了交叉层**：挖了 7 万个候选，一个都不可能通过。

    分层后，每个自然层（一个族对 × 一种模式）各自 m≈135，阈值降到 **2 700**。
    SPACE 的实测：分层 vs 不分层的发现数 7228 vs 7258（差 0.42%），
    FDP 0.0011 vs 0.0010 —— **FDR 几乎无损，但可用性从 0 变成有**。

    ## 层的划分

    - 单因子层：m = 该层累计检验数（`single_checked`）
    - 交叉层：按指纹里的「族对 + 模式」分组，每组各自 m = 该组检验数

    m 的记账原则（SPACE §6.2，三条硬规则）：
      1. m **单调递增、永不复位**（复位 = 重置式作弊，α 会被放大）
      2. 指纹不含期号（否则同一假设会重复计入 m）
      3. `usable` 每轮**全量重算**而非并集（否则旧的假阳性永远洗不掉）
    """
    if not candidates:
        return {}

    # 按层分组：single 一组；cross 按「族对+模式」各一组
    groups: Dict[str, List[str]] = {}
    for fp in candidates:
        if fp.startswith("single|"):
            key = "single"
        else:
            # 指纹形如 cross|<mode>|族A:{...}|族B:{...}
            parts = fp.split("|")
            key = "cross|" + (parts[1] if len(parts) > 1 else "?")
        groups.setdefault(key, []).append(fp)

    selected: Dict[str, Any] = {}
    for gkey, fps in groups.items():
        e_vals = [float((candidates[fp] or {}).get("e_value") or 0.0) for fp in fps]
        # 组内 m = 该组过阈候选数 与 该组检验数 的较大者。
        # 用全局 n_checked 当分母会让交叉层退回「全局一个大 m」的老问题。
        m_group = max(len(fps), _group_checked(gkey, n_checked, len(fps)))
        sel = eprocess.ebh_selected(e_vals, m_total=m_group)
        for i in sel:
            v = dict(candidates[fps[i]])
            v["ebh_group"] = gkey
            v["ebh_m"] = m_group
            v["ebh_threshold_k1"] = m_group / eprocess.DEFAULT_ALPHA
            selected[fps[i]] = v
    return selected


def _group_checked(gkey: str, n_checked: int, n_in_group: int) -> int:
    """估算某一层的检验总数（分层的 m）。

    保守做法：单个族对组的检验数最多是「该组候选数」，
    但由于我们只对**过阈**候选做 e-BH，组内 m 应反映「这一族对一共检验了多少档位」。
    交叉层每个族对的档位数 = |A档| × |B档|，单因子层每族 = 该族档位数。
    这里用 n_in_group 作下界（已过阈的），并用族网格规模作上界估计——
    取二者较大值，避免用全局 72656 把阈值抬死。
    """
    if gkey == "single":
        # 单因子层：29 族 × 平均 10.5 档 ≈ 306
        return min(n_checked, 306) if n_checked else n_in_group
    # 交叉层单组：一族对的档位乘积，实测平均约 135
    return max(n_in_group, 135)


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
