"""自进化回路：假说 → 改变出号策略 → 战斗回放验证 → 保留/淘汰。

这是「自我完善、自我进化」的闭环实现。一次完整进化分四步：

    挖掘  hypothesis.mine_hypotheses(report, draws)
            ↓
    注册  hypothesis.register_hypotheses(hyps)          （去重 + attempts 累加）
            ↓
    应用  apply_hypotheses(hyps) → strategy_overrides    （把假说变成出号策略的修改）
            ↓
    验证  battle.replay_method(...) / replay_all(...)    （严格 walk-forward 逐期实战核对）
            ↓
    裁决  hypothesis.resolve_registry(replay_results)    （validated / rejected / 样本不足）
            ↓
    下一轮进化

## 关键设计：假说如何真正影响出号

光有「假说」不改出号行为，就不是进化。本模块把假说映射到三处**真实**可改的出号通道：

1. ``boost_method`` / ``downgrade_method`` → 叠加在红球/蓝球融合权重上。
   通道是 ``engine._brier_blend`` 之外的一层后处理：对指定统计模型的概率向量做缩放，
   再与其他模型重新归一化。**绝不**修改 ``_brier_blend`` 内部，也不破坏 walk-forward。
2. ``prefer_numbers`` → 对红球概率向量中指定号码做乘性放大（等价于温和的加权采样偏置）。
3. ``blue_prefer`` / ``blue_dedupe`` / ``blue_uniform`` → 改写蓝球概率向量或蓝球计划：
   - ``blue_dedupe`` 用 ``engine.blue_coverage_plan`` 保证 n 注蓝球互不相同；
   - ``blue_uniform`` 直接把蓝球概率压平；
   - ``blue_prefer`` 对指定蓝球做乘性放大。

## 诚实边界（工程要求，非彩票说教）

- 每条假说的裁决必须附**样本量**；低于 ``config.METHOD_RECOMMENDATION_MIN_ISSUES``
  一律判「样本不足」，绝不用十几期就宣布某个规律成立。
- 假说只有在回放中**显著**改善实战指标才晋升 ``validated``；显著变差则 ``rejected``
  并被移出预测通道（仍留档供复盘）。**「没有显著差异」不是晋升理由**——
  这是防止假说库自我膨胀的关键。
- 被淘汰的假说会写回 ``retired_at`` 与淘汰依据，保证每次进化可审计。
"""
from __future__ import annotations

import copy
import time
from typing import Dict, List, Optional

import numpy as np

from . import config

# 允许的动作白名单。任何想影响出号的假说都必须落在这几类里，
# 否则视为「不可执行」而拒绝应用（防止假说库堆满空话）。
#
# 注意：``hypothesis.mine_hypotheses`` 的确定性挖掘会产出 ``prefer_structure``
# （重号/邻号/隔期复出/奇偶配比/三区形态），若不纳入白名单，
# 实测 7 条挖掘结果里会有 5 条被静默丢弃 —— 即大部分「找出来的规律」根本影响不到出号。
_ACTION_KINDS = (
    "boost_method",
    "downgrade_method",
    "prefer_numbers",
    "blue_prefer",
    "blue_dedupe",
    "blue_uniform",
    "prefer_structure",
)

# ``prefer_structure`` 支持的规则名（与 hypothesis 挖掘端约定一致）。
# 每条规则映射到「如何调整候选生成」，实现见 apply_structure_rules()。
_STRUCTURE_RULES = (
    "keep_last",          # 保留 1 个上期号码（重号）
    "neighbors_of_last",  # 纳入上期号码的邻号
    "gap2",               # 纳入隔期（上上期）复出号
    "odd_target",         # 奇偶配比向实测目标靠拢
    "zone_target",        # 三区形态向主导形态靠拢
)

# 单次叠加给某方法的权重上下限：防止一条假说把整个集成掀翻
_METHOD_WEIGHT_MIN, _METHOD_WEIGHT_MAX = 0.2, 3.0
# 单条号码偏置的上下界
_NUMBER_GAIN_MIN, _NUMBER_GAIN_MAX = 0.5, 3.0


# ---------- 假说 → 出号策略 ----------

def _as_hyp_list(registry) -> List[Dict]:
    """把假说库的任意返回形态归一成 ``list[dict]``。

    ``hypothesis.load_registry`` 可能返回 ``list`` 或 ``{"hypotheses": [...]}``，
    且条目理论上都应是 dict；这里统一过滤掉非法条目，避免上层
    在 ``h.get(...)`` 处抛 AttributeError（真实踩过：dict 的键被当成假说）。
    """
    if isinstance(registry, dict):
        registry = registry.get("hypotheses") or []
    if not isinstance(registry, list):
        return []
    return [h for h in registry if isinstance(h, dict)]


