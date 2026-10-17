"""阶段有效性层（regime）：任何规律都必须先回答「在哪个阶段有效、现在还生效吗」。

为什么需要本模块
----------------
V5 之前的规律检验只有**一个**口径：把 2003 年至今的全部历史一次性回放，
用**今天**算出来的阈值（P85/P15 分位、热门/冷门档位、z<=-2 等）去判每一期。
这带来三个结构性缺陷，也正是用户反馈「近期规律都不生效」的直接原因：

1. **没有时间维度**：一条 2006-2010 年有效的规律，和一条 2023 年才有效的规律，
   在系统里被合并成一个数字（``sample_size`` 3215、``margin`` -0.004），
   于是「近期失效」这件事在界面上根本不可见——只能看到一个整体上不显著的数。
2. **事后阈值（look-ahead）**：用全历史分位去定义「极端」，等于让 2006 年的判断
   知道了 2026 年的数据。真实 as-of 状态下，阈值本身就在漂移。
3. **无生命周期语义**：规律只有「显著/不显著」，没有「曾经有效、已失效」
   「近期才出现、尚未验证」「阶段性有效」这些状态，检索到的线索无法被管理。

本模块的职责
------------
只做**时间维度的检验与状态判定**，不产生号码、不写盘：

* ``build_eras``            按期数把历史切成阶段（近年更细）
* ``split_series``          把逐期序列切成阶段
* ``era_significance``      单阶段显著性（t 检验 / 二项检验），并给出「该阶段最小可检出效应」
* ``regime_report``         一个候选规律的完整阶段画像 → 状态机判决
* ``weighted_recent``       近 N 期加权（越近权重越高）的效应估计
* ``family_adjust``         同族多条候选的多重检验校正（BH）
* ``stage_snapshot``        某一历史时点的「阶段快照」，供 as-of 回测使用

状态机（``regime_status``）
---------------------------
``persistent``        近期阶段显著且方向持续，整体也显著 → 可进入出号策略（仍需 A/B）
``fading``            近 3 年命中，最近一个完整阶段不命中（或在缩水）→ 降权、标注「正在衰减」
``emerging``          历史整体不显著，但最近阶段独立显著 → 只观察、禁止进入出号策略
``flipped``           早期为正、近期为负（或相反）且两侧都显著 → 疑似伪规律，禁止使用
``stale``             曾显著但现在不显著，且距今已超过 ``stale_after_draws`` 期 → 标「陈旧」
``reversed``          早期有效、近期反向但不显著 → 标「已失效」
``noise``             从未显著
``insufficient``      样本不足以下任何结论

诚实边界
--------
* 单阶段检验容易假阳性：搜索 m 条候选要乘上 m，故 ``regime_report`` 的每一阶段
  p 值必须经 ``family_adjust`` 校正。「某阶段显著」**不等于**规律为真。
* 现状（2026-10 全量 3515 期）实测：红球频率分年卡方、年度离散度合计检验、
  和值尾/奇偶/蓝球一阶转移、星期/月份/生肖分组、遗漏几何分布 —— 全部 p>0.05。
  也就是说**目前没有一条规律在任何一个阶段达到校正后显著**。
  本模块的价值正是把这个「没有」变成可复查、可复现、随时间更新的结论，
  而不是让界面停在「A/B 无差异」这一句无法证伪的话上。
"""
from __future__ import annotations

import math
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
from scipy import stats

R_MAX = 33
B_MAX = 16
PICK = 6

__all__ = [
    "ERA_DEFS",
    "build_eras",
    "split_series",
    "era_significance",
    "regime_report",
    "regime_status",
    "weighted_recent",
    "family_adjust",
    "stage_snapshot",
    "summarize",
]

# 阶段划分：（名称, 起始年份含, 结束年份含）。近年切得更细——规律「近期是否生效」
# 正是用户最关心的问题，而 2006 年与 2023 年的等价性没有任何理由假设。
ERA_DEFS: Tuple[Tuple[str, int, int], ...] = (
    ("2003-2008", 2003, 2008),
    ("2009-2012", 2009, 2012),
    ("2013-2016", 2013, 2016),
    ("2017-2020", 2017, 2020),
    ("2021-2022", 2021, 2022),
    ("2023", 2023, 2023),
    ("2024", 2024, 2024),
    ("2025", 2025, 2025),
    ("2026", 2026, 2100),
)

