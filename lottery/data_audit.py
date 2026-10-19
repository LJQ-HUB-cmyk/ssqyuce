"""V12-14 / L3-3：数据异常期检测器（**LLM 只提名，代码来判**）。

为什么要有这个模块
------------------
历史上唯一被真实 LLM 提出、又被数据复现的发现，是「某段期号的和值均值偏低 /
奇数偏少」（P3 §4，合成零分布下 p=0.003 / 0.0065）。这说明 LLM 在
**「指出哪段数据看起来不对劲」**这件事上是有用的；但「看起来不对劲」不是结论，
它必须交给确定性代码去判。因此本模块把 L3-3 拆成两段、并严格分开：

    ① ``build_audit_prompt``  —— 把**已经算好**的确定性统计（最近窗口 vs 全历史基线）
       写成 prompt。LLM 只负责「读数字并提名」，**不让它自己算任何东西**
       （LLM 算出来的数是不可复核的，一旦让它算，后面所有判定都失去锚点）。
    ② ``parse_anomalies``     —— 解析 LLM 提名的异常项。LLM 输出格式天然不可靠，
       因此解析失败必须**安全降级**（返回空列表 + ``parse_error``），绝不抛异常。
    ③ ``judge_anomaly``       —— **确定性判定器**：对每个被指名的特征，
       用滑窗（W=300，或按 claim 的 ``range`` 指定段）算出观测值，
       用固定 seed 的置换/重采样给出 p 值与效应量，判
       ``real_defect`` / ``regime_shift`` / ``noise``。

三个判决的唯一定义（代码可判定，不含任何主观判断）
-------------------------------------------------
- ``real_defect``   = 在该段**显著**（p_adj<α）**且**方向与提名一致**且**
                     在**独立复现段**（不重叠的另一段）也显著且同向。
                     —— 两段独立数据都成立，才不是偶然。
- ``regime_shift`` = 在该段显著且同向，但**不满足两段复现**（只在单段出现）。
                     —— 可能是真的阶段变化，也可能是一次巧合；只能当「状态变化」看。
- ``noise``        = 在该段不显著，或在复现段方向相反。
                     —— 没有任何可判定证据。

项目铁律：**没有 ``scope`` 的 p 值不得展示**
---------------------------------------------
本模块每个判决里都同时给出：

- ``p_value``: **未校正**的置换 p 值（只用于解释，不得单独展示）；
- ``m_adj``:   该次判定里一共检验了多少个特征（多重检验分母）；
- ``p_adj``:   ``min(1, p_value * m_adj)``（Bonferroni）；
- ``scope``:   这次 p 值覆盖的范围（哪段数据、哪个特征集、如何校正）。

``p_value`` 与 ``p_adj`` 永远成对出现，且 ``scope`` 明写分母来源 —— 这是 A3 的要求。
若把 ``m_adj`` 偷换成 1（即假装「只检验了一个东西」），``p_adj`` 会变小，
``noise`` 会被错判成 ``regime_shift``/``real_defect``；测试里有专门的反向用例。

依赖：numpy（+ 项目内 ``rigor`` 的常量）。不写数据库、不访问网络、不真调 LLM。
"""
from __future__ import annotations

import ast
import json
import math
import os
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

__all__ = [
    "FEATURES",
    "FEATURE_ALIASES",
    "DEFAULT_WINDOW",
    "DEFAULT_W_SEG",
    "DEFAULT_N_PERM",
    "DEFAULT_SEED",
    "DEFAULT_ALPHA",
    "build_audit_prompt",
    "parse_anomalies",
    "judge_anomaly",
    "audit_draws",
    "run_audit",
    "latest_audit",
    "AUDIT_SYSTEM_PROMPT",
    "ANOMALY_SCHEMA_ZH",
]

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

#: 段窗口长度（方案建议 W=300）。判定时真实使用的段长 = min(W, 可用期数)。
DEFAULT_W_SEG = 300
#: prompt 里展示的「最近窗口」长度。
DEFAULT_WINDOW = 40
#: 置换/重采样次数（固定 seed 可复现）。
DEFAULT_N_PERM = 2000
#: 置换 seed（固定，可复现）。
DEFAULT_SEED = 20261018
#: 显著性水平。
DEFAULT_ALPHA = 0.05
#: 判定至少需要的段内期数（低于此数一律 noise，理由写进 note_zh）。
MIN_SEGMENT_N = 30

#: 支持的确定性特征（代码口径，与 LLM 的提名无关）。
FEATURES: Tuple[str, ...] = ("sum_mean", "odd_count", "zone_dist", "num_freq", "consecutive")

#: LLM 可能使用的别名 → 规范特征名（只做映射，不做模糊匹配猜测）。
FEATURE_ALIASES: Dict[str, str] = {
    "sum_mean": "sum_mean", "sum": "sum_mean", "和值": "sum_mean", "和值均值": "sum_mean",
    "red_sum": "sum_mean", "sum_range": "sum_mean",
    "odd_count": "odd_count", "odd": "odd_count", "奇数": "odd_count",
    "奇数个数": "odd_count", "奇偶": "odd_count", "parity": "odd_count",
    "zone_dist": "zone_dist", "zone": "zone_dist", "zones": "zone_dist",
    "三区": "zone_dist", "三区分布": "zone_dist", "zone_count": "zone_dist",
    "num_freq": "num_freq", "freq": "num_freq", "frequency": "num_freq",
    "单号频次": "num_freq", "单号频率": "num_freq", "red_frequency": "num_freq",
    "number_freq": "num_freq",
    "consecutive": "consecutive", "consec": "consecutive", "连号": "consecutive",
    "连号率": "consecutive",
}

#: 特征的中文说明（写进 prompt 与返回体，保证用户看到的口径与代码一致）。
FEATURE_DESC_ZH: Dict[str, str] = {
    "sum_mean": "红球和值的段内均值（理论均值 102，可用精确枚举分布算标准误）",
    "odd_count": "每期奇数红球个数的段内均值（理论值 6×17/33 = 3.0909）",
    "zone_dist": "三区（1-11 / 12-22 / 23-33）计数分布的卡方偏离（理论为超几何）",
    "num_freq": "33 个红球出现频次的均匀性卡方（df=32）",
    "consecutive": "「至少一组连号」的出现率（理论 1−C(28,6)/C(33,6) ≈ 0.65985）",
}

