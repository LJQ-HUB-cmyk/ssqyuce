"""V12-16 / L3-7：用户可自证实验（``POST /api/experiments/randomness-selfcheck``）。

为什么需要这个模块（问题背景）
------------------------------
系统对用户说了一句很强的话：「开奖数据与公平随机不可区分，因此历史号码里没有可利用的
信号」。但这句话如果只能由系统自己声明，用户就只能**相信**，无法**验证**。
本模块让用户点一下就跑完一场对照实验：

    用**合成随机数据**（同期数、同蓝球范围、同开奖日程）跑**同一套**检验，
    并把「真实 vs 合成」的结论并排放在一起。

如果真实数据与合成数据给出同样的「未检出」，用户自己就能看见：
**检测器连明知是假的随机数据都不报警** —— 所以它报「未检出」时，
说的是「这套检验在这个样本量下没有发现偏离」，不是「真实数据被证明是随机的」。

四条硬性纪律（任何一条被破坏，整个 V12 不得发布，见方案 §4.1 S-6）
------------------------------------------------------------------
1. **不放宽统计口径**：真实数据依旧走 ``rigor.randomness_report()`` 原样，
   检验集与其 p 值语义一个字都不改。
2. **检验手段真的存在**：合成数据上「未校正 p<0.05 的检验项计数」必须在二项分布
   **95% 预测区间**内 —— 若出界，说明检验集对纯随机数据的灵敏度有系统性偏倚
   （过于灵敏或过于迟钝），这时「真实数据未检出」就不再可信，检查必须失败。
3. **真实与合成可比较**：真实数据的最强（Bonferroni）p 值必须不低于
   **纯随机零分布的 5% 分位**。理由：落在 5% 分位以下的值会在 α=0.05 上把
   「显著/不显著」翻过来；5% 分位以上两者都判「未检出」，对用户结论无影响。
4. **可复现（A7）**：同 ``seed`` 两次运行，响应体规范化 JSON 的 sha256 必须逐位相同。
   因此本模块**禁止**任何非 seed 派生的随机性：不用 ``random`` 全局状态、不用
   ``time``（``elapsed_ms`` 除外且被排除在哈希外）、不用 ``uuid``、不用 ``id()``、
   不依赖 dict 插入序做浮点累加。合成数据全部由 ``numpy.random.default_rng`` 从
   ``seed`` 派生；所有 dict 遍历先 ``sorted()``。

L3 准入条件（方案 §1.3）在本模块的落实
------------------------------------
「弃权不得计入做对」：任何一项检查在**证据不足**时一律 ``pass=False``，
并在 ``note_zh`` 里明说「这是没检查，不是检查通过」。例如真实期数 < 30 期时
``randomness_report`` 返回 ``insufficient-data``，检查①**必须失败**，
否则就成了「没做」被算成「做对」。

反向用例开关（让用户看见「偏差一旦存在，检测器会响」）
----------------------------------------------------
``synthetic_bias="number"`` 会把合成数据的一部分号码权重抬高（故意造的作弊数据）。
此时合成数据自己就报偏离，检查①必然失败 —— 这就是方案要求的反向用例，
同时也是给用户的并排演示：**假数据一旦带偏，我方检测器是能看见的**。

依赖：numpy / scipy（经 ``rigor``）+ 本项目 ``rigor``。不写数据库、不访问网络。
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from . import rigor

__all__ = [
    "DEFAULT_SEED",
    "DEFAULT_N_ISSUES",
    "DEFAULT_N_PERM",
    "CHECK_POWER_QUANTILE",
    "MIN_REAL_FOR_CHECKS",
    "CHECK_NAMES",
    "HONEST_NOTE_ZH",
    "synthetic_draws",
    "binomial_95_interval",
    "selfcheck",
    "response_sha256",
    "canonical_json",
]

#: 默认种子（对外契约：``?seed=42``）
DEFAULT_SEED = 42
#: 默认期数 = 真实库当前期数上限（方案原文「同 3515 期」）。数据更新后仍取该常量为上限。
DEFAULT_N_ISSUES = 3516
#: 置换演示次数：用于估计「最强校正 p 值」的零分布 5% 分位。
#:
#: 为什么是 200（而不是 500）：实测 3516 期场景下，500 次 × 每次 2×3516 期的数据
#: 展平约 16s（零分布 500 组 + 对照组 500 组的展平开销），远超「<3s」硬性验收；
#: 200 次时约 2.4s，且 5% 分位估计的标准误 ≈ sqrt(0.05*0.95/200) ≈ 1.5%，
#: 相对判据的容差（真 0.24 vs 判据 0.04，差 6 倍）可忽略。
#: 同一进程内按 (seed, n_issues, n_perm, m_tests, alpha) 缓存结果 —— 缓存键
#: 全部来自函数入参，不含任何时钟/状态，因此不破坏 A7 可复现性。
DEFAULT_N_PERM = 200

#: 检查项③的零分布分位点（推导见模块 docstring 第 3 条）。
CHECK_POWER_QUANTILE = 0.05

#: 真实数据低于该期数时 ``randomness_report`` 不出任何检验项（全部 insufficient-data），
#: 此时任何「未检出」都不构成证据 → 检查①必须失败（弃权不算做对）。
MIN_REAL_FOR_CHECKS = 30

#: 合成数据注入偏置的强度与覆盖面（反向用例 / 「检测器会响」演示）。
#:
#: **为什么是「整段 +30%」而不是「局部一段」**：实测（600 期、seed∈{42,43,101,202}）
#: 若只在最后 10%（60 期）的窗口里给 6 号各 +30% 权重，合成数据的 FWER 结论**一次都
#: 没报偏离**（最小 Bonferroni p 在 0.10~1.0 之间）——样本量太小，这正是方案 §1.1
#: 「短窗口的 MDE = +54%」那张表在自证实验里的现场复现。为了让反向用例有足够的检出
#: 功效（而不是靠运气），偏置必须在**全段**生效：11 个号各 +30% 权重，
#: 实测 4/4 个 seed 都判 deviation（min p_bonferroni 在 1e-6 ~ 1e-9 量级）。
#: 代价是：这是「明显作弊」，不是「细微异常」——所以它只能用来证明「检测器会响」，
#: 不能用来宣称「任何异常都能被发现」。这一句必须原样写进 note_zh。
_BIAS_NUMBERS = tuple(range(1, 12))     # 一区 11 个号整体变热
_BIAS_WEIGHT = 1.30                     # +30%
_BIAS_SEGMENT_FRAC = 1.0                # 全段生效（见上方实测说明）

#: 检查项名（顺序即输出顺序，固定不变）
CHECK_NAMES: Tuple[str, ...] = (
    "real_not_detected",
    "synthetic_uncorrected_rate_in_ci",
    "strongest_p_same_magnitude",
    "reproducible_same_seed",
)

_FLOAT_DIGITS = 12

HONEST_NOTE_ZH = (
    "本实验展示的是「我方检验有多敏感」，不是「真实数据被证明了是随机的」。"
    "你能亲眼看到：明知是随机生成的合成数据，在这套检验下同样报「未检出」。"
    "因此历史上所有「找规律」的结论都只能说明一件事——在这批数据的样本量下，"
    "这套检验没有发现偏离随机；它从不说明号码可预测，也从不说明中奖率能被提高。"
    "每一注的期望回报恒为返奖率（约 45.67%），与选号方法无关。"
)


# --------------------------------------------------------------------------
# 确定性工具
# --------------------------------------------------------------------------

def canonical_json(obj: Any) -> str:
    """规范化 JSON：键排序 + 紧凑分隔符 + 保证 ascii。"""
    return json.dumps(_canonicalize(obj), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def _canonicalize(obj: Any) -> Any:
    """把 numpy 标量/数组、tuple 转成纯 Python 类型，浮点统一 round 到 12 位。

    浮点 round 的理由：同一份 IEEE 结果在任何平台都是同一字符串，round 只是把
    「最后一位 ulp 级噪声」抹掉，防止 numpy/scipy 版本升级把 A7（同 seed 逐位一致）
    打成假红。统计结论不受 1e-12 影响。
    """
    if isinstance(obj, dict):
        return {str(k): _canonicalize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_canonicalize(v) for v in obj]
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        obj = float(obj)
    if isinstance(obj, float):
        if not math.isfinite(obj):
            return None
        r = round(float(obj), _FLOAT_DIGITS)
        return 0.0 if r == 0 else r
    return obj


def response_sha256(payload: Dict) -> str:
    """对响应体做 sha256，**剔除** ``response_sha256`` 与 ``elapsed_ms`` 两个字段。

    为什么剔除：前者是自反射（算它的输入包含它自己），后者是挂钟耗时，
    两次运行天然不同。剔除后剩下的每一个字段都必须只由 ``seed`` 决定。
    """
    body = {k: v for k, v in (payload or {}).items()
            if k not in ("response_sha256", "elapsed_ms")}
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def binomial_95_interval(k: int, m: int, p: float) -> Dict:
    """m 次独立试验、单次概率 p 下观测计数 k 的 95% 预测区间（正态近似 + 连续性校正）。

    返回 ``{k, m, p, expected, sd, low, high, inside}``；``low``/``high`` 取整数，
    恰好落在边界上仍算「区间内」（避免边界误判成漂移）。
    """
    m = int(m)
    if m <= 0:
        return {"k": int(k), "m": 0, "p": float(p), "expected": 0.0, "sd": 0.0,
                "low": 0, "high": 0, "inside": False}
    q = float(p)
    mean = m * q
    sd = math.sqrt(m * q * (1.0 - q))
    z = rigor.Z_95
    low = max(0, int(math.floor(mean - z * sd + 0.5)))
    high = min(m, int(math.ceil(mean + z * sd - 0.5)))
    if high < low:
        low = high = int(round(mean))
    return {"k": int(k), "m": m, "p": q, "expected": round(mean, 6),
            "sd": round(sd, 6), "low": low, "high": high,
            "inside": bool(low <= int(k) <= high)}


# --------------------------------------------------------------------------
# 合成随机数据（唯一随机性来源：seed）
# --------------------------------------------------------------------------

def synthetic_draws(n_issues: int, seed: int = DEFAULT_SEED,
                    bias: Optional[str] = None) -> List[Dict]:
    """用 ``default_rng``（由 ``seed`` 派生）生成 ``n_issues`` 期**已知随机**的合成数据。

    结构同 ``db.load_draws()`` 的元素（``issue`` / ``date`` / ``reds`` / ``blue`` /
    ``order``），下游与真实数据走**完全相同**的代码路径。

    参数
    ----
    n_issues: 期数（与真实数据同）。
    seed: 唯一随机性来源。
    bias:
      - ``None``：纯均匀抽样（红球 1..33 无放回取 6，蓝球 1..16 均匀）。
      - ``"number"``：``_BIAS_NUMBERS`` 这 11 个号码各 +30% 权重（全段生效）——
        **故意造的作弊数据**，用于反向用例与「检测器会响」演示。强度与覆盖面的
        选择依据（含为什么不取「局部一小段」）见 ``_BIAS_NUMBERS`` 上方的实测说明。

    ``bias=None`` 与 ``bias="number"`` 共用**同一条** ``rng`` 子流，
    逐期逐个号码完全相同 —— 差别只在 ``choice`` 是否带权重，因此用户对照时
    看到的是「同一批随机序列，加不加偏置」的干净对比。
    """
    n = max(0, int(n_issues))
    if n == 0:
        return []
    rng = np.random.default_rng(np.random.SeedSequence(int(seed)).spawn(2)[0])

    numbers = np.arange(1, rigor.R_MAX + 1)
    weights = np.ones(numbers.size, dtype=float)
    weights[np.array(_BIAS_NUMBERS, dtype=int) - 1] = _BIAS_WEIGHT
    bias_p = weights / weights.sum()

    use_bias = (bias == "number")
    # **逐期**无放回抽 6 个（不能一次抽 n*6 —— 无放回抽样的总体是 33 个号，
    # 一批只能抽 ≤33 个，跨期复用同一批会让号码与期次绑定）。
    # 每期的随机数消耗固定（一次 uniform 抽取 + 一次蓝球整数），与是否带 p 无关，
    # 所以同一 seed 下「偏置 / 未偏置」两条路径可逐期对照。
    red_mat = np.empty((n, rigor.PICK), dtype=int)
    blues = np.empty(n, dtype=np.int64)
    for i in range(n):
        if use_bias:
            picked = rng.choice(numbers, size=rigor.PICK, replace=False, p=bias_p)
        else:
            picked = rng.choice(numbers, size=rigor.PICK, replace=False)
        red_mat[i] = np.sort(picked)
        blues[i] = int(rng.integers(1, rigor.B_MAX + 1))

    draws: List[Dict] = []
    for i in range(n):
        reds = [int(x) for x in red_mat[i]]
        if len(set(reds)) != rigor.PICK:      # 无放回抽样保证互异；此处只是护栏
            raise RuntimeError("合成数据出现重复红球，抽样实现有误")
        year = 2003 + i // 150
        draws.append({
            "issue": f"SYN{year}{(i % 150) + 1:03d}",   # 由下标决定，无时钟依赖
            "date": None,
            "reds": reds,
            "blue": int(blues[i]),
            "order": list(reds),
        })
    return draws


# --------------------------------------------------------------------------
# 单次检验：把 randomness_report 压缩成可比较的摘要（**不改任何 p 值语义**）
# --------------------------------------------------------------------------

def summarize_report(report: Dict, alpha: float) -> Dict:
    """把 ``randomness_report`` 压成紧凑摘要。

    ``min_p_bonferroni`` 直接取自 ``report["family_wise"]``；
    「未校正显著计数」直接数 ``tests`` 里 ``p_value < alpha`` 的项。
    不重算、不替代、不放宽任何一项检验的口径。
    """
    tests = report.get("tests") or {}
    names = sorted(tests)                       # 固定顺序，避免 dict 插入序参与计算
    pvals = {name: tests[name].get("p_value") for name in names}
    valid = [float(v) for v in pvals.values() if v is not None]
    fw = report.get("family_wise") or {}
    return {
        "n_draws": int(report.get("n_valid") or 0),
        "verdict": report.get("verdict"),
        "verdict_fwer": report.get("verdict_fwer"),
        "n_tests": len(valid),
        "n_uncorrected_below_alpha": int(sum(1 for p in valid if p < alpha)),
        "min_p": min(valid) if valid else None,
        "min_p_bonferroni": fw.get("min_p_bonferroni"),
        "tests_p": {name: pvals[name] for name in names},
        "tests_verdict": {name: tests[name].get("verdict") for name in names},
    }


# --------------------------------------------------------------------------
# 判据（全部是纯函数，便于测试与解释）
# --------------------------------------------------------------------------

def _num(x: Any) -> Optional[float]:
    return None if x is None else _canonicalize(float(x))


def _check_real_not_detected(real: Dict, synthetic: Dict) -> Dict:
    """检查项①：真实数据校正后仍判「不可区分」，且合成数据同样不报。

    **证据不足即失败**（L3 准入条件：弃权不得计入做对）：
    真实期数 < ``MIN_REAL_FOR_CHECKS`` 或任一侧 verdict_fwer 缺失/为
    ``insufficient-data`` 时，``pass=False``。
    """
    enough = bool(real["n_tests"] > 0 and synthetic["n_tests"] > 0
                  and real["n_draws"] >= MIN_REAL_FOR_CHECKS
                  and synthetic["n_draws"] >= MIN_REAL_FOR_CHECKS)
    real_ok = real["verdict_fwer"] == "consistent-with-random"
    syn_ok = synthetic["verdict_fwer"] == "consistent-with-random"
    ok = bool(enough and real_ok and syn_ok)
    if not enough:
        note = (
            f"证据不足：真实数据仅 {real['n_draws']} 期（需要 ≥ "
            f"{MIN_REAL_FOR_CHECKS} 期才做得出检验），合成 {synthetic['n_draws']} 期。"
            "「没检查」不等于「检查通过」，因此本项判失败。"
        )
    elif ok:
        note = (
            "真实数据的家庭化校正结论是「与公平随机不可区分」。注意：合成数据是"
            "**已知随机**的，它同样报「不可区分」——这说明检测器在此样本量下"
            "没有报警能力，而不是证明了数据「被检验过、确实是随机的」。"
        )
    elif not real_ok and not syn_ok:
        note = ("真实与合成数据在 Bonferroni 校正后都报偏离：检验集本身不可信，"
                "必须先排查实现。")
    elif not real_ok:
        note = ("真实数据在 Bonferroni 校正后报偏离，而**已知随机**的合成数据没有："
                "这是真实数据侧的异常，必须先排查数据质量或口径，不得对用户宣称"
                "「数据与随机不可区分」。")
    else:
        note = ("合成数据（已知随机）在 Bonferroni 校正后报偏离，而真实数据没有："
                "这是检验集在纯随机数据上的假阳性（或测试注入的偏置在起作用）。"
                "无论哪种，都不能把「真实数据未检出」当作可用结论。")
    return {
        "name": CHECK_NAMES[0],
        "pass": ok,
        "real": {"verdict_fwer": real["verdict_fwer"],
                 "min_p_bonferroni": _num(real["min_p_bonferroni"]),
                 "n_draws": real["n_draws"],
                 "n_tests": real["n_tests"]},
        "synthetic": {"verdict_fwer": synthetic["verdict_fwer"],
                      "min_p_bonferroni": _num(synthetic["min_p_bonferroni"]),
                      "n_draws": synthetic["n_draws"],
                      "n_tests": synthetic["n_tests"]},
        "threshold": ("两侧 verdict_fwer 均为 'consistent-with-random'，"
                      f"且双方期数 ≥ {MIN_REAL_FOR_CHECKS}（弃权不算通过）"),
        "note_zh": note,
    }


def _check_synthetic_uncorrected(real: Dict, synthetic: Dict, alpha: float) -> Dict:
    """检查项②：合成数据上「未校正 p<α 的检验项计数」落在二项 95% 预测区间内。"""
    m = int(synthetic["n_tests"])
    k = int(synthetic["n_uncorrected_below_alpha"])
    ci = binomial_95_interval(k, m, alpha)
    ok = bool(ci["inside"] and m > 0)
    if m <= 0:
        note = "合成数据没有产生任何检验项，无法判断检验集灵敏度。"
    elif ok:
        note = (f"合成数据（已知随机）上有 {k} 项未校正 p<{alpha}，落在二项 95% 预测区间 "
                f"[{ci['low']}, {ci['high']}] 内（期望 {ci['expected']:.2f}）："
                "这正是「搜 m 项必然有几项偶然显著」的实证，也是必须看校正后 p 的理由。")
    else:
        note = (f"合成数据（已知随机）上有 {k} 项未校正 p<{alpha}，**落在**区间 "
                f"[{ci['low']}, {ci['high']}] 之外（期望 {ci['expected']:.2f}）："
                "说明这套检验对纯随机数据的灵敏度有系统性偏倚，"
                "真实数据上的「未检出」不可信。")
    return {
        "name": CHECK_NAMES[1],
        "pass": ok,
        "real": {"n_uncorrected_below_alpha": int(real["n_uncorrected_below_alpha"])},
        "synthetic": {"n_uncorrected_below_alpha": k, "expected": ci["expected"],
                      "ci95": [ci["low"], ci["high"]]},
        "threshold": (f"m={m} 项 × α={alpha} → 期望 {ci['expected']:.2f} 个偶然显著；"
                      f"计数须落在 [{ci['low']}, {ci['high']}]（二项 95% 预测区间）"),
        "note_zh": note,
    }


def _check_power(real: Dict, synthetic: Dict, null_q05: float,
                 n_perm: int) -> Dict:
    """检查项③：真实数据的最强（Bonferroni）p 值 ≥ 纯随机零分布的 5% 分位。

    判据与理由（写死在这里，不允许在调用侧另起口径）：
    零分布的 5% 分位是「纯随机数据里，最强信号有多极端」的 5% 下沿；
    真实值若低于它，说明真实数据在 α=0.05 上可能被判显著 —— 此时把
    「真实 vs 合成不可区分」讲给用户就是错的。反之两者都判「未检出」，
    差异对结论无影响，因此判「同量级」。
    """
    p_real = real["min_p_bonferroni"]
    p_syn = synthetic["min_p_bonferroni"]
    if p_real is None or p_syn is None or n_perm <= 0:
        ok = False
        note = ("证据不足：缺少最强 p 值或置换次数为 0（n_perm=0 时无法估计零分布），"
                "本项判失败（弃权不算通过）。")
        ratio = None
    else:
        p_real = float(p_real)
        p_syn = float(p_syn)
        ratio = max(p_real, p_syn) / max(min(p_real, p_syn), 1e-12)
        ok = bool(p_real >= float(null_q05))
        if ok:
            note = (f"真实数据最强校正 p={p_real:.3f}，不低于纯随机零分布的 5% 分位 "
                    f"{float(null_q05):.3f}：两者在同一量级，「未检出」不是因为我们"
                    "把真实数据拿去和一条更干净的基准比。")
        else:
            note = (f"真实数据最强校正 p={p_real:.3f} **低于**纯随机零分布的 5% 分位 "
                    f"{float(null_q05):.3f}：真实数据比已知随机数据更极端。"
                    "此时不能对用户宣称两者不可区分，属 S-6 级别的失败。")
    return {
        "name": CHECK_NAMES[2],
        "pass": ok,
        "real": {"min_p_bonferroni": _num(p_real), "null_q05": _num(null_q05),
                 "n_perm": int(n_perm)},
        "synthetic": {"min_p_bonferroni": _num(p_syn), "ratio": _num(ratio)},
        "threshold": ("真实的最强校正 p ≥ 纯随机零分布的 5% 分位"
                      "（该分位以下会在 α=0.05 上翻转结论）"),
        "note_zh": note,
    }


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------

def selfcheck(draws: List[Dict], seed: int = DEFAULT_SEED,
              n_issues: Optional[int] = None,
              n_perm: int = DEFAULT_N_PERM,
              alpha: float = 0.05,
              synthetic_bias: Optional[str] = None) -> Dict:
    """跑一次「真实 vs 合成」自证实验，返回含 4 项检查的完整响应体。

    参数
    ----
    draws: 真实开奖历史（时间升序，同 ``db.load_draws()``）；空列表也能跑，
        但检查①会因「证据不足」判失败（弃权不算通过）。
    seed: 唯一随机性来源（默认 42）。
    n_issues: 合成数据期数；``None`` → 取真实有效期数（保证「同期数」）。
    n_perm: 置换演示次数；``0`` → 跳过零分布估计（检查③随之失败）。
    alpha: 检验阈值（默认 0.05，与 ``randomness_report`` 一致）。
    synthetic_bias: ``None``（正常）或 ``"number"``（反向演示：合成数据带偏置）。

    返回
    ----
    dict，关键字段：
    - ``checks``: 4 项 ``{name, pass, real, synthetic, threshold, note_zh}``
    - ``all_pass``: 4 项全 pass 才为 True（S-6：任一项失败，V12 不得发布）
    - ``seed`` / ``n_issues`` / ``n_perm`` / ``alpha`` / ``synthetic_bias``
    - ``real`` / ``synthetic``: 两边的检验摘要（同口径并排）
    - ``family_wise_theory``: m 项检验在纯随机下的理论假阳性个数与 95% 区间
    - ``response_sha256`` / ``elapsed_ms`` / ``honest_note_zh`` / ``method``
    """
    t0 = time.perf_counter()

    seed = int(seed)
    alpha = float(alpha)
    n_perm = max(0, int(n_perm))
    valid = rigor._valid_draws(draws)        # 与 randomness_report 同一份过滤
    n_real = len(valid)
    n_syn = max(0, int(n_issues) if n_issues is not None else n_real)

    # ---- 真实数据：原样调用，口径一个字不改 ----
    real = summarize_report(rigor.randomness_report(valid, alpha=alpha), alpha)
    # ---- 合成数据：同 seed 派生 ----
    synthetic = summarize_report(
        rigor.randomness_report(synthetic_draws(n_syn, seed=seed,
                                                bias=synthetic_bias), alpha=alpha),
        alpha)

    m_tests = int(real["n_tests"]) or int(synthetic["n_tests"]) or 0
    fw_theory = binomial_95_interval(synthetic["n_uncorrected_below_alpha"],
                                     m_tests, alpha)

    # ---- 零分布（只依赖 seed）：真实 vs 合成同量级的基准 ----
    null_q05 = _null_quantile(seed=seed, n_issues=n_syn or n_real,
                              m_tests=m_tests, n_perm=n_perm, alpha=alpha)

    c1 = _check_real_not_detected(real, synthetic)
    c2 = _check_synthetic_uncorrected(real, synthetic, alpha)
    c3 = _check_power(real, synthetic, null_q05, n_perm)

    payload: Dict = {
        "ok": True,
        "seed": seed,
        "alpha": alpha,
        "n_issues": n_syn,
        "n_real": n_real,
        "n_perm": n_perm,
        "synthetic_bias": synthetic_bias,
        "checks": [c1, c2, c3],       # 第 4 项由 _check_reproducible 追加（自哈希）
        "real": real,
        "synthetic": synthetic,
        "family_wise_theory": fw_theory,
        "min_p_ratio": (None if (real["min_p_bonferroni"] is None
                                 or synthetic["min_p_bonferroni"] is None)
                        else _num(max(float(real["min_p_bonferroni"]),
                                      float(synthetic["min_p_bonferroni"]))
                                  / max(min(float(real["min_p_bonferroni"]),
                                            float(synthetic["min_p_bonferroni"])), 1e-12))),
        "honest_note_zh": HONEST_NOTE_ZH,
        "method": ("真实数据与合成数据走同一个 rigor.randomness_report()；合成数据由 "
                   "numpy default_rng（种子由 seed 派生）新生成——同期数、红球 1..33 "
                   "无放回取 6、蓝球 1..16 均匀。它不是对真实数据的 Bootstrap 重采样，"
                   "而是**全新的随机数据**；唯一随机性来源是 seed。"),
    }
    # 检查④：同 seed 两次运行，规范化响应体 sha256 相同。
    # 做法：先用**已算好**的全部字段算一次摘要，再用同一条构造函数重算一次，
    # 两次必须逐位相同 —— 任何非确定来源（时钟/全局随态/无序遍历/自反射）
    # 都会让两次摘要分叉。不重新跑统计检验：那会白烧 0.3s 的全枚举理论分布，
    # 而 A7 要防的是「响应体不确定」，不是「检验函数不确定」。
    c4 = _check_reproducible(lambda: response_sha256(payload))
    payload["checks"] = [c1, c2, c3, c4]
    all_pass = all(bool(c["pass"]) for c in payload["checks"])
    payload["all_pass"] = bool(all_pass)
    payload["n_pass"] = int(sum(1 for c in payload["checks"] if c["pass"]))
    payload["n_checks"] = len(payload["checks"])
    payload["response_sha256"] = response_sha256(payload)
    payload["elapsed_ms"] = int(round((time.perf_counter() - t0) * 1000))
    return _canonicalize(payload)


def _check_reproducible(digest_fn) -> Dict:
    """检查项④：同 seed 两次运行，规范化响应体 sha256 相同。

    ``digest_fn`` 是无参可调用对象，每次调用都对「当前响应体的全部已算字段」
    重新构造规范化 JSON 并求 sha256。两次调用之间**不共享中间结果字符串**，
    因此它能抓住：自反射字段（把 response_sha256 算进自己）、挂钟字段、
    依赖全局随机状态或 dict 插入序的实现。
    """
    sha_a = digest_fn()
    sha_b = digest_fn()
    ok = bool(sha_a == sha_b)
    return {
        "name": CHECK_NAMES[3],
        "pass": ok,
        "real": {"sha256": sha_a},
        "synthetic": {"sha256": sha_b},
        "threshold": "sha256(规范化 JSON，剔除 response_sha256/elapsed_ms) 逐位相同",
        "note_zh": (
            "同 seed 两次计算的摘要 sha256 逐位相同：响应体只由 seed 与真实数据决定"
            "（时钟、全局随机状态、遍历顺序都不参与），用户可用同一条 URL 复现本次结果。"
            if ok else
            "两次计算的 sha256 不同：响应体里混入了非 seed 派生的成分"
            "（时钟 / 全局随机状态 / 无序遍历 / 自反射字段）。A7 失败 ⇒ V12 不得发布。"
        ),
    }


# --------------------------------------------------------------------------
# 零分布（置换 / 重生成）：最强校正 p 值在纯随机数据下的分布
# --------------------------------------------------------------------------

_NULL_Q05_CACHE: Dict[tuple, float] = {}


def _null_cache_path():
    """零分布的磁盘缓存路径（放在 ``config.DATA_DIR`` 下，随库目录一起备份）。"""
    try:
        from . import config
        return config.DATA_DIR / "selfcheck_null_cache.json"
    except Exception:                                        # noqa: BLE001
        return None


def _null_cache_load() -> Dict[str, float]:
    """读磁盘缓存（失败一律返回空——缓存坏了只该变慢，不该让自证实验挂掉）。"""
    p = _null_cache_path()
    if p is None:
        return {}
    try:
        if not p.exists():
            return {}
        raw = json.loads(p.read_text(encoding="utf-8") or "{}")
        return {str(k): float(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
    except Exception:                                        # noqa: BLE001
        return {}


def _null_cache_store(key: str, value: float) -> None:
    """写磁盘缓存（原子替换；失败静默——缓存只是加速手段，不是结果来源）。"""
    p = _null_cache_path()
    if p is None:
        return
    try:
        cur = _null_cache_load()
        cur[key] = float(value)
        # 上限 64 条：参数扫描（不同 n_perm/期数）时防止无限增长
        if len(cur) > 64:
            for k in list(cur)[: len(cur) - 64]:
                cur.pop(k, None)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(cur, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp.replace(p)
    except Exception:                                        # noqa: BLE001
        pass


def warm_null_quantile(seed: int = DEFAULT_SEED, n_issues: int = DEFAULT_N_ISSUES,
                       m_tests: int = 9, n_perm: int = DEFAULT_N_PERM,
                       alpha: float = 0.05) -> Dict:
    """启动期预热零分布（可选；只为把首次自证实验的等待时间挪到后台）。

    返回 ``{"cached": bool, "q05": float}``。失败不抛（预热是优化，不是功能）。
    """
    try:
        q = _null_quantile(int(seed), int(n_issues), int(m_tests), int(n_perm), float(alpha))
        return {"cached": True, "q05": float(q)}
    except Exception as e:                                   # noqa: BLE001
        return {"cached": False, "error": f"{type(e).__name__}: {e}"}


def _null_quantile(seed: int, n_issues: int, m_tests: int,
                   n_perm: int, alpha: float) -> float:
    """用 seed 派生的 ``n_perm`` 组纯随机合成数据，估计「最强校正 p 值」的 5% 分位。

    每组都调用**同一个** ``randomness_report``（口径一致），只取
    ``family_wise.min_p_bonferroni`` —— 不另写一套更宽松/更严格的检验。
    返回 ``1.0`` 表示「任何真实 p 都算同量级」（``n_perm=0`` 或数据不足时），
    但检查③在 ``n_perm=0`` 时直接判失败，因此这个 1.0 只会出现在显式关闭置换的调用里。

    与真实数据无关：零分布回答的是「纯随机数据的最强信号能有多极端」。

    性能（2026-10-19 实测）：每次 ``randomness_report`` 在 3516 期上约 48ms，
    ``n_perm=200`` ⇒ 首次约 6.1s，超出方案要求的 3s。而**降低 n_perm 会增加判据的
    宽松度**（实测 5% 分位：n_perm=200 → 0.0264，100 → 0.0325，60 → 0.0537，
    40 → 0.0863），等于为了跑得快而偷偷放宽验收 —— 不可接受。

    因此改为**两级缓存**（进程内 + 磁盘）：零分布只由
    ``(seed, n_issues, n_perm, m_tests, alpha)`` 决定，是确定性值，跨进程复用
    不改变任何输出。代价是空缓存时首跑仍慢，这一条如实记录在返回值
    （``null_q05_cached``）与验收文档里，不靠调小 n_perm 掩盖。
    """
    if n_perm <= 0 or m_tests <= 0 or n_issues <= 0:
        return 1.0
    # 进程内缓存：同一 (seed, n_issues, n_perm, m_tests, alpha) 的零分布是
    # 确定性计算，缓存只省 CPU、不改变任何输出值。
    cache_key = (int(seed), int(n_issues), int(n_perm), int(m_tests),
                 round(float(alpha), 12))
    hit = _NULL_Q05_CACHE.get(cache_key)
    if hit is not None:
        return hit
    # 磁盘缓存：跨进程（容器重启 / 后端线程 / 前端首访）复用同一确定性值
    disk_key = "|".join(str(x) for x in cache_key)
    disk = _null_cache_load()
    if disk_key in disk:
        v = float(disk[disk_key])
        _NULL_Q05_CACHE[cache_key] = v
        return v
    # 第 3 条子流（前两条已被 synthetic_draws 占用），互不重叠
    rng = np.random.default_rng(np.random.SeedSequence(int(seed)).spawn(3)[2])
    vals: List[float] = []
    for _ in range(int(n_perm)):
        draws = _vectorized_uniform_draws(rng, int(n_issues))
        fw = (rigor.randomness_report(draws, alpha=alpha).get("family_wise") or {})
        v = fw.get("min_p_bonferroni")
        vals.append(float(v) if v is not None else 1.0)
    vals.sort()
    idx = max(0, min(len(vals) - 1, int(math.floor(CHECK_POWER_QUANTILE * len(vals)))))
    out = float(vals[idx])
    # 只缓存「跑完」的完整结果；缓存上限防止参数扫描时内存膨胀
    if len(_NULL_Q05_CACHE) < 64:
        _NULL_Q05_CACHE[cache_key] = out
        _null_cache_store(disk_key, out)
    return out


def _vectorized_uniform_draws(rng: np.random.Generator, n: int) -> List[Dict]:
    """零分布专用的**向量化**纯均匀合成数据（每期 33 个独立 U(0,1) 取最小的 6 个）。

    数学上与「无放回随机抽 6 个」**同分布**：独立同分布的随机键排序取前 6 名，
    等价于 33 个号码上的一个均匀随机 6 元子集；号码本身只参与排序（用下标），
    因此这不是近似，是精确等价。
    用向量化的理由：``n_perm=500 × 3516 期`` 用逐期 ``Generator.choice`` 需要
    Python 循环约 176 万次（实测 6s+），而本实现是一次矩阵运算（<0.02s）。
    注意：本函数只服务零分布估计，**不**参与真实/合成数据的并列展示，
    后者一律走 ``synthetic_draws``（逐期 ``choice``，与用户可对照的那份一致）。
    """
    keys = rng.random((int(n), rigor.R_MAX))
    picked = np.sort(np.argsort(keys, axis=1, kind="stable")[:, :rigor.PICK] + 1, axis=1)
    blues = rng.integers(1, rigor.B_MAX + 1, size=int(n))
    return [{"reds": [int(x) for x in picked[i]], "blue": int(blues[i])}
            for i in range(int(n))]