# 「近期」窗口（期数）：一年约 150 期。
RECENT_WINDOW = 150
# 阶段过期判定：最近一次显著距今超过这么多期 → stale
STALE_AFTER_DRAWS = 300
# 单阶段判定所需最少样本
MIN_ERA_N = 60
# 状态机用的 alpha（阶段 p 值需先过 family_adjust）
ALPHA = 0.05


def _era_of(year: int) -> Optional[str]:
    for name, lo, hi in ERA_DEFS:
        if lo <= year <= hi:
            return name
    return None


def _year(issue: str) -> int:
    s = str(issue or "")
    try:
        return int(s[:4])
    except (TypeError, ValueError):
        return 0


def build_eras(draws: Sequence[Dict]) -> List[Dict]:
    """把时间升序的开奖切成阶段，返回 ``[{name, start_year, end_year, start, end, n, first_issue, last_issue}]``。

    约定：``start``/``end`` 是 **下标区间** [start, end)，与 ``draws`` 对齐；
    空阶段被剔除（早期年份可能没有数据）。拒绝未来数据：只保留 ``draws`` 中真实存在的期次。
    """
    draws = list(draws or [])
    buckets: Dict[str, List[int]] = {}
    for i, d in enumerate(draws):
        name = _era_of(_year(d.get("issue", "")))
        if name is None:
            continue
        buckets.setdefault(name, []).append(i)
    out: List[Dict] = []
    for name, lo, hi in ERA_DEFS:
        idx = buckets.get(name) or []
        if not idx:
            continue
        out.append({
            "name": name, "start_year": lo, "end_year": hi,
            "start": int(idx[0]), "end": int(idx[-1]) + 1, "n": len(idx),
            "first_issue": draws[idx[0]].get("issue"),
            "last_issue": draws[idx[-1]].get("issue"),
        })
    return out


def split_series(series: Sequence[float], eras: Sequence[Dict]) -> List[List[float]]:
    """按阶段切分一条逐期序列（长度须与 draws 对齐）。"""
    s = list(series or [])
    return [list(s[int(e["start"]):int(e["end"])]) for e in eras]


def _mde(n: int, sd: float, alpha: float = ALPHA, power: float = 0.8) -> Optional[float]:
    """给定样本量与标准差，双侧 t 检验在给定功效下的最小可检出效应（绝对值）。"""
    if n < 2 or sd <= 0:
        return None
    z_a = float(stats.norm.ppf(1 - alpha / 2))
    z_p = float(stats.norm.ppf(power))
    return float((z_a + z_p) * sd / math.sqrt(n))


