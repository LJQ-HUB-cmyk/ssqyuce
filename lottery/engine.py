"""集成预测引擎：统计模型 + LLM 通道 → 硬约束过滤 → 置信度评分 → Top-N 输出。"""
from __future__ import annotations

import json
import random
import time
from typing import Dict, List, Optional

import numpy as np

from . import backtest as BT
from . import config, db, features as F, llm_client, methods as METH, ml_model, models as M
from .llm_client import LLMChannelError

R_MAX, B_MAX = 33, 16


# ---------- 硬约束 ----------

def _last_sums(draws: List[Dict], n: int = 500) -> np.ndarray:
    return F.sums(draws[-n:]) if len(draws) >= n else F.sums(draws)


def constraint_ctx(draws: List[Dict]) -> Dict:
    s = _last_sums(draws)
    zc = F.zone_counts(draws[-500:] if len(draws) >= 500 else draws)
    zc_hist = {}
    for z in zc:
        zc_hist[z] = zc_hist.get(z, 0) + 1
    top_zones = [k for k, _ in sorted(zc_hist.items(), key=lambda x: -x[1])[:10]]
    oc = F.odd_counts(draws[-500:] if len(draws) >= 500 else draws)
    odd_hist = {}
    for o in oc:
        odd_hist[int(o)] = odd_hist.get(int(o), 0) + 1
    odd_range = sorted(
        (k for k, v in odd_hist.items() if v / max(1, len(oc)) >= 0.02)
    )
    return {
        "sum_min": float(np.percentile(s, 5)),
        "sum_max": float(np.percentile(s, 95)),
        "top_zones": top_zones,
        "odd_range": odd_range if odd_range else [2, 3, 4],
    }


def pass_constraints(reds: List[int], blue: int, ctx: Dict) -> bool:
    s = sum(reds)
    if not (ctx["sum_min"] <= s <= ctx["sum_max"]):
        return False
    z1 = sum(1 for r in reds if 1 <= r <= 11)
    z2 = sum(1 for r in reds if 12 <= r <= 22)
    z3 = 6 - z1 - z2
    if (z1, z2, z3) not in ctx["top_zones"]:
        return False
    odd = sum(1 for r in reds if r % 2 == 1)
    if odd not in ctx["odd_range"]:
        return False
    return True


# ---------- 统计采样 ----------

def _weighted_sample(weights: np.ndarray, k: int, rng: random.Random) -> List[int]:
    pool = list(range(1, len(weights) + 1))
    w = weights.astype(float).copy()
    chosen = []
    for _ in range(k):
        if w.sum() <= 0:
            w = np.ones_like(w)
        p = (w / w.sum()).tolist()
        pick = rng.choices(pool, weights=p, k=1)[0]
        chosen.append(pick)
        w[pick - 1] = 0.0
    return sorted(chosen)


def sample_stat_ticket(red_blend: np.ndarray, blue_blend: np.ndarray,
                       ctx: Dict, rng: random.Random, uniform: bool = False) -> Optional[Dict]:
    """按混合概率加权不放回抽样，硬约束不过则重试。"""
    for _ in range(60):
        if uniform:
            reds = sorted(rng.sample(range(1, R_MAX + 1), 6))
            blue = rng.randint(1, B_MAX)
        else:
            reds = _weighted_sample(red_blend, 6, rng)
            blue = _weighted_sample(blue_blend, 1, rng)[0]
        if pass_constraints(reds, blue, ctx):
            return {"reds": reds, "blue": blue}
    return None


def ensemble_mass(red_blend: np.ndarray, blue_blend: np.ndarray,
                  reds: List[int], blue: int) -> float:
    """候选号在混合分布上的概率质量（0-100 比例尺）。"""
    r_mass = float(np.mean(red_blend[np.array(reds) - 1]))
    b_mass = float(blue_blend[blue - 1])
    norm_r = float(np.max(red_blend))
    norm_b = float(np.max(blue_blend))
    if norm_r <= 0:
        norm_r = 1.0
    if norm_b <= 0:
        norm_b = 1.0
    return 100.0 * (0.75 * r_mass / norm_r + 0.25 * b_mass / norm_b)


# ---------- V4-B：融合权重（walk-forward 样本外 + softmax + 上限 + 收缩） ----------
#
# 历史缺陷（v4 前）：_brier_blend 用「最近 50 期」算样本内 Brier，再以
#   weights = max(0.01, 1/brier) 定权。三重错误叠加：
#   (a) 样本内：模型本身就是在这些期上拟合/平滑出来的，Brier 系统性偏乐观；
#   (b) 1/brier 无上界：样本内 brier→0 时权重→+∞（实测样本内 brier 0.001
#       会拿到约 149x 于均匀模型的权重），是追逐噪声而非贝叶斯融合；
#   (c) 无权重上限、无收缩：小样本抖动直接变成单一方法独裁。
# 现改为：严格 walk-forward 样本外 Brier → softmax(-brier/T) → 权重上限 → 收缩。

# 融合权重评估窗口（期）。旧值 50 保持不变，仅用于样本外评估。
BLEND_EVAL_WINDOW = 50
# walk-forward 评估时必须保留的最小训练历史（期）。用于把评估窗口截断到
# len(draws)-300 之内，保证每个评估点都有足够历史重建模型。
BLEND_MIN_TRAIN = 300
# 少于该期数不做样本外评估 → 退化为等权（数据不足时诚实等权，不假装有信息）。
BLEND_MIN_EVAL = 10
# softmax 温度 T：w ∝ exp(-brier/T)。T 越大越接近等权。红球 Brier 的合理量级
# 约 0.10-0.20（均匀模型 33 维、单期 6 命中约 0.149），取 0.05 使「brier 相差
# 0.05」≈ 权重比 e≈2.7x —— 有区分度但远不到独裁。旧 1/brier 在同样差距下可
# 达数十倍。蓝球 16 维 Brier 量级更小，用单独的温度。
BLEND_SOFTMAX_TEMP_RED = 0.05
BLEND_SOFTMAX_TEMP_BLUE = 0.02
# 任一方法在融合中的权重上限。防止单一方法独裁（旧逻辑无上限）。
MAX_MODEL_WEIGHT = 0.6
# softmax 前的 Brier 保护下限，避免极端 0 值导致 exp 溢出/独裁。
# 注意：这不是旧的「1/brier 放大」——它只用于数值稳定，权重动态范围仍由温度控制。
BLEND_BRIER_FLOOR = 0.001
# 权重收缩系数 λ：final = (1-λ)·uniform + λ·data。λ=0.5 防小样本抖动。
WEIGHT_SHRINKAGE = 0.5


class _BlendArray(np.ndarray):
    """带 .attrs 的 float ndarray，用于携带融合明细。

    纯 ndarray 无法挂属性；子类化后对调用方完全透明（算术、切片、sum、
    np.asarray 行为不变），仅多出 `.attrs` 供审计字段（weight_source /
    n_eval / raw_briers / weights）传递，避免改动 _brier_blend 的返回签名。
    """

    def __new__(cls, data, attrs: Optional[Dict] = None):
        obj = np.asarray(data, dtype=float).view(cls)
        obj.attrs = dict(attrs or {})
        return obj

    def __array_finalize__(self, obj):
        if obj is None:
            return
        self.attrs = getattr(obj, "attrs", None)
        # 视图/切片默认丢弃明细，避免携带过期元数据
        if self.attrs is not None and self.base is not None:
            self.attrs = None


def _with_meta(arr: np.ndarray, meta: Dict) -> "_BlendArray":
    """把融合明细挂到红球融合向量上（返回 ndarray 子类，调用方无感）。"""
    return _BlendArray(arr, meta)


def _softmax_weights(briers: Dict[str, float], temp: float) -> Dict[str, float]:
    """softmax over -brier：w_m ∝ exp(-brier_m / T)，再做权重上限 + 均匀收缩。

    相比旧的 1/brier 倒数放大：
    - 指数形式天然有界（任一权重 ≤ 1，且差异由温度 T 控制）；
    - 再经 MAX_MODEL_WEIGHT 截断 + (1-λ) 均匀收缩，杜绝单一方法独裁；
    - 全为 nan/inf 或空池时回退等权。
    """
    if not briers:
        return {}
    names = list(briers)
    vals = np.array([briers[n] for n in names], dtype=float)
    vals = np.where(np.isfinite(vals), vals, np.nanmax(vals[np.isfinite(vals)]) if np.isfinite(vals).any() else 1.0)
    logits = -np.maximum(vals, BLEND_BRIER_FLOOR) / max(temp, 1e-9)
    logits = logits - logits.max()          # 数值稳定
    w = np.exp(logits)
    s = w.sum()
    weights = (w / s) if s > 0 else np.full(len(names), 1.0 / len(names))

    # 权重收缩：向均匀权重靠拢，防小样本抖动
    n = len(names)
    lam = float(min(max(WEIGHT_SHRINKAGE, 0.0), 1.0))
    weights = (1.0 - lam) * (1.0 / n) + lam * weights

    # 权重上限（迭代截断：被截的部分按比例回补给未达上限的方法）
    weights = _cap_weights(weights, MAX_MODEL_WEIGHT)
    return {name: float(v) for name, v in zip(names, weights)}


def _cap_weights(weights: np.ndarray, cap: float) -> np.ndarray:
    """把每个权重截断到 cap，并把超出的质量按比例回补给未达上限的分量。

    若 cap·n < 1（上限不可行），退化为均匀权重。
    """
    w = np.asarray(weights, dtype=float).copy()
    n = w.size
    if n == 0:
        return w
    if cap * n <= 1.0:
        return np.full(n, 1.0 / n)
    for _ in range(n + 1):
        over = w > cap + 1e-12
        if not over.any():
            break
        excess = float((w[over] - cap).sum())
        w[over] = cap
        free = ~over & (w < cap)
        if not free.any():
            break
        w[free] += excess * (w[free] / w[free].sum())
    w = np.clip(w, 0.0, cap)
    s = w.sum()
    return w / s if s > 0 else np.full(n, 1.0 / n)


def _rebuild_probs(name: str, hist: List[Dict]) -> Optional[Dict[str, np.ndarray]]:
    """用**给定的历史**（下标 < t 的期）重建某个方法的概率分布。

    这是 walk-forward 的核心：评估第 t 期时，只允许看见 hist = draws[:t]。

    可重建的只有模块内的统计基线（models.build_models 的成员）与
    models.blue_specialist；其余（ml / 外部注入 / LLM 通道）无法用历史回放，
    返回 None，由调用方按 fixed-fallback 处理。
    """
    try:
        if name == "blue_specialist":
            return {"blue": M.blue_specialist(hist)}
        models = M.build_models(hist)
        if name in models:
            m = models[name]
            return {"red": np.asarray(m["red"], dtype=float),
                    "blue": np.asarray(m["blue"], dtype=float)}
    except Exception as e:  # noqa: BLE001
        print(f"[engine] walk-forward 重建 {name} 失败（{len(hist)} 期）: {e}")
    return None


def _walk_forward_brier(pool: Dict[str, Dict], draws: List[Dict],
                        n_eval: int) -> Dict[str, Dict[str, float]]:
    """严格 walk-forward 样本外 Brier。

    对每个评估期 t（绝对下标），**只用 draws[:t] 重建该方法的概率分布**，
    再与第 t 期真实结果比较。绝不使用第 t 期及以后的数据。

    注意：调用方传入的向量只用于**确定方法池与维度**，不用于算分——算分一律
    用重建结果。这正是修掉「样本内评估」缺陷的关键：旧实现拿调用方那条已经在
    （含评估窗口的）全量历史上拟合好的向量去和同期真实结果比，Brier 必然乐观。

    返回 {method: {"red": b, "blue": b, "n": n}}，仅含成功评估的方法。
    """
    out: Dict[str, Dict[str, float]] = {}
    if n_eval <= 0 or not draws:
        return out

    n = len(draws)
    start = n - n_eval                     # 首个评估期的绝对下标
    acc: Dict[str, Dict[str, float]] = {}

    for t in range(start, n):
        hist = draws[:t]                   # ← 严格样本外：只到第 t-1 期
        if len(hist) < 2:
            continue
        cur = draws[t]
        tr = np.zeros(R_MAX)
        for r in cur["reds"]:
            tr[r - 1] = 1.0
        tb = np.zeros(B_MAX)
        tb[cur["blue"] - 1] = 1.0

        for name, m in pool.items():
            rebuilt = _rebuild_probs(name, hist)
            if rebuilt is None:
                continue                   # 无法用历史重建 → 不参与样本外定权
            slot = acc.setdefault(name, {"red": 0.0, "blue": 0.0, "n": 0})
            hit = False
            if m.get("red") is not None and rebuilt.get("red") is not None:
                if rebuilt["red"].shape == np.asarray(m["red"]).shape:
                    slot["red"] += float(np.mean((rebuilt["red"] - tr) ** 2))
                    hit = True
            if m.get("blue") is not None and rebuilt.get("blue") is not None:
                if rebuilt["blue"].shape == np.asarray(m["blue"]).shape:
                    slot["blue"] += float(np.mean((rebuilt["blue"] - tb) ** 2))
                    hit = True
            if hit:
                slot["n"] += 1

    for name, slot in acc.items():
        if slot["n"] > 0:
            out[name] = {"red": slot["red"] / slot["n"],
                         "blue": slot["blue"] / slot["n"],
                         "n": slot["n"]}
    return out


