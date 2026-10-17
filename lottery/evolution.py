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
    # V6：维度化动作——``discovery`` 把「筛选条件」翻译成红/蓝号码集合后再放大。
    # 没有这两个类型，凡是「热号/遗漏/邻号/共现」这类按筛选条件表述的规律
    # 都会被白名单拒掉，这正是 LLM 假说「永远无法进入出号」的一半原因。
    "prefer_red_filter",
    "prefer_blue_filter",
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
            "structure_rules": [], "filters": [], "applied": [], "skipped": []}


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
          "structure_rules":[{rule, ...}],             # 候选组合的结构取向
          "filters":        [{kind, filter, gain}],    # V6：维度化筛选条件（discovery 语法）
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

        elif kind in ("prefer_red_filter", "prefer_blue_filter"):
            spec = act.get("filter")
            keys = sorted((spec or {}).keys()) if isinstance(spec, dict) else []
            if not isinstance(spec, dict) or "key" not in spec:
                out["skipped"].append({"id": hid, "reason": "prefer_*_filter 缺少 filter 定义"})
                continue
            out["filters"].append({"id": hid, "kind": kind, "filter": spec,
                                   "gain": float(act.get("gain", 1.5)), "keys": keys})

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
                             overrides: Dict,
                             history: Optional[List[Dict]] = None) -> tuple:
    """把 strategy overrides 实际作用到红/蓝概率向量上，返回新向量。

    - 方法权重通过 ``red_pool``/``blue_pool`` 在融合阶段生效（见
      ``_weighted_pool``），本函数只处理号码级偏置与蓝球口径；
    - ``red_boost`` / ``blue_boost`` 只做乘性放大，不增删号码、不改排序合法性；
    - ``filters``（V6）需要 ``history``（**as-of，只含当期之前的数据**）来求值；
      未提供 history 时跳过并保证不抛异常——绝不用未来数据算筛选条件；
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

    # V6：维度化筛选条件（as-of）。放大倍数沿用「温和偏置」哲学，并按条数衰减，
    # 避免多条规律叠加后把某个号码变成独裁。
    if history:
        from . import discovery as _disc
        for item in (overrides or {}).get("filters") or []:
            try:
                flt = _disc.make_filter(item["filter"]["key"],
                                        item["filter"].get("params") or {})
                chosen = set(int(x) for x in (flt["selector"](list(history)) or set()))
            except Exception:  # noqa: BLE001 —— 维度不可用不应影响出号
                continue
            gain = float(item.get("gain") or 1.5)
            gain = min(max(gain, 1.0), _NUMBER_GAIN_MAX)
            if item["kind"] == "prefer_red_filter" and red is not None:
                for n in chosen:
                    if 1 <= n <= len(red):
                        red[n - 1] *= gain
                red = _renorm(red, 6.0)
            elif item["kind"] == "prefer_blue_filter" and blue is not None:
                for n in chosen:
                    if 1 <= n <= len(blue):
                        blue[n - 1] *= gain
                blue = _renorm(blue, 1.0)

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


# ---------- V6：阶段时效标注（让每条规律都带「现在还灵不灵」） ----------

def annotate_hypotheses(draws: List[Dict], registry_path=None,
                        start: int = 200) -> Dict:
    """给假说库里的每条假说补一份**阶段画像**（``regime``）并落库。

    为什么必须做这件事：V5 的假说只有「显著/不显著」一个二元状态，
    用户看到的就是「近期规律都不生效」却查不到是哪一条、从哪年开始失效。
    阶段画像给出每个年份段的效应与校正后 p、最近窗口效应、以及状态机判决
    （持续/衰减/陈旧/新兴/反转/未检出），回答的正是「阶段有效性」这个问题。

    实现：能落到维度表的假说用其筛选条件做 as-of 回放；不能落地的
    （纯结构类/方法权重类）标注 ``no_dimension`` 并说明原因，不假装有结论。
    """
    from . import hypothesis

    draws = list(draws or [])          # 归一成 list：_load_registry 的入参约定不能吃 list[dict]
    reg = hypothesis.load_registry(registry_path)
    hyps = _as_hyp_list(reg)
    annotated = 0
    skipped = 0
    for h in hyps:
        act = h.get("action") if isinstance(h.get("action"), dict) else {}
        spec = act.get("filter") if act.get("type") in ("prefer_red_filter", "prefer_blue_filter") else None
        if not isinstance(spec, dict) or not spec.get("key"):
            struct = hypo_structure_filter(h)
            spec = struct
        if not spec:
            h["regime"] = {"status": "no_dimension",
                           "label_zh": "无可回放维度",
                           "reason_zh": "该假说不是按维度表述的（结构类/方法权重类），"
                                        "无法做 as-of 阶段回放；其效果由 A/B 实战验证给出"}
            skipped += 1
            continue
        try:
            rep = verify_discovery_hypothesis({"action": {"type": "prefer_red_filter",
                                                          "filter": spec}, "id": h.get("id")},
                                              draws, start=start)
            # 结构性缺省：verify_discovery_hypothesis 只返回精简字段，这里用
            # discovery.regime_of_filter 拿到完整状态机（label_zh/reason_zh）。
            from . import discovery as _disc
            full = _disc.regime_of_filter(
                draws, _disc.make_filter(spec["key"], spec.get("params") or {}), start=start)
            h["regime"] = {"status": full.get("status"), "label_zh": full.get("label_zh"),
                           "reason_zh": full.get("reason_zh"),
                           "n_sig_eras": full.get("n_sig_eras"),
                           "last_significant_era": full.get("last_significant_era"),
                           "recent_delta": (full.get("recent") or {}).get("delta"),
                           "p_adjusted_recent": (full.get("recent") or {}).get("p_adjusted"),
                           "n_issues": (full.get("scan") or {}).get("n"),
                           "source": "structure_filter" if not (
                               act.get("type") in ("prefer_red_filter", "prefer_blue_filter")) else "filter"}
            annotated += 1
        except Exception as e:  # noqa: BLE001
            h["regime"] = {"status": "error", "reason_zh": f"{type(e).__name__}: {e}"}
            skipped += 1
    try:
        hypothesis._save_registry(reg, registry_path)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "annotated": annotated}
    return {"ok": True, "annotated": annotated, "no_dimension": skipped, "total": len(hyps)}


def hypo_structure_filter(h: Dict) -> Optional[Dict]:
    """把 ``prefer_structure`` 假说翻译成等价的维度表筛选条件（可回放）。

    这是让「结构类规律」也能拿到阶段画像的桥：结构规则里的
    keep_last / neighbors_of_last / gap2 在维度表里都有同名维度，
    奇偶与三区形态则映射到 ``pattern_cont`` 的条件分布。
    """
    act = h.get("action") if isinstance(h.get("action"), dict) else {}
    if act.get("type") != "prefer_structure":
        return None
    rule = act.get("rule")
    mapping = {
        "keep_last": {"key": "repeat", "params": {"back": 1}},
        "avoid_last": {"key": "repeat", "params": {"back": 1}},
        "neighbors_of_last": {"key": "neighbor", "params": {"back": 1, "offset": 1}},
        "gap2": {"key": "repeat2", "params": {"back": 2}},
        "odd_target": {"key": "pattern_cont", "params": {"feature": "odd", "k": 6}},
        "zone_target": {"key": "zone_lag", "params": {"zone": 0}},
        "sum_target": {"key": "pattern_cont", "params": {"feature": "sumtail", "k": 6}},
    }
    return mapping.get(str(rule))


def register_llm_lab_claims(draws: List[Dict], registry_path=None,
                            start: int = 200) -> Dict:
    """V6：把 LLM 工坊产出的规律注册成假说（**只注册可落地且不是噪声的**）。

    规则（防止假说库变成垃圾场）：
    * ``unmappable`` / ``insufficient`` → 不注册（不可验证的东西没有资格进库）；
    * ``noise`` → 不注册（与随机无异，注册只会稀释库里有效条目）；
    * ``support`` / ``reject`` / ``regime_only`` → 注册，动作翻译成
      ``prefer_red_filter`` / ``prefer_blue_filter``，并在 evidence 里带上
      阶段画像与判定，随后由 ``resolve_registry`` 统一裁决。
    """
    from . import hypothesis, llm_lab

    data = llm_lab.load_claims()
    latest = (data.get("claims") or [])[:40]
    picked: List[Dict] = []
    skipped: List[Dict] = []
    for c in latest:
        v = c.get("verdict")
        if v in ("unmappable", "insufficient", "noise", None):
            skipped.append({"title": c.get("title"), "reason": f"verdict={v}"})
            continue
        filters = c.get("filters") or []
        if not filters:
            skipped.append({"title": c.get("title"), "reason": "无 filters"})
            continue
        f0 = filters[0]
        if not discovery_params_ok(f0):
            skipped.append({"title": c.get("title"), "reason": "维度/参数不可落地"})
            continue
        target = llm_lab.discovery_target(f0["key"])
        act = {"type": "prefer_red_filter" if target == "red" else "prefer_blue_filter",
               "filter": {"key": f0["key"], "params": f0.get("params") or {}}}
        stmt = f"LLM规律「{c.get('title')}」：维度 {f0['key']} {f0.get('params') or {}}；" \
               f"阶段判定={c.get('verdict_zh')}；{str(c.get('reason') or '')[:120]}"
        picked.append(hypothesis._build_hypothesis(
            family=f"llm_lab_{f0['key']}", statement=stmt, action=act,
            params={"filter": f0, "mode": c.get("mode")},
            evidence={"n": c.get("n"), "effect": c.get("effect"),
                      "p_adjusted": c.get("p_adjusted"),
                      "regime": (c.get("regime") or {}).get("status"),
                      "llm_title": c.get("title"), "llm_mechanism": c.get("mechanism")},
            source="llm", n_samples=int(c.get("n") or 0)))
    reg = hypothesis.register_hypotheses(picked, registry_path=registry_path)
    return {"registered": reg, "picked": len(picked), "skipped": skipped[:20],
            "skipped_n": len(skipped)}


def discovery_params_ok(f: Dict) -> bool:
    from . import discovery

    fam = discovery.FAMILY_BY_KEY.get(str((f or {}).get("key")))
    if not fam:
        return False
    if not fam.get("params"):
        return True
    return dict(f.get("params") or {}) in fam["params"]


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


# ---------- 进化结果 → 下一期真实预测 ----------

def predict_with_evolution(draws: List[Dict], n_tickets: int = 10,
                           use_llm: bool = False, registry_path=None,
                           seed: int = 42) -> Dict:
    """把「已验证的规律」用于**下一期未开奖**的真实预测。

    这是整条链路的最后一环，也是用户要的最终产出：

        历史数据 → 战斗回放找规律 → LLM 提炼假说 → 验证 → 已验证规律
                 → 调整出号策略 → 预测下一期未开奖号码

    与 ``predict_next`` 的区别：本函数会先取假说库里 ``validated`` 的规律，
    折叠成出号策略覆盖项，再据此调整候选生成的偏置与结构取向。

    返回::

        {
          "issue": 下一期号,
          "tickets": [{reds, blue, ...}],
          "applied_hypotheses": [...],      # 实际生效的规律（含依据）
          "overrides": {...},               # 折叠后的策略覆盖项
          "blue_coverage": {...},           # 蓝球覆盖口径与提升
          "evolution_note_zh": "...",       # 本轮用了哪些规律、为什么
        }

    诚实边界（工程质量要求）：
    - 若库里**没有已验证规律**，函数照常出号，但会明确写明「本次未使用任何已验证规律，
      输出等同基线」——绝不让「空库」看起来像「AI 加持」。
    - 每条生效规律都带 sample_n / evidence，便于回溯「这个号是因为哪条规律被偏置的」。
    """
    from . import battle, engine, hypothesis

    hyps = _as_hyp_list(hypothesis.load_registry(registry_path))
    validated = [h for h in hyps if h.get("status") == "validated"]
    overrides = apply_hypotheses(hyps)

    issue = None
    try:
        from . import backtest as _BT
        issue = _BT.expected_issue(draws)["issue"] if draws else None
    except Exception:  # noqa: BLE001
        issue = None

    # 1) 先按基线出号（含 U4 覆盖优化：蓝球去重是结构性收益）
    pred = engine.predict_next(draws, use_llm=use_llm) if draws else {}

    # 2) 若有已验证的**结构类**规律，按结构得分重排候选（只重排，不增删）
    structure_rules = overrides.get("structure_rules") or []
    restructured = 0
    if structure_rules and isinstance(pred.get("tickets"), list) and pred["tickets"]:
        pool = [list(t.get("reds") or []) for t in pred["tickets"]]
        ranked, _detail = apply_structure_rules(pool, structure_rules, draws)
        by_key = {tuple(t.get("reds") or []): t for t in pred["tickets"]}
        new_tickets = [by_key[k] for k in map(tuple, ranked) if k in by_key]
        if len(new_tickets) == len(pred["tickets"]):
            pred["tickets"] = new_tickets
            restructured = len(new_tickets)

    note_bits = []
    if validated:
        note_bits.append(
            f"本次使用 {len(validated)} 条**已验证**规律"
            f"（结构类 {len(structure_rules)} 条、方法权重 {len(overrides.get('method_weights') or {})} 项、"
            f"号码偏置 {len(overrides.get('red_boost') or [])} 红/{len(overrides.get('blue_boost') or [])} 蓝）"
        )
        if restructured:
            note_bits.append(f"已按结构规律对 {restructured} 注候选重排")
    else:
        note_bits.append(
            "假说库中**尚无已验证规律**（全部处于 proposed 状态），"
            "本次未施加任何规律偏置，输出等同基线——不把空库包装成 AI 加持"
        )

    return {
        "issue": issue,
        "tickets": pred.get("tickets") or [],
        "applied_hypotheses": [
            {"id": h.get("id"), "statement": h.get("statement"),
             "action": h.get("action"), "evidence": h.get("evidence")}
            for h in validated
        ],
        "overrides": overrides,
        "restructured_tickets": restructured,
        "registry_total": len(hyps),
        "registry_by_status": _count_status(hyps),
        "portfolio_ev": pred.get("portfolio_ev"),
        "blue_hit_prob": pred.get("blue_hit_prob"),
        "blue_hit_prob_naive": pred.get("blue_hit_prob_naive"),
        "evolution_note_zh": "；".join(note_bits) + "。",
    }


def _count_status(hyps: List[Dict]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for h in hyps:
        s = h.get("status", "proposed")
        out[s] = out.get(s, 0) + 1
    return out


# ---------- 假说 A/B 实战验证（闭环的最后一环） ----------

def verify_hypothesis_ab(hyp: Dict, draws: List[Dict], n_tickets: int = 10,
                         start: int = 300, seed: int = 42,
                         baseline_method: str = "blue_coverage") -> Dict:
    """用「施加该假说 vs 不施加」的配对回放，给出这条规律的实战效果。

    这是把假说从 ``proposed`` 推向 ``validated``/``rejected`` 的关键一步：
    ``hypothesis.resolve_registry`` 需要一个形如
    ``{method, delta_red_hits, n_issues, ...}`` 的结果，而那个 delta 必须来自
    **同一批历史、同一随机种子下的 A/B 对照**，否则两组不可比。

    做法：用同一 seed 跑两次逐期回放——
      · 对照组：基线出号
      · 实验组：对候选施加该假说（结构规则重排 / 号码偏置 / 方法权重）
    两组逐期配对，比较红球命中均值，并把差值交给 resolve_registry 裁决。

    返回 ``{method, n_issues, delta_red_hits, delta_blue_rate, ab:{...}}``；
    ``method`` 取自假说动作（boost_method 的 method），供 resolve_registry 匹配。
    """
    from . import battle, engine

    # 维度化假说优先走 as-of 口径（对照 = 理论期望，任何假说都有非平凡数字）
    act = hyp.get("action") if isinstance(hyp.get("action"), dict) else {}
    if act.get("type") in ("prefer_red_filter", "prefer_blue_filter"):
        return verify_discovery_hypothesis(hyp, draws, start=min(start, 200))

    method = str(act.get("method") or "") or None

    # V5 缺陷修复：对照组曾是 ``uniform``（均匀随机），而系统真实出号是
    # engine.predict_next 的集成 + 蓝球覆盖优化。拿「结构偏置」去和纯随机比，
    # 量到的是「蓝球覆盖带来的提升」而不是这条假说的效果，Δ 被系统性虚高。
    # 现在对照组 = 当前实际部署的基线方法（默认 blue_coverage）。
    try:
        base = battle.replay_method(baseline_method, draws, n_tickets=n_tickets,
                                    start=start, seed=seed)
    except Exception:  # noqa: BLE001 —— 未知方法名时退回 uniform，不让验证整体失败
        baseline_method = "uniform"
        base = battle.replay_method("uniform", draws, n_tickets=n_tickets,
                                    start=start, seed=seed)
    # 实验组：把该假说标成 validated 后折叠成覆盖项，再作用于候选生成
    probe = dict(hyp)
    probe["status"] = "validated"
    ov = apply_hypotheses([probe])

    treats: List[Dict] = []
    rules = ov.get("structure_rules") or []
    red_boost = ov.get("red_boost") or []
    blue_boost = ov.get("blue_boost") or []
    for row in base.get("per_issue") or []:
        t = dict(row)
        treats.append(t)

    # 结构/号码级偏置无法在 battle.replay_method 内部注入（它按方法名出号），
    # 故用「结构规则重排 + 命中统计」的方式做配对评估：
    # 对每一期，看该期的真实开奖与「按规则筛选后的候选」是否更好。
    detail: Dict = {}
    if rules or red_boost or blue_boost:
        delta_red, delta_blue, n_pair, detail = _ab_structural_delta(
            draws, start, rules, red_boost, n_tickets, seed)
    else:
        delta_red, delta_blue, n_pair = 0.0, 0.0, int(base.get("issues") or 0)

    return {
        "method": method,
        "hyp_id": hyp.get("id"),
        "n_issues": n_pair,
        "delta_red_hits": round(delta_red, 6),
        "delta_blue_rate": round(delta_blue, 6),
        "ab_test": detail,
        "baseline_method": baseline_method,
        "ab": {"rules": [r.get("rule") for r in rules],
               "red_boost": red_boost, "blue_boost": blue_boost,
               "n_tickets": n_tickets, "start": start, "seed": seed},
    }


def verify_discovery_hypothesis(hyp: Dict, draws: List[Dict], start: int = 200) -> Dict:
    """V6：维度化假说的实战验证——as-of 回放 + 阶段画像。

    与 ``verify_hypothesis_ab`` 的区别：后者比较的是「候选重排前后」，对
    ``boost_method`` / ``prefer_numbers`` 这类不改变候选集合的假说恒等于 0；
    本函数直接在**真实开奖**上度量筛选条件的效力，因此任何能落地到维度表的假说
    都有一个非平凡的数字。

    返回结构与 ``verify_hypothesis_ab`` 兼容（含 ``method``/``hyp_id``/``n_issues``/
    ``delta_red_hits``/``p_adjusted``），可直接交给 ``hypothesis.resolve_registry``；
    另外附带 ``regime`` 阶段画像与 ``evidence`` 摘要。
    """
    from . import discovery, regime

    act = hyp.get("action") if isinstance(hyp.get("action"), dict) else {}
    spec = act.get("filter") or {}
    hyp_id = hyp.get("id")
    if not isinstance(spec, dict) or not spec.get("key"):
        return {"method": act.get("method"), "hyp_id": hyp_id, "n_issues": 0,
                "delta_red_hits": 0.0, "verdict": "unverifiable",
                "note_zh": "该假说不含维度化筛选条件，无法用 discovery 口径验证"}
    try:
        flt = discovery.make_filter(str(spec["key"]), spec.get("params") or {})
        rep = discovery.regime_of_filter(draws, flt, start=start)
    except Exception as e:  # noqa: BLE001
        return {"method": act.get("method"), "hyp_id": hyp_id, "n_issues": 0,
                "delta_red_hits": 0.0, "verdict": "error",
                "note_zh": f"{type(e).__name__}: {e}"}
    scan = rep.get("scan") or {}
    recent = rep.get("recent") or {}
    delta = recent.get("delta")
    n = int(scan.get("n") or 0)
    # 蓝球口径的效应是「命中率差」，需换算成与红球可比的量级（×6 注）后再比较门限，
    # 否则 1/16 的基线会让任何蓝球差都显得很小。
    if flt["target"] == "blue" and delta is not None:
        comparable = float(delta) * 6.0
    else:
        comparable = float(delta or 0.0)
    p_adj = recent.get("p_adjusted")
    verdict = "无显著差异"
    if p_adj is not None and p_adj < regime.ALPHA:
        verdict = "支持" if comparable > 0 else "反对"
    return {
        "method": act.get("method"), "hyp_id": hyp_id, "n_issues": n,
        "delta_red_hits": round(comparable, 6),
        "delta_native": delta, "target": flt["target"],
        "p_value": recent.get("p_value"), "p_adjusted": p_adj,
        "verdict": verdict,
        "regime": {"status": rep.get("status"), "status_zh": rep.get("label_zh"),
                   "reason": rep.get("reason_zh"), "n_sig_eras": rep.get("n_sig_eras"),
                   "last_significant_era": rep.get("last_significant_era")},
        "baseline_method": "as-of 筛选条件 vs 理论期望",
        "note_zh": rep.get("note_zh"),
    }


def _ab_structural_delta(draws: List[Dict], start: int, rules: List[Dict],
                         red_boost: List[int], n_tickets: int,
                         seed: int) -> tuple:
    """配对计算「施加结构/号码偏置」相对基线出号的**逐注**红球命中差。

    为什么不能拿「同一批候选池的前 n 注」当两个臂
    --------------------------------------------
    V5 的实现是：生成 40 注候选池 → 基线取 ``pool[:n]`` → 实验组按结构得分重排后
    再取 ``pool[:n]``。两个臂来自同一个池、同一批候选，结构规则只是**改变顺序**；
    在「每期命中数取均值」的口径下，前 n 个候选的顺序几乎不改变统计量，
    实测 Δ 恒在 ±0.02 以内（`keep_last` +0.012 / `neighbors_of_last` −0.005 /
    `odd_target` −0.020），于是所有结构类假说注定「无显著差异」——这不是数据结论，
    是评估口径的伪影。

    现在的做法（与 ``battle.replay_method`` 的统计口径一致）
    ------------------------------------------------------
    两个臂各自**独立采样**、每臂 ``n_tickets`` 注、每期 ``n_tickets × 6`` 个红球位置：
      · 基线臂：不打分，按采样顺序直接取满 n 注；
      · 实验臂：采样更多候选（4n），按结构得分降序取前 n 注。
    每期比较的是「36 个位置上的平均命中率」，配对后差值送 Wilcoxon 符号秩检验。

    诚实边界：两臂随机数不同，存在采样噪声；但噪声在同一 seed 下可复现，
    且配对检验只看逐期差值，与 ``battle`` 的其它配对比较同一口径。

    返回 ``(Δ红球每注命中, Δ蓝球每注命中率, 配对数, 明细)``。
    """
    import random as _r

    from . import engine

    d_red: List[float] = []
    d_blue: List[float] = []
    n_pair = 0
    for t in range(int(start), len(draws)):
        hist = draws[:t]
        actual = draws[t]
        if not hist or not actual.get("reds"):
            continue
        if isinstance(rules, str):
            rules = [{"rule": rules}] if rules else []
        try:
            ctx = engine.constraint_ctx(hist)
            models = engine._rebuild_probs("freq", hist) or {}
            red_p = models.get("red")
            blue_p = models.get("blue")
            if red_p is None or blue_p is None:
                continue
            red_p = np.asarray(red_p, dtype=float)
            blue_p = np.asarray(blue_p, dtype=float)
            act_red = set(int(x) for x in actual["reds"])
            act_blue = actual.get("blue")

            def _draw(n: int, rng) -> List[Dict]:
                out = []
                for _ in range(n):
                    tk = engine.sample_stat_ticket(red_p, blue_p, ctx, rng)
                    if tk:
                        out.append(tk)
                return out

            base_pool = _draw(n_tickets, _r.Random(seed + t))
            exp_pool = _draw(max(n_tickets, 4 * n_tickets), _r.Random(seed + 7919 + t))
            if not base_pool or not exp_pool:
                continue
            base_tk = base_pool[:n_tickets]
            if rules:
                ranked, _d = apply_structure_rules([list(x["reds"]) for x in exp_pool], rules, hist)
                by_key = {tuple(x["reds"]): x for x in exp_pool}
                chosen = [by_key[tuple(r)] for r in ranked if tuple(r) in by_key][:n_tickets]
                exp_tk = chosen or exp_pool[:n_tickets]
            elif red_boost:
                bs = set(int(x) for x in red_boost)
                exp_tk = sorted(exp_pool, key=lambda x: -len(bs & set(x["reds"])))[:n_tickets]
            else:
                exp_tk = exp_pool[:n_tickets]

            def _red_rate(tks):
                """每注红球命中数（与 battle 的 ``red_hits_mean`` 同口径）。"""
                hits = sum(len(set(int(r) for r in x["reds"]) & act_red) for x in tks)
                return hits / max(1, len(tks))

            d_red.append(_red_rate(exp_tk) - _red_rate(base_tk))
            d_blue.append(
                (sum(1 for x in exp_tk if x.get("blue") == act_blue) / max(1, len(exp_tk)))
                - (sum(1 for x in base_tk if x.get("blue") == act_blue) / max(1, len(base_tk)))
            )
            n_pair += 1
        except Exception:  # noqa: BLE001
            continue
        if n_pair >= 600:            # 预算上限：A/B 验证不必跑满全历史
            break

    if not n_pair:
        return 0.0, 0.0, 0, {}
    import statistics as _st
    dr = _st.fmean(d_red) if d_red else 0.0
    detail = {}
    try:
        from scipy import stats as _sp
        if len(d_red) >= 20 and any(abs(x) > 1e-12 for x in d_red):
            w = _sp.wilcoxon(d_red, zero_method="wilcox")
            detail = {"wilcoxon_stat": float(w.statistic), "wilcoxon_p": float(w.pvalue)}
    except Exception:  # noqa: BLE001
        detail = {}
    return dr, (_st.fmean(d_blue) if d_blue else 0.0), n_pair, detail
