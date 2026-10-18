"""V11 多尺度扫描：按不同时间窗口评估规律，并标注每档的**检出能力**。

为什么需要它（用户 2026-10-18 提问）
------------------------------------
用户问：「长期是所有期数，中长期 150 期，中期 80-100，短期 30-60，这样分是否合适？
150 期差不多一年了，很难找到合适的规律。」

问题是真的，但**缩短窗口不会让规律更容易找到，只会让"能检出的效应"门槛更高**：

===========  ================  ==============
窗口          可检出的最小 Δ    相对基线
===========  ================  ==============
30 期         0.444             +40.7%
40 期         0.384             +35.2%
60 期         0.314             +28.8%
100 期        0.243             +22.3%
150 期        0.199             +18.2%
300 期        0.140             +12.9%
500 期        0.109             +10.0%
全部 3515 期  0.041             +3.8%
===========  ================  ==============

而 Lead 在 3515 期实测：**最好的真实效应只有 +0.116（约 +10.6%）**，
在 30~100 期窗口下**根本不可检出**——这些窗口「捞到显著」的几乎都是噪声。

因此本模块的立场不是「分档就能找到规律」，而是：

1. **把每档的检出能力摆在明面上**（MDE、名义阈值、校正后阈值）；
2. 短窗口的「显著」必须与它极高的 MDE 并排显示，用户一眼能看出不可信；
3. 分档的真正用途是**检测变化**（某档突然变强 → 值得关注），而不是「短窗口更容易找规律」。

数据驱动的档位选择（依据 docs/reviews/PHASE_USABLE_STUDY.md）
------------------------------------------------------------
Lead 实测各窗口「当下显著 → 下一窗口仍同向」的延续率：

======  ==============  ==================
窗口    延续率          稳定性
======  ==============  ==================
30 期   ~52%            稳定（= 随机）
40 期   62~64%          稳定
50 期   66%             稳定
60 期   61~67%          稳定
70 期   48~56%          掉回随机
80 期   44~54%          掉回随机
150 期  ~52%            稳定（= 随机）
======  ==============  ==================

40~60 期有一个清晰的高延续率带，但**前后半段交叉验证不一致**（前半 74% vs 后半 53%），
且 Bonferroni 校正后 p=0.073 > 0.05 —— 属「值得观察的弱迹象」，不是已验证规律。

因此采用 **5 档**（覆盖用户提出的四个层级 + 两年档）：

* ``short``  40 期    —— 用户「短期」；仅能发现极端效应（MDE +35%）
* ``mid``    60 期    —— 用户「短期上限/中期下沿」；实测延续率最高的区间
* ``medium`` 300 期   —— 用户「中期」的替代：**避开实测最差的 70~100 期**
* ``long``   150 期   —— 用户「中长期」；一年尺度，检测年度级变化
* ``all``    全部期数 —— 用户「长期」；唯一能检出小效应（+3.8%）的档

刻意**不采用 80~100 期**：实测该区间延续率掉回随机水平，是最差的选择。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from . import discovery

# 单注红球命中标准差（超几何 N=33,K=6,n=6；已用 pmf 数值验证）
SIGMA1 = 0.8678
# 理论基线：每注命中期望 6*6/33
BASELINE_MEAN = 6 * 6 / 33
# 双侧 α=0.05 + power=0.8 的临界常数
_Z_ALPHA = 1.959964
_Z_POWER = 0.841621

# 档位定义（顺序即展示顺序：从长到短）
SCALES: List[Dict[str, Any]] = [
    {"key": "all",    "label_zh": "长期（全部期数）", "window": None,
     "role_zh": "唯一能检出小效应（MDE +3.8%）的档；找通用规律看这里"},
    {"key": "long",   "label_zh": "中长期（150 期 ≈ 1 年）", "window": 150,
     "role_zh": "年度尺度：检测年度级变化；MDE +18%，只能发现较大效应"},
    {"key": "medium", "label_zh": "中期（300 期 ≈ 2 年）", "window": 300,
     "role_zh": "两年级结构变化；MDE +13%（刻意避开实测最差的 70~100 期）"},
    {"key": "mid",    "label_zh": "短中期（60 期 ≈ 5 个月）", "window": 60,
     "role_zh": "实测延续率最高区间（66%）；MDE +29%，仅能发现极端效应"},
    {"key": "short",  "label_zh": "短期（40 期 ≈ 3 个月）", "window": 40,
     "role_zh": "最短档；MDE +35%，『显著』几乎必是噪声，仅作观察"},
]

SCALE_BY_KEY = {s["key"]: s for s in SCALES}


def mde_for(n: int) -> Dict[str, float]:
    """窗口 n 期在 α=0.05 / power=0.8 下能检出的最小效应。

    返回 ``{abs, rel, z_crit}``：绝对 Δ、相对基线比例、名义临界 z。
    """
    n = max(2, int(n))
    abs_mde = (_Z_ALPHA + _Z_POWER) * SIGMA1 / np.sqrt(n)
    return {
        "n": int(n),
        "abs": float(abs_mde),
        "rel": float(abs_mde / BASELINE_MEAN),
        "z_crit": float(_Z_ALPHA),
    }


def _window_series(draws: Sequence[Dict], flt: Dict, window: Optional[int]) -> Dict:
    """取某窗口内的 (obs, exp) 序列；window=None 表示全部可评估期。"""
    n = len(draws)
    start = max(200, 0 if window is None else n - int(window))
    res = discovery.asof_scan(draws, flt, start=start, end=n)
    obs = np.asarray(res.get("obs") or [], dtype=float)
    exp = np.asarray(res.get("exp") or [], dtype=float)
    return {"start": start, "n": len(obs), "obs": obs, "exp": exp}


def scale_scan(draws: Sequence[Dict], key: str, params: Dict,
               scales: Optional[Sequence[str]] = None) -> Dict:
    """对单个维度做多尺度扫描。

    返回 ``{key, params, scales: {scale_key: {...}}, recommendation}``。
    每个 scale 内含：``n`` / ``delta`` / ``z`` / ``p_raw`` / ``mde`` /
    ``verdict``（显著/不显著）/ ``credible``（该显著是否超越 MDE，即"够大"）。

    **``credible`` 是本模块的核心设计**：一个 p<0.05 的"显著"如果效应量还没达到
    该窗口的 MDE，说明它是靠样本波动凑出来的，标注为不可信。
    """
    scales = list(scales or [s["key"] for s in SCALES])
    try:
        flt = discovery.make_filter(key, params or {})
    except Exception as exc:                              # noqa: BLE001
        return {"key": key, "params": params, "error": f"make_filter 失败: {exc}",
                "scales": {}, "recommendation": "invalid"}

    out: Dict[str, Dict] = {}
    for skey in scales:
        spec = SCALE_BY_KEY.get(skey)
        if not spec:
            continue
        ser = _window_series(draws, flt, spec["window"])
        if ser["n"] < 5:
            out[skey] = {"n": ser["n"], "label_zh": spec["label_zh"],
                         "verdict": "insufficient", "credible": False,
                         "reason_zh": f"窗口内仅 {ser['n']} 期可评估，不下结论"}
            continue
        d = ser["obs"] - ser["exp"]
        mean = float(d.mean())
        sd = float(d.std(ddof=1))
        z = float(mean / (sd / np.sqrt(ser["n"]))) if sd > 0 else 0.0
        # 双侧 p（正态近似；大样本下 t 与 z 差异可忽略）
        p_raw = float(2 * (1 - _norm_cdf(abs(z))))
        m = mde_for(ser["n"])
        significant = p_raw < 0.05
        # 关键：显著 **且** 效应量达到该档 MDE 才算"可信的发现"
        credible = bool(significant and abs(mean) >= m["abs"])
        out[skey] = {
            "n": ser["n"], "label_zh": spec["label_zh"], "role_zh": spec["role_zh"],
            "delta": round(mean, 4), "z": round(z, 2), "p_raw": round(p_raw, 4),
            "mde_abs": round(m["abs"], 4), "mde_rel": round(m["rel"], 4),
            "significant": bool(significant), "credible": credible,
            "verdict": ("credible" if credible else
                        ("noise-level" if significant else "not-significant")),
            "reason_zh": _scale_reason(mean, m, significant, credible),
        }

    return {
        "key": key, "params": params or {},
        "scales": out,
        "recommendation": _recommend(out),
        "scale_legend": {s["key"]: {"label_zh": s["label_zh"],
                                    "window": s["window"], "role_zh": s["role_zh"]}
                         for s in SCALES if s["key"] in out},
    }


def _scale_reason(mean: float, mde: Dict, significant: bool, credible: bool) -> str:
    if credible:
        return (f"效应 {mean:+.4f} 达到该档检出下限 {mde['abs']:.4f}，"
                f"是本档可信的发现（仍需前置注册 + A/B 确认）")
    if significant:
        return (f"p<0.05 但效应 {mean:+.4f} 未达该档检出下限 {mde['abs']:.4f} —— "
                f"属『靠样本波动凑出来的显著』，不可信")
    return (f"未达显著（效应 {mean:+.4f} vs 本档需检测到 {mde['abs']:.4f}）")


def _recommend(out: Dict[str, Dict]) -> str:
    """综合多档结论给出建议。

    * 任一档 ``credible`` → ``watch``（值得观察，仍需前置注册）
    * 只有短档显著（长档不显著）→ ``short-noise``（典型的多尺度假阳性）
    * 全部不显著 → ``none``
    * 长档显著但短档反向 → ``regime-shift``（可能的结构变化，最值得关注）
    """
    cred = [k for k, v in out.items() if v.get("credible")]
    sig = [k for k, v in out.items() if v.get("significant")]
    if cred:
        return "watch"
    if not sig:
        return "none"
    long_sig = out.get("all", {}).get("significant") or out.get("long", {}).get("significant")
    short_sig = out.get("short", {}).get("significant") or out.get("mid", {}).get("significant")
    if short_sig and not long_sig:
        return "short-noise"
    # 长短方向相反
    la = out.get("all", {}).get("delta")
    sa = out.get("mid", {}).get("delta")
    if la is not None and sa is not None and la * sa < 0:
        return "regime-shift"
    return "weak"


def multi_scale_scan(draws: Sequence[Dict],
                     cases: Optional[Sequence[tuple]] = None,
                     scales: Optional[Sequence[str]] = None,
                     only_interesting: bool = True) -> Dict:
    """批量多尺度扫描（默认覆盖关键维度）。

    返回 ``{scales: [...], results: [...], summary: {...}}``。
    ``only_interesting=True`` 时只保留 recommendation 不为 ``none`` 的条目，
    避免界面上全是"不显著"。
    """
    if cases is None:
        cases = _default_cases()
    results = [scale_scan(draws, k, p, scales=scales) for k, p in cases]
    if only_interesting:
        kept = [r for r in results if r.get("recommendation") not in (None, "none")]
    else:
        kept = results
    from collections import Counter
    rec = Counter(r.get("recommendation") for r in results)
    return {
        "scales": [s for s in SCALES if not scales or s["key"] in scales],
        "results": kept,
        "n_scanned": len(results),
        "n_interesting": len(kept),
        "summary": dict(rec),
        "note_zh": ("多尺度扫描把每档的『检出能力』摆在明面上：短窗口看似更容易"
                    "『显著』，其实是它的检出下限高达 +35%，任何『显著』都可能是噪声；"
                    "只有效应量达到该档下限（credible）才值得进一步验证。"),
    }


def _default_cases() -> List[tuple]:
    """默认扫描的维度集合（覆盖热/冷/重号/邻号/遗漏五类）。"""
    return [
        ("hot", {"window": 30, "k": 6}),
        ("hot", {"window": 50, "k": 6}),
        ("hot", {"window": 100, "k": 6}),
        ("cold", {"window": 30, "k": 6}),
        ("cold", {"window": 50, "k": 6}),
        ("repeat", {"back": 1}),
        ("repeat2", {"back": 2}),
        ("neighbor", {"back": 1, "offset": 1}),
        ("neighbor", {"back": 1, "offset": 2}),
        ("omission_high", {"k": 6}),
    ]


def _norm_cdf(x: float) -> float:
    """标准正态 CDF（用 erf，避免引入 scipy 依赖）。"""
    from math import erf, sqrt
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))
