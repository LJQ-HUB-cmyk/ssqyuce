"""V4-B 融合权重与 Brier 加权（S3）。

## 迁移说明（D-02 拆分 S3）

内容从 `lottery/engine.py` 原 178–525 行**逐字迁移**，逻辑未改一行。
迁出范围：`_BlendArray` / `_with_meta` / `_softmax_weights` / `_cap_weights` /
`_rebuild_probs` / `_model_builder_table` / `_walk_forward_brier` / `_brier_blend` /
`_renormalize` / `_apply_adaptive_red`。

**未迁出**（它们与 S1/S2 的硬约束函数混杂，留到第 4 批）：
`_last_sums` / `_weighted_sample` / `ensemble_mass`。

## 常量为什么要用取值函数

拆前 `BLEND_*` / `MAX_MODEL_WEIGHT` / `B_MAX` 等是 `engine.py` 的模块级全局，
函数里裸用。迁出后本文件是独立命名空间，裸名会 NameError。

**必须用 `_c("NAME")` 现取，不能 `from .engine import NAME`**：
后者在 import 期把值绑死，`monkeypatch.setattr(engine, "BLEND_MIN_EVAL", N)` 打不穿。
守卫：`tests/test_v14_engine_split.py::test_no_module_bound_engine_function_references`
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from . import config      # noqa: F401
from .engine_base import R_MAX, B_MAX, TICKET_COST  # noqa: F401  纯常量，无循环

# ---- 融合权重超参（原在 engine.py，随 S3 一起迁出）----
# 设计理由见原 engine.py 注释：严格 walk-forward 样本外 Brier →
# softmax(-brier/T) → 权重上限 → 收缩。旧逻辑用样本内 1/brier 无上界，
# 实测 brier=0.001 时权重达 149x，是追噪声而非贝叶斯融合。
BLEND_EVAL_WINDOW = 50
BLEND_MIN_TRAIN = 300
BLEND_MIN_EVAL = 10
BLEND_SOFTMAX_TEMP_RED = 0.05
BLEND_SOFTMAX_TEMP_BLUE = 0.02
MAX_MODEL_WEIGHT = 0.6
BLEND_BRIER_FLOOR = 0.001
WEIGHT_SHRINKAGE = 0.5

from . import models as M  # noqa: E741  与拆前的 engine.py 完全一致的别名




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

    **性能（2026-10-19，容器 CPU 100% 治理）**：初版无论要哪个方法都调
    ``M.build_models(hist)``——那会**同时**算 freq / omission / markov / bayes 四个模型。
    实测在 600 期回放下单次 verify 调用 markov_model 就吃掉 20.6s / 32.4s。
    而绝大多数调用方只取其中**一个**模型（如 ``_ab_structural_delta`` 只取 "freq"，
    而 freq_model 是 O(30) 的轻量计算）。因此这里按需**只算被请求的那一个**：
    单模型与全量构建的返回值逐位相同（同一函数、同一输入），只是不再顺手算三个用不到的。
    """
    try:
        if name == "blue_specialist":
            return {"blue": M.blue_specialist(hist)}
        builder = _MODEL_BUILDERS.get(name)
        if builder is None:
            return None
        m = builder(hist)
        return {"red": np.asarray(m["red"], dtype=float),
                "blue": np.asarray(m["blue"], dtype=float)}
    except Exception as e:  # noqa: BLE001
        print(f"[engine] walk-forward 重建 {name} 失败（{len(hist)} 期）: {e}")
    return None


def _model_builder_table() -> Dict[str, object]:
    """``models.build_models`` 的成员名 → 单模型构造函数（惰性构造，避免 import 期依赖）。

    与 ``M.build_models`` 必须保持**同一集合**；测试 ``test_v12_model_builders_parity``
    断言两边的方法集合与返回值逐位一致，防止将来在 build_models 里加了模型却忘了登记。
    """
    return {
        "freq": M.freq_model,
        "omission": M.omission_model,
        "markov": M.markov_model,
        "bayes": M.bayes_model,
    }


_MODEL_BUILDERS: Dict[str, object] = _model_builder_table()


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
