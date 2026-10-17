"""V10-05 离线回放分表（``replay_results``）：严格 as-of 的方法级离线样本。

为什么要有这个模块（缺陷背景）
------------------------------
真实交战样本（``eval_details``）只有 30 注量级（``docs/V10_IMPROVEMENT_PLAN.md``
§0：2026115–2026119 共 5 期 × 5 注），在这个样本量下最小可检出效应
（``rigor._min_detectable_effect``）约 ±32%，**任何方法差异都测不出来**。
因此需要一份零成本的**离线样本外**证据：在历史数据上逐期 walk-forward 回放，
得到几千注量级的样本。

但它有两个必须守住的口径，否则这份证据比没有更糟：

1. **严格 as-of（只用 ``draws[:t]``）**
   V6 已实测证明：把阈值/分位用**全样本**（含第 t 期及以后）算出来再套到每期，
   回测会虚高 **0.0308 红球/期**——这已接近整个可检出量级（MDE ≈ 0.023/期 @
   n≈1000）。一旦泄漏未来信息，整份回放作废。本模块：

   * 第 t 期的**所有**输入都只来自 ``draws[:t]``，``draws[t+1:]`` **从不被读取**
     （``draws[t]`` 只用于给第 t 期打分）；
   * **阈值/分位**（和值分位、分区/奇偶区间、热度 top-k）用**滚动窗口**
     （``window`` 期，默认 500，与 ``engine.constraint_ctx`` 内部窗口一致）——
     既不用全样本阈值，也不含未来，更早于窗口的历史**不参与**；
   * 概率分布（``engine._rebuild_probs`` / 贝叶斯等）用**完整的 as-of 历史**
     ``draws[:t]``。这是合法口径而不是"全样本"：它只含第 t 期之前的信息，
     且不会像 V6 的"全样本阈值"那样把未来统计量套到早期期次上；
   * 每期随机数用 ``random.Random(seed + t)``（与 ``battle`` 同一约定），
     保证可复现且与其它期解耦。

   对应测试见 ``tests/test_v10_replay.py``：
   ``test_replay_ignores_future_draws``（改未来 → 结果一字不变）、
   ``test_replay_uses_past_history``（改历史 → 结果必变）、
   ``test_as_of_threshold_only_sees_rolling_window``（超出窗口的更早历史
   不影响、窗口内的历史必影响）、以及反向用例
   ``test_full_sample_threshold_would_show_fake_gain``（构造"只有用了未来数据
   才会出现的好结果"，断言实现不会出现）。

2. **必须显式传通道**
   ``evaluate.offline_backtest`` 直接调 ``E.predict_next(history, persist=False)``，
   会按**生产配置**算（``config.METHOD_MODE`` / ``config.METHODS_SPEC`` /
   环境变量 / ``methods_config.json``），回放结果因此依赖"当时的机器状态"。
   本模块要求调用方显式给出 ``channels``，缺省也走
   ``methods.resolve_channels(force_production=True)`` 并把
   ``fingerprint`` 写进每一行（``channels_fp``），使"哪一份通道配置产出了
   这批回放"可追溯；**不会**隐式落回 production 默认值去猜。

3. **与真实交战样本物理分表、禁止相加**
   离线回放写 ``replay_results``；真实交战在 ``eval_details``。两者**不同表、
   不同口径、不同样本量**（且离线回放是 walk-forward 模拟、真实交战含在线
   预测的全部现实噪声），相加得出的"总命中率"没有公共分母。
   :func:`samples_side_by_side` 提供**并排**展示（各自样本量 + 各自可检出 MDE），
   返回体里**刻意不含**任何 ``combined`` / ``pooled`` / ``total`` 字段。

样本量与 MDE
------------
一律复用既有实现，**不新写** ``sample_size_needed()``（项目里已有 3 个同义函数名）：

* ``rigor.min_sample_size(effect, sd)``：要检出某效应需要多少期；
* :func:`detectable_mde`：当前 n 期能检出的最小效应（**反解** ``min_sample_size``，
  不另写功效公式），与 ``rigor._min_detectable_effect`` 结果一致（有测试交叉校验）。

``mode`` 口径
-------------
``replay_results`` 主键是 ``(method, mode, issue)``（§2.1.5 冻结 DDL）：
``mode="replay"`` 是离线回放，``mode="research_arm"`` 是 LLM 研究对照臂
（V10-09）。同一 method 会同时以两种 mode 写入，**主键必须含 mode**，
否则 ``INSERT OR REPLACE`` 会让两者互相覆盖（S2 B4）。
"""
from __future__ import annotations