def _brier_blend(models_dict: Dict, draws: List[Dict], adaptive: Optional[Dict] = None) -> tuple:
    """基于 **walk-forward 样本外** Brier 的模型概率融合，叠加 U3 自适应降权。

    权重策略（V4-B）：
    1. 严格 walk-forward：只用第 t 期之前的历史重建每个方法的概率，再与第 t
       期真实结果算 Brier（默认窗口 50 期，且截断到 len(draws)-300 之内）。
    2. softmax(-brier/T) 取代旧的 1/brier 倒数放大。
    3. 权重上限 MAX_MODEL_WEIGHT=0.6，防止单一方法独裁。
    4. 权重收缩 WEIGHT_SHRINKAGE=0.5：final = 0.5·uniform + 0.5·data。
    无法用历史重建的方法（ml / 外部注入）**不参与**定权，以预设等权参与融合，
    并在返回值中标注 weight_source="fixed-fallback"。

    adaptive: {method: weight}，来自 METH.adaptive_weights()；None = 不施加。
    蓝球始终并入 M.blue_specialist()（U2），避免蓝球“裸奔”。

    返回 (red_blend, blue_blend)。函数签名与返回类型保持不变（red_blend 仍是
    ndarray，调用方无需改动）；融合明细（weights / raw_briers / weight_source /
    n_eval）挂在 red_blend.attrs 上，供 API 审计暴露。
    """
    models_dict = dict(models_dict)
    # U2：蓝球专用模型恒在混合里（红球仅按调用方给定的模型池）
    blue_spec = {"blue_specialist": {
        "red": None, "blue": M.blue_specialist(draws), "name": "blue_specialist"}}
    n_eval = min(BLEND_EVAL_WINDOW, len(draws) - BLEND_MIN_TRAIN)
    # 融合用模型池：red 用 models_dict，blue 用 models_dict + blue_specialist
    red_pool = {k: m["red"] for k, m in models_dict.items() if m.get("red") is not None}
    blue_pool = {k: m["blue"] for k, m in models_dict.items() if m.get("blue") is not None}
    blue_pool["blue_specialist"] = blue_spec["blue_specialist"]["blue"]

    if n_eval < BLEND_MIN_EVAL:
        # 数据不足：等权（blue_specialist 权重与统计模型对等），不假装有信息
        n = len(red_pool) or 1
        red_blend = sum(red_pool.values()) / n if red_pool else np.full(R_MAX, 1.0 / R_MAX)
        blue_blend = sum(blue_pool.values()) / len(blue_pool) if blue_pool else np.full(B_MAX, 1.0 / B_MAX)
        red_blend = _apply_adaptive_red(red_blend, adaptive, red_pool)
        red_blend = np.asarray(red_blend, dtype=float)
        if red_blend.sum() <= 0:
            red_blend = np.full(R_MAX, 1.0 / R_MAX)
        else:
            red_blend = red_blend / red_blend.sum() * 6
        blue_blend = np.asarray(blue_blend, dtype=float)
        blue_blend = (blue_blend / blue_blend.sum() if blue_blend.sum() > 0
                      else np.full(B_MAX, 1.0 / B_MAX))
        meta = {
            "weight_source": "fixed-fallback",
            "reason": "insufficient_history",
            "n_eval": max(n_eval, 0),
            "raw_briers": {},
            "weights": {"red": {k: 1.0 / (len(red_pool) or 1) for k in red_pool},
                        "blue": {k: 1.0 / (len(blue_pool) or 1) for k in blue_pool}},
        }
        return _with_meta(red_blend, meta), blue_blend

    # ---- 严格 walk-forward 样本外 Brier ----
    # 蓝球侧：blue_specialist 同样可用历史重建，纳入评估
    wf_pool: Dict[str, Dict] = dict(models_dict)
    # ---- 严格 walk-forward 样本外 Brier ----
    # 传入的是「方法池 + 各自需要的维度」，算分一律用 draws[:t] 重建的结果，
    # 不使用调用方传入的（已在含评估窗口的全量历史上拟合的）向量。
    wf_pool: Dict[str, Dict] = {}
    for name, m in models_dict.items():
        if m.get("red") is not None or m.get("blue") is not None:
            wf_pool[name] = m
    wf_pool["blue_specialist"] = blue_spec["blue_specialist"]
    wf = _walk_forward_brier(wf_pool, draws, n_eval)

    raw_briers: Dict[str, Dict[str, float]] = {}
    # 无法重建的方法 → 预设等权（fixed-fallback）
    evaluated = set(wf.keys())
    fallback_red = sorted(k for k in red_pool if k not in evaluated)
    fallback_blue = sorted(k for k in blue_pool if k not in evaluated)
    partial = bool(fallback_red or fallback_blue)

    # 红球权重：可评估者用样本外 Brier 定权；不可评估者取池内「有效权重的中位数」
    # 作为预设权重，使其以对等地位参与，而**不**使用其样本内 Brier。
    red_briers = {k: wf[k]["red"] for k in red_pool if k in evaluated}
    blue_briers = {k: wf[k]["blue"] for k in blue_pool if k in evaluated}
    raw_briers["red"] = {k: round(float(v), 6) for k, v in red_briers.items()}
    raw_briers["blue"] = {k: round(float(v), 6) for k, v in blue_briers.items()}
    for k in fallback_red:
        raw_briers["red"][k] = None
    for k in fallback_blue:
        raw_briers["blue"][k] = None

    weights_red = _softmax_weights(red_briers, BLEND_SOFTMAX_TEMP_RED) if red_briers else {}
    weights_blue = _softmax_weights(blue_briers, BLEND_SOFTMAX_TEMP_BLUE) if blue_briers else {}
    # 回退方法：给「可评估方法」的最大权重（即对等参与，而非靠样本内 Brier 抢权）
    for k in fallback_red:
        weights_red[k] = (max(weights_red.values()) if weights_red
                          else 1.0 / max(1, len(red_pool)))
    for k in fallback_blue:
        weights_blue[k] = (max(weights_blue.values()) if weights_blue
                           else 1.0 / max(1, len(blue_pool)))
    if not red_pool:
        weights_red = {}
    if not blue_pool:
        weights_blue = {}

    # U3 自适应降权：对 red 池的方法按权重打折（在融合权重上施加，语义不变）
    if adaptive:
        for name in list(weights_red.keys()):
            weights_red[name] = weights_red[name] * float(adaptive.get(name, 1.0))
        weights_red = _renormalize(weights_red)

    def _norm_red_blend(d: Dict[str, float]) -> np.ndarray:
        out = np.zeros(R_MAX)
        for name, m in red_pool.items():
            out += d.get(name, 0.0) * np.asarray(m, dtype=float)
        return out

    red_blend = _apply_adaptive_red(
        _norm_red_blend(weights_red) if weights_red else np.zeros(R_MAX),
        adaptive, red_pool)
    red_blend = np.asarray(red_blend, dtype=float)
    norm_b = sum(weights_blue.values()) or 1.0
    weights_blue_pct = {k: v / norm_b for k, v in weights_blue.items()}
    blue_blend = np.zeros(B_MAX)
    for name, m in blue_pool.items():
        blue_blend += weights_blue_pct.get(name, 0.0) * np.asarray(m, dtype=float)

    weight_source = "fixed-fallback" if partial else "walk-forward"
    print(f"[engine] walk-forward 融合 (n_eval={n_eval}, source={weight_source}) "
          f"红球权重: { {k: round(v, 4) for k, v in weights_red.items()} } | "
          f"蓝球权重: { {k: round(v, 4) for k, v in weights_blue_pct.items()} }")

    if red_blend.sum() <= 0:
        red_blend = np.full(R_MAX, 1.0 / R_MAX)
    else:
        red_blend = red_blend / red_blend.sum() * 6
    blue_blend = (blue_blend / blue_blend.sum() if blue_blend.sum() > 0
                  else np.full(B_MAX, 1.0 / B_MAX))
    meta = {
        "weight_source": weight_source,
        "n_eval": int(n_eval),
        "raw_briers": raw_briers,
        "weights": {"red": {k: round(float(v), 6) for k, v in weights_red.items()},
                    "blue": {k: round(float(v), 6) for k, v in weights_blue_pct.items()}},
        "max_model_weight": MAX_MODEL_WEIGHT,
        "shrinkage": WEIGHT_SHRINKAGE,
        "fallback_methods": {"red": fallback_red, "blue": fallback_blue},
    }
    return _with_meta(red_blend, meta), blue_blend


def _renormalize(weights: Dict[str, float]) -> Dict[str, float]:
    """把权重表归一到 sum=1（空表或全 0 时回退等权）。"""
    tot = sum(v for v in weights.values() if v > 0)
    if tot <= 0:
        n = len(weights) or 1
        return {k: 1.0 / n for k in weights}
    return {k: (v / tot if v > 0 else 0.0) for k, v in weights.items()}


def _apply_adaptive_red(red_blend: np.ndarray, adaptive: Optional[Dict],
                        red_pool: Dict) -> np.ndarray:
    """把自适应权重施加到红球融合上（blue 侧已在权重表打折）。"""
    if not adaptive:
        return red_blend
    out = np.zeros_like(red_blend, dtype=float)
    for name, m in red_pool.items():
        out += adaptive.get(name, 1.0) * m
    return out if out.sum() > 0 else red_blend


# ---------- 主流程 ----------

def build_context(draws: List[Dict], stats: Dict, patterns: List[Dict]) -> Dict:
    return {
        "stats": stats,
        "recent": stats.get("recent", []),
        "patterns": [
            {
                "key": p.get("key"), "name_zh": p.get("name_zh"), "kind": p.get("kind"),
                "grade": p.get("grade"), "margin": p.get("margin"),
                "p_value": p.get("p_value"), "p_adj": p.get("p_adj"),
                "n": p.get("sample_size"),
                "desc": (p.get("desc") or "")[:80],
                "ci": [
                    p.get("backtest", {}).get("ci_lower"),
                    p.get("backtest", {}).get("ci_upper"),
                ],
            }
            for p in patterns if p.get("grade") in ("A", "B")
        ],
        "feedback": _last_feedback(draws),
        "constraints": constraint_ctx(draws),
        # V7：把「阶段时效 + 验证结论」真正交给 LLM，而不是只给 A/B 级战绩。
        # usable=可用于出号；monitor=只观察（新兴/衰减/陈旧）；rejected=不可用。
        "regime_rules": _regime_rules_for_prompt(draws),
    }


def _regime_rules_for_prompt(draws: List[Dict], limit: int = 8) -> List[Dict]:
    """规律运行时快照 → LLM 可读的规律清单（含阶段状态与可信度分层）。"""
    try:
        from . import patterns_runtime
        snap = patterns_runtime.load_snapshot(current_issue=draws[-1]["issue"] if draws else None)
        return patterns_runtime.rules_for_prompt(snap, limit=limit)
    except Exception:  # noqa: BLE001
        return []


def _last_feedback(draws: List[Dict]) -> Optional[Dict]:
    """上期预测 vs 实际开奖的命中摘要（供 LLM 回馈下一期，M3.2）。"""
    if len(draws) < 2:
        return None
    last = draws[-1]
    try:
        preds = db.load_predictions(last["issue"])
    except Exception:  # noqa: BLE001
        preds = []
    if not preds:
        return None
    try:
        from . import evaluate as EV
        res = EV.tickets_result(preds, last)
        return {
            "issue": last["issue"],
            "n_tickets": len(preds),
            "avg_red_hits": round(float(np.mean(res["red_hits"])), 2),
            "blue_hits": int(sum(res["blue_hits"])),
            "best_level": res["best_level"],
            "reward": round(res["reward"], 1),
        }
    except Exception:  # noqa: BLE001
        return None


# ---------- V7：规律 → 出号（运行时快照驱动） ----------

def _rules_hit(history: List[Dict], spec: Dict) -> Optional[set]:
    """按维度筛选条件求「本期该规律的候选号码集合」（严格 as-of：只用 history）。"""
    try:
        from . import discovery
        key = str((spec or {}).get("key") or "")
        if not key:
            return None
        flt = discovery.make_filter(key, (spec or {}).get("params") or {})
        return set(int(x) for x in (flt["selector"](list(history)) or set()))
    except Exception:  # noqa: BLE001 —— 规律不可用不应影响出号
        return None


def _norm_sorted_reds(t: Dict) -> tuple:
    """票的红球签名：必须**排序后**比较。

    ``evidence_by_ticket`` 用 ``tuple(t["reds"])`` 当键，而候选的红球可能未排序
    （LLM 输出、或规律命中的号码集合本身是无序的）。不统一排序会导致
    "第 2 次应用规律时找不到证据"，表现为重复加成、引用丢失——本轮实测踩到。
    """
    return tuple(sorted(int(x) for x in (t.get("reds") or [])))


def _rule_matches_ticket(t: Dict, rule: Dict, hits: set) -> int:
    if rule.get("target") == "blue":
        return 1 if int(t.get("blue") or 0) in hits else 0
    return len(set(int(x) for x in t.get("reds") or []) & hits)


def _rule_noise_explainer(counts: Dict) -> str:
    """把「为什么没有可用规律」翻译成人能读的原因（用户反馈第 3 条）。

    只说"0 条可用"会让系统看起来像没工作；实际上它是三道门槛全部如实执行的结果，
    这里把门槛说出来，并给出"下一步做什么"。
    """
    n_rej = int((counts or {}).get("rejected", 0) or 0)
    n_mon = int((counts or {}).get("monitor", 0) or 0)
    total = n_rej + n_mon
    if not total:
        return ("原因：本轮扫描（91 个维度：频率×6 个时间尺度/遗漏/回补比/重号/邻号/同尾/"
               "三区/派生号/共现对/开奖日/形态条件/蓝球 6 类）没有产出任何带阶段证据的线索。"
               "点「AI 规律闭环」可重新跑一轮。")
    return (f"原因：快照里 {total} 条线索全部停在门槛之前 —— 需**同时**满足"
            "① 至少一个阶段在 BH 校正后显著 ② 最近 150 期加权仍显著 ③ "
            "A/B 实战验证同行优于基线。目前它们都只满足 0~2 条"
            "（如重号：近 200 期均值 1.084 vs 理论 1.091，bootstrap 95% 置信区间 "
            "[1.0555, 1.1110] 覆盖了理论值，与随机不可区分）。"
            "这些线索仍列在「规律时效」页，可逐个看阶段画像。")


def apply_rules_to_candidates(candidates: List[Dict], history: List[Dict],
                              snapshot: Optional[Dict]) -> Dict:
    """把「可用规律」作用到候选上，并返回**可审计的影响描述**。

    三种结果（``mode``）：
      * ``off``        —— 没有快照 / 没有 usable 规律（系统如实说明"本期无可用规律"）；
      * ``shadow``     —— 已算出规律偏置，但**不改号码**，只记录"若生效会怎样"
                           （哪些注被顶替、置信度变化多少、本期命中会怎么变）；
      * ``active``     —— 规律真正改变候选排序与入选注（``LOTT_RULE_APPLY=1``）。

    偏置强度刻意温和：每条规律把该注的置信度提高 ``RULE_CONF_BOOST``，
    且**只在候选池内部重排**，不凭空生成号码——规律不能凌驾于概率模型之上。
    """
    from . import patterns_runtime

    out: Dict = {"mode": "off", "usable_rules": [], "matched": {}, "changed": False,
                 "swapped": [], "note_zh": "本期没有可用规律（未通过阶段验证），出号等同纯模型基线"}
    if not snapshot:
        return out
    usable = patterns_runtime.usable_rules(snapshot)
    snap_stale = bool(snapshot.get("stale"))
    if not usable:
        counts = snapshot.get("counts") or {}
        out["note_zh"] = (
            f"本期无可用规律：规律快照 0 条通过「阶段持续有效 + A/B 实战验证」，"
            f"另有 {int(counts.get('monitor', 0))} 条仅监控、"
            f"{int(counts.get('rejected', 0))} 条已被否决"
            + ("；规律快照已过期，建议刷新" if snap_stale
               else "。" + _rule_noise_explainer(counts))
        )
        if snap_stale:
            out["note_zh"] += "（规律快照已过期，需刷新）"
        out["snapshot"] = {"counts": counts, "stale": snap_stale,
                           "generated_at": snapshot.get("generated_at_text")}
        return out

    hits_by_rule: Dict[str, set] = {}
    usable_rows = []
    for r in usable:
        hits = _rules_hit(history, r.get("dim") or {})
        if hits is None:
            continue
        hits_by_rule[str(r.get("id"))] = hits
        usable_rows.append(r)
    if not usable_rows:
        out["note_zh"] = "规律快照里的可用规律维度当前无法求值（维度不可用），出号等同基线"
        return out

    before = [(tuple(t["reds"]), t["blue"], float(t.get("confidence") or 0)) for t in candidates]
    # 原始排序键必须**在加成之前**取值：``list(candidates)`` 只是浅拷贝，
    # 元素仍是同一个 dict，加成写回后读到的就不是原值了（会导致加成被重复计权）。
    _orig_conf = {(_norm_sorted_reds(t), int(t.get("blue") or 0)): float(t.get("confidence") or 0)
                  for t in candidates}
    _orig_pos = {k: i for i, k in enumerate(_orig_conf)}
    placement_before = [(_norm_sorted_reds(t), int(t.get("blue") or 0)) for t in candidates]
    evidence_by_ticket: Dict[tuple, List[str]] = {}
    for t in candidates:
        boost = 0.0
        refs: List[str] = []
        for r in usable_rows:
            hits = hits_by_rule[str(r.get("id"))]
            n = _rule_matches_ticket(t, r, hits)
            if n <= 0:
                continue
            gain = config.RULE_CONF_BOOST * n
            boost += gain
            refs.append(f"{r.get('statement', '')[:36]}（命中 {n} 个 → 置信度 +{gain:.1f}）")
        if boost:
            t["confidence"] = float(t.get("confidence") or 0) + boost
            evidence_by_ticket[(_norm_sorted_reds(t), int(t.get("blue") or 0))] = refs
    # **只有生效模式才真的排序**。影子模式必须原样返回候选顺序——它是"若生效会怎样"的
    # 模拟，不能反过来影响本期号码；也不能让调用方误以为候选已被规律改动。
    # 排序键是"原始置信度 + 规律加成"，而不是已经加成的置信度——
    # 否则同一条规律会被重复计权（加成本身已写回 t["confidence"]），
    # 而且结果会依赖候选的初始顺序，违背"规律只是温和偏置"的约定。
    if patterns_runtime.apply_enabled():
        # 稳定性：同分时保持原始顺序（否则 50.0+10 与 10.0+50 这类巧合会互换，
        # 让"规律造成了几注重排"变成随机噪声）。
        candidates.sort(key=lambda t: (
            -(_orig_conf.get((_norm_sorted_reds(t), int(t.get("blue") or 0)), 0.0) + float(t.get("confidence") or 0)),
            _orig_pos.get((_norm_sorted_reds(t), int(t.get("blue") or 0)), 10 ** 6)))
    after = [(tuple(t["reds"]), t["blue"], float(t.get("confidence") or 0)) for t in candidates]

    out.update({
        "mode": "active" if patterns_runtime.apply_enabled() else "shadow",
        "usable_rules": [{"id": r.get("id"), "statement": r.get("statement"),
                          "dim": r.get("dim"), "regime": r.get("regime")} for r in usable_rows],
        "matched": {k: sorted(v) for k, v in hits_by_rule.items()},
        "changed": before != after,
        "evidence_by_ticket": evidence_by_ticket,
        "snapshot": {"counts": snapshot.get("counts"), "stale": snap_stale,
                     "generated_at": snapshot.get("generated_at_text")},
    })
    # 规律的真实影响有两个维度，必须分开报，否则会得出错误结论：
    #   · boosted —— 有多少注的置信度被规律抬高（"规律命中了这些注"）；
    #   · resorted —— 有多少注的**排序位置**因此变化（"规律真的改变了出号顺序"）。
    # 影子模式下置信度照样被算出并记录（便于回答"若生效会怎样"），但不排序。
    conf_before = {(_norm_sorted_reds(t), int(t.get("blue") or 0)): float(t.get("confidence") or 0)
                   for t in candidates}
    boosted = [k for k, refs in evidence_by_ticket.items()]
    before_order = list(placement_before)
    after_order = [(_norm_sorted_reds(t), int(t.get("blue") or 0)) for t in candidates]
    n_resorted = sum(1 for a, b in zip(before_order, after_order) if a != b)
    out["swapped"] = [{"from": list(a[0]) + [a[1]], "to": list(b[0]) + [b[1]],
                       "conf_before": round(a[2], 1), "conf_after": round(b[2], 1)}
                      for a, b in zip(before, after) if a[0] != b[0] or a[1] != b[1]][:6]
    out["n_resorted"] = n_resorted
    out["n_boosted"] = len(boosted)
    out["note_zh"] = (
        f"可用规律 {len(usable_rows)} 条（已通过阶段持续有效 + A/B 验证）；"
        + ("已生效：候选按规律证据重排" if out["mode"] == "active"
           else "影子模式：已算出规律影响但不改号码（LOTT_RULE_APPLY=0）")
        + (f"；{len(boosted)} 注被规律命中" if boosted else "；本期无候选被规律选中")
        + (f"；{n_resorted} 注排序发生变化" if n_resorted else
           ("；排序未变（规律未改变入选结果）" if boosted else ""))
    )
    if snap_stale:
        out["note_zh"] += "；⚠️ 规律快照已过期，建议刷新"
    return out


