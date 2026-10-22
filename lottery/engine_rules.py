"""S5 规律出号与规则应用（V10）。

## 迁移说明（D-02 拆分 S5）

内容从 `lottery/engine.py` 原 128–335 行**逐字迁移**，逻辑未改一行。
迁出：`_regime_rules_for_prompt` / `_last_feedback` / `_rules_hit` /
`_norm_sorted_reds` / `_rule_matches_ticket` / `_rule_noise_explainer` /
`apply_rules_to_candidates`。

## 诚实边界（原文保留）

`apply_rules_to_candidates` 做的是**在已生成的候选池上做加权/筛选**。
规则来自历史统计（重号、连号、区间分布等），**它们不改变开奖随机性** ——
规则命中率高低只反映「历史形态偏好」，不是「未来更可能」。
本项目 V14 的结论是：**最强的规律候选经多重检验校正后 p=0.2995，不显著。**
"""
from __future__ import annotations

import random
from typing import Dict, Iterable, List, Optional

import numpy as np

from . import config      # noqa: F401
from .engine_base import R_MAX, B_MAX, TICKET_COST  # noqa: F401  纯常量，无循环





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