import math
import random
import time
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import numpy as np

from . import battle, db, engine as E, evaluate as EV, methods as METH, rigor

# ---------- 常量（口径冻结，供测试与前端引用） ----------

#: 回放样本的 mode 取值（§2.1.5：主键 (method, mode, issue) 必须含 mode）
MODE_REPLAY = "replay"
MODE_RESEARCH_ARM = "research_arm"
REPLAY_MODES: Tuple[str, ...] = (MODE_REPLAY, MODE_RESEARCH_ARM)

#: 两套样本的来源标记（前端/报表必须**并排**展示，禁止相加）
SOURCE_LIVE = "eval_details"
SOURCE_REPLAY = "replay_results"

#: 可回放的方法（``ml`` / ``llm`` 不可回放，理由见 :data:`NON_REPLAYABLE_METHODS`）
REPLAYABLE_METHODS: Tuple[str, ...] = tuple(battle.DEFAULT_METHODS)

#: 不可回放的方法与原因（诚实标注，绝不用统计结果冒充）
NON_REPLAYABLE_METHODS: Dict[str, str] = dict(battle.NON_REPLAYABLE_METHODS)

#: 回放默认参数
DEFAULT_N_TICKETS = 5
DEFAULT_START = 300          # 前 300 期不足以建立稳定分布（与 battle 一致）
DEFAULT_SEED = 7
DEFAULT_WINDOW = 500         # 热度阈值/约束上下文的滚动窗口期数
ALPHA = 0.05                 # 显著性口径

#: 落库列（顺序即 INSERT 顺序；§2.1.5 冻结 DDL）
COLUMNS: Tuple[str, ...] = (
    "method", "mode", "issue", "n_tickets", "red_hits",
    "best_red_hit", "blue_hit", "prize", "channels_fp", "created_at",
)

_INSERT_SQL = (
    "INSERT OR REPLACE INTO replay_results "
    "(method, mode, issue, n_tickets, red_hits, best_red_hit, blue_hit, prize, "
    " channels_fp, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)"
)


# ---------- 通道：显式解析，绝不隐式落回 production 默认 ----------

def resolve_replay_channels() -> Dict:
    """回放用的通道解析：``resolve_channels(force_production=True)``。

    ``force_production=True`` 让回放不受 research 模式（全开）影响；
    ``fingerprint`` 缺失直接报错（fail-closed），**不**用空串冒充"已显式指定"。
    """
    channels = METH.resolve_channels(force_production=True)
    if not isinstance(channels, dict) or not channels.get("fingerprint"):
        raise RuntimeError("回放通道解析失败：resolve_channels() 未返回 fingerprint")
    return channels


def _require_channels(channels: Optional[Dict]) -> Dict:
    """校验/补齐显式通道：缺 ``fingerprint`` 或 ``spec_raw`` 直接报错（fail-closed）。

    为什么必须校验 ``spec_raw``：``METH.implement_spec(None)`` 会**回落到读环境变量**，
    那正是"隐式落回 production 默认配置"的入口；回放必须显式拿着通道走。
    """
    if channels is None:
        return resolve_replay_channels()
    if not isinstance(channels, dict) or not channels.get("fingerprint"):
        raise ValueError("channels 必须来自 resolve_channels()（含 fingerprint）；"
                         "回放不接受隐式 production 默认通道")
    if "spec_raw" not in channels or not channels.get("families"):
        raise ValueError("channels 缺少 spec_raw / families 字段；回放不接受隐式默认通道")
    return channels