AUDIT_SYSTEM_PROMPT = (
    "你是数据质量核对员。你**不计算**任何数字，只做两件事：\n"
    "1) 读用户给出的、已经算好的统计（最近窗口 vs 全历史基线），指出哪段期号的"
    "哪个特征「看起来不像均匀抽样」；\n"
    "2) 按给定 JSON schema 输出你的提名。\n"
    "禁止自己算均值/方差/p 值；禁止给出预测或选号建议。"
    "你没有能力判断真伪——真伪由确定性代码判定，你的输出只是「提名」。"
)

ANOMALY_SCHEMA_ZH = (
    '输出格式（只输出 JSON 数组，不要任何解释文字）：'
    '[{"feature": "sum_mean|odd_count|zone_dist|num_freq|consecutive",'
    ' "range": {"start": 整数下标, "end": 整数下标, "len": 整数},'
    ' "direction": "above|below", "severity": "low|medium|high",'
    ' "reason_zh": "一句话说明你看到的现象"}]。'
    "下标从 0 开始，按时间升序（0 = 最早一期，-1 = 最新一期）。"
    "range 省略时视为「最近一段」。"
)


# --------------------------------------------------------------------------
# 确定性统计（build_audit_prompt 的全部数字都来自这里）
# --------------------------------------------------------------------------

def _valid_draws(draws: Optional[Sequence[Dict]]) -> List[Dict]:
    """结构合法的期次过滤（与 ``rigor._valid_draws`` 同口径，但只保留所需字段）。"""
    out: List[Dict] = []
    for d in draws or []:
        try:
            reds = sorted(int(x) for x in d["reds"])
            blue = int(d["blue"])
        except (KeyError, TypeError, ValueError):
            continue
        if len(reds) != 6 or len(set(reds)) != 6:
            continue
        if any(not 1 <= r <= 33 for r in reds) or not 1 <= blue <= 16:
            continue
        out.append({"issue": d.get("issue"), "reds": reds, "blue": blue})
    return out


def red_sums(draws: Sequence[Dict]) -> np.ndarray:
    return np.array([sum(d["reds"]) for d in draws], dtype=float)


def odd_counts(draws: Sequence[Dict]) -> np.ndarray:
    return np.array([sum(1 for r in d["reds"] if r % 2 == 1) for d in draws], dtype=float)


def consecutive_flags(draws: Sequence[Dict]) -> np.ndarray:
    return np.array([1.0 if any(b - a == 1 for a, b in zip(d["reds"], d["reds"][1:]))
                     else 0.0 for d in draws], dtype=float)


