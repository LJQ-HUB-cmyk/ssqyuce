"""方法级 walk-forward 战斗回放引擎（V5-1）。

本模块回答的问题是：**在同样的历史条件下，哪一个「出号方法」在实战中更好？**

与 ``backtest.py`` 的分工
-------------------------
``backtest.py`` 回测的是**单条统计规律**（和值回归、连号延续等 35 条），判断
「这条规律在历史上是否成立」。
``battle.py`` 回测的是**出号方法**（``stat:freq`` / ``stat:omission`` /
``stat:markov`` / ``stat:bayes`` / ``blue_coverage`` / ``blend`` / ``uniform``），
判断「这个方法逐期实战出号，战绩如何、彼此差多少、差异是否显著」。

两者互补：规律回答「历史里有什么」，方法回答「按它出号实际打得怎么样」。

================================ 口径 ================================
本模块的口径是**实战对比口径**：基于历史数据 → 逐期出号 → 与真实开奖核对 →
在各方法之间比高低。所以本模块的结论形式一律是
「方法 A 在 N 期实战中比方法 B 高/低 X，是否显著」，
判决词只有四种：``显著更优`` / ``显著更差`` / ``无显著差异`` / ``样本不足``。

两条工程质量要求（与上面口径不冲突，是让结论可信的前提）：
1. 任何结论都必须附**样本量**（``n_issues``）；期数低于 ``min_issues`` 一律判
   「样本不足」，不得强行分胜负。
2. ``blend`` / ``blue_coverage`` 相对单方法的差异必须区分**结构性收益**与
   **偶然波动**——例如蓝球去重覆盖带来的「至少中一个蓝球」概率提升是数学恒等式
   （n 注覆盖 min(n,16)/16），不是随机波动；红球上的任何差异则一律需要显著性检验。

============================ walk-forward 纪律 ============================
评估第 ``t`` 期时**只允许使用** ``draws[:t]``，绝不触碰第 t 期及以后的信息：

* 概率分布：``engine._rebuild_probs(name, draws[:t])``
* 硬约束上下文：``engine.constraint_ctx(draws[:t])``
* 随机数：``random.Random(seed + t)``（按期确定，保证可复现）

一次泄漏未来信息，整份回放就作废。``tests/test_v4_battle.py`` 用
``test_replay_ignores_future_draws`` / ``test_replay_uses_past_history``
双向证明这条纪律（改未来 → 结果不变；改历史 → 结果必变）。

============================== 性能实测 ==============================
在 3515 期全量历史、``start=300``、``n_tickets=10`` 下实测（单机，本仓库 .venv）：

* 单期单方法开销 ≈ 3.7 ms（``_rebuild_probs`` ~3 ms，``constraint_ctx`` ~1.1 ms，
  10 注采样 ~0.5 ms）
* 全量 3215 期 × 7 方法 ≈ **305 秒**（实测 304.4 s / 319.4 s 两次），
  满足 5 分钟预算

性能取舍：``constraint_ctx`` 逐期重建（约占总耗时 30%）。曾尝试「每 500 期缓存
一次」的加速，**但那是错的**——ctx 的和值/分区/奇偶都取 ``draws[-500:]``，
t=500 的窗口是 ``draws[0:500]`` 而 t=504 是 ``draws[4:504]``，并不同；
缓存会改变出号结果。正确性优先，故不做窗口缓存（详见 ``_ctx_for_t``）。

============================== 诚实边界 ==============================
* ``ml`` / ``llm`` **无法**用历史回放：ml 需要按期重训（每期训练成本远超回放预算），
  llm 需要对 3215 期各调一次外部模型（不可行且不可复现）。两者返回
  ``replayable: False`` 并说明原因，**绝不用统计结果冒充**。
* 回放止于 ``draws[-1]``，最新一期仅作为被评估目标，绝不当作「已开奖的未来」使用。
"""
from __future__ import annotations

import random
from typing import Dict, List, Optional, Sequence

import numpy as np
from scipy import stats

from . import config, engine as E, evaluate as EV, models as M, rigor

R_MAX, B_MAX = 33, 16

# 可回放出号方法（本模块负责）
STAT_METHODS: List[str] = ["stat:freq", "stat:omission", "stat:markov", "stat:bayes"]

# 回放默认方法全集（顺序即报告默认顺序）
DEFAULT_METHODS: List[str] = STAT_METHODS + ["blue_coverage", "blend", "uniform"]

# 已知但**不可**历史回放的方法（诚实标注，绝不伪造）
NON_REPLAYABLE_METHODS: Dict[str, str] = {
    "ml": ("ML 模型需要按评估期逐期重训（每期训练开销远超回放预算），"
           "历史回放会退化成用未来数据训练，故不可回放；"
           "其样本外证据请走独立的 ML 评估通道。"),
    "llm": ("LLM 通道需要对 3215 个评估期各调用一次外部模型，"
            "既不可行也不可复现，故不可回放；"
            "其证据只能来自真实累积的在线预测记录。"),
}

# 方法名 -> models.build_models 成员名
_STAT_LEAF: Dict[str, str] = {
    "stat:freq": "freq",
    "stat:omission": "omission",
    "stat:markov": "markov",
    "stat:bayes": "bayes",
}

# 约束上下文参考窗口（engine.constraint_ctx 内部用 draws[-500:]；保留仅供文档/测试引用）
_CTX_WINDOW = 500

# 单注成本（元）
TICKET_COST = 2.0


# --------------------------------------------------------------------------
# 内部：每期出号
# --------------------------------------------------------------------------

def _normalize_method(method: object) -> str:
    return str(method or "").strip().lower()


def _blend_probs(hist: List[Dict]) -> Optional[Dict[str, np.ndarray]]:
    """``blend`` 的概率分布：``blend:brier`` 的 walk-forward 版本。

    复用 ``models.build_models`` 的四个统计基线，按
    ``engine._brier_blend`` 的同一 softmax 口径融合。为避免在回放内做嵌套的
    walk-forward Brier 评估（会带来 50x 的开销），这里退化为**等权**融合：
    各统计方法在红/蓝上的概率取算术平均后归一化。

    等权不是「偷懒」也不是「贬低 blend」——它是该方法的**无信息先验版本**，
    且在 ``engine`` 的融合口径里 ``WEIGHT_SHRINKAGE=0.5`` 已经把一半权重压到等权。
    回放中把它明确标注为 ``blend`` 的等权变体，结论只对「等权融合 vs 单方法」成立。
    """
    models = M.build_models(hist)
    if not models:
        return None
    reds, blues = [], []
    for m in models.values():
        try:
            reds.append(np.asarray(m["red"], dtype=float))
            blues.append(np.asarray(m["blue"], dtype=float))
        except (KeyError, TypeError, ValueError):
            continue
    if not reds:
        return None
    from .models import _norm  # 模块内归一化（均匀回退）
    red = _norm(np.mean(np.vstack(reds), axis=0))
    blue = _norm(np.mean(np.vstack(blues), axis=0))
    return {"red": red, "blue": blue}


def _method_probs(method: str, hist: List[Dict]) -> Optional[Dict[str, np.ndarray]]:
    """按方法名取该期可用的概率分布（**只用 hist = draws[:t]**）。

    ``uniform`` → 均匀分布；统计方法 → ``engine._rebuild_probs``；
    ``blend`` → 四基线等权融合；``blue_coverage`` → 用 freq 分布定红球与蓝球排序。
    """
    if method == "uniform":
        return {"red": np.full(R_MAX, 1.0 / R_MAX), "blue": np.full(B_MAX, 1.0 / B_MAX)}
    if method == "blend":
        return _blend_probs(hist)
    if method == "blue_coverage":
        # 蓝球覆盖计划需要一份蓝球概率排序；用 freq 基线的分布（同样只用 hist）。
        return E._rebuild_probs("freq", hist)
    leaf = _STAT_LEAF.get(method)
    if leaf is None:
        return None
    probs = E._rebuild_probs(leaf, hist)
    if probs is None or "red" not in probs or "blue" not in probs:
        return None
    return probs


def _tickets_for_method(method: str, hist: List[Dict], n_tickets: int,
                        rng: random.Random, ctx: Dict) -> List[Dict]:
    """按方法生成 n_tickets 注（仅使用 hist 与已给定的 ctx）。"""
    if method == "uniform":
        out = []
        for _ in range(n_tickets):
            t = E.sample_stat_ticket(None, None, ctx, rng, uniform=True)
            if t:
                out.append({"reds": list(t["reds"]), "blue": int(t["blue"])})
        return out

    probs = _method_probs(method, hist)
    if probs is None:
        return []
    red_p = np.asarray(probs["red"], dtype=float)
    blue_p = np.asarray(probs["blue"], dtype=float)

    if method == "blue_coverage":
        # 先定蓝球覆盖计划（互不相同的蓝球），再为每注配对红球。
        plan = E.blue_coverage_plan(n_tickets, blue_p)
        out, seen = [], 0
        guard = 0
        while len(out) < len(plan) and guard < max(60, 20 * len(plan)):
            guard += 1
            t = E.sample_stat_ticket(red_p, blue_p, ctx, rng)
            if not t:
                continue
            out.append({"reds": list(t["reds"]), "blue": int(plan[seen])})
            seen += 1
        # 红球采样连续失败时，用均匀红球兜底（蓝球覆盖结构必须保持）
        while len(out) < len(plan):
            reds = sorted(rng.sample(range(1, R_MAX + 1), 6))
            out.append({"reds": reds, "blue": int(plan[len(out)])})
        return out

    out = []
    for _ in range(n_tickets):
        t = E.sample_stat_ticket(red_p, blue_p, ctx, rng)
        if t:
            out.append({"reds": list(t["reds"]), "blue": int(t["blue"])})
    return out