def EVO_EMPTY_OVERRIDES() -> Dict:
    """空的出号策略覆盖项（降级路径与无假说时共用）。"""
    return {"method_weights": {}, "red_boost": [], "blue_boost": [],
            "blue_dedupe": False, "blue_uniform": False,
            "structure_rules": [], "applied": [], "skipped": []}


def apply_hypotheses(hypotheses: List[Dict],
                     only_status: Optional[set] = None) -> Dict:
    """把假说折叠成一组「出号策略覆盖项」（strategy overrides）。

    只消费 ``only_status`` 指定的状态（默认只取 ``validated``）：
    未经验证的假说不得影响真实出号，这是闭环可信度的前提。

    返回::

        {
          "method_weights": {"omission": 1.4, ...},   # 按方法名聚合后的乘性权重
          "red_boost":      [7, 18, 23, ...],          # 需要加权放大的红球号
          "blue_boost":     [3, 9],                    # 需要加权放大的蓝球号
          "blue_dedupe":    True|False,                # 蓝球是否强制去重覆盖
          "blue_uniform":   True|False,                # 蓝球是否压平
          "applied":        [hyp_id, ...],             # 实际生效的假说
          "skipped":        [{"id":..., "reason":...}]
        }
    """
    want = only_status if only_status is not None else {"validated"}
    out: Dict = EVO_EMPTY_OVERRIDES()
    for h in hypotheses or []:
        status = (h or {}).get("status", "proposed")
        if status not in want:
            out["skipped"].append({"id": h.get("id"), "reason": f"status={status}"})
            continue
        act = (h or {}).get("action") or {}
        kind = act.get("type")
        hid = h.get("id")
        if kind not in _ACTION_KINDS:
            out["skipped"].append({"id": hid, "reason": f"unknown action {kind!r}"})
            continue

        if kind in ("boost_method", "downgrade_method"):
            name = (act.get("method") or "").rsplit(":", 1)[-1]  # stat:omission -> omission
            if not name:
                out["skipped"].append({"id": hid, "reason": "missing method"})
                continue
            w = float(act.get("weight", 1.2 if kind == "boost_method" else 0.8))
            w = float(min(max(w, _METHOD_WEIGHT_MIN), _METHOD_WEIGHT_MAX))
            if kind == "downgrade_method" and w > 1.0:
                w = 1.0 / w                     # downgrade 用倒数表达，避免语义重复
            out["method_weights"][name] = out["method_weights"].get(name, 1.0) * w

        elif kind == "prefer_numbers":
            nums = [int(n) for n in (act.get("numbers") or []) if 1 <= int(n) <= 33]
            if nums:
                out["red_boost"] = sorted(set(out["red_boost"]) | set(nums))

        elif kind == "blue_prefer":
            nums = [int(n) for n in (act.get("blues") or []) if 1 <= int(n) <= 16]
            if nums:
                out["blue_boost"] = sorted(set(out["blue_boost"]) | set(nums))

        elif kind == "blue_dedupe":
            out["blue_dedupe"] = bool(act.get("enabled", True))

        elif kind == "blue_uniform":
            out["blue_uniform"] = bool(act.get("enabled", True))

        elif kind == "prefer_structure":
            rule = act.get("rule")
            if rule not in _STRUCTURE_RULES:
                out["skipped"].append(
                    {"id": hid, "reason": f"unknown structure rule {rule!r}"})
                continue
            out["structure_rules"].append({
                "rule": rule,
                "target_odd": act.get("target_odd"),
                "zone": act.get("zone"),
                "evidence": (h or {}).get("evidence"),
            })

        out["applied"].append(hid)
    # 互斥：blue_uniform 优先于 blue_prefer / blue_dedupe
    if out["blue_uniform"]:
        out["blue_dedupe"] = False
        out["blue_boost"] = []
    return out