def channels_spec(channels: Dict) -> Dict:
    """由显式 ``channels`` 推出**生效**的开关规格（而不是读 ``config`` 的隐式状态）。"""
    return METH.effective_spec(channels.get("mode") or "production",
                               METH.implement_spec(channels.get("spec_raw")))


def channels_methods(channels: Dict) -> List[str]:
    """按显式通道规格筛出**可回放**的方法；``llm`` / ``ml`` 不在此列。"""
    spec = channels_spec(channels)
    return [m for m in REPLAYABLE_METHODS if METH.is_enabled(m, spec)]


def _normalize_mode(mode: object) -> str:
    m = str(mode or "").strip().lower()
    if m not in REPLAY_MODES:
        raise ValueError(f"未知 replay mode={mode!r}；合法取值 {REPLAY_MODES}")
    return m


# ---------- 严格 as-of 的工具 ----------

def as_of_hot_reds(history: Sequence[Dict], window: int = DEFAULT_WINDOW,
                   k: int = 6) -> List[int]:
    """只用 ``history``（调用方保证是 ``draws[:t]``）的**最近 window 期**算热度 top-k。

    这是"阈值/分位不得用全样本"的具体落实：
      * 只看 ``history`` —— 未来（``draws[t:]``）连传都不传进来；
      * 只看最近 ``window`` 期 —— 更早的历史（``history[:-window]``）不参与，
        因此不会退化成"全样本阈值"。
    """
    horizon = list(history)
    if window and window > 0:
        horizon = horizon[-int(window):]
    if not horizon:
        return []
    counts = np.zeros(rigor.R_MAX, dtype=float)
    for d in horizon:
        for r in d.get("reds") or ():
            idx = int(r) - 1
            if 0 <= idx < rigor.R_MAX:
                counts[idx] += 1.0
    order = np.argsort(-counts, kind="stable")
    return [int(i) + 1 for i in order[:max(0, int(k))]]


def _ctx_as_of(history: Sequence[Dict], window: int = DEFAULT_WINDOW) -> Dict:
    """第 t 期的约束上下文：``engine.constraint_ctx`` 只看传入切片。

    ``constraint_ctx`` 内部对和值/分区/奇偶都取 ``draws[-500:]``（= 滚动窗口），
    这里把切片**显式**截到 ``window`` 期，使"滚动窗口"这件事可被测试直接观察
    （见 ``test_as_of_threshold_only_sees_rolling_window``）。
    """
    if window and window > 0:
        return E.constraint_ctx(list(history)[-int(window):])
    return E.constraint_ctx(list(history))


# ---------- MDE：反解 rigor.min_sample_size（不新写功效函数） ----------

def detectable_mde(n_issues: int, sd: float = rigor.BASELINE_SD_HITS,
                   alpha: float = ALPHA, power: float = 0.8) -> Optional[float]:
    """当前 ``n_issues`` 期样本在给定 α/功效下能检出的**最小效应**（逐注红球命中数）。

    实现是**反解** ``rigor.min_sample_size``：``min_sample_size`` 随 ``|effect|``
    单调递减，因此求最小的 ``e`` 使 ``min_sample_size(e, sd) <= n`` 即可——
    在 log 空间二分 80 次，精度远超需求，且**没有**第二份功效公式。

    返回 ``None`` 表示该样本量下连"极大效应"都检不出（n 太小/ n<=0）。
    不变式（有测试）：``min_sample_size(mde, sd) <= n`` 且
    ``min_sample_size(mde * (1 - 1e-6), sd) > n``。
    """
    n = int(n_issues)
    if n <= 0 or sd <= 0:
        return None
    hi = float(sd) * 10.0
    if rigor.min_sample_size(hi, float(sd), alpha, power) > n:
        return None
    lo = 1e-9
    for _ in range(80):
        mid = math.sqrt(lo * hi)
        if rigor.min_sample_size(mid, float(sd), alpha, power) <= n:
            hi = mid
        else:
            lo = mid
    return float(hi)