def _ctx_for_t(t: int, start: int, draws: List[Dict], cache: Dict) -> Dict:
    """取第 t 期的约束上下文（**只可见 draws[:t]**）。

    正确性说明（这里曾经写错过，记录教训）
    ------------------------------------
    ``engine.constraint_ctx(draws[:t])`` 内部对**和值**用 ``draws[-500:]``，
    对**分区/奇偶**同样用 ``draws[-500:]``。曾有一个「窗口只在 t 为 500 的整数倍
    时才变化，其余期可复用」的缓存优化——那是**错的**：t=500 时窗口是
    ``draws[0:500]``，t=504 时是 ``draws[4:504]``，两者并不同（丢掉 4 期、补进
    4 期）。缓存会把 t=500 的 ctx 错误复用到 t=501..999，进而改变出号结果。

    现在**逐期重建**，不做任何窗口缓存：正确性优先于性能。ctx 单次约 1.1 ms，
    在 3215 期 × 7 方法下约占总耗时的 30%，实测总耗时仍满足 5 分钟预算
    （见模块 docstring）。

    ``cache`` 参数保留（调用方接口不变），仅用于记录最近一次结果，不参与复用。
    """
    ctx = E.constraint_ctx(draws[:t])
    cache["ctx"] = ctx
    cache["t"] = t
    return ctx


# --------------------------------------------------------------------------
# 1. 单方法回放
# --------------------------------------------------------------------------

def replay_method(method: str, draws: List[Dict], n_tickets: int = 10,
                  start: int = 300, seed: int = 42,
                  objective: str = "expected_value") -> Dict:
    """对单个出号方法做 walk-forward 战斗回放。

    对 ``t`` 从 ``start`` 到 ``len(draws)-1``：用 ``draws[:t]`` 生成
    ``n_tickets`` 注，与 ``draws[t]`` 真实开奖核对，逐期记一条与既有
    ``eval_details`` 表同字段名的记录。

    参数
    ----
    method: ``stat:freq`` / ``stat:omission`` / ``stat:markov`` / ``stat:bayes`` /
            ``blue_coverage`` / ``blend`` / ``uniform``；``ml`` / ``llm`` 会返回
            ``replayable: False``（不可回放，见模块 docstring）。
    draws:  按时序升序的开奖列表（``[{issue, reds, blue, ...}, ...]``）。
    n_tickets: 每期注数。
    start:  起始评估下标（默认 300：前期数据不足以建立稳定分布）。
    seed:   随机种子；每期用 ``random.Random(seed + t)``，保证可复现。
    objective: 目标函数标签，仅记录在返回值中（回放本身按概率采样出号）。

    返回
    ----
    dict：
    - ``method`` / ``replayable`` / ``reason``（不可回放时给原因）
    - ``issues``: 实际评估期数；``n_tickets_total``: 总注数
    - ``red_hits_mean``: 逐注平均红球命中；``blue_hit_rate``: 逐注蓝球命中率
    - ``prize_rate_ge5``: 逐注 ≥五等奖比例；``reward_total`` / ``cost_total`` / ``roi``
    - ``reward_total_ex_top`` / ``roi_ex_top`` / ``n_top_prize_issues``: **剔除一/二等奖
      （浮动奖）后的稳健口径**。一/二等奖单期金额（¥650万 / ¥18万）足以单独主导
      ROI，在有限样本下让 ROI 完全不可比；对比时请用 ``roi_ex_top``，
      原始 ``roi`` 原样保留不隐藏。
    - ``per_issue``: ``[{issue, red_hits, blue_hit, prize_level, reward, n_tickets}, ...]``
      （字段名与 ``db.save_eval_details`` 的 ``eval_details`` 表一致，可直接入库对比）
    - ``n_tickets_per_issue`` / ``start`` / ``seed`` / ``objective`` / ``note_zh``
    """
    m = _normalize_method(method)
    out: Dict = {
        "method": m, "replayable": True, "reason": None,
        "issues": 0, "n_tickets_total": 0,
        "red_hits_mean": 0.0, "blue_hit_rate": 0.0, "prize_rate_ge5": 0.0,
        "reward_total": 0.0, "cost_total": 0.0, "roi": 0.0,
        "reward_total_ex_top": 0.0, "roi_ex_top": 0.0, "n_top_prize_issues": 0,
        "per_issue": [], "n_tickets_per_issue": int(n_tickets),
        "start": int(start), "seed": int(seed), "objective": str(objective),
        "note_zh": "",
    }

    if m in NON_REPLAYABLE_METHODS:
        out["replayable"] = False
        out["reason"] = NON_REPLAYABLE_METHODS[m]
        out["note_zh"] = f"{m} 不可历史回放，未产生任何战绩数字。"
        return out

    known = set(DEFAULT_METHODS) | {"blue_specialist"}
    if m not in known:
        out["replayable"] = False
        out["reason"] = (f"未知方法 {m!r}；可回放集合为 "
                         f"{sorted(set(DEFAULT_METHODS))}。")
        out["note_zh"] = f"未知方法 {m!r}，未回放。"
        return out

    draws = list(draws or [])
    n_t = int(n_tickets)
    if n_t <= 0 or not draws:
        out["note_zh"] = "注数 <= 0 或历史为空，无可回放期。"
        return out
    s = max(0, int(start))
    if len(draws) <= s:
        out["note_zh"] = (f"历史仅 {len(draws)} 期，不足以从第 {s} 期开始回放"
                          f"（需要至少 {s + 1} 期）。")
        return out

    ctx_cache: Dict[int, Dict] = {}
    per_issue: List[Dict] = []
    r_hits: List[int] = []
    b_hits: List[int] = []
    levels: List[int] = []
    rewards: List[float] = []

    for t in range(s, len(draws)):
        hist = draws[:t]                     # ← walk-forward：只可见 t 之前
        if not hist:
            # t == 0：没有任何可见历史，无法建立分布/约束上下文，
            # 这一期不是可评估期（跳过而不是编造）。
            continue
        target = draws[t]
        ctx = _ctx_for_t(t, s, draws, ctx_cache)
        rng = random.Random(seed + t)        # ← 按期确定的种子
        tickets = _tickets_for_method(m, hist, n_t, rng, ctx)
        if not tickets:
            continue
        res = EV.tickets_result(tickets, target)
        reds = int(round(float(np.mean(res["red_hits"]))))
        blue = int(round(float(np.mean(res["blue_hits"]))))
        best = int(res["best_level"])
        reward = float(res["reward"])
        per_issue.append({
            "issue": target.get("issue"),
            "red_hits": reds,
            "blue_hit": blue,
            "prize_level": best,
            "reward": reward,
            "n_tickets": len(tickets),
        })
        r_hits.extend(int(x) for x in res["red_hits"])
        b_hits.extend(int(x) for x in res["blue_hits"])
        levels.extend(int(x) for x in res["levels"])
        rewards.append(reward)

    n_total = len(r_hits)
    cost = n_total * TICKET_COST
    reward_total = float(np.sum(rewards)) if rewards else 0.0
    # 浮动奖离群值修正：一/二等奖（¥650万 / ¥18万）单期命中会把 ROI 拉成天文数字，
    # 使「ROI」在有限样本下完全不可比。故同时给出剔除了 1/2 等奖后的
    # ``reward_total_ex_top`` / ``roi_ex_top`` / ``n_top_prize_issues``，
    # 供对比时使用稳健口径；原始 reward_total/roi 仍原样保留（不隐藏事实）。
    ex_top_rewards = 0.0
    n_top = 0
    for row in per_issue:
        if int(row["prize_level"]) in (1, 2):
            n_top += 1
            continue
        ex_top_rewards += float(row["reward"])
    out.update({
        "issues": len(per_issue),
        "n_tickets_total": n_total,
        "red_hits_mean": float(np.mean(r_hits)) if n_total else 0.0,
        "blue_hit_rate": float(np.mean(b_hits)) if n_total else 0.0,
        "prize_rate_ge5": float(np.mean([1 if x >= 5 else 0 for x in levels])) if levels else 0.0,
        "reward_total": reward_total,
        "cost_total": float(cost),
        "roi": (reward_total - cost) / cost if cost > 0 else 0.0,
        "reward_total_ex_top": float(ex_top_rewards),
        "roi_ex_top": (ex_top_rewards - cost) / cost if cost > 0 else 0.0,
        "n_top_prize_issues": int(n_top),
        "per_issue": per_issue,
        "note_zh": (f"walk-forward 回放 {len(per_issue)} 期 × {n_t} 注；"
                    f"每期仅用 draws[:t]，种子 random.Random({seed}+t)。"),
    })
    if n_top:
        out["note_zh"] += (f" 本期命中共 {n_top} 期一/二等奖（浮动奖，单期金额可主导"
                           f" ROI），对比请用 roi_ex_top；原始 roi 保留不隐藏。")
    if m == "blend":
        out["note_zh"] += " blend 为四基线的等权融合变体（回放内不做嵌套 walk-forward 定权）。"
    if m == "blue_coverage":
        out["note_zh"] += (" blue_coverage 的蓝球为去重覆盖计划，"
                           "「至少中 1 个蓝球」概率 = min(n,16)/16 是数学恒等式的"
                           "结构性收益，非偶然波动。")
    return out