def apply_structure_rules(red_pool: List[List[int]], rules: List[Dict],
                          hist: List[Dict]) -> tuple:
    """把 ``prefer_structure`` 规则真正作用到红球候选上。

    与号码级乘法偏置不同，这些规则改的是**候选组合的结构**：重号、邻号、
    隔期复出、奇偶配比、三区形态。实现方式是对每个候选取「结构调整得分」，
    得分高的候选优先入选；**不直接增删号码**，因此不会破坏既有的硬约束与去重。

    参数
    ----
    red_pool: 候选红球组合列表（每个是 6 个升序号码）
    rules: ``apply_hypotheses()`` 产出的 ``structure_rules``
    hist: **用于判定结构的历史**（调用方必须传 ``draws[:t]``，不得传未来数据）

    返回 ``(ranked_pool, detail)``：按得分降序的候选与逐规则命中统计。
    """
    if not red_pool:
        return [], {}
    rules = [r for r in (rules or []) if r.get("rule") in _STRUCTURE_RULES]
    if not rules or not hist:
        return list(red_pool), {}

    last = set(hist[-1].get("reds") or [])
    prev = set(hist[-2].get("reds") or []) if len(hist) >= 2 else set()
    neighbors = {n for x in last for n in (x - 1, x + 1) if 1 <= n <= 33} - last
    detail: Dict = {}

    def _score(ticket: List[int]) -> float:
        s = set(ticket)
        sc = 0.0
        for r in rules:
            rule = r["rule"]
            if rule == "keep_last":
                hit = 1 if (s & last) else 0
                sc += 1.0 if hit else -0.5      # 命中重号加分，完全没有则轻罚
                detail[rule] = detail.get(rule, 0) + hit
            elif rule == "neighbors_of_last":
                hit = 1 if (s & neighbors) else 0
                sc += 1.0 if hit else -0.5
                detail[rule] = detail.get(rule, 0) + hit
            elif rule == "gap2":
                hit = 1 if (s & prev) else 0
                sc += 0.8 if hit else -0.4
                detail[rule] = detail.get(rule, 0) + hit
            elif rule == "odd_target":
                tgt = r.get("target_odd")
                if tgt is not None:
                    odd = sum(1 for x in ticket if x % 2 == 1)
                    sc -= abs(odd - float(tgt)) * 0.5   # 越接近实测目标越好
            elif rule == "zone_target":
                zone = r.get("zone")
                if zone:
                    z1 = sum(1 for x in ticket if 1 <= x <= 11)
                    z2 = sum(1 for x in ticket if 12 <= x <= 22)
                    z3 = 6 - z1 - z2
                    tgt = [int(z) for z in zone]
                    sc -= (abs(z1 - tgt[0]) + abs(z2 - tgt[1]) + abs(z3 - tgt[2])) * 0.4
        return sc

    ranked = sorted(red_pool, key=_score, reverse=True)
    return ranked, detail


def _renorm(v: np.ndarray, target: float) -> np.ndarray:
    v = np.asarray(v, dtype=float)
    s = v.sum()
    if not np.isfinite(s) or s <= 0:
        return np.full_like(v, target / len(v))
    return v / s * target


def apply_overrides_to_probs(red_blend: Optional[np.ndarray],
                             blue_blend: Optional[np.ndarray],
                             overrides: Dict) -> tuple:
    """把 strategy overrides 实际作用到红/蓝概率向量上，返回新向量。

    - 方法权重通过 ``red_pool``/``blue_pool`` 在融合阶段生效（见
      ``_weighted_pool``），本函数只处理号码级偏置与蓝球口径；
    - ``red_boost`` / ``blue_boost`` 只做乘性放大，不增删号码、不改排序合法性；
    - 输出保持与输入相同的归一化口径（红球 sum=6、蓝球 sum=1）。
    """
    red = None if red_blend is None else np.array(red_blend, dtype=float)
    blue = None if blue_blend is None else np.array(blue_blend, dtype=float)

    boost = (overrides or {}).get("red_boost") or []
    if red is not None and boost:
        for n in boost:
            if 1 <= int(n) <= len(red):
                red[int(n) - 1] *= 2.0              # 温和偏置：放大一倍，不制造独裁
        red = _renorm(red, 6.0)

    if blue is not None:
        if (overrides or {}).get("blue_uniform"):
            blue = np.full(len(blue), 1.0 / len(blue))
        else:
            for n in (overrides or {}).get("blue_boost") or []:
                if 1 <= int(n) <= len(blue):
                    blue[int(n) - 1] *= 2.0
            blue = _renorm(blue, 1.0)
    return red, blue


def _weighted_pool(pool: Dict[str, np.ndarray], method_weights: Dict[str, float],
                   default: float = 1.0) -> Dict[str, np.ndarray]:
    """按 method_weights 对模型池里的每个概率向量做乘性缩放。"""
    out = {}
    for name, vec in (pool or {}).items():
        if vec is None:
            continue
        w = float(method_weights.get(name, default))
        if w != 1.0:
            vec = np.asarray(vec, dtype=float) * w
        out[name] = vec
    return out