# ---------- 逐期回放（严格 as-of 的核心循环） ----------

def _tickets_for_method(method: str, history: Sequence[Dict], n_tickets: int,
                        rng: random.Random, ctx: Dict) -> List[Dict]:
    """按方法生成 n 注，**只用** ``history``（= ``draws[:t]``）与 as-of 的 ``ctx``。

    直接复用 ``battle._tickets_for_method``：方法级出号只有**一份**实现，
    避免"回放模块自己再写一套"造成两份漂移（V6 的教训：阈值口径写两遍必错）。
    """
    return battle._tickets_for_method(method, list(history), int(n_tickets), rng, ctx)


def _issue_rows(draws: Sequence[Dict], t: int, methods: Sequence[str], *,
                n_tickets: int, seed: int, window: int, mode: str,
                channels_fp: str, created_at: float) -> Iterator[Dict]:
    """第 t 期的逐方法回放行。

    **as-of 边界只有一行**：``history = draws[:t]`` 是第 t 期唯一的输入来源，
    ``target = draws[t]`` 只用来给这一期打分。``draws[t+1:]`` 在本函数内
    没有任何读取路径——这也是 ``test_replay_ignores_future_draws`` 能
    "改未来 → 结果一字不变"的原因。
    """
    history = list(draws[:t])
    target = draws[t]
    if not history:
        return
    ctx = _ctx_as_of(history, window)
    hot = as_of_hot_reds(history, window)
    for method in methods:
        # 与 battle 同一约定：按期确定随机数 → 可复现、且与其它期解耦
        rng = random.Random(int(seed) + int(t))
        tickets = _tickets_for_method(method, history, n_tickets, rng, ctx)
        if not tickets:
            continue
        res = EV.tickets_result(tickets, target)
        n = len(tickets)
        red_sum = int(sum(res["red_hits"]))
        best = int(max(res["red_hits"])) if res["red_hits"] else 0
        blue_cnt = int(sum(res["blue_hits"]))
        prize_cash = float(res["reward"])
        yield {
            # --- 落库列（§2.1.5 冻结 DDL） ---
            "method": str(method),
            "mode": mode,
            "issue": str(target["issue"]),
            "n_tickets": n,
            "red_hits": red_sum,
            "best_red_hit": best,
            "blue_hit": blue_cnt,
            "prize": int(round(prize_cash)),
            "channels_fp": str(channels_fp),
            "created_at": float(created_at),
            # --- 仅内存诊断字段（不落库；供 as-of 反向测试与报表用） ---
            "t": int(t),
            "as_of_hot": hot,
            "as_of_window": int(window),
            "red_hits_mean": red_sum / n,
            "blue_hit_rate": blue_cnt / n,
            "prize_cash": prize_cash,
            "best_level": int(res["best_level"]),
        }


def replay_rows(draws: Sequence[Dict], methods: Optional[Sequence[str]] = None, *,
                channels: Optional[Dict] = None, mode: str = MODE_REPLAY,
                n_tickets: int = DEFAULT_N_TICKETS, start: int = DEFAULT_START,
                seed: int = DEFAULT_SEED, window: int = DEFAULT_WINDOW,
                created_at: Optional[float] = None) -> List[Dict]:
    """生成回放行（不落库）；``start`` 之前与最后一期之后都不评估。"""
    mode = _normalize_mode(mode)
    channels = _require_channels(channels)
    fp = str(channels["fingerprint"])
    methods = [m for m in (methods if methods is not None else channels_methods(channels))
               if m in REPLAYABLE_METHODS]
    draws = list(draws)
    stamp = time.time() if created_at is None else float(created_at)
    rows: List[Dict] = []
    for t in range(int(start), len(draws)):
        rows.extend(_issue_rows(draws, t, methods, n_tickets=n_tickets, seed=seed,
                                window=window, mode=mode, channels_fp=fp,
                                created_at=stamp))
    return rows