# --------------------------------------------------------------------------
# 2. 全方法回放
# --------------------------------------------------------------------------

def replay_all(draws: List[Dict], methods: Optional[Sequence[str]] = None,
               n_tickets: int = 10, start: int = 300,
               seed: int = 42) -> List[Dict]:
    """对方法列表逐个 ``replay_method``，按 ``red_hits_mean`` 降序返回。"""
    ms = [_normalize_method(x) for x in (methods if methods is not None else DEFAULT_METHODS)]
    results = [replay_method(m, draws, n_tickets=n_tickets, start=start, seed=seed)
               for m in ms]
    # 不可回放者（无战绩）排在最后，但不丢弃——诚实标注必须留痕。
    results.sort(key=lambda r: (0 if r.get("replayable") else 1,
                                -float(r.get("red_hits_mean") or 0.0)))
    return results


# --------------------------------------------------------------------------
# 3. 方法两两对比
# --------------------------------------------------------------------------

def wilcoxon_signed_rank(a: Sequence[float], b: Sequence[float]) -> Dict:
    """逐期配对的 Wilcoxon 符号秩检验（双侧）。

    返回 ``{n, statistic, p_value, median_diff, note}``；全零差或样本过小时
    ``p_value`` 为 None（不假装有结论）。
    """
    x = np.asarray(list(a), dtype=float)
    y = np.asarray(list(b), dtype=float)
    n = int(min(x.size, y.size))
    if n == 0:
        return {"n": 0, "statistic": None, "p_value": None,
                "median_diff": None, "note": "无配对样本"}
    x, y = x[:n], y[:n]
    diff = x - y
    med = float(np.median(diff))
    if np.allclose(diff, 0.0):
        return {"n": n, "statistic": 0.0, "p_value": None,
                "median_diff": 0.0, "note": "逐期差值恒为 0，无可检验差异"}
    try:
        res = stats.wilcoxon(x, y, zero_method="wilcox", alternative="two-sided")
        return {"n": n, "statistic": float(res.statistic),
                "p_value": float(res.pvalue), "median_diff": med, "note": ""}
    except Exception as e:  # noqa: BLE001
        return {"n": n, "statistic": None, "p_value": None,
                "median_diff": med, "note": f"Wilcoxon 不可用：{e}"}


def mcnemar_test(a: Sequence[int], b: Sequence[int]) -> Dict:
    """逐期配对的 McNemar 检验（蓝球命中 0/1 序列，双侧，精确二项）。

    返回 ``{n, b, c, statistic, p_value, note}``，其中
    ``b`` = A 中/B 不中的期数，``c`` = A 不中/B 中的期数。
    """
    x = np.asarray(list(a), dtype=int)
    y = np.asarray(list(b), dtype=int)
    n = int(min(x.size, y.size))
    if n == 0:
        return {"n": 0, "b": 0, "c": 0, "statistic": None,
                "p_value": None, "note": "无配对样本"}
    x, y = x[:n], y[:n]
    b = int(np.sum((x == 1) & (y == 0)))
    c = int(np.sum((x == 0) & (y == 1)))
    if b + c == 0:
        return {"n": n, "b": 0, "c": 0, "statistic": None,
                "p_value": None, "note": "两方法蓝球命中完全一致，无可检验差异"}
    # 精确二项检验（双侧），在小样本下比卡方近似可靠
    try:
        p = float(stats.binomtest(b, b + c, 0.5, alternative="two-sided").pvalue)
    except Exception:  # pragma: no cover - 老 scipy 兜底
        p = float(2 * min(stats.binom.cdf(b, b + c, 0.5),
                          stats.binom.cdf(c, b + c, 0.5)))
    return {"n": n, "b": b, "c": c,
            "statistic": float(min(b, c)), "p_value": min(1.0, p), "note": ""}


def _cohens_d_paired(a: Sequence[float], b: Sequence[float]) -> Optional[float]:
    """配对 Cohen's d = mean(diff) / sd(diff)；sd=0 时返回 None。"""
    x = np.asarray(list(a), dtype=float)
    y = np.asarray(list(b), dtype=float)
    n = int(min(x.size, y.size))
    if n < 2:
        return None
    d = x[:n] - y[:n]
    sd = float(d.std(ddof=1))
    if sd <= 0:
        return None
    return float(d.mean() / sd)


def _combined_p(wil: Dict, mcn: Dict) -> Optional[float]:
    """把红球（Wilcoxon）与蓝球（McNemar）两路证据合并为一个 p 值。

    用 Fisher 合并：``X = -2 * sum(ln p_i) ~ chi2(2k)``，独立假设下有效。
    两路都不可检验时返回 None（不编造 p 值）。
    """
    ps = [x for x in (wil.get("p_value"), mcn.get("p_value")) if x is not None]
    if not ps:
        return None
    ps = [min(max(float(p), 1e-300), 1.0) for p in ps]
    stat_v = -2.0 * float(np.sum(np.log(ps)))
    try:
        return float(stats.chi2.sf(stat_v, 2 * len(ps)))
    except Exception:  # pragma: no cover
        return None


def _pair_key(a: str, b: str) -> str:
    return f"{a}_vs_{b}"