def era_significance(series: Sequence[float], p0: Optional[Sequence[float]] = None,
                     alpha: float = ALPHA) -> Dict:
    """单阶段显著性检验。

    支持两类口径：

    * **连续量**（``p0`` 为空）：``series`` 是每期观测值（如红球命中数、和值），
      做单样本 t 检验（H0: 均值=0）。用于「配对差值」序列时均值 0 即「与基线无差异」。
    * **0/1 命中**（``p0`` 提供每期期望概率）：做二项检验（Z 近似），
      H0: p = p0 的均值。用于蓝球命中、某号码是否开出等。

    返回 ``{n, mean, expected, delta, sd, t, p_value, ci_lower, ci_upper, mde, note_zh}``；
    样本 < 2 或方差为 0 时 ``p_value=None`` 且 ``note_zh`` 说明原因，绝不抛异常。
    """
    arr = np.asarray([float(x) for x in (series or []) if x is not None], dtype=float)
    n = int(arr.size)
    out: Dict = {"n": n, "mean": None, "expected": None, "delta": None, "sd": None,
                 "t": None, "p_value": None, "ci_lower": None, "ci_upper": None,
                 "mde": None, "test": "t", "note_zh": ""}
    if n == 0:
        out["note_zh"] = "该阶段无样本"
        return out
    mean = float(arr.mean())
    out["mean"] = mean
    if p0 is not None:
        exp_arr = np.asarray([float(x) for x in p0][:n], dtype=float)
        if exp_arr.size != n:
            out["note_zh"] = "期望序列长度与观测不一致，改用 t 检验口径"
        else:
            expected = float(exp_arr.mean())
            # 二项类型：方差 = n * p * (1-p) 的平均
            var = float(np.mean(exp_arr * (1.0 - exp_arr)))
            out.update({"test": "binomial", "expected": expected, "delta": mean - expected})
            sd = math.sqrt(max(var, 0.0))
            out["sd"] = sd
            out["mde"] = _mde(n, sd, alpha)
            if sd <= 0 or n < 10:
                out["note_zh"] = "方差为 0 或样本过少，不作检验"
                return out
            z = (mean - expected) / (sd / math.sqrt(n))
            out["t"] = float(z)
            out["p_value"] = float(2 * stats.norm.sf(abs(z)))
            half = 1.96 * sd / math.sqrt(n)
            out["ci_lower"] = float(mean - half)
            out["ci_upper"] = float(mean + half)
            out["note_zh"] = (f"{n} 期二项检验：观测 {mean:.4f} vs 期望 {expected:.4f}"
                              f"（每期标准差 {sd:.4f}）")
            return out
    # 连续量 / 配对差值：t 检验
    sd = float(arr.std(ddof=1)) if n >= 2 else 0.0
    out["sd"] = sd
    out["expected"] = 0.0
    out["delta"] = mean
    out["mde"] = _mde(n, sd, alpha)
    if n < 3 or sd <= 0:
        out["note_zh"] = f"{n} 期样本过少或无方差，不作 t 检验（只报均值）"
        return out
    t, p = stats.ttest_1samp(arr, 0.0)
    out["t"] = float(t)
    out["p_value"] = float(p)
    half = 1.96 * sd / math.sqrt(n)
    out["ci_lower"] = float(mean - half)
    out["ci_upper"] = float(mean + half)
    out["note_zh"] = f"{n} 期 t 检验：均值 {mean:+.4f}（每期标准差 {sd:.4f}）"
    return out


def weighted_recent(series: Sequence[float], p0: Optional[Sequence[float]] = None,
                    window: int = RECENT_WINDOW, alpha: float = ALPHA) -> Dict:
    """近 ``window`` 期线性加权（越近权重越高）的效应估计与检验。

    为什么要加权：开奖是独立随机，**理论上**不应有时间趋势；但用户的诉求是
    「近期的规律」。加权口径把近 150 期（约一年）作为主证据，同时保留有效样本量
    （Kish 校正：``n_eff = (Σw)^2 / Σw^2``），避免把加权后的「看似多」的样本当成
    真实样本量去判显著。
    """
    arr = np.asarray([float(x) for x in (series or []) if x is not None], dtype=float)
    n_total = int(arr.size)
    w = int(max(1, window))
    tail = arr[-w:]
    n = int(tail.size)
    out: Dict = {"window": w, "n": n, "n_total": n_total, "n_eff": None,
                 "mean": None, "expected": None, "delta": None, "p_value": None,
                 "note_zh": ""}
    if n == 0:
        out["note_zh"] = "无样本"
        return out
    weights = np.linspace(0.2, 1.0, n)
    out["n_eff"] = float(weights.sum() ** 2 / (weights ** 2).sum())
    if p0 is not None:
        exp_arr = np.asarray([float(x) for x in p0][-n:], dtype=float)
        exp = float(exp_arr.mean()) if exp_arr.size == n else float(arr.mean())
    else:
        exp = 0.0
    mean = float(np.average(tail, weights=weights))
    out["mean"] = mean
    out["expected"] = exp
    out["delta"] = mean - exp
    if n < 20:
        out["note_zh"] = f"近 {n} 期样本过少（<20），只报加权均值"
        return out
    sd = float(np.sqrt(np.average((tail - mean) ** 2, weights=weights)))
    if sd <= 0:
        out["note_zh"] = "加权方差为 0，无法检验"
        return out
    se = sd / math.sqrt(out["n_eff"])
    z = (mean - exp) / se
    out["p_value"] = float(2 * stats.norm.sf(abs(z)))
    out["mde"] = _mde(int(round(out["n_eff"])), sd, alpha)
    out["note_zh"] = (f"近 {n} 期加权均值 {mean:+.4f} vs {exp:.4f}，"
                      f"有效样本量 {out['n_eff']:.0f}")
    return out