def _parse_tickets_response(res: Optional[dict], method_name: str) -> List[Dict]:
    """校验并规范化 LLM 返回的 tickets（含 evidence / counter_evidence / structure_scores）。"""
    out: List[Dict] = []
    if not res or not isinstance(res.get("tickets"), list):
        return out
    for t in res["tickets"]:
        try:
            reds = sorted(int(x) for x in t["reds"])
            blue = int(t["blue"])
            conf = float(t.get("confidence", 50))
            if len(set(reds)) != 6 or not all(1 <= x <= R_MAX for x in reds):
                continue
            if not 1 <= blue <= B_MAX:
                continue
            out.append({
                "reds": reds, "blue": blue, "method": f"llm:{method_name}",
                "confidence": conf,
                "reasoning": str(t.get("reasoning", ""))[:300],
                "patterns_used": [str(x) for x in t.get("patterns_used", [])],
                "evidence": t.get("evidence") if isinstance(t.get("evidence"), dict) else {},
                "counter_evidence": (t.get("counter_evidence")
                                     if isinstance(t.get("counter_evidence"), list) else []),
                "structure_scores": (t.get("structure_scores")
                                     if isinstance(t.get("structure_scores"), dict) else {}),
            })
        except (TypeError, ValueError):
            continue
    return out


def _pick_verify_cfg(model_cfgs: List[Dict]) -> Optional[Dict]:
    """第三轮校验模型：优先 LOTT_LLM_VERIFY_MODEL，否则回退第一个可用模型。"""
    if config.LLM_VERIFY_MODEL:
        for c in model_cfgs:
            if c.get("model") == config.LLM_VERIFY_MODEL or c.get("name") == config.LLM_VERIFY_MODEL:
                return c
    return model_cfgs[0] if model_cfgs else None


def llm_tickets(draws: List[Dict], stats: Dict, patterns: List[Dict],
                rng: random.Random, llm_samples: Optional[int] = None,
                llm_verify: Optional[bool] = None,
                deadline: Optional[float] = None,
                strict: bool = False,
                red_probs: Optional[np.ndarray] = None,
                blue_probs: Optional[np.ndarray] = None) -> List[Dict]:
    """多模型 LLM 采样生成候选（U3 角色重定位：组合结构优化，非自由猜号）。

    red_probs/blue_probs: 统计/ML 的 33/16 维概率分布，供 LLM 在硬约束下挑选
            红球（LLM 不再越权自由选号）；蓝球最终由 blue_probs（含 blue_specialist）
            加权覆盖，**LLM 的 blue 仅作提示并被引擎覆盖**。

    llm_samples: 覆盖全局 LLM_SAMPLES（离线评估可传 1 以控制成本与时长）。
    llm_verify: 是否执行第三轮校验（默认跟随 config.LLM_VERIFY_ENABLED）。
    deadline: LLM 阶段墙钟截止时间戳（time.time() 口径），钳制每次调用超时。
    strict: True 时通道不可用/超时/输出不可解析直接抛 LLMChannelError（Web 预测
            快速失败，不再降级）；False 保持原有“失败返回空列表降级统计”语义。
    """
    from concurrent.futures import ThreadPoolExecutor
    if config.LLM_DISABLED:
        if strict:
            raise LLMChannelError("LLM 已被停用（设置页「停用 LLM」或 LOTT_LLM_DISABLED=1）")
        return []
    model_cfgs = config.llm_model_list()
    if config.LLM_EVAL_MODEL:
        # 评估专用模型（LOTT_LLM_EVAL_MODEL）：评估期间限定单模型，控制成本
        filtered = [c for c in model_cfgs
                    if c.get("model") == config.LLM_EVAL_MODEL or c.get("name") == config.LLM_EVAL_MODEL]
        if filtered:
            model_cfgs = filtered
    if not model_cfgs:
        if strict:
            raise LLMChannelError("LLM 通道未配置（API 地址 / Key / 模型缺失，请在设置页配置并保存）")
        print("[llm] 无可用模型配置，跳过 LLM 通道")
        return []
    ctx = build_context(draws, stats, patterns)

    # 观察轮次：多模型轮转尝试（任一成功即用）。
    # 单模型挂掉（限流/参数不兼容/余额）不应废掉整个 LLM 通道——
    # 配置多个模型（LOTT_LLM_MODEL_LIST / LOTT_LLM_EXTRA_MODELS）即自动获得观察轮容错。
    obs_rotation: List[Dict] = []
    _seen = set()
    for c in model_cfgs:
        k = (str(c.get("base_url")), str(c.get("model")))
        if k in _seen:
            continue
        _seen.add(k)
        obs_rotation.append(c)
    obs = None
    obs_errors: List[str] = []
    for c in obs_rotation:
        try:
            obs = llm_client.chat_json(
                llm_client.SYSTEM_BASE,
                llm_client.observations_prompt(
                    llm_client.compact_stats(ctx["stats"]), ctx["recent"], ctx["patterns"],
                    feedback=ctx.get("feedback")),
                max_tokens=1600, temperature=0.7, model_cfg=c,
                deadline=deadline, strict=strict,
            )
        except LLMChannelError as e:
            obs_errors.append(f"{c.get('model') or c.get('name') or '?'}: {e}")
            obs = None
        if obs is not None:
            break
    if obs is None:
        if strict:
            if obs_errors:
                # 报错要把**全部**模型的失败原因都带上，并以主通道（列表首个）的原因开头：
                # 否则尾部模型（例如设置页换模型后残留的旧模型 404）会把真正的原因
                # （如「模型只返回推理内容、未产出正文」）掩盖掉，让人查错方向跑偏。
                raise LLMChannelError(
                    f"LLM 观察轮全部模型失败（{len(obs_errors)} 个）——"
                    + "；".join(obs_errors))
            raise LLMChannelError("LLM 观察轮返回内容无法解析为 JSON（全部模型尝试失败）")
        print(f"[llm] 观察生成失败（已尝试 {len(obs_rotation)} 个模型），跳过 LLM 通道")
        return []

    n_models = len(model_cfgs)
    tickets: List[Dict] = []

    def _ticket_call(cfg: Dict) -> List[Dict]:
        res = llm_client.chat_json(
            llm_client.SYSTEM_BASE,
            llm_client.tickets_prompt(  # noqa
                llm_client.compact_stats(ctx["stats"]), ctx["recent"], ctx["patterns"], obs,
                feedback=ctx.get("feedback"),
                red_probs=red_probs.tolist() if red_probs is not None else None,
                blue_probs=blue_probs.tolist() if blue_probs is not None else None,
                regime_rules=ctx.get("regime_rules")),
            max_tokens=2000, temperature=0.9, model_cfg=cfg,
            deadline=deadline, strict=strict,
        )
        return _parse_tickets_response(res, cfg["name"])

    n_samples = int(llm_samples) if llm_samples else config.LLM_SAMPLES
    calls = [model_cfgs[i % n_models] for i in range(n_samples)]
    call_errors: List[str] = []
    with ThreadPoolExecutor(max_workers=min(len(calls), 4)) as ex:
        futures = [ex.submit(_ticket_call, cfg) for cfg in calls]
        for f in futures:
            try:
                tickets.extend(f.result())
            except LLMChannelError as e:  # strict 模式下单个采样失败不影响其余采样
                call_errors.append(str(e))
    if strict and not tickets:
        raise LLMChannelError(call_errors[0] if call_errors
                              else "LLM 选号轮未返回任何可解析的候选")

    # 第三轮校验（M3.2）：critique → 发现问题才 refine（低温 + 校验模型）
    # 校验轮失败不致命（已有候选在手），严格模式下同样保留原候选
    if llm_verify is None:
        llm_verify = config.LLM_VERIFY_ENABLED
    if llm_verify and tickets:
        vcfg = _pick_verify_cfg(model_cfgs)
        if vcfg:
            try:
                critique = llm_client.chat_json(
                    llm_client.SYSTEM_BASE,
                    llm_client.critique_prompt(
                        llm_client.compact_stats(ctx["stats"]), ctx["recent"], ctx["patterns"],
                        ctx.get("feedback") or {}, tickets),
                    max_tokens=600, temperature=0.2, model_cfg=vcfg,
                    deadline=deadline, strict=False)
                if critique and critique.get("verdict") == "problematic":
                    refined = llm_client.chat_json(
                        llm_client.SYSTEM_BASE,
                        llm_client.refine_prompt(
                            llm_client.compact_stats(ctx["stats"]), critique, tickets,
                            feedback=ctx.get("feedback")),
                        max_tokens=2000, temperature=0.2, model_cfg=vcfg,
                        deadline=deadline, strict=False)
                    parsed = _parse_tickets_response(refined, vcfg.get("model", "verify"))
                    if parsed:
                        tickets = parsed
                        print(f"[llm] 第三轮校验已修正选号（{len(parsed)} 注，校验模型 {vcfg.get('model')}）")
            except Exception as ex:  # noqa: BLE001
                print(f"[llm] 第三轮校验异常，保留原候选: {ex}")
    # U3：LLM 不再决定蓝球 —— 蓝球由 blue_probs（含 blue_specialist）加权覆盖
    if blue_probs is not None and tickets:
        _bp = np.asarray(blue_probs, dtype=float)
        if _bp.sum() > 0:
            _bp = _bp / _bp.sum()
        for t in tickets:
            t["blue"] = _weighted_sample(_bp, 1, rng)[0]
    return tickets


def _ml_result_block(ml_entry: Optional[Dict], use_ml: bool,
                         extra: Optional[Dict] = None) -> Dict:
    """构造预测结果里的 ML 元数据块。"""
    extra = extra or {}
    if not use_ml or ml_entry is None:
        return {"enabled": bool(use_ml), "ready": ml_entry is not None, **extra}
    metrics = ml_model.get_ml_metrics(ml_entry["red"], ml_entry["blue"])
    return {
        "enabled": True,
        "ready": True,
        "red_avg_brier": metrics.get("red_avg_brier_cal"),
        "blue_avg_brier": metrics.get("blue_avg_brier_cal"),
        "red_ece": metrics.get("red_ece"),
        "blue_ece": metrics.get("blue_ece"),
        "trained_at": ml_entry.get("trained_at"),
        **extra,
    }


# ---------- U2 蓝球模式与覆盖 ----------

def _min_significant_blue_sample(base_rate: Optional[float] = None,
                                 target_rate: Optional[float] = None,
                                 z: float = 1.96) -> int:
    """反推「能把 base_rate 与 target_rate 区分开」所需的最小样本量。

    单期蓝球命中是伯努利变量（命中 1 / 未中 0），命中率估计的标准误为
    sd/sqrt(n)，其中 sd = sqrt(p*(1-p))。要让观测到的下跌不是噪声，样本量必须
    满足 z * sd / sqrt(n) <= |base_rate - target_rate|，即

        n >= (z * sd / delta)^2

    取 p = 1/16（sd ≈ 0.2421）、delta = 6.25% - 4.5% = 0.0175、z = 1.96：
    n >= (1.96 * 0.2421 / 0.0175)^2 ≈ 735 期。

    也就是说 30 期窗口（标准误 ≈ 4.42%）在数学上**根本不可能**判定
    「4.5% 与 6.25% 谁更好」——一个标准误就覆盖了整个 1.75% 的差额。
    """
    if base_rate is None:
        base_rate = getattr(config, "BLUE_RANDOM_RATE", 1.0 / B_MAX)
    if target_rate is None:
        target_rate = config.BLUE_FALLBACK_MIN_RATE
    delta = abs(float(base_rate) - float(target_rate))
    if delta <= 0:
        return 2 ** 31 - 1                     # 两者不可区分 → 永不触发
    p = min(max(float(base_rate), 1e-9), 1.0 - 1e-9)
    sd = (p * (1.0 - p)) ** 0.5
    return int(np.ceil((z * sd / delta) ** 2))


def _blue_fallback_min_sample() -> int:
    """回退判定所要求的最小样本量（期）。

    优先级：显式配置 config.BLUE_FALLBACK_MIN_SAMPLE (>0) → 统计反推值。
    结果再与 BLUE_FALLBACK_WINDOW 取大，保证修复后只会**更严**、不会更松。
    """
    window = max(int(getattr(config, "BLUE_FALLBACK_WINDOW", 30)), 1)
    explicit = int(getattr(config, "BLUE_FALLBACK_MIN_SAMPLE", 0) or 0)
    if explicit > 0:
        return max(window, explicit)
    return max(window, _min_significant_blue_sample())


def _apply_blue_mode(blue_blend: np.ndarray) -> np.ndarray:
    """按配置把蓝球概率切到目标口径：uniform 模式 / 运行命中率回退 → 均匀 16。

    蓝球单注命中理论基线恒为 1/16 = 6.25%，任何模型都无法提高其长期期望。
    本函数只做一件事：当**样本量足够**且模型长期命中率**统计显著**低于基线时，
    回退均匀 16，避免被一个确实失效的模型带偏。

    修复要点（V4）：旧实现用 BLUE_FALLBACK_WINDOW = 30 期做判定，是**在噪声上
    做决策**。30 期命中率的标准误 = sqrt(0.0625*0.9375/30) ≈ 4.42%，已远大于
    6.25% - 4.5% = 1.75% 的差额——30 期观测到 2/30（6.67%）与「真值 4.5%」或
    「真值 6.25%」都完全相容，p 值远不显著。所以本函数改为：

    1. 由 _blue_fallback_min_sample() 得到最小显著样本量（默认 ≈ 735 期，
       见 _min_significant_blue_sample 的推导）；
    2. 样本量不足 → **恒用覆盖计划**，绝不因短期波动回退；
    3. 样本量达标且命中率仍显著低于阈值 → 才回退均匀 16。

    BLUE_FALLBACK_WINDOW 保留为「至少要看多少期」的下限（向后兼容），真正决定
    是否允许触发的是最小显著样本量；可用 config.BLUE_FALLBACK_MIN_SAMPLE 显式
    覆盖（设 0 走统计反推，不支持放松到低于 window）。
    """
    if config.BLUE_MODE == "uniform":
        return np.full(B_MAX, 1.0 / B_MAX)
    window = max(int(getattr(config, "BLUE_FALLBACK_WINDOW", 30)), 1)
    rate = METH._blue_running_rate(window)
    if rate is None:
        return blue_blend
    min_n = _blue_fallback_min_sample()
    observed = window
    try:
        # _blue_running_rate 按「每期每方法」展开，用同口径估算实际样本量
        rows = METH._per_method_issue_rows(window * 50)
        observed = len(sum(rows.values(), [])) or window
    except Exception:  # noqa: BLE001 —— 样本口径不可得时保守退回窗口期数
        observed = window
    if observed < min_n:
        # 样本不足：不得在噪声上做决策（30 期标准误 4.42% >> 1.75% 差额）
        return blue_blend
    if rate < config.BLUE_FALLBACK_MIN_RATE:
        print(f"[engine] 蓝球模型近 {observed} 期命中率 {rate:.3f} < "
              f"{config.BLUE_FALLBACK_MIN_RATE}（已达最小显著样本量 {min_n}），"
              f"本周期回退均匀 16")
        return np.full(B_MAX, 1.0 / B_MAX)
    return blue_blend