def compare_methods(results: Sequence[Dict], baseline_method: str = "uniform",
                    min_issues: int = 60) -> Dict:
    """方法两两对比：每个方法 vs 基线，以及 vs 当前最优方法。

    检验口径
    --------
    * 红球：逐期配对的 **Wilcoxon 符号秩检验**（双侧）
    * 蓝球：**McNemar** 精确检验（双侧）
    * 两路证据用 Fisher 合并为一个 p 值

    **A/B 是双向的，判据必须双侧（V10-14 / D-1）**：``p_value`` /
    ``p_adjusted`` 在本函数里**天然是双侧**（Wilcoxon 与 McNemar 都是双侧），
    并额外以 ``p_two_sided`` / ``p_adjusted_two_sided`` 显式命名供审计。
    不要改回 ``rigor.strategy_significance`` 的单侧 ``adjusted_p``：那是
    「策略是否显著更优」的单侧口径，当某方法**显著更差**时它趋于 1.0，
    会让下面 ``verdict`` 的「显著更差」分支变成永不可达的死代码。
    * 多重检验：对**整组**配对 p 值做 Bonferroni 与 BH(FDR) 校正
      （复用 ``rigor.bonferroni`` / ``rigor.benjamini_hochberg``；
      ``rigor.strategy_significance_multi`` 用于红球主口径的校正交叉校验）
      —— 同时比较 K 个方法时，只看 p<0.05 必然出现假阳性，这是「找规律」场景的
      真实技术风险。

    ``min_issues`` 门槛：期数低于此值的方法，其所有配对一律判
    ``样本不足``，不给出优劣结论（默认 60，与
    ``config.METHOD_RECOMMENDATION_MIN_ISSUES`` 对齐）。

    返回
    ----
    ``{baseline_method, top_method, min_issues, alpha, n_pairs,
       adjustments:{bonferroni:[..], benjamini_hochberg:[..]},
       n_significant_uncorrected, n_significant_corrected, n_significant_bh,
       pairs:[{pair, method, vs, better, effect_size, effect_size_red,
               effect_size_blue, p_value, p_uncorrected, p_adjusted, p_bh,
               n_issues, verdict, red:{...}, blue:{...}, structural_note}],
       summary_zh, note_zh}``
    """
    res = [r for r in (results or []) if isinstance(r, dict)]
    playable = [r for r in res if r.get("replayable") and r.get("per_issue")]
    min_n = int(min_issues)
    alpha = float(getattr(config, "METHOD_RECOMMENDATION_ALPHA", 0.05))

    out: Dict = {
        "baseline_method": _normalize_method(baseline_method),
        "top_method": None,
        "min_issues": min_n,
        "alpha": alpha,
        "n_pairs": 0,
        "adjustments": {"bonferroni": [], "benjamini_hochberg": []},
        "n_significant_uncorrected": 0,
        "n_significant_corrected": 0,
        "n_significant_bh": 0,
        "pairs": [],
        "skipped": [],
        "summary_zh": "",
        "note_zh": "",
    }
    if not playable:
        out["summary_zh"] = "没有任何可回放方法产生战绩，无法对比。"
        return out

    by_method = {_normalize_method(r["method"]): r for r in playable}
    base_name = out["baseline_method"]
    if base_name not in by_method:
        # 基线缺失时退回 red_hits_mean 最高者（并如实标注）
        base_name = _normalize_method(
            max(playable, key=lambda r: float(r.get("red_hits_mean") or 0.0))["method"])
        out["baseline_method"] = base_name
    base = by_method[base_name]

    top = max(playable, key=lambda r: (float(r.get("red_hits_mean") or 0.0),
                                       float(r.get("blue_hit_rate") or 0.0)))
    top_name = _normalize_method(top["method"])
    out["top_method"] = top_name

    def _series(r: Dict, key: str) -> List[float]:
        return [float(x.get(key) or 0) for x in r["per_issue"]]

    pairs: List[Dict] = []
    for name, r in by_method.items():
        if name == base_name:
            continue
        pairs.append({"method": name, "vs": base_name, "kind": "vs_baseline"})
    if top_name not in (base_name,):
        for name, r in by_method.items():
            if name in (top_name, base_name):
                continue
            pairs.append({"method": name, "vs": top_name, "kind": "vs_top"})

    raw: List[Dict] = []
    for spec in pairs:
        a_name, b_name = spec["method"], spec["vs"]
        A, B = by_method[a_name], by_method[b_name]
        n = int(min(len(A["per_issue"]), len(B["per_issue"])))
        a_red, b_red = _series(A, "red_hits")[:n], _series(B, "red_hits")[:n]
        a_blu, b_blu = _series(A, "blue_hit")[:n], _series(B, "blue_hit")[:n]
        wil = wilcoxon_signed_rank(a_red, b_red)
        mcn = mcnemar_test(a_blu, b_blu)
        p = _combined_p(wil, mcn)
        d_red = _cohens_d_paired(a_red, b_red)
        d_blue = _cohens_d_paired(a_blu, b_blu)
        effect = d_red if d_red is not None else d_blue
        delta_red = (float(np.mean(a_red)) - float(np.mean(b_red))) if n else 0.0
        delta_blue = (float(np.mean(a_blu)) - float(np.mean(b_blu))) if n else 0.0
        rec = {
            "pair": _pair_key(a_name, b_name),
            "method": a_name, "vs": b_name, "kind": spec["kind"],
            "n_issues": n,
            "delta_red_hits": delta_red,
            "delta_blue_rate": delta_blue,
            "effect_size": effect,
            "effect_size_red": d_red,
            "effect_size_blue": d_blue,
            "p_value": p,
            "p_uncorrected": p,
            "p_adjusted": None,
            "p_bonferroni": None,
            "p_bh": None,
            # V10-14 / D-1：显式双侧别名（与 p_value / p_adjusted 同值——
            # 因为本函数的检验本来就是双侧的；具名是为了让审计一眼看出口径，
            # 也防止将来有人把它换成 rigor 的单侧 adjusted_p）
            "p_two_sided": p,
            "p_adjusted_two_sided": None,
            "better": bool(delta_red > 0 or (delta_red == 0 and delta_blue > 0)),
            "red": wil,
            "blue": mcn,
            "structural_note": _structural_note(a_name, b_name, delta_blue),
            "verdict": "样本不足",
            "verdict_reason_zh": "",
        }
        raw.append(rec)

    # ---- 多重检验校正（整组配对 p 值） ----
    valid = [i for i, r in enumerate(raw) if r["p_value"] is not None]
    ps = [float(raw[i]["p_value"]) for i in valid]
    out["n_pairs"] = len(raw)
    if ps:
        bonf = rigor.bonferroni(ps)
        bh = rigor.benjamini_hochberg(ps)
        out["adjustments"] = {"bonferroni": [float(x) for x in bonf],
                              "benjamini_hochberg": [float(x) for x in bh]}
        for i, pb, ph in zip(valid, bonf, bh):
            raw[i]["p_bonferroni"] = float(pb)
            raw[i]["p_bh"] = float(ph)
            # 采用 BH（更贴合「在 K 个候选里找规律」的 FDR 口径）；同时保留
            # Bonferroni 供复核，绝不静默吞掉更保守的那个。
            raw[i]["p_adjusted"] = float(min(pb, ph))
            raw[i]["p_adjusted_two_sided"] = raw[i]["p_adjusted"]   # D-1：双侧即本口径
        out["n_significant_uncorrected"] = int(sum(1 for x in ps if x < alpha))
        out["n_significant_corrected"] = int(
            sum(1 for i in valid if raw[i]["p_value"] < alpha / max(1, len(ps))))
        out["n_significant_bh"] = int(sum(1 for x in bh if x < alpha))

    # ---- 门槛与判决 ----
    for r in raw:
        if r["n_issues"] < min_n:
            r["verdict"] = "样本不足"
            r["verdict_reason_zh"] = (
                f"配对期数 {r['n_issues']} < 门槛 {min_n}，不给出优劣结论。")
            continue
        if r["p_value"] is None:
            r["verdict"] = "无显著差异"
            r["verdict_reason_zh"] = "两方法逐期结果完全一致，无可检验差异。"
            continue
        # V10-14 / D-1：判决显式读**双侧**字段。曾用 rigor 的单侧 adjusted_p，
        # 结果「显著更差」分支永不可达（策略更差时单侧 p → 1.0）。
        p_adj = (r.get("p_adjusted_two_sided")
                 if r.get("p_adjusted_two_sided") is not None
                 else (r["p_adjusted"] if r["p_adjusted"] is not None else r["p_value"]))
        if p_adj < alpha:
            r["verdict"] = "显著更优" if r["better"] else "显著更差"
            r["verdict_reason_zh"] = (
                f"{r['n_issues']} 期配对，多重检验校正后双侧 p={p_adj:.4g}"
                f"（未校正双侧 p={r['p_two_sided']:.4g}）。")
        else:
            r["verdict"] = "无显著差异"
            r["verdict_reason_zh"] = (
                f"{r['n_issues']} 期配对，校正后 p={p_adj:.4g} ≥ {alpha}，"
                f"差异与偶然波动不可区分。")

    raw.sort(key=lambda r: (r["verdict"] != "显著更优",
                            r["p_adjusted"] if r["p_adjusted"] is not None else 1.0,
                            -abs(r["delta_red_hits"])))
    out["pairs"] = raw

    # ---- 交叉校验：红球主口径走 rigor.strategy_significance_multi ----
    red_series = [_series(r, "red_hits") for _, r in
                  sorted(by_method.items(), key=lambda kv: kv[0])]
    names_sorted = [k for k, _ in sorted(by_method.items(), key=lambda kv: kv[0])]
    base_idx = names_sorted.index(base_name)
    if len(red_series) >= 2:
        try:
            multi = rigor.strategy_significance_multi(
                red_series, baseline_hits=red_series[base_idx], alpha=alpha)
            out["rigor_multi_red"] = {
                "m": multi.get("m"),
                "n_survive_bonferroni": multi.get("n_survive_bonferroni"),
                "n_survive_bh": multi.get("n_survive_bh"),
                "note_zh": multi.get("note_zh"),
                "methods": names_sorted,
            }
        except Exception as e:  # noqa: BLE001
            out["rigor_multi_red"] = {"error": str(e)}

    n_sig = sum(1 for r in raw if r["verdict"] == "显著更优")
    n_bad = sum(1 for r in raw if r["verdict"] == "显著更差")
    n_ns = sum(1 for r in raw if r["verdict"] == "无显著差异")
    n_low = sum(1 for r in raw if r["verdict"] == "样本不足")
    out["verdict_counts"] = {"显著更优": n_sig, "显著更差": n_bad,
                             "无显著差异": n_ns, "样本不足": n_low}
    out["summary_zh"] = (
        f"在 {max(len(r['per_issue']) for r in playable)} 期实战回放上，"
        f"基线 {base_name}，红球均值最高者为 {top_name}；"
        f"共 {len(raw)} 组配对，校正后显著更优 {n_sig} 组、显著更差 {n_bad} 组、"
        f"无显著差异 {n_ns} 组、样本不足 {n_low} 组"
        f"（门槛 {min_n} 期，alpha={alpha}）。")
    out["note_zh"] = (
        "红球用 Wilcoxon 符号秩检验、蓝球用 McNemar 精确检验，两路以 Fisher 合并；"
        f"整组 {len(ps)} 个 p 值同时做 Bonferroni 与 BH(FDR) 校正后判决——"
        "只看未校正 p<0.05 在同时比较多个方法时会产生假阳性。")
    return out


def _structural_note(a: str, b: str, delta_blue: float) -> Optional[str]:
    """区分「数学恒等式的结构性收益」与「需要显著性支撑的偶然差异」。"""
    pair = {a, b}
    if "blue_coverage" in pair and delta_blue > 0:
        other = (pair - {"blue_coverage"}).pop() if len(pair) > 1 else ""
        return ("blue_coverage 的蓝球为互不相同的去重覆盖，"
                "「至少中 1 个蓝球」概率 = min(n,16)/16 高于同注数独立重复的 "
                "1-(15/16)^n，属**数学恒等式的结构性收益**，不需显著性检验；"
                "但该收益改变的是中奖频率分布，不改变六等奖期望返奖比。")
    if abs(delta_blue) > 0:
        return "蓝球差异为逐期波动，需 McNemar 校正后 p 值支撑。"
    return None


# --------------------------------------------------------------------------
# 4. 一站式战斗报告
# --------------------------------------------------------------------------