# ---------- 落库（INSERT OR REPLACE；幂等） ----------

def save_replay_rows(rows: Iterable[Dict], conn=None) -> int:
    """写入 ``replay_results``：``INSERT OR REPLACE``，主键 ``(method, mode, issue)``。

    幂等：同一 ``(method, mode, issue)`` 重跑覆盖同一行，**不产生重复行**。
    """
    conn = conn or db.get_conn()
    n = 0
    for row in rows:
        conn.execute(_INSERT_SQL, tuple(row[c] for c in COLUMNS))
        n += 1
    conn.commit()
    return n


def load_replay_rows(mode: Optional[str] = None, method: Optional[str] = None,
                     limit_issues: Optional[int] = None) -> List[Dict]:
    """读回放行（可只取某一个 mode / 某一个方法）。"""
    sql = "SELECT * FROM replay_results"
    where: List[str] = []
    args: List = []
    if mode is not None:
        where.append("mode=?")
        args.append(_normalize_mode(mode))
    if method is not None:
        where.append("method=?")
        args.append(str(method))
    if limit_issues:
        where.append("issue IN (SELECT DISTINCT issue FROM replay_results "
                     "ORDER BY CAST(issue AS INTEGER) DESC LIMIT ?)")
        args.append(int(limit_issues))
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY method, mode, CAST(issue AS INTEGER), issue"
    return [dict(r) for r in db.get_conn().execute(sql, args).fetchall()]


# ---------- 汇总与显著性 ----------

def _aggregate_rows(rows: Sequence[Dict], *, method: str, source: str,
                    kind: str = "replay") -> Dict:
    """把一个方法（或一个来源）的行汇总成一块**自洽**的指标（不含跨来源字段）。"""
    if not rows:
        return {
            "source": source, "kind": kind, "method": method,
            "n_issues": 0, "n_tickets": 0,
            "red_hits_mean": None, "blue_hit_rate": None,
            "best_red_hit_max": 0, "prize_total": 0.0,
            "mde": None, "significance": None,
            "note_zh": "该来源下没有样本。",
        }
    n_issues = len({str(r["issue"]) for r in rows})
    n_tickets = int(sum(int(r["n_tickets"]) for r in rows))
    red_total = float(sum(float(r["red_hits"]) for r in rows))
    blue_total = float(sum(float(r["blue_hit"]) for r in rows))
    # 逐期均值序列（而不是逐注序列）：同一期内的 n 注共用一次开奖，
    # 逐注当独立样本属于伪重复，会高估显著性。这里 n = 期数，保守。
    per_issue = [float(r["red_hits"]) / max(1, int(r["n_tickets"])) for r in rows]
    sig = rigor.strategy_significance(per_issue, rigor.BASELINE_MEAN_HITS,
                                      n_trials=1, alpha=ALPHA)
    return {
        "source": source,
        "kind": kind,
        "method": method,
        "n_issues": n_issues,
        "n_tickets": n_tickets,
        "red_hits_mean": (red_total / n_tickets) if n_tickets else None,
        "blue_hit_rate": (blue_total / n_tickets) if n_tickets else None,
        "best_red_hit_max": int(max(int(r["best_red_hit"]) for r in rows)),
        "prize_total": float(sum(float(r["prize_cash"] if "prize_cash" in r else r["prize"])
                                 for r in rows)),
        "mde": detectable_mde(n_issues),
        "significance": sig,
        "note_zh": ("样本量 n=%d 期（%d 注）；当前样本量下可检出的最小效应约 ±%.4f 红球/注。"
                    % (n_issues, n_tickets, detectable_mde(n_issues) or float("nan"))),
    }