def blue_coverage_plan(n_tickets: int,
                       blue_probs: Optional[np.ndarray] = None) -> List[int]:
    """蓝球覆盖计划：在固定注数预算下，决定 n 注各自应分配的蓝球号。

    ========== 为什么这是本系统唯一真实可得的改进 ==========
    双色球是独立随机事件，**任何算法都无法提高单注命中率**：
      * 红球平均命中恒为 6*6/33 = 1.0909 个/注（与用什么模型选号无关）；
      * 蓝球单注命中恒为 1/16 = 6.25%。
    一等奖需 6 红全中，概率 1/C(33,6) = 1/1_107_568，多注只能线性叠加，
    没有算法空间。所以「提高命中率」是假目标。

    真正可优化的是**多注组合的覆盖结构**：六等奖只需蓝球命中（固定 ¥5）。
    若把 n 注的蓝球**取为互不相同**，则「至少中一个蓝球」的概率从
    「n 次独立重复」变成「n 个不同号覆盖」：

        随机重复（可重复抽样）：P = 1 - (15/16)^n
        去重覆盖（本函数）    ：P = min(n, 16) / 16

    ========== 概率对照表（至少中 1 个蓝球） ==========
        n    随机重复 1-(15/16)^n   去重覆盖 min(n,16)/16   相对提升
        1         6.25%                  6.25%               0%   ← 单注无任何优化空间
        2        12.11%                 12.50%              +3.2%
        3        17.61%                 18.75%              +6.5%
        5        27.58%                 31.25%             +13.3%
       10        47.55%                 62.50%             +31.4%
       16        64.39%                100.00%             +55.3%
       20        72.47%                100.00%            (已饱和，仅成本翻倍)

    ========== 诚实结论（必须明示，不可省略） ==========
    覆盖优化提高的是**中奖频率（体感）**，**不是期望收益**：
      * 六等奖期望返奖比 = (5 * p) / (2 * n) **恒为 0.156（15.6%）**，与 n 无关
        ——因为 p 与 n 同步增长，成本也同步增长，覆盖改变的是分布而非期望；
      * n = 16 覆盖满 16 个蓝球时 p = 100%，但成本 16 注 = ¥32，而六等奖只有
        ¥5，**必然亏本 ¥27**（净回报 -84.4%）；
      * 一/二等奖为浮动奖金且概率极小，任何覆盖方案都不能把负期望变成正期望。
    因此正确口径是：**同样 10 注，「中个蓝球（¥5）」的概率从 47.6% 提到 62.5%**，
    而不是「更容易赚钱」。理性购彩。

    ========== 选取规则 ==========
    按 blue_probs **降序**取号，因此既是「最多不同号」也是「概率最高的一组号」；
    n > 16 时蓝球只有 16 个可用，超出部分按概率加权循环重复，并**优先补齐当前
    出现次数最少的号**，让重复在 16 个号之间尽量均匀。
    blue_probs 为 None / 形状非 (16,) / 含非有限值 / 和 <= 0 时回退均匀 1/16 权重。

    与旧 `_pick_blue_coverage` 的区别：旧逻辑是在**候选票池里挑**，候选不足时只能
    重复，覆盖数是候选池大小的副产物；新逻辑**先定蓝球计划，再配对红球**，
    覆盖数只由 n 决定，结构性最优。
    """
    if n_tickets <= 0:
        return []
    k = int(n_tickets)
    probs: Optional[np.ndarray]
    try:
        probs = np.asarray(blue_probs, dtype=float) if blue_probs is not None else None
    except (TypeError, ValueError):
        probs = None
    if probs is None or probs.shape != (B_MAX,) or not np.all(np.isfinite(probs)) \
            or float(probs.sum()) <= 0:
        probs = np.full(B_MAX, 1.0 / B_MAX)
    else:
        probs = np.clip(probs, 0.0, None)
        if float(probs.sum()) <= 0:
            probs = np.full(B_MAX, 1.0 / B_MAX)
    # 降序排号：概率最高的号先上；n <= 16 时天然互不相同
    order = [int(i) + 1 for i in np.argsort(-probs, kind="stable")]
    plan = order[:min(k, B_MAX)]
    if k > B_MAX:
        # 超出 16 注：按概率加权重复，优先补齐当前出现次数最少的号
        counts: Dict[int, int] = {b: 1 for b in plan}
        for _ in range(k - B_MAX):
            b = min(order, key=lambda x: (counts.get(x, 0), -float(probs[x - 1]), x))
            counts[b] = counts.get(b, 0) + 1
            plan.append(b)
    return plan


def _plan_blue_coverage(scored: List[Dict], n_tickets: int,
                        blue_probs: Optional[np.ndarray] = None) -> List[Dict]:
    """V4 single 模式：**先定蓝球覆盖计划，再配对红球**。

    实现方式是在候选池里为计划中的每个蓝球挑一注置信度最高的候选，并把该候选的
    蓝球改写为计划值（红球保持不变）。若某计划号的候选不足，则取当前置信度最高且
    未被选用的候选改写。

    候选池比注数还小时（例如候选池只有 10 注却请求 16 注）：**不凭空补齐**（不复制
    已选票），输出注数上限 = 候选池大小 m。但覆盖计划继续生效——此时只取计划的
    前 m 个蓝球，仍保证这 m 注的蓝球**互不相同**，覆盖恰为 min(m, 16) 个号。
    这正是旧 `_pick_blue_coverage` 做不到的：它的覆盖数是候选池里「恰好有哪些蓝球」
    的副产物，而本函数的覆盖数只由 m 决定。

    因此「至少中一个蓝球」的概率严格等于 min(m, 16)/16（m = 实际输出注数）。
    """
    if n_tickets <= 0 or not scored:
        return []
    # 候选池大小即输出注数上限：不复制已选票、不凭空补齐
    m = min(int(n_tickets), len(scored))
    plan = blue_coverage_plan(m, blue_probs)
    ordered = sorted(scored, key=lambda x: -float(x.get("confidence", 0.0)))
    picked: List[Dict] = []
    chosen: set = set()
    for b in plan:
        if len(picked) >= m:
            break
        hit = next((t for t in ordered if t["blue"] == b and id(t) not in chosen), None)
        if hit is None:
            # 候选池里没有该蓝球 → 取当前置信度最高且未被选用的候选，改写其蓝球
            hit = next((t for t in ordered if id(t) not in chosen), None)
        if hit is None:
            break                      # 候选池耗尽：不复制已选票
        chosen.add(id(hit))
        t = dict(hit)
        t["blue"] = int(b)
        t["blue_planned"] = True
        picked.append(t)
    return picked