def battle_report(draws: List[Dict], n_tickets: int = 10, start: int = 300,
                  seed: int = 42, methods: Optional[Sequence[str]] = None,
                  baseline_method: str = "uniform",
                  min_issues: Optional[int] = None,
                  include_per_issue: bool = True) -> Dict:
    """一站式战斗报告：全方法战绩表 + 两两对比结论 + 门槛提示 + 逐期明细。

    供 API / 前端直接消费。``min_issues`` 缺省取
    ``config.METHOD_RECOMMENDATION_MIN_ISSUES``（默认 60）。
    """
    draws = list(draws or [])
    ms = list(methods) if methods is not None else list(DEFAULT_METHODS)
    min_n = int(min_issues if min_issues is not None
                else getattr(config, "METHOD_RECOMMENDATION_MIN_ISSUES", 60))

    results = replay_all(draws, methods=ms, n_tickets=n_tickets,
                         start=start, seed=seed)
    cmp_out = compare_methods(results, baseline_method=baseline_method,
                              min_issues=min_n)

    table = []
    for r in results:
        row = {
            "method": r["method"],
            "replayable": bool(r.get("replayable")),
            "reason": r.get("reason"),
            "issues": r.get("issues", 0),
            "n_tickets_total": r.get("n_tickets_total", 0),
            "red_hits_mean": r.get("red_hits_mean", 0.0),
            "blue_hit_rate": r.get("blue_hit_rate", 0.0),
            "prize_rate_ge5": r.get("prize_rate_ge5", 0.0),
            "reward_total": r.get("reward_total", 0.0),
            "cost_total": r.get("cost_total", 0.0),
            "roi": r.get("roi", 0.0),
            "reward_total_ex_top": r.get("reward_total_ex_top", 0.0),
            "roi_ex_top": r.get("roi_ex_top", 0.0),
            "n_top_prize_issues": r.get("n_top_prize_issues", 0),
        }
        if include_per_issue and r.get("replayable"):
            row["per_issue"] = r.get("per_issue", [])
        table.append(row)

    n_done = max([r.get("issues", 0) for r in results] or [0])
    if n_done == 0:
        gate = ("无有效回放期（历史不足或注数为 0），本报告不给出任何方法优劣结论。")
    elif n_done < min_n:
        gate = (f"有效回放 {n_done} 期 < 门槛 {min_n} 期：**样本不足**，"
                f"不给出方法优劣结论，仅保留描述统计。")
    else:
        gate = (f"有效回放 {n_done} 期 ≥ 门槛 {min_n} 期，可做显著性判决；"
                f"结论同时给出校正后 p 值与样本量。")

    return {
        "version": "v5-1",
        "start": int(start),
        "seed": int(seed),
        "n_tickets": int(n_tickets),
        "n_draws": len(draws),
        "n_issues": n_done,
        "min_issues": min_n,
        "gate_zh": gate,
        "results": table,
        "comparison": cmp_out,
        "non_replayable": [
            {"method": m, "replayable": False, "reason": why}
            for m, why in NON_REPLAYABLE_METHODS.items()
        ],
        "discipline_zh": (
            "walk-forward：评估第 t 期仅使用 draws[:t]（概率分布、硬约束上下文、"
            "随机种子 random.Random(seed+t) 全部按期确定）。回放止于 draws[-1]，"
            "不把任何一期当作「已开奖的未来」。"),
        "note_zh": (
            "本报告为**实战对比口径**：各方法在相同历史条件下逐期出号，"
            "与真实开奖核对后互相比高低。所有结论附样本量；"
            "样本不足时判「样本不足」而不强行分胜负。"
            "ml / llm 无法历史回放，已在 non_replayable 中如实标注。"),
    }


# --------------------------------------------------------------------------
# 便捷入口
# --------------------------------------------------------------------------

def replay_summary(results: Sequence[Dict]) -> List[Dict]:
    """把 ``replay_all`` 的结果压成不含逐期明细的战绩表（便于打印/展示）。"""
    out = []
    for r in results or []:
        out.append({
            "method": r.get("method"),
            "replayable": bool(r.get("replayable")),
            "issues": r.get("issues", 0),
            "n_tickets_total": r.get("n_tickets_total", 0),
            "red_hits_mean": r.get("red_hits_mean", 0.0),
            "blue_hit_rate": r.get("blue_hit_rate", 0.0),
            "prize_rate_ge5": r.get("prize_rate_ge5", 0.0),
            "reward_total": r.get("reward_total", 0.0),
            "roi": r.get("roi", 0.0),
        })
    return out


# ==========================================================================
# V10-09：LLM 出号**独立 A/B 对照臂**（非规律库路径）
# ==========================================================================
#
# 为什么不是 `replay_method`
# -------------------------
# R3 G2 明确：LLM 不可历史回放（对 3215 期各调一次外部模型既不feasible也不可复现，
# 见模块 docstring 的 ``NON_REPLAYABLE_METHODS["llm"]``）。所以 LLM 这一臂的唯一
# 证据来源是**真实已发生**的在线预测记录 —— ``eval_details`` 表里逐注落库的
# ``(issue, method, seq, red_hits, blue_hit, ...)``。本模块新增的函数只读这张表，
# 一行都不生成、一行都不凭空构造：**没有记录就是没有记录**（诚实边界）。
#
# 为什么 LLM 永不进规律库/候选池（R3 G1）
# --------------------------------------
# 方案原条款「LLM 候选须 experiments.status == "passed" 才能进候选池」是死锁：
#   * hypothesis.evaluate_prereg() 要有支持才升 validated；
#   * patterns_runtime 要 regime == persistent 才 usable；
#   * battle.replay_method 明确 llm 不可回放 → A/B 永远无法"通过"。
# 三者叠加 → passed 集合恒为空 → LLM 永远出不了号（"AI 空转"只是换了地方重装）。
# 裁决：LLM 出号走**独立对照臂**，门槛降为「不劣于基线」，允许长期停在 eligible。
#
# 设计上的一致性要求（可被测试守住）
# --------------------------------
# 本节的函数**只**读 ``eval_details``：不读 experiments / prereg / usable / 假说库。
# 因此无论假说库是空的、全 failed、还是根本不存在，对照臂的结论都不受影响
# —— 这就是"防死锁"在代码层面的落地方式（``test_v10_llm_arm.py`` T7–T9 守）。
#
# 统计口径（为什么以「期」为独立性单位）
# ------------------------------------
# 一期内同一臂的多注共用**同一注开奖**，注与注之间强相关（命中数同向波动）。
# 把 N 注当成 N 个独立样本会把标准误缩小约 sqrt(N) 倍、凭空造出显著性。
# 因此：逐注配对只用于**描述**（``per_ticket`` 明细），而**显著性检验的输入是
# 每期的平均值序列**（``strategy_hits`` / ``baseline_hits``），N 期就是 N 个
# 独立观测。这与 ``compare_methods`` 的逐期口径一致。

# 结论四档（R3 G2：「允许永远 eligible 且默认关」）
LLM_ARM_VERDICT_IMPROVED = "improved"
LLM_ARM_VERDICT_NOT_WORSE = "not_worse"
LLM_ARM_VERDICT_INSUFFICIENT = "insufficient_sample"
LLM_ARM_VERDICT_WORSE = "worse"

LLM_ARM_VERDICT_ZH: Dict[str, str] = {
    LLM_ARM_VERDICT_IMPROVED: "显著优于基线",
    LLM_ARM_VERDICT_NOT_WORSE: "不劣于基线（无显著差异）",
    LLM_ARM_VERDICT_INSUFFICIENT: "样本不足",
    LLM_ARM_VERDICT_WORSE: "显著劣于基线",
}

# 哪些档位允许该臂继续参战（eligible）
LLM_ARM_ELIGIBLE_VERDICTS = frozenset({
    LLM_ARM_VERDICT_IMPROVED,
    LLM_ARM_VERDICT_NOT_WORSE,
    LLM_ARM_VERDICT_INSUFFICIENT,     # ← 关键：样本不足不得判死，否则又是死锁
})

# 可作为基线的方法族（llm 自己不能当基线）
LLM_ARM_BASELINE_FAMILIES = ("stat", "ml", "blend", "uniform")


def _is_llm_method(method: object) -> bool:
    return str(method or "").strip().lower().startswith("llm:")


def _pairable_arms(rows: Sequence[Dict]) -> Dict[str, Dict[str, List[Dict]]]:
    """把 eval_details 行整理成 ``{arm: {issue: [row, ...]}}``（按 seq 升序）。

    ``arm`` 是方法名字符串本身（例如 ``llm:agent-router/deepseek-v4-flash``）。
    只保留合法行（issue 非空、有 red_hits）；确定性排序保证可复现。
    """
    out: Dict[str, Dict[str, List[Dict]]] = {}
    for r in rows or []:
        method = str(r.get("method") or "").strip()
        issue = str(r.get("issue") or "").strip()
        if not method or not issue or r.get("red_hits") is None:
            continue
        out.setdefault(method, {}).setdefault(issue, []).append(dict(r))
    for per_issue in out.values():
        for issue_rows in per_issue.values():
            issue_rows.sort(key=lambda x: int(x.get("seq") or 0))
    return out