def _group_by_method(rows: Sequence[Dict]) -> Dict[str, List[Dict]]:
    out: Dict[str, List[Dict]] = {}
    for r in rows:
        out.setdefault(str(r.get("method") or "unknown"), []).append(r)
    return out


def offline_replay(draws: Optional[Sequence[Dict]] = None,
                   methods: Optional[Sequence[str]] = None,
                   channels: Optional[Dict] = None,
                   mode: str = MODE_REPLAY,
                   n_tickets: int = DEFAULT_N_TICKETS,
                   start: int = DEFAULT_START,
                   seed: int = DEFAULT_SEED,
                   window: int = DEFAULT_WINDOW,
                   persist: bool = True) -> Dict:
    """离线回放主入口（V10-05）。

    参数
    ----
    draws:    升序历史（``db.load_draws()`` 结构）；None → 从库里读。
    methods:  要回放的方法；None → 由**显式通道**（``channels_methods``）决定。
              传 ``ml`` / ``llm`` 时进 ``skipped``（不可回放，不用统计结果冒充）。
    channels: 显式通道 dict；None → ``resolve_channels(force_production=True)``。
              **不接受**"隐式 production 默认"：``channels_fp`` 必须来自显式解析。
    mode:     ``replay``（默认）或 ``research_arm``；落库主键含 mode，互不覆盖。
    persist:  是否写 ``replay_results``（默认 True；False 时纯内存，供测试/预览）。

    返回
    ----
    ``channels`` / ``channels_fp`` / ``mode`` / ``methods`` / ``skipped`` /
    ``n_issues`` / ``per_method``（逐方法汇总，含样本量与 MDE、
    显著性判决）/ ``rows``（逐期行，含 as-of 诊断字段）/
    ``sample_note_zh``（与 ``eval_details`` 不得相加的说明）。
    """
    mode = _normalize_mode(mode)
    draws = list(draws) if draws is not None else db.load_draws()
    channels = _require_channels(channels)
    fp = str(channels["fingerprint"])

    requested = list(methods) if methods is not None else channels_methods(channels)
    skipped: Dict[str, str] = {}
    methods_eff: List[str] = []
    for m in requested:
        key = str(m).strip().lower()
        if key in REPLAYABLE_METHODS:
            if key not in methods_eff:
                methods_eff.append(key)
        else:
            skipped[str(m)] = NON_REPLAYABLE_METHODS.get(
                key, f"未知/不可回放方法 {m!r}；可回放集合 = {REPLAYABLE_METHODS}")

    n_issues = max(0, len(draws) - int(start))
    if methods_eff and n_issues > 0:
        rows = replay_rows(draws, methods_eff, channels=channels, mode=mode,
                           n_tickets=n_tickets, start=start, seed=seed,
                           window=window)
    else:
        rows = []
    saved = save_replay_rows(rows) if (persist and rows) else 0
    grouped = _group_by_method(rows)
    per_method = {m: _aggregate_rows(grouped.get(m, []), method=m,
                                     source=SOURCE_REPLAY)
                  for m in methods_eff}
    return {
        "mode": mode,
        "channels": channels,
        "channels_fp": fp,
        "methods": methods_eff,
        "skipped": skipped,
        "start": int(start),
        "window": int(window),
        "n_tickets_per_issue": int(n_tickets),
        "seed": int(seed),
        "n_issues": n_issues,
        "n_rows": len(rows),
        "n_written": int(saved),
        "per_method": per_method,
        "rows": rows,
        "as_of_zh": (f"严格 as-of：第 t 期只用 draws[:t]，从第 {int(start)} 期起逐期评估"
                     f"（共 {n_issues} 期）；热度阈值与约束上下文只用最近 "
                     f"{int(window)} 期滚动窗口。"),
        "sample_note_zh": (
            "本结果是**离线回放样本**（replay_results），不是真实交战样本"
            "（eval_details）。两者样本量、口径、独立性都不同，**禁止相加**或合并求均值；"
            "请用 samples_side_by_side() 并排展示。"),
    }