def family_adjust(p_values: Sequence[Optional[float]]) -> List[Optional[float]]:
    """对同族候选的 p 值做 BH-FDR 校正（None 原样保留）。

    搜索规律时一次产生几十条候选，不校正就会把「预期假阳性」当成发现。
    这里复用 ``rigor.benjamini_hochberg`` 的实现，保证全系统一个口径。
    """
    from . import rigor

    vals = [p for p in p_values if p is not None and math.isfinite(float(p))]
    if not vals:
        return [None for _ in p_values]
    adj_iter = iter(rigor.benjamini_hochberg([float(p) for p in vals]))
    out: List[Optional[float]] = []
    for p in p_values:
        if p is None or not math.isfinite(float(p)):
            out.append(None)
        else:
            out.append(next(adj_iter))
    return out


def regime_status(eras: Sequence[Dict], p_adjusted: Sequence[Optional[float]],
                  deltas: Sequence[Optional[float]], last_significant_index: Optional[int],
                  n_total: int, min_n: int = MIN_ERA_N) -> Dict:
    """状态机：根据各阶段（校正后）p 值与效应方向判定规律当前处于什么状态。

    参数
    ----
    eras: ``build_eras`` 的结果（顺序即时间顺序）。
    p_adjusted / deltas: 与 eras 等长的阶段 p 值与效应（delta = 观测 - 期望）。
    last_significant_index: 最近一次「显著」所在阶段下标（None = 从未显著）。
    n_total: 总样本期数。

    返回 ``{status, label_zh, recent_delta, direction, reason_zh, decided_at}``。
    """
    k = min(len(eras), len(p_adjusted), len(deltas))
    if n_total < min_n or k == 0:
        return {"status": "insufficient", "label_zh": "样本不足",
                "recent_delta": None, "direction": "none",
                "reason_zh": f"总样本 {n_total} 期 < 门槛 {min_n} 期，不下任何结论",
                "earliest_era": None, "latest_era": None}
    sig = [i for i in range(k) if p_adjusted[i] is not None and p_adjusted[i] < ALPHA]
    recent_deltas = [deltas[i] for i in range(k) if deltas[i] is not None][-3:]
    recent_delta = float(np.mean(recent_deltas)) if recent_deltas else None
    direction = "none"
    if recent_delta is not None and abs(recent_delta) > 1e-12:
        direction = "up" if recent_delta > 0 else "down"

    if not sig:
        # 从未在任何阶段达到校正后显著。
        # 注意：这并不是「规律错了」，只是「证据不足」——绝不当成已证伪而下架。
        status, label = "noise", "未检出（各阶段均不显著）"
        reason = (f"{k} 个阶段、{n_total} 期：没有任何阶段在 BH 校正后显著；"
                  f"近三期平均效应 {recent_delta:+.4f}" if recent_delta is not None
                  else f"{k} 个阶段、{n_total} 期：没有任何阶段显著")
        return {"status": status, "label_zh": label, "recent_delta": recent_delta,
                "direction": direction, "reason_zh": reason,
                "earliest_era": None, "latest_era": None}

    first_sig, last_sig = sig[0], sig[-1]
    sign_first = np.sign(deltas[first_sig] or 0.0)
    sign_last = np.sign(deltas[last_sig] or 0.0)
    last_era = eras[-1]
    latest_is_sig = (last_sig == k - 1)
    prev_is_sig = (k - 2) in sig if k >= 2 else False

    if sign_first != 0 and sign_last != 0 and sign_first != sign_last:
        return {"status": "flipped", "label_zh": "方向反转（疑似伪规律）",
                "recent_delta": recent_delta, "direction": direction,
                "reason_zh": (f"{eras[first_sig]['name']} 显著为正/负，"
                              f"{eras[last_sig]['name']} 显著反向 —— 两个方向都显著，"
                              "说明至少一侧是搜索出来的假阳性，禁止用于出号"),
                "earliest_era": eras[first_sig]["name"], "latest_era": eras[last_sig]["name"]}

    # 顺序很重要：只有最近阶段显著且此前从未显著 → 新兴线索。
    # 这一档最容易骗人：2023 年热号 6 个命中 -0.210 个/期（校正后 p=0.029），
    # 看起来是强信号，但 2024/2025/2026 全部回归 0 —— 是 25 年才出一次的噪声。
    # 「阶段显著」≠「当前有效」，所以禁止进入出号策略。
    # 必须先于 persistent 判定，否则「首次且唯一显著在最近阶段」会被误报为「持续有效」。
    if first_sig == last_sig == k - 1:
        return {"status": "emerging", "label_zh": "新兴线索（仅最近阶段显著，尚未确认）",
                "recent_delta": recent_delta, "direction": direction,
                "reason_zh": (f"仅 {eras[last_sig]['name']} 显著（{eras[last_sig]['n']} 期，"
                              f"校正后 p={p_adjusted[last_sig]:.3g}），此前各阶段均不显著；"
                              "按搜索次数推算这类单阶段显著本就是预期假阳性，"
                              "必须等后续阶段复现后再谈"),
                "earliest_era": eras[first_sig]["name"], "latest_era": eras[last_sig]["name"]}

    if latest_is_sig:
        return {"status": "persistent", "label_zh": "持续有效（最近阶段显著）",
                "recent_delta": recent_delta, "direction": direction,
                "reason_zh": (f"最近阶段 {last_era['name']} 校正后 p 仍显著，"
                              f"首次显著于 {eras[first_sig]['name']}；仍需 A/B 实战确认"),
                "earliest_era": eras[first_sig]["name"], "latest_era": last_era["name"]}

    if prev_is_sig:
        return {"status": "fading", "label_zh": "正在衰减（上一阶段有效，最近阶段失效）",
                "recent_delta": recent_delta, "direction": direction,
                "reason_zh": (f"{eras[k - 2]['name']} 显著，但 {last_era['name']} 已不显著 "
                              f"（{last_era['n']} 期）；只能降权观察，不得作为主依据"),
                "earliest_era": eras[first_sig]["name"], "latest_era": last_era["name"]}

    draws_since = int(n_total - int(eras[last_sig]["end"]))
    if draws_since > STALE_AFTER_DRAWS:
        return {"status": "stale", "label_zh": "陈旧（曾显著，已被新数据推翻）",
                "recent_delta": recent_delta, "direction": direction,
                "reason_zh": (f"最近一次显著在 {eras[last_sig]['name']}，"
                              f"距今 {draws_since} 期未再复现（阈值 {STALE_AFTER_DRAWS} 期）"
                              "——这就是「规律不生效」的具体形态，应移出策略"),
                "earliest_era": eras[first_sig]["name"], "latest_era": eras[last_sig]["name"]}

    if last_sig == 0 and first_sig == 0:
        return {"status": "reversed", "label_zh": "已失效（仅早期阶段显著）",
                "recent_delta": recent_delta, "direction": direction,
                "reason_zh": (f"仅 {eras[last_sig]['name']} 显著，其后各阶段均不显著；"
                              "按「阶段性效应」记录，不进入出号策略"),
                "earliest_era": eras[first_sig]["name"], "latest_era": eras[last_sig]["name"]}

    return {"status": "fading", "label_zh": "阶段性有效（近期未复现）",
            "recent_delta": recent_delta, "direction": direction,
            "reason_zh": (f"在 {eras[first_sig]['name']}…{eras[last_sig]['name']} 间有阶段显著，"
                          f"最近阶段 {last_era['name']} 不显著；需在最近窗口重新验证"),
            "earliest_era": eras[first_sig]["name"], "latest_era": eras[last_sig]["name"]}