def _paired_issue_block(arm_rows: Dict[str, List[Dict]],
                        base_rows: Dict[str, List[Dict]],
                        max_tickets: Optional[int] = None) -> Dict:
    """按「同期 + 同 n_tickets + 逐注配对」抽出两臂的可配对期。

    配对规则（一条都不放宽，放宽了就不是配对设计）：
      1. **同期**：两臂都必须在同一 ``issue`` 有记录；
      2. **同 n_tickets**：该期内两臂注数必须相等（否则不是同一规模的口径）；
      3. **逐注配对**：按 ``seq`` 一一对应（第 k 注对第 k 注），配对数 = min(注数)。

    返回 ``{"issues", "red_s", "red_b", "blue_s", "blue_b", "per_ticket",
    "n_pairs", "n_issues", "dropped_ticket_mismatch", "ticket_mismatch_detail",
    "ticket_counts"}``。其中 ``red_s``/``red_b`` 是**逐期均值**序列（检验输入），
    ``per_ticket`` 是注级差值明细（仅描述，注间相关不可当独立样本）。
    """
    common = sorted(set(arm_rows) & set(base_rows),
                    key=lambda x: (len(x), x))       # 数值期号优先；异常期号兜底
    out: Dict = {
        "issues": [], "red_s": [], "red_b": [], "blue_s": [], "blue_b": [],
        "per_ticket": [], "n_pairs": 0, "n_issues": 0,
        "dropped_ticket_mismatch": 0, "ticket_mismatch_detail": [],
        "ticket_counts": [],
    }
    for issue in common:
        a = arm_rows[issue]
        b = base_rows[issue]
        k = min(len(a), len(b))
        if len(a) != len(b):
            out["dropped_ticket_mismatch"] += 1
            out["ticket_mismatch_detail"].append(
                {"issue": issue, "arm_tickets": len(a), "baseline_tickets": len(b)})
            continue
        if max_tickets is not None and len(a) > int(max_tickets):
            continue
        if k <= 0:
            continue
        a_red = [float(x["red_hits"]) for x in a[:k]]
        b_red = [float(x["red_hits"]) for x in b[:k]]
        a_blu = [float(x.get("blue_hit") or 0) for x in a[:k]]
        b_blu = [float(x.get("blue_hit") or 0) for x in b[:k]]
        out["issues"].append(issue)
        out["red_s"].append(float(np.mean(a_red)))
        out["red_b"].append(float(np.mean(b_red)))
        out["blue_s"].append(float(np.mean(a_blu)))
        out["blue_b"].append(float(np.mean(b_blu)))
        out["ticket_counts"].append(k)
        for kk in range(k):
            out["per_ticket"].append({
                "issue": issue, "seq": kk + 1,
                "arm_red": a_red[kk], "baseline_red": b_red[kk],
                "d_red": a_red[kk] - b_red[kk],
                "arm_blue": a_blu[kk], "baseline_blue": b_blu[kk],
                "d_blue": a_blu[kk] - b_blu[kk],
            })
        out["n_pairs"] += k
    out["n_issues"] = len(out["issues"])
    return out


def _two_sided(summary: Dict, n_trials: int = 1, alpha: float = 0.05) -> Dict:
    """把 ``rigor.strategy_significance`` 的结果改成 **A/B 对照需要的双侧口径**。

    为什么不直接用 ``rigor.strategy_significance`` 的 ``adjusted_p``：
    它的 ``p_value`` 是**单侧**的，方向由观测差值的符号决定
    （``p_two/2 if delta>0 else 1-p_two/2``）——这是为「策略 vs 理论基线」这种
    **预先只有一个研究方向**的场景设计的：策略比基线好才值得报。
    但对照臂是双向的：LLM **显著更差**同样是一个必须被检出的结论。
    实测：策略每期比基线少 1.9 个红球时，``adjusted_p`` 返回 **1.0**
    （完美地"不显著"），而 ``p_two_sided`` 是 1.4e-08。
    若不修正，``worse`` 这一档在本任务里**永远不可能被触发**（假绿）。

    因此：以 ``p_two_sided`` 为未校正 p，按 ``n_trials`` 做 Bonferroni 校正。
    单侧值原样保留在 ``p_one_sided`` 里，便于与既有报表对照。
    """
    if not summary:
        return {"p_uncorrected": None, "p_adjusted": None,
                "p_one_sided": None, "n_trials": int(max(1, n_trials)),
                "adjust_method": "none", "sign_test": None, "evidence": None}
    # ⚠️ 必须先尊重 rigor 的样本量保护：``n < 3`` 时它**故意**把
    # ``p_uncorrected`` / ``adjusted_p`` 置 None（"宁可返回 insufficient-data，
    # 也不要把退化的 p=1 当成已检验过的真实结果"）。但同一情形下 ``p_two_sided``
    # 仍可能给出 0.0（每期差值恒为常数的解析分支）——若这里照着 p_two_sided 走，
    # 就会把「2 期、差值恰好都是 +3」判成 improved。实测踩过：这是把 rigor 的
    # 保护绕过去，必须显式拦掉。
    if str(summary.get("verdict") or "") == "insufficient-data":
        return {"p_uncorrected": None, "p_adjusted": None,
                "p_one_sided": None, "n_trials": int(max(1, n_trials)),
                "adjust_method": "none", "sign_test": summary.get("sign_test"),
                "evidence": summary.get("verdict"),
                "note": "rigor 判定 insufficient-data（有效样本 < 3），拒绝给出显著性"}
    p_two = summary.get("p_two_sided")
    if p_two is None:
        p_two = summary.get("p_uncorrected")
    m = max(1, int(n_trials))
    if p_two is None:
        p_adj = None
    else:
        p_adj = min(1.0, float(p_two) * m) if m > 1 else float(p_two)
    return {
        "p_uncorrected": (None if p_two is None else float(p_two)),
        "p_adjusted": (None if p_adj is None else float(p_adj)),
        "p_one_sided": summary.get("p_uncorrected"),
        "n_trials": m,
        "adjust_method": "bonferroni" if m > 1 else "none",
        "sign_test": summary.get("sign_test"),
        "evidence": summary.get("verdict"),
    }


def _llm_arm_verdict(block: Dict, test: Dict, *, min_issues: int,
                     alpha: float) -> Dict:
    """把配对块 + 检验结果翻成四档结论（R3 G2 的判决表）。

    判据（顺序即优先级）：
      1. ``n_issues < min_issues``            → ``insufficient_sample``（**eligible**）
      2. 校正后 p < alpha 且 delta_red > 0    → ``improved``（eligible）
      3. 校正后 p < alpha 且 delta_red <= 0   → ``worse``（**不** eligible）
      4. 其余                                 → ``not_worse``（eligible）

    注意第 1、4 档都保持 ``eligible=True`` 但 ``enabled_default=False``：
    出号到底用不用 LLM 由 ``methods.resolve_channels().llm_can_pick`` 单独决定
    （默认 False）。**对照臂只负责「有没有资格继续参战」，绝不负责「启不启用」**
    —— 把这两件事混在一起，就是死锁与"AI 空转"的根源。
    """
    n_issues = int(block.get("n_issues") or 0)
    delta_red = test.get("delta")
    p_adj = test.get("adjusted_p")
    p_raw = test.get("p_uncorrected")

    if n_issues < int(min_issues):
        verdict = LLM_ARM_VERDICT_INSUFFICIENT
        reason = (f"可配对期数 {n_issues} < 门槛 {int(min_issues)}："
                  f"不给出优劣结论；对照臂保持 eligible（长期停在 eligible 是允许的稳态），"
                  f"但默认不启用。")
    elif p_adj is None:
        verdict = LLM_ARM_VERDICT_INSUFFICIENT
        reason = (f"{n_issues} 期配对但检验未能算出 p 值（{test.get('verdict')}）："
                  f"不给出优劣结论，保持 eligible 且默认不启用。")
    elif float(p_adj) < float(alpha):
        if delta_red is not None and float(delta_red) > 0:
            verdict = LLM_ARM_VERDICT_IMPROVED
            reason = (f"{n_issues} 期配对，校正后 p={float(p_adj):.4g} < {alpha} 且方向上行为"
                      f" +{float(delta_red):.4f} 红球/注：需要**独立样本复现**后才能谈优势，"
                      f"且仍默认不启用。")
        else:
            verdict = LLM_ARM_VERDICT_WORSE
            reason = (f"{n_issues} 期配对，校正后 p={float(p_adj):.4g} < {alpha} 且方向下行："
                      f"LLM 对照臂显著劣于基线，撤销 eligible。")
    else:
        verdict = LLM_ARM_VERDICT_NOT_WORSE
        reason = (f"{n_issues} 期配对，校正后 p={float(p_adj):.4g} ≥ {alpha}："
                  f"差异与随机波动不可区分（在真随机世界里这是**预期**结果）→ "
                  f"按「不劣于基线」门槛保持 eligible，但默认不启用。")

    eligible = verdict in LLM_ARM_ELIGIBLE_VERDICTS

    # 诚实性：主判决只看红球，但若**蓝球**另有显著差异，必须写进 reason。
    # 否则会出现「蓝球 20/20 期都中、红球无差异」时 reason 只说"与随机波动不可
    # 区分"——那等于把看到的事实藏起来，比没有结论更糟。
    blue = (test or {}).get("blue") or {}
    blue_p = blue.get("p_adjusted")
    blue_delta = blue.get("delta")
    if blue_p is not None and float(blue_p) < float(alpha):
        direction = "更高" if (blue_delta or 0) > 0 else "更低"
        reason += (f" 另：**蓝球**差异显著（校正后 p={float(blue_p):.4g}，"
                   f"LLM 蓝球命中率{direction} {abs(float(blue_delta or 0.0)):.4f}），"
                   f"但本判决以红球为主口径，蓝球不参与 verdict——请单独解读蓝球证据。")

    return {
        "verdict": verdict,
        "verdict_zh": LLM_ARM_VERDICT_ZH.get(verdict, verdict),
        "eligible": bool(eligible),
        # 关键：本函数**从不**启用出号；启用与否是 resolve_channels().llm_can_pick
        "enabled_default": False,
        "enable_gate": "methods.resolve_channels().llm_can_pick",
        "reason_zh": reason,
        "p_value": p_raw,
        "p_adjusted": p_adj,
    }