# ---------- 两套样本：物理隔离 + 并排展示（禁止相加） ----------

def live_sample() -> Dict:
    """真实交战样本（``eval_details``，逐注）聚合。"""
    rows = db.load_eval_details()
    if not rows:
        return {**_aggregate_rows([], method="*", source=SOURCE_LIVE, kind="live"),
                "source_zh": "真实交战样本（eval_details）",
                "n_issues": 0}
    # eval_details 是逐注行；这里把它当成"组合口径"的一块，不做任何跨来源混合
    n_issues = len({str(r["issue"]) for r in rows})
    n_tickets = len(rows)
    red_mean = float(np.mean([float(r["red_hits"]) for r in rows]))
    blue_rate = float(np.mean([float(r["blue_hit"]) for r in rows]))
    per_issue: Dict[str, List[float]] = {}
    for r in rows:
        per_issue.setdefault(str(r["issue"]), []).append(float(r["red_hits"]))
    series = [float(np.mean(v)) for _, v in sorted(per_issue.items())]
    return {
        "source": SOURCE_LIVE,
        "source_zh": "真实交战样本（eval_details）",
        "kind": "live",
        "method": "*",
        "n_issues": n_issues,
        "n_tickets": n_tickets,
        "red_hits_mean": red_mean,
        "blue_hit_rate": blue_rate,
        "best_red_hit_max": int(max(int(r["red_hits"]) for r in rows)),
        "prize_total": float(sum(float(r["reward"] or 0.0) for r in rows)),
        "mde": detectable_mde(n_issues),
        "significance": rigor.strategy_significance(series, rigor.BASELINE_MEAN_HITS,
                                                    n_trials=1, alpha=ALPHA) if series else None,
        "note_zh": ("真实交战样本：期数极少（MDE 极大），任何方法差异都测不出来；"
                    "它**不能**与离线回放样本相加。"),
    }


def replay_sample(mode: str = MODE_REPLAY) -> Dict:
    """离线回放样本（``replay_results``，按期）聚合。"""
    mode = _normalize_mode(mode)
    rows = load_replay_rows(mode=mode)
    block = _aggregate_rows(rows, method="*", source=SOURCE_REPLAY, kind="replay")
    block["source_zh"] = (f"离线回放样本（replay_results, mode={mode}）")
    block["modes_zh"] = ("replay=mode 为离线回放；research_arm=LLM 研究对照臂。"
                         "两种 mode 分主键存放，互不覆盖。")
    return block


def samples_side_by_side(mode: str = MODE_REPLAY) -> Dict:
    """把**真实交战样本**与**离线回放样本**并排放出（各自样本量 + 各自 MDE）。

    返回体**刻意不含** ``combined`` / ``pooled`` / ``total`` 等跨来源字段：
    调用方无法"顺手相加"。两块的 ``source`` 字段也保证前端能标注来源。
    """
    live = live_sample()
    replay = replay_sample(mode=mode)
    return {
        "live": live,
        "replay": replay,
        "mixable": False,
        "note_zh": ("两套样本物理分表（eval_details / replay_results），"
                    "来源、口径、样本量都不同：真实交战是极少样本的真实预测记录，"
                    "离线回放是 walk-forward 模拟。**禁止相加或合并求均值**；"
                    "只允许并排对照，并各自标注样本量与可检出 MDE。"),
    }


# 方案 §3.6 的接口名是 ``run_replay(draws, methods, channels=None, mode="replay")``，
# 本文件按任务书用 ``offline_replay`` 作主名（语义更准：这是**离线**回放）。
# 两者是**同一个函数**，不另写实现，避免出现两份口径漂移。
run_replay = offline_replay