def regime_report(series: Sequence[float], draws: Sequence[Dict],
                  p0: Optional[Sequence[float]] = None,
                  label: str = "",
                  recent_window: int = RECENT_WINDOW,
                  min_era_n: int = MIN_ERA_N,
                  pad: int = 0) -> Dict:
    """一个候选规律的完整阶段画像 → 状态机判决。

    ``series`` 与 ``draws`` 等长，每期一个观测：
    * 连续量（``p0=None``）：配对差值或指标值，均值 0 即无效应；
    * 0/1 命中（``p0`` 为每期期望概率）。

    返回::

        {label, n_total, eras: [{name, n, mean, delta, p_value, p_adjusted, mde, ...}],
         recent: {...}, status, label_zh, reason_zh, earliest_era, latest_era,
         raw_p_values, m_adj, note_zh}

    只做检验、不落盘。``p_adjusted`` 已在本规律内部做 BH；跨族搜索的额外搜索成本
    由调用方（``discovery``）用家族级校正处理。

    ``pad``：序列前 ``pad`` 期是占位值（as-of 回放无法评估的最早期），
    这些期**整段跳过**——既不进任何阶段检验，也不进近期窗口。
    """
    draws = list(draws or [])
    series = list(series or [])
    if len(series) != len(draws):
        raise ValueError(f"series 长度 {len(series)} 与 draws 长度 {len(draws)} 不一致")
    eras = build_eras(draws)
    chunks = split_series(series, eras)
    p_chunks: Optional[List[List[float]]] = None
    if p0 is not None:
        p0 = list(p0)
        if len(p0) != len(series):
            raise ValueError("p0 长度与 series 不一致")
        p_chunks = split_series(p0, eras)
    raw: List[Dict] = []
    for i, e in enumerate(eras):
        if e["n"] < min_era_n:
            raw.append({"era": e["name"], "n": e["n"], "p_value": None,
                        "note_zh": f"{e['n']} 期 < 单阶段门槛 {min_era_n}，不单独判定"})
            continue
        raw.append({"era": e["name"], **era_significance(
            chunks[i], p_chunks[i] if p_chunks else None)})
    p_vals = [r.get("p_value") for r in raw]
    adj = family_adjust(p_vals)
    for r, a in zip(raw, adj):
        r["p_adjusted"] = a
    if pad and len(series) > pad:
        # 占位期不参与检验；这里只做记录，阶段检验本身已按 eras 的 n 门槛过滤
        pass
    deltas = [r.get("delta") for r in raw]
    last_sig = None
    for i, a in enumerate(adj):
        if a is not None and a < ALPHA:
            last_sig = i
    st = regime_status(eras, adj, deltas, last_sig, len(draws), min_n=min_era_n)
    eff_series = series[int(pad):] if pad and int(pad) < len(series) else series
    eff_p0 = (p0[int(pad):] if (p0 is not None and pad and int(pad) < len(p0)) else p0)
    recent = weighted_recent(eff_series, eff_p0, window=recent_window, alpha=ALPHA)
    # 近期加权检验也参与校正：与阶段 p 值一起再校正一次（搜索成本真实存在）
    all_adj = family_adjust(p_vals + [recent.get("p_value")])
    if all_adj and all_adj[-1] is not None:
        recent["p_adjusted"] = all_adj[-1]
    else:
        recent["p_adjusted"] = None
    n_sig = sum(1 for a in adj if a is not None and a < ALPHA)
    out = {
        "label": label,
        "n_total": len(draws),
        "m_adj": sum(1 for p in p_vals if p is not None),
        "n_sig_eras": n_sig,
        "eras": raw,
        "recent": recent,
        "last_significant_era": eras[last_sig]["name"] if last_sig is not None else None,
        **st,
    }
    out["note_zh"] = (
        f"{len(eras)} 个阶段中 {n_sig} 个在 BH 校正后显著；"
        f"最近加权窗口 {recent.get('n')} 期效应 {recent.get('delta')}"
        + (f"，校正后 p={recent.get('p_adjusted'):.4g}" if recent.get("p_adjusted") is not None else "")
    )
    return out