#: 号码 → 区索引（1-11→0，12-22→1，23-33→2）。显式表，不用 ``// 11`` 推导
ZONE_OF_NUMBER = np.array([min(2, (n - 1) // 11) for n in range(1, 34)], dtype=int)


def zone_matrix(draws: Sequence[Dict]) -> np.ndarray:
    """(n, 3) 三区计数矩阵：1-11 / 12-22 / 23-33。"""
    out = np.zeros((len(draws), 3), dtype=int)
    for i, d in enumerate(draws):
        for r in d["reds"]:
            out[i, ZONE_OF_NUMBER[r - 1]] += 1
    return out


def red_count_matrix(draws: Sequence[Dict]) -> np.ndarray:
    """(n, 33) 每期每个号码是否出现的 0/1 矩阵。"""
    out = np.zeros((len(draws), 33), dtype=int)
    for i, d in enumerate(draws):
        for r in d["reds"]:
            out[i, r - 1] = 1
    return out


#: 和值理论均值 6×(1+33)/2 = 102
SUM_THEORY_MEAN = 102.0
#: 和值理论标准差：6 个不重复号码之和的方差 = 6·(33²−1)/12 · (33−6)/(33−1) = 140.625
SUM_THEORY_SD = math.sqrt(6 * (33 ** 2 - 1) / 12 * (33 - 6) / (33 - 1))
#: 奇数个数理论均值 6·17/33
ODD_THEORY_MEAN = 6 * 17 / 33
#: 连号率理论值 1−C(28,6)/C(33,6)
CONSEC_THEORY_RATE = 1.0 - math.comb(28, 6) / math.comb(33, 6)
#: 三区理论均值：每区 6×11/33 = 2.0
ZONE_THEORY_MEAN = 2.0


def _mean(x: np.ndarray) -> Optional[float]:
    return float(np.mean(x)) if x.size else None


def _top_numbers(rc: np.ndarray, k: int = 5) -> List[Dict]:
    """出现次数最高/最低的号码（排序键 (count desc, number asc)，无 dict 遍历依赖）。"""
    if rc.size == 0:
        return []
    counts = rc.sum(axis=0)
    order = sorted(range(33), key=lambda i: (-int(counts[i]), i + 1))
    return [{"num": i + 1, "count": int(counts[i])} for i in order[:k]]


def build_stats(draws: Sequence[Dict], window: int = DEFAULT_WINDOW) -> Dict:
    """把「最近 window 期 vs 全历史基线」算成一份纯数字报告（**LLM 只读，不参与计算**）。"""
    n = len(draws)
    w = max(1, min(int(window), n)) if n else 0
    recent = list(draws[-w:]) if w else []
    sums_all, sums_rec = red_sums(draws), red_sums(recent)
    odd_all, odd_rec = odd_counts(draws), odd_counts(recent)
    con_all, con_rec = consecutive_flags(draws), consecutive_flags(recent)
    zone_rec = zone_matrix(recent)
    rc_all = red_count_matrix(draws)
    rc_rec = red_count_matrix(recent)
    # 三区卡方（相对均匀 2.0）：Σ (obs − exp)² / exp，exp = 3·6·w/33... 直接按每区 2.0·w
    exp_zone = ZONE_THEORY_MEAN * max(1, w)
    chi_zone_rec = float(np.sum((zone_rec.sum(axis=0) - exp_zone) ** 2 / exp_zone)) if w else None
    # 单号频次卡方（均匀）：每号期望 6w/33
    exp_num = 6.0 * max(1, w) / 33.0
    counts_rec = rc_rec.sum(axis=0).astype(float)
    chi_num_rec = float(np.sum((counts_rec - exp_num) ** 2 / exp_num)) if w else None
    return {
        "n_all": n,
        "window": w,
        "range_recent": {"start": n - w, "end": n - 1, "len": w},
        "sum": {"recent_mean": _mean(sums_rec), "all_mean": _mean(sums_all),
                "theory_mean": SUM_THEORY_MEAN,
                "theory_se": round(SUM_THEORY_SD / math.sqrt(w), 4) if w else None},
        "odd": {"recent_mean": _mean(odd_rec), "all_mean": _mean(odd_all),
                "theory_mean": round(ODD_THEORY_MEAN, 4)},
        "consecutive": {"recent_rate": _mean(con_rec), "all_rate": _mean(con_all),
                        "theory_rate": round(CONSEC_THEORY_RATE, 6)},
        "zone": {"chi2_recent": None if chi_zone_rec is None else round(chi_zone_rec, 4),
                 "df": 2, "expected_zone": ZONE_THEORY_MEAN,
                 "recent_counts": (zone_rec.sum(axis=0).tolist() if w else [])},
        "num_freq": {"chi2_recent": None if chi_num_rec is None else round(chi_num_rec, 4),
                     "df": 32,
                     "top5_recent": _top_numbers(rc_rec),
                     "top5_all": _top_numbers(rc_all)},
        "baseline_note": ("以上 all_* 为全历史基线，recent_* 为最近窗口。"
                          "所有数字均由确定性代码算出，LLM 不得自行计算。"),
    }


def build_audit_prompt(draws: Sequence[Dict], window: int = DEFAULT_WINDOW) -> str:
    """把「最近 window 期的确定性统计 vs 全历史基线」写成给 LLM 的 prompt。

    **只陈述已算好的数字**：prompt 里每一个数都来自 ``build_stats``，
    LLM 的任务是「读并提名」，不是「算并推断」。
    数据为空时也返回一个合法 prompt（明确告知「无数据，请输出空数组」）。
    """
    valid = _valid_draws(draws)
    if not valid:
        return ("数据审计任务：当前没有任何有效开奖数据。\n"
                "请按 schema 输出空数组 []（这是唯一正确的回答）。\n"
                f"{ANOMALY_SCHEMA_ZH}")
    stats = build_stats(valid, window=window)
    issues = [str(d.get("issue") or "") for d in valid]
    lines = [
        "数据审计任务：下面给你的是**已经算好**的确定性统计，请勿重新计算，也不要猜。",
        f"数据期数 = {stats['n_all']}（时间升序，下标 0..{stats['n_all'] - 1}），"
        f"最近窗口 = 最后 {stats['window']} 期（下标 {stats['range_recent']['start']}"
        f"..{stats['range_recent']['end']}）。",
        "",
        "## 特征与理论值",
    ]
    for key in FEATURES:
        lines.append(f"- {key}：{FEATURE_DESC_ZH[key]}")
    lines += [
        "",
        "## 已算好的数字",
        f"- 和值：最近窗口均值 {stats['sum']['recent_mean']}｜全历史均值 {stats['sum']['all_mean']}"
        f"｜理论均值 {stats['sum']['theory_mean']}｜理论标准误 {stats['sum']['theory_se']}",
        f"- 奇数个数：最近窗口均值 {stats['odd']['recent_mean']}｜全历史均值 "
        f"{stats['odd']['all_mean']}｜理论值 {stats['odd']['theory_mean']}",
        f"- 连号率：最近窗口 {stats['consecutive']['recent_rate']}｜全历史 "
        f"{stats['consecutive']['all_rate']}｜理论值 {stats['consecutive']['theory_rate']}",
        f"- 三区分布：最近窗口各区计数 {stats['zone']['recent_counts']}"
        f"｜相对每区 {stats['zone']['expected_zone']} 的卡方 = {stats['zone']['chi2_recent']}"
        f"（df={stats['zone']['df']}）",
        f"- 单号频次：最近窗口卡方 = {stats['num_freq']['chi2_recent']}"
        f"（df={stats['num_freq']['df']}）｜最近窗口 top5 = "
        f"{[(d['num'], d['count']) for d in stats['num_freq']['top5_recent']]}"
        f"｜全历史 top5 = {[(d['num'], d['count']) for d in stats['num_freq']['top5_all']]}",
        "",
        f"## 起始/结束期号（仅用于给你定位，不需要你计算）\n- 首期 {issues[0]}｜末期 {issues[-1]}",
        "",
        "## 你的任务",
        "如果某个特征在某段期号上「看起来不像均匀抽样」，按下面的 schema 提名。",
        "不要编造数字；没有把握就不提名（输出 []）。真伪由确定性代码判定，"
        "你的提名不会直接成为结论。",
        "",
        ANOMALY_SCHEMA_ZH,
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 解析（LLM 输出不可靠 → 必须安全降级）
# --------------------------------------------------------------------------

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def _extract_json_blob(text: str) -> Optional[Any]:
    """从自由文本里找出第一个可解析的 JSON 值（数组优先）。

    依次尝试：原文 → ``` 代码块 → 第一个 ``[`` 到最后一个 ``]`` →
    第一个 ``{`` 到最后一个 ``}``。全部失败返回 None（**不抛异常**）。
    """
    if not isinstance(text, str) or not text.strip():
        return None
    candidates: List[str] = [text.strip()]
    for m in _JSON_FENCE.finditer(text):
        candidates.append(m.group(1).strip())
    for opener, closer in (("[", "]"), ("{", "}")):
        i, j = text.find(opener), text.rfind(closer)
        if 0 <= i < j:
            candidates.append(text[i:j + 1])
    for cand in candidates:
        for parser in (json.loads, ast.literal_eval):
            try:
                obj = parser(cand)
            except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
                continue
            if isinstance(obj, (list, dict)):
                return obj
    return None


def _norm_feature(name: Any) -> Optional[str]:
    if not isinstance(name, str):
        return None
    key = name.strip().lower().replace(" ", "").replace("-", "_")
    if key in FEATURE_ALIASES:
        return FEATURE_ALIASES[key]
    raw = name.strip()
    return FEATURE_ALIASES.get(raw)


def _norm_direction(d: Any) -> str:
    s = str(d or "").strip().lower()
    if s in ("above", "high", "偏高", "高", "up", "positive", "+", "higher"):
        return "above"
    if s in ("below", "low", "偏低", "低", "down", "negative", "-", "lower"):
        return "below"
    return "any"


def _norm_severity(s: Any) -> str:
    v = str(s or "").strip().lower()
    return v if v in ("low", "medium", "high") else "medium"


def _norm_range(r: Any, n_draws: Optional[int]) -> Dict:
    """把 range 归一化成 ``{start, end, len}``（负数下标表示从末尾数）。

    无法识别时返回 ``{}``（= 由判定器自行取「最近一段」）。
    """
    if isinstance(r, str):
        m = re.findall(r"-?\d+", r)
        r = {"start": int(m[0]), "end": int(m[1])} if len(m) >= 2 else (
            {"len": int(m[0])} if m else {})
    if not isinstance(r, dict):
        return {}
    out: Dict[str, int] = {}
    for key in ("start", "end", "len"):
        val = r.get(key)
        if isinstance(val, bool):
            continue
        if isinstance(val, (int, np.integer)):
            out[key] = int(val)
        elif isinstance(val, float) and float(val).is_integer():
            out[key] = int(val)
        elif isinstance(val, str) and re.fullmatch(r"-?\d+", val.strip()):
            out[key] = int(val.strip())
    return out


def parse_anomalies(llm_text: Any) -> Tuple[List[Dict], Optional[str]]:
    """解析 LLM 返回的异常项，**永不抛异常**。

    返回 ``(items, parse_error)``：
    - ``items``: ``[{feature, range, direction, severity, reason_zh}]``，
      只保留能映射到 5 个确定性特征的项（映射不到的直接丢弃并计入
      ``parse_error`` 的说明），顺序与 LLM 输出一致；
    - ``parse_error``: ``None`` 表示完全解析成功；否则是一句中文说明
      （**降级**：调用方应把整次审计标成 ``degraded=True``）。

    接受的输入形态：JSON 数组、``{"anomalies": [...]}``、``{"items": [...]}``、
    带 ``` 代码块的文本、Python 字面量（单引号/``True``）、None。
    任何解析异常都在内部捕获 —— 这是本函数存在的全部理由。
    """
    if llm_text is None:
        return [], "LLM 未返回任何内容（可能超时或被禁用）"
    if isinstance(llm_text, (list, dict)):
        obj: Any = llm_text
    else:
        obj = _extract_json_blob(str(llm_text))
    if obj is None:
        return [], "输出中没有可解析的 JSON（已降级为空提名）"
    if isinstance(obj, dict):
        for key in ("anomalies", "items", "findings", "result"):
            if isinstance(obj.get(key), list):
                obj = obj[key]
                break
        else:
            obj = [obj] if _norm_feature(obj.get("feature")) else []
    if not isinstance(obj, list):
        return [], "JSON 顶层不是数组（已降级为空提名）"

    items: List[Dict] = []
    dropped = 0
    for raw in obj:
        if not isinstance(raw, dict):
            dropped += 1
            continue
        feat = _norm_feature(raw.get("feature"))
        if feat is None:
            dropped += 1
            continue
        items.append({
            "feature": feat,
            "range": _norm_range(raw.get("range"), None),
            "direction": _norm_direction(raw.get("direction")),
            "severity": _norm_severity(raw.get("severity")),
            "reason_zh": str(raw.get("reason_zh") or raw.get("reason") or "")[:300],
        })
    if not items:
        return [], ("LLM 输出里没有可映射到 5 个确定性特征（sum_mean / odd_count / "
                    "zone_dist / num_freq / consecutive）的提名（已降级为空提名）")
    err = None
    if dropped:
        err = f"有 {dropped} 条提名无法映射到确定性特征，已丢弃（其余 {len(items)} 条保留）"
    return items, err


# --------------------------------------------------------------------------
# 确定性判定器
# --------------------------------------------------------------------------

def _resolve_segment(spec: Dict, n: int) -> Tuple[int, int]:
    """把 ``{start, end, len}`` 解析成**左闭右开**区间 ``[lo, hi)``（支持负下标）。"""
    if n <= 0:
        return 0, 0

    def _ix(v: int) -> int:
        return v + n if v < 0 else v

    if "start" in spec and "end" in spec:
        lo, hi = _ix(spec["start"]), _ix(spec["end"]) + 1
    elif "len" in spec:
        ln = max(0, int(spec["len"]))
        if "end" in spec:
            hi = _ix(spec["end"]) + 1
            lo = hi - ln
        else:
            lo, hi = n - ln, n
    elif "start" in spec:
        lo, hi = _ix(spec["start"]), n
    else:
        lo, hi = n, n
    lo = max(0, min(n, lo))
    hi = max(0, min(n, hi))
    if hi < lo:
        lo, hi = hi, lo
    return lo, hi


MIN_EFFECT_REL = 0.05


def _replication_segment(lo: int, hi: int, seg_len: int, n: int,
                         effect_rel: Optional[float] = None) -> Tuple[int, int]:
    """独立复现段（**纯几何规则，不看显著性**，因此没有「挑显著的那段」的自由度）。

    优先级：
    1. 与主段**不重叠**的剩余区间里，取与主段**同长**（``seg_len``）的一段：
       先看后段（主段之后，数据异常通常是后来的问题），后段不够长再看前段；
    2. 若两侧剩余都 < ``seg_len`` 但都 ≥ ``MIN_SEGMENT_N``，取**较长**的一侧；
    3. 若一侧 < ``MIN_SEGMENT_N``，取另一侧；
    4. 两侧都 < ``MIN_SEGMENT_N`` → 返回 ``(0, 0)``（复现不可判定）。

    既然后段优先是硬规则，就不会出现「按显著性挑段」；
    调用方（``judge_anomaly``）会把这个选择原样写进 ``scope`` 让用户复核。
    """
    tail_len = n - hi
    head_len = lo
    candidates: List[Tuple[str, int, int]] = []
    if tail_len >= MIN_SEGMENT_N:
        candidates.append(("tail", hi, min(n, hi + seg_len)))
    if head_len >= MIN_SEGMENT_N:
        candidates.append(("head", max(0, lo - seg_len), lo))
    if not candidates:
        return 0, 0
    full = [c for c in candidates if (c[2] - c[1]) >= seg_len]
    pool = full if full else candidates
    # 同长优先后段；否则取较长的一侧（仍不看显著性）
    pool.sort(key=lambda c: (-(c[2] - c[1]), 0 if c[0] == "tail" else 1))
    return pool[0][1], pool[0][2]


def _perm_p(observed: float, null_samples: np.ndarray, direction: str) -> float:
    """置换 p 值（双侧或单向），带 +1 平滑（避免 p=0 这种不可实现的报告值）。"""
    arr = np.asarray(null_samples, dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0 or not math.isfinite(observed):
        return 1.0
    if direction == "above":
        ext = int(np.sum(arr >= observed))
    elif direction == "below":
        ext = int(np.sum(arr <= observed))
    else:
        center = float(np.median(arr))
        ext = int(np.sum(np.abs(arr - center) >= abs(observed - center)))
    return float((ext + 1) / (arr.size + 1))


def _segment_value(feature: str, draws: Sequence[Dict],
                   lo: int, hi: int) -> Tuple[Optional[float], int]:
    """段内观测值（标量，统一定义为「越大越偏离中心」的信号量）。"""
    seg = draws[lo:hi]
    n = len(seg)
    if feature == "sum_mean":
        return _mean(red_sums(seg)), n
    if feature == "odd_count":
        return _mean(odd_counts(seg)), n
    if feature == "consecutive":
        return _mean(consecutive_flags(seg)), n
    if feature == "zone_dist":
        if n == 0:
            return None, 0
        obs = zone_matrix(seg).sum(axis=0).astype(float)
        exp = np.full(3, 2.0 * n)   # 每期每区期望 6/3 = 2.0
        return float(np.sum((obs - exp) ** 2 / exp)), n
    if feature == "num_freq":
        if n == 0:
            return None, 0
        counts = red_count_matrix(seg).sum(axis=0).astype(float)
        exp = np.full(33, 6.0 * n / 33.0)
        return float(np.sum((counts - exp) ** 2 / exp)), n
    return None, n


def _null_values(feature: str, rng: np.random.Generator, n_perm: int, n: int,
                 m: Optional[np.ndarray] = None) -> np.ndarray:
    """在「均匀随机」零假设下重采样 ``n_perm`` 组 n 期数据，返回同口径的统计量。

    重采样框架与真实抽奖一致：每期从 33 个红球里无放回抽 6 个（向量化实现：
    33 个独立 U(0,1) 取最小 6 个 —— 与无放回抽样**同分布**）。
    为了让「和值 / 奇数个数 / 连号率」这些**线性统计量**的零分布更精确，
    对它们额外采用「按真实序列重排」的置换零分布（见 ``judge_anomaly``）：
    重排不改变边缘分布，只破坏时间结构，是检验「某一段异常」的正确零假设。
    """
    keys = rng.random((int(n_perm), int(n), 33))
    picked = np.sort(np.argsort(keys, axis=2, kind="stable")[:, :, :6], axis=2) + 1
    if feature == "sum_mean":
        return picked.sum(axis=2).mean(axis=1)
    if feature == "odd_count":
        return (picked % 2 == 1).sum(axis=2).mean(axis=1)
    if feature == "consecutive":
        diffs = np.diff(picked, axis=2)
        return (diffs == 1).any(axis=2).mean(axis=1)
    if feature == "zone_dist":
        z = np.minimum(2, (picked - 1) // 11)
        per_draw = np.stack([(z == k).sum(axis=2) for k in range(3)],
                            axis=2).astype(float)
        # 每期每区期望 6/3 = 2.0；先按每期汇总再做卡方
        exp = 2.0 * int(n)
        return ((per_draw.sum(axis=1) - exp) ** 2 / exp).sum(axis=1)
    if feature == "num_freq":
        counts = np.zeros((int(n_perm), 33), dtype=float)
        flat_perm = np.repeat(np.arange(int(n_perm)), int(n) * 6)
        np.add.at(counts, (flat_perm, picked.reshape(-1) - 1), 1.0)
        exp = 6.0 * int(n) / 33.0
        return ((counts - exp) ** 2 / exp).sum(axis=1)
    raise ValueError(f"unknown feature: {feature}")


def _effect(feature: str, observed: Optional[float], real_sd: Optional[float]) -> Dict:
    """效应量：返回绝对效应与相对效应（相对理论基线的百分比）。

    对均值类特征（和值/奇数/连号率）用「观测值 − 理论均值」；
    对卡方类特征（三区/单号）用「观测卡方 − 自由度」（df 即零假设下的期望卡方）。
    """
    if observed is None:
        return {"effect": None, "effect_rel": None, "baseline": None}
    if feature == "sum_mean":
        eff = float(observed - SUM_THEORY_MEAN)
        return {"effect": eff, "effect_rel": eff / SUM_THEORY_MEAN,
                "baseline": SUM_THEORY_MEAN}
    if feature == "odd_count":
        eff = float(observed - ODD_THEORY_MEAN)
        return {"effect": eff, "effect_rel": eff / ODD_THEORY_MEAN,
                "baseline": ODD_THEORY_MEAN}
    if feature == "consecutive":
        eff = float(observed - CONSEC_THEORY_RATE)
        return {"effect": eff, "effect_rel": eff / CONSEC_THEORY_RATE,
                "baseline": CONSEC_THEORY_RATE}
    if feature == "zone_dist":
        eff = float(observed - 2.0)
        return {"effect": eff, "effect_rel": eff / 2.0, "baseline": 2.0}
    eff = float(observed - 33.0)
    return {"effect": eff, "effect_rel": eff / 33.0, "baseline": 33.0}


def judge_anomaly(claim: Dict, draws: Sequence[Dict], *,
                  window: int = DEFAULT_W_SEG,
                  n_perm: int = DEFAULT_N_PERM,
                  seed: int = DEFAULT_SEED,
                  alpha: float = DEFAULT_ALPHA,
                  m_adj: Optional[int] = None) -> Dict:
    """**确定性判定器**：对一条 LLM 提名给出 ``real_defect`` / ``regime_shift`` / ``noise``。

    参数
    ----
    claim: ``{feature, range?, direction?}``（``parse_anomalies`` 的输出可直接喂进来）。
    draws: 时间升序的开奖历史。
    window: 单段长度 W（默认 300）；claim 的 ``range`` 会覆盖它（但不会超过可用期数）。
    n_perm: 置换/重采样次数（固定 seed → 可复现）。
    seed: 随机种子（**唯一**随机性来源）。
    alpha: 显著性水平。
    m_adj: 多重检验分母。``None`` 时按「本次被判定的特征数」自动取
        （至少为 1，且不小于 5 个受支持特征中的实际检验数）。

    返回
    ----
    dict，关键字段（**p 值一定带 m_adj 与 scope**）：

    - ``feature`` / ``verdict`` ∈ {real_defect, regime_shift, noise}
    - ``p_value``: 未校正置换 p（不得单独展示）
    - ``m_adj``: 多重检验分母
    - ``p_adj``: ``min(1, p_value * m_adj)``
    - ``scope``: ``{segment, replication, n, m_adj, correction, note_zh}``
    - ``effect`` / ``effect_rel``: 绝对/相对效应（相对理论基线）
    - ``n``: 主段期数；``segments``: 主段/复现段的区间与各自 p
    - ``direction``: 观测方向（above/below/any）
    - ``degraded``: 输入不可判定时为 True（例如未知特征、期数不足）
    - ``note_zh``: 中文结论
    """
    base: Dict = {
        "feature": None, "verdict": "noise", "p_value": None, "p_adj": None,
        "m_adj": None, "effect": None, "effect_rel": None, "n": 0,
        "scope": None, "segments": [], "direction": "any", "degraded": False,
        "alpha": float(alpha), "seed": int(seed), "note_zh": "",
    }
    valid = _valid_draws(draws)
    n_total = len(valid)
    feat = _norm_feature((claim or {}).get("feature"))
    if feat is None:
        base.update({"degraded": True,
                     "feature": (claim or {}).get("feature"),
                     "note_zh": ("提名里的特征无法映射到确定性口径（支持：sum_mean / "
                                 "odd_count / zone_dist / num_freq / consecutive），"
                                 "无法判定，按 noise 处理。")})
        return base
    base["feature"] = feat
    direction = _norm_direction((claim or {}).get("direction"))

    if n_total < MIN_SEGMENT_N:
        base.update({"degraded": True,
                     "scope": _scope({"n": n_total}, m_adj or 1),
                     "note_zh": (f"有效数据仅 {n_total} 期（< {MIN_SEGMENT_N}），"
                                 "任何异常判定都没有功效，按 noise 处理。")})
        return base

    w = max(MIN_SEGMENT_N, int(window))
    spec = _norm_range((claim or {}).get("range"), n_total)
    lo, hi = _resolve_segment(spec, n_total) if spec else (max(0, n_total - w), n_total)
    if hi - lo < MIN_SEGMENT_N:
        base.update({"degraded": True,
                     "note_zh": (f"提名区间 [{lo},{hi}) 只有 {hi - lo} 期"
                                 f"（< {MIN_SEGMENT_N}），无法判定，按 noise 处理。")})
        lo, hi = max(0, n_total - w), n_total
    n_seg = hi - lo

    rng = np.random.default_rng(np.random.SeedSequence(int(seed)))
    observed, _ = _segment_value(feat, valid, lo, hi)

    # 零分布一律用「独立重采样」：每期从 33 个号无放回抽 6 个（与真实抽奖同分布）。
    # **绝不能**用「把真实序列打乱重排」当零分布：对和值均值 / 奇数均值 / 连号率
    # 这类**整段聚合的线性统计量**，重排完全不改变它们的取值（实测方差 4.4e-16），
    # 于是 p 值只反映真实数据自身的随机波动，纯随机数据也会被判「显著」——
    # 这正是方案 §4.2「短窗口显著」反例的实现级翻版。
    null = _null_values(feat, rng, int(n_perm), n_seg)
    p_main = _perm_p(observed if observed is not None else float("nan"), null, direction)

    # 观测量级与理论零中心（median(null) = 理论期望；用中位数而非均值，抗极值）
    center = float(np.median(null)) if null.size else float("nan")
    if observed is None or not math.isfinite(center):
        obs_direction = "any"
    elif observed > center:
        obs_direction = "above"
    elif observed < center:
        obs_direction = "below"
    else:
        obs_direction = "any"
    base["direction"] = obs_direction

    # ---- 独立复现段：**不重叠的另一段**（用于检验「只在单段出现」）----
    # 只取一段，是因为「复现」这个词必须对应一个确定的、事先定好的对照段；
    # 取多段再要求「全部显著」会把判据变成「整个数据集都偏」，
    # 那样 real_defect 只在「全局作弊」时可达，与「局部注入的缺陷」这一目标背离。
    # 段的选择是纯几何规则（见 _replication_segment），**不看显著性**。
    rlo, rhi = _replication_segment(lo, hi, n_seg, n_total)
    rep = None
    if rhi - rlo >= MIN_SEGMENT_N:
        obs_r, _ = _segment_value(feat, valid, rlo, rhi)
        null_r = _null_values(feat, rng, int(n_perm), rhi - rlo)
        p_r = _perm_p(obs_r if obs_r is not None else float("nan"), null_r, direction)
        rc = float(np.median(null_r)) if null_r.size else float("nan")
        dir_r = ("above" if (obs_r is not None and math.isfinite(rc) and obs_r > rc)
                 else ("below" if (obs_r is not None and math.isfinite(rc) and obs_r < rc)
                       else "any"))
        rep = {"start": rlo, "end": rhi - 1, "n": rhi - rlo,
               "p_value": p_r, "direction": dir_r}

    # ---- 多重检验分母 ----
    # 只有**主段**决定 verdict，所以 m_adj 只数「主段上的检验次数」：
    # m_adj=None → 按本函数据支持的特征数（5 个）校正；调用方给了就用调用方的
    # （``audit_draws`` 传「本次实际判定的特征数」，这是更保守也更准确的分母）。
    # 复现段单独给 p 值，并**明确标注它不构成独立检验**（它依附于主段的结果，
    # 只在主段显著之后才被检验），因此不并入 p_adj 的分母。
    m_used = len(FEATURES) if m_adj is None else max(1, int(m_adj))
    p_adj = None if p_main is None else float(min(1.0, p_main * m_used))
    significant = bool(p_adj is not None and p_adj < alpha)
    rep_significant = False
    rep_same_dir = False
    if rep is not None:
        rep["p_adj_scope_is_main"] = float(min(1.0, rep["p_value"] * m_used))
        rep_significant = bool(rep["p_value"] < alpha)
        rep_same_dir = bool(obs_direction != "any" and rep["direction"] == obs_direction)
    replicated = bool(rep is not None and rep_significant and rep_same_dir)
    claimed_ok = bool(direction == "any" or direction == obs_direction)

    if significant and claimed_ok and replicated:
        verdict = "real_defect"
        note = (f"{feat} 在主段 [{lo},{hi}) 显著（p_adj={p_adj:.3g} < {alpha}，"
                f"方向{_dir_zh(obs_direction)}），且在**不重叠**的复现段 "
                f"[{rlo},{rhi}) 也显著同向（未校正 p={rep['p_value']:.3g}）："
                "两段独立数据同时成立，判 real_defect（数据质量缺陷）。")
    elif significant and claimed_ok:
        verdict = "regime_shift"
        if rep is None:
            why = ("主段之外没有可用的不重叠复现段（剩余期数不足以构成同长段），"
                   "「两段复现」不可判定")
        elif rep_significant and not rep_same_dir:
            why = f"复现段方向相反（{_dir_zh(rep['direction'])}）"
        else:
            why = (f"复现段 [{rlo},{rhi}) 不显著（未校正 p={rep['p_value']:.3g} ≥ {alpha}）")
        note = (f"{feat} 在主段 [{lo},{hi}) 显著（p_adj={p_adj:.3g} < {alpha}），"
                f"但{why}：只在单段出现，判 regime_shift（状态变化/暂时现象），"
                "不能当数据缺陷，也不能当可用于预测的规律。")
    else:
        verdict = "noise"
        if p_adj is None:
            note = f"{feat} 无法计算 p 值，判 noise。"
        elif not significant:
            note = (f"{feat} 在主段 [{lo},{hi}) 不显著（p_adj={p_adj:.3g} ≥ {alpha}，"
                    f"未校正 p={p_main:.3g} × m_adj={m_used}）：与纯随机波动不可区分，"
                    "判 noise。")
        else:
            note = (f"{feat} 虽显著（p_adj={p_adj:.3g}），但观测方向"
                    f"（{_dir_zh(obs_direction)}）与提名方向（{_dir_zh(direction)}）不一致，"
                    "判 noise（提名方向错误）。")

    eff = _effect(feat, observed, None)
    scope = _scope({"start": lo, "end": hi - 1, "len": n_seg,
                    "replication": (None if rep is None else
                                    {"start": rep["start"], "end": rep["end"],
                                     "n": rep["n"]}),
                    "replication_p_note": ("复现段 p 值**不**并入 p_adj 的分母："
                                           "它依附于主段，只在主段显著后才有意义；"
                                           "其值需与 scope.segment 一起展示。")},
                   m_used, verdict=verdict)
    base.update({
        "verdict": verdict,
        "p_value": float(p_main),
        "p_adj": p_adj,
        "m_adj": m_used,
        "effect": eff["effect"],
        "effect_rel": eff["effect_rel"],
        "baseline": eff["baseline"],
        "observed": None if observed is None else float(observed),
        "n": int(n_seg),
        "scope": scope,
        "segments": [{"role": "main", "start": lo, "end": hi - 1, "n": n_seg,
                      "p_value": float(p_main), "p_adj": p_adj,
                      "direction": obs_direction}] + (
            [] if rep is None else
            [{"role": "replication", "start": rep["start"], "end": rep["end"],
              "n": rep["n"], "p_value": rep["p_value"],
              "p_adj": float(min(1.0, rep["p_value"] * m_used)),
              "m_adj": m_used,
              "m_adj_note": ("复现段的 p_adj 用与主段相同的 m_adj，"
                             "但它不参与 verdict 的显著性判定（判决看未校正 p < α）；"
                             "该 m_adj 只用于标注口径，不得跨段解读。"),
              "direction": rep["direction"],
              "significant": rep_significant, "same_direction": rep_same_dir}]),
        "replicated": replicated,
        "n_replication": 0 if rep is None else 1,
        "n_perm": int(n_perm),
        "note_zh": note,
    })
    return base


def _dir_zh(d: str) -> str:
    return {"above": "偏高", "below": "偏低"}.get(d, "不明确")


def _scope(segments: Dict, m_adj: int, verdict: str = "") -> Dict:
    """p 值的适用范围（**无 scope 的 p 值不得展示**）。"""
    return {
        "segment": segments,
        "m_adj": int(m_adj),
        "correction": "bonferroni",
        "p_value_kind": "permutation-two-sided-within-claimed-direction",
        "note_zh": (f"p_adj = min(1, p_value × m_adj)，m_adj={m_adj}（本次判定的检验数）。"
                    "未校正 p 不得单独展示；该 p 只对该段数据与该特征有效，"
                    "不外推到其它期号或其它特征。"),
        "verdict": verdict or None,
    }


# --------------------------------------------------------------------------
# 编排：LLM 提名 → 代码判定（全链路；LLM 不可用时 degraded=True，绝不崩）
# --------------------------------------------------------------------------

def audit_draws(draws: Sequence[Dict], *, window: int = DEFAULT_WINDOW,
                w_seg: int = DEFAULT_W_SEG, n_perm: int = DEFAULT_N_PERM,
                seed: int = DEFAULT_SEED, alpha: float = DEFAULT_ALPHA,
                use_llm: bool = True) -> Dict:
    """跑一次完整审计：算统计 → 让 LLM 提名（可关）→ 逐条代码判定。

    返回
    ----
    dict：``{ok, generated_at, n_draws, window, claims, judgments, summary,
    degraded, note_zh, llm}``。

    - ``degraded=True`` 出现在：LLM 不可用/返回空、解析降级、或没有可判定提名。
      此时 ``judgments`` 可能为空，但**不抛异常**。
    - ``summary``: ``{n_claims, n_real_defect, n_regime_shift, n_noise,
      n_degraded, n_undetermined}`` —— 注意 ``n_noise`` 是「被判定为噪声」，
      **不含**「没法判」的那部分（``n_degraded``）。弃权不并入任何一类。
    """
    valid = _valid_draws(draws)
    prompt = build_audit_prompt(valid, window=window)
    stats = build_stats(valid, window=window) if valid else {}
    llm_meta: Dict = {"called": False, "degraded": False, "error": None, "raw_chars": 0}
    raw_text: Any = None
    if use_llm:
        try:
            from . import llm_client
            llm_meta["called"] = True
            raw_text = llm_client.chat_json(
                AUDIT_SYSTEM_PROMPT, prompt, max_tokens=1200, temperature=0.2,
                purpose="data_audit")
            llm_meta["raw_chars"] = len(str(raw_text)) if raw_text is not None else 0
            if raw_text is None:
                llm_meta["degraded"] = True
                llm_meta["error"] = "LLM 不可用或返回空（未配置通道 / 超时 / 被禁用）"
        except Exception as e:  # noqa: BLE001 - LLM 任何故障都必须降级而不是崩
            llm_meta["degraded"] = True
            llm_meta["error"] = f"{type(e).__name__}: {e}"
            raw_text = None
    else:
        llm_meta["degraded"] = True
        llm_meta["error"] = "use_llm=0（本次不调用 LLM）"

    claims, parse_error = parse_anomalies(raw_text)
    if parse_error:
        llm_meta["degraded"] = True
        llm_meta["error"] = llm_meta["error"] or parse_error

    # 多重检验分母 = 本次实际判定的特征数（同一特征只判一次）；
    # 不同特征的判定要共用同一个 m，否则「检验了多少个东西」会被低估。
    feats: List[str] = []
    for c in claims:
        if c["feature"] not in feats:
            feats.append(c["feature"])
    m_adj = max(1, len(feats))

    judgments: List[Dict] = []
    for c in claims:
        j = judge_anomaly(c, valid, window=w_seg, n_perm=n_perm, seed=seed,
                          alpha=alpha, m_adj=m_adj)
        j["claim"] = c
        judgments.append(j)

    counts = {"real_defect": 0, "regime_shift": 0, "noise": 0}
    for j in judgments:
        counts[j["verdict"]] = counts.get(j["verdict"], 0) + 1
    summary = {
        "n_claims": len(claims),
        "n_real_defect": counts["real_defect"],
        "n_regime_shift": counts["regime_shift"],
        "n_noise": counts["noise"],
        "n_degraded": sum(1 for j in judgments if j.get("degraded")),
        "n_undetermined": max(0, len(feats) - sum(1 for f in feats
                                                   if any(j["feature"] == f for j in judgments))),
        "m_adj": m_adj,
        "alpha": float(alpha),
        "n_draws": len(valid),
    }
    degraded = bool(llm_meta["degraded"] or not judgments)
    if not valid:
        note = "本地暂无有效开奖数据，审计未执行（degraded=True）。"
    elif not claims:
        note = ("LLM 没有提出可判定的异常项（或 LLM 不可用）："
                f"本次 n_claims=0，degraded={degraded}。"
                "「没有提名」不等于「数据干净」——它只表示本轮没做判定。")
    else:
        note = (f"LLM 提名 {len(claims)} 项，代码判定：real_defect "
                f"{counts['real_defect']} / regime_shift {counts['regime_shift']} / "
                f"noise {counts['noise']}（m_adj={m_adj}，α={alpha}）。"
                "只有 real_defect 才是数据缺陷；regime_shift 是状态变化，"
                "noise 表示与随机波动不可区分。")
    return {
        "ok": True,
        "generated_at": None,          # 由调用方补（审计产物本身不参与可复现性验收）
        "n_draws": len(valid),
        "window": int(window),
        "w_seg": int(w_seg),
        "n_perm": int(n_perm),
        "seed": int(seed),
        "alpha": float(alpha),
        "stats": stats,
        "prompt_chars": len(prompt),
        "claims": claims,
        "judgments": judgments,
        "summary": summary,
        "degraded": degraded,
        "llm": llm_meta,
        "note_zh": note,
        "feature_desc_zh": {k: FEATURE_DESC_ZH[k] for k in FEATURES},
    }


# --------------------------------------------------------------------------
# 持久化（最近一次审计结果）：``GET /api/audit/anomaly/latest`` 只读它
# --------------------------------------------------------------------------

def _latest_path():
    """审计产物路径。**函数而非模块常量**：测试会替换 ``config.DATA_DIR``。"""
    from . import config
    return config.DATA_DIR / "data_audit_latest.json"


def save_latest(payload: Dict) -> str:
    """把审计结果写盘（原子替换）。返回落盘路径；失败返回空串（不抛）。"""
    path = _latest_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, default=str),
                       encoding="utf-8")
        os.replace(str(tmp), str(path))
        return str(path)
    except OSError:
        return ""


def latest_audit() -> Dict:
    """读最近一次审计结果。无记录/损坏时返回 ``{"available": False, ...}``。"""
    path = _latest_path()
    if not path.exists():
        return {"available": False,
                "note_zh": ("尚未运行过数据审计。可调用 POST /api/audit/anomaly/run "
                            "在后台跑一次（不阻塞），完成后本端点即可读到结果。"),
                "path": str(path)}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {"available": False,
                "note_zh": f"审计产物无法解析（{type(e).__name__}），已忽略。",
                "path": str(path)}
    if not isinstance(payload, dict):
        return {"available": False, "note_zh": "审计产物格式异常，已忽略。",
                "path": str(path)}
    payload = dict(payload)
    payload["available"] = True
    payload["path"] = str(path)
    return payload


def run_audit(draws: Optional[Sequence[Dict]] = None, *, use_llm: bool = True,
              window: int = DEFAULT_WINDOW, w_seg: int = DEFAULT_W_SEG,
              n_perm: int = DEFAULT_N_PERM, seed: int = DEFAULT_SEED,
              progress=None) -> Dict:
    """跑一次审计并落盘（供 ``POST /api/audit/anomaly/run`` 的后台任务调用）。

    任何异常都被转换成 ``ok=False, degraded=True`` 的返回体 —— 后台任务里
    抛异常只会让任务变成 failed，谁也看不到原因。
    """
    try:
        if progress:
            progress(0.05, "读取开奖数据…")
        if draws is None:
            from . import db
            draws = db.load_draws()
        if progress:
            progress(0.2, "计算确定性统计…")
        result = audit_draws(draws, window=window, w_seg=w_seg, n_perm=n_perm,
                             seed=seed, use_llm=use_llm)
        result["generated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if progress:
            progress(0.9, "写入审计产物…")
        saved = save_latest(result)
        result["saved"] = bool(saved)
        result["saved_path"] = saved
        return result
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "degraded": True,
                "error": "数据审计执行失败",
                "detail": f"{type(e).__name__}: {e}",
                "note_zh": "数据审计执行失败（已降级，不影响出号）。"}