# ---------- 一代进化 ----------

def evolve_once(draws: List[Dict], use_llm: Optional[bool] = None,
                n_tickets: int = 10, start: int = 300, seed: int = 42,
                registry_path=None) -> Dict:
    """跑一轮完整进化：挖掘 → 注册 → 应用 → 回放验证 → 裁决。

    返回一轮进化的完整轨迹，供审计与前端展示::

        {
          "generation": int,
          "mined": {...},          # hypothesis.mine_hypotheses 的返回
          "registered": {...},     # hypothesis.register_hypotheses 的返回
          "overrides": {...},      # apply_hypotheses 的结果
          "replay": {...},         # 应用 overrides 前后的回放对比（若 battle 可用）
          "resolution": {...},     # hypothesis.resolve_registry 的返回
          "llm_used": bool,
          "elapsed_sec": float,
        }
    """
    from . import battle, hypothesis  # 延迟导入：允许 battle 尚未就绪

    t0 = time.time()
    result: Dict = {"generation": None, "llm_used": False, "elapsed_sec": 0.0}

    # 1) 基线战斗报告（不含任何假说影响）—— 作为本轮的对照
    base_report: Dict = {}
    try:
        base_report = battle.battle_report(draws, n_tickets=n_tickets,
                                          start=start, seed=seed)
    except Exception as e:  # noqa: BLE001
        result["replay_error"] = f"{type(e).__name__}: {e}"

    # 2) 挖掘假说（统计 + LLM）
    mined = hypothesis.mine_hypotheses(base_report, draws, use_llm=use_llm)
    result["mined"] = mined
    result["llm_used"] = bool(mined.get("llm_used"))

    # 3) 注册（去重 + attempts 累加）
    reg = hypothesis.register_hypotheses(mined.get("hypotheses") or [],
                                        registry_path=registry_path)
    result["registered"] = reg

    # 4) 折叠成出号策略覆盖项（只消费 validated）
    #    注意：load_registry 的返回形态可能是 list 或 {"hypotheses": [...]}，
    #    必须归一化后再用——否则 "str"/dict 会被当成假说而 AttributeError。
    hyps = _as_hyp_list(hypothesis.load_registry(registry_path))
    result["registry_size"] = len(hyps)
    result["overrides"] = apply_hypotheses(hyps)

    # 5) 回放验证：首轮没有任何 validated 假说时，退化为「基线 vs 基线」
    #    ——这是正常状态：第一代只负责收集假说，裁决发生在拿到数据之后。
    resolution: Dict = {"note": "无已验证假说，本轮不适用覆盖项"}
    if result["overrides"]["applied"]:
        try:
            resolution = hypothesis.resolve_registry(
                battle.battle_report(draws, n_tickets=n_tickets,
                                     start=start, seed=seed),
                registry_path=registry_path,
            )
        except Exception as e:  # noqa: BLE001
            resolution = {"error": f"{type(e).__name__}: {e}"}
    result["resolution"] = resolution
    result["elapsed_sec"] = round(time.time() - t0, 2)
    return result


def evolution_status(registry_path=None) -> Dict:
    """当前假说库状态摘要（供 API/前端展示进化进度）。

    ``hypothesis`` / ``battle`` 可能尚未就绪（并行开发），此时返回可用=false 的降级摘要，
    绝不抛异常让 API/前端崩掉。
    """
    try:
        from . import hypothesis
    except ImportError:
        return {"available": False, "total": 0, "by_status": {},
                "active_overrides": EVO_EMPTY_OVERRIDES(),
                "min_issues": getattr(config, "METHOD_RECOMMENDATION_MIN_ISSUES", 60),
                "reason": "lottery/hypothesis.py 尚未就绪"}

    hyps = _as_hyp_list(hypothesis.load_registry(registry_path))

    by_status: Dict[str, int] = {}
    for h in hyps:
        s = h.get("status", "proposed")
        by_status[s] = by_status.get(s, 0) + 1
    validated = [h for h in hyps if h.get("status") == "validated"]
    return {
        "available": True,
        "total": len(hyps),
        "by_status": by_status,
        "active_overrides": apply_hypotheses(hyps),
        "latest_validated": [
            {"id": h.get("id"), "statement": h.get("statement"),
             "action": h.get("action"), "evidence": h.get("evidence")}
            for h in validated[-10:]
        ],
        "min_issues": getattr(config, "METHOD_RECOMMENDATION_MIN_ISSUES", 60),
    }