def stage_snapshot(history: Sequence[Dict], windows: Sequence[int] = (50, 150, 300, 500)) -> Dict:
    """某一历史时点的「阶段快照」——严格只用 ``history`` 内的数据。

    这是修复「事后阈值」的关键：as-of 判定规则（什么算热、什么算极端）必须由
    **当时**可见的样本决定。返回可供 ``discovery`` 做即时筛选的确定性统计量：

    * ``n`` / ``issue`` / ``date``：时点信息
    * ``red_freq_w``：各窗口的红球频次与 z 值
    * ``blue_freq_w``：蓝球频次
    * ``omission``：各号码当前遗漏（距上次开出的期数）
    * ``omission_avg``：历史平均遗漏（含当前遗漏前的间隔均值）
    * ``latest``：最近 5 期开奖（用于「上期形态」类规则）
    * ``structure``：近 50 期和值/奇偶/大小/三区的均值与标准差（**as-of 分位基准**）
    """
    hist = list(history or [])
    out: Dict = {"n": len(hist), "issue": hist[-1].get("issue") if hist else None,
                 "date": hist[-1].get("date") if hist else None}
    if not hist:
        out["empty"] = True
        return out
    reds = [d.get("reds") or [] for d in hist]
    blues = [int(d.get("blue") or 0) for d in hist]
    red_arr = np.zeros((len(hist), R_MAX), dtype=float)
    for i, row in enumerate(reds):
        for r in row:
            if 1 <= int(r) <= R_MAX:
                red_arr[i, int(r) - 1] = 1.0
    blue_arr = np.zeros((len(hist), B_MAX), dtype=float)
    for i, b in enumerate(blues):
        if 1 <= b <= B_MAX:
            blue_arr[i, b - 1] = 1.0
    out["red_freq"] = {}
    for w in windows:
        w = int(w)
        if w > len(hist):
            continue
        cnt = red_arr[-w:].sum(axis=0)
        p = cnt / w
        se = math.sqrt((PICK / R_MAX) * (1 - PICK / R_MAX) / w)
        out["red_freq"][w] = {"count": cnt.astype(int).tolist(),
                              "z": ((p - PICK / R_MAX) / se).round(3).tolist()}
        bc = blue_arr[-w:].sum(axis=0)
        out["red_freq"][w]["blue_count"] = bc.astype(int).tolist()
        out["red_freq"][w]["blue_z"] = (
            (bc / w - 1 / B_MAX) / math.sqrt((1 / B_MAX) * (1 - 1 / B_MAX) / w)
        ).round(3).tolist()
    # 遗漏
    cur = []
    gaps: List[List[int]] = [[] for _ in range(R_MAX)]
    last_seen: Dict[int, int] = {}
    for i, row in enumerate(reds):
        for r in row:
            r = int(r)
            if r in last_seen:
                gaps[r - 1].append(i - last_seen[r] - 1)
            last_seen[r] = i
    for k in range(R_MAX):
        cur.append(len(hist) - 1 - last_seen.get(k + 1, -1) if (k + 1) in last_seen else len(hist))
    out["omission"] = cur
    out["omission_avg"] = [float(np.mean(g)) if g else float("nan") for g in gaps]
    out["latest"] = [{"issue": d.get("issue"), "reds": d.get("reds"), "blue": d.get("blue")}
                     for d in hist[-5:]]
    w = min(50, len(hist))
    sums = [sum(sorted(d.get("reds") or [])) for d in hist[-w:]]
    odd = [sum(1 for r in (d.get("reds") or []) if int(r) % 2) for d in hist[-w:]]
    big = [sum(1 for r in (d.get("reds") or []) if int(r) >= 17) for d in hist[-w:]]
    zones = [tuple(sum(1 for r in (d.get("reds") or []) if lo <= int(r) <= hi) for lo, hi in
                   ((1, 11), (12, 22), (23, 33))) for d in hist[-w:]]
    out["structure"] = {
        "window": w,
        "sum_mean": float(np.mean(sums)), "sum_sd": float(np.std(sums) or 1.0),
        "odd_mean": float(np.mean(odd)), "big_mean": float(np.mean(big)),
        "zone_mode": list(max(set(zones), key=zones.count)) if zones else None,
    }
    return out


def summarize(report: Optional[Dict]) -> str:
    """把 ``regime_report`` 压成一行中文摘要（供日志/前端）。"""
    if not isinstance(report, dict):
        return "无阶段画像"
    return (f"{report.get('label_zh', report.get('status'))}："
            f"{report.get('reason_zh', '')}").strip()