def _pick_blue_coverage(scored: List[Dict], n_tickets: int) -> List[Dict]:
    """picked 阶段：优先让 n 注尽量覆盖 n 个不同蓝球（U2 蓝球覆盖优先）。

    先按置信度降序尽量每个蓝球只取 1 注铺满 n；铺不满（候选池不足）再放宽
    允许同蓝重复补齐（与旧逻辑同蓝上限 max(2, n//5) 对齐兜底）。

    注：**同一张候选票绝不可被选两次**。候选池按 (reds, blue) 去重后，
    若放宽同蓝上限时重新遍历到已选中的票，会凭空复制出重复注——既浪费投注
    金额，也虚增注数（实测 10 注档可产出 3 注重复）。故第二遍按对象身份
    跳过已选票。
    """
    if n_tickets <= 0:
        return []
    if not scored:
        return []
    ordered = sorted(scored, key=lambda x: -x["confidence"])
    picked: List[Dict] = []
    used_blue = set()
    # 第一遍：每个蓝球取置信度最高的一注
    for t in ordered:
        if len(picked) >= n_tickets:
            break
        if t["blue"] in used_blue:
            continue
        used_blue.add(t["blue"])
        picked.append(t)
    # 第二遍：不足则放宽同蓝上限补齐（跳过已选票，避免重复）
    if len(picked) < n_tickets:
        cap = max(2, n_tickets // 5)
        blue_count: Dict[int, int] = {}
        for t in picked:
            blue_count[t["blue"]] = blue_count.get(t["blue"], 0) + 1
        chosen = {id(t) for t in picked}
        for t in ordered:
            if len(picked) >= n_tickets:
                break
            if id(t) in chosen:          # 已选过的候选票不再重复计入
                continue
            if t["blue"] in used_blue and blue_count.get(t["blue"], 0) >= cap:
                continue
            if t["blue"] not in used_blue:
                used_blue.add(t["blue"])
            blue_count[t["blue"]] = blue_count.get(t["blue"], 0) + 1
            chosen.add(id(t))
            picked.append(t)
    return picked[:n_tickets]


def _pick_legacy_blue(scored: List[Dict], n_tickets: int) -> List[Dict]:
    """旧版蓝球分散：同蓝上限 max(2, n//5)。仅在 LOTT_BLUE_COVER=0 时使用（向后兼容）。"""
    if n_tickets <= 0 or not scored:
        return []
    ordered = sorted(scored, key=lambda x: -x["confidence"])
    picked: List[Dict] = []
    blue_count = {}
    for t in ordered:
        if len(picked) >= n_tickets:
            break
        if blue_count.get(t["blue"], 0) >= max(2, n_tickets // 5):
            continue
        blue_count[t["blue"]] = blue_count.get(t["blue"], 0) + 1
        picked.append(t)
    return picked


# ---------- V4-C：奖金期望模型（覆盖优化的目标函数） ----------
#
# 奖级规则（中国福利彩票双色球官方口径，与 lottery/evaluate.py:prize_level 一致）：
#   一等奖 6 红 + 蓝      二等奖 6 红           三等奖 5 红 + 蓝
#   四等奖 5 红 / 4 红+蓝  五等奖 4 红 / 3 红+蓝  六等奖 蓝（3 红以下也可能六等奖）
# 单注成本固定 ¥2。
#
# **浮动奖金诚实声明**：一/二等奖为浮动奖（奖池分配），既无固定值也不可预知。
# 本模块强制要求调用方传入一个**明确的假设常量**，并在返回结果里以
# `jackpot_assumption` 字段原样标注，绝不假装知道真实金额。默认假设取自
# evaluate.PRIZE_CASH 的历史均值近似（一等 ¥6,500,000 / 二等 ¥180,000）。
TICKET_COST = 2.0
PRIZE_CASH_FIXED = {3: 3000.0, 4: 200.0, 5: 10.0, 6: 5.0}   # 固定奖级（元）
JACKPOT_ASSUMPTION = {
    "first": 6_500_000.0,    # 一等奖浮动奖假设（历史均值近似，非真实值）
    "second": 180_000.0,     # 二等奖浮动奖假设（历史均值近似，非真实值）
    "source": "evaluate.PRIZE_CASH 历史均值近似；一/二等奖为浮动奖，此处仅为假设常量",
}


def expected_prize(reds: List[int], blue: int,
                   jackpot: Optional[Dict[str, float]] = None) -> Dict:
    """单注期望奖金（元）与其奖级概率分解。

    对 6 红 + 1 蓝的单注，枚举「实际开奖的红球命中数 r」的分布并求和：
    实际开奖的 6 个红球是从 33 个中均匀取出的 6 个，预测的 6 个红球中命中 r 个的
    概率服从超几何分布

        P(r) = C(6, r) * C(27, 6-r) / C(33, 6)

    蓝球命中概率恒为 1/16（独立）。于是各奖级概率为 P(r) × p_blue 的组合。

    诚实边界：任何「选号算法」都无法改变 P(r) 与 p_blue——它们只由组合数学决定。
    本函数的作用是给出**目标函数**，让「覆盖优化」比的是期望值而不是幻觉命中率。
    """
    from math import comb
    jp = dict(JACKPOT_ASSUMPTION)
    if jackpot:
        jp.update({k: float(v) for k, v in jackpot.items() if k in ("first", "second")})
    total = comb(33, 6)
    p_blue = 1.0 / B_MAX
    cash = dict(PRIZE_CASH_FIXED)
    cash[1] = float(jp["first"])
    cash[2] = float(jp["second"])
    ev = 0.0
    prob_by_level: Dict[int, float] = {}
    for lvl in range(1, 7):
        prob_by_level[lvl] = 0.0
    prob_by_hit: Dict[int, float] = {}
    for r in range(0, 7):
        p_r = comb(6, r) * comb(27, 6 - r) / total
        prob_by_hit[r] = p_r
        # 与 evaluate.prize_level 完全一致的奖级判定
        if r == 6:
            prob_by_level[1] += p_r * p_blue
            prob_by_level[2] += p_r * (1.0 - p_blue)
        elif r == 5:
            prob_by_level[3] += p_r * p_blue
            prob_by_level[4] += p_r * (1.0 - p_blue)
        elif r == 4:
            prob_by_level[4] += p_r * p_blue
            prob_by_level[5] += p_r * (1.0 - p_blue)
        elif r == 3:
            prob_by_level[5] += p_r * p_blue
            prob_by_level[6] += p_r * (1.0 - p_blue)
        else:
            prob_by_level[6] += p_r * p_blue
    for lvl, p in prob_by_level.items():
        ev += p * cash.get(lvl, 0.0)
    return {
        "expected_reward": ev,
        "cost": TICKET_COST,
        "ev_ratio": ev / TICKET_COST,
        "prob_by_level": prob_by_level,
        "prob_by_red_hits": prob_by_hit,
        "blue_hit_prob": p_blue,
        "jackpot_assumption": dict(jp),
        "prize_cash": cash,
    }


def _portfolio_blue_hit_prob(blues: List[int]) -> float:
    """一组蓝球的「至少中 1 个蓝球」概率 = 不同蓝球数 / 16（独立均匀开奖）。"""
    if not blues:
        return 0.0
    return min(len(set(int(b) for b in blues)), B_MAX) / float(B_MAX)


def portfolio_expected_value(tickets: List[Dict],
                             jackpot: Optional[Dict[str, float]] = None) -> Dict:
    """整组注单的期望奖金与覆盖指标。

    返回 {expected_reward, cost, ev_ratio, blue_coverage_count,
          blue_hit_prob, prize_prob_by_level, jackpot_assumption}。

    **同注数下期望奖金恒等**：因为每注的红球命中分布都是同一个超几何分布、
    蓝球命中恒为 1/16，n 注的期望严格是单注的 n 倍——覆盖优化**不改变期望**，
    只改变「中奖频率的分布」（例如把「至少中 1 个蓝球」的概率从
    1-(15/16)^n 提到 min(n,16)/16）。这是本项目最重要的诚实边界：
    同样 10 注，中个蓝球的概率从 47.6% 提到 62.5%，但期望返奖比始终是 15.6%。
    """
    tickets = list(tickets or [])
    n = len(tickets)
    one = expected_prize(tickets[0]["reds"] if n else [],
                         int(tickets[0]["blue"]) if n else 1, jackpot=jackpot)
    blues = [int(t["blue"]) for t in tickets]
    distinct = len(set(blues))
    ev = one["expected_reward"] * n
    cost = TICKET_COST * n
    # 至少中一个蓝球：随机重复 vs 去重覆盖（对照展示）
    p_naive = 1.0 - (1.0 - 1.0 / B_MAX) ** n if n > 0 else 0.0
    p_cover = _portfolio_blue_hit_prob(blues)
    return {
        "tickets": n,
        "expected_reward": ev,
        "cost": cost,
        "net": ev - cost,
        "ev_ratio": (ev / cost) if cost > 0 else 0.0,
        "blue_coverage_count": distinct,
        "blue_hit_prob": p_cover,
        "blue_hit_prob_naive": p_naive,
        # 六等奖（蓝球命中 ¥5）期望返奖比：(5*p)/(2*n) —— 与 p/n 口径同构
        "sixth_ev_ratio": (cash6 := 5.0) and ((cash6 * p_cover) / cost if cost > 0 else 0.0),
        # 全部注单的奖级概率（取并集的上界近似：不同注之间不独立，此处按独立求和）
        "prize_prob_by_level": {k: v * n for k, v in one["prob_by_level"].items()},
        "prize_prob_by_level_note": "按各注独立求和的上界近似，非精确联合分布",
        "jackpot_assumption": one["jackpot_assumption"],
        "prize_cash": one["prize_cash"],
    }


# ===========================================================================
# V10-03 票单分布（解析计算，**不是模拟**）
# ===========================================================================
#
# 为什么放在 engine.py 而不是 rigor.py：rigor.py 刻意不 import engine（避免
# ``battle → engine → rigor → engine`` 循环），而票单层语义（奖金假设、注数、
# blue_coverage_plan）本来就都住在 engine.py。见 docs/V10 §3.4 落点变更说明。
#
# ---------- 数学（全部闭式；测试用 C(33,6)=1,107,568 全枚举交叉验证） ----------
#
# 单注红球命中数 K ~ Hypergeometric(N=33, K=6, n=6)：
#     E[K]   = 6*6/33 = 12/11                = 1.0909090909…
#     Var(K) = 729/968                       = 0.7530991736…   (sd ≈ 0.867813)
# 第 i、j 两注的红球交集大小为 A_ij，则
#     Cov(K_i, K_j) = (27/176)*A_ij - 81/484
# 因此整组命中总数 T = Σ_i K_i：
#     Var[T]    = n*σ₁² + (27/88)*Σ_{i<j}A_ij - (81/242)*C(n,2)
#     var_ratio = Var[T]/(n*σ₁²)
#               = 1 + (n-1)*(11/54)*(Ā - 12/11),   Ā = Σ_{i<j}A_ij / C(n,2)
#
# ---------- 三个必须记住的边界 ----------
# 1. **var_ratio 恒 ≥ 0，下界恰为 0**：n=11 且每个红号恰出现 2 次时 Var[T]=0。
#    它**不是**普通"方差比"，而是协方差结构的线性泛函；UI 必须同时给出
#    var_T 与 nσ₁² 两个正数，不能只显示 var_ratio。
# 2. Ā 的中性值是 **12/11 ≈ 1.0909**（此时 var_ratio=1），**不是 0**；
#    Ā=0 只在 n ≤ 5（5 注互不相交）时可行，6 注起需要 36 个号 > 33。
# 3. 改变票单分布**不改变期望**（EV 恒为 n × 单注 EV），只改变
#    （a）命中总数方差、（b）「至少一注命中 ≥r 红」的概率。任何把目标写成
#    「提高期望」的实现都是错的。

RED_PER_TICKET = 6
SIGMA1_SQ = 729.0 / 968.0                       # 单注红球命中数方差
E_HITS_PER_TICKET = RED_PER_TICKET * RED_PER_TICKET / float(R_MAX)   # 12/11
VAR_RATIO_SLOPE = 11.0 / 54.0                   # 闭式斜率 (n-1)*(11/54)
REDUNDANCY_NEUTRAL = 12.0 / 11.0                # Ā 中性值：var_ratio == 1
REDUNDANCY_MAX = float(RED_PER_TICKET)          # Ā 的上界（两注完全相同）
EV_RATIO_SIXTH = (5.0 / B_MAX) / TICKET_COST    # 0.15625（仅六等奖，与 n 无关）

# 奖金假设表（一/二等奖为浮动奖，只能给"假设常量"，必须随结果一起展示）
PRIZE_ASSUMPTION_TABLE_A = {
    "label": "V4 假设表 A",
    "first": 5_000_000.0,          # 一等奖浮动奖假设（V4_ACCURACY_DIAGNOSIS.md:177-187）
    "second": 150_000.0,           # 二等奖浮动奖假设
    "note": "一/二等奖为浮动奖，此处为假设常量（V4 口径），非真实金额",
}
PRIZE_ASSUMPTION_TABLE_B = {
    "label": "V4 假设表 B（= engine.JACKPOT_ASSUMPTION）",
    "first": float(JACKPOT_ASSUMPTION["first"]),
    "second": float(JACKPOT_ASSUMPTION["second"]),
    "note": "一/二等奖为浮动奖，此处为历史均值近似假设常量，非真实金额",
}

_POPCOUNT_LUT: Optional[np.ndarray] = None
_ALL_RED_MASKS: Optional[np.ndarray] = None
_EXACT_TAIL_CACHE: Dict = {}
_EXACT_TAIL_CACHE_MAX = 64


def _popcount_lut() -> np.ndarray:
    global _POPCOUNT_LUT
    if _POPCOUNT_LUT is None:
        _POPCOUNT_LUT = np.array([bin(i).count("1") for i in range(2048)], dtype=np.uint8)
    return _POPCOUNT_LUT


def _popcount33(x: np.ndarray) -> np.ndarray:
    """33 位掩码的 popcount（拆 3 段 11 位查表，避免建 2^33 表）。"""
    lut = _popcount_lut()
    return (lut[x & 2047] + lut[(x >> 11) & 2047] + lut[(x >> 22) & 2047]).astype(np.int8)


def _red_mask(reds) -> int:
    m = 0
    for r in reds:
        m |= 1 << (int(r) - 1)
    return m


def _all_red_masks() -> np.ndarray:
    """C(33,6) 全部可能开奖红球组合的位掩码（1,107,568 项 ≈ 8.9MB，惰性构建一次）。"""
    global _ALL_RED_MASKS
    if _ALL_RED_MASKS is None:
        import itertools
        combos = np.array(list(itertools.combinations(range(R_MAX), RED_PER_TICKET)), dtype=np.int64)
        masks = np.bitwise_or.reduce(np.left_shift(np.int64(1), combos), axis=1)
        _ALL_RED_MASKS = masks.astype(np.uint64)
    return _ALL_RED_MASKS


def _validate_tickets(tickets) -> List[Dict]:
    """规范化为 ``[{"reds": [...6 个升序唯一红号...], "blue": int}]``；非法输入抛 ValueError。"""
    out: List[Dict] = []
    for idx, t in enumerate(tickets or []):
        reds = t.get("reds") if isinstance(t, dict) else None
        if not isinstance(reds, (list, tuple)) or len(reds) != RED_PER_TICKET:
            raise ValueError(f"第 {idx} 注 reds 必须是 {RED_PER_TICKET} 个红号，实际 {reds!r}")
        try:
            rs = sorted(int(x) for x in reds)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"第 {idx} 注 reds 含非整数：{reds!r}") from exc
        if len(set(rs)) != RED_PER_TICKET:
            raise ValueError(f"第 {idx} 注红号有重复：{rs}")
        if rs[0] < 1 or rs[-1] > R_MAX:
            raise ValueError(f"第 {idx} 注红号越界（应在 1..{R_MAX}）：{rs}")
        blue = t.get("blue") if isinstance(t, dict) else None
        try:
            b = int(blue) if blue is not None else 0
        except (TypeError, ValueError):
            b = 0
        out.append({"reds": rs, "blue": b})
    return out


def _pairwise_stats(reds_list: List[List[int]]) -> Dict:
    """两两交集统计：ΣA_ij、Ā、并集覆盖、冗余度。O(n²)，热路径可用。"""
    n = len(reds_list)
    sets = [set(r) for r in reds_list]
    union: set = set()
    for s in sets:
        union |= s
    sum_a = 0
    for i in range(n):
        si = sets[i]
        for j in range(i + 1, n):
            sum_a += len(si & sets[j])
    n_pairs = n * (n - 1) // 2
    abar = (sum_a / n_pairs) if n_pairs else 0.0
    return {
        "n": n,
        "sum_a": int(sum_a),
        "n_pairs": int(n_pairs),
        "abar": float(abar),
        "union_coverage": int(len(union)),
    }


def _variance_from_abar(n: int, abar: float) -> float:
    """Var[T] = n·σ₁² + (27/88)·ΣA − (81/242)·C(n,2)。"""
    sum_a = abar * (n * (n - 1) // 2)
    return (n * SIGMA1_SQ + (27.0 / 88.0) * sum_a
            - (81.0 / 242.0) * (n * (n - 1) // 2))


def _var_ratio(n: int, abar: float) -> float:
    """var_ratio = 1 + (n−1)·(11/54)·(Ā − 12/11)；恒 ≥ 0（下界 0）。"""
    if n <= 0:
        return 0.0
    return 1.0 + (n - 1) * VAR_RATIO_SLOPE * (abar - REDUNDANCY_NEUTRAL)


def _variance_stats(n: int, sum_a: int) -> Dict[str, float]:
    """用**有理数精确**算 ``Var[T]`` 与 ``var_ratio``，再转 float。

    为什么不用浮点闭式：两个边界值会在浮点下"擦伤"——n=11（每号恰 2 次）时
    ``Var[T]`` 恰为 0，但闭式 ``1+(n−1)(11/54)(Ā−12/11)`` 算出来是 2.2e-16；
    Ā=12/11 时 var_ratio 恰为 1，闭式是 1.0000000000000002。测试要求
    ±1e-9 甚至精确 0/1，所以这里走 ``fractions.Fraction``（n≤33，开销可忽略）。
    """
    from fractions import Fraction
    n = int(n)
    if n <= 0:
        return {"var_T": 0.0, "var_ratio": 0.0, "n_sigma1_sq": 0.0}
    s1 = Fraction(729, 968)
    pairs = n * (n - 1) // 2
    var_exact = n * s1 + Fraction(27, 88) * int(sum_a) - Fraction(81, 242) * pairs
    n_sig_exact = n * s1
    ratio_exact = var_exact / n_sig_exact
    # 交叉核对闭式（1+(n−1)(11/54)(Ā−12/11)）与协方差式一致
    abar_exact = Fraction(int(sum_a), pairs) if pairs else Fraction(0)
    closed = 1 + Fraction(n - 1) * Fraction(11, 54) * (abar_exact - Fraction(12, 11))
    if abs(float(closed) - float(ratio_exact)) > 1e-9:      # pragma: no cover - 纯防御
        raise AssertionError(f"var_ratio 闭式与协方差式不一致：{closed} vs {ratio_exact}")
    return {
        "var_T": float(var_exact),
        "var_ratio": float(ratio_exact),
        "n_sigma1_sq": float(n_sig_exact),
    }


def _prob_at_least_r_single(r: int) -> float:
    """单注命中 ≥r 红球的超几何概率。"""
    from math import comb
    total = comb(R_MAX, RED_PER_TICKET)
    return sum(comb(RED_PER_TICKET, k) * comb(R_MAX - RED_PER_TICKET, RED_PER_TICKET - k)
               for k in range(int(r), RED_PER_TICKET + 1)) / total


def _poisson_binomial_at_least_one(ps) -> float:
    """泊松二项：**独立**事件下 P(至少一个发生) = 1 − Π(1−p_i)。

    仅当把 n 注的红球命中数当成相互独立时才成立。真实票单里票间是相关的
    （两注相同时完全相关 → 真值是 p 而不是 1−(1−p)^n；两注不相交时是负相关），
    所以它只能当**热路径近似**用，精确值走 :func:`ticket_distribution` 的全枚举。
    """
    q = 1.0
    for p in ps:
        q *= (1.0 - float(p))
    return 1.0 - q


def _iid_tail_probs(tickets: List[Dict], r_values) -> Dict[int, float]:
    """独立（iid）近似下的 P(≥1 注命中 ≥r 红)，每注边际概率相同。"""
    n = len(tickets)
    out: Dict[int, float] = {}
    for rv in r_values:
        p1 = _prob_at_least_r_single(rv)
        # 每注边际概率相同 → 泊松二项退化为 1−(1−p)^n
        out[int(rv)] = _poisson_binomial_at_least_one([p1] * n)
    return out


def _exact_red_tail(tickets: List[Dict]) -> Dict:
    """全枚举 C(33,6) 求精确 P(≥1 注命中 ≥r 红) 与 Var[T]；按票组哈希缓存。

    复杂度 O(C(33,6)·n)（n=10 约 0.1~0.2s），只在评估/报表/测试用；
    ``predict_next`` 的热路径不要调用它（用 ``with_distribution_exact=False``）。
    """
    key = tuple(sorted(tuple(int(x) for x in t["reds"]) for t in tickets))
    cached = _EXACT_TAIL_CACHE.get(key)
    if cached is not None:
        return cached
    if not key:
        cached = {"p_ge": {r: 0.0 for r in (1, 2, 3, 4, 5, 6)}, "mean_T": 0.0,
                  "var_T": 0.0, "n_draws": 0}
    else:
        masks_all = _all_red_masks()
        max_hits = np.zeros(masks_all.shape[0], dtype=np.int8)
        total_hits = np.zeros(masks_all.shape[0], dtype=np.int16)
        for reds in key:
            h = _popcount33(masks_all & np.uint64(_red_mask(reds)))
            np.maximum(max_hits, h, out=max_hits)
            total_hits += h
        cached = {
            "p_ge": {r: float((max_hits >= r).mean()) for r in (1, 2, 3, 4, 5, 6)},
            "mean_T": float(total_hits.mean()),
            "var_T": float(total_hits.var()),
            "n_draws": int(masks_all.shape[0]),
        }
    if len(_EXACT_TAIL_CACHE) >= _EXACT_TAIL_CACHE_MAX:
        _EXACT_TAIL_CACHE.clear()
    _EXACT_TAIL_CACHE[key] = cached
    return cached


def p_at_least_r_mc(tickets: List[Dict], r: int = 4, n_draws: int = 200_000,
                    seed: int = 0) -> Dict:
    """固定种子蒙特卡洛：估计 P(≥1 注命中 ≥r 红)，用于**交叉校验**解析值。

    返回 ``{"p": float, "r": int, "n_draws": int, "seed": int, "stderr": float}``。
    stderr 为标准误 ``sqrt(p(1−p)/n)``——拿它和解析值比较时才有意义：
    固定种子下结果可复现，但**不是**精确值。
    """
    ts = _validate_tickets(tickets)
    r = int(r)
    n_draws = int(n_draws)
    if n_draws <= 0 or not ts:
        return {"p": 0.0, "r": r, "n_draws": max(0, n_draws), "seed": int(seed), "stderr": 0.0}
    rng = np.random.default_rng(int(seed))
    tmasks = [np.uint64(_red_mask(t["reds"])) for t in ts]
    chunk = 100_000
    hit_any = 0
    done = 0
    while done < n_draws:
        c = min(chunk, n_draws - done)
        # 均匀随机取 6 个不同红号：对 33 个均匀 key 取最小的 6 个 ⇒ 均匀 6-子集
        keys = rng.random((c, R_MAX))
        picks = np.argpartition(keys, RED_PER_TICKET, axis=1)[:, :RED_PER_TICKET]
        draw_masks = np.bitwise_or.reduce(
            np.left_shift(np.int64(1), picks.astype(np.int64)), axis=1).astype(np.uint64)
        max_hits = np.zeros(c, dtype=np.int8)
        for tm in tmasks:
            h = _popcount33(draw_masks & tm)
            np.maximum(max_hits, h, out=max_hits)
        hit_any += int((max_hits >= r).sum())
        done += c
    p = hit_any / float(n_draws)
    return {"p": float(p), "r": r, "n_draws": n_draws, "seed": int(seed),
            "stderr": float((p * (1.0 - p) / n_draws) ** 0.5)}


def _single_ticket_ev(jackpot: Optional[Dict] = None, *, sixth: str = "published") -> Dict:
    """单注期望奖金（元）与各奖级贡献。

    ``sixth="published"``：V4 文档口径 —— 六等奖按「蓝球命中」的**边际**概率 ×¥5
    计（0.0625×5 = ¥0.3125），与高奖级存在重复计数；这是项目自 V4 起对外发表、
    docs/V10 §3.4.5 冻结的 `ev_ratio_full ≈ 0.45675` 的来源（一等 ¥5,000,000 /
    二等 ¥150,000 时单注 ¥0.9135）。
    ``sixth="strict"``：按 ``evaluate.prize_level`` 的**互斥**奖级严格计算
    （六等奖只含「蓝球命中且红球 ≤2」）；同一假设下为 ¥0.8954（0.4477），
    与上面差 ~2% —— 两者都给出，让口径可核对而不是二选一。
    """
    from math import comb
    jp = dict(PRIZE_ASSUMPTION_TABLE_A)
    if isinstance(jackpot, dict):
        for k in ("first", "second"):
            if k in jackpot:
                jp[k] = float(jackpot[k])
        if jackpot.get("label"):
            jp["label"] = str(jackpot["label"])
    total = comb(R_MAX, RED_PER_TICKET)

    def p_r(r: int) -> float:
        return comb(RED_PER_TICKET, r) * comb(R_MAX - RED_PER_TICKET, RED_PER_TICKET - r) / total

    pb = 1.0 / B_MAX
    cash = dict(PRIZE_CASH_FIXED)
    cash[1] = float(jp["first"])
    cash[2] = float(jp["second"])
    by_level = {
        1: p_r(6) * pb,
        2: p_r(6) * (1 - pb),
        3: p_r(5) * pb,
        4: p_r(5) * (1 - pb) + p_r(4) * pb,
        5: p_r(4) * (1 - pb) + p_r(3) * pb,
    }
    if sixth == "strict":
        by_level[6] = (p_r(0) + p_r(1) + p_r(2)) * pb          # 互斥：蓝球命中且红球 ≤2
    else:
        by_level[6] = pb                                        # V4 发表口径：蓝球边际概率
    contrib = {lvl: by_level[lvl] * cash.get(lvl, 0.0) for lvl in range(1, 7)}
    ev = float(sum(contrib.values()))
    return {
        "ev_yuan": ev,
        "ev_ratio": ev / TICKET_COST,
        "by_level_prob": {k: float(v) for k, v in by_level.items()},
        "by_level_contrib_yuan": {k: float(v) for k, v in contrib.items()},
        "prize_cash": {int(k): float(v) for k, v in cash.items()},
        "prize_assumption": (f"{jp['label']}: 一等 ¥{jp['first']:,.0f} / 二等 ¥{jp['second']:,.0f}"
                             f"（{'V4 六等奖按蓝球边际概率' if sixth != 'strict' else '严格互斥奖级'}）"),
        "prize_assumption_detail": jp,
    }


def ticket_distribution(tickets: List[Dict], *,
                        with_distribution_exact: bool = True,
                        r_values=(3, 4, 5),
                        jackpot: Optional[Dict] = None,
                        mc_draws: int = 0,
                        mc_seed: int = 0) -> Dict:
    """**票单分布**：一组注单的解析统计（期望 / 尾部概率 / 方差结构 / EV 口径）。

    这是「票单分布」卡的唯一数据来源（V10-10 前端消费），也是目标函数的度量工具。

    返回（全部为原生 ``float``/``int``，可直接 ``json.dumps``）：

    ==================  ===========================================================
    ``n``               注数
    ``union_coverage``  并集覆盖的红号个数（≤33）
    ``redundancy``      冗余度 Ā = Σ_{i<j}A_ij / C(n,2)，**中性值 12/11 ≈ 1.0909**
    ``redundancy_neutral``  中性值常量（=12/11，供前端并排展示）
    ``expected_hits``   命中总数期望 = n·12/11（与选号无关，是恒等式）
    ``var_T``           命中总数方差（解析，见模块注释）
    ``n_sigma1_sq``     n·σ₁²（与 var_T 一起看：var_ratio 只是二者的商）
    ``var_ratio``       var_T / (n·σ₁²) = 1+(n−1)(11/54)(Ā−12/11)；**恒 ≥ 0，下界 0**
    ``p_at_least_r``    P(≥1 注命中 ≥r 红)，键 "3"/"4"/"5"
    ``p_at_least_r_method``  ``"exact_enum"``（C(33,6) 全枚举）或 ``"iid_approx"``
    ``p_at_least_r_iid``     泊松二项（独立假设）近似值，与上面并排以便看见相关性误差
    ``p_any_blue``      P(≥1 注命中蓝球) = 不同蓝球数/16（blue_coverage_plan 闭式最优）
    ``ev_ratio_sixth``  仅六等奖的返奖比 = (5/16)/2 = **0.15625**（与 n 无关）
    ``ev_ratio_full``   整注（一~六等奖全计）返奖比 ≈ **0.45673**，**必须连
                        ``prize_assumption`` 一起展示**
    ``prize_assumption``  奖金假设表（一/二等奖是浮动奖，只能给假设常量）
    ``ev_ratio_full_strict``  同上但按互斥奖级严格算（≈0.4477），口径对照用
    ==================  ===========================================================

    ``with_distribution_exact=True``（默认）：``p_at_least_r`` 走 C(33,6) 全枚举，
    精确值，按票组哈希缓存（首次 n=10 约 0.1~0.2s）。这是本函数**唯一诚实**的口径
    ——泊松二项忽略票间相关（5 注全同时会把 0.0049 估成 0.0243，高估 5 倍）。
    ``False``：用 iid 近似（``p_at_least_r_method="iid_approx"`），热路径用；
    结果里同时给出精确/近似两列，调用方不得把近似值当结论。

    ``mc_draws>0``：额外用固定种子蒙特卡洛交叉校验，结果放 ``p_at_least_r_mc``。
    """
    ts = _validate_tickets(tickets)
    r_values = tuple(int(x) for x in r_values)
    n = len(ts)
    pair = _pairwise_stats([t["reds"] for t in ts])
    abar = pair["abar"]
    vstats = _variance_stats(n, pair["sum_a"])
    var_t = vstats["var_T"]
    n_sig = vstats["n_sigma1_sq"]
    ratio = vstats["var_ratio"]
    # var_ratio 恒 ≥ 0（n=11 且每个红号恰 2 次时恰为 0）；精确有理数下不应出现负值
    if ratio < 0.0:                                          # pragma: no cover - 纯防御
        raise AssertionError(f"var_ratio 不应为负（解析式下界 0）：{ratio}")

    iid_tail = _iid_tail_probs(ts, r_values) if n else {r: 0.0 for r in r_values}
    if with_distribution_exact and n:
        exact = _exact_red_tail(ts)
        tail = {r: float(exact["p_ge"].get(r, 0.0)) for r in r_values}
        method = "exact_enum"
        enum_var_t = float(exact["var_T"])
    else:
        tail = dict(iid_tail)
        method = "iid_approx"
        enum_var_t = None

    blues = [int(t["blue"]) for t in ts if 1 <= int(t["blue"]) <= B_MAX]
    distinct_blue = len(set(blues))
    p_any_blue = min(distinct_blue, B_MAX) / float(B_MAX)

    ev_pub = _single_ticket_ev(jackpot, sixth="published")
    ev_strict = _single_ticket_ev(jackpot, sixth="strict")
    ev_b = _single_ticket_ev(dict(PRIZE_ASSUMPTION_TABLE_B), sixth="published")

    out = {
        "n": int(n),
        "union_coverage": int(pair["union_coverage"]),
        "redundancy": float(abar),
        "redundancy_neutral": float(REDUNDANCY_NEUTRAL),
        "redundancy_sum": float(pair["sum_a"]),
        "redundancy_max": float(REDUNDANCY_MAX),
        "expected_hits": float(n * E_HITS_PER_TICKET),
        "sigma1_sq": float(SIGMA1_SQ),
        "n_sigma1_sq": float(n_sig),
        "var_T": float(var_t),
        "var_ratio": float(ratio),
        "var_ratio_note": ("var_T/(nσ₁²) 恒 ≥ 0；下界 0 在 n=11 且每个红号恰出现 2 次时取到，"
                           "中性点 Ā=12/11 时 var_ratio=1。改变分布不改变期望。"),
        "p_at_least_r": {str(r): float(v) for r, v in tail.items()},
        "p_at_least_r_method": method,
        "p_at_least_r_iid": {str(r): float(v) for r, v in iid_tail.items()},
        "p_any_blue": float(p_any_blue),
        "blue_coverage_count": int(distinct_blue),
        "ev_ratio_sixth": float(EV_RATIO_SIXTH),
        "ev_ratio_full": float(ev_pub["ev_ratio"]),
        "ev_reward_yuan": float(ev_pub["ev_yuan"]),
        "prize_assumption": ev_pub["prize_assumption"],
        "prize_cash": ev_pub["prize_cash"],
        "ev_ratio_full_strict": float(ev_strict["ev_ratio"]),
        "ev_ratio_full_table_b": float(ev_b["ev_ratio"]),
        "ev_ratio_full_note": ("ev_ratio_sixth 只是六等奖口径（0.15625），整注口径见 "
                               "ev_ratio_full；一/二等奖为浮动奖，必须连 prize_assumption 一起展示"),
    }
    if mc_draws:
        mc = {str(r): float(p_at_least_r_mc(ts, r=r, n_draws=mc_draws, seed=mc_seed)["p"])
              for r in r_values}
        out["p_at_least_r_mc"] = mc
        out["p_at_least_r_mc_detail"] = {k: v for k, v in
                                         p_at_least_r_mc(ts, r=r_values[0] if r_values else 4,
                                                         n_draws=mc_draws, seed=mc_seed).items()}
    if enum_var_t is not None:
        out["var_T_exact_enum"] = float(enum_var_t)
    return out


def _expand_blue_compound(base_reds: List[int], blue_probs: np.ndarray,
                          k: int, rng: random.Random,
                          red_blend: Optional[np.ndarray] = None,
                          blue_blend: Optional[np.ndarray] = None) -> List[Dict]:
    """U2 蓝球复式：红 6 固定 + 蓝 k 个不同 → k 注。

    蓝球按 blue_probs 加权不放回抽样（蓝球覆盖 k 个不同号）。
    """
    k = max(1, min(k, B_MAX))
    blues = _weighted_sample(blue_probs, k, rng)
    out: List[Dict] = []
    for b in blues:
        t = {"reds": list(base_reds), "blue": b, "method": "blend:blue_compound"}
        if red_blend is not None and blue_blend is not None:
            t["confidence"] = min(100.0, max(1.0, round(10 + 0.25 * ensemble_mass(
                red_blend, blue_blend, t["reds"], t["blue"]), 1)))
        else:
            t["confidence"] = 50.0
        out.append(t)
    return out


def _expand_dan_tuo(dan: List[int], blue: int, n_tickets: int,
                    rng: random.Random, red_blend: np.ndarray,
                    ctx: Dict, blue_blend: Optional[np.ndarray] = None) -> List[Dict]:
    """U2 红胆拖：胆 n 个固定，围绕胆从剩余红池抽“拖”补齐 6 红，产出 n_tickets 注。

    胆来自模型红球概率 Top；拖号从去掉胆后的红池按概率加权抽样。
    """
    dan = sorted(set(dan))
    n_dan = len(dan)
    if n_dan >= 6:
        t = {"reds": dan[:6], "blue": blue, "method": "blend:dan_tuo"}
        t["confidence"] = 50.0
        return [t]
    need = 6 - n_dan
    # 剩余红池：除胆外按 red_blend 加权（仅对 remaining 建立权重，长度一致）
    remaining = [x for x in range(1, R_MAX + 1) if x not in dan]
    base_w = red_blend.astype(float).copy()
    for d in dan:
        base_w[d - 1] = 0.0
    rem_w = np.array([base_w[x - 1] for x in remaining], dtype=float)
    out: List[Dict] = []
    seen = set()
    for _ in range(n_tickets * 8):
        if len(out) >= n_tickets:
            break
        if rem_w.sum() <= 0:
            rem_w = np.ones(len(remaining))
        p = (rem_w / rem_w.sum()).tolist()
        picks = set()
        tries = 0
        while len(picks) < need and tries < 200:
            c = rng.choices(remaining, weights=p, k=1)[0]
            picks.add(c)
            tries += 1
        if len(picks) < need:
            continue
        reds = sorted(list(dan) + list(picks))
        if pass_constraints(reds, blue, ctx):
            key = tuple(reds)
            if key in seen:
                continue
            seen.add(key)
            t = {"reds": reds, "blue": blue, "method": "blend:dan_tuo"}
            if blue_blend is not None:
                t["confidence"] = min(100.0, max(1.0, round(10 + 0.25 * ensemble_mass(
                    red_blend, blue_blend, reds, blue), 1)))
            else:
                t["confidence"] = 50.0
            out.append(t)
    return out


# ---------- U4 组合覆盖优化（V10-03：可插拔目标 + 蓝球职责边界） ----------
#
# 冻结契约（V10-03 §3.4.3）：
#
#   **蓝球由 blue_coverage_plan 唯一负责（闭式最优 P(≥1蓝)=min(n,16)/16），
#   目标函数只作用于红球子问题。**
#
# 这条契约来自一个真实缺陷：V4 的 ``coverage_optimize`` 在选完红球后用
# ``plan`` 强制改写蓝球，而目标函数里却仍含 ``distinct_blue`` 项 —— 优化器
# 在为一个**马上会被覆盖掉的属性**爬坡（零梯度 + 把蓝球噪声注入红球选择）。
# 现在：``blue_probs is not None``（plan 生效）时，**任何目标都不含蓝球项**，
# 并在 ``coverage_optimize_compare()`` 的 ``objective_meta.gradient_blue_degenerate``
# 里如实回报 True；只有 plan 不生效（蓝球保持候选池原值）时，蓝球才是真实
# 自由度，才允许进入目标。
#
# 四种目标（config.COVERAGE_OBJECTIVE / 别名表 config.COVERAGE_OBJECTIVE_ALIASES）：
#
#   ev_conserving（默认，别名 expected_value / ev）
#       EV 守恒 —— 同注数下 EV 恒为 n×单注EV，与票单结构**无关**，所以目标里
#       **没有也不允许有 EV 项**；只比较红球结构：并集覆盖 − 冗余度 Ā。
#       这是唯一诚实的目标：它优化的东西（覆盖/方差结构）真实存在，
#       但**不宣称**提高期望收益。
#   pmax_high
#       直接最大化 P(≥1 注命中 ≥r 红)。**不得**用「最小化冗余度」或「最大化
#       并集覆盖」当代理：n=10 时铺开反而比 iid 低约 2.6%（票间负相关）。
#       搜索期用固定种子「公共随机数」MC 估计（噪声 <1%），结果须用
#       ``ticket_distribution()`` 给出精确值与 ``var_ratio``（方差代价）。
#   coverage
#       旧口径（V4）：红球并集 + 1.5×蓝球覆盖数，保留向后兼容。
#   custom_weight
#       用户偏好加权（P2-3 承接）：权重来自 config.COVERAGE_OBJECTIVE_WEIGHTS
#       或调用参数；项：pmax_high / union / neg_redundancy / blue_coverage。
#
# **任何模式都不许把目标写成「提高期望」。**

COVERAGE_OBJECTIVES = ("ev_conserving", "pmax_high", "coverage", "custom_weight")

# custom_weight 的默认权重（可由 config.COVERAGE_OBJECTIVE_WEIGHTS 或调用参数覆盖）
DEFAULT_CUSTOM_WEIGHTS = {
    "pmax_high": 1.0,        # P(≥1 注 ≥r 红) 估计值（0..1）
    "union": 0.5,            # 并集覆盖 / 33（0..1）
    "neg_redundancy": 0.5,   # 1 − Ā/6（0..1，1 = 完全不重叠）
    "blue_coverage": 0.0,    # 蓝球覆盖度 / 16（仅在 plan 未接管蓝球时有效）
}

# 搜索期 P(≥1注≥r红) 的公共随机数 MC 样本量（越大越准、越慢；2000 档的
# 标准误 ≈ sqrt(p(1-p)/2000) ≈ 0.003，够排序用，最终值一律用精确枚举复核）
_PMAX_SEARCH_DRAWS = 2000


def _normalize_coverage_objective(objective: Optional[str]) -> str:
    """把目标名（含 V10-00a 别名表）规范化为 4 个合法值之一，未知值回退 ev_conserving。"""
    if not objective:
        return "ev_conserving"
    key = str(objective).strip().lower()
    aliases = getattr(config, "COVERAGE_OBJECTIVE_ALIASES", {}) or {}
    resolved = aliases.get(key, key)
    return resolved if resolved in COVERAGE_OBJECTIVES else "ev_conserving"


def _custom_objective_weights(weights: Optional[Dict] = None) -> Dict[str, float]:
    """合并「默认 ← config.COVERAGE_OBJECTIVE_WEIGHTS ← 调用参数」的权重表。"""
    merged = dict(DEFAULT_CUSTOM_WEIGHTS)
    for src in (getattr(config, "COVERAGE_OBJECTIVE_WEIGHTS", None), weights):
        if not isinstance(src, dict):
            continue
        for k, v in src.items():
            if k not in merged:
                continue
            try:
                merged[k] = float(v)
            except (TypeError, ValueError):
                continue
    return merged


class _PmaxEstimator:
    """搜索期的 P(≥1注≥r红) 估计器：固定种子「公共随机数」+ 逐注命中布尔向量缓存。

    为什么不用解析式：真实概率需要对 C(33,6)=1,107,568 种开奖全枚举（约 0.1~0.3s），
    模拟退火每步都要算，放不进搜索；而泊松二项忽略票间相关（5 注全同时高估 5 倍）。
    折中：搜索用同一批随机开奖（common random numbers，方差小、候选间可比），
    最终展示值仍由 ``ticket_distribution(with_distribution_exact=True)`` 精确给出。
    """

    def __init__(self, r: int = 4, n_draws: int = _PMAX_SEARCH_DRAWS, seed: int = 0):
        self.r = int(r)
        rng = np.random.default_rng(int(seed))
        keys = rng.random((int(n_draws), R_MAX))
        picks = np.argpartition(keys, RED_PER_TICKET, axis=1)[:, :RED_PER_TICKET]
        self.draw_masks = np.bitwise_or.reduce(
            np.left_shift(np.int64(1), picks.astype(np.int64)), axis=1).astype(np.uint64)
        self._cache: Dict[tuple, np.ndarray] = {}

    def _hit_ge(self, reds) -> np.ndarray:
        key = tuple(int(x) for x in reds)
        arr = self._cache.get(key)
        if arr is None:
            arr = _popcount33(self.draw_masks & np.uint64(_red_mask(key))) >= self.r
            self._cache[key] = arr
        return arr

    def p(self, tickets: List[Dict]) -> float:
        if not tickets:
            return 0.0
        any_hit = np.zeros(self.draw_masks.shape[0], dtype=bool)
        for t in tickets:
            any_hit |= self._hit_ge(t["reds"])
        return float(any_hit.mean())


def _ticket_objective(tickets: List[Dict], objective: str = "ev_conserving", r: int = 3,
                      *, estimator: Optional[_PmaxEstimator] = None,
                      weights: Optional[Dict] = None,
                      blue_owned_by_plan: bool = False) -> float:
    """覆盖优化目标函数（V10-03 可插拔；语义见本节顶部冻结契约）。

    参数
    ----
    objective : 4 个合法值（别名经 config.COVERAGE_OBJECTIVE_ALIASES 归一）。
    r         : 「命中 ≥r 红」的 r，只有 pmax_high / custom_weight 用到。
    estimator : pmax_high / custom_weight 用的 MC 估计器；缺省时退回精确枚举
                （单次约 0.1~0.3s，仅在直接调用本函数时发生，搜索里必须传）。
    blue_owned_by_plan : True 表示蓝球马上会被 blue_coverage_plan 改写
                → **目标里绝不能含蓝球项**（否则就是零梯度项）。

    注意：**没有任何模式包含 EV/奖金项**。同注数下 EV 恒等（n×单注 EV），
    把它写进目标只会给优化器注入常数噪声，还会诱导「优化能提高期望」的错误叙事。
    """
    if not tickets:
        return 0.0
    obj = _normalize_coverage_objective(objective)
    reds_list = [[int(x) for x in t["reds"]] for t in tickets]
    pair = _pairwise_stats(reds_list)
    union = pair["union_coverage"]
    abar = pair["abar"]
    distinct_blue = len({int(t.get("blue", 0)) for t in tickets
                         if 1 <= int(t.get("blue", 0)) <= B_MAX})
    # 蓝球项只在「蓝球不被 plan 接管」时才是真实自由度
    blue_term = 0.0 if blue_owned_by_plan else (distinct_blue / float(B_MAX))

    if obj == "coverage":
        # 旧口径：红球并集 + 1.5×蓝球覆盖数；蓝球被 plan 接管时该项删除（零梯度修复）
        return float(union) + (0.0 if blue_owned_by_plan else 1.5 * distinct_blue)

    if obj == "ev_conserving":
        # EV 恒等 → 只比结构：并集覆盖越大越好、冗余度 Ā 越小越好
        score = float(union) - float(abar)
        if not blue_owned_by_plan:
            score += 1.5 * distinct_blue      # 蓝球未被 plan 接管时，覆盖数是真实可优化量
        return float(score)

    if obj == "pmax_high":
        if estimator is not None:
            return float(estimator.p(tickets))
        return float(_exact_red_tail(_validate_tickets(tickets))["p_ge"].get(int(r), 0.0))

    # custom_weight：归一化项的加权和（不含量纲混杂；不含 EV）
    w = _custom_objective_weights(weights)
    p_high = (estimator.p(tickets) if estimator is not None
              else float(_exact_red_tail(_validate_tickets(tickets))["p_ge"].get(int(r), 0.0)))
    terms = {
        "pmax_high": p_high,
        "union": union / float(R_MAX),
        "neg_redundancy": 1.0 - (abar / REDUNDANCY_MAX if REDUNDANCY_MAX > 0 else 0.0),
        "blue_coverage": blue_term,
    }
    return float(sum(w.get(k, 0.0) * v for k, v in terms.items()))


def _objective_meta(objective: str, r: int, *, blue_owned_by_plan: bool,
                    estimator: Optional[_PmaxEstimator] = None) -> Dict:
    """目标函数的自述元数据（供 compare / 前端说明「这个目标在优化什么、不优化什么」）。"""
    obj = _normalize_coverage_objective(objective)
    return {
        "objective": obj,
        "objective_input": objective,
        "r": int(r),
        "optimizes_red_structure": obj in ("ev_conserving", "pmax_high", "coverage", "custom_weight"),
        "optimizes_ev": False,          # 恒 False：任何模式都不允许把目标写成「提高期望」
        "ev_identity_note": "同注数下 EV 恒为 n×单注 EV，与票单结构无关；本目标不含 EV 项",
        "blue_owned_by_plan": bool(blue_owned_by_plan),
        # 蓝球被 plan 接管 ⇒ 目标里的蓝球项对最终输出零梯度，故已从目标中删除
        "gradient_blue_degenerate": bool(blue_owned_by_plan),
        "pmax_estimator": ("mc_common_random_numbers" if (estimator is not None
                                                          and obj in ("pmax_high", "custom_weight"))
                           else None),
        "note_zh": ("蓝球由 blue_coverage_plan 闭式负责；目标只作用于红球子问题。"
                    "pmax_high 的方差代价须用 ticket_distribution()['var_ratio'] 一并展示。"),
    }


def coverage_optimize(pool: List[Dict], n_tickets: int, r: int = 3,
                      n_blues: int = 4,
                      trials: int = 3000, rng: Optional[random.Random] = None,
                      objective: str = "expected_value",
                      blue_probs: Optional[np.ndarray] = None,
                      objective_weights: Optional[Dict] = None) -> List[Dict]:
    """U4 覆盖优化器：从候选池选 n_tickets 注（贪心初始化 + 模拟退火精修）。

    objective（V10-03 可插拔，别名经 config.COVERAGE_OBJECTIVE_ALIASES 归一）：
      - "ev_conserving" / 旧名 "expected_value"（默认）：EV 守恒 —— 目标里**没有
        EV 项**（EV 恒等），只比红球结构（并集覆盖 − 冗余度 Ā）。
      - "pmax_high"：直接最大化 P(≥1 注命中 ≥r 红)；搜索期用公共随机数 MC 估计，
        结果须配 ``ticket_distribution()`` 的精确概率与 ``var_ratio`` 展示方差代价。
      - "coverage"：旧口径（红球并集 + 蓝球覆盖数），保留向后兼容。
      - "custom_weight"：用户偏好加权（``objective_weights`` 或
        config.COVERAGE_OBJECTIVE_WEIGHTS）。

    **蓝球职责边界（冻结）**：``blue_probs`` 给定时，蓝球由
    :func:`blue_coverage_plan` 闭式决定并在最后统一改写；因此此时**任何目标里都
    不含蓝球项**（含蓝球项就是零梯度，只会往红球选择里注入蓝球噪声）。
    ``blue_probs`` 未给定时蓝球保持候选池原值，此时蓝球覆盖数才是真实自由度。

    诚实边界：本优化器改变的是**中奖频率的分布**（覆盖结构 / 方差），
    **不改变期望收益**；任何把目标写成「提高期望」的实现都是错的。
    """
    rng = rng if rng is not None else random.Random()
    pool = [dict(t) for t in pool]
    obj = _normalize_coverage_objective(objective)
    if n_tickets <= 0 or not pool:
        return []

    # 蓝球覆盖计划前置：先把蓝球按概率降序铺满（n<=16 时互不相同）
    plan = blue_coverage_plan(n_tickets, blue_probs) if blue_probs is not None else None
    blue_owned = plan is not None
    estimator = None
    if obj in ("pmax_high", "custom_weight"):
        estimator = _PmaxEstimator(r=r, n_draws=_PMAX_SEARCH_DRAWS,
                                   seed=rng.getrandbits(31))

    def _score(sel: List[Dict]) -> float:
        return _ticket_objective(sel, obj, r, estimator=estimator,
                                 weights=objective_weights,
                                 blue_owned_by_plan=blue_owned)

    if len(pool) <= n_tickets:
        out = pool[:n_tickets]
        if plan:
            for t, b in zip(out, plan):
                t["blue"] = int(b)
        return out

    # 目标退化（候选池里所有注的红球完全相同）→ 任何选择目标值相同，退火无意义
    distinct_red_sets = {tuple(sorted(int(x) for x in t["reds"])) for t in pool}
    objective_degenerate = len(distinct_red_sets) == 1

    # 贪心：逐步加入使目标函数增益最大的一注
    selected: List[Dict] = []
    remaining = list(pool)
    cur = 0.0
    while len(selected) < n_tickets and remaining:
        best, best_gain = None, None
        for t in remaining:
            sc = _score(selected + [t])
            gain = sc - cur
            if best_gain is None or gain > best_gain:
                best, best_gain = t, gain
        remaining.remove(best)
        selected.append(best)
        cur = _score(selected)

    # 模拟退火：交换单注（目标退化时跳过——判据是「极差为 0」，不是拍一个迭代数）
    current = list(selected)
    cur_score = cur
    best_sel = list(current)
    best_score = cur_score
    for it in range(0 if objective_degenerate else trials):
        if not remaining:
            remaining = [t for t in pool if t not in current]
        if not remaining:
            break
        i = rng.randrange(len(current))
        j = rng.randrange(len(remaining))
        old_t = current[i]
        new_t = remaining[j]
        current[i] = new_t
        nb_score = _score(current)
        delta = nb_score - cur_score
        # 接受更优；等分时以随温度衰减的概率接受（探索），随迭代降温
        if delta > 0 or rng.random() < (0.5 / (1 + it ** 0.6)):
            cur_score = nb_score
            if nb_score > best_score:
                best_score = nb_score
                best_sel = list(current)
        else:
            current[i] = old_t
    out = best_sel[:n_tickets]
    if plan:
        # 计划重排蓝球：保证 n<=16 时覆盖满不同蓝球（覆盖是结构性保证）
        for t, b in zip(out, plan):
            t["blue"] = int(b)
            t["blue_planned"] = True
    return out


def coverage_optimize_compare(pool: List[Dict], n_tickets: int, r: int = 3,
                              n_blues: int = 4, trials: int = 3000,
                              rng: Optional[random.Random] = None,
                              objective: str = "expected_value",
                              blue_probs: Optional[np.ndarray] = None,
                              objective_weights: Optional[Dict] = None) -> Dict:
    """覆盖优化前后对比（V4）：返回优化前的朴素基线指标与优化后的指标。

    优化前基线 = 候选池按置信度降序取前 n 注（不做任何覆盖优化）。
    返回 ``{"before": {...}, "after": {...}, "improvement": {...}, "objective": ...,
    "objective_effective": ..., "objective_meta": {...}}``。

    V10-03 新增 ``objective_meta``：
      * ``gradient_blue_degenerate=True`` —— 蓝球被 plan 接管、目标里已无蓝球项；
      * ``var_ratio_before`` / ``var_ratio_after`` —— 方差代价（pmax_high 必须展示）；
      * ``p_at_least_r_before/after`` —— 「至少一注命中 ≥r 红」，pmax_high 时用精确枚举。
    ``objective`` 字段保持调用方传入的原值（V4 兼容），规范化后的值放
    ``objective_effective``。
    """
    rng = rng if rng is not None else random.Random()
    pool = [dict(t) for t in pool]
    obj = _normalize_coverage_objective(objective)
    before = sorted(pool, key=lambda x: -float(x.get("confidence", 0.0)))[:n_tickets]
    after = coverage_optimize(pool, n_tickets, r=r, n_blues=n_blues,
                              trials=trials, rng=rng, objective=obj,
                              blue_probs=blue_probs,
                              objective_weights=objective_weights)
    b = portfolio_expected_value(before)
    a = portfolio_expected_value(after)
    plan_used = blue_probs is not None
    exact = obj == "pmax_high"
    dist_b = ticket_distribution(before, with_distribution_exact=exact, r_values=(r,))
    dist_a = ticket_distribution(after, with_distribution_exact=exact, r_values=(r,))
    meta = _objective_meta(obj, r, blue_owned_by_plan=plan_used)
    meta.update({
        "var_ratio_before": float(dist_b["var_ratio"]),
        "var_ratio_after": float(dist_a["var_ratio"]),
        "redundancy_before": float(dist_b["redundancy"]),
        "redundancy_after": float(dist_a["redundancy"]),
        "union_coverage_before": int(dist_b["union_coverage"]),
        "union_coverage_after": int(dist_a["union_coverage"]),
        f"p_at_least_{int(r)}_before": float(dist_b["p_at_least_r"][str(int(r))]),
        f"p_at_least_{int(r)}_after": float(dist_a["p_at_least_r"][str(int(r))]),
        "p_at_least_r_method": dist_a["p_at_least_r_method"],
    })
    return {
        "before": b,
        "after": a,
        "improvement": {
            "blue_coverage_count": a["blue_coverage_count"] - b["blue_coverage_count"],
            "blue_hit_prob": a["blue_hit_prob"] - b["blue_hit_prob"],
            "blue_hit_prob_relative": (
                (a["blue_hit_prob"] - b["blue_hit_prob"]) / b["blue_hit_prob"]
                if b["blue_hit_prob"] > 0 else 0.0),
            "expected_reward": a["expected_reward"] - b["expected_reward"],
            "ev_ratio": a["ev_ratio"] - b["ev_ratio"],
        },
        "objective": objective,
        "objective_effective": obj,
        "objective_meta": meta,
        "jackpot_assumption": a["jackpot_assumption"],
    }


def channels_snapshot(*, force_production: bool = False) -> Dict:
    """本期出号通道快照（V10-04b：出号决策的**唯一**读口）。

    为什么不再直读 `config.LLM_ONLY_OUTPUT`（R3 P0-4 / R1 P0-1）：
    出号口径散落在 6 层开关里（LOTT_METHODS / LOTT_METHOD_MODE /
    LOTT_LLM_ONLY_OUTPUT / LOTT_LLM_DISABLED / methods_config.json / .env），
    引擎自己再判一次必然与 `methods.resolve_channels()` 漂移——
    "族被关掉但 only 仍生效"、"research 模式偷偷出 LLM 注"都来自这种二次判定。

    取值顺序：优先用 `config.CHANNELS` 的冻结快照（设置页保存后由
    `config.refresh_channels()` 刷新）；快照缺失或不含 V10 字段时才现算。
    """
    snap = getattr(config, "CHANNELS", None)
    if force_production or not isinstance(snap, dict) or "llm_can_pick" not in snap:
        return METH.resolve_channels(force_production=force_production)
    return snap


def predict_next(draws: List[Dict], use_llm: Optional[bool] = None,
                 n_tickets: Optional[int] = None, persist: bool = True,
                 use_ml: Optional[bool] = None,
                 rng: Optional[random.Random] = None,
                 llm_samples: Optional[int] = None,
                 llm_verify: Optional[bool] = None,
                 llm_required: bool = False,
                 llm_deadline: Optional[float] = None,
                 bet_mode: Optional[str] = None,
                 coverage: bool = False,
                 coverage_r: Optional[int] = None) -> Dict:
    """对下一期生成预测。

    use_ml: 是否把 M2 ML 概率模型（GBDT+RF 集成）并入 Brier 加权融合；
            默认跟随 config.ML_ENABLED。
    llm_required: M4.5 快速失败。True（Web 端明确勾选「使用大模型」）时，
            LLM 通道不可用/超时/输出异常直接抛 LLMChannelError，由 API 层
            转为「预测失败」明确提示，不再静默降级为纯统计模型。
            False（调度器/离线评估/CLI）保持原有优雅降级语义。
    llm_deadline: LLM 阶段墙钟截止时间戳；llm_required 时默认取
            now + config.LLM_TOTAL_TIMEOUT，保证响应落在代理超时窗口内。
    bet_mode: U2 投注模式。single（默认，单式）/ blue_compound（蓝球复式：
            红 6 固定 + 蓝 k 个）/ dan_tuo（红胆拖）。None = 跟随 config.BET_MODE。
    coverage: U4 覆盖优化。True 时从候选池做“至少命中 coverage_r 个红球”+蓝球覆盖
            的组合优化（贪心+退火），而非逐注独立采样。
    coverage_r: coverage 模式的目标红球命中数（默认 config.COVERAGE_R）。
    """
    if use_llm is None:
        use_llm = not config.LLM_DISABLED
    if use_ml is None:
        use_ml = config.ML_ENABLED
    if bet_mode is None:
        bet_mode = config.BET_MODE
    if bet_mode not in ("single", "blue_compound", "dan_tuo"):
        bet_mode = "single"
    # M4.2 方法 A/B 开关：按运行模式取生效规格（production=严格过滤 / research=全部启用）
    methods_spec = METH.effective_spec(config.METHOD_MODE, config.METHODS_SPEC)
    # U3 自适应降权权重（production 且开启时；否则空 = 不施加）
    adaptive = METH.adaptive_weights()
    # 关闭的通道不生成候选（LLM 同时省 API 成本）
    use_llm = use_llm and METH.is_enabled("llm", methods_spec)
    use_ml = use_ml and METH.is_enabled("ml", methods_spec)
    # U3：被自适应降权（downweighted）且权重大幅下调的方法在 production 移出候选池
    if adaptive:
        _drop = {m for m, w in adaptive.items() if w <= config.ADAPTIVE_DOWNWEIGHT_FACTOR}
        # 仅对已知方法族做过滤（统一按“族:叶子”全名匹配候选 method）
    # M4.5 快速失败：明确要求 LLM 却用不上 → 直接失败并给出可读原因
    if llm_required and not use_llm:
        if config.LLM_DISABLED:
            reason = "LLM 已被停用（设置页「停用 LLM」或 LOTT_LLM_DISABLED=1）"
        elif not config.llm_configured():
            reason = "LLM 通道未配置（API 地址 / Key / 模型缺失，请在设置页配置并保存）"
        else:
            reason = "LLM 方法通道已被方法开关关闭（LOTT_METHODS / 设置页方法开关）"
        raise LLMChannelError(reason)
    if llm_required and llm_deadline is None:
        llm_deadline = time.time() + config.LLM_TOTAL_TIMEOUT
    n_tickets = n_tickets or config.N_TICKETS
    issue = BT.expected_issue(draws)["issue"]

    stats = F.compute_features(draws)
    patterns = db.load_patterns()
    ctx = constraint_ctx(draws)

    # 统计模型 + M2 ML 概率模型 → 混合概率（Brier 加权融合）
    # M4.2：仅保留开关内启用的统计基线（stat:xxx）；全关时回退均匀分布兜底
    bl = {name: m for name, m in M.build_models(draws).items()
          if METH.is_enabled(f"stat:{name}", methods_spec)}
    if not bl:
        bl = {"uniform": M.uniform_model()}
    ml_entry, ml_extra = None, {}
    if use_ml and ml_model.HAS_SKLEARN and len(draws) >= config.ML_MIN_START + 10:
        if ml_model.ml_ready(draws):
            ml_entry = ml_model.get_ml_models(draws)
            if ml_entry is not None:
                bl["ml"] = {
                    "red": ml_model.predict_red_probs(ml_entry["red"], draws),
                    "blue": ml_model.predict_blue_probs(ml_entry["blue"], draws),
                    "name": "ml",
                }
        else:
            # 尚未训练完成（如后台预热中）：本轮先不阻塞请求，仅标记状态
            ml_extra["warming_up"] = True
    red_blend, blue_blend = _brier_blend(bl, draws, adaptive=adaptive or None)
    # U2：蓝球模式与运行命中率回退
    blue_blend = _apply_blue_mode(blue_blend)

    rng = rng if rng is not None else random.Random()
    candidates: List[Dict] = []

    # U3 自适应降权后的候选池：被大幅降权的方法不再产候选（production 且开启）
    def _method_allowed(method_name: str) -> bool:
        if not adaptive:
            return True
        w = adaptive.get(method_name, 1.0)
        return w > config.ADAPTIVE_DOWNWEIGHT_FACTOR

    # 各统计模型 + M2 ML 模型分别采样
    for name, model in bl.items():
        mname = "ml" if name == "ml" else f"stat:{name}"
        if not _method_allowed(mname):
            continue
        for _ in range(2):
            t = sample_stat_ticket(model["red"], model["blue"], ctx, rng)
            if t:
                t["method"] = mname
                candidates.append(t)
    # 均匀对照（M4.2 开关同样适用）
    if METH.is_enabled("uniform", methods_spec):
        for _ in range(2):
            t = sample_stat_ticket(None, None, ctx, rng, uniform=True)
            if t:
                t["method"] = "uniform"
                candidates.append(t)

    # LLM 候选
    # M4.5：Web 预测（llm_required=True）严格模式——大模型不可用或超时直接抛错，
    # 明确提示「预测失败」，不支持降级为纯统计；调度器/评估保持优雅降级。
    llm_cands = []  # type: ignore
    if use_llm:
        if llm_required:
            llm_cands = llm_tickets(draws, stats, patterns, rng,
                                    llm_samples=llm_samples,
                                    llm_verify=llm_verify,
                                    deadline=llm_deadline, strict=True,
                                    red_probs=red_blend, blue_probs=blue_blend)
            if not llm_cands:
                raise LLMChannelError("LLM 选号轮未返回任何有效候选（模型输出不可解析）")
        else:
            try:
                llm_cands = llm_tickets(draws, stats, patterns, rng,
                                        llm_samples=llm_samples,
                                        llm_verify=llm_verify,
                                        red_probs=red_blend, blue_probs=blue_blend)
            except Exception as e:  # noqa: BLE001
                print(f"[engine] LLM 通道异常，降级为纯统计: {e}")
                llm_cands = []
    candidates.extend(llm_cands)
    llm_models_used = sorted({
        t["method"].split(":", 1)[1] for t in llm_cands if t["method"].startswith("llm:")
    })
    # M4.2 兜底：最终按生效开关过滤候选（allow/deny 模式下丢弃关闭通道的票）
    candidates = METH.filter_candidates(candidates, methods_spec)

    # V7：规律 → 出号。规律快照（阶段时效 + 维度扫描 + 假说库）由
    # patterns_runtime 定期刷新，这里只读不重算（<1ms），并按 shadow/active 模式记录影响。
    from . import patterns_runtime
    _rule_snap = patterns_runtime.load_snapshot(current_issue=issue)

    # 去重 + 评分
    seen = set()
    scored: List[Dict] = []
    for t in candidates:
        key = (tuple(t["reds"]), t["blue"])
        if key in seen:
            continue
        seen.add(key)
        if t["method"].startswith("llm:"):
            em = ensemble_mass(red_blend, blue_blend, t["reds"], t["blue"])
            conf = round(20 + 0.45 * (0.5 * t.get("confidence", 50) + 0.5 * em), 1)
        else:
            em = ensemble_mass(red_blend, blue_blend, t["reds"], t["blue"])
            conf = round(10 + 0.25 * em, 1)
        t["confidence"] = min(100.0, max(1.0, conf))
        scored.append(t)

    # V7：把可用规律作用到**已评分候选**上（shadow 只记录、active 才真正改变号码）
    _rule_info = apply_rules_to_candidates(scored, draws, _rule_snap)
    _rule_active = _rule_info.get("mode") == "active"
    if _rule_active:
        # 用重排后的顺序重建 scored（置信度已在函数内更新）
        order = {(tuple(t["reds"]), t["blue"]): i for i, t in enumerate(scored)}
        scored.sort(key=lambda t: order.get((tuple(t["reds"]), t["blue"]), 10 ** 6))
    _rule_refs = _rule_info.get("evidence_by_ticket") or {}

    # LLM 出号通道（V10-04b：判据一律来自 resolve_channels() 快照，不再直读
    # config.LLM_ONLY_OUTPUT）。三种模式：
    #   off  → LLM 只提供候选，最终出号由确定性排序 + 蓝球覆盖计划决定；
    #   only → LLM 通道产出有效候选时，最终注数以 LLM **实际返回**的注数为准，
    #          不再用统计/ML 候选补齐到 N_TICKETS（"显示注数 = LLM 真实产出"）；
    #   mix  → LLM 与 stat/ml 候选同池混合排序后补齐到 N_TICKETS（= 默认路径）。
    # 且必须 `llm_can_pick`（族启用 + 非 off + production）：族被关掉时即使
    # 兼容开关 LOTT_LLM_ONLY_OUTPUT=1 也**绝不**走 LLM 出号（拒绝列表优先）。
    # 仅在 single + 非 coverage 的标准路径生效；coverage / blue_compound / dan_tuo
    # 是显式的组合策略模式，其"展开成 k 注"语义与本模式冲突，故保持原逻辑。
    _channels = channels_snapshot()
    _llm_mode = str(_channels.get("llm_output_mode") or "off")
    llm_only = bool(
        _channels.get("llm_can_pick")
        and _llm_mode == "only"
        and llm_cands
        and not coverage
        and bet_mode == "single"
    )
    if llm_only:
        llm_scored = [t for t in scored if t["method"].startswith("llm:")]
        # LLM 候选已过（红球,蓝球）去重；按置信度降序，最多保留 n_tickets 注，
        # 但**不补齐**到 n_tickets——LLM 产出多少就展示多少。
        llm_scored.sort(key=lambda x: -x["confidence"])
        picked = llm_scored[:n_tickets]
    # U4/V4：coverage 模式 — 从候选池做组合覆盖优化（默认奖金期望目标）
    elif coverage:
        cr = int(coverage_r if coverage_r is not None else config.COVERAGE_R)
        picked = coverage_optimize(scored, n_tickets, r=cr,
                                   n_blues=config.COVERAGE_BLUE_K,
                                   trials=config.COVERAGE_TRIALS, rng=rng,
                                   objective=config.COVERAGE_OBJECTIVE,
                                   blue_probs=blue_blend)
    elif bet_mode == "blue_compound":
        # U2 蓝球复式：取红球置信度最高的一注红（固定），展开 k 个蓝球
        k = max(1, min(config.BLUE_COMPOUND_K, B_MAX, n_tickets))
        if scored:
            base = sorted(scored, key=lambda x: -x["confidence"])[0]
            picked = _expand_blue_compound(base["reds"], blue_blend, k, rng,
                                           red_blend=red_blend, blue_blend=blue_blend)
        else:
            picked = []
    elif bet_mode == "dan_tuo":
        # U2 红胆拖：胆 = 红球概率 top DAN_TUO_DANS，围绕胆拖多注
        dan = [int(x) for x in np.argsort(-red_blend)[:config.DAN_TUO_DANS] + 1]
        blue = int(np.argmax(blue_blend)) + 1
        picked = _expand_dan_tuo(dan, blue, n_tickets, rng, red_blend, ctx,
                                 blue_blend=blue_blend)
    else:
        # 默认 single：蓝球覆盖优先（V4：先定蓝球计划，再配对红球）
        scored.sort(key=lambda x: -x["confidence"])
        if config.BLUE_COVER:
            picked = _plan_blue_coverage(scored, n_tickets, blue_blend)
        else:
            picked = _pick_legacy_blue(scored, n_tickets)

    shortfall_reason = None
    if len(picked) < n_tickets:
        if llm_only:
            # 非缺陷：按设计以 LLM 实际产出注数为准，不再补齐统计票
            shortfall_reason = "llm_only_actual_count"
        else:
            shortfall_reason = "all_methods_disabled" if not scored else "candidate_pool_exhausted"
    # V7：票级「规律引用」——只写**真正参与该注计算**的规律，
    # 不再由 LLM 自行填写 pattern-key（那样既不可核验，也容易变成装饰性文字）。
    for t in picked:
        refs = _rule_refs.get((tuple(t["reds"]), t["blue"])) or []
        if refs:
            t["patterns_used"] = list(refs)
            t.setdefault("evidence", {})
            if isinstance(t.get("evidence"), dict):
                t["evidence"]["规律引用"] = "；".join(refs)
        elif _rule_info.get("mode") == "off":
            t.setdefault("evidence", {})
            if isinstance(t.get("evidence"), dict) and "规律引用" not in t["evidence"]:
                t["evidence"]["规律引用"] = "无可用规律（0 条通过阶段验证）"
        elif _rule_info.get("usable_rules"):
            t.setdefault("evidence", {})
            if isinstance(t.get("evidence"), dict) and "规律引用" not in t["evidence"]:
                t["evidence"]["规律引用"] = "可用规律未命中本注（该注完全由概率模型决定）"
    n_distinct_blue = len({t["blue"] for t in picked})
    # V4-C：覆盖指标与期望奖金（诚实口径：覆盖提高中奖频率，不提高期望收益）
    _blue_hit_prob = _portfolio_blue_hit_prob([t["blue"] for t in picked])
    _blue_hit_naive = (1.0 - (1.0 - 1.0 / B_MAX) ** len(picked)) if picked else 0.0
    _portfolio_ev = portfolio_expected_value(picked) if picked else None
    # V10-03：票单分布元数据（coverage 模式）。用 iid 近似保证热路径开销恒定
    # （精确枚举 C(33,6) 留给评估/报表/测试；需要时传 with_distribution_exact=True）。
    _coverage_dist = None
    if coverage and picked:
        try:
            _coverage_dist = ticket_distribution(picked, with_distribution_exact=False)
        except (ValueError, TypeError):
            _coverage_dist = None
    result = {
        "issue": issue,
        "requested_tickets": n_tickets,
        "actual_tickets": len(picked),
        "shortfall_reason": shortfall_reason,
        "llm_only_output": llm_only,
        # V10-04b（§2.1.6）：把本期生效通道一并回传，前端与审计据此渲染/核对，
        # 不再各自猜"LLM 到底能不能出号"。
        "channels": {
            "spec_raw": _channels.get("spec_raw"),
            "source": _channels.get("source"),
            "mode": _channels.get("mode"),
            "families": _channels.get("families"),
            "llm_output_mode": _llm_mode,
            "llm_can_pick": bool(_channels.get("llm_can_pick")),
            "fingerprint": _channels.get("fingerprint"),
        },
        "generated_at": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
        "target_draw": {"issue": draws[-1]["issue"], "date": draws[-1]["date"],
                        "reds": draws[-1]["reds"], "blue": draws[-1]["blue"]},
        "tickets": picked,
        "llm_used": use_llm and bool(llm_cands),
        "llm_models": llm_models_used,
        "red_probs": red_blend.tolist(),
        "blue_probs": blue_blend.tolist(),
        "bet_mode": bet_mode,
        "coverage_mode": coverage,
        "coverage_objective": config.COVERAGE_OBJECTIVE if coverage else None,
        "coverage_distribution": _coverage_dist,
        "blue_covered": n_distinct_blue,
        "blue_coverage_rate": round(n_distinct_blue / B_MAX, 4),
        "blue_hit_prob": round(_blue_hit_prob, 6),
        "blue_hit_prob_naive": round(_blue_hit_naive, 6),
        "portfolio_ev": _portfolio_ev,
        "adaptive": METH.adaptive_status() if config.ADAPTIVE_ENABLED else None,
        "rules": {
            "mode": _rule_info.get("mode"),
            "usable": len(_rule_info.get("usable_rules") or []),
            "n_boosted": _rule_info.get("n_boosted", 0),
            "n_resorted": _rule_info.get("n_resorted", 0),
            "swapped": _rule_info.get("swapped") or [],
            "note_zh": _rule_info.get("note_zh"),
            "usable_rules": _rule_info.get("usable_rules") or [],
            "snapshot": _rule_info.get("snapshot"),
            "apply_enabled": patterns_runtime.apply_enabled(),
        },
        "patterns_summary": {
            "A": sum(1 for p in patterns if p["grade"] == "A"),
            "B": sum(1 for p in patterns if p["grade"] == "B"),
            "C": sum(1 for p in patterns if p["grade"] == "C"),
        },
        "ml": _ml_result_block(ml_entry, use_ml, ml_extra),
        "note": ((f"已叠加 {len(_rule_info.get('usable_rules') or [])} 条通过阶段验证的规律"
                  if _rule_info.get("usable_rules") else
                  "样本外回测未发现稳定显著的规律，预测仅基于统计结构的均衡建议")
                 + "；置信度为模型结构分，不构成中奖概率。理性购彩。"),
    }
    if persist:
        db.save_features(issue, stats)
        db.save_predictions(issue, picked)
        # M4.1：保存方法、版本、模型与 LLM 配置快照，供开奖后长期对照
        db.save_eval_meta(issue, picked, result=result)
    return result