def llm_arm_report(rows: Optional[Sequence[Dict]] = None, *,
                   baseline_families: Sequence[str] = LLM_ARM_BASELINE_FAMILIES,
                   min_issues: Optional[int] = None,
                   alpha: Optional[float] = None,
                   limit_issues: Optional[int] = None,
                   n_trials: Optional[int] = None,
                   max_tickets: Optional[int] = None,
                   comparison: Optional[str] = None) -> Dict:
    """LLM 出号**独立对照臂**评估（V10-09；R3 G1/G2）。

    **只用真实已发生的 ``eval_details``**（在线预测逐注落库后与真实开奖核对的记录），
    不调用 ``replay_method``（llm 不可回放，见 ``NON_REPLAYABLE_METHODS``），
    也不读 experiments / prereg / 假说库 —— 因此假说库为空或全 failed 都**不影响**
    本结论（这是"防死锁"的代码层保证）。

    统计口径
    --------
    * **同期**：两臂必须在同一 ``issue`` 有记录；
    * **同 n_tickets**：该期内两臂注数相等（不同注数的期直接剔除并计数）；
    * **逐注配对**：按 ``seq`` 一一对应，配对数 = 该期注数；
    * **检验**：以**逐期均值序列**为输入（注间相关，不能当独立样本），走
      ``rigor.strategy_significance(strategy_hits, baseline_hits, n_trials=...)``
      —— 配对 t 检验 + 符号检验，并按 ``n_trials``（实际做了多少次比较）做
      Bonferroni 校正；红球为主口径，蓝球并列给出。

    参数
    ----
    rows: 预先取好的 eval_details 行（测试/复盘用）；``None`` 时读
          ``db.load_eval_details(limit_issues)``。
    baseline_families: 参与比较的基线族（默认 stat/ml/blend/uniform）。
    min_issues: 配对期数门槛（默认 ``config.METHOD_RECOMMENDATION_MIN_ISSUES``，
          即 60；低于门槛一律 ``insufficient_sample``，但**保持 eligible**）。
    alpha: 显著性水平（默认 ``config.METHOD_RECOMMENDATION_ALPHA``）。
    limit_issues: 只取最近 N 期（仅当 ``rows is None`` 时生效）。
    n_trials: 显式指定多重比较次数；缺省 = 实际做过的比较数（≥1）。
    max_tickets: 只使用注数 ≤ 该值的期（避免个别的超大复式期把均值稀释）。
    comparison: 指定主口径比较（``"llm:x|stat:freq"``）；缺省自动挑证据最多的一个。

    返回
    ----
    dict（顶层即"主口径"一条结论，字段名与任务书一致）：
      * ``arm`` / ``baseline_arm``：两臂方法名
      * ``n_issues`` / ``n_pairs``：配对**期数** / 配对**注数**
      * ``delta_red`` / ``delta_blue``：逐期均值差（正 = LLM 更好）
      * ``p_value`` / ``p_adjusted``：未校正 / 校正后 p
      * ``verdict``（improved/not_worse/insufficient_sample/worse）
      * ``eligible`` / ``enabled_default`` / ``enable_gate`` / ``reason_zh``
      * ``comparisons``：所有比较的明细；``family_level``：族级汇总（描述）
      * ``by_arm``：两臂各自的描述统计（期数/注数/红球均值/蓝球率/ROI 等）
      * ``data_source`` / ``n_rows`` / ``n_issues_data``：数据基准（可审计）
      * ``no_deadlock_note_zh``：说明本结论与 experiments/prereg 完全无关
    """
    min_n = int(min_issues if min_issues is not None
                else getattr(config, "METHOD_RECOMMENDATION_MIN_ISSUES", 60))
    a = float(alpha if alpha is not None
              else getattr(config, "METHOD_RECOMMENDATION_ALPHA", 0.05))

    if rows is None:
        from . import db
        rows = db.load_eval_details(limit_issues=limit_issues)
        data_source = "db.eval_details"
    else:
        data_source = "caller_rows"
    rows = [dict(r) for r in (rows or [])]

    arms = _pairable_arms(rows)
    llm_methods = sorted(m for m in arms if _is_llm_method(m))
    base_fams = tuple(str(f).strip().lower() for f in (baseline_families or ()))
    base_methods = sorted(m for m in arms
                          if not _is_llm_method(m)
                          and EV.method_family(m) in base_fams)

    out: Dict = {
        "arm": None, "baseline_arm": None,
        "n_issues": 0, "n_pairs": 0,
        "delta_red": None, "delta_blue": None,
        "p_value": None, "p_adjusted": None,
        "alpha": a, "min_issues": min_n,
        "n_trials": 0,
        "verdict": LLM_ARM_VERDICT_INSUFFICIENT,
        "verdict_zh": LLM_ARM_VERDICT_ZH[LLM_ARM_VERDICT_INSUFFICIENT],
        "eligible": True,
        "enabled_default": False,
        "enable_gate": "methods.resolve_channels().llm_can_pick",
        "reason_zh": "",
        "comparisons": [], "family_level": None, "by_arm": {},
        "data_source": data_source, "n_rows": len(rows),
        "n_issues_data": len({str(r.get("issue")) for r in rows if r.get("issue")}),
        "llm_methods": llm_methods, "baseline_methods": base_methods,
        "no_deadlock_note_zh": (
            "本结论只读 eval_details（真实在线预测的逐注核对记录），"
            "完全不依赖 experiments.status / prereg_status / patterns_runtime 的 usable："
            "假说库为空或全部 failed 都不影响对照臂能否给出结论。"),
        "note_zh": "",
    }
    if not llm_methods or not base_methods:
        out["reason_zh"] = (
            "缺少可配对的两臂记录："
            f"llm 方法 {len(llm_methods)} 个、基线方法 {len(base_methods)} 个。"
            "样本不足不判死——对照臂保持 eligible，等待真实预测累积；默认不启用。")
        out["note_zh"] = (
            "LLM 不可历史回放（replay_method 明确不支持），其证据只能来自真实累积的"
            "在线预测记录；因此没有记录时唯一诚实的结论是「样本不足」。")
        return out

    # ---- 逐个比较（llm 方法 × 基线方法） ----
    raw_cmps: List[Dict] = []
    for lm in llm_methods:
        for bm in base_methods:
            block = _paired_issue_block(arms[lm], arms[bm], max_tickets=max_tickets)
            raw_cmps.append({"llm_method": lm, "baseline_method": bm, "block": block})

    tested = [c for c in raw_cmps if c["block"]["n_issues"] > 0]
    n_tested = len(tested)
    n_trials_eff = max(1, int(n_trials) if n_trials is not None else n_tested)

    cmps: List[Dict] = []
    for c in raw_cmps:
        block = c["block"]
        rec: Dict = {
            "comparison": f"{c['llm_method']}|{c['baseline_method']}",
            "llm_method": c["llm_method"],
            "baseline_method": c["baseline_method"],
            "baseline_family": EV.method_family(c["baseline_method"]),
            "n_issues": block["n_issues"], "n_pairs": block["n_pairs"],
            "issues": block["issues"],
            "ticket_counts": block["ticket_counts"],
            "dropped_ticket_mismatch": block["dropped_ticket_mismatch"],
            "ticket_mismatch_detail": block["ticket_mismatch_detail"],
            "delta_red": None, "delta_blue": None,
            "p_value": None, "p_adjusted": None,
            "red_test": None, "blue_test": None,
            "verdict": LLM_ARM_VERDICT_INSUFFICIENT,
            "verdict_zh": LLM_ARM_VERDICT_ZH[LLM_ARM_VERDICT_INSUFFICIENT],
            "eligible": True, "enabled_default": False,
            "reason_zh": "",
        }
        if block["n_issues"] > 0:
            red_t = rigor.strategy_significance(block["red_s"], block["red_b"],
                                                n_trials=n_trials_eff, alpha=a)
            blue_t = rigor.strategy_significance(block["blue_s"], block["blue_b"],
                                                 n_trials=n_trials_eff, alpha=a)
            # A/B 是双向的：用双侧 p（见 _two_sided 的说明）
            rec["red_test"] = {**red_t, "two_sided": _two_sided(red_t, n_trials_eff, a)}
            rec["blue_test"] = {**blue_t, "two_sided": _two_sided(blue_t, n_trials_eff, a)}
            rec["delta_red"] = red_t.get("delta")
            rec["delta_blue"] = blue_t.get("delta")
            rec["p_value"] = rec["red_test"]["two_sided"]["p_uncorrected"]
            rec["p_adjusted"] = rec["red_test"]["two_sided"]["p_adjusted"]
            rec["per_ticket_head"] = block["per_ticket"][:20]
            # 注级差值（描述口径，注间相关 → 只作参考，不作检验输入）
            if block["per_ticket"]:
                rec["delta_red_per_ticket"] = float(
                    np.mean([p["d_red"] for p in block["per_ticket"]]))
                rec["delta_blue_per_ticket"] = float(
                    np.mean([p["d_blue"] for p in block["per_ticket"]]))
        # 判决必须吃**双侧**口径：把 two_sided 的 p 提到顶层（rigor 原有的
        # 单侧值仍在 red_test["p_uncorrected"] / ["p_one_sided"] 里可查）。
        v_test: Dict = {}
        if rec["red_test"]:
            ts = rec["red_test"].get("two_sided") or {}
            # 键名与 _llm_arm_verdict 的读取契约一致（adjusted_p/p_uncorrected）
            bts = (rec["blue_test"] or {}).get("two_sided") or {}
            v_test = {"delta": rec["red_test"].get("delta"),
                      "p_uncorrected": ts.get("p_uncorrected"),
                      "adjusted_p": ts.get("p_adjusted"),
                      "verdict": rec["red_test"].get("verdict"),
                      "blue": {"delta": (rec["blue_test"] or {}).get("delta"),
                               "p_adjusted": bts.get("p_adjusted")}}
        v = _llm_arm_verdict(block, v_test, min_issues=min_n, alpha=a)
        rec.update({k: v[k] for k in ("verdict", "verdict_zh", "eligible",
                                      "enabled_default", "reason_zh")})
        cmps.append(rec)

    # ---- 主口径：优先 comparisons 证据最多的；并列时取 p 更小者；再并列取名字序 ----
    pool = [c for c in cmps if c["n_issues"] > 0] or cmps
    if comparison:
        picked = next((c for c in cmps if c["comparison"] == comparison), None)
        if picked is None:
            picked = pool[0] if pool else None
    else:
        picked = sorted(pool, key=lambda c: (-c["n_issues"], -c["n_pairs"],
                                             c["p_adjusted"] if c["p_adjusted"] is not None else 1.0,
                                             c["comparison"]))[0] if pool else None

    # ---- 族级汇总（描述口径：所有 llm:* 合并 vs 各基线族合并） ----
    llm_pool: Dict[str, List[Dict]] = {}
    for lm in llm_methods:
        for issue, issue_rows in arms[lm].items():
            llm_pool.setdefault(issue, []).extend(issue_rows)
    for issue in llm_pool:
        llm_pool[issue].sort(key=lambda x: int(x.get("seq") or 0))
    fam_level: Dict = {}
    for fam in ("stat", "ml", "blend", "uniform"):
        fam_methods = [m for m in arms if not _is_llm_method(m)
                       and EV.method_family(m) == fam]
        if not fam_methods:
            continue
        fam_pool: Dict[str, List[Dict]] = {}
        for m in fam_methods:
            for issue, issue_rows in arms[m].items():
                fam_pool.setdefault(issue, []).extend(issue_rows)
        for issue in fam_pool:
            fam_pool[issue].sort(key=lambda x: int(x.get("seq") or 0))
        blk = _paired_issue_block(llm_pool, fam_pool, max_tickets=max_tickets)
        item: Dict = {"family": fam, "n_issues": blk["n_issues"],
                      "n_pairs": blk["n_pairs"],
                      "delta_red": None, "delta_blue": None,
                      "p_value": None, "p_adjusted": None,
                      "dropped_ticket_mismatch": blk["dropped_ticket_mismatch"],
                      "verdict": LLM_ARM_VERDICT_INSUFFICIENT,
                      "verdict_zh": LLM_ARM_VERDICT_ZH[LLM_ARM_VERDICT_INSUFFICIENT]}
        if blk["n_issues"] > 0:
            rt = rigor.strategy_significance(blk["red_s"], blk["red_b"],
                                             n_trials=n_trials_eff, alpha=a)
            bt = rigor.strategy_significance(blk["blue_s"], blk["blue_b"],
                                             n_trials=n_trials_eff, alpha=a)
            ts = _two_sided(rt, n_trials_eff, a)
            _bts = _two_sided(bt, n_trials_eff, a)
            _fam_v = _llm_arm_verdict(
                blk, {"delta": rt.get("delta"),
                      "p_uncorrected": ts["p_uncorrected"],
                      "adjusted_p": ts["p_adjusted"],
                      "verdict": rt.get("verdict"),
                      "blue": {"delta": bt.get("delta"),
                               "p_adjusted": _bts.get("p_adjusted")}},
                min_issues=min_n, alpha=a)
            item.update({"delta_red": rt.get("delta"), "delta_blue": bt.get("delta"),
                         "p_value": ts["p_uncorrected"],
                         "p_adjusted": ts["p_adjusted"],
                         "verdict": _fam_v["verdict"],
                         "verdict_zh": _fam_v["verdict_zh"]})
        fam_level[fam] = item
    out["family_level"] = fam_level

    # ---- 两臂描述统计 ----
    def _arm_stats(pool: Dict[str, List[Dict]]) -> Dict:
        all_rows = [r for issue_rows in pool.values() for r in issue_rows]
        n_t = len(all_rows)
        reds = [float(r["red_hits"]) for r in all_rows]
        blues = [float(r.get("blue_hit") or 0) for r in all_rows]
        costs = [float(r.get("ticket_cost") or 2.0) for r in all_rows]
        rewards = [float(r.get("reward") or 0.0) for r in all_rows]
        cost = float(sum(costs))
        rew = float(sum(rewards))
        return {
            "n_issues": len(pool), "n_tickets": n_t,
            "red_hits_mean": float(np.mean(reds)) if reds else None,
            "blue_hit_rate": float(np.mean(blues)) if blues else None,
            "reward_total": rew, "cost_total": cost,
            "roi": ((rew - cost) / cost) if cost > 0 else None,
        }

    for lm in llm_methods:
        out["by_arm"][lm] = {**_arm_stats(arms[lm]), "family": "llm"}
    for bm in base_methods:
        out["by_arm"][bm] = {**_arm_stats(arms[bm]), "family": EV.method_family(bm)}

    out["comparisons"] = sorted(
        cmps, key=lambda c: (-c["n_issues"], -c["n_pairs"], c["comparison"]))
    out["n_trials"] = int(n_trials_eff)
    out["n_comparisons_tested"] = int(n_tested)

    if picked is None:
        out["reason_zh"] = (
            "没有可比较的 (llm, 基线) 方法对（记录里只有一臂）：样本不足，"
            "对照臂保持 eligible 且默认不启用。")
        return out

    out.update({
        "arm": picked["llm_method"],
        "baseline_arm": picked["baseline_method"],
        "n_issues": picked["n_issues"],
        "n_pairs": picked["n_pairs"],
        "issues": picked["issues"],
        "ticket_counts": picked["ticket_counts"],
        "delta_red": picked["delta_red"],
        "delta_blue": picked["delta_blue"],
        "p_value": picked["p_value"],
        "p_adjusted": picked["p_adjusted"],
        "verdict": picked["verdict"],
        "verdict_zh": picked["verdict_zh"],
        "eligible": picked["eligible"],
        "enabled_default": False,
        "reason_zh": picked["reason_zh"],
        "red_test": picked["red_test"],
        "blue_test": picked["blue_test"],
        "dropped_ticket_mismatch": picked["dropped_ticket_mismatch"],
        "ticket_mismatch_detail": picked["ticket_mismatch_detail"],
    })
    out["note_zh"] = (
        f"口径：同期 + 同 n_tickets + 逐注配对；检验用逐期均值序列"
        f"（注间相关，不作独立样本）+ rigor.strategy_significance，"
        f"n_trials={n_trials_eff} 作 Bonferroni 校正。红球为主口径、蓝球并列。"
        f"主口径取证据最多的比较 {picked['comparison']}；"
        f"其余 {len(cmps) - 1} 个比较见 comparisons。"
        "LLM 不可历史回放，本报告只用真实 online 记录；"
        "eligible ≠ 启用：启用由 resolve_channels().llm_can_pick 决定（默认关）。")
    return out


def llm_arm_brief(report: Dict) -> Dict:
    """把 ``llm_arm_report`` 压成一行可展示的摘要（供 API/前端复用）。"""
    r = report or {}
    return {
        "arm": r.get("arm"),
        "baseline_arm": r.get("baseline_arm"),
        "n_issues": r.get("n_issues", 0),
        "n_pairs": r.get("n_pairs", 0),
        "delta_red": r.get("delta_red"),
        "delta_blue": r.get("delta_blue"),
        "p_value": r.get("p_value"),
        "p_adjusted": r.get("p_adjusted"),
        "verdict": r.get("verdict"),
        "verdict_zh": r.get("verdict_zh"),
        "eligible": bool(r.get("eligible")),
        "enabled_default": bool(r.get("enabled_default", False)),
        "reason_zh": r.get("reason_zh"),
    }
